"""Execution-start human review across the real Core graph and local persistence."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from thymira.core import (
    ExecutionDispatchError,
    InlineDispatcher,
    RunApprovalError,
    RunController,
    RunNotAwaitingApprovalError,
    RunService,
    RuntimeState,
    SessionService,
    StateCheckpointer,
    SubgraphDeps,
    build_runtime_graph,
)
from thymira.core.control_plane import RunEventLog
from thymira.core.execution_review import (
    EXECUTION_START_RESUME_TARGET,
    INTERVIEW_LIMIT_RESUME_TARGET,
    review_resume_target,
    reviewed_risk_profile,
)
from thymira.core.run_state import RunProjection
from thymira.core.usage import UsageLedger
from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import (
    ActionRule,
    CapabilityRule,
    Gate,
    LocalApprovalService,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    pending_approvals,
)
from thymira.schemas import (
    EVENT_LOG_FORMAT_VERSION,
    Actor,
    Decision,
    Event,
    EventType,
    ExecutionConstraints,
    PolicyDecision,
    Run,
    RunCondition,
    RunOutcome,
    RunStatus,
    new_id,
)
from thymira.state import (
    LocalArtifactStore,
    LocalCheckpointRepository,
    LocalRecordRepository,
    LocalRunStore,
    LocalSessionRepository,
)
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyProgress
from thymira.tools import (
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
)


class _RecordingSubgraph:
    """A deterministic THY or MIRA boundary that records invocations."""

    def __init__(self, name: str, tool: "_LocalTool | None" = None) -> None:
        self.name = name
        self.calls = 0
        self.tool = tool
        self.tool_results: list[ToolResult] = []

    def graph_version(self) -> str:
        """Return the stable fake graph version."""
        return "v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Record one call and provide MIRA's required completion evidence."""
        self.calls += 1
        if self.tool is not None:
            assert state.risk_profile is not None
            assert state.execution_decision is not None
            execution = deps.tool_manager.execute(
                ToolContext(
                    run_id=state.run_id,
                    agent_id=new_id("agent"),
                    workspace=Path(),
                    event_log=deps.event_log,
                    gate=deps.gate,
                    artifact_store=deps.artifact_store,
                    # Exactly what ThySubgraph.invoke hands the real Tool Manager.
                    risk_profile=reviewed_risk_profile(
                        state.risk_profile, state.execution_decision, deps.event_log.events()
                    ),
                    execution_constraints=state.execution_decision.execution_constraints,
                    execution_decision=state.execution_decision,
                ),
                self.tool.name,
            )
            self.tool_results.append(execution.result)
            state = state.model_copy(
                update={
                    "thy_progress": (
                        ThyProgress(
                            plan=(
                                AgentTask(
                                    id="local-probe",
                                    agent=ThyAgentKind.DATA,
                                    phase=ThyPhase.EXECUTE,
                                    instruction="Run the local probe.",
                                ),
                            )
                        )
                        if execution.pending_approval is not None
                        else None
                    )
                }
            )
        if self.name == "mira":
            prior_events = deps.event_log.events()
            prior_hash = prior_events[-1].hash if prior_events else None
            revision = sum(event.type is EventType.AUDIT_COMPLETED for event in prior_events) + 1
            deps.event_log.append(
                EventType.AUDIT_COMPLETED,
                Actor.system(),
                {
                    "status": "passed",
                    "audit_revision": revision,
                    "audit_report": {
                        "run_id": state.run_id,
                        "status": "passed",
                        "controls": [],
                        "findings": [],
                        "terminal_hash": prior_hash,
                        "policy_sha256": deps.gate.engine.policy_sha256,
                    },
                },
                subject_id=state.run_id,
                producer="test.mira",
            )
        return state


@dataclass(frozen=True, slots=True)
class _LocalTool(Tool):
    """A registered no-I/O tool proving the approved path reaches ToolManager."""

    name: str = "local_probe"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="local_probe", external_effects=())
    )
    description: str = "Record an in-process invocation."
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue
    calls: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Record the authorised invocation."""
        del arguments
        self.calls.append(invocation)
        return ToolResult(success=True, stdout="ok", value=LegacyToolValue(text="ok"))


@dataclass(frozen=True)
class _ReviewRun:
    """Collaborators for one local execution-review boundary."""

    service: RunService
    run: Run
    gate: Gate
    controller: RunController
    log: RunEventLog
    thy: _RecordingSubgraph
    mira: _RecordingSubgraph
    tool: _LocalTool
    store: LocalRunStore
    dispatcher: InlineDispatcher
    execution_gate_calls: list[None]
    dispatch_error: ExecutionDispatchError | None


def _review_tools(tool: _LocalTool) -> ToolManager:
    """Include CR-001's training consumer while THY calls the otherwise permissive probe."""
    training = _LocalTool(
        name="training_probe",
        capability=ToolCapability(id="training_probe", risk_tags=("model_training",)),
    )
    return ToolManager(ToolRegistry((tool, training)))


