"""Composition of the THY, MIRA and Gate runtime boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from thymira.core.execution_review import (
    execution_gate_state,
    execution_state_allows_thy,
    persist_execution_gate_state,
)
from thymira.core.governance_binding import final_governance_binding
from thymira.core.graph.state import RuntimeState
from thymira.core.rework import REWORK_TARGETS, rework_signal_from_decision
from thymira.core.run_state import RunTransitionKind
from thymira.events import canonical_json, sha256_text
from thymira.observability import evidence, guardrail, phase
from thymira.policies import UnknownApprovalError, decision_from_events, pending_approvals
from thymira.schemas import (
    Actor,
    ActorKind,
    Decision,
    EventType,
    RunCondition,
    RunStage,
    WaitReason,
    approval_names_decision,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from thymira.core.control_plane import RunController
    from thymira.core.graph.protocol import Subgraph, SubgraphDeps


_GRAPH_NODES: tuple[str, ...] = (
    "start",
    "interview",
    "preflight",
    "classify",
    "execution_gate",
    "thy",
    "mira",
    "gate",
    "complete",
)
_GRAPH_EDGES: tuple[tuple[str, str], ...] = (
    (START, "start"),
    ("start", "interview"),
    ("classify", "execution_gate"),
    ("mira", "gate"),
    ("complete", END),
)
_CONDITIONAL_EDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("interview", ("preflight", END)),
    ("preflight", ("classify", END)),
    ("execution_gate", ("thy", END)),
    ("thy", ("mira", END)),
    ("gate", ("thy", "complete", END)),
)


def graph_definition_hash(thy: Subgraph, mira: Subgraph) -> str:
    """Return a stable hash for the composed graph and both child definitions."""
    definition = {
        "nodes": list(_GRAPH_NODES),
        "edges": [list(edge) for edge in _GRAPH_EDGES],
        "conditional_edges": [[source, list(targets)] for source, targets in _CONDITIONAL_EDGES],
        "thy": {"name": thy.name, "version": thy.graph_version()},
        "mira": {"name": mira.name, "version": mira.graph_version()},
    }
    return sha256_text(canonical_json(definition))


def build_runtime_graph(
    thy: Subgraph,
    mira: Subgraph,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    deps: SubgraphDeps | None = None,
    controller: RunController | None = None,
    interview: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None = None,
    preflight: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None = None,
    classify: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None = None,
    execution_gate: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None = None,
    assurance: Callable[[RuntimeState, SubgraphDeps], None] | None = None,
) -> CompiledStateGraph:
    """Build and compile intake -> governance preflight -> classification -> THY -> MIRA -> Gate.

    ``deps`` supplies the explicit handles passed to both child subgraphs. It is optional at
    build time so callers can inspect or compose the graph before runtime wiring; invoking a
    graph without it raises a clear error. ``controller`` is optional for pure graph tests and,
    when supplied, persists lifecycle transitions through the single Run writer. ``assurance`` is
    the MIRA-owned assurance-bundle writer invoked once the Run is terminal, so completing a Run
    produces the bundle and no read surface has to build it.
    """
    builder = StateGraph(RuntimeState)
    nodes = _RuntimeGraphNodes(
        thy, mira, deps, controller, interview, preflight, classify, execution_gate, assurance
    )
    builder.add_node("start", nodes.start)
    builder.add_node("interview", nodes.run_interview)
    builder.add_node("preflight", nodes.run_preflight)
    builder.add_node("classify", nodes.run_classification)
    builder.add_node("execution_gate", nodes.run_execution_gate)
    builder.add_node("thy", nodes.run_thy)
    builder.add_node("mira", nodes.run_mira)
    builder.add_node("gate", nodes.review)
    builder.add_node("complete", nodes.complete)
    for source, target in _GRAPH_EDGES:
        builder.add_edge(source, target)
    builder.add_conditional_edges("interview", nodes.route_after_interview)
    builder.add_conditional_edges("preflight", nodes.route_after_preflight)
    builder.add_conditional_edges("execution_gate", nodes.route_after_execution_gate)
    builder.add_conditional_edges("thy", nodes.route_after_thy)
    builder.add_conditional_edges("gate", nodes.route)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True, slots=True)
class _RuntimeGraphNodes:
    """Bind composition dependencies to the LangGraph node functions.

    Each lifecycle advance is guarded by the Run's current stage so a node is idempotent to
    re-execution. Under at-least-once queue delivery (RA-CORE-11) a worker may re-enter a node
    after a crash -- the run store commits a transition eagerly within a node, but the LangGraph
    checkpoint commits only at the node boundary, so a mid-node crash can leave the run store one
    transition ahead of the checkpoint. Advancing only from the expected stage lets a redelivered
    task resume cleanly instead of re-applying an already-committed transition.
    """

    thy: Subgraph
    mira: Subgraph
    deps: SubgraphDeps | None
    controller: RunController | None
    interview: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None
    preflight: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None
    classify: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None
    execution_gate: Callable[[RuntimeState, SubgraphDeps], RuntimeState] | None
    assurance: Callable[[RuntimeState, SubgraphDeps], None] | None

    def start(self, state: RuntimeState) -> dict[str, Any]:
        """Record the transition from creation to planning when persistence is wired."""
        if self.controller is not None and (
            self.controller.current_state(state.run_id).stage is RunStage.CREATED
        ):
            self.controller.advance(state.run_id, RunTransitionKind.START)
        return state.model_dump(mode="python")

    def run_thy(self, state: RuntimeState) -> dict[str, Any]:
        """Invoke THY, persist the execution stage it enters, and park a pass that stopped.

        Only the shared runtime state reaches the next node. Both lifecycle writes are guarded so
        a redelivered node re-applies neither: `BEGIN_EXECUTION` only from `PLANNING`, and the
        park only while the Run is not already waiting. A pass that hands back a `ThyProgress`
        stopped on a tool call awaiting a human, and `route_after_thy` then ends the turn.
        """
        deps = _require_deps(self.deps)
        if self.controller is not None:
            deps = replace(
                deps,
                before_thy_execute=partial(self._begin_execution, state.run_id),
            )
        with phase("thy"):
            updated = self.thy.invoke(state, deps=deps)
        _validate_identity(state, updated, "THY")
        if state.rework_signal is not None:
            updated = updated.model_copy(update={"rework_signal": None})
        if self.controller is not None:
            # Compatibility fallback for synthetic Subgraph implementations that predate the
            # Plan -> Execute boundary hook. The real ThySubgraph calls this before any work.
            self._begin_execution(updated.run_id)
            if updated.thy_progress is not None:
                self._park_for_tool_review(updated.run_id)
        return updated.model_dump(mode="python")

    def _begin_execution(self, run_id: str) -> None:
        """Persist THY's Plan -> Execute boundary idempotently through the sole Run writer."""
        assert self.controller is not None  # noqa: S101  # installed only when controller exists
        if self.controller.current_state(run_id).stage is RunStage.PLANNING:
            self.controller.advance(run_id, RunTransitionKind.BEGIN_EXECUTION)

    def route_after_thy(self, state: RuntimeState) -> str:
        """End this graph turn while a tool call THY made waits for a human's answer.

        Read from the persisted Run state, like `route_after_interview`, never from the graph
        state: the controller is the only writer of the wait. MIRA audits the pass that
        finishes, not a knowingly incomplete one it would have to invalidate on the resume.
        """
        if self.controller is None:
            return "mira"
        current = self.controller.current_state(state.run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason is WaitReason.APPROVAL:
            return END
        return "mira"

    def _park_for_tool_review(self, run_id: str) -> None:
        """Park the Run on the tool-call review THY stopped at, through the single Run writer.

        The decision is rebuilt from its own `policy.decision` event, as
        `RunService._pending_approval` does, and `park_for_review` re-checks that record. Never
        through `MiraControlPlane.handle(REQUEST_APPROVAL)`: under the shipped policy that action
        itself requires an approval first (`GOV-006`), so the Run would never park.

        A review the log cannot back becomes a `RuntimeError`, like the one below: an
        `UnknownApprovalError` is a `LookupError`, and a lookup miss is not what happened here --
        the log is structurally incomplete. `RunService._pending_approval` narrows the same
        failure on the same events the same way, so the two paths report it identically.
        (`InlineDispatcher` now turns *any* exception into a recorded `run.failed`, so this
        narrowing is about naming the failure, no longer about reaching the boundary at all.)
        """
        assert self.controller is not None  # noqa: S101  # guarded by the caller
        current = self.controller.current_state(run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason is WaitReason.APPROVAL:
            return  # a redelivered node: the park is already persisted
        events = _require_deps(self.deps).event_log.events()
        for pending in pending_approvals(events):
            try:
                decision = decision_from_events(events, pending.decision_id)
            except UnknownApprovalError as exc:
                raise RuntimeError(
                    f"run {run_id}: pending review {pending.decision_id} has no valid "
                    "policy.decision record"
                ) from exc
            if decision.subject_kind == "tool_call":
                self.controller.park_for_review(run_id, decision)
                return
        raise RuntimeError(
            f"run {run_id}: THY reported a task awaiting approval, but no tool-call review "
            "is pending"
        )

    def run_interview(self, state: RuntimeState) -> dict[str, Any]:
        """Collect a missing activity fact, stopping before any THY work when one is requested."""
        if self.interview is None:
            return state.model_dump(mode="python")
        updated = self.interview(state, _require_deps(self.deps))
        _validate_identity(state, updated, "risk interview")
        return updated.model_dump(mode="python")

    def route_after_interview(self, state: RuntimeState) -> str:
        """End this graph turn while the policy-gated intake waits for a human response."""
        if self.controller is None:
            return "preflight"
        current = self.controller.current_state(state.run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason in {
            WaitReason.INFORMATION,
            WaitReason.APPROVAL,
        }:
            return END
        return "preflight"

    def run_preflight(self, state: RuntimeState) -> dict[str, Any]:
        """Run the optional governance preflight before THY creates execution evidence."""
        if self.preflight is None:
            return state.model_dump(mode="python")
        with phase("mira") as observation:
            with evidence("mira-preflight"):
                updated = self.preflight(state, _require_deps(self.deps))
            observation.update(output={"findings": 0})
        _validate_identity(state, updated, "MIRA preflight")
        return updated.model_dump(mode="python")

    def route_after_preflight(self, state: RuntimeState) -> str:
        """End this graph turn while MIRA's preflight waits for more context.

        Found live: preflight can request human context on an uncertain inherent-risk judgement
        (`MiraSubgraph._request_human_context`) and durably park the Run through the control
        plane, but this edge used to be unconditional -- the graph ran `classify` and `thy`
        regardless, reaching real evidence and a real Policy Engine decision in the same pass
        while the Run's persisted state stayed `waiting: information` underneath it. That state
        then outlives the pass: neither `resume` (refuses outright on an information wait) nor
        `answer` (there is no pending risk-interview question to answer -- this park carries no
        `activity_profile.questioned` event) can ever clear it, so the Run is stuck for good. This
        mirrors `route_after_interview`, reading the same persisted wait reason.
        """
        if self.controller is None:
            return "classify"
        current = self.controller.current_state(state.run_id).state
        if current.condition is RunCondition.WAITING and current.wait_reason in {
            WaitReason.INFORMATION,
            WaitReason.APPROVAL,
        }:
            return END
        return "classify"

    def run_classification(self, state: RuntimeState) -> dict[str, Any]:
        """Classify the MIRA-profiled risk before THY may create tool execution evidence."""
        if self.classify is None:
            return state.model_dump(mode="python")
        updated = self.classify(state, _require_deps(self.deps))
        _validate_identity(state, updated, "risk classification")
        return updated.model_dump(mode="python")

    def run_mira(self, state: RuntimeState) -> dict[str, Any]:
        """Invoke MIRA after entering the audit stage."""
        if self.controller is not None and (
            self.controller.current_state(state.run_id).stage
            in (RunStage.EXECUTING, RunStage.EXPERIMENTING)
        ):
            self.controller.advance(state.run_id, RunTransitionKind.BEGIN_AUDIT)
        runtime_deps = _require_deps(self.deps)
        event_count = len(runtime_deps.event_log.events())
        with phase("mira") as observation:
            with evidence("deterministic-controls"):
                pass
            updated = self.mira.invoke(state, deps=runtime_deps)
            observation.update(output={"findings": len(updated.findings)})
        _validate_identity(state, updated, "MIRA")
        # The production MiraSubgraph records the full report through MiraAuditFlow. Keep this
        # defensive fallback for lightweight Subgraph implementations that only return findings.
        if not any(
            event.type is EventType.AUDIT_COMPLETED
            for event in runtime_deps.event_log.events()[event_count:]
        ):
            runtime_deps.event_log.append(
                EventType.AUDIT_COMPLETED,
                Actor.system(),
                {"status": "completed"},
                subject_id=state.run_id,
                producer="thymira.core",
                producer_version="0.1",
            )
        return updated.model_dump(mode="python")

    def run_execution_gate(self, state: RuntimeState) -> dict[str, Any]:
        """Obtain and persist the Policy Engine's authorised constraints before THY starts."""
        if self.execution_gate is None:
            return state.model_dump(mode="python")
        runtime_deps = _require_deps(self.deps)
        updated = execution_gate_state(state, runtime_deps, self.execution_gate)
        _validate_identity(state, updated, "execution Gate")
        persist_execution_gate_state(self.controller, updated, runtime_deps)
        return updated.model_dump(mode="python")

    def route_after_execution_gate(self, state: RuntimeState) -> str:
        """Never invoke THY when the pre-execution decision is not executable."""
        if self.execution_gate is None:
            return "thy"
        allowed = execution_state_allows_thy(self.controller, state, _require_deps(self.deps))
        return "thy" if allowed else END

    def review(self, state: RuntimeState) -> dict[str, Any]:
        """Ask the Core Gate for one findings decision and persist its consequence."""
        runtime_deps = _require_deps(self.deps)
        if any(finding.run_id != state.run_id for finding in state.findings):
            raise ValueError("MIRA findings must target the composed Run")
        usage_snapshot = runtime_deps.usage_ledger.snapshot()
        with guardrail("policy-review-findings") as observation:
            decision = self._findings_decision(state, usage_snapshot)
            observation.update(
                output={"decision": decision.decision.value, "findings": len(state.findings)}
            )
        if self.controller is not None:
            return self._apply_review(state, decision, usage_snapshot)
        return state.model_copy(update={"usage": usage_snapshot, "decision": decision}).model_dump(
            mode="python"
        )

    def _findings_decision(self, state: RuntimeState, usage: dict[str, int | float | None]) -> Any:
        """Reuse an approved checkpoint decision, otherwise ask the single Core Gate."""
        runtime_deps = _require_deps(self.deps)
        if state.decision is not None and self._approval_recorded(state.decision.id):
            return state.decision
        return runtime_deps.gate.review_findings(state.findings, cost_so_far=usage)

    def _apply_review(
        self, state: RuntimeState, decision: Any, usage: dict[str, int | float | None]
    ) -> dict[str, Any]:
        """Apply a findings decision, including the Core-owned rework route."""
        assert self.controller is not None  # noqa: S101  # caller guards this method
        if decision.decision is Decision.BLOCK:
            binding = final_governance_binding(
                state.run_id, decision, _require_deps(self.deps).event_log.events(), state.findings
            )
            self.controller.advance(state.run_id, RunTransitionKind.BLOCK, payload=binding)
            return self._review_state(state, decision, usage)
        if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
            approval_outcome = self._approval_outcome(decision.id)
            if approval_outcome is False:
                binding = final_governance_binding(
                    state.run_id,
                    decision,
                    _require_deps(self.deps).event_log.events(),
                    state.findings,
                )
                self.controller.advance(state.run_id, RunTransitionKind.BLOCK, payload=binding)
                return self._review_state(state, decision, usage)
            if approval_outcome is None:
                self.controller.park_for_review(state.run_id, decision)
                return self._review_state(state, decision, usage)
        translation = rework_signal_from_decision(
            decision,
            state.findings,
            reopen_count=state.reopen_count,
            max_reopens=state.max_reopens,
            approved=decision.decision is Decision.REQUIRE_HUMAN_REVIEW,
        )
        if translation.signal is not None and not self._has_no_progress(state):
            self._start_rework(state, decision, translation.signal)
            return self._rework_state(state, translation.signal)
        if translation.signal is not None:
            decision = self._escalate_rework(
                state, decision, "rework produced no measurable progress"
            )
        elif translation.reason and self._should_escalate(state, translation.reason, decision):
            decision = self._escalate_rework(state, decision, translation.reason)
        else:
            binding = final_governance_binding(
                state.run_id, decision, _require_deps(self.deps).event_log.events(), state.findings
            )
            self._complete_run(state.run_id, binding)
        return self._review_state(state, decision, usage)

    @staticmethod
    def _review_state(
        state: RuntimeState, decision: Any, usage: dict[str, int | float | None]
    ) -> dict[str, Any]:
        """Return the checkpointable findings decision and usage snapshot."""
        return state.model_copy(update={"usage": usage, "decision": decision}).model_dump(
            mode="python"
        )

    def route(self, state: RuntimeState) -> str:
        """Continue only for decisions that permit successful completion."""
        if state.rework_signal is not None:
            return "thy"
        decision = state.decision
        if decision is None:
            raise RuntimeError("runtime graph Gate node did not produce a policy decision")
        if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
            if decision.rule_id == "rework_escalation":
                return END
            if self._approval_recorded(decision.id):
                return "complete"
            return END
        return "complete" if decision.decision in (Decision.PASS, Decision.WARNING) else END

    def complete(self, state: RuntimeState) -> dict[str, Any]:
        """Persist completion after a passing or approved Gate decision."""
        if self.controller is not None:
            current = self.controller.current_state(state.run_id)
            if current.stage is not RunStage.REPORTING:
                self.controller.advance(state.run_id, RunTransitionKind.BEGIN_REPORTING)
            if self.controller.current_state(state.run_id).condition is not RunCondition.TERMINAL:
                self.controller.advance(state.run_id, RunTransitionKind.COMPLETE)
                runtime_deps = _require_deps(self.deps)
                if not any(
                    event.type is EventType.RUN_COMPLETED
                    for event in runtime_deps.event_log.events()
                ):
                    runtime_deps.event_log.append(
                        EventType.RUN_COMPLETED,
                        Actor.system(),
                        subject_id=state.run_id,
                        producer="thymira.core",
                        producer_version="0.1",
                    )
            self._write_assurance(state)
        return state.model_dump(mode="python")

    def _write_assurance(self, state: RuntimeState) -> None:
        """Produce the assurance bundle once the Run is terminal, never on a read.

        The writer is idempotent: it rebuilds from the same recorded evidence and replaces the
        export in place, so a redelivered ``complete`` node writes the identical bundle.
        """
        if self.assurance is None:
            return
        self.assurance(state, _require_deps(self.deps))

    def _rework_state(self, state: RuntimeState, signal: Any) -> dict[str, Any]:
        """Return the checkpoint state that carries one authorized signal into THY."""
        return state.model_copy(
            update={
                "usage": _require_deps(self.deps).usage_ledger.snapshot(),
                # The signal carries the source decision. Clear the transient Gate result so the
                # next MIRA pass must produce a fresh findings decision.
                "decision": None,
                "rework_signal": signal,
                "reopen_count": state.reopen_count + 1,
                "rework_refusal": None,
            }
        ).model_dump(mode="python")

    def _start_rework(self, state: RuntimeState, decision: Any, signal: Any) -> None:
        """Record the signal and then persist the Run reopening through RunController."""
        runtime_deps = _require_deps(self.deps)
        audit_hash = self._latest_audit_hash()
        runtime_deps.event_log.append(
            EventType.REWORK_STARTED,
            Actor.system(),
            {
                "policy_decision_id": decision.id,
                "finding_ids": list(decision.finding_ids),
                "control_ids": sorted(
                    {
                        finding.control_id
                        for finding in state.findings
                        if finding.id in decision.finding_ids
                    }
                ),
                "reopened_task_ids": list(state.task_ids) or [task.id for task in state.plan],
                "rework_signal": signal.to_json_dict(),
                "audit_fingerprint": self._audit_fingerprint(),
                "source_audit_terminal_hash": audit_hash,
            },
            subject_id=state.run_id,
            causation_id=decision.id,
            producer="thymira.core",
            producer_version="0.1",
        )
        assert self.controller is not None  # noqa: S101  # guarded by the caller
        approval_id = self._approval_id(decision.id)
        self.controller.reopen(
            state.run_id,
            signal,
            reopen_count=state.reopen_count,
            max_reopens=state.max_reopens,
            approval_id=approval_id,
            source_audit_terminal_hash=audit_hash,
        )

    def _escalate_rework(self, state: RuntimeState, decision: Any, reason: str) -> Any:
        """Stop a non-progressing or exhausted correction and ask the Gate for a human."""
        runtime_deps = _require_deps(self.deps)
        escalation = runtime_deps.gate.escalate_rework(
            tuple(decision.finding_ids),
            reason=reason,
        )
        runtime_deps.event_log.append(
            EventType.REWORK_ESCALATED,
            Actor.system(),
            {
                "source_policy_decision_id": decision.id,
                "escalation_policy_decision_id": escalation.id,
                "reason": reason,
                "audit_fingerprint": self._audit_fingerprint(),
            },
            subject_id=state.run_id,
            causation_id=decision.id,
            producer="thymira.core",
            producer_version="0.1",
        )
        assert self.controller is not None  # noqa: S101  # guarded by the caller
        self.controller.park_for_review(state.run_id, escalation)
        return escalation

    def _complete_run(self, run_id: str, binding: dict[str, Any]) -> None:
        """Close a passing Run through the controller and append its terminal fact."""
        runtime_deps = _require_deps(self.deps)
        assert self.controller is not None  # noqa: S101  # guarded by the caller
        self.controller.advance(run_id, RunTransitionKind.BEGIN_REPORTING)
        self.controller.advance(run_id, RunTransitionKind.COMPLETE, payload=binding)
        runtime_deps.event_log.append(
            EventType.RUN_COMPLETED,
            Actor.system(),
            subject_id=run_id,
            producer="thymira.core",
            producer_version="0.1",
        )

    def _latest_audit_hash(self) -> str | None:
        """Return the terminal hash recorded by the latest persisted MIRA report."""
        for event in reversed(_require_deps(self.deps).event_log.events()):
            if event.type is EventType.AUDIT_COMPLETED:
                report = event.payload.get("audit_report")
                if isinstance(report, dict):
                    value = report.get("terminal_hash")
                    return value if isinstance(value, str) else None
        return None

    def _audit_fingerprint(self) -> str:
        """Hash findings, control outcomes and active artifact bytes, excluding event timestamps."""
        runtime_deps = _require_deps(self.deps)
        report: Any = {}
        for event in reversed(runtime_deps.event_log.events()):
            if event.type is EventType.AUDIT_COMPLETED:
                report = event.payload.get("audit_report", {})
                break
        artifacts = [
            (name, artifact.sha256)
            for name, artifact in sorted(runtime_deps.artifact_store.manifest().items())
            if artifact.valid
        ]
        return sha256_text(
            canonical_json(
                {
                    "controls": _stable_audit_entries(
                        report.get("controls", []) if isinstance(report, dict) else []
                    ),
                    "findings": _stable_audit_entries(
                        report.get("findings", []) if isinstance(report, dict) else []
                    ),
                    "artifacts": artifacts,
                }
            )
        )

    def _has_no_progress(self, state: RuntimeState) -> bool:
        """Detect a repeated evidence fingerprint before spending another reopen."""
        fingerprint = self._audit_fingerprint()
        return any(
            event.type is EventType.REWORK_STARTED
            and event.payload.get("audit_fingerprint") == fingerprint
            and any(
                finding.control_id in REWORK_TARGETS
                and finding.control_id in event.payload.get("control_ids", [])
                for finding in state.findings
            )
            for event in _require_deps(self.deps).event_log.events()
        )

    @staticmethod
    def _should_escalate(state: RuntimeState, reason: str, decision: Any) -> bool:
        """Escalate only bounded rework failures, leaving ordinary findings terminal."""
        if not any(
            finding.id in decision.finding_ids and finding.control_id in REWORK_TARGETS
            for finding in state.findings
        ):
            return False
        return "budget exhausted" in reason or "only moves backwards" in reason

    def _approval_id(self, decision_id: str) -> str | None:
        """Return the exact recorded approval id, when the control-plane writer supplied one."""
        for event in reversed(_require_deps(self.deps).event_log.events()):
            if (
                event.type is EventType.HUMAN_APPROVAL
                and approval_names_decision(event.payload, decision_id)
                and event.payload.get("approved") is True
                and ("automatic" not in event.payload or event.payload["automatic"] is False)
                and event.actor.kind is ActorKind.HUMAN
                and event.actor.authenticated
            ):
                value = event.payload.get("id")
                return value if isinstance(value, str) else None
        return None

    def _approval_recorded(self, decision_id: str) -> bool:
        """Return whether an approval for this exact decision was recorded.

        The answer may have been written by either approval writer, under either of the two names
        the decision id carries, so the match goes through
        :func:`~thymira.schemas.approval_names_decision` -- which is also what keeps a payload
        naming no decision from pairing with a decision id that is absent.
        """
        return self._approval_outcome(decision_id) is True

    def _approval_outcome(self, decision_id: str) -> bool | None:
        """Return True/False for an exact human answer, or None while it is unresolved."""
        runtime_deps = _require_deps(self.deps)
        outcomes = [
            event.payload.get("approved")
            for event in runtime_deps.event_log.events()
            if event.type is EventType.HUMAN_APPROVAL
            and approval_names_decision(event.payload, decision_id)
            and ("automatic" not in event.payload or event.payload["automatic"] is False)
            and event.actor.kind is ActorKind.HUMAN
            and event.actor.authenticated
            and isinstance(event.payload.get("approved"), bool)
        ]
        return outcomes[-1] if outcomes else None


def _stable_audit_entries(values: object) -> list[Any]:
    """Normalize generated audit identities before comparing rework progress."""
    if not isinstance(values, list):
        return []
    normalized = [_stable_audit_entry(value) for value in values]
    return sorted(normalized, key=canonical_json)


def _stable_audit_entry(value: object) -> object:
    """Remove audit timestamps and generated identities from one fingerprinted entry."""
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"id", "agent_id", "created_at", "evaluated_at"}:
            continue
        if key == "evidence" and isinstance(item, list):
            normalized[key] = [
                {
                    entry_key: entry_value
                    for entry_key, entry_value in entry.items()
                    if not (entry.get("kind") == "event" and entry_key in {"ref", "sha256"})
                }
                if isinstance(entry, dict)
                else entry
                for entry in item
            ]
            continue
        normalized[key] = item
    return normalized


def _require_deps(deps: SubgraphDeps | None) -> SubgraphDeps:
    """Require explicit runtime handles at invocation time."""
    if deps is None:
        raise RuntimeError("runtime graph invocation requires SubgraphDeps")
    return deps


def _validate_identity(before: RuntimeState, after: RuntimeState, name: str) -> None:
    """Ensure a child graph cannot redirect the composition to another Run or project."""
    if after.run_id != before.run_id or after.project_id != before.project_id:
        raise ValueError(f"{name} subgraph cannot change the Run or project identity")


__all__ = ["build_runtime_graph", "graph_definition_hash"]
