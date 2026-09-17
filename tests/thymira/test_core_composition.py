"""Runtime composition tests for RA-CORE-06."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from langgraph.graph import END

from thymira.agents.llm import ScriptedProvider
from thymira.core import (
    InlineDispatcher,
    MiraControlPlane,
    RiskInterviewService,
    RunController,
    RunInformationRequiredError,
    RunService,
    RuntimeState,
    RunTransitionKind,
    SessionService,
    StateCheckpointer,
    Subgraph,
    SubgraphDeps,
    build_runtime_graph,
    graph_definition_hash,
    initial_runtime_state,
)
from thymira.core.control_plane import RunEventLog
from thymira.core.execution_review import INTERVIEW_LIMIT_RESUME_TARGET
from thymira.core.governance_binding import final_governance_binding
from thymira.core.graph.compose import _RuntimeGraphNodes
from thymira.core.phases import Phase
from thymira.core.usage import UsageLedger
from thymira.events import verify_events
from thymira.policies import (
    ApprovalRequest,
    Approver,
    Gate,
    LocalApprovalService,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    load_default_policy,
    pending_approvals,
)
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    ActivityProfile,
    Actor,
    ActorKind,
    AuditFinding,
    EventType,
    Framework,
    ModelRoutePolicy,
    RiskAssessment,
    RiskLevel,
    Run,
    RunCondition,
    RunOutcome,
    RunStage,
    Severity,
    WaitReason,
    approval_decision_id,
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
from thymira.tools import ToolManager, ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

_REVIEWER = Actor(kind="human", id="reviewer", authenticated=True)
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


class _ThySubgraph:
    """Scripted THY boundary for the composition tests."""

    name = "scripted-thy"

    def graph_version(self) -> str:
        """Return the test graph version."""
        return "thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Advance the shared state to the preparation phase."""
        del deps
        return state.model_copy(update={"phase": Phase.PREPARATION})


class _MiraSubgraph:
    """Scripted MIRA boundary that emits its completion evidence."""

    name = "scripted-mira"

    def __init__(self, finding: AuditFinding | None = None) -> None:
        self._finding = finding

    def graph_version(self) -> str:
        """Return the test graph version."""
        return "mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Emit the audit completion event and return findings to the Gate node."""
        findings = (self._finding,) if self._finding is not None else ()
        prior_events = deps.event_log.events()
        prior_hash = prior_events[-1].hash if prior_events else None
        revision = sum(event.type is EventType.AUDIT_COMPLETED for event in prior_events) + 1
        deps.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "status": "passed_with_warnings" if findings else "passed",
                "audit_revision": revision,
                "audit_report": {
                    "run_id": state.run_id,
                    "status": "passed_with_warnings" if findings else "passed",
                    "controls": [],
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                    "terminal_hash": prior_hash,
                    "policy_sha256": deps.gate.engine.policy_sha256,
                },
            },
            subject_id=state.run_id,
            producer="test.mira",
        )
        return state.model_copy(update={"findings": findings})


_PARKED_TASK = AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="x")


def _review_gated_tool_call(deps: SubgraphDeps, tool: str) -> None:
    """Record one tool-call decision the shipped policy escalates to a human, and its request."""
    deps.gate.check_capability(
        subject_id=tool,
        capability=ToolCapability(id=tool, external_effects=()),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
        summary=tool,
        details={"tool": tool, "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
    )


class _ParkingThy(_ThySubgraph):
    """THY that stops on review-gated tool calls, then finishes once it has parked ``parks`` times.

    ``reviews`` is how many tool calls one parking pass leaves waiting -- more than one is what a
    real fan-out produces, and it is what separates the decision the Run parks on from the latest
    one recorded.
    """

    def __init__(self, *, reviews: int = 1, parks: int = 1) -> None:
        self.calls: list[bool] = []  # one entry per invoke: True when seeded
        self._reviews = reviews
        self._parks = parks

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Park on unapproved tool calls until ``parks`` passes have done so, then finish."""
        self.calls.append(state.thy_progress is not None)
        if len(self.calls) > self._parks:
            return super().invoke(state.model_copy(update={"thy_progress": None}), deps=deps)
        for index in range(self._reviews):
            _review_gated_tool_call(deps, f"echo_{len(self.calls)}_{index + 1}")
        return state.model_copy(update={"thy_progress": ThyProgress(plan=(_PARKED_TASK,))})


class _ParkingThyAlsoAskingForReview(_ParkingThy):
    """A parking pass that leaves a later, run-level review pending behind the tool-call one.

    The shape MIRA's own ``request_approval`` produces under ``GOV-006``: a second unresolved
    ``REQUIRE_HUMAN_REVIEW`` recorded after the call the Run is parked on.
    """

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Park as usual, then record one run-level review after the tool-call one."""
        updated = super().invoke(state, deps=deps)
        if updated.thy_progress is not None:
            deps.gate.check_action(
                subject_kind="run",
                subject_id=state.run_id,
                action_type="request_approval",
                summary="a run-level review recorded after the tool-call one",
            )
        return updated


class _ParkingThyThatCrashesOnResume(_ParkingThy):
    """A THY that parks once and then hits an ordinary programming defect on the resumed pass."""

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Park on the first pass; raise a non-structural exception on every later one."""
        if self.calls:
            raise AttributeError("'NoneType' object has no attribute 'plan'")
        return super().invoke(state, deps=deps)


class _OrphanReviewThy(_ThySubgraph):
    """THY reporting a task awaiting a human whose pending review has no decision record."""

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Leave a `human.approval_requested` no `policy.decision` event ever backed."""
        deps.event_log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            {"decision_id": new_id("decision"), "summary": "its decision was never recorded"},
            subject_id=state.run_id,
        )
        return state.model_copy(update={"thy_progress": ThyProgress(plan=(_PARKED_TASK,))})