class _CrashAfterParkController(RunController):
    """Fail once after the Run transition commits but before the node checkpoint commits."""

    crashed = False

    def park_for_review(
        self,
        run_id: str,
        decision: PolicyDecision,
        *,
        resume_from: str | None = None,
    ) -> tuple[Event, RunProjection]:
        """Persist the park, then emulate one worker crash."""
        result = super().park_for_review(run_id, decision, resume_from=resume_from)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("failpoint after execution-review park")
        return result


def _rebuilt_service(
    review: _ReviewRun, tmp_path: Path
) -> tuple[RunService, _RecordingSubgraph, _RecordingSubgraph, _LocalTool, list[None]]:
    """Reopen persistence and rebuild every dispatcher dependency for an approved resume."""
    store = LocalRunStore(tmp_path / "runs")
    tool = _LocalTool()
    tool_manager = _review_tools(tool)
    thy = _RecordingSubgraph("thy", tool)
    mira = _RecordingSubgraph("mira")
    execution_gate_calls: list[None] = []

    def graph_factory(run: Run):
        log = RunEventLog(store, run.id)
        deps = SubgraphDeps(
            event_log=log,
            gate=Gate(PolicyEngine(review.gate.engine.policy), log),
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
            tool_manager=tool_manager,
            record_repository=LocalRecordRepository(tmp_path / "records"),
            usage_ledger=UsageLedger(),
        )

        def execution_gate(state: RuntimeState, graph_deps: SubgraphDeps) -> RuntimeState:
            execution_gate_calls.append(None)
            assert state.risk_profile is not None
            decision = graph_deps.gate.authorize_execution(
                state.risk_profile,
                graph_deps.tool_manager.capabilities(),
            )
            return state.model_copy(
                update={
                    "execution_constraints": decision.execution_constraints,
                    "execution_decision": decision,
                }
            )

        return build_runtime_graph(
            thy,
            mira,
            execution_gate=execution_gate,
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=RunController(store),
        )

    dispatcher = InlineDispatcher(store, graph_factory)
    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=dispatcher,
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
    )
    return service, thy, mira, tool, execution_gate_calls


