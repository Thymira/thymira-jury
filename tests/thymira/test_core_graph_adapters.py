"""Production Subgraph adapters and the composition-root graph factory (RA-CORE-06).

The ThyGraph and MIRA audit flow runs here are real (in-process, driven by a
``ScriptedProvider`` so no network call happens). The two integration tests drive a whole Run
from creation to a terminal state through ``build_runtime_graph`` and the local persistence stack.

MiraSubgraph runs MIRA-02's audit-agent fan-out inside the composition while leaving the single
run-level ``Gate.review_findings`` to the composition's ``review`` node: the tests here pin both
that the fan-out runs and that exactly one findings-level ``policy.decision`` is recorded per Run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL
from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import EchoArguments, FakeTool
from thymira import observability
from thymira.agents import (
    AgentCatalog,
    LLMToolCall,
    ModelRouteDeniedError,
    RiskClassification,
    load_agent_specs,
)
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.api import build_default_deps
from thymira.core import (
    GoalBoardService,
    GraphFactory,
    LiveOwnerContext,
    MiraControlPlane,
    MiraSubgraph,
    RiskInterviewService,
    RunController,
    RunEventLog,
    RuntimeState,
    RunTransitionKind,
    SubgraphDeps,
    ThyExecutionError,
    ThySubgraph,
    UsageLedger,
    UsageLedgerRegistry,
    WaitReason,
    build_audit_agent_runner,
    build_runtime_graph_factory,
    composed_graph_definition_hash,
    default_activity_profile,
    default_thy_catalog,
    graph_definition_hash,
    initial_runtime_state,
)
from thymira.core.graph.adapters import MAX_RISK_UNCERTAINTY_ATTEMPTS
from thymira.core.graph.risk_reconciliation import reconcile_reviewed_risk_classification
from thymira.events import InMemoryEventLog, canonical_json, sha256_text, verify_events
from thymira.mira import (
    AuditAgentOutput,
    AuditAgentSpec,
    AuditInput,
    MiraPreflightResult,
    canonical_graph_definition_hash,
    load_default_specs,
)
from thymira.mira.agents.verification import build_finding_verifier
from thymira.mira.checks import (
    AuditContext,
    AuditReport,
    ControlStatus,
    audit_run,
    replay_request_ledger,
)
from thymira.mira.kb import LocalRegulationStore, RegulationChunk
from thymira.mira.kb.ingest import write_jsonl
from thymira.mira.preflight import (
    MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX,
    RiskJudgment,
    load_default_packs,
)
from thymira.mira.tools import build_mira_tool_registry
from thymira.mira.verification import DiscoveryLimits
from thymira.policies import (
    ActionRule,
    BudgetRule,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    load_default_policy,
    load_policy_stack,
)
from thymira.schemas import (
    ActivityProfile,
    Actor,
    ActorKind,
    AuditFinding,
    Decision,
    Event,
    EventType,
    ExecutionOutcomeKind,
    Framework,
    ModelRoutePolicy,
    ProjectConfig,
    RiskAssessment,
    RiskLevel,
    Run,
    RunCondition,
    RunStage,
    Session,
    Severity,
    TerminalAuditBinding,
    TurnEnded,
    TurnEndReason,
    WorkItem,
    WorkResult,
    new_id,
)
from thymira.state import (
    LocalArtifactStore,
    LocalBoardRepository,
    LocalCheckpointRepository,
    LocalDagRepository,
    LocalLifecycleRepository,
    LocalRecordRepository,
    LocalRunStore,
    LocalSessionRepository,
)
from thymira.thy.graph import graph_definition_hash as thy_graph_definition_hash
from thymira.thy.models import (
    AgentTask,
    ArtifactRequirement,
    PlanOutput,
    ThyAgentKind,
    ThyPhase,
    ThyProgress,
)
from thymira.tools import ToolContext, ToolManager, ToolRegistry
from thymira.tools.builtins import builtins_registry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from thymira.schemas import Id


@dataclass(frozen=True, slots=True)
class _ThyOutputStub:
    """A minimal ``ThyOutput`` double: what ``ThySubgraph.invoke`` reads, and nothing else.

    Besides ``.error`` it now reads the fields it records on the trace's ``thy`` observation --
    the composing node cannot, because this adapter returns the runtime state unchanged.
    """

    error: str | None
    summary: str | None = None
    experiment_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    progress: ThyProgress | None = None
    usage: dict[str, int | float | None] = field(default_factory=dict)


_PLAN_OUTPUT = PlanOutput(
    tasks=(
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
)

# A policy that lets THY's plan through (the default stack escalates ``plan.proposed`` to a human,
# which no synchronous approver can satisfy) and leaves findings at the WARNING default so any
# MIRA finding routes the composed Run to completion rather than to human review.
_PLAN_PASSES = Policy(
    name="plan-passes",
    version="1.0",
    action_rules=(
        ActionRule(
            id="TEST-PLAN",
            action_types=("plan.proposed",),
            decision=Decision.PASS,
            reason="test policy: THY's plan needs no review",
        ),
    ),
)
TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind direct adapter providers to a code-owned route snapshot and model choice."""
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "test-model")
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _run(prompt: str = "analyze the dataset") -> Run:
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt=prompt,
        model_route_policy=TEST_ROUTE_POLICY,
    )


def _run_mira(mira: MiraSubgraph, run: Run, deps: SubgraphDeps) -> RuntimeState:
    """Run governance preflight before MIRA's evidence audit in direct adapter tests."""
    state = mira.preflight(initial_runtime_state(run), deps=deps)
    return mira.invoke(state, deps=deps)


def test_mira_preflight_escalates_repeated_risk_uncertainty_to_human_review(
    tmp_path: Path,
) -> None:
    """Repeated model uncertainty on one profile escalates to review instead of looping forever.

    Found live (2026-09-11) against ``examples/credit-risk``: every activity-profile fact was
    declared, yet MIRA's inherent-risk classifier kept landing just under
    ``RISK_CONFIDENCE_THRESHOLD`` and parked the Run through ``_request_human_context``, whose
    ``missing_information`` (``["risk_classification"]``) is not one of the fixed interview
    fields -- no ``activity_profile.questioned`` event a human could ever answer. ``resume`` (bug-
    hunt: preflight-information-park-is-unrecoverable) correctly re-attempts the identical
    judgement, but three real resumes in three real attempts reproduced the exact same park each
    time. This pins the bound: the ``MAX_RISK_UNCERTAINTY_ATTEMPTS``-th uncertain judgement
    escalates to ``REQUIRE_HUMAN_REVIEW`` (``wait_reason: approval``) instead of re-parking on
    ``wait_reason: information`` again.
    """
    run = _run()
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    event_log = RunEventLog(store, run.id)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(PolicyEngine(load_default_policy()), event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    controller = RunController(store)
    control_plane = MiraControlPlane(store, PolicyEngine(load_default_policy()))
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Profile credit applications.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="EU",
        data_categories=("credit_history",),
        sensitive_attributes=(),
        potential_consequences=("An urgent application could be reviewed late.",),
    )
    uncertain = RiskJudgment(
        risk_level=RiskLevel.UNKNOWN,
        activity_category="unknown",
        confidence=0.5,
        missing_information=("risk_classification",),
        justification=f"{MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX}: borderline confidence.",
    )
    provider = ScriptedProvider([uncertain] * (MAX_RISK_UNCERTAINTY_ATTEMPTS + 1))
    controller.advance(run.id, RunTransitionKind.START)

    # The Nth uncertain judgement itself reaches the bound (no wasted (N+1)th model call is
    # needed to notice), so the last of these attempts escalates rather than re-parking again.
    for attempt in range(1, MAX_RISK_UNCERTAINTY_ATTEMPTS + 1):
        mira = MiraSubgraph(
            run,
            profile,
            load_default_packs(),
            Actor.system(),
            provider=provider,
            route_policy=TEST_ROUTE_POLICY,
            control_plane=control_plane,
        )
        mira.preflight(initial_runtime_state(run), deps=deps)
        state = controller.current_state(run.id)
        if attempt < MAX_RISK_UNCERTAINTY_ATTEMPTS:
            assert state.wait_reason is WaitReason.INFORMATION, attempt
            controller.advance(
                run.id, RunTransitionKind.RESUME, payload={"resume_from": "information"}
            )
        else:
            assert state.wait_reason is WaitReason.APPROVAL, attempt

    events = store.events(run.id)
    assert EventType.HUMAN_APPROVAL_REQUESTED in [event.type for event in events]
    assert verify_events(events).valid

    # A human accepts proceeding despite the unresolved uncertainty -- out of band, the way the
    # API's approve route records it -- and `resume` re-enters preflight (bug-hunt's own "start"
    # anchor for this kind of park; see `InlineDispatcher._resume_node`).
    requested = next(
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.payload.get("summary", "").startswith("MIRA could not classify inherent risk")
    )
    recorded_assessment = next(
        event for event in events if event.type is EventType.RISK_ASSESSMENT_RECORDED
    )
    assert requested.payload["request_payload"] == {
        "activity_profile_id": profile.id,
        "activity_profile_version": profile.version,
        "max_attempts": MAX_RISK_UNCERTAINTY_ATTEMPTS,
        "reason": "risk_classification_uncertain",
        "risk_assessment_id": recorded_assessment.subject_id,
    }
    store.append(
        run.id,
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": requested.payload["decision_id"], "approved": True},
        expected_version=store.version(run.id),
        subject_id=run.id,
    )
    controller.advance(
        run.id,
        RunTransitionKind.RESUME,
        payload={"resume_from": "risk_uncertainty"},
    )

    mira = MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
        control_plane=control_plane,
    )
    mira.preflight(initial_runtime_state(run), deps=deps)

    # Still uncertain (the same scripted judgement), but a human already accepted that -- so this
    # is the fix's whole point: no second park, no infinite loop.
    final_state = controller.current_state(run.id)
    assert final_state.condition is RunCondition.ACTIVE
    assert final_state.wait_reason is None
    final_events = store.events(run.id)
    approval_requests = [
        event
        for event in final_events
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.payload.get("summary", "").startswith("MIRA could not classify inherent risk")
    ]
    assert len(approval_requests) == 1
    assert verify_events(final_events).valid


