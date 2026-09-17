"""The real gate, end to end through the Core composition (harness basics 3).

A real ThyGraph delegates to a real data agent whose one tool the Policy Engine escalates to a
human (a local, effect-free tool under the base policy stack, with an unclassified risk profile:
the fail-safe path). The step ends cleanly, THY parks, the Core ends the turn before MIRA; a human
answers through ``RunService.resolve_approval``; the resumed pass re-plans nothing, re-runs only
the one task that asked, and the Tool Manager runs that exact call once under the recorded
approval -- or denies it as a rejection the agent adapts to.

Real here: the Core composition (``build_runtime_graph`` / ``build_runtime_graph_factory``,
``RunController``, ``RunService``, ``InlineDispatcher``, ``StateCheckpointer``), ``ThySubgraph``
over a real ``ThyGraph`` with a real data agent, the ``ToolManager`` with one registered
effect-free tool, the Policy Engine over the shipped policy stack, and every local repository
under ``tmp_path``. Scripted: the model (``ScriptedProvider``) and, for the three Core-level
scenarios, MIRA (``_MiraDouble``) -- the last scenario runs the production factory with the real
deterministic MIRA. Everything is asserted from the hash-chained event log.

In that last scenario the real audit ends differently by branch: approving the call also answers
a second, findings-gate review that A15's own failure raises (an unclassified risk profile, by
this proof's construction) and the report ends ``failed``; rejecting the call never executes it,
so A15 has nothing to check, no findings-gate review follows, and the report ends
``passed_with_warnings``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, Field

from tests.thymira.api_support import TEST_CREDENTIAL
from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import FakeTool
from thymira.agents import AgentCatalog, AgentEndReason, LLMToolCall, load_agent_specs
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.settlement import delegation_key
from thymira.api import RuntimeDeps, build_default_deps
from thymira.core import (
    InlineDispatcher,
    RunController,
    RunEventLog,
    RunNotAwaitingApprovalError,
    RunService,
    RuntimeState,
    SessionService,
    StateCheckpointer,
    SubgraphDeps,
    ThySubgraph,
    UsageLedger,
    build_runtime_graph,
    build_runtime_graph_factory,
)
from thymira.events import JsonlEventLog, verify_events, verify_log
from thymira.mira.checks import AuditContext, audit_run
from thymira.policies import (
    ActionRule,
    ApprovalRequest,
    Gate,
    Policy,
    PolicyEngine,
    ToolCapability,
    load_policy_stack,
    pending_approvals,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    Decision,
    EventType,
    ModelRoutePolicy,
    Run,
    StopReason,
    new_id,
)
from thymira.state import (
    LocalArtifactStore,
    LocalCheckpointRepository,
    LocalRecordRepository,
    LocalRunStore,
    LocalSessionRepository,
)
from thymira.thy.models import AgentTask, PlanOutput, ThyAgentKind, ThyPhase
from thymira.tools import ToolManager, ToolRegistry, tool_intent_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from thymira.agents import LLMMessage
    from thymira.schemas import Event

pytestmark = pytest.mark.integration

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind every direct and composition-root provider seam to one code-owned test route."""
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "test-model")
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_PLAN_REASON = "test policy: THY's plan needs no review"
_PLAN_PASSES = Policy(
    name="plan-passes",
    version="1.0",
    action_rules=(
        ActionRule(
            id="TEST-PLAN",
            action_types=("plan.proposed",),
            decision=Decision.PASS,
            reason=_PLAN_REASON,
        ),
    ),
)
# The same rule as a project overlay, so the production factory's Gate, the Run's recorded policy
# hash and MIRA's snapshot are all one policy (`thymira.api.deps._project_policy`).
_PROJECT_POLICY_OVERLAY = f"""name: plan-passes
version: "1.0"
action_rules:
  - id: TEST-PLAN
    description: THY's plan proposal may proceed in this proof.
    action_types: [plan.proposed]
    decision: PASS
    reason: "{_PLAN_REASON}"
"""
_PROJECT_CONFIG = "project:\n  name: approval-proof\n  domain: credit_risk\n"