def _review_run(
    tmp_path: Path,
    *,
    start_decision: Decision = Decision.REQUIRE_HUMAN_REVIEW,
    synchronous_answer: bool | None = None,
    crash_after_park: bool = False,
    constraint_review: bool = False,
    missing_execution_decision: bool = False,
    uncertain_classification: bool = False,
) -> _ReviewRun:
    """Build one real local Run whose execution.start policy requires a human review.

    With ``uncertain_classification`` the start rule itself passes and the review comes from the
    engine's own fail-safe escalation of a classification flagged for human review.
    """
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="analyse",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    policy = Policy(
        name="execution-review",
        version="1.0",
        action_rules=(
            ActionRule(
                id="TEST-EXECUTION-REVIEW",
                action_types=("execution.start",),
                decision=Decision.PASS if uncertain_classification else start_decision,
                reason="A human must approve execution.",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="TEST-LOCAL-TOOL",
                decision=Decision.PASS,
                reason="The local probe is allowed.",
                external_effects=(),
                execution_constraints=ExecutionConstraints(),
            ),
        ),
    )
    if constraint_review:
        # A run-wide review constraint of the test's own: the shipped CR-001 reviews the training
        # call itself and deliberately carries no such constraint any more.
        policy = policy.model_copy(
            update={
                "capability_rules": (
                    CapabilityRule(
                        id="TEST-CONSTRAINT-REVIEW",
                        risk_tags=("model_training",),
                        risk_factors=("sensitive_attributes",),
                        decision=Decision.REQUIRE_HUMAN_REVIEW,
                        reason="Training on sensitive data is reviewed for the whole Run.",
                        execution_constraints=ExecutionConstraints(requires_human_review=True),
                    ),
                    *policy.capability_rules,
                )
            }
        )
    engine = PolicyEngine(policy)
    tool = _LocalTool()
    tool_manager = _review_tools(tool)
    thy = _RecordingSubgraph("thy", tool)
    mira = _RecordingSubgraph("mira")
    controller = _CrashAfterParkController(store) if crash_after_park else RunController(store)
    execution_gate_calls: list[None] = []

    def graph_factory(_run: Run):
        log = RunEventLog(store, run.id)
        execution_gate = (
            Gate(engine, log)
            if synchronous_answer is None
            else Gate(
                engine,
                log,
                approver=lambda _request: synchronous_answer,
                human=Actor(kind="human", id="reviewer", authenticated=True),
            )
        )
        deps = SubgraphDeps(
            event_log=log,
            gate=execution_gate,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
            tool_manager=tool_manager,
            record_repository=LocalRecordRepository(tmp_path / "records"),
            usage_ledger=UsageLedger(),
        )

        def classify(state: RuntimeState, _deps: SubgraphDeps) -> RuntimeState:
            return state.model_copy(
                update={
                    "risk_profile": RiskProfile(
                        risk_level="medium",
                        activity_category="credit_risk_analysis",
                        confidence=0.9,
                        needs_human_review=uncertain_classification,
                        risk_factors=("sensitive_attributes",) if constraint_review else (),
                    )
                }
            )

        def execution_gate(state: RuntimeState, graph_deps: SubgraphDeps) -> RuntimeState:
            execution_gate_calls.append(None)
            if missing_execution_decision:
                return state
            assert state.risk_profile is not None
            decision = graph_deps.gate.authorize_execution(
                state.risk_profile,
                graph_deps.tool_manager.capabilities(),
            )
            return state.model_copy(
                update={
                    "execution_constraints": decision.execution_constraints,
                    "execution_decision": decision,
                }
            )

        return build_runtime_graph(
            thy,
            mira,
            classify=classify,
            execution_gate=execution_gate,
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=controller,
        )

    dispatcher = InlineDispatcher(store, graph_factory)
    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=dispatcher,
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
    )
    dispatch_error = None
    try:
        dispatcher.submit(run.id)
    except ExecutionDispatchError as exc:
        if not (crash_after_park or missing_execution_decision):
            raise
        dispatch_error = exc
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    approval_gate = Gate(
        engine,
        RunEventLog(store, run.id),
        approver=lambda _request: True,
        human=reviewer,
    )
    return _ReviewRun(
        service,
        run,
        approval_gate,
        RunController(store),
        RunEventLog(store, run.id),
        thy,
        mira,
        tool,
        store,
        dispatcher,
        execution_gate_calls,
        dispatch_error,
    )


def test_execution_review_parks_before_thy(tmp_path: Path) -> None:
    """A pending execution.start review stops all orchestrator work."""
    review = _review_run(tmp_path)

    state = review.controller.current_state(review.run.id)

    assert state.condition is RunCondition.WAITING
    assert review.thy.calls == 0
    assert review.mira.calls == 0
    assert review.tool.calls == []
    pending = pending_approvals(review.log.events())[0]
    parked = next(
        event
        for event in review.log.events()
        if event.type is EventType.RUN_TRANSITIONED
        and event.payload.get("command") == "wait_for_approval"
    )
    assert parked.payload["resume_from"] == "execution_start"
    assert parked.payload["policy_decision"]["id"] == pending.decision_id


@pytest.mark.parametrize(
    ("answer", "expected_status", "expected_calls"),
    [(True, RunStatus.COMPLETED, 1), (False, RunStatus.BLOCKED, 0)],
)
def test_synchronous_execution_review_applies_answer_before_routing(
    tmp_path: Path,
    *,
    answer: bool,
    expected_status: RunStatus,
    expected_calls: int,
) -> None:
    """A Gate answer recorded in the execution node never leaves a waiting zombie."""
    review = _review_run(tmp_path, synchronous_answer=answer)

    assert review.service.get_run(review.run.id).status is expected_status
    assert len(review.tool.calls) == expected_calls


def test_blocked_execution_start_is_terminal_before_thy(tmp_path: Path) -> None:
    """An upfront BLOCK becomes a terminal Run without invoking THY."""
    review = _review_run(tmp_path, start_decision=Decision.BLOCK)

    state = review.controller.current_state(review.run.id)
    assert state.condition is RunCondition.TERMINAL
    assert state.outcome is RunOutcome.BLOCKED
    assert review.tool.calls == []