@pytest.mark.parametrize("mismatched_scope", ["assessment", "profile_version"])
def test_risk_reconciliation_refuses_an_approval_for_another_assessment_scope(
    mismatched_scope: str,
) -> None:
    """A Run-level approval cannot retire another assessment or profile version's finding."""
    run = _run()
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=2,
        run_id=run.id,
        purpose="Profile credit applications.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="EU",
        data_categories=("credit_history",),
        sensitive_attributes=(),
        potential_consequences=("An urgent application could be reviewed late.",),
    )
    assessment = RiskAssessment(
        id=new_id("assessment"),
        run_id=run.id,
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        assessor=Actor.system(),
        subject_kind="activity",
        subject_id=profile.activity_id,
        risk_level=RiskLevel.UNKNOWN,
        activity_category="unknown",
        confidence=0.5,
        missing_information=("risk_classification",),
        justification=f"{MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX}: borderline confidence.",
        assessment_method="mira-inherent-risk-model",
        assessment_method_version="1.0",
    )
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run.id,
        control_id="MIRA-RISK-001",
        framework=Framework.INTERNAL,
        title="Base risk cannot be classified",
        finding="Risk classification requires human review.",
        severity=Severity.MEDIUM,
        confidence=0.5,
    )
    preflight = MiraPreflightResult(
        risk_assessment=assessment,
        pack_bindings=(),
        audit_findings=(finding,),
    )
    risk = RiskProfile(
        risk_level="high",
        activity_category="data_analysis",
        confidence=0.9,
        needs_human_review=True,
    )
    request_scope: dict[str, object] = {
        "risk_assessment_id": assessment.id,
        "activity_profile_id": profile.id,
        "activity_profile_version": profile.version,
        "max_attempts": MAX_RISK_UNCERTAINTY_ATTEMPTS,
        "reason": "risk_classification_uncertain",
    }
    if mismatched_scope == "assessment":
        request_scope["risk_assessment_id"] = new_id("assessment")
    else:
        request_scope["activity_profile_version"] = profile.version - 1

    decision_id = new_id("decision")
    log = InMemoryEventLog(run.id)
    log.append(
        EventType.RISK_ASSESSMENT_RECORDED,
        Actor.system(),
        {"risk_assessment": assessment.model_dump(mode="json")},
        subject_id=assessment.id,
        producer="thymira.mira",
    )
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision_id,
            "summary": (
                "MIRA could not classify inherent risk with sufficient confidence after one "
                "attempt; human review is required."
            ),
            "request_payload": request_scope,
        },
        subject_id=run.id,
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": True},
        subject_id=run.id,
    )
    log.append(
        EventType.RISK_CLASSIFIED,
        Actor.system(),
        {
            "activity_profile_id": profile.id,
            "activity_profile_version": profile.version,
            "mira_risk_assessment_id": assessment.id,
            "risk_profile": risk.model_dump(mode="json"),
        },
        subject_id=profile.id,
        producer="thymira.core",
    )

    reconciled = reconcile_reviewed_risk_classification(
        preflight,
        risk,
        log.events(),
        profile,
        uncertainty_reviewed=True,
    )

    assert reconciled.risk_assessment == assessment
    assert reconciled.audit_findings == (finding,)


def test_final_mira_audit_retires_reviewed_risk_classification_uncertainty(
    tmp_path: Path,
) -> None:
    """A later reviewed Core classification resolves MIRA's synthetic missing-risk finding."""
    run = _run()
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    event_log = RunEventLog(store, run.id)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(PolicyEngine(load_default_policy()), event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    controller = RunController(store)
    control_plane = MiraControlPlane(store, PolicyEngine(load_default_policy()))
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Profile credit applications.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="EU",
        data_categories=("credit_history",),
        sensitive_attributes=(),
        potential_consequences=("An urgent application could be reviewed late.",),
    )
    uncertain = RiskJudgment(
        risk_level=RiskLevel.UNKNOWN,
        activity_category="unknown",
        confidence=0.5,
        missing_information=("risk_classification",),
        justification=f"{MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX}: borderline confidence.",
    )
    controller.advance(run.id, RunTransitionKind.START)
    MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        provider=ScriptedProvider([uncertain]),
        route_policy=TEST_ROUTE_POLICY,
        control_plane=control_plane,
    ).preflight(initial_runtime_state(run), deps=deps)

    requested = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.payload.get("summary", "").startswith("MIRA could not classify inherent risk")
    )
    store.append(
        run.id,
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": requested.payload["decision_id"], "approved": True},
        expected_version=store.version(run.id),
        subject_id=run.id,
    )
    controller.advance(
        run.id,
        RunTransitionKind.RESUME,
        payload={"resume_from": "risk_uncertainty"},
    )
    risk = RiskInterviewService(store, control_plane).classify(
        run,
        profile,
        event_log,
        ScriptedProvider(
            [
                {
                    "risk_level": "high",
                    "activity_category": "data_analysis",
                    "risk_factors": (),
                    "missing_information": (),
                    "confidence": 0.9,
                    "needs_human_review": True,
                    "summary": "High-impact analysis; reviewed before execution.",
                    "criteria": (),
                }
            ]
        ),
        route_policy=TEST_ROUTE_POLICY,
    )

    state = initial_runtime_state(run).model_copy(update={"risk_profile": risk})
    assert risk.risk_level == "high"
    assert risk.missing_information == ()
    result = MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
    ).invoke(state, deps=deps)

    assert not any(finding.control_id == "MIRA-RISK-001" for finding in result.findings)
    classified = next(
        event for event in store.events(run.id) if event.type is EventType.RISK_CLASSIFIED
    )
    assessed = next(
        event for event in store.events(run.id) if event.type is EventType.RISK_ASSESSMENT_RECORDED
    )
    assert classified.payload["mira_risk_assessment_id"] == assessed.subject_id
    assert verify_events(store.events(run.id)).valid


def _data_agent_catalog(directory: Path) -> AgentCatalog:
    """A single no-tool ``data`` agent whose scripted output is a ``DataProfileOutput``."""
    directory.mkdir(parents=True)
    (directory / "data.yaml").write_text(
        "name: data\n"
        "role: agent\n"
        "task_kinds: [analyze]\n"
        "max_turns: 2\n"
        "max_depth: 1\n"
        "system_prompt_ref: prompts/data.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
        newline="\n",
    )
    prompts = directory / "prompts"
    prompts.mkdir()
    (prompts / "data.md").write_text("You are the data agent.", encoding="utf-8", newline="\n")
    return load_agent_specs(directory, known_capabilities=frozenset())