def _runtime(
    tmp_path: Path,
    *,
    finding: AuditFinding | None = None,
    usage_ledger: UsageLedger | None = None,
    approver: Approver | None = None,
) -> tuple[Run, LocalRunStore, SubgraphDeps, RunController]:
    """Build local runtime dependencies for a graph invocation."""
    run = Run(
        id=finding.run_id if finding is not None else new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the dataset",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    event_log = RunEventLog(store, run.id)
    gate = Gate(PolicyEngine(load_default_policy()), event_log, approver=approver)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=gate,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=usage_ledger or UsageLedger(),
    )
    return run, store, deps, RunController(store)


def _composed_graph_factory(
    tmp_path: Path,
    thy: Subgraph,
    mira: Subgraph,
    deps: SubgraphDeps,
    controller: RunController,
) -> Callable[[Run], CompiledStateGraph]:
    """Rebuild the composed graph over one shared checkpoint directory, as a dispatcher does."""

    def factory(_run: Run) -> CompiledStateGraph:
        """Build the graph for the one Run these dependencies belong to."""
        return build_runtime_graph(
            thy,
            mira,
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=controller,
        )

    return factory


def _service_for(
    tmp_path: Path, store: LocalRunStore, factory: Callable[[Run], CompiledStateGraph]
) -> RunService:
    """Build the Run service the API composes, over the same store and graph factory."""
    return RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
    )


def _answer(service: RunService, store: LocalRunStore, run_id: str, *, approved: bool) -> Run:
    """Record one human answer through a Gate the API supplies, and continue the Run."""
    return service.resolve_approval(
        run_id,
        gate=Gate(
            PolicyEngine(load_default_policy()),
            RunEventLog(store, run_id),
            approver=lambda _request: approved,
            human=_REVIEWER,
        ),
        actor=_REVIEWER,
    )


def _transitions(store: LocalRunStore, run_id: str) -> list[tuple[str | None, str | None]]:
    """The Run's persisted transitions, as ``(command, resume_from)`` pairs in order."""
    return [
        (event.payload.get("command"), event.payload.get("resume_from"))
        for event in store.events(run_id)
        if event.type is EventType.RUN_TRANSITIONED
    ]


def _parked_decision_id(store: LocalRunStore, run_id: str) -> str:
    """The id of the decision the latest ``wait_for_approval`` transition names."""
    return next(
        event.payload["policy_decision"]["id"]
        for event in reversed(store.events(run_id))
        if event.type is EventType.RUN_TRANSITIONED
        and event.payload.get("command") == "wait_for_approval"
    )


def _answered_decision_ids(store: LocalRunStore, run_id: str) -> list[str | None]:
    """Every decision a recorded ``human.approval`` names, in order."""
    return [
        approval_decision_id(event.payload)
        for event in store.events(run_id)
        if event.type is EventType.HUMAN_APPROVAL
    ]