def test_execution_gate_without_decision_fails_before_thy(tmp_path: Path) -> None:
    """A configured execution Gate cannot omit its decision and release execution."""
    review = _review_run(tmp_path, missing_execution_decision=True)

    assert review.dispatch_error is not None
    assert "execution Gate did not record an execution decision" in str(review.dispatch_error)
    assert review.thy.calls == 0
    assert review.mira.calls == 0
    assert review.tool.calls == []


def test_approved_execution_review_resumes_into_thy(tmp_path: Path) -> None:
    """A rebuilt service releases the preserved decision exactly once after approval."""
    review = _review_run(tmp_path)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    rebuilt, thy, mira, tool, execution_gate_calls = _rebuilt_service(review, tmp_path)

    resumed = rebuilt.resolve_approval(review.run.id, gate=review.gate, actor=reviewer)
    redelivered = rebuilt.resume(review.run.id, actor=reviewer)

    assert resumed.status is RunStatus.COMPLETED, resumed.error
    assert redelivered.status is RunStatus.COMPLETED
    assert thy.calls == 1
    assert len(tool.calls) == 1
    assert mira.calls == 1
    assert execution_gate_calls == []
    assert (
        sum(
            event.type is EventType.POLICY_DECISION
            and event.payload.get("rule_id") == "TEST-EXECUTION-REVIEW"
            for event in review.log.events()
        )
        == 1
    )


def test_redelivery_after_persisted_park_reuses_execution_decision(tmp_path: Path) -> None:
    """Checkpoint-lag redelivery cannot bypass the RunService resume transition."""
    review = _review_run(tmp_path, crash_after_park=True)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    LocalApprovalService(review.log).resolve(
        review.run.id,
        decision_id,
        approved=True,
        by=Actor(kind="human", id="reviewer", authenticated=True),
    )

    review.dispatcher.resume(review.run.id)

    assert len(review.execution_gate_calls) == 1
    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.thy.calls == 0
    assert review.tool.calls == []
    assert (
        sum(
            event.type is EventType.POLICY_DECISION
            and event.payload.get("rule_id") == "TEST-EXECUTION-REVIEW"
            for event in review.log.events()
        )
        == 1
    )

    resolved = review.service.resume(
        review.run.id,
        actor=Actor(kind="human", id="reviewer", authenticated=True),
    )

    assert resolved.status is RunStatus.COMPLETED
    assert review.thy.calls == 1
    assert len(review.tool.calls) == 1


def test_declared_unauthenticated_human_cannot_resolve_execution_review(tmp_path: Path) -> None:
    """A self-declared human identity cannot answer an execution-start review."""
    review = _review_run(tmp_path)
    reviewer = Actor(kind="human", id="cli-reviewer", authenticated=False)
    gate = Gate(
        review.gate.engine,
        review.log,
        approver=lambda _request: True,
        human=reviewer,
    )

    with pytest.raises(RunApprovalError, match="authenticated"):
        review.service.resolve_approval(review.run.id, gate=gate, actor=reviewer)

    assert review.service.get_run(review.run.id).status is RunStatus.WAITING_FOR_APPROVAL
    assert review.tool.calls == []


def test_rejected_execution_review_blocks_without_thy(tmp_path: Path) -> None:
    """A rejected run-level execution decision is terminal before THY."""
    review = _review_run(tmp_path)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    rejecting_gate = Gate(
        PolicyEngine(
            Policy(
                name="execution-review",
                version="1.0",
                action_rules=(
                    ActionRule(
                        id="TEST-EXECUTION-REVIEW",
                        action_types=("execution.start",),
                        decision=Decision.REQUIRE_HUMAN_REVIEW,
                        reason="A human must approve execution.",
                    ),
                ),
            )
        ),
        review.log,
        approver=lambda _request: False,
        human=reviewer,
    )

    rejected = review.service.resolve_approval(review.run.id, gate=rejecting_gate, actor=reviewer)

    state = review.controller.current_state(review.run.id)
    assert state.condition is RunCondition.TERMINAL
    assert state.outcome is RunOutcome.BLOCKED
    assert rejected.status is RunStatus.BLOCKED
    assert review.thy.calls == 0
    assert review.mira.calls == 0
    assert review.tool.calls == []