def _subgraph_deps(run: Run, tmp_path: Path, *, policy: Policy) -> SubgraphDeps:
    """Build in-memory subgraph deps whose Gate shares the one event log THY/MIRA write to."""
    log = InMemoryEventLog(run.id)
    return SubgraphDeps(
        event_log=log,
        gate=Gate(PolicyEngine(policy), log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts" / run.id, run.id),
        tool_manager=ToolManager(builtins_registry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )


def _workspace(tmp_path: Path, *, frameworks: tuple[Framework, ...] = ()) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    governance = ""
    if frameworks:
        governance = "governance:\n  frameworks:\n" + "".join(
            f"    - {framework.value}\n" for framework in frameworks
        )
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n" + governance,
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _declared_activity_profile(run: Run) -> ActivityProfile:
    """Return stable, complete activity facts for integration tests that are not interviews."""
    return default_activity_profile(run).model_copy(
        update={
            "purpose": "Profile credit applications.",
            "affected_population": "Credit applicants.",
            "decision_effect": "Changes review order but not credit approval.",
            "autonomy": "Recommendation only.",
            "human_oversight": "An analyst can override every recommendation.",
            "jurisdiction": "EU",
            "data_categories": ("credit_history",),
            "sensitive_attributes": (),
            "potential_consequences": ("An urgent application could be reviewed late.",),
        }
    )


def _internal_audit_spec(
    name: str = "internal-audit", *, tool_allowlist: tuple[str, ...] = ()
) -> AuditAgentSpec:
    """A minimal INTERNAL audit agent with an optional explicitly declared tool surface."""
    return AuditAgentSpec(
        name=name,
        framework=Framework.INTERNAL,
        task_kinds=("audit_judgement",),
        tool_allowlist=tool_allowlist,
        max_turns=1,
        system_prompt="Return candidate findings only.",
    )


def _agent_finding(run_id: str, control_id: str) -> AuditFinding:
    """An injected agent candidate carrying the INTERNAL framework the test spec declares."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id=control_id,
        framework=Framework.INTERNAL,
        title=f"Agent finding {control_id}",
        finding=f"Candidate finding from {control_id}.",
        severity=Severity.MEDIUM,
        confidence=0.9,
    )


def _scripted_agent_response(control_id: str = "AGENT-INTERNAL-1") -> dict[str, object]:
    """One scripted structured response validating as the runner's ``_AuditAgentResponse``."""
    return {
        "findings": [
            {
                "control_id": control_id,
                "title": "Composed audit-agent finding",
                "finding": "The audit agent reasoned over the bounded evidence projection.",
                "severity": "MEDIUM",
                "confidence": 0.9,
            }
        ]
    }


def _mira_agent_starts(events: list[Event]) -> list[Event]:
    """Return the audit-agent start events MIRA emitted, distinct from THY's agent starts."""
    return [
        event
        for event in events
        if event.type is EventType.AGENT_STARTED and event.producer == "thymira.mira"
    ]


# --------------------------------------------------------------------------- structure / identity


def test_default_thy_catalog_carries_the_three_thy_agent_kinds() -> None:
    catalog = default_thy_catalog()

    assert set(catalog.names()) == {"data", "coding", "experiment"}
    # Each spec keeps its resolved system prompt and output schema through the merge.
    for name in catalog.names():
        assert catalog.system_prompt(name)
        assert catalog.output_schema(name) is not None


def test_composed_graph_definition_hash_matches_the_real_subgraphs() -> None:
    run = _run()
    thy = ThySubgraph(run, default_thy_catalog())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    assert thy.name == "thy"
    assert mira.name == "mira"
    assert thy.graph_version() == thy_graph_definition_hash()
    assert mira.graph_version() == canonical_graph_definition_hash()
    # The Run-independent default equals the hash of the actual composed subgraphs.
    assert composed_graph_definition_hash() == graph_definition_hash(thy, mira)


def test_default_activity_profile_declares_unknown_facts_instead_of_fabricating_them() -> None:
    run = _run()
    profile = default_activity_profile(run)

    assert profile.run_id == run.id
    assert profile.project_id is None
    # Honest: the risk-classifying facts are undeclared, so MIRA cannot invent a low-risk profile.
    assert profile.purpose == "undeclared"
    assert profile.data_categories == ("undeclared",)
    assert profile.sensitive_attributes == ("undeclared",)
    assert profile.potential_consequences == ("undeclared",)
    assert profile.jurisdiction == "undeclared"


# --------------------------------------------------------------------------- ThySubgraph


def test_thy_subgraph_drives_a_real_thy_graph_and_preserves_identity(tmp_path: Path) -> None:
    run = _run()
    execute_entries: list[str] = []
    deps = replace(
        _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES),
        before_thy_execute=lambda: execute_entries.append(run.id),
    )
    thy = ThySubgraph(
        run,
        _data_agent_catalog(tmp_path / "catalog"),
        provider=ScriptedProvider([_PLAN_OUTPUT, DataProfileOutput(row_count=1, columns=("a",))]),
    )

    result = thy.invoke(initial_runtime_state(run), deps=deps)

    assert result.run_id == run.id
    assert result.project_id == run.project_id
    events = deps.event_log.events()
    # A real ThyGraph ran: it gated its plan and delegated the one task to the data agent.
    assert any(event.type is EventType.POLICY_DECISION for event in events)
    assert any(event.type is EventType.AGENT_MESSAGE for event in events)
    assert execute_entries == [run.id]


def test_thy_subgraph_raises_when_thy_reports_an_error(tmp_path: Path) -> None:
    """The default policy escalates ``plan.proposed`` to a human, so THY halts with an error."""
    run = _run()
    execute_entries: list[str] = []
    deps = replace(
        _subgraph_deps(run, tmp_path, policy=load_policy_stack()),
        before_thy_execute=lambda: execute_entries.append(run.id),
    )
    thy = ThySubgraph(
        run,
        _data_agent_catalog(tmp_path / "catalog"),
        provider=ScriptedProvider([_PLAN_OUTPUT]),
    )

    with pytest.raises(ThyExecutionError, match="THY halted"):
        thy.invoke(initial_runtime_state(run), deps=deps)

    assert execute_entries == []


def test_thy_subgraph_threads_real_tools_when_a_workspace_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured ``project_dir`` reaches ``run_thy`` as a real tool registry and context.

    Before this, ``ThySubgraph.invoke`` never passed either through, so ``AgentRunner`` (which
    binds tools only when both are non-``None``) built every delegated agent with an empty tools
    tuple -- no real ``run_python``/MLflow call ever happened in production; a real model just
    generated text shaped like a tool result. This pins the wiring without needing to script an
    actual tool call (``tests/thymira/test_thy_e2e.py`` already covers that at the ``run_thy``
    level).
    """
    from thymira.core.graph import adapters as adapters_module

    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    workspace = _workspace(tmp_path)
    thy = ThySubgraph(run, _data_agent_catalog(tmp_path / "catalog"), project_dir=workspace)

    captured: dict[str, object] = {}

    def fake_run_thy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _ThyOutputStub(error=None)

    monkeypatch.setattr(adapters_module, "run_thy", fake_run_thy)
    thy.invoke(initial_runtime_state(run), deps=deps)

    assert captured["tool_registry"] is deps.tool_manager.registry
    tool_context = captured["tool_context"]
    assert isinstance(tool_context, ToolContext)
    assert tool_context.run_id == run.id
    # Bug-hunt H2: the workspace a tool step writes into is this Run's own subdirectory, not the
    # bare project directory shared by every Run against it.
    assert tool_context.workspace == workspace / ".thymira" / "runtime" / "workspaces" / run.id
    assert tool_context.gate is deps.gate
    assert tool_context.artifact_store is deps.artifact_store


def test_thy_subgraph_stays_tool_less_without_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``project_dir`` means no workspace a tool could act on, so THY stays tool-less."""
    from thymira.core.graph import adapters as adapters_module

    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    thy = ThySubgraph(run, _data_agent_catalog(tmp_path / "catalog"))

    captured: dict[str, object] = {}

    def fake_run_thy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _ThyOutputStub(error=None)

    monkeypatch.setattr(adapters_module, "run_thy", fake_run_thy)
    thy.invoke(initial_runtime_state(run), deps=deps)

    assert captured["tool_registry"] is None
    assert captured["tool_context"] is None


def test_thy_subgraph_seeds_a_resumed_pass_from_the_carried_progress(tmp_path: Path) -> None:
    """A carried plan means no plan call and no plan decision; a finished pass clears it."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    thy = ThySubgraph(run, _data_agent_catalog(tmp_path / "catalog"), provider=provider)
    seeded = initial_runtime_state(run).model_copy(
        update={"thy_progress": ThyProgress(plan=_PLAN_OUTPUT.tasks)}
    )

    result = thy.invoke(seeded, deps=deps)

    assert result.thy_progress is None
    # One model call: the delegated data agent. Plan neither replanned nor re-gated the plan --
    # a second `plan.proposed` decision would be a new authorization for work already authorized.
    assert len(provider.calls) == 1
    assert not any(
        event.type is EventType.POLICY_DECISION
        and event.payload.get("rule_id") == _PLAN_PASSES.action_rules[0].id
        for event in deps.event_log.events()
    )


def test_thy_subgraph_rejects_a_state_for_a_different_run(tmp_path: Path) -> None:
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    thy = ThySubgraph(run, _data_agent_catalog(tmp_path / "catalog"))

    with pytest.raises(ValueError, match="different Run"):
        thy.invoke(initial_runtime_state(_run()), deps=deps)


# --------------------------------------------------------------------------- MiraSubgraph


def test_mira_subgraph_produces_findings_and_emits_the_evidence_events(tmp_path: Path) -> None:
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    preflight_state = mira.preflight(initial_runtime_state(run), deps=deps)
    preflight_types = {event.type for event in deps.event_log.events()}
    assert EventType.RISK_ASSESSMENT_RECORDED in preflight_types
    assert EventType.PACK_BINDING_RECORDED in preflight_types
    assert EventType.CONTROL_EVALUATION_RECORDED not in preflight_types

    result = mira.invoke(preflight_state, deps=deps)

    assert result.run_id == run.id
    assert result.project_id == run.project_id
    # Real, deterministic findings (undeclared risk facts + a missing-evidence control).
    assert result.findings
    assert all(finding.run_id == run.id for finding in result.findings)
    types = {event.type for event in deps.event_log.events()}
    assert EventType.AUDIT_STARTED in types
    assert EventType.RISK_ASSESSMENT_RECORDED in types
    assert EventType.PACK_BINDING_RECORDED in types
    assert EventType.CONTROL_EVALUATION_RECORDED in types
    assert EventType.AUDIT_FINDING in types
    assert not any(finding.control_id == "A2" for finding in result.findings)


def test_mira_subgraph_audits_a_failed_turn_through_the_final_flow(tmp_path: Path) -> None:
    """A failed owner exit still reaches canonical MIRA evidence and a replayable report."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    state = mira.preflight(initial_runtime_state(run), deps=deps)
    deps.event_log.append(
        EventType.RUN_FAILED,
        Actor.system(),
        {
            "error": "worker failed after dispatch",
            "cause": {
                "code": "worker_failure",
                "phase": "worker.execute",
                "exception_type": "TestFailure",
                "message": "worker failed after dispatch",
                "effects_may_have_occurred": True,
            },
        },
    )

    terminal = mira.audit_terminal(state, deps=deps)

    assert terminal.run_id == run.id
    events = deps.event_log.events()
    completed = [event for event in events if event.type is EventType.AUDIT_COMPLETED]
    assert len(completed) == 1
    report = AuditReport.model_validate(completed[0].payload["audit_report"])
    assert report.run_id == run.id
    assert EventType.CONTROL_EVALUATION_RECORDED in {event.type for event in events}
    assert verify_events(events).valid


@pytest.mark.integration
def test_runtime_factory_exposes_owner_bound_terminal_mira_audit(tmp_path: Path) -> None:
    """The owner-bound terminal flow retains its model usage and replayable request evidence."""
    root = tmp_path / "runtime"
    run_store = LocalRunStore(root / "runs")
    sessions = LocalSessionRepository(root / "repositories")
    repository = LocalLifecycleRepository(root, run_store=run_store, session_repository=sessions)
    session = Session(id=new_id("session"), project_id=new_id("project"), client="test")
    run = Run(
        id=new_id("run"),
        project_id=session.project_id,
        session_id=session.id,
        prompt="terminal audit composition",
        model_route_policy=TEST_ROUTE_POLICY,
    )
    work = WorkItem(
        run_id=run.id,
        ordinal=0,
        kind="run.execute",
        idempotency_key=f"terminal:{run.id}",
    )
    committed = repository.commit_publication(repository.prepare_publication(session, run, work))
    committed.close()
    owner_context = LiveOwnerContext()
    usage_ledgers = UsageLedgerRegistry()
    provider = ScriptedProvider(
        [
            RiskJudgment(
                risk_level=RiskLevel.LOW,
                activity_category="declared_no_sensitive_data",
                confidence=0.99,
                justification="The complete profile declares no sensitive attributes.",
            )
        ]
    )

    def complete_profile(candidate: Run) -> ActivityProfile:
        """Return a complete profile so terminal preflight performs one model judgement."""
        return ActivityProfile(
            id=new_id("profile"),
            activity_id=new_id("activity"),
            version=1,
            run_id=candidate.id,
            purpose="Describe a bounded local dataset.",
            affected_population="No person is directly affected by this descriptive test.",
            decision_effect="The report does not make or recommend a decision.",
            autonomy="Descriptive analysis only.",
            human_oversight="A human reviews the report before any later use.",
            jurisdiction="EU",
            data_categories=("non_sensitive_test_data",),
            sensitive_attributes=(),
            potential_consequences=("An analyst could misread the report.",),
        )

    factory = build_runtime_graph_factory(
        run_store,
        gate_factory=lambda run_id: Gate(
            PolicyEngine(load_policy_stack()), RunEventLog(run_store, run_id)
        ),
        artifact_store_factory=lambda run_id: LocalArtifactStore(
            root / "artifacts" / run_id, run_id
        ),
        tool_manager=ToolManager(builtins_registry()),
        record_repository=LocalRecordRepository(root / "records"),
        checkpoint_repository=LocalCheckpointRepository(root / "checkpoints"),
        plan_repository=LocalBoardRepository(root / "boards"),
        dag_repository=LocalDagRepository(root / "dag"),
        specs=(),
        provider=provider,
        activity_profile_factory=complete_profile,
        usage_ledgers=usage_ledgers,
        owner_provider=owner_context.for_run,
        owner_binder=owner_context.bind,
    )
    terminal_auditor = getattr(factory, "audit_terminal", None)
    assert callable(terminal_auditor)

    claim = repository.claim_work(work.work_id, "terminal-worker")
    if claim is None:
        raise AssertionError("test setup could not claim work")
    owner = repository.acquire_owner(run.id, claim.claim_token)
    if owner is None:
        raise AssertionError("test setup could not acquire owner")
    with repository.run_handle(owner) as handle, owner_context.bind(handle):
        result = WorkResult(kind=ExecutionOutcomeKind.FAILED)
        repository.mark_dispatched(claim, owner)
        repository.settle_work(claim, result, owner)
        turn = TurnEnded(
            turn_id=new_id("turn"),
            run_id=run.id,
            work_ids=(work.work_id,),
            step_count=0,
            end_reason=TurnEndReason.FAILED,
        )
        handle.append(
            EventType.TURN_ENDED,
            Actor.system(),
            turn.to_json_dict(),
            expected_version=repository.version(run.id),
        )
        terminal_auditor(
            handle,
            work,
            result,
            TerminalAuditBinding(
                run_id=run.id,
                work_id=work.work_id,
                turn_id=turn.turn_id,
                outcome_kind=result.kind,
                result_sha256=sha256_text(canonical_json(result.to_json_dict())),
                end_reason=TurnEndReason.FAILED,
            ),
        )

    events = run_store.events(run.id)
    assert any(event.type is EventType.AUDIT_COMPLETED for event in events)
    assert usage_ledgers.for_run(run.id).snapshot()["requests"] == 1
    replay = replay_request_ledger(tuple(events))
    assert len(replay.requests) == 1
    assert len(replay.responses) == 1
    assert replay.in_flight == ()
    assert verify_events(events).valid


@pytest.mark.integration
@pytest.mark.parametrize("route_policy_kind", ["static", "callable"])
def test_terminal_audit_applies_narrowed_route_policy_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_policy_kind: str,
) -> None:
    """A terminal audit honours the factory's current narrowing, not the wider Run snapshot."""
    root = tmp_path / "runtime"
    run_store = LocalRunStore(root / "runs")
    repository = LocalLifecycleRepository(
        root,
        run_store=run_store,
        session_repository=LocalSessionRepository(root / "repositories"),
    )
    session = Session(id=new_id("session"), project_id=new_id("project"), client="test")
    run_policy = ModelRoutePolicy.from_routes(
        ("test-model", "unused-model"), authority="code-owned-tests"
    )
    run = Run(
        id=new_id("run"),
        project_id=session.project_id,
        session_id=session.id,
        prompt="terminal route-policy narrowing",
        model_route_policy=run_policy,
    )
    work = WorkItem(
        run_id=run.id,
        ordinal=0,
        kind="run.execute",
        idempotency_key=f"terminal-route:{run.id}",
    )
    committed = repository.commit_publication(repository.prepare_publication(session, run, work))
    committed.close()

    # Persist preflight without a provider so the terminal model boundary under test is the audit
    # agent itself. This keeps an unrelated risk-classification route from obscuring the result.
    for variable in (
        "THYMIRA_MIRA_MODEL",
        "THYMIRA_ORCHESTRATOR_MODEL",
        "THYMIRA_MODEL_FRONTIER",
        "THYMIRA_MODEL_STANDARD",
        "THYMIRA_MODEL_FAST",
        "THYMIRA_AGENT_MODEL",
        "THYMIRA_MODEL",
    ):
        monkeypatch.setenv(variable, "")
    profile, event_log, tool_manager = (
        _declared_activity_profile(run),
        RunEventLog(run_store, run.id),
        ToolManager(builtins_registry()),
    )

    def artifact_store_factory(run_id: Id) -> LocalArtifactStore:
        return LocalArtifactStore(root / "artifacts" / run_id, run_id)

    record_repository, plan_repository, dag_repository = (
        LocalRecordRepository(root / "records"),
        LocalBoardRepository(root / "boards"),
        LocalDagRepository(root / "dag"),
    )
    MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.INTERNAL,),
        route_policy=run_policy,
    ).preflight(
        initial_runtime_state(run),
        deps=SubgraphDeps(
            event_log=event_log,
            gate=Gate(PolicyEngine(load_policy_stack()), event_log),
            artifact_store=artifact_store_factory(run.id),
            tool_manager=tool_manager,
            record_repository=record_repository,
            usage_ledger=UsageLedger(),
            plan_repository=plan_repository,
            dag_repository=dag_repository,
        ),
    )
    monkeypatch.setenv("THYMIRA_MIRA_MODEL", "test-model")

    narrowed_policy = run_policy.narrowed_to(())
    resolved_runs: list[str] = []

    def resolve_route_policy(candidate: Run) -> ModelRoutePolicy:
        """Return the current operator narrowing for this terminal Run."""
        resolved_runs.append(candidate.id)
        return narrowed_policy

    configured_route_policy = (
        narrowed_policy if route_policy_kind == "static" else resolve_route_policy
    )
    provider = ScriptedProvider([{"findings": []}])
    owner_context = LiveOwnerContext()
    factory = build_runtime_graph_factory(
        run_store,
        gate_factory=lambda run_id: Gate(
            PolicyEngine(load_policy_stack()), RunEventLog(run_store, run_id)
        ),
        artifact_store_factory=artifact_store_factory,
        tool_manager=tool_manager,
        record_repository=record_repository,
        checkpoint_repository=LocalCheckpointRepository(root / "checkpoints"),
        plan_repository=plan_repository,
        dag_repository=dag_repository,
        specs=(_internal_audit_spec(),),
        provider=provider,
        activity_profile_factory=lambda _run: profile,
        project_config=ProjectConfig.model_validate(
            {
                "project": {"name": "terminal-route-policy", "domain": "tests"},
                "governance": {"frameworks": [Framework.INTERNAL.value]},
            }
        ),
        owner_provider=owner_context.for_run,
        owner_binder=owner_context.bind,
        route_policy=configured_route_policy,
    )
    terminal_auditor = getattr(factory, "audit_terminal", None)
    assert callable(terminal_auditor)

    claim = repository.claim_work(work.work_id, "terminal-route-worker")
    if claim is None:
        raise AssertionError("test setup could not claim work")
    owner = repository.acquire_owner(run.id, claim.claim_token)
    if owner is None:
        raise AssertionError("test setup could not acquire owner")
    with repository.run_handle(owner) as handle, owner_context.bind(handle):
        result = WorkResult(kind=ExecutionOutcomeKind.FAILED)
        repository.mark_dispatched(claim, owner)
        repository.settle_work(claim, result, owner)
        turn = TurnEnded(
            turn_id=new_id("turn"),
            run_id=run.id,
            work_ids=(work.work_id,),
            step_count=0,
            end_reason=TurnEndReason.FAILED,
        )
        handle.append(
            EventType.TURN_ENDED,
            Actor.system(),
            turn.to_json_dict(),
            expected_version=repository.version(run.id),
        )
        with pytest.raises(ModelRouteDeniedError, match="test-model"):
            terminal_auditor(
                handle,
                work,
                result,
                TerminalAuditBinding(
                    run_id=run.id,
                    work_id=work.work_id,
                    turn_id=turn.turn_id,
                    outcome_kind=result.kind,
                    result_sha256=sha256_text(canonical_json(result.to_json_dict())),
                    end_reason=TurnEndReason.FAILED,
                ),
            )

    events = run_store.events(run.id)
    denials = [event for event in events if event.type is EventType.MODEL_ROUTE_DENIED]
    assert provider.calls == []
    assert not any(event.type is EventType.MODEL_REQUEST_RECORDED for event in events)
    assert len(denials) == 1
    assert denials[0].payload["reason"] == "route_not_allowed"
    assert denials[0].payload["policy_sha256"] == narrowed_policy.sha256
    assert resolved_runs == ([run.id] if route_policy_kind == "callable" else [])
    assert verify_events(events).valid