def _complete_activity_profile(
    run: Run, *, purpose: str = "Profile credit applications."
) -> ActivityProfile:
    """Build one fully evidenced profile for the intake boundary."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose=purpose,
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=("credit_history",),
        sensitive_attributes=(),
        potential_consequences=("An urgent application could be reviewed late.",),
    )


def _invoke(
    tmp_path: Path,
    *,
    finding: AuditFinding | None = None,
    usage_ledger: UsageLedger | None = None,
    approver: Approver | None = None,
) -> tuple[RuntimeState, LocalRunStore, Run]:
    """Invoke the runtime graph with local persistence and checkpoints."""
    run, store, deps, controller = _runtime(
        tmp_path, finding=finding, usage_ledger=usage_ledger, approver=approver
    )
    graph = build_runtime_graph(
        _ThySubgraph(),
        _MiraSubgraph(finding),
        checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
        deps=deps,
        controller=controller,
    )
    result = graph.invoke(
        initial_runtime_state(run),
        {"configurable": {"thread_id": run.id}},
    )
    return RuntimeState.model_validate(result), store, run


def test_composed_graph_reaches_completed_and_preserves_event_order(tmp_path: Path) -> None:
    """THY, MIRA and Gate run in order and a passing review completes the Run."""
    state, store, run = _invoke(tmp_path)

    assert state.decision is not None
    assert state.decision.decision.value == "PASS"
    events = store.events(run.id)
    event_types = [event.type for event in events]
    assert event_types[0] is EventType.RUN_STARTED
    assert event_types.index(EventType.AUDIT_COMPLETED) < event_types.index(
        EventType.POLICY_DECISION
    )
    assert event_types.index(EventType.POLICY_DECISION) < event_types.index(EventType.RUN_COMPLETED)
    assert verify_events(events).valid
    audit = next(event for event in events if event.type is EventType.AUDIT_COMPLETED)
    decision = next(event for event in events if event.type is EventType.POLICY_DECISION)
    terminal = next(
        event
        for event in events
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "complete"
    )
    assert terminal.payload["policy_decision_id"] == decision.payload["id"]
    assert terminal.payload["audit_completed_seq"] == audit.seq
    assert terminal.payload["audit_terminal_hash"] == audit.payload["audit_report"]["terminal_hash"]
    final_state = RunController(store).current_state(run.id).state
    assert final_state.stage is RunStage.REPORTING
    assert final_state.condition is RunCondition.TERMINAL
    assert final_state.outcome is RunOutcome.COMPLETED


def test_execute_failure_is_persisted_after_begin_execution(tmp_path: Path) -> None:
    """A THY failure after the Plan -> Execute boundary must not look like a planning failure."""
    run, store, deps, controller = _runtime(tmp_path)

    class _ExecuteFailingThy(_ThySubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            assert deps.before_thy_execute is not None
            deps.before_thy_execute()
            raise RuntimeError("execute failed after the plan was allowed")

    graph = build_runtime_graph(
        _ExecuteFailingThy(),
        _MiraSubgraph(),
        checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
        deps=deps,
        controller=controller,
    )

    with pytest.raises(RuntimeError, match="execute failed"):
        graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})

    assert controller.current_state(run.id).stage is RunStage.EXECUTING
    assert [command for command, _ in _transitions(store, run.id)] == [
        "start",
        "begin_execution",
    ]
    assert verify_events(store.events(run.id)).valid


def test_composed_graph_refuses_completion_without_a_typed_audit_report(tmp_path: Path) -> None:
    """Core cannot close on a findings decision when MIRA supplied no report to bind."""
    run, store, deps, controller = _runtime(tmp_path)

    class _UnboundMira(_MiraSubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            deps.event_log.append(
                EventType.AUDIT_COMPLETED,
                Actor.system(),
                {"status": "completed"},
                subject_id=state.run_id,
                producer="test.mira",
            )
            return state

    graph = build_runtime_graph(
        _ThySubgraph(),
        _UnboundMira(),
        checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
        deps=deps,
        controller=controller,
    )

    with pytest.raises(RuntimeError, match=r"audit\.completed"):
        graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})

    assert not any(event.type is EventType.RUN_COMPLETED for event in store.events(run.id))


def test_final_binding_rejects_audit_added_after_the_policy_decision(tmp_path: Path) -> None:
    """A re-audit after the Gate decision makes that decision stale for terminal closure."""
    run, store, deps, _controller = _runtime(tmp_path)
    started = deps.event_log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "audit_revision": 1,
            "audit_report": {
                "run_id": run.id,
                "status": "passed",
                "controls": [],
                "findings": [],
                "terminal_hash": store.events(run.id)[0].hash,
                "policy_sha256": deps.gate.engine.policy_sha256,
            },
        },
        subject_id=run.id,
        producer="test.mira",
    )
    decision = deps.gate.review_findings(())
    decision_event = next(
        event
        for event in deps.event_log.events()
        if event.type is EventType.POLICY_DECISION and event.payload.get("id") == decision.id
    )
    deps.event_log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "audit_revision": 2,
            "audit_report": {
                "run_id": run.id,
                "status": "passed",
                "controls": [],
                "findings": [],
                "terminal_hash": decision_event.hash,
                "policy_sha256": deps.gate.engine.policy_sha256,
            },
        },
        subject_id=run.id,
        producer="test.mira",
    )

    with pytest.raises(RuntimeError, match="must precede"):
        final_governance_binding(run.id, decision, deps.event_log.events())
    assert started.seq < decision_event.seq


def test_risk_interview_asks_every_missing_fact_before_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each answer resumes the interview node before classification or THY tool evidence."""
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "test-model")
    run, store, deps, controller = _runtime(tmp_path)
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Prioritise applications for human review.",
        affected_population="undeclared",
        decision_effect="undeclared",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=("credit_history",),
        sensitive_attributes=(),
        potential_consequences=("An urgent application could be reviewed late.",),
    )
    interview = RiskInterviewService(
        store,
        MiraControlPlane(store, PolicyEngine(load_default_policy())),
    )
    provider = ScriptedProvider(
        [
            {
                "risk_level": "low",
                "activity_category": "decision_support",
                "risk_factors": (),
                "missing_information": (),
                "confidence": 0.9,
                "needs_human_review": False,
                "summary": "The activity supports a human review queue.",
                "criteria": (),
            }
        ]
    )
    checkpoints = LocalCheckpointRepository(tmp_path / "checkpoints")

    class _ToolUsingThy(_ThySubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            deps.event_log.append(
                EventType.TOOL_STARTED,
                Actor.system(),
                {"tool_name": "read_dataset"},
                subject_id=state.run_id,
                producer="test.thy",
            )
            return super().invoke(state, deps=deps)

    def preflight(state: RuntimeState, runtime_deps: SubgraphDeps) -> RuntimeState:
        active_profile = interview.current_profile(run, profile)
        assessment = RiskAssessment(
            id=new_id("assessment"),
            run_id=run.id,
            activity_profile_id=active_profile.id,
            activity_profile_version=active_profile.version,
            assessor=Actor.system(),
            subject_kind="activity",
            subject_id=active_profile.activity_id,
            risk_level=RiskLevel.HIGH,
            activity_category="decision_support",
            confidence=1.0,
            justification="The reviewed inherent-risk method classifies this activity as high.",
            assessment_method="test-mira",
            assessment_method_version="1.0",
        )
        runtime_deps.event_log.append(
            EventType.RISK_ASSESSMENT_RECORDED,
            Actor.system(),
            {"risk_assessment": assessment.model_dump(mode="json")},
            subject_id=assessment.id,
            producer="thymira.mira",
        )
        return state

    def begin_interview(state: RuntimeState, _: SubgraphDeps) -> RuntimeState:
        """Ask the next missing field before the graph can reach THY."""
        interview.begin(run, profile)
        return state

    def classify(state: RuntimeState, runtime_deps: SubgraphDeps) -> RuntimeState:
        """Run the scripted classifier after the MIRA inherent-risk evidence exists."""
        interview.classify(
            run,
            profile,
            runtime_deps.event_log,
            provider,
            route_policy=TEST_ROUTE_POLICY,
        )
        return state

    def graph_factory(_: Run):
        return build_runtime_graph(
            _ToolUsingThy(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(checkpoints),
            deps=deps,
            controller=controller,
            interview=begin_interview,
            preflight=preflight,
            classify=classify,
        )

    InlineDispatcher(store, graph_factory).submit(run.id)

    first_events = store.events(run.id)
    assert EventType.ACTIVITY_PROFILE_QUESTIONED in [event.type for event in first_events]
    assert EventType.TOOL_STARTED not in [event.type for event in first_events]
    wait_reason = controller.current_state(run.id).wait_reason
    assert wait_reason is not None
    assert wait_reason.value == "information"

    actor = Actor(kind=ActorKind.HUMAN, id="risk-owner", authenticated=False)
    first_service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
        risk_interview=interview,
    )
    first_service.answer_risk_interview(run.id, answer="Credit applicants.", actor=actor)

    after_first_answer = store.events(run.id)
    questions = [
        event for event in after_first_answer if event.type is EventType.ACTIVITY_PROFILE_QUESTIONED
    ]
    assert [event.payload["field"] for event in questions] == [
        "affected_population",
        "decision_effect",
    ]
    assert [event.payload["profile_version"] for event in questions] == [1, 2]
    assert EventType.RISK_CLASSIFIED not in [event.type for event in after_first_answer]
    assert EventType.TOOL_STARTED not in [event.type for event in after_first_answer]
    assert controller.current_state(run.id).wait_reason is WaitReason.INFORMATION

    second_service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
        risk_interview=interview,
    )
    second_service.answer_risk_interview(
        run.id,
        answer="Changes review order but not the final decision.",
        actor=actor,
    )

    events = store.events(run.id)
    types = [event.type for event in events]
    classified = next(event for event in events if event.type is EventType.RISK_CLASSIFIED)
    assert types.index(EventType.RISK_CLASSIFIED) < types.index(EventType.TOOL_STARTED)
    assert classified.payload["risk_profile"]["risk_level"] == "high"
    assert verify_events(events).valid