_DECIDED_INSTRUCTION = "collect the already decided baseline"
_PENDING_INSTRUCTION = "profile it"
_PLAN = PlanOutput(
    tasks=(
        AgentTask(
            id="decided-prefix",
            agent=ThyAgentKind.DATA,
            phase=ThyPhase.EXECUTE,
            instruction=_DECIDED_INSTRUCTION,
        ),
        AgentTask(
            id="pending-tool-task",
            agent=ThyAgentKind.DATA,
            phase=ThyPhase.EXECUTE,
            instruction=_PENDING_INSTRUCTION,
        ),
    )
)
_CATALOG_DRIFT_PLAN = PlanOutput(
    tasks=(
        *_PLAN.tasks,
        AgentTask(
            id="removed-agent-task",
            agent=ThyAgentKind.STATISTICS,
            phase=ThyPhase.EXECUTE,
            instruction="compute statistics after the approved profile",
            depends_on=("pending-tool-task",),
        ),
    )
)
_PREFIX_RESULT = DataProfileOutput(row_count=1, columns=("prefix",))
_ECHO = LLMToolCall(
    id="c1", name="echo", arguments={"value": "x", "description": "look at the value"}
)
_FINAL = LLMToolCall(id="c2", name="final_result", arguments={"row_count": 1, "columns": ["value"]})
_REJECTION_REASON = "tool call rejected by a human: reviewed"
_REVIEWER = Actor(kind="human", id="reviewer", authenticated=True)


class _EchoArguments(BaseModel):
    """The echo tool's schema; `description` is the mandatory intent every tool call carries."""

    value: str = Field(min_length=1)
    description: str = Field(min_length=1)


def _echo_tool() -> FakeTool:
    """Build the one effect-free local tool this proof exposes to the data agent."""
    return FakeTool(
        name="echo",
        capability=ToolCapability(id="echo", external_effects=()),
        description="Echo a value.",
        arguments_model=_EchoArguments,
    )


@dataclass
class _RecordingTurn:
    """A scripted turn that records the conversation it was shown before answering.

    A callable script item is how a scripted model reports what a tool actually returned
    (`ScriptedProvider.complete_turn`), so `seen` is the proof that the denial reached the model.
    """

    reply: LLMToolCall
    seen: list[str] = field(default_factory=list)

    def __call__(self, messages: Sequence[LLMMessage]) -> LLMToolCall:
        """Record the rendered conversation and answer with the scripted reply."""
        self.seen.append("\n".join(message.content for message in messages))
        return self.reply


class _MiraDouble:
    """The MIRA boundary for the Core-level scenarios: it only records that it was reached."""

    name = "scripted-mira"

    def graph_version(self) -> str:
        """Return the test MIRA graph version."""
        return "mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Record one audit completion, so reaching MIRA is visible in the log."""
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