def test_mira_subgraph_reads_owner_board_history_in_the_core_path(tmp_path: Path) -> None:
    """Core passes the owner repositories through to MIRA's independent board replay."""
    run = _run()
    base_deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    plan_repository = LocalBoardRepository(tmp_path / "boards")
    GoalBoardService(plan_repository).record_round(run.id, cause="schema")
    deps = replace(
        base_deps,
        plan_repository=plan_repository,
        dag_repository=LocalDagRepository(tmp_path / "dag"),
    )
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    mira.preflight(initial_runtime_state(run), deps=deps)

    replay_events = [
        event for event in deps.event_log.events() if event.type is EventType.MIRA_BOARD_REPLAYED
    ]
    assert len(replay_events) == 1
    assert replay_events[0].payload["plan_revision"] == 1


def test_fresh_mira_subgraph_restores_preflight_from_events(tmp_path: Path) -> None:
    """A resumed graph hydrates preflight instead of appending duplicate governance evidence."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    profile = default_activity_profile(run)
    mira = MiraSubgraph(run, profile, load_default_packs(), Actor.system())

    _run_mira(mira, run, deps)
    restored = MiraSubgraph(run, profile, load_default_packs(), Actor.system())
    restored.invoke(initial_runtime_state(run), deps=deps)

    events = deps.event_log.events()
    assert sum(event.type is EventType.RISK_ASSESSMENT_RECORDED for event in events) == 1
    assert sum(event.type is EventType.PACK_BINDING_RECORDED for event in events) == 2
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in events) == 2


class _ScoringClient:
    """A stand-in Langfuse client that records the scores the audit hands it."""

    def __init__(self, scores: list[dict[str, object]]) -> None:
        self._scores = scores

    def create_trace_id(self, *, seed: str | None = None) -> str:
        """Derive a trace id the way the real client does."""
        return (seed or "x").ljust(32, "0")[:32]

    def create_score(self, **fields: object) -> None:
        """Record one score."""
        self._scores.append(fields)


def test_mira_subgraph_mirrors_every_control_verdict_onto_the_run_s_trace(tmp_path: Path) -> None:
    """Pinned on the composed graph because that is the only path a Run takes.

    The obvious home for this was `MiraAuditOrchestrator.assemble_result`, and a score written
    there would never have been emitted: the graph drives `run_preflight` and `run_deterministic`
    separately and assembles the findings itself, so `assemble_result` has no production caller.
    A real end-to-end Run is what caught it — the trace came back with the Gate's decisions and
    none of MIRA's.
    """
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())
    scored: list[dict[str, object]] = []
    observability.configure(client=_ScoringClient(scored))
    try:
        mira.invoke(initial_runtime_state(run), deps=deps)
    finally:
        observability.reset()

    names = [str(score["name"]) for score in scored]
    assert [name for name in names if name.startswith("mira.control.")]
    assert names.count("mira.controls.failed") == 1
    assert names.count("mira.audit.status") == 1
    assert all(score["trace_id"] == run.id.ljust(32, "0")[:32] for score in scored)


def test_an_audit_still_completes_when_the_trace_backend_is_broken(tmp_path: Path) -> None:
    """MIRA's verdict may never depend on telemetry reaching anyone."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())
    observability.configure(client=object())
    try:
        result = mira.invoke(initial_runtime_state(run), deps=deps)
    finally:
        observability.reset()

    assert result.findings