@pytest.mark.parametrize("approved", [True, False])
def test_constraint_review_needs_a_separate_tool_answer_after_reconstruction(
    tmp_path: Path, *, approved: bool
) -> None:
    """A run-wide review constraint reviews even a read-only call after start approval."""
    review = _review_run(
        tmp_path,
        start_decision=Decision.PASS,
        constraint_review=True,
    )
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    start_decision = next(
        PolicyDecision.model_validate(event.payload)
        for event in review.log.events()
        if event.type is EventType.POLICY_DECISION and event.payload.get("subject_kind") == "run"
    )

    assert start_decision.execution_constraints.requires_human_review
    review.service.resolve_approval(review.run.id, gate=review.gate, actor=reviewer)

    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.tool.calls == []
    assert not any(event.type is EventType.AUDIT_COMPLETED for event in review.log.events())
    pending = pending_approvals(review.log.events())
    assert len(pending) == 1
    tool_review = pending[0]
    assert tool_review.decision_id != start_decision.id
    assert tool_review.subject_id != review.run.id
    assert tool_review.tool_call is not None
    assert tool_review.tool_call["tool"] == "local_probe"
    assert tool_review.tool_call["arguments"] == {}

    rebuilt, _thy, _mira, tool, _gate_calls = _rebuilt_service(review, tmp_path)
    rebuilt_log = RunEventLog(LocalRunStore(tmp_path / "runs"), review.run.id)
    gate = Gate(
        PolicyEngine(review.gate.engine.policy),
        rebuilt_log,
        approver=lambda _request: approved,
        human=reviewer,
    )
    result = rebuilt.resolve_approval(review.run.id, gate=gate, actor=reviewer)
    redelivered = rebuilt.resume(review.run.id, actor=reviewer)

    assert result.status is RunStatus.COMPLETED, result.error
    assert redelivered.status is RunStatus.COMPLETED
    assert len(tool.calls) == int(approved)
    events = rebuilt_log.events()
    assert verify_events(events).valid
    assert pending_approvals(events) == ()
    starts = [event for event in events if event.type is EventType.TOOL_STARTED]
    assert len(starts) == int(approved)
    if approved:
        assert starts[0].payload["decision_id"] == tool_review.decision_id
    start_records = [
        event.payload
        for event in events
        if event.type is EventType.POLICY_DECISION and event.payload.get("subject_kind") == "run"
    ]
    assert start_records == [start_decision.to_json_dict()]
    controls = {
        control.control_id: control
        for control in audit_run(AuditContext(review.run.id, events)).controls
    }
    assert controls["A3"].status is (
        ControlStatus.PASSED if approved else ControlStatus.NOT_APPLICABLE
    )
    assert controls["A6"].status is ControlStatus.PASSED


def test_automatic_answer_does_not_authorise_execution(tmp_path: Path) -> None:
    """An automatic approval record cannot release the execution checkpoint."""
    review = _review_run(tmp_path)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    automatic_gate = Gate(
        review.gate.engine,
        review.log,
        approver=auto_approve,
        human=reviewer,
    )

    with pytest.raises(RunApprovalError, match="execution approval Gate"):
        review.service.resolve_approval(review.run.id, gate=automatic_gate, actor=reviewer)

    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.tool.calls == []

    resolved = review.service.resolve_approval(
        review.run.id,
        gate=review.gate,
        actor=reviewer,
    )

    assert resolved.status is RunStatus.COMPLETED
    assert len(review.tool.calls) == 1


@pytest.mark.parametrize(
    "invalid_actor",
    [Actor.system(), Actor(kind="agent", id="delegate", authenticated=True)],
)
def test_public_nonhuman_answer_leaves_execution_request_resolvable(
    tmp_path: Path, invalid_actor: Actor
) -> None:
    """RunService rejects a nonhuman resolver before Gate appends a terminal answer."""
    review = _review_run(tmp_path)
    invalid_gate = Gate(
        review.gate.engine,
        review.log,
        approver=lambda _request: True,
        human=invalid_actor,
    )

    with pytest.raises(RunApprovalError, match="declared human actor"):
        review.service.resolve_approval(
            review.run.id,
            gate=invalid_gate,
            actor=invalid_actor,
        )

    assert not any(event.type is EventType.HUMAN_APPROVAL for event in review.log.events())
    resolved = review.service.resolve_approval(
        review.run.id,
        gate=review.gate,
        actor=Actor(kind="human", id="reviewer", authenticated=True),
    )
    assert resolved.status is RunStatus.COMPLETED
    assert len(review.tool.calls) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_out_of_band_execution_answer_uses_the_parked_target(
    tmp_path: Path, *, approved: bool
) -> None:
    """A persisted human answer resumes an approval or blocks a rejection on plain resume."""
    review = _review_run(tmp_path)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    LocalApprovalService(review.log).resolve(
        review.run.id,
        decision_id,
        approved=approved,
        by=Actor(kind="human", id="reviewer", authenticated=True),
    )

    resumed = review.service.resume(
        review.run.id,
        actor=Actor(kind="human", id="reviewer", authenticated=True),
    )

    assert resumed.status is (RunStatus.COMPLETED if approved else RunStatus.BLOCKED)
    assert len(review.tool.calls) == int(approved)