def _catalog(directory: Path) -> AgentCatalog:
    """Build a one-agent catalog whose only tool is `echo`."""
    directory.mkdir(parents=True)
    (directory / "data.yaml").write_text(
        "name: data\nrole: agent\ntask_kinds: [analyze]\ntool_allowlist: [echo]\nmax_turns: 2\n"
        "max_depth: 1\nsystem_prompt_ref: prompts/data.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "prompts").mkdir()
    (directory / "prompts" / "data.md").write_text(
        "You are the data agent.", encoding="utf-8", newline="\n"
    )
    return load_agent_specs(directory, known_capabilities=frozenset({"echo"}))


def _workspace(directory: Path, *, policy_overlay: bool = False) -> Path:
    """Create the project directory THY inspects, optionally with the plan-passes overlay."""
    thymira = directory / ".thymira"
    thymira.mkdir(parents=True)
    (thymira / "config.yaml").write_text(_PROJECT_CONFIG, encoding="utf-8", newline="\n")
    if policy_overlay:
        (thymira / "policies.yaml").write_text(
            _PROJECT_POLICY_OVERLAY, encoding="utf-8", newline="\n"
        )
    return directory


@dataclass(frozen=True, slots=True)
class _Composed:
    """One parked Run and the handles a test needs to answer and inspect it."""

    run: Run
    store: LocalRunStore
    service: RunService
    engine: PolicyEngine
    echo: FakeTool
    provider: ScriptedProvider


def _compose(tmp_path: Path, script: Sequence[Any]) -> _Composed:
    """Dispatch one Run through the real composition and return it parked on its tool review."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="profile the dataset",
        model_route_policy=TEST_ROUTE_POLICY,
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    engine = PolicyEngine(load_policy_stack().merged_with(_PLAN_PASSES))
    echo = _echo_tool()
    registry = ToolRegistry((echo,))
    provider = ScriptedProvider(script)
    catalog = _catalog(tmp_path / "catalog")
    workspace = _workspace(tmp_path / "workspace")

    def graph_factory(_run: Run) -> CompiledStateGraph:
        """Rebuild the composed graph over the one shared checkpoint directory."""
        log = RunEventLog(store, run.id)
        deps = SubgraphDeps(
            event_log=log,
            gate=Gate(engine, log),
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
            tool_manager=ToolManager(registry),
            record_repository=LocalRecordRepository(tmp_path / "records"),
            usage_ledger=UsageLedger(),
        )
        return build_runtime_graph(
            ThySubgraph(
                run, catalog, provider=provider, project_dir=workspace, tool_registry=registry
            ),
            _MiraDouble(),
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
    dispatcher.submit(run.id)
    return _Composed(run, store, service, engine, echo, provider)


def _of(events: Sequence[Event], kind: EventType) -> list[Event]:
    """Filter one event type out of an authoritative Run history."""
    return [event for event in events if event.type is kind]


def _values(events: Sequence[Event], kind: EventType, key: str) -> list[Any]:
    """Read one payload field from every event of a type, in recorded order."""
    return [event.payload.get(key) for event in _of(events, kind)]


def _agent_lifecycle_values(events: Sequence[Event], key: str) -> list[Any]:
    """Read terminal and parked agent lifecycle fields in their recorded order."""
    lifecycle = (EventType.AGENT_PARKED, EventType.AGENT_COMPLETED)
    return [event.payload.get(key) for event in events if event.type in lifecycle]


def _tool_call_decisions(events: Sequence[Event]) -> list[Event]:
    """Every Policy Engine verdict recorded about a tool call."""
    return [
        event
        for event in _of(events, EventType.POLICY_DECISION)
        if event.payload.get("subject_kind") == "tool_call"
    ]


def _transitions(events: Sequence[Event]) -> list[tuple[Any, Any]]:
    """The Run's persisted transitions, as ``(command, resume_from)`` pairs in order."""
    return [
        (event.payload.get("command"), event.payload.get("resume_from"))
        for event in _of(events, EventType.RUN_TRANSITIONED)
    ]


def _answer(composed: _Composed, *, approved: bool) -> Run:
    """Answer the exact review the Run is parked on, through the real RunService."""
    return composed.service.resolve_approval(
        composed.run.id,
        gate=Gate(
            composed.engine,
            RunEventLog(composed.store, composed.run.id),
            approver=lambda _request: approved,
            human=_REVIEWER,
        ),
        actor=_REVIEWER,
        note="reviewed",
    )


def test_the_first_pass_parks_on_the_review_before_mira(tmp_path: Path) -> None:
    """The pending task ends cleanly and the Run parks before MIRA is ever reached."""
    composed = _compose(tmp_path, [_PLAN, _PREFIX_RESULT, _ECHO, _FINAL])

    events = composed.store.events(composed.run.id)

    assert _of(events, EventType.AUDIT_COMPLETED) == []
    assert composed.echo.seen_invocation == []
    assert _of(events, EventType.TOOL_STARTED) == []
    parked_key = _of(events, EventType.AGENT_PARKED)[0].payload["delegation_key"]
    parked = _of(events, EventType.AGENT_PARKED)[0]
    assert len(_of(events, EventType.AGENT_PARKED)) == 1
    assert parked.payload["objective"] == _PENDING_INSTRUCTION
    assert parked_key == delegation_key(
        run_id=composed.run.id,
        task_id=cast("str", parked.payload["task_id"]),
        agent_id=cast("str", parked.subject_id),
        parent_agent=cast("str", parked.payload["parent_agent"]),
        agent=cast("str", parked.payload["agent"]),
        objective=cast("str", parked.payload["objective"]),
        delegation_depth=cast("int", parked.payload["delegation_depth"]),
    )
    assert parked_key not in {
        event.payload.get("delegation_key") for event in _of(events, EventType.SUBAGENT_SETTLED)
    }
    assert not any(
        event.type is EventType.AGENT_MESSAGE and event.payload.get("delegation_key") == parked_key
        for event in events
    )
    decisions = _tool_call_decisions(events)
    assert [event.payload["decision"] for event in decisions] == ["REQUIRE_HUMAN_REVIEW"]
    # The Run is parked on that exact tool-call review, by the single Run writer.
    parked = _of(events, EventType.RUN_TRANSITIONED)[-1]
    assert parked.payload["command"] == "wait_for_approval"
    assert parked.payload["wait_reason"] == "approval"
    assert parked.payload["policy_decision"]["id"] == decisions[0].payload["id"]
    assert parked.payload["policy_decision"]["subject_kind"] == "tool_call"
    requested = _of(events, EventType.HUMAN_APPROVAL_REQUESTED)
    assert len(requested) == 1
    assert requested[0].payload["tool"] == "echo"
    assert requested[0].payload["arguments"] == _ECHO.arguments
    assert len(requested[0].payload["tool_intent_sha256"]) == 64
    assert requested[0].payload["decision_id"] == decisions[0].payload["id"]
    assert _of(events, EventType.HUMAN_APPROVAL) == []
    denied = _of(events, EventType.TOOL_DENIED)
    assert len(denied) == 1
    assert denied[0].payload["decision_id"] == decisions[0].payload["id"]
    assert denied[0].payload["tool_intent_sha256"] == requested[0].payload["tool_intent_sha256"]
    assert _values(events, EventType.AGENT_STARTED, "objective") == [
        _DECIDED_INSTRUCTION,
        _PENDING_INSTRUCTION,
    ]
    assert _agent_lifecycle_values(events, "end_reason") == [
        AgentEndReason.COMPLETED.value,
        AgentEndReason.AWAITING_APPROVAL.value,
    ]
    assert verify_events(events).valid


def test_an_approval_re_runs_only_the_pending_task_and_the_call_runs_once(
    tmp_path: Path,
) -> None:
    """The approval resumes the parked pass: nothing re-plans, only the call that asked runs.

    The resumed pass replays the parked call through `deferred_tool_results`
    (`thymira.agents.resume`) instead of asking the model to redo it, so only one more scripted
    item -- the model's reaction to the real tool result -- follows the park.
    """
    composed = _compose(tmp_path, [_PLAN, _PREFIX_RESULT, _ECHO, _FINAL])

    final = _answer(composed, approved=True)
    events = composed.store.events(composed.run.id)

    assert final.status.value == "COMPLETED"
    assert _of(events, EventType.RUN_FAILED) == []
    assert ("wait_for_approval", None) in _transitions(events)
    assert ("resume", "execution") in _transitions(events)
    approvals = _of(events, EventType.HUMAN_APPROVAL)
    assert len(approvals) == 1
    assert approvals[0].payload["approved"] is True
    # One review for one call: the resumed pass asked nobody a second time.
    decisions = _tool_call_decisions(events)
    assert len(decisions) == 1
    assert len(_of(events, EventType.HUMAN_APPROVAL_REQUESTED)) == 1
    # The plan was proposed, gated and routed exactly once: Plan short-circuited on the seed.
    plan_decisions = [
        event
        for event in _of(events, EventType.POLICY_DECISION)
        if event.payload.get("rule_id") == "TEST-PLAN"
    ]
    assert len(plan_decisions) == 1
    assert _values(events, EventType.MODEL_SELECTED, "task").count("plan") == 1
    # The decided prefix kept its outcome; only the task that asked ran again.
    assert _values(events, EventType.AGENT_STARTED, "objective") == [
        _DECIDED_INSTRUCTION,
        _PENDING_INSTRUCTION,
    ]
    assert _agent_lifecycle_values(events, "end_reason") == [
        AgentEndReason.COMPLETED.value,
        AgentEndReason.AWAITING_APPROVAL.value,
        AgentEndReason.COMPLETED.value,
    ]
    # The Tool Manager ran that exact call once, under the ticket the human answered.
    requested = _of(events, EventType.HUMAN_APPROVAL_REQUESTED)[0]
    started = _of(events, EventType.TOOL_STARTED)
    assert len(started) == 1
    assert started[0].payload["decision_id"] == requested.payload["decision_id"]
    assert started[0].payload["tool_intent_sha256"] == requested.payload["tool_intent_sha256"]
    assert started[0].payload["arguments"] == _ECHO.arguments
    assert _values(events, EventType.TOOL_COMPLETED, "status") == ["COMPLETED"]
    assert len(_of(events, EventType.TOOL_DENIED)) == 1
    assert len(composed.echo.seen_invocation) == 1
    assert len(_of(events, EventType.AUDIT_COMPLETED)) == 1
    assert len(_of(events, EventType.SUBAGENT_SETTLED)) == 2
    parked = _of(events, EventType.AGENT_PARKED)[0]
    resumed_completion = [
        event
        for event in _of(events, EventType.AGENT_COMPLETED)
        if event.payload.get("task_id") == parked.payload["task_id"]
    ][-1]
    assert resumed_completion.payload["resumed"] is True
    assert (
        resumed_completion.payload["execution_identity_sha256"]
        == parked.payload["execution_identity_sha256"]
    )
    assert (
        len(
            [
                event
                for event in _of(events, EventType.AGENT_MESSAGE)
                if "delegation_key" in event.payload
            ]
        )
        == 2
    )
    assert verify_events(events).valid


def test_a_rejection_resumes_and_the_agent_gets_a_factual_denial_without_reasking(
    tmp_path: Path,
) -> None:
    """A rejection is a tool result the agent adapts to, never a second review or a failed Run."""
    adapting = _RecordingTurn(_FINAL)
    composed = _compose(tmp_path, [_PLAN, _PREFIX_RESULT, _ECHO, adapting])

    final = _answer(composed, approved=False)
    events = composed.store.events(composed.run.id)

    assert final.status.value == "COMPLETED"
    assert _of(events, EventType.RUN_FAILED) == []
    assert ("resume", "execution") in _transitions(events)
    assert _values(events, EventType.HUMAN_APPROVAL, "approved") == [False]
    # The call was denied twice for one decision: once unanswered, once by the human's answer.
    denied = _of(events, EventType.TOOL_DENIED)
    assert len(denied) == 2
    assert denied[1].payload["reason"] == _REJECTION_REASON
    assert denied[1].payload["decision_id"] == denied[0].payload["decision_id"]
    assert denied[1].payload["tool_intent_sha256"] == denied[0].payload["tool_intent_sha256"]
    assert _of(events, EventType.TOOL_STARTED) == []
    assert composed.echo.seen_invocation == []
    # Nobody was asked again, and the model was told why before it answered.
    assert len(_of(events, EventType.HUMAN_APPROVAL_REQUESTED)) == 1
    assert len(_tool_call_decisions(events)) == 1
    assert len(adapting.seen) == 1
    assert _REJECTION_REASON in adapting.seen[0]
    assert "[denied]" in adapting.seen[0]
    assert _values(events, EventType.AGENT_COMPLETED, "end_reason")[-1] == (
        AgentEndReason.COMPLETED.value
    )
    assert len(_of(events, EventType.AUDIT_COMPLETED)) == 1
    assert verify_events(events).valid


def _production_deps(
    tmp_path: Path,
    workspace: Path,
    provider: ScriptedProvider,
    echo: FakeTool,
    *,
    catalog_for_build: Callable[[AgentCatalog, int], AgentCatalog] | None = None,
) -> RuntimeDeps:
    """Assemble the API composition root over a graph the production factory builds."""
    catalog = _catalog(tmp_path / "catalog")
    registry = ToolRegistry((echo,))
    assembled: list[RuntimeDeps] = []
    graph_builds = 0

    def graph_factory(run: Run) -> CompiledStateGraph:
        """Build this Run's graph from the same handles the API composition root owns."""
        nonlocal graph_builds
        deps = assembled[0]
        assert deps.project_resolution is not None
        selected_catalog = (
            catalog if catalog_for_build is None else catalog_for_build(catalog, graph_builds)
        )
        graph_builds += 1
        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=deps.gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=ToolManager(registry),
            tool_registry=registry,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            project_dir=workspace,
            project_config=deps.project_resolution.config,
            catalog=selected_catalog,
            specs=(),
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
            route_policy=TEST_ROUTE_POLICY,
        )(run)

    assembled.append(
        build_default_deps(
            tmp_path / "runtime",
            workspace=workspace,
            graph_factory=graph_factory,
            principal_resolver=TEST_CREDENTIAL,
        )
    )
    return assembled[0]