def test_mira_subgraph_never_calls_the_gate(tmp_path: Path) -> None:
    """The composition owns the single ``review_findings`` decision; MIRA writes no verdict."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    _run_mira(mira, run, deps)

    types = {event.type for event in deps.event_log.events()}
    assert EventType.POLICY_DECISION not in types
    # The canonical MIRA flow owns the complete audit fact; Core still owns the Gate decision.
    assert EventType.AUDIT_COMPLETED in types


def test_mira_subgraph_with_no_specs_runs_zero_audit_agents(tmp_path: Path) -> None:
    """The default (empty) fan-out matches the pre-MIRA-02 deterministic-only behaviour."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())

    _run_mira(mira, run, deps)

    assert _mira_agent_starts(deps.event_log.events()) == []


def test_mira_subgraph_runs_injected_agents_and_merges_their_findings(tmp_path: Path) -> None:
    """The fan-out appends agent findings after the deterministic ones, before the single Gate."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    specs = (_internal_audit_spec("first"), _internal_audit_spec("second"))
    calls: list[tuple[str, str]] = []

    def fake_runner(
        spec: AuditAgentSpec,
        run_id: str,
        events: tuple[Event, ...],
        report: AuditReport,
    ) -> AuditAgentOutput:
        del events
        calls.append((spec.name, report.run_id))
        control_id = "AGENT-FIRST" if spec.name == "first" else "AGENT-SECOND"
        return AuditAgentOutput.from_spec(spec, findings=(_agent_finding(run_id, control_id),))

    mira = MiraSubgraph(
        run,
        default_activity_profile(run),
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.INTERNAL,),
        specs=specs,
        run_agent=fake_runner,
    )

    result = _run_mira(mira, run, deps)

    control_ids = [finding.control_id for finding in result.findings]
    # Both agents ran in spec order, each over the composed Run and its deterministic report.
    assert calls == [("first", run.id), ("second", run.id)]
    # Deterministic findings are preserved and the agent candidates are appended, in caller order.
    assert "AGENT-FIRST" in control_ids
    assert "AGENT-SECOND" in control_ids
    assert control_ids[-2:] == ["AGENT-FIRST", "AGENT-SECOND"]
    assert any(finding.control_id.startswith("MIRA-RISK") for finding in result.findings)
    # Still no run-level verdict: the composition's ``review`` node owns the one Gate call.
    types = {event.type for event in deps.event_log.events()}
    assert EventType.POLICY_DECISION not in types
    assert EventType.AUDIT_COMPLETED in types


def test_mira_subgraph_deduplicates_an_agent_echo_of_a_deterministic_finding(
    tmp_path: Path,
) -> None:
    """An agent that re-states a deterministic finding collapses to one in the canonical flow."""
    run = _run()
    profile = default_activity_profile(run)

    # Both runs carry the same roster, because the roster is what declares the governance
    # frameworks the requirements-coverage control (A26) asserts over. A baseline with no specs
    # would declare none, and the two finding sets would differ for that reason alone.
    spec = _internal_audit_spec("echo").model_copy(update={"framework": Framework.METHODOLOGY})

    def silent_runner(
        spec: AuditAgentSpec,
        _run_id: str,
        _events: tuple[Event, ...],
        _report: AuditReport,
    ) -> AuditAgentOutput:
        return AuditAgentOutput.from_spec(spec)

    baseline_deps = _subgraph_deps(run, tmp_path / "baseline", policy=load_policy_stack())
    baseline_mira = MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.METHODOLOGY,),
        specs=(spec,),
        run_agent=silent_runner,
    )
    baseline = _run_mira(baseline_mira, run, baseline_deps)
    echoed = next(
        finding for finding in baseline.findings if finding.framework is Framework.METHODOLOGY
    )

    def echo_runner(
        spec: AuditAgentSpec,
        _run_id: str,
        _events: tuple[Event, ...],
        _report: AuditReport,
    ) -> AuditAgentOutput:
        # A fresh id but the same control, statement and evidence: the dedup key must collapse it.
        duplicate = echoed.model_copy(update={"id": new_id("finding")})
        return AuditAgentOutput.from_spec(spec, findings=(duplicate,))

    deps = _subgraph_deps(run, tmp_path / "echo", policy=load_policy_stack())
    mira = MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.METHODOLOGY,),
        specs=(spec,),
        run_agent=echo_runner,
    )

    result = _run_mira(mira, run, deps)

    matching = [finding for finding in result.findings if finding.control_id == echoed.control_id]
    assert len(matching) == 1
    # The composed audit is faithful to the standalone deterministic audit's finding set.
    assert {finding.control_id for finding in result.findings} == {
        finding.control_id for finding in baseline.findings
    }


def test_mira_subgraph_runs_a_real_scripted_audit_agent(tmp_path: Path) -> None:
    """Drive the real runner with a ``ScriptedProvider`` inside the composed MIRA subgraph.

    Lifecycle events and a framework-tagged finding land in the composed Run, with model-policy
    checks recorded before the provider call.
    """
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    provider = ScriptedProvider([_scripted_agent_response()])
    mira = MiraSubgraph(
        run,
        default_activity_profile(run),
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.INTERNAL,),
        specs=(_internal_audit_spec(),),
        provider=provider,
    )

    result = _run_mira(mira, run, deps)

    events = deps.event_log.events()
    assert len(_mira_agent_starts(events)) == 1
    assert any(event.type is EventType.MODEL_SELECTED for event in events)
    assert any(
        event.type is EventType.AGENT_COMPLETED and event.producer == "thymira.mira"
        for event in events
    )
    agent_findings = [
        finding for finding in result.findings if finding.control_id == "model:AGENT-INTERNAL-1"
    ]
    assert len(agent_findings) == 1
    assert agent_findings[0].framework is Framework.INTERNAL
    decisions = [event for event in events if event.type is EventType.POLICY_DECISION]
    assert decisions


def test_mira_subgraph_connects_allowlisted_regulation_search_and_carries_its_evidence(
    tmp_path: Path,
) -> None:
    """An EU audit can read its local source even while the audited activity needs review."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    chunk = RegulationChunk.from_text(
        source_id="eu-ai-act-art-14",
        version="2024",
        framework=Framework.EU_AI_ACT,
        location="Article 14",
        text="Human oversight must enable intervention and override.",
    )
    corpus_path = tmp_path / "regulation.jsonl"
    write_jsonl((chunk,), corpus_path)
    registry = build_mira_tool_registry(LocalRegulationStore(corpus_path))
    external_ref = f"{chunk.source_id}/{chunk.location}"
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={"query": "human oversight", "framework": "EU_AI_ACT", "k": 1},
            ),
            LLMToolCall(
                id="call-output",
                name="final_result",
                arguments={
                    "findings": [
                        {
                            "control_id": "REG-001",
                            "title": "Human oversight evidence is incomplete",
                            "finding": "The run does not demonstrate an intervention path.",
                            "severity": "HIGH",
                            "confidence": 0.9,
                            "evidence": [
                                {
                                    "kind": "external",
                                    "ref": external_ref,
                                    "sha256": chunk.sha256,
                                }
                            ],
                        }
                    ]
                },
            ),
        ]
    )
    mira = MiraSubgraph(
        run,
        default_activity_profile(run),
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.EU_AI_ACT,),
        specs=(
            _internal_audit_spec(tool_allowlist=("search_regulation",)).model_copy(
                update={"framework": Framework.EU_AI_ACT, "max_turns": 2}
            ),
        ),
        provider=provider,
        tool_registry=registry,
        workspace=tmp_path,
    )

    state = mira.preflight(initial_runtime_state(run), deps=deps)
    state = state.model_copy(
        update={
            "risk_profile": RiskProfile(
                risk_level="medium",
                activity_category="data_analysis",
                missing_information=("downstream_use",),
                confidence=0.82,
                needs_human_review=True,
            )
        }
    )
    result = mira.invoke(state, deps=deps)

    events = deps.event_log.events()
    starts = [event for event in events if event.type is EventType.TOOL_STARTED]
    completes = [event for event in events if event.type is EventType.TOOL_COMPLETED]
    tool_decisions = [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "tool_call"
    ]
    finding = next(finding for finding in result.findings if finding.control_id == "model:REG-001")

    assert len(starts) == len(completes) == len(tool_decisions) == 1
    assert starts[0].payload["tool"] == "search_regulation"
    assert completes[0].payload["result_sha256"]
    assert tool_decisions[0].payload["decision"] == Decision.PASS.value
    assert tool_decisions[0].payload["rule_id"] == "GOV-005"
    assert not any(
        event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.subject_id == tool_decisions[0].subject_id
        for event in events
    )
    assert result.risk_profile == state.risk_profile
    assert finding.evidence[0].ref == external_ref
    assert finding.evidence[0].sha256 == chunk.sha256
    assert any(
        event.type is EventType.AUDIT_FINDING
        and event.payload["finding"]["control_id"] == "model:REG-001"
        for event in events
    )


def test_mira_subgraph_uses_canonical_discovery_and_verification(tmp_path: Path) -> None:
    """Core reuses MIRA's bounded discovery and drops unsupported agent findings once."""
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=load_policy_stack())
    provider = ScriptedProvider(
        [
            _scripted_agent_response(),
            _scripted_agent_response(),
            {"supported": False},
        ]
    )
    mira = MiraSubgraph(
        run,
        default_activity_profile(run),
        load_default_packs(),
        Actor.system(),
        frameworks=(Framework.INTERNAL,),
        specs=(_internal_audit_spec(),),
        provider=provider,
        verify_finding=build_finding_verifier(provider, route_policy=TEST_ROUTE_POLICY),
        discovery=DiscoveryLimits(max_rounds=4),
    )

    result = _run_mira(mira, run, deps)

    assert len(_mira_agent_starts(deps.event_log.events())) == 2
    assert "AGENT-INTERNAL-1" not in {finding.control_id for finding in result.findings}
    completed = next(
        event for event in deps.event_log.events() if event.type is EventType.AUDIT_COMPLETED
    )
    assert completed.payload["verified_dropped"] == 1
    assert len(provider.calls) == 3