def test_out_of_band_rejection_attributes_the_rejecting_human(tmp_path: Path) -> None:
    """A later resume records the rejection's author rather than the resume caller."""
    review = _review_run(tmp_path)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    alice = Actor(kind="human", id="alice", authenticated=True)
    bob = Actor(kind="human", id="bob", authenticated=True)
    LocalApprovalService(review.log).resolve(
        review.run.id,
        decision_id,
        approved=False,
        by=alice,
    )

    result = review.service.resume(review.run.id, actor=bob)

    blocked = next(
        event
        for event in reversed(review.log.events())
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "block"
    )
    assert result.status is RunStatus.BLOCKED
    assert blocked.payload["rejected_by"] == alice.to_json_dict()
    assert blocked.payload["resumed_by"] == bob.to_json_dict()
    assert review.tool.calls == []


def test_unrelated_answer_does_not_authorise_execution(tmp_path: Path) -> None:
    """A human answer naming another decision leaves the parked execution review pending."""
    review = _review_run(tmp_path)
    review.log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind="human", id="reviewer", authenticated=True),
        {"decision_id": new_id("decision"), "approved": True, "automatic": False},
        subject_id=review.run.id,
    )

    with pytest.raises(RunNotAwaitingApprovalError, match="waiting for human approval"):
        review.service.resume(
            review.run.id,
            actor=Actor(kind="human", id="reviewer", authenticated=True),
        )

    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.tool.calls == []


def test_conflicting_human_answers_keep_execution_rejection_final(tmp_path: Path) -> None:
    """A later approval cannot erase a valid rejection for the same start decision."""
    review = _review_run(tmp_path)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    for approved in (False, True):
        review.log.append(
            EventType.HUMAN_APPROVAL,
            reviewer,
            {"decision_id": decision_id, "approved": approved, "automatic": False},
            subject_id=review.run.id,
        )

    result = review.service.resume(review.run.id, actor=reviewer)

    assert result.status is RunStatus.BLOCKED
    assert review.tool.calls == []


@pytest.mark.parametrize(
    "answering_actor",
    [Actor.system(), Actor(kind="agent", id="reviewer", authenticated=True)],
)
def test_untrusted_answer_does_not_authorise_execution(
    tmp_path: Path, answering_actor: Actor
) -> None:
    """System and agent actors cannot release an execution review."""
    review = _review_run(tmp_path)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    review.log.append(
        EventType.HUMAN_APPROVAL,
        answering_actor,
        {"decision_id": decision_id, "approved": True, "automatic": False},
        subject_id=review.run.id,
    )

    with pytest.raises(RunNotAwaitingApprovalError, match="waiting for human approval"):
        review.service.resume(
            review.run.id,
            actor=Actor(kind="human", id="reviewer", authenticated=True),
        )

    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.tool.calls == []


@pytest.mark.parametrize("approved", [True, False])
def test_declared_unauthenticated_human_answer_does_not_authorise_execution(
    tmp_path: Path, approved: bool
) -> None:
    """A valid chain carrying a declared human answer still leaves execution parked."""
    review = _review_run(tmp_path)
    decision_id = pending_approvals(review.log.events())[0].decision_id
    review.log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind="human", id="claimed-reviewer", authenticated=False),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=review.run.id,
    )

    with pytest.raises(RunNotAwaitingApprovalError, match="waiting for human approval"):
        review.service.resume(
            review.run.id,
            actor=Actor(kind="human", id="reviewer", authenticated=True),
        )

    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING
    assert review.tool.calls == []