def _answer_every_review(deps: RuntimeDeps, run_id: str, *, tool_call: bool) -> None:
    """Answer each review the Run parks on: ``tool_call`` for the call, approval for findings.

    A rejected call is still evidence MIRA audits, so the Run reaches its findings review either
    way; only the call itself is answered with ``tool_call``.
    """

    def approver(request: ApprovalRequest) -> bool:
        return tool_call if request.decision.subject_kind == "tool_call" else True

    for _ in range(3):
        if deps.run_service.get_run(run_id).status.value != "WAITING_FOR_APPROVAL":
            return
        deps.run_service.resolve_approval(
            run_id,
            gate=deps.gate_factory(run_id, approver=approver, human=_REVIEWER),
            actor=_REVIEWER,
            note="reviewed",
        )


@pytest.mark.parametrize("approved", [True, False])
def test_the_production_factory_and_real_mira_accept_either_human_answer(
    tmp_path: Path, approved: bool
) -> None:
    """Real MIRA accepts either answer to the call the composed Run parked on.

    A3 and A6 recompute the manager's ticket, so an approved retry is authorised evidence and a
    rejected one is a denial the log explains.
    """
    workspace = _workspace(tmp_path / "workspace", policy_overlay=True)
    echo = _echo_tool()
    provider = ScriptedProvider([_PLAN, _PREFIX_RESULT, _ECHO, _FINAL])
    deps = _production_deps(tmp_path, workspace, provider, echo)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )

    run = deps.run_service.create_run(
        session.id, "profile the dataset", actor=Actor.system(), workspace=workspace
    )
    _answer_every_review(deps, run.id, tool_call=approved)

    events = deps.run_store.events(run.id)
    assert deps.run_service.get_run(run.id).status.value == "COMPLETED"
    assert _of(events, EventType.RUN_FAILED) == []
    tool_starts = _of(events, EventType.TOOL_STARTED)
    assert len(tool_starts) == int(approved)
    assert len(echo.seen_invocation) == int(approved)
    if approved:
        assert tool_starts[0].payload["arguments"] == _ECHO.arguments
    # A human was really asked, never auto-answered: the tool review always, and -- only once
    # approving lets A15's own failure raise a findings-gate review -- the findings review too.
    approvals = _of(events, EventType.HUMAN_APPROVAL)
    assert len(approvals) == (2 if approved else 1)
    assert all(event.actor.kind is ActorKind.HUMAN for event in approvals)
    assert all(event.payload["automatic"] is False for event in approvals)
    completed = _of(events, EventType.AUDIT_COMPLETED)
    assert len(completed) == 1
    report = completed[0].payload["audit_report"]
    controls = {entry["control_id"]: entry for entry in report["controls"]}
    assert controls["A3"]["status"] == ("PASSED" if approved else "NOT_APPLICABLE")
    assert controls["A6"]["status"] == "PASSED"
    # One denial when the call ran after approval (the pre-approval attempt); two when it never
    # did (the pending attempt and the human's final rejection) -- structural, not the exact
    # detail prose, which the manager is free to reword.
    assert len(_of(events, EventType.TOOL_DENIED)) == (1 if approved else 2)
    # A15 ("risk classified before tool execution") is the one control this proof cannot satisfy:
    # the factory is built without a risk interview, exactly as the production-factory test in
    # `test_core_graph_adapters.py` builds it, and it is that unclassified profile which makes the
    # Policy Engine escalate the call to a human in the first place. Nothing else may fail. A
    # rejected call never executes, so A15 has nothing to check and stays NOT_APPLICABLE; an
    # approved one does, A15 FAILS, and that is the one failure that ends the report `failed`.
    failed = {name for name, entry in controls.items() if entry["status"] == "FAILED"}
    assert failed == ({"A15"} if approved else set())
    assert report["status"] == ("failed" if approved else "passed_with_warnings")
    assert verify_events(events).valid