def test_resume_re_attempts_a_preflight_information_park_with_no_pending_question(
    tmp_path: Path,
) -> None:
    """`resume` can clear an information wait that carries no risk-interview question.

    Found live (2026-09-09) against `examples/credit-risk` on `main` (52829dd): every one of the
    nine fixed activity-profile facts was already declared, yet MIRA's inherent-risk preflight
    still judged them insufficient and parked the Run for `wait_reason: information` through
    `MiraSubgraph._request_human_context`. Neither `resume` (which used to refuse *every*
    information wait outright) nor `answer` (there is no `activity_profile.questioned` event this
    park ever creates) could clear it -- and the graph's own `preflight -> classify` edge was
    unconditional, so the very same pass kept going regardless, reaching a real Policy Engine
    decision while the persisted wait sat there orphaned underneath it. Both are fixed: `preflight`
    now has a `route_after_preflight` conditional edge (mirroring `route_after_interview`), and
    `resume` allows exactly the information waits `RiskInterviewService.has_pending_question`
    reports as unanswerable through the interview.
    """
    run, store, deps, controller = _runtime(tmp_path)
    interview = RiskInterviewService(
        store, MiraControlPlane(store, PolicyEngine(load_default_policy()))
    )
    attempts = {"count": 0}

    def preflight(state: RuntimeState, runtime_deps: SubgraphDeps) -> RuntimeState:
        attempts["count"] += 1
        if attempts["count"] == 1:
            MiraControlPlane(store, PolicyEngine(load_default_policy())).handle(
                ActionIntent(
                    id=new_id("intent"),
                    run_id=run.id,
                    requester=Actor.system(),
                    action_kind=ActionKind.REQUEST_INFORMATION,
                    subject_kind="run",
                    subject_id=run.id,
                    purpose="Provide the missing context needed to classify inherent risk.",
                    payload={"missing_information": ["deployment_context"]},
                    idempotency_key=f"mira-risk-information:{run.id}:v1",
                )
            )
        return state

    checkpoints = LocalCheckpointRepository(tmp_path / "checkpoints")

    def graph_factory(_: Run) -> CompiledStateGraph:
        return build_runtime_graph(
            _ThySubgraph(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(checkpoints),
            deps=deps,
            controller=controller,
            preflight=preflight,
        )

    InlineDispatcher(store, graph_factory).submit(run.id)

    assert attempts["count"] == 1
    assert controller.current_state(run.id).wait_reason is WaitReason.INFORMATION
    assert interview.has_pending_question(run.id) is False

    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
        risk_interview=interview,
    )

    resumed = service.resume(
        run.id, actor=Actor(kind=ActorKind.HUMAN, id="operator", authenticated=True)
    )

    assert attempts["count"] == 2
    assert controller.current_state(run.id).condition is not RunCondition.WAITING
    assert resumed.status.value == "COMPLETED"
    assert verify_events(store.events(run.id)).valid


def test_resume_still_refuses_an_information_wait_with_a_pending_interview_question(
    tmp_path: Path,
) -> None:
    """`resume` must not bypass the ordinary risk-interview question through the same door."""
    run, store, deps, controller = _runtime(tmp_path)
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="undeclared",
        affected_population="undeclared",
        decision_effect="undeclared",
        autonomy="undeclared",
        human_oversight="undeclared",
        jurisdiction="undeclared",
        data_categories=(),
        sensitive_attributes=(),
        potential_consequences=(),
    )
    interview = RiskInterviewService(
        store, MiraControlPlane(store, PolicyEngine(load_default_policy()))
    )

    def begin_interview(state: RuntimeState, _: SubgraphDeps) -> RuntimeState:
        interview.begin(run, profile)
        return state

    checkpoints = LocalCheckpointRepository(tmp_path / "checkpoints")

    def graph_factory(_: Run) -> CompiledStateGraph:
        return build_runtime_graph(
            _ThySubgraph(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(checkpoints),
            deps=deps,
            controller=controller,
            interview=begin_interview,
        )

    InlineDispatcher(store, graph_factory).submit(run.id)
    assert controller.current_state(run.id).wait_reason is WaitReason.INFORMATION
    assert interview.has_pending_question(run.id) is True

    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
        risk_interview=interview,
    )

    with pytest.raises(RunInformationRequiredError, match="activity-profile answer"):
        service.resume(run.id, actor=Actor(kind=ActorKind.HUMAN, id="operator", authenticated=True))


def test_risk_interview_skips_a_complete_profile(tmp_path: Path) -> None:
    """Declared facts do not create a redundant question or waiting state."""
    run, store, _, _ = _runtime(tmp_path)
    profile = _complete_activity_profile(run)
    interview = RiskInterviewService(
        store,
        MiraControlPlane(store, PolicyEngine(load_default_policy())),
    )

    status = interview.begin(run, profile)

    assert status.profile == profile
    assert status.pending_question is None
    assert status.requires_human_review is False
    assert [event.type for event in store.events(run.id)] == [
        EventType.RUN_STARTED,
        EventType.ACTIVITY_PROFILE_RECORDED,
    ]