def _park_transition(run_id: str, decision_id: str, resume_from: str | None) -> Event:
    """One ``wait_for_approval`` transition naming ``decision_id`` and its resume target."""
    payload: dict[str, Any] = {
        "command": "wait_for_approval",
        "policy_decision": {"id": decision_id},
    }
    if resume_from is not None:
        payload["resume_from"] = resume_from
    return Event(
        event_id=new_id("event"),
        run_id=run_id,
        seq=0,
        type=EventType.RUN_TRANSITIONED,
        schema_version=EVENT_LOG_FORMAT_VERSION,
        actor=Actor.system(),
        producer="test",
        producer_version="1.0",
        payload=payload,
    )


@pytest.mark.parametrize(
    "target",
    [EXECUTION_START_RESUME_TARGET, INTERVIEW_LIMIT_RESUME_TARGET],
)
def test_review_resume_target_returns_every_known_target(target: str) -> None:
    """Both parks that carry a target read back exactly the one they persisted."""
    run_id = new_id("run")
    decision_id = new_id("decision")

    events = [_park_transition(run_id, decision_id, target)]

    assert review_resume_target(events, decision_id) == target


def test_review_resume_target_is_none_for_a_park_without_a_target() -> None:
    """A legacy park carries no target, and callers fall back on the decision's subject."""
    run_id = new_id("run")
    decision_id = new_id("decision")

    events = [_park_transition(run_id, decision_id, None)]

    assert review_resume_target(events, decision_id) is None


def test_review_resume_target_rejects_an_unknown_target() -> None:
    """An unrecognised target is a corrupted or forward-dated log, never a silent Gate resume."""
    run_id = new_id("run")
    decision_id = new_id("decision")

    events = [_park_transition(run_id, decision_id, "somewhere_else")]

    with pytest.raises(ValueError, match="unsupported approval resume target"):
        review_resume_target(events, decision_id)


def _tool_decisions(events: list[Event]) -> list[Event]:
    return [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "tool_call"
    ]


def test_uncertain_classification_parks_once_at_execution_start(tmp_path: Path) -> None:
    """A passing start rule under a flagged classification still stops before any tool."""
    review = _review_run(tmp_path, uncertain_classification=True)

    state = review.controller.current_state(review.run.id)
    pending = pending_approvals(review.log.events())[0]

    assert state.condition is RunCondition.WAITING
    assert review.thy.calls == 0
    assert review.tool.calls == []
    assert "Fail-safe escalation: classification flagged for human review." in pending.reason
    assert pending.summary.startswith(
        "execution.start: risk medium (credit_risk_analysis); factors: none; missing: none; "
        "confidence 0.90; needs_human_review=True"
    )


def test_approving_the_classification_review_releases_tools_without_re_escalation(
    tmp_path: Path,
) -> None:
    """After the one human answer, the tool decision is the rule's own PASS, not a repeat."""
    review = _review_run(tmp_path, uncertain_classification=True)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    rebuilt, thy, _mira, tool, _calls = _rebuilt_service(review, tmp_path)

    resumed = rebuilt.resolve_approval(review.run.id, gate=review.gate, actor=reviewer)

    assert resumed.status is RunStatus.COMPLETED, resumed.error
    assert thy.calls == 1
    assert len(tool.calls) == 1
    decisions = _tool_decisions(review.log.events())
    assert [event.payload["decision"] for event in decisions] == ["PASS"]
    assert "Fail-safe escalation" not in decisions[0].payload["reason"]
    assert len(pending_approvals(review.log.events())) == 0
    assert verify_events(review.log.events()).valid


def test_rejecting_the_classification_review_blocks_the_run(tmp_path: Path) -> None:
    review = _review_run(tmp_path, uncertain_classification=True)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    rejecting_gate = Gate(
        review.gate.engine, review.log, approver=lambda _request: False, human=reviewer
    )

    rejected = review.service.resolve_approval(review.run.id, gate=rejecting_gate, actor=reviewer)

    assert rejected.status is RunStatus.BLOCKED
    assert review.controller.current_state(review.run.id).outcome is RunOutcome.BLOCKED
    assert review.tool.calls == []