def test_a_restored_plan_settles_a_later_task_whose_agent_was_removed(
    tmp_path: Path,
) -> None:
    """A narrower resume catalog fails one restored task without bypassing MIRA or the Gate."""

    def catalog_for_build(data_catalog: AgentCatalog, build: int) -> AgentCatalog:
        """Expose Statistics for Plan's first pass, then model its removal before resume."""
        if build > 0:
            return data_catalog
        name = ThyAgentKind.STATISTICS.value
        data_spec = data_catalog.get(ThyAgentKind.DATA.value)
        return AgentCatalog(
            (data_spec, data_spec.model_copy(update={"name": name, "tool_allowlist": ()})),
            system_prompts={
                ThyAgentKind.DATA.value: data_catalog.system_prompt(ThyAgentKind.DATA.value),
                name: data_catalog.system_prompt(ThyAgentKind.DATA.value),
            },
            output_schemas={
                ThyAgentKind.DATA.value: data_catalog.output_schema(ThyAgentKind.DATA.value),
                name: data_catalog.output_schema(ThyAgentKind.DATA.value),
            },
        )

    workspace = _workspace(tmp_path / "workspace", policy_overlay=True)
    echo = _echo_tool()
    provider = ScriptedProvider([_CATALOG_DRIFT_PLAN, _PREFIX_RESULT, _ECHO, _FINAL])
    deps = _production_deps(
        tmp_path,
        workspace,
        provider,
        echo,
        catalog_for_build=catalog_for_build,
    )
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )

    run = deps.run_service.create_run(
        session.id, "profile the dataset", actor=Actor.system(), workspace=workspace
    )
    assert deps.run_service.get_run(run.id).status.value == "WAITING_FOR_APPROVAL"
    _answer_every_review(deps, run.id, tool_call=True)

    path = tmp_path / "runtime" / "runs" / run.id / "events.jsonl"
    verification = verify_log(path)
    replayed = JsonlEventLog(path, run.id).events()
    independent_report = audit_run(AuditContext(run.id, replayed))
    unresolved = [
        event
        for event in replayed
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("agent") == ThyAgentKind.STATISTICS.value
    ]

    assert verification.valid
    assert verification.event_count == len(replayed)
    assert len(unresolved) == 1
    assert unresolved[0].payload["status"] == "FAILED"
    assert unresolved[0].payload["registered_agents"] == [ThyAgentKind.DATA.value]
    unresolved_settlements = [
        event
        for event in replayed
        if event.type is EventType.SUBAGENT_SETTLED
        and event.payload.get("agent") == ThyAgentKind.STATISTICS.value
    ]
    assert len(unresolved_settlements) == 1
    unresolved_settlement = unresolved_settlements[0]
    assert unresolved_settlement.payload["stop_reason"] == StopReason.ABNORMAL.value
    assert unresolved_settlement.subject_id == unresolved[0].subject_id
    assert (
        unresolved_settlement.payload["delegation_key"] == unresolved[0].payload["delegation_key"]
    )
    assert unresolved_settlement.seq < unresolved[0].seq
    assert len(provider.calls) == 4
    plan_decisions = [
        event
        for event in replayed
        if event.type is EventType.POLICY_DECISION and event.payload.get("rule_id") == "TEST-PLAN"
    ]
    assert len(plan_decisions) == 1
    assert _values(replayed, EventType.MODEL_SELECTED, "task").count("plan") == 1
    audit_completed = _of(replayed, EventType.AUDIT_COMPLETED)
    findings_decisions = [
        event
        for event in replayed
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert len(audit_completed) == 1
    assert len(findings_decisions) == 1
    assert unresolved[0].seq < audit_completed[0].seq < findings_decisions[0].seq
    assert independent_report.status in {"passed", "passed_with_warnings", "failed"}
    a31 = next(control for control in independent_report.controls if control.control_id == "A31")
    assert a31.status.value == "PASSED"
    assert deps.run_service.get_run(run.id).status.value == "COMPLETED"
    assert _of(replayed, EventType.RUN_FAILED) == []
    terminal = [
        event for event in replayed if event.type in {EventType.RUN_COMPLETED, EventType.RUN_FAILED}
    ]
    assert [event.type for event in terminal] == [EventType.RUN_COMPLETED]


_OTHER_ECHO = LLMToolCall(
    id="c3", name="echo", arguments={"value": "y", "description": "look at another value"}
)


def test_a_resume_proposing_different_arguments_gets_no_credit_from_the_parked_review(
    tmp_path: Path,
) -> None:
    """F5.2: the parked approval covers the parked intent, and nothing else.

    A human was shown ``echo(value="x")``. Resumption executes that approved, original call, then
    the conversation proposes ``echo(value="y")``. The second call gets no credit from the first
    answer: it is denied and raises a review of its own.
    """
    composed = _compose(tmp_path, [_PLAN, _PREFIX_RESULT, _ECHO, _OTHER_ECHO, _FINAL])
    parked = _of(composed.store.events(composed.run.id), EventType.HUMAN_APPROVAL_REQUESTED)[0]

    _answer(composed, approved=True)
    events = composed.store.events(composed.run.id)

    tool_starts = _of(events, EventType.TOOL_STARTED)
    assert len(tool_starts) == 1
    assert tool_starts[0].payload["arguments"] == _ECHO.arguments
    assert len(composed.echo.seen_invocation) == 1
    requested = _of(events, EventType.HUMAN_APPROVAL_REQUESTED)
    assert len(requested) == 2
    # A different call is a different ticket, so it is a different question for a human.
    assert requested[1].payload["tool"] == "echo"
    assert requested[1].payload["arguments"] == _OTHER_ECHO.arguments
    assert requested[1].payload["tool_intent_sha256"] != parked.payload["tool_intent_sha256"]
    assert requested[1].payload["decision_id"] != parked.payload["decision_id"]
    # Both tickets are recomputed from the arguments each request recorded, so the difference is
    # a property of the two calls rather than of two values the producer happened to write.
    for request in (parked, requested[1]):
        assert request.payload["tool_intent_sha256"] == tool_intent_sha256(
            request.payload["tool"], request.payload["arguments"]
        )
    # The human answered once, and her yes authorized only the parked call.
    assert _values(events, EventType.HUMAN_APPROVAL, "approved") == [True]
    assert len(_tool_call_decisions(events)) == 2
    assert verify_events(events).valid


def test_cancelling_a_parked_run_closes_the_review_and_the_call_never_runs(
    tmp_path: Path,
) -> None:
    """F5.1 caller abort, end to end, read back by a reader that wrote none of it.

    ``RunService.cancel`` writes the cancel through ``RunController``; the review the Run was
    parked on leaves the pending set, so it can no longer be answered; and the persisted
    ``events.jsonl``, re-opened through a freshly constructed ``JsonlEventLog``, verifies and
    audits with no execution of the ticketed call.
    """
    composed = _compose(tmp_path, [_PLAN, _PREFIX_RESULT, _ECHO, _ECHO, _FINAL])
    before = composed.store.events(composed.run.id)
    assert len(pending_approvals(before)) == 1

    cancelled = composed.service.cancel(
        composed.run.id, actor=_REVIEWER, reason="the caller went away"
    )

    assert cancelled.status.value == "BLOCKED"
    events = composed.store.events(composed.run.id)
    assert pending_approvals(events) == ()
    assert _transitions(events)[-1][0] == "cancel"
    parked = _of(events, EventType.AGENT_PARKED)[-1]
    key = parked.payload["delegation_key"]
    completion = next(
        event
        for event in events
        if event.type is EventType.AGENT_COMPLETED and event.payload.get("delegation_key") == key
    )
    settlement = next(
        event
        for event in events
        if event.type is EventType.SUBAGENT_SETTLED and event.payload.get("delegation_key") == key
    )
    message = next(
        event
        for event in events
        if event.type is EventType.AGENT_MESSAGE and event.payload.get("delegation_key") == key
    )
    terminal_transition = _of(events, EventType.RUN_TRANSITIONED)[-1]
    assert parked.seq < completion.seq < settlement.seq < message.seq < terminal_transition.seq
    assert completion.payload["stop_reason"] == StopReason.STOPPED.value
    assert (
        completion.payload["execution_identity_sha256"]
        == parked.payload["execution_identity_sha256"]
    )
    assert settlement.payload["stop_reason"] == StopReason.STOPPED.value
    assert message.payload["stop_reason"] == StopReason.STOPPED.value
    assert (
        len(
            [
                event
                for event in events
                if event.type is EventType.AGENT_COMPLETED
                and event.payload.get("delegation_key") == key
            ]
        )
        == 1
    )
    # Neither answering nor resuming can revive the closed review.
    with pytest.raises(RunNotAwaitingApprovalError):
        _answer(composed, approved=True)
    assert composed.service.resume(composed.run.id, actor=_REVIEWER).status.value == "BLOCKED"

    # The oracle: a reader that wrote none of this opens the persisted log for itself.
    path = tmp_path / "runs" / composed.run.id / "events.jsonl"
    verification = verify_log(path)
    replayed = JsonlEventLog(path, composed.run.id).events()
    report = audit_run(AuditContext(composed.run.id, replayed))

    assert verification.valid
    assert verification.event_count == len(events)
    assert [event.type for event in replayed if event.type is EventType.TOOL_STARTED] == []
    assert composed.echo.seen_invocation == []
    assert _values(replayed, EventType.HUMAN_APPROVAL, "approved") == []
    a3 = next(control for control in report.controls if control.control_id == "A3")
    assert a3.status.value in {"PASSED", "NOT_APPLICABLE"}, a3