def test_risk_interview_escalates_after_the_question_limit(tmp_path: Path) -> None:
    """A placeholder response cannot keep a Run in an unbounded interview loop."""
    run, store, _, controller = _runtime(tmp_path)
    profile = _complete_activity_profile(run, purpose="undeclared")
    interview = RiskInterviewService(
        store,
        MiraControlPlane(store, PolicyEngine(load_default_policy())),
        max_questions=1,
    )
    controller.advance(run.id, RunTransitionKind.START)
    pending = interview.begin(run, profile).pending_question
    assert pending is not None
    assert pending.field == "purpose"

    updated = interview.answer(
        run,
        answer="undeclared",
        actor=Actor(kind=ActorKind.HUMAN, id="risk-owner", authenticated=False),
    )
    controller.advance(run.id, RunTransitionKind.RESUME, payload={"resume_from": "information"})

    status = interview.begin(run, profile)

    assert updated.version == 2
    assert status.profile == updated
    assert status.pending_question is None
    assert status.requires_human_review is True
    assert EventType.HUMAN_APPROVAL_REQUESTED in [event.type for event in store.events(run.id)]
    wait_reason = controller.current_state(run.id).wait_reason
    assert wait_reason is not None
    assert wait_reason.value == "approval"
    assert ("wait_for_approval", INTERVIEW_LIMIT_RESUME_TARGET) in _transitions(store, run.id)


def test_composed_graph_runs_governance_preflight_before_thy(tmp_path: Path) -> None:
    """The optional governance preflight precedes THY without adding a second Gate decision."""
    run, store, deps, controller = _runtime(tmp_path)
    calls: list[str] = []

    class _RecordingThy(_ThySubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            calls.append("thy")
            return super().invoke(state, deps=deps)

    class _RecordingMira(_MiraSubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            calls.append("mira")
            return super().invoke(state, deps=deps)

    def preflight(state: RuntimeState, deps: SubgraphDeps) -> RuntimeState:
        del deps
        calls.append("preflight")
        return state

    graph = build_runtime_graph(
        _RecordingThy(),
        _RecordingMira(),
        checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
        deps=deps,
        controller=controller,
        preflight=preflight,
    )
    graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})

    assert calls == ["preflight", "thy", "mira"]
    decisions = [
        event
        for event in store.events(run.id)
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert len(decisions) == 1
    assert verify_events(store.events(run.id)).valid


def test_composed_graph_parks_a_review_required_run(tmp_path: Path) -> None:
    """A high-severity finding is decided by the Gate and parks the Run for a human."""
    run_id = new_id("run")
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="GOV-TEST",
        framework=Framework.EU_AI_ACT,
        title="Human review required",
        finding="The result needs a human decision.",
        severity=Severity.HIGH,
        confidence=0.95,
    )
    state, store, run = _invoke(tmp_path, finding=finding)

    assert state.decision is not None
    assert state.decision.decision.value == "REQUIRE_HUMAN_REVIEW"
    final_state = RunController(store).current_state(run.id).state
    assert final_state.condition is RunCondition.WAITING
    assert final_state.wait_reason is not None
    assert final_state.wait_reason.value == "approval"
    assert verify_events(store.events(run.id)).valid


def test_composed_graph_passes_usage_snapshot_to_gate(tmp_path: Path) -> None:
    """The composed Gate receives and returns the authoritative ledger snapshot."""
    run_id = new_id("run")
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="GOV-TEST",
        framework=Framework.EU_AI_ACT,
        title="Human review required",
        finding="The result needs a human decision.",
        severity=Severity.HIGH,
        confidence=0.95,
    )
    ledger = UsageLedger()
    ledger.charge(requests=2, tokens=1200, cost_usd=0.12)
    captured: list[ApprovalRequest] = []

    def approver(request: ApprovalRequest) -> bool:
        """Capture the public approval request and leave the decision unresolved."""
        captured.append(request)
        return False

    state, store, run = _invoke(
        tmp_path,
        finding=finding,
        usage_ledger=ledger,
        approver=approver,
    )

    snapshot = ledger.snapshot()
    assert state.usage == snapshot
    assert captured
    assert captured[0].cost_so_far == snapshot
    approval_event = next(
        event for event in store.events(run.id) if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    assert approval_event.payload["cost_so_far"] == snapshot


def test_approved_review_resumes_from_gate_without_replaying_children(tmp_path: Path) -> None:
    """An approved parked graph continues to completion from its Gate checkpoint."""
    run, store, deps, controller = _runtime(
        tmp_path,
        finding=AuditFinding(
            id=new_id("finding"),
            run_id=(run_id := new_id("run")),
            control_id="GOV-TEST",
            framework=Framework.EU_AI_ACT,
            title="Human review required",
            finding="The result needs a human decision.",
            severity=Severity.HIGH,
            confidence=0.95,
        ),
    )
    assert run.id == run_id
    thy_calls: list[str] = []
    mira_calls: list[str] = []

    class _CountingThy(_ThySubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            thy_calls.append(state.run_id)
            return super().invoke(state, deps=deps)

    class _CountingMira(_MiraSubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            mira_calls.append(state.run_id)
            return super().invoke(state, deps=deps)

    thy = _CountingThy()
    mira = _CountingMira(
        AuditFinding(
            id=new_id("finding"),
            run_id=run.id,
            control_id="GOV-TEST",
            framework=Framework.EU_AI_ACT,
            title="Human review required",
            finding="The result needs a human decision.",
            severity=Severity.HIGH,
            confidence=0.95,
        )
    )

    def graph_factory(_run: Run):
        return build_runtime_graph(
            thy,
            mira,
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=controller,
        )

    graph = graph_factory(run)
    graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    assert RunController(store).current_state(run.id).condition is RunCondition.WAITING

    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
    )
    approval_gate = Gate(
        PolicyEngine(load_default_policy()),
        RunEventLog(store, run.id),
        approver=lambda _request: True,
        human=Actor(kind="human", id="reviewer", authenticated=True),
    )

    completed = service.resolve_approval(
        run.id,
        gate=approval_gate,
        actor=Actor(kind="human", id="reviewer", authenticated=True),
    )

    assert completed.status.value == "COMPLETED"
    assert thy_calls == [run.id]
    assert mira_calls == [run.id]
    assert verify_events(store.events(run.id)).valid


def test_composed_graph_hash_changes_when_a_child_version_changes() -> None:
    """The provenance hash includes both child graph versions."""
    thy = _ThySubgraph()
    mira = _MiraSubgraph()
    first = graph_definition_hash(thy, mira)

    class _NewMira(_MiraSubgraph):
        def graph_version(self) -> str:
            """Return a changed graph version."""
            return "mira-v2"

    assert first != graph_definition_hash(thy, _NewMira())


def test_a_control_plane_approval_resumes_a_parked_run(tmp_path: Path) -> None:
    """A human answer recorded under the frozen contract field must unpark the Run.

    `Gate.request_approval` -- the path behind `MiraControlPlane` -- appends
    `Approval.to_json_dict()`, whose Contract 0.3 decision field is `policy_decision_id`, while
    `Gate.check_action` writes the ad-hoc `decision_id`. Readers that knew only the ad-hoc name
    left `RunService._pending_approval` reporting the decision as unanswered (so `resume` refused
    the Run) and the composed graph's routing edge returning to END: an approved Run could never
    continue.
    """
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=(run_id := new_id("run")),
        control_id="GOV-TEST",
        framework=Framework.EU_AI_ACT,
        title="Human review required",
        finding="The result needs a human decision.",
        severity=Severity.HIGH,
        confidence=0.95,
    )
    run, store, deps, controller = _runtime(tmp_path, finding=finding)
    assert run.id == run_id

    def graph_factory(_run: Run):
        """Rebuild the composed graph over the shared checkpoint directory."""
        return build_runtime_graph(
            _ThySubgraph(),
            _MiraSubgraph(finding),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=deps,
            controller=controller,
        )

    graph = graph_factory(run)
    graph.invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    assert RunController(store).current_state(run.id).condition is RunCondition.WAITING

    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    decision_id = next(
        event.payload["id"]
        for event in store.events(run.id)
        if event.type is EventType.POLICY_DECISION
    )
    RunEventLog(store, run.id).append(
        EventType.HUMAN_APPROVAL,
        reviewer,
        {
            "id": new_id("approval"),
            "run_id": run.id,
            "policy_decision_id": decision_id,
            "authorization_context_sha256": "0" * 64,
            "approved": True,
        },
        subject_id=run.id,
    )
    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
    )

    resumed = service.resume(run.id, actor=reviewer)

    assert resumed.status.value == "COMPLETED"
    assert verify_events(store.events(run.id)).valid