def test_automatic_answer_to_the_classification_review_keeps_per_call_escalation(
    tmp_path: Path,
) -> None:
    """An automatic approval is evidence, never the human answer the escalation waits for."""
    review = _review_run(tmp_path, uncertain_classification=True)
    decision = pending_approvals(review.log.events())[0]
    Gate(review.gate.engine, review.log, approver=auto_approve).resolve_pending_approval(
        next(
            PolicyDecision.model_validate(event.payload)
            for event in review.log.events()
            if event.type is EventType.POLICY_DECISION
            and event.payload["id"] == decision.decision_id
        )
    )

    risk = RiskProfile(risk_level="medium", activity_category="x", confidence=0.9)
    parked = next(
        PolicyDecision.model_validate(event.payload)
        for event in review.log.events()
        if event.type is EventType.POLICY_DECISION and event.payload["id"] == decision.decision_id
    )

    assert reviewed_risk_profile(risk, parked, review.log.events()).reviewed_decision_id is None
    assert review.controller.current_state(review.run.id).condition is RunCondition.WAITING


_ESCALATED_REASON = (
    "Execution may start. Fail-safe escalation: classification flagged for human review."
)


def _start_review(
    run_id: str,
    *,
    reason: str = _ESCALATED_REASON,
    subject_kind: Literal["run", "tool_call"] = "run",
) -> PolicyDecision:
    return PolicyDecision(
        id=new_id("decision"),
        run_id=run_id,
        subject_kind=subject_kind,
        subject_id=run_id,
        decision=Decision.REQUIRE_HUMAN_REVIEW,
        rule_id="GOV-007",
        reason=reason,
        policy_name="base@1.0",
        policy_sha256="0" * 64,
    )


def _answered(
    decision: PolicyDecision,
    *,
    actor: Actor,
    approved: bool = True,
    automatic: bool = False,
    decision_id: str | None = None,
) -> list[Event]:
    log = InMemoryEventLog(decision.run_id)
    log.append(
        EventType.HUMAN_APPROVAL,
        actor,
        {
            "decision_id": decision_id or decision.id,
            "approved": approved,
            "automatic": automatic,
        },
        subject_id=decision.subject_id,
    )
    return list(log.events())


_REVIEWER = Actor(kind="human", id="reviewer", authenticated=True)
_UNCERTAIN = RiskProfile(
    risk_level="medium", activity_category="x", confidence=0.9, needs_human_review=True
)


def test_reviewed_risk_profile_marks_a_human_approved_uncertainty_start() -> None:
    run_id = new_id("run")
    decision = _start_review(run_id)

    reviewed = reviewed_risk_profile(_UNCERTAIN, decision, _answered(decision, actor=_REVIEWER))

    assert reviewed.reviewed_decision_id == decision.id
    assert reviewed.model_copy(update={"reviewed_decision_id": None}) == _UNCERTAIN


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"automatic": True}, id="automatic-answer"),
        pytest.param({"approved": False}, id="rejection"),
        pytest.param(
            {"actor": Actor(kind="human", id="declared", authenticated=False)},
            id="unauthenticated-human",
        ),
        pytest.param(
            {"actor": Actor(kind="system", id="automation", authenticated=True)},
            id="system-actor",
        ),
        pytest.param({"decision_id": new_id("decision")}, id="another-decision"),
    ],
)
def test_reviewed_risk_profile_ignores_answers_that_are_not_a_human_approval(
    case: dict[str, Any],
) -> None:
    decision = _start_review(new_id("run"))
    events = _answered(
        decision,
        actor=case.get("actor", _REVIEWER),
        approved=case.get("approved", True),
        automatic=case.get("automatic", False),
        decision_id=case.get("decision_id"),
    )

    assert reviewed_risk_profile(_UNCERTAIN, decision, events) == _UNCERTAIN


@pytest.mark.parametrize(
    "decision_kwargs",
    [
        pytest.param({"reason": "A human must approve execution."}, id="action-rule-review"),
        pytest.param({"subject_kind": "tool_call"}, id="tool-call-review"),
    ],
)
def test_reviewed_risk_profile_ignores_reviews_that_did_not_escalate_the_classification(
    decision_kwargs: dict[str, Any],
) -> None:
    decision = _start_review(new_id("run"), **decision_kwargs)

    assert reviewed_risk_profile(_UNCERTAIN, decision, _answered(decision, actor=_REVIEWER)) == (
        _UNCERTAIN
    )


def test_reviewed_risk_profile_without_a_decision_is_unchanged() -> None:
    assert reviewed_risk_profile(_UNCERTAIN, None, []) == _UNCERTAIN