def test_build_audit_agent_runner_executes_one_agent_over_run_evidence() -> None:
    """The composition's runner builder derives the input, mints an identity and runs the agent."""
    log = InMemoryEventLog(new_id("run"))
    report = AuditReport(run_id=log.run_id, status="passed", controls=())
    runner = build_audit_agent_runner(
        log,
        Actor.system(),
        provider=ScriptedProvider([_scripted_agent_response("METHOD-X")]),
        route_policy=TEST_ROUTE_POLICY,
    )

    output = runner(_internal_audit_spec(), log.run_id, (), report)

    assert output.agent_name == "internal-audit"
    assert [finding.control_id for finding in output.findings] == ["model:METHOD-X"]
    assert output.findings[0].framework is Framework.INTERNAL
    assert len(_mira_agent_starts(log.events())) == 1


def test_build_audit_agent_runner_derives_run_id_from_report_when_events_are_empty() -> None:
    """``AuditInput.from_run`` derives the Run id, so the runner needs no separate run-id source."""
    log = InMemoryEventLog(new_id("run"))
    report = AuditReport(run_id=log.run_id, status="passed", controls=())
    runner = build_audit_agent_runner(
        log, Actor.system(), provider=ScriptedProvider([{}]), route_policy=TEST_ROUTE_POLICY
    )

    output = runner(_internal_audit_spec(), log.run_id, (), report)

    assert output.findings == ()
    audit_input = AuditInput.from_run((), report)
    assert audit_input.run_id == log.run_id


# --------------------------------------------------------------------------- production factory


@pytest.mark.integration
def test_production_factory_drives_a_run_to_a_completed_terminal_state(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, frameworks=(Framework.INTERNAL,))
    catalog = _data_agent_catalog(tmp_path / "catalog")
    # One shared provider feeds THY, two bounded MIRA discovery rounds, and one verifier.
    provider = ScriptedProvider(
        [
            _PLAN_OUTPUT,
            DataProfileOutput(row_count=1, columns=("a",)),
            _scripted_agent_response(),
            _scripted_agent_response(),
            {"supported": True},
        ]
    )
    engine = PolicyEngine(_PLAN_PASSES)
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]

        def gate_factory(run_id: Id) -> Gate:
            return Gate(engine, deps.event_store.open(run_id))

        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            specs=(_internal_audit_spec(),),
            project_config=deps.project_resolution.config,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)

    assert final.status == "COMPLETED"
    assert verify_events(events).valid
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert any(event.type is EventType.AUDIT_STARTED for event in events)
    # The audit-agent fan-out ran inside the composed Run.
    assert len(_mira_agent_starts(events)) == 2
    assert any(
        event.type is EventType.AUDIT_FINDING
        and (event.payload.get("finding") or {}).get("control_id") == "model:AGENT-INTERNAL-1"
        for event in events
    )
    # Exactly one run-level findings decision and one audit-completed fact for the whole Run.
    findings_decisions = [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert len(findings_decisions) == 1
    assert sum(1 for event in events if event.type is EventType.AUDIT_COMPLETED) == 1
    completed = next(event for event in events if event.type is EventType.AUDIT_COMPLETED)
    assert completed.payload["verified_dropped"] == 0
    # THY, two bounded MIRA discovery rounds, and one verifier call share this provider.
    assert len(provider.calls) == 5


@pytest.mark.integration
def test_denied_mira_regulation_search_fails_the_run_without_a_passing_audit(
    tmp_path: Path,
) -> None:
    """A required MIRA evidence refusal cannot close the audit or Run as successful."""
    workspace = _workspace(tmp_path, frameworks=(Framework.INTERNAL,))
    catalog = _data_agent_catalog(tmp_path / "catalog")
    provider = ScriptedProvider(
        [
            _PLAN_OUTPUT,
            DataProfileOutput(row_count=1, columns=("a",)),
            LLMToolCall(
                id="call-search",
                name="search_regulation",
                arguments={"query": "human oversight", "k": 1},
            ),
            LLMToolCall(id="call-output", name="final_result", arguments={"findings": []}),
        ]
    )
    policy = _PLAN_PASSES.model_copy(
        update={
            "capability_rules": (
                CapabilityRule(
                    id="REVIEW-AUDIT-EVIDENCE",
                    decision=Decision.REQUIRE_HUMAN_REVIEW,
                    reason="test policy: regulation evidence requires review",
                    external_effects=(),
                ),
            )
        }
    )
    engine = PolicyEngine(policy)
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]

        def gate_factory(run_id: Id) -> Gate:
            return Gate(engine, deps.event_store.open(run_id))

        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            specs=(
                _internal_audit_spec(tool_allowlist=("search_regulation",)).model_copy(
                    update={
                        "max_turns": 2,
                        "required_tool_calls": ("search_regulation",),
                    }
                ),
            ),
            project_config=deps.project_resolution.config,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )

    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)
    assert final.status == "FAILED"
    assert verify_events(events).valid
    assert any(event.type is EventType.TOOL_DENIED for event in events)
    assert any(
        event.type is EventType.AGENT_COMPLETED
        and event.producer == "thymira.mira"
        and event.payload.get("status") == "FAILED"
        for event in events
    )
    assert not any(event.type is EventType.AUDIT_COMPLETED for event in events)
    assert not any(event.type is EventType.RUN_COMPLETED for event in events)
    failure = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert "required audit tool did not complete" in (failure.payload.get("error") or "")


@pytest.mark.integration
def test_production_factory_runs_the_shipped_audit_agents(tmp_path: Path) -> None:
    """The factory passes project governance to MIRA before filtering its shipped roster."""
    declared = (Framework.EU_AI_ACT, Framework.CREDIT_RISK)
    workspace = _workspace(tmp_path, frameworks=declared)
    catalog = _data_agent_catalog(tmp_path / "catalog")
    shipped = load_default_specs()
    applicable = tuple(spec for spec in shipped if spec.framework in declared)
    audit_responses: list[LLMToolCall] = []
    for index, spec in enumerate(applicable):
        if spec.name in {"compaction_fidelity", "reg_evidence"}:
            continue
        if "search_regulation" in spec.required_tool_calls:
            framework = "GDPR" if spec.framework is Framework.CREDIT_RISK else "EU_AI_ACT"
            query = (
                "automated decision human intervention"
                if framework == "GDPR"
                else "human oversight"
            )
            audit_responses.append(
                LLMToolCall(
                    id=f"call-search-{index}",
                    name="search_regulation",
                    arguments={"query": query, "framework": framework, "k": 1},
                )
            )
        audit_responses.append(
            LLMToolCall(
                id=f"call-output-{index}",
                name="final_result",
                arguments={"findings": []},
            )
        )
    provider = ScriptedProvider(
        [
            RiskJudgment(
                risk_level=RiskLevel.LOW,
                activity_category="declared_no_sensitive_data",
                confidence=0.99,
                justification="The complete profile declares no sensitive attributes.",
            ),
            RiskClassification(
                risk_level=RiskLevel.MEDIUM,
                activity_category="data_analysis",
                confidence=0.99,
                needs_human_review=False,
                summary="Bounded local analysis with indirect impact on represented applicants.",
            ),
            _PLAN_OUTPUT,
            DataProfileOutput(row_count=1, columns=("a",)),
            *audit_responses,
        ]
    )
    policy = _PLAN_PASSES.model_copy(
        update={
            "action_rules": (
                *_PLAN_PASSES.action_rules,
                ActionRule(
                    id="TEST-EXECUTION",
                    action_types=("execution.start",),
                    decision=Decision.PASS,
                    reason="test policy: the bounded local test execution may start",
                ),
            ),
            "capability_rules": (
                CapabilityRule(
                    id="TEST-LOCAL-REGULATION",
                    decision=Decision.PASS,
                    reason="test policy: MIRA may read the injected local regulation corpus",
                    data_access=("regulation_store",),
                    external_effects=(),
                ),
            ),
        }
    )
    engine = PolicyEngine(policy)
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]

        def gate_factory(run_id: Id) -> Gate:
            return Gate(engine, deps.event_store.open(run_id))

        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            project_config=deps.project_resolution.config,
            activity_profile_factory=_declared_activity_profile,
            risk_interview=deps.risk_interview,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)

    failure = next((event for event in events if event.type is EventType.RUN_FAILED), None)
    assert final.status == "COMPLETED", failure.payload if failure is not None else events[-1]
    assert verify_events(events).valid
    # Only the project-declared EU AI Act and credit-risk agents may use the model.
    assert len(_mira_agent_starts(events)) == len(applicable)
    expected_audit_calls = sum(
        2 if spec.required_tool_calls else 1
        for spec in applicable
        if spec.name not in {"compaction_fidelity", "reg_evidence"}
    )
    assert len(provider.calls) == 4 + expected_audit_calls
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in events)
    findings_decisions = [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]
    assert len(findings_decisions) == 1
    assert sum(1 for event in events if event.type is EventType.AUDIT_COMPLETED) == 1
    started = next(event for event in events if event.type is EventType.RUN_STARTED)
    completed = next(event for event in events if event.type is EventType.AUDIT_COMPLETED)
    report = AuditReport.model_validate(completed.payload["audit_report"])
    assert started.payload["graph_definition_hash"] == composed_graph_definition_hash()
    assert report.graph_definition_hash == canonical_graph_definition_hash()


@pytest.mark.integration
def test_production_factory_records_execute_before_missing_deliverable_failure(
    tmp_path: Path,
) -> None:
    """A post-Execute THY failure keeps the persisted lifecycle at the Execute stage."""
    workspace = _workspace(tmp_path, frameworks=(Framework.INTERNAL,))
    catalog = _data_agent_catalog(tmp_path / "catalog")
    plan = PlanOutput(
        tasks=(
            AgentTask(
                id="missing-report",
                agent=ThyAgentKind.DATA,
                phase=ThyPhase.EXECUTE,
                instruction="Create the required report.",
                required_artifacts=(ArtifactRequirement(name="reports/result.md"),),
            ),
        )
    )
    provider = ScriptedProvider([plan, DataProfileOutput(row_count=1, columns=("a",))])
    engine = PolicyEngine(_PLAN_PASSES)
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]

        def gate_factory(run_id: Id) -> Gate:
            return Gate(engine, deps.event_store.open(run_id))

        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            specs=(_internal_audit_spec(),),
            project_config=deps.project_resolution.config,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )

    run = deps.run_service.create_run(
        session.id, "create the report", actor=Actor.system(), workspace=workspace
    )

    events = deps.run_store.events(run.id)
    assert deps.run_service.get_run(run.id).status == "FAILED"
    assert RunController(deps.run_store).current_state(run.id).stage is RunStage.EXECUTING
    assert [
        event.payload.get("command") for event in events if event.type is EventType.RUN_TRANSITIONED
    ] == ["start", "begin_execution", "fail"]
    failed = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert "required artifacts missing: reports/result.md" in failed.payload["error"]
    assert verify_events(events).valid