@pytest.mark.parametrize("approved", [True, False])
def test_a_tool_call_review_parks_the_run_before_mira_and_either_answer_resumes_into_thy(
    tmp_path: Path, approved: bool
) -> None:
    """A tool call awaiting a human ends the turn before MIRA; either answer resumes into THY.

    MIRA audits the pass that finishes, never the knowingly incomplete one it would have to
    invalidate on the resume, so the park has to end the graph turn at ``thy``. Both answers
    resume: an approval lets the waiting call run once, a rejection is a recorded denial the agent
    adapts to.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy()
    mira_calls: list[str] = []

    class _CountingMira(_MiraSubgraph):
        def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
            """Record that MIRA was reached before delegating to the scripted audit."""
            mira_calls.append(state.run_id)
            return super().invoke(state, deps=deps)

    factory = _composed_graph_factory(tmp_path, thy, _CountingMira(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    parked = RunController(store).current_state(run.id).state
    assert parked.condition is RunCondition.WAITING
    assert parked.wait_reason is WaitReason.APPROVAL
    assert mira_calls == []
    assert thy.calls == [False]

    answered = _answer(_service_for(tmp_path, store, factory), store, run.id, approved=approved)

    assert answered.status.value == "COMPLETED"
    assert thy.calls == [False, True]
    assert mira_calls == [run.id]
    resumed = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
    )
    assert resumed.payload["resume_from"] == "execution"
    assert resumed.payload["approved"] is approved
    assert verify_events(store.events(run.id)).valid


def test_a_tool_call_review_answered_out_of_band_resumes_into_thy_on_a_plain_resume(
    tmp_path: Path,
) -> None:
    """A plain resume must re-enter where the Run was parked, not at the Gate.

    The governance ``ApprovalService`` records the human answer as evidence and never transitions
    the Run, so :meth:`RunService.resume` is what continues it. Without the parked decision's
    subject kind it wrote ``resume_from: "approval"``, the dispatcher re-entered at ``gate``, the
    Gate node found no findings decision and the Run failed instead of finishing.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy()

    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    assert RunController(store).current_state(run.id).state.wait_reason is WaitReason.APPROVAL
    log = RunEventLog(store, run.id)
    decision_id = pending_approvals(log.events())[0].decision_id
    LocalApprovalService(log).resolve(run.id, decision_id, approved=True, by=_REVIEWER)

    resumed = _service_for(tmp_path, store, factory).resume(run.id, actor=_REVIEWER)

    assert resumed.status.value == "COMPLETED"
    assert thy.calls == [False, True]
    transition = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
    )
    assert transition.payload["resume_from"] == "execution"
    assert verify_events(store.events(run.id)).valid


def test_a_pending_review_without_its_policy_decision_fails_the_run_instead_of_escaping(
    tmp_path: Path,
) -> None:
    """A review the log cannot back must fail the Run, never escape the dispatcher.

    ``decision_from_events`` raises ``UnknownApprovalError`` -- a ``LookupError``, which
    ``InlineDispatcher`` does not catch. Left as it is raised, it propagates out of
    ``create_run`` and the Run is left with no terminal event at all: exactly the zombie this
    change exists to prevent.
    """
    store = LocalRunStore(tmp_path / "runs")
    sessions = SessionService(LocalSessionRepository(tmp_path / "sessions"))

    def factory(run: Run) -> CompiledStateGraph:
        """Build one Run's dependencies and graph, as the production factory does."""
        event_log = RunEventLog(store, run.id)
        return build_runtime_graph(
            _OrphanReviewThy(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
            deps=SubgraphDeps(
                event_log=event_log,
                gate=Gate(PolicyEngine(load_default_policy()), event_log),
                artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
                tool_manager=ToolManager(ToolRegistry()),
                record_repository=LocalRecordRepository(tmp_path / "records"),
                usage_ledger=UsageLedger(),
            ),
            controller=RunController(store),
        )

    service = _service_for(tmp_path, store, factory)
    session = sessions.create(project_id=new_id("project"), client="test")

    created = service.create_run(
        session.id, "Profile the dataset", actor=Actor.system(), workspace=tmp_path
    )

    assert service.get_run(created.id).status.value == "FAILED"
    failed = next(event for event in store.events(created.id) if event.type is EventType.RUN_FAILED)
    assert "has no valid policy.decision record" in failed.payload["error"]
    assert verify_events(store.events(created.id)).valid


def test_two_pending_tool_call_reviews_answer_the_decision_the_run_was_parked_on(
    tmp_path: Path,
) -> None:
    """The human answers the review the Run is parked on, not the last one recorded.

    The park takes the first pending tool-call review; reading the *latest* unresolved decision
    instead answered a different call, left the parked one waiting forever and still let the Run
    complete. Park, branch and answer must come from the one record: the ``wait_for_approval``
    transition.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy(reviews=2)
    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    parked = _parked_decision_id(store, run.id)
    assert len(pending_approvals(store.events(run.id))) == 2

    answered = _answer(_service_for(tmp_path, store, factory), store, run.id, approved=True)

    assert answered.status.value == "COMPLETED"
    assert _answered_decision_ids(store, run.id) == [parked]
    assert ("resume", "execution") in _transitions(store, run.id)
    assert thy.calls == [False, True]
    assert verify_events(store.events(run.id)).valid


def test_a_run_level_review_recorded_later_does_not_divert_the_answer_from_the_park(
    tmp_path: Path,
) -> None:
    """A later run-level review must not take the answer meant for the tool-call park.

    Answering it wrote ``resume_from: "approval"``, the dispatcher re-entered at ``gate``, the
    Gate node found no findings decision and the Run failed with "did not produce a policy
    decision" -- a Run killed by a review it was never parked on.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThyAlsoAskingForReview()
    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    parked = _parked_decision_id(store, run.id)

    answered = _answer(_service_for(tmp_path, store, factory), store, run.id, approved=True)

    assert answered.status.value == "COMPLETED"
    assert _answered_decision_ids(store, run.id) == [parked]
    assert thy.calls == [False, True]
    assert not any(event.type is EventType.RUN_FAILED for event in store.events(run.id))
    assert verify_events(store.events(run.id)).valid


def test_a_redelivered_thy_node_neither_parks_nor_advances_the_run_twice(tmp_path: Path) -> None:
    """At-least-once delivery re-enters `thy` after the park was committed; both guards hold.

    The run store commits a transition inside the node while the checkpoint commits only at the
    node boundary, so a crash in between replays `run_thy` against a Run that is already EXECUTING
    and already WAITING. Neither advance may be applied a second time, and the turn must still
    end before MIRA.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy()
    mira = _MiraSubgraph()
    factory = _composed_graph_factory(tmp_path, thy, mira, deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    # The redelivered task: the same node function, over the same input state, after the run
    # store already holds `begin_execution` and `wait_for_approval`.
    nodes = _RuntimeGraphNodes(
        thy=thy,
        mira=mira,
        deps=deps,
        controller=controller,
        interview=None,
        preflight=None,
        classify=None,
        execution_gate=None,
        assurance=None,
    )

    nodes.run_thy(initial_runtime_state(run))

    assert [command for command, _ in _transitions(store, run.id)] == [
        "start",
        "begin_execution",
        "wait_for_approval",
    ]
    assert nodes.route_after_thy(initial_runtime_state(run)) == END
    assert verify_events(store.events(run.id)).valid


def test_a_second_park_inside_a_resumed_pass_parks_and_resumes_again(tmp_path: Path) -> None:
    """A resumed pass that stops on another tool call parks again and resumes the same way."""
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy(parks=2)
    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    service = _service_for(tmp_path, store, factory)

    first = _answer(service, store, run.id, approved=True)
    second = _answer(service, store, run.id, approved=True)

    assert first.status.value == "WAITING_FOR_APPROVAL"
    assert second.status.value == "COMPLETED"
    assert thy.calls == [False, True, True]
    assert [
        (command, resume_from)
        for command, resume_from in _transitions(store, run.id)
        if command in {"wait_for_approval", "resume"}
    ] == [
        ("wait_for_approval", None),
        ("resume", "execution"),
        ("wait_for_approval", None),
        ("resume", "execution"),
    ]
    assert pending_approvals(store.events(run.id)) == ()
    assert verify_events(store.events(run.id)).valid


def test_a_thy_defect_on_the_resumed_pass_fails_the_parked_run_instead_of_zombieing_it(
    tmp_path: Path,
) -> None:
    """A resumed pass that crashes leaves a terminal FAILED Run, never one stuck RUNNING.

    The human's answer is already recorded and already spent by the time THY runs again, so a
    crash the dispatch boundary did not catch used to leave the Run with no terminal event: not
    resumable, not failed, and with an approval nobody can give a second time.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThyThatCrashesOnResume()
    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    service = _service_for(tmp_path, store, factory)

    final = _answer(service, store, run.id, approved=True)

    assert final.status.value == "FAILED"
    events = store.events(run.id)
    failed = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert "'NoneType' object has no attribute 'plan'" in failed.payload["error"]
    assert _answered_decision_ids(store, run.id) == [_parked_decision_id(store, run.id)]
    assert verify_events(events).valid


def test_a_redelivered_thy_node_against_an_already_waiting_run_never_parks_twice(
    tmp_path: Path,
) -> None:
    """The park guard, entered directly: a second `thy` pass that still reports progress.

    The redelivery test above re-enters `run_thy` with a THY double that has already used up its
    parks, so its finish branch runs and `_park_for_tool_review` is never reached. Here the double
    reports progress again, which is what drives the node into the guard.
    """
    run, store, deps, controller = _runtime(tmp_path)
    thy = _ParkingThy(parks=2)
    factory = _composed_graph_factory(tmp_path, thy, _MiraSubgraph(), deps, controller)
    factory(run).invoke(initial_runtime_state(run), {"configurable": {"thread_id": run.id}})
    nodes = _RuntimeGraphNodes(
        thy=thy,
        mira=_MiraSubgraph(),
        deps=deps,
        controller=controller,
        interview=None,
        preflight=None,
        classify=None,
        execution_gate=None,
        assurance=None,
    )

    nodes.run_thy(initial_runtime_state(run))

    assert thy.calls == [False, False]
    assert [command for command, _ in _transitions(store, run.id)] == [
        "start",
        "begin_execution",
        "wait_for_approval",
    ]
    assert nodes.route_after_thy(initial_runtime_state(run)) == END
    assert verify_events(store.events(run.id)).valid


def _question_limit_run(
    tmp_path: Path,
) -> tuple[Run, LocalRunStore, RunController, RunService]:
    """Drive a Run to the bounded interview's question-limit park through the composed graph.

    One field is left undeclared and ``max_questions=1``, so the single question the interview may
    ask is answered with a placeholder and the next interview pass has to escalate.
    """
    run, store, deps, controller = _runtime(tmp_path)
    profile = _complete_activity_profile(run, purpose="undeclared")
    interview = RiskInterviewService(
        store,
        MiraControlPlane(store, PolicyEngine(load_default_policy())),
        max_questions=1,
    )

    def begin_interview(state: RuntimeState, _: SubgraphDeps) -> RuntimeState:
        """Ask, or escalate, before the graph may reach preflight."""
        interview.begin(run, profile)
        return state

    checkpoints = LocalCheckpointRepository(tmp_path / "checkpoints")

    def graph_factory(_: Run) -> CompiledStateGraph:
        """Rebuild the composed graph over the one shared checkpoint directory."""
        return build_runtime_graph(
            _ThySubgraph(),
            _MiraSubgraph(),
            checkpointer=StateCheckpointer(checkpoints),
            deps=deps,
            controller=controller,
            interview=begin_interview,
        )

    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=InlineDispatcher(store, graph_factory),
        policy_sha256="0" * 64,
        graph_definition_hash="1" * 64,
        risk_interview=interview,
    )
    InlineDispatcher(store, graph_factory).submit(run.id)
    assert controller.current_state(run.id).wait_reason is WaitReason.INFORMATION

    service.answer_risk_interview(
        run.id,
        answer="undeclared",
        actor=Actor(kind=ActorKind.HUMAN, id="risk-owner", authenticated=False),
    )

    assert controller.current_state(run.id).wait_reason is WaitReason.APPROVAL
    return run, store, controller, service