@pytest.mark.integration
def test_production_factory_thy_failure_leaves_a_consistent_failed_run(tmp_path: Path) -> None:
    """The default policy halts THY's plan; the composed Run becomes a verifiable FAILED Run."""
    workspace = _workspace(tmp_path)
    catalog = _data_agent_catalog(tmp_path / "catalog")
    provider = ScriptedProvider([_PLAN_OUTPUT])
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]
        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=deps.gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            project_config=deps.project_resolution.config,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)

    assert final.status == "FAILED"
    assert RunController(deps.run_store).current_state(run.id).stage is RunStage.PLANNING
    assert verify_events(events).valid
    failed = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert "inline execution failed" in (failed.payload.get("error") or "")
    assert not any(
        event.type is EventType.RUN_TRANSITIONED
        and event.payload.get("command") == "begin_execution"
        for event in events
    )
    # Governance preflight runs before THY, but no evidence-dependent audit or Gate review follows.
    assert any(event.type is EventType.AUDIT_STARTED for event in events)
    assert any(event.type is EventType.RISK_ASSESSMENT_RECORDED for event in events)
    assert any(event.type is EventType.PACK_BINDING_RECORDED for event in events)
    assert not any(event.type is EventType.CONTROL_EVALUATION_RECORDED for event in events)
    assert not any(
        event.type is EventType.AGENT_STARTED and event.producer == "thymira.mira"
        for event in events
    )
    assert not any(
        event.type is EventType.POLICY_DECISION and event.payload.get("subject_kind") == "findings"
        for event in events
    )


@pytest.mark.integration
def test_a_failed_dataset_intake_leaves_a_consistent_failed_run_not_a_zombie(
    tmp_path: Path,
) -> None:
    """A missing declared dataset halts THY at Inspect -- and the composed Run still ends FAILED.

    Mirrors `test_production_factory_thy_failure_leaves_a_consistent_failed_run`: there, THY
    halts because the default policy escalates `plan.proposed`; here it halts earlier, at
    Inspect, because `.thymira/config.yaml` declares a dataset that is not on disk. Either way
    `InlineDispatcher` converts the raised `ThyExecutionError` into a recorded `RUN_FAILED` --
    proving a failed intake is a failed Run, never a zombie with no terminal event.

    `ThySubgraph.invoke` is asserted directly first (mirroring
    `test_thy_subgraph_raises_when_thy_reports_an_error`): the specific reason ("is missing") is
    genuinely known at THY's own boundary. `InlineDispatcher.submit` (`thymira.core.dispatch`)
    then prefixes it with `run {id}: inline execution failed: ` on the way to the persisted
    `RUN_FAILED` event -- the prefix is pinned by `tests/thymira/test_e2e_runtime_spine.py` -- so
    what this test asserts on the persisted event is only that the Run reaches a terminal FAILED
    state, never hangs with no terminal event at all.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "datasets:\n  - name: german_credit\n    path: data/applications.csv\n",
        encoding="utf-8",
        newline="\n",
    )
    catalog = _data_agent_catalog(tmp_path / "catalog")

    direct_run = _run()
    direct_deps = _subgraph_deps(direct_run, tmp_path / "direct", policy=_PLAN_PASSES)
    direct_thy = ThySubgraph(
        direct_run,
        catalog,
        provider=ScriptedProvider([_PLAN_OUTPUT]),
        project_dir=workspace,
    )
    with pytest.raises(ThyExecutionError, match=r"is missing"):
        direct_thy.invoke(initial_runtime_state(direct_run), deps=direct_deps)

    provider = ScriptedProvider([_PLAN_OUTPUT])
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]
        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=deps.gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            project_config=deps.project_resolution.config,
            project_dir=workspace,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)

    assert final.status == "FAILED"
    assert verify_events(events).valid
    assert any(event.type is EventType.RUN_FAILED for event in events)


@pytest.mark.integration
def test_a_dataset_registered_before_a_later_one_fails_is_still_announced(
    tmp_path: Path,
) -> None:
    """The chain shows what Inspect got through even when a later declared dataset fails.

    Two datasets are declared; the first is on disk and registers cleanly, the second is not.
    `ThySubgraph.invoke` must call `_record_artifact_changes` (`thymira.core.graph.adapters`)
    before it raises `ThyExecutionError`, not only on the success path, so the first dataset's
    `artifact.created` event is on the chain instead of disappearing with the exception -- and
    the composed Run still ends FAILED, exactly as
    `test_a_failed_dataset_intake_leaves_a_consistent_failed_run_not_a_zombie` proves for a
    single missing dataset.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / "data").mkdir()
    (workspace / "data" / "applications.csv").write_text(
        "a,b\n1,2\n3,4\n", encoding="utf-8", newline="\n"
    )
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "datasets:\n"
        "  - name: applications\n    path: data/applications.csv\n"
        "  - name: missing_one\n    path: data/missing.csv\n",
        encoding="utf-8",
        newline="\n",
    )
    catalog = _data_agent_catalog(tmp_path / "catalog")

    direct_run = _run()
    direct_deps = _subgraph_deps(direct_run, tmp_path / "direct", policy=_PLAN_PASSES)
    direct_thy = ThySubgraph(
        direct_run,
        catalog,
        provider=ScriptedProvider([_PLAN_OUTPUT]),
        project_dir=workspace,
    )
    with pytest.raises(ThyExecutionError, match=r"is missing"):
        direct_thy.invoke(initial_runtime_state(direct_run), deps=direct_deps)

    direct_events = direct_deps.event_log.events()
    assert any(
        event.type is EventType.ARTIFACT_CREATED
        and event.payload.get("name") == "datasets/applications.csv"
        for event in direct_events
    )

    provider = ScriptedProvider([_PLAN_OUTPUT])
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]
        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=deps.gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            project_config=deps.project_resolution.config,
            project_dir=workspace,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    run = deps.run_service.create_run(
        session.id, "analyze the dataset", actor=Actor.system(), workspace=workspace
    )

    final = deps.run_service.get_run(run.id)
    events = deps.run_store.events(run.id)

    assert final.status == "FAILED"
    assert verify_events(events).valid
    assert any(event.type is EventType.RUN_FAILED for event in events)
    assert any(
        event.type is EventType.ARTIFACT_CREATED
        and event.payload.get("name") == "datasets/applications.csv"
        for event in events
    )


def _staged_dataset_path(project_dir: Path, run_id: str) -> Path:
    """Where `_stage_declared_datasets` (`thymira.core.graph.adapters`) puts `applications.csv`."""
    workspace = project_dir / ".thymira" / "runtime" / "workspaces" / run_id
    return workspace / "data" / "applications.csv"


def test_a_registered_datasets_raw_file_is_staged_into_the_runs_own_workspace(
    tmp_path: Path,
) -> None:
    """A `coding` step's only tools (`read_file`/`run_python`) can now actually reach the data.

    Bug-hunt follow-up, reproduced live: H2 gave every Run its own workspace, isolated from
    `project_dir`; `profile_dataset`/`query_sql` read a registered dataset from the `ArtifactStore`
    by name and were unaffected, but nothing ever copied the raw file to where `read_file`/
    `run_python` -- which only resolve paths under the workspace -- could reach it. A real EDA run
    reproduced the resulting `FileNotFoundError` for every path the model guessed.
    """
    project_dir = tmp_path / "workspace"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / "data").mkdir()
    original_content = "a,b\n1,2\n3,4\n"
    (project_dir / "data" / "applications.csv").write_text(
        original_content, encoding="utf-8", newline="\n"
    )
    (project_dir / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "datasets:\n  - name: applications\n    path: data/applications.csv\n",
        encoding="utf-8",
        newline="\n",
    )
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    catalog = _data_agent_catalog(tmp_path / "catalog")
    thy = ThySubgraph(
        run,
        catalog,
        provider=ScriptedProvider([_PLAN_OUTPUT, DataProfileOutput(row_count=1, columns=("a",))]),
        project_dir=project_dir,
    )

    thy.invoke(initial_runtime_state(run), deps=deps)

    staged = _staged_dataset_path(project_dir, run.id)
    assert staged.read_text(encoding="utf-8") == original_content


def test_coding_runtime_context_names_each_datasets_declared_workspace_path(
    tmp_path: Path,
) -> None:
    """A logical dataset name cannot be mistaken for its artifact-store filename."""
    project_dir = tmp_path / "workspace"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / "data").mkdir()
    (project_dir / "data" / "applications.csv").write_text(
        "credit_amount\n100\n", encoding="utf-8", newline="\n"
    )
    (project_dir / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "datasets:\n  - name: german_credit\n    path: data/applications.csv\n",
        encoding="utf-8",
        newline="\n",
    )
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    provider = ScriptedProvider([_PLAN_OUTPUT, DataProfileOutput(row_count=1, columns=("a",))])
    thy = ThySubgraph(
        run, _data_agent_catalog(tmp_path / "catalog"), provider=provider, project_dir=project_dir
    )

    thy.invoke(initial_runtime_state(run), deps=deps)

    prompts = "\n".join(call["prompt"] for call in provider.calls)
    assert "german_credit: data/applications.csv" in prompts
    assert "datasets/german_credit.csv (dataset)" not in prompts