def test_an_approved_question_limit_review_resumes_the_interview(tmp_path: Path) -> None:
    """The approval re-enters the interview instead of a Gate that has no decision to reuse.

    Found live in `run_5a9e9662d6ce4527bc1c250bf6b5b9fd`: the question-limit park carried no
    `resume_from`, so `resolve_approval` recorded `resume_from: approval` and
    `InlineDispatcher._resume_node` anchored the resumed pass at `gate`. That node's outgoing edge
    demands a policy decision the Run had never reached -- it never got past the interview -- so it
    raised `runtime graph Gate node did not produce a policy decision`, the Run ended `run.failed`
    with no governance decision at all, and the human's answer was spent for nothing.
    """
    run, store, controller, service = _question_limit_run(tmp_path)

    resumed = _answer(service, store, run.id, approved=True)

    assert ("resume", INTERVIEW_LIMIT_RESUME_TARGET) in _transitions(store, run.id)
    final = controller.current_state(run.id).state
    assert final.condition is RunCondition.TERMINAL
    assert final.outcome is RunOutcome.COMPLETED
    assert resumed.status.value == "COMPLETED"
    events = store.events(run.id)
    assert [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert not [
        event for event in events if "did not produce a policy decision" in str(event.payload)
    ]
    assert verify_events(events).valid


def test_a_question_limit_review_rejected_through_resolve_approval_blocks(tmp_path: Path) -> None:
    """A review refused through the Gate ends the Run on the existing rejection branch."""
    run, store, controller, service = _question_limit_run(tmp_path)

    _answer(service, store, run.id, approved=False)

    final = controller.current_state(run.id).state
    assert final.condition is RunCondition.TERMINAL
    assert final.outcome is RunOutcome.BLOCKED
    assert _review_requests(store, run.id) == 1
    # A terminal Run is an idempotent no-op, so nothing re-enters the interview to escalate again.
    service.resume(run.id, actor=_REVIEWER)

    assert _review_requests(store, run.id) == 1
    assert controller.current_state(run.id).state.outcome is RunOutcome.BLOCKED
    assert verify_events(store.events(run.id)).valid


def test_a_question_limit_review_rejected_out_of_band_blocks_on_a_plain_resume(
    tmp_path: Path,
) -> None:
    """The governance route records a refusal without transitioning; resume must honour it.

    Found by adversarial review: ``LocalApprovalService.resolve(approved=False)`` leaves the Run
    ``waiting: approval``; a plain resume that only knew the execution-start target re-entered
    the interview, which saw neither a resolved nor a pending review and requested the same
    review again -- unboundedly.
    """
    run, store, controller, service = _question_limit_run(tmp_path)
    log = RunEventLog(store, run.id)
    decision_id = pending_approvals(log.events())[0].decision_id
    LocalApprovalService(log).resolve(run.id, decision_id, approved=False, by=_REVIEWER)
    assert controller.current_state(run.id).state.wait_reason is WaitReason.APPROVAL

    service.resume(run.id, actor=_REVIEWER)

    final = controller.current_state(run.id).state
    assert final.condition is RunCondition.TERMINAL
    assert final.outcome is RunOutcome.BLOCKED
    assert _review_requests(store, run.id) == 1
    blocked = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "block"
    )
    assert blocked.payload["rejected_by"]["id"] == _REVIEWER.id
    assert verify_events(store.events(run.id)).valid


def _review_requests(store: LocalRunStore, run_id: str) -> int:
    """How many human reviews this Run has ever requested."""
    return sum(event.type is EventType.HUMAN_APPROVAL_REQUESTED for event in store.events(run_id))