def test_staging_never_overwrites_a_codings_own_later_edit_to_its_workspace_copy(
    tmp_path: Path,
) -> None:
    """A resumed pass must not clobber a `coding` step's legitimate in-workspace edit.

    `ThySubgraph.invoke` runs once per LangGraph pass (every resume is another call), so staging
    runs again on every pass over the same Run. If a `coding` step had modified its own copy of
    the dataset between passes, re-staging must leave that edit alone.
    """
    project_dir = tmp_path / "workspace"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / "data").mkdir()
    (project_dir / "data" / "applications.csv").write_text(
        "a,b\n1,2\n", encoding="utf-8", newline="\n"
    )
    (project_dir / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "datasets:\n  - name: applications\n    path: data/applications.csv\n",
        encoding="utf-8",
        newline="\n",
    )
    run = _run()
    deps = _subgraph_deps(run, tmp_path, policy=_PLAN_PASSES)
    catalog = _data_agent_catalog(tmp_path / "catalog")

    def _new_pass() -> ThySubgraph:
        # A real resume rebuilds the whole graph -- a fresh `ThySubgraph` -- through the graph
        # factory each pass, so a fresh scripted provider mirrors that rather than exhausting one.
        return ThySubgraph(
            run,
            catalog,
            provider=ScriptedProvider(
                [_PLAN_OUTPUT, DataProfileOutput(row_count=1, columns=("a",))]
            ),
            project_dir=project_dir,
        )

    _new_pass().invoke(initial_runtime_state(run), deps=deps)
    staged = _staged_dataset_path(project_dir, run.id)
    staged.write_text("a,b\n1,2\nedited,by,coding\n", encoding="utf-8", newline="\n")

    _new_pass().invoke(initial_runtime_state(run), deps=deps)

    assert staged.read_text(encoding="utf-8") == "a,b\n1,2\nedited,by,coding\n"


def test_thy_adapter_records_its_real_tool_budget_refusal(tmp_path: Path) -> None:
    run = _run()
    policy = _PLAN_PASSES.model_copy(
        update={
            "capability_rules": (
                CapabilityRule(
                    id="ALLOW-ECHO",
                    decision=Decision.PASS,
                    reason="allow the local echo tool",
                    external_effects=(),
                ),
            ),
            "budget_rules": (
                BudgetRule(
                    id="HARD-CALLS",
                    decision=Decision.BLOCK,
                    reason="tool-call budget exhausted",
                    max_tool_calls=1,
                ),
            ),
        }
    )
    deps = _subgraph_deps(run, tmp_path, policy=policy)
    deps.usage_ledger.charge_tool()
    base = _data_agent_catalog(tmp_path / "catalog")
    catalog = AgentCatalog(
        (base.get("data").model_copy(update={"tool_allowlist": ("echo",)}),),
        system_prompts={"data": base.system_prompt("data")},
        output_schemas={"data": DataProfileOutput},
    )
    tool = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    provider = ScriptedProvider(
        [
            _PLAN_OUTPUT,
            LLMToolCall(id="echo-call", name="echo", arguments={"value": "x"}),
            LLMToolCall(
                id="final", name="final_result", arguments={"row_count": 0, "columns": ["value"]}
            ),
        ]
    )
    thy = ThySubgraph(
        run,
        catalog,
        provider=provider,
        project_dir=_workspace(tmp_path),
        tool_registry=ToolRegistry((tool,)),
    )
    state = initial_runtime_state(run).model_copy(
        update={
            "risk_profile": RiskProfile(
                risk_level="limited", activity_category="analysis", confidence=1
            ),
        }
    )

    thy.invoke(state, deps=deps)

    events = deps.event_log.events()
    denial = next(event for event in events if event.type is EventType.TOOL_DENIED)
    budget = next(
        event
        for event in events
        if event.type is EventType.POLICY_DECISION and event.payload.get("rule_id") == "HARD-CALLS"
    )
    assert budget.payload["decision"] == Decision.BLOCK.value
    assert denial.payload["decision_id"] == budget.payload["id"]
    assert budget.seq < denial.seq
    assert not any(event.type is EventType.TOOL_STARTED for event in events)
    assert deps.usage_ledger.snapshot()["tool_calls"] == 1
    report = audit_run(
        AuditContext(run.id, events, deps.artifact_store, deps.gate.engine.policy_sha256)
    )
    assert next(c for c in report.controls if c.control_id == "A6").status is ControlStatus.PASSED
    assert verify_events(events).valid


# ------------------------------------------------ the usage ledger across a park and a resume

_BUDGET_OVERLAY = """name: plan-passes-with-a-tool-budget
version: "1.0"
action_rules:
  - id: TEST-PLAN
    description: THY's plan proposal may proceed in this proof.
    action_types: [plan.proposed]
    decision: PASS
    reason: "test policy: THY's plan needs no review"
budget_rules:
  - id: HARD-CALLS
    description: This proof allows one tool call for the whole Run.
    decision: BLOCK
    reason: "tool-call budget exhausted"
    max_tool_calls: 1
"""

_TWO_TOOL_TASKS = PlanOutput(
    tasks=(
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
        AgentTask(
            id="t2", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it again"
        ),
    )
)
_ECHO_CALL = LLMToolCall(id="echo-call", name="echo", arguments={"value": "x"})
_FINAL_CALL = LLMToolCall(
    id="final", name="final_result", arguments={"row_count": 0, "columns": ["value"]}
)


def _requests(snapshot: Mapping[str, int | float | None]) -> int:
    """The measured request count of a usage snapshot, narrowed to the int the ledger keeps."""
    value = snapshot["requests"]
    assert isinstance(value, int)
    return value


def _budget_workspace(tmp_path: Path) -> Path:
    """A project whose own overlay passes THY's plan and caps the Run at one tool call."""
    workspace = tmp_path / "workspace"
    thymira = workspace / ".thymira"
    thymira.mkdir(parents=True)
    (thymira / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n", encoding="utf-8", newline="\n"
    )
    (thymira / "policies.yaml").write_text(_BUDGET_OVERLAY, encoding="utf-8", newline="\n")
    return workspace


def _echo_agent_catalog(directory: Path) -> AgentCatalog:
    """A single ``data`` agent whose one declared tool is ``echo``."""
    directory.mkdir(parents=True)
    (directory / "data.yaml").write_text(
        "name: data\n"
        "role: agent\n"
        "task_kinds: [analyze]\n"
        "tool_allowlist: [echo]\n"
        "max_turns: 2\n"
        "max_depth: 1\n"
        "system_prompt_ref: prompts/data.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
        newline="\n",
    )
    prompts = directory / "prompts"
    prompts.mkdir()
    (prompts / "data.md").write_text("You are the data agent.", encoding="utf-8", newline="\n")
    return load_agent_specs(directory, known_capabilities=frozenset({"echo"}))


@pytest.mark.integration
def test_the_usage_ledger_survives_the_parks_a_tool_review_causes(tmp_path: Path) -> None:
    """A resumed pass keeps charging the Run's own ledger, so a reached ceiling still holds.

    The dispatcher rebuilds a Run's graph on every resume, and every park is a resume. With the
    ledger built inside the graph factory, each park handed the Policy Engine a Run that had spent
    nothing: the model requests of every earlier pass vanished from the budget snapshot, and a
    tool-call ceiling reached before a park could be walked straight past after it.

    Two tool calls, one ceiling: the first is approved and executes, the second is approved by the
    same human and must be refused by the budget guard, because the Run has already spent the one
    call it was allowed.
    """
    workspace = _budget_workspace(tmp_path)
    catalog = _echo_agent_catalog(tmp_path / "catalog")
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    registry = ToolRegistry((echo,))
    provider = ScriptedProvider(
        [
            _TWO_TOOL_TASKS,
            _ECHO_CALL,  # first pass: parked, unanswered
            # The resumed pass replays this exact call through `deferred_tool_results` (bug-hunt
            # follow-up: `thymira.agents.resume`) instead of asking the model to redo it, so only
            # one more scripted item -- the model's reaction to the real tool result -- follows.
            _FINAL_CALL,  # second pass: the approved call executes and spends the one allowed call
            _ECHO_CALL,  # the second task: parked, unanswered
            _FINAL_CALL,  # third pass: replayed, refused by the budget guard, model reacts
        ]
    )
    ledgers = UsageLedgerRegistry()
    assembled: list = []
    built: list[GraphFactory] = []

    def graph_factory(run: Run) -> CompiledStateGraph:
        """Build the Run's graph the way the composition root does: one factory, many builds."""
        if not built:
            deps = assembled[0]
            assert deps.project_resolution is not None
            built.append(
                build_runtime_graph_factory(
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
                    catalog=catalog,
                    specs=(),
                    usage_ledgers=ledgers,
                    plan_repository=deps.board_repository,
                    dag_repository=deps.dag_repository,
                )
            )
        return built[0](run)

    assembled.append(
        build_default_deps(
            tmp_path / "runtime",
            workspace=workspace,
            graph_factory=graph_factory,
            principal_resolver=TEST_CREDENTIAL,
        )
    )
    deps = assembled[0]
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)

    run = deps.run_service.create_run(
        session.id, "profile the dataset", actor=Actor.system(), workspace=workspace
    )
    ledger = ledgers.for_run(run.id)
    snapshots = [dict(ledger.snapshot())]
    for _ in range(4):
        if deps.run_service.get_run(run.id).status.value != "WAITING_FOR_APPROVAL":
            break
        deps.run_service.resolve_approval(
            run.id,
            gate=deps.gate_factory(run.id, approver=lambda _request: True, human=reviewer),
            actor=reviewer,
            note="reviewed",
        )
        snapshots.append(dict(ledger.snapshot()))

    events = deps.run_store.events(run.id)
    resumes = [
        event.payload.get("resume_from")
        for event in events
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
    ]
    assert resumes.count("execution") == 2
    # The Run parked twice, and each pass added to the totals of the passes before it.
    assert snapshots[0]["tool_calls"] == 0
    assert _requests(snapshots[0]) > 0
    assert snapshots[1]["tool_calls"] == 1
    assert _requests(snapshots[1]) > _requests(snapshots[0])
    assert _requests(ledger.snapshot()) == len(provider.calls)
    # The ceiling reached before the second park still refuses the call the human approved.
    assert ledger.snapshot()["tool_calls"] == 1
    assert len([event for event in events if event.type is EventType.TOOL_STARTED]) == 1
    assert len(echo.seen_invocation) == 1
    budget = next(
        event
        for event in events
        if event.type is EventType.POLICY_DECISION and event.payload.get("rule_id") == "HARD-CALLS"
    )
    denied = [event for event in events if event.type is EventType.TOOL_DENIED]
    assert denied[-1].payload["decision_id"] == budget.payload["id"]
    assert "tool-call budget exhausted" in denied[-1].payload["reason"]
    # The recorded evidence says the same: the snapshot the findings Gate saw carries every pass.
    priced = [
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED and "cost_so_far" in event.payload
    ]
    assert priced[-1].payload["cost_so_far"]["requests"] == _requests(ledger.snapshot())
    assert verify_events(events).valid
