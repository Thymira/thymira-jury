"""In-process end-to-end coverage for Core rework and final assurance evidence."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import pytest

from thymira.core import (
    MiraSubgraph,
    RunController,
    RuntimeState,
    StateCheckpointer,
    SubgraphDeps,
    UsageLedger,
    build_runtime_graph,
    default_activity_profile,
    initial_runtime_state,
    write_run_assurance,
)
from thymira.core.audit_freshness import AuditFreshnessService
from thymira.core.control_plane import RunEventLog
from thymira.events import InMemoryEventLog, canonical_json, verify_events
from thymira.mira import (
    ASSURANCE_BUNDLE_EXPORT,
    AssuranceBundle,
    build_assurance,
    canonical_graph_definition_hash,
    load_default_packs,
    verify_assurance_bundle,
)
from thymira.mira.checks import AuditReport, ControlStatus
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.policies import (
    Approver,
    FindingRule,
    Gate,
    Policy,
    PolicyEngine,
    auto_approve,
    auto_reject,
    load_default_policy,
)
from thymira.schemas import (
    ActivityProfile,
    Actor,
    ActorKind,
    ArtifactKind,
    AuditFinding,
    Decision,
    EventType,
    Framework,
    ModelRoutePolicy,
    PackBinding,
    PolicyDecision,
    RiskAssessment,
    RiskLevel,
    Run,
    RunCondition,
    RunOutcome,
    RunStage,
    Severity,
    new_id,
)
from thymira.state import (
    LocalArtifactStore,
    LocalCheckpointRepository,
    LocalRecordRepository,
    LocalRunStore,
)
from thymira.tools import ToolManager, ToolRegistry

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind MIRA's direct risk-model seam to a code-owned route snapshot and model choice."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph


class _ReworkThy:
    """Small THY boundary that writes one revisioned report per execution."""

    name = "e2e-thy"

    def __init__(self, *, fixed_artifact: bool = False) -> None:
        self.calls = 0
        self._fixed_artifact = fixed_artifact

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "e2e-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Produce a new or unchanged evidence revision, depending on the test case."""
        self.calls += 1
        previous = deps.artifact_store.get("analysis.md")
        content = "unchanged evidence" if self._fixed_artifact else f"revision {self.calls}"
        artifact = deps.artifact_store.save_text(
            "analysis.md",
            content,
            produced_by=state.run_id,
            kind=ArtifactKind.REPORT,
            media_type="text/markdown",
        )
        deps.event_log.append(
            EventType.ARTIFACT_CREATED,
            Actor.system(),
            {
                "name": artifact.name,
                "sha256": artifact.sha256,
                "artifact_id": artifact.id,
            },
            subject_id=artifact.id,
            producer="test.thy",
        )
        if previous is not None and previous.valid:
            deps.event_log.append(
                EventType.ARTIFACT_INVALIDATED,
                Actor.system(),
                {
                    "name": previous.name,
                    "artifact_id": previous.id,
                    "reason": "superseded by rework",
                },
                subject_id=previous.id,
                producer="test.thy",
            )
        return state


class _SequenceMira:
    """MIRA boundary that emits a deterministic audit for each graph pass."""

    name = "e2e-mira"

    def __init__(self, rounds: tuple[tuple[AuditFinding, ...], ...]) -> None:
        self._rounds = rounds
        self.calls = 0

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "e2e-mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Record findings and the exact audit snapshot consumed by Core's Gate."""
        round_number = min(self.calls, len(self._rounds) - 1)
        findings = self._rounds[round_number]
        first_audit = self.calls == 0
        self.calls += 1
        profile = ActivityProfile(
            id=new_id("profile"),
            activity_id=new_id("activity"),
            version=1,
            run_id=state.run_id,
            purpose="Produce a governed model analysis.",
            affected_population="Credit applicants.",
            decision_effect="Supports human review only.",
            autonomy="Recommendation only.",
            human_oversight="A reviewer can override the recommendation.",
            jurisdiction="ES",
            data_categories=("credit_history",),
            potential_consequences=("A review could be delayed.",),
        )
        deps.event_log.append(
            EventType.AUDIT_STARTED,
            Actor.system(),
            {
                "audit_round": self.calls,
                **(
                    {
                        "activity_profile_id": profile.id,
                        "activity_profile_version": profile.version,
                        "activity_profile": profile.to_json_dict(),
                    }
                    if first_audit
                    else {}
                ),
            },
            subject_id=state.run_id,
            producer="test.mira",
        )
        if first_audit:
            assessment = RiskAssessment(
                id=new_id("assessment"),
                run_id=state.run_id,
                activity_profile_id=profile.id,
                activity_profile_version=profile.version,
                assessor=Actor.system(),
                subject_kind="activity",
                subject_id=profile.activity_id,
                risk_level=RiskLevel.LOW,
                activity_category="decision_support",
                confidence=1.0,
                justification="The activity is recommendation-only and low risk.",
                assessment_method="e2e",
                assessment_method_version="1.0",
            )
            binding = PackBinding(
                id=new_id("binding"),
                activity_profile_id=profile.id,
                activity_profile_version=profile.version,
                pack_id=new_id("pack"),
                pack_version="1.0",
                applicability_rules_version="1.0",
                jurisdiction=profile.jurisdiction,
                applicable=True,
                rationale="The test pack applies to this profile.",
            )
            deps.event_log.append(
                EventType.RISK_ASSESSMENT_RECORDED,
                Actor.system(),
                {"risk_assessment": assessment.to_json_dict()},
                subject_id=assessment.id,
                producer="test.mira",
            )
            deps.event_log.append(
                EventType.PACK_BINDING_RECORDED,
                Actor.system(),
                {"pack_binding": binding.to_json_dict()},
                subject_id=binding.id,
                producer="test.mira",
            )
        for finding in findings:
            deps.event_log.append(
                EventType.AUDIT_FINDING,
                Actor.system(),
                {"finding": finding.to_json_dict()},
                subject_id=finding.id,
                producer="test.mira",
            )
        events = deps.event_log.events()
        report = AuditReport(
            run_id=state.run_id,
            status="passed_with_warnings" if findings else "passed",
            controls=(),
            findings=findings,
            terminal_hash=events[-1].hash,
            manifest_sha256=artifact_manifest_sha256(deps.artifact_store),
            graph_definition_hash=canonical_graph_definition_hash(),
            policy_sha256=deps.gate.engine.policy_sha256,
        )
        deps.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "status": report.status,
                "audit_revision": self.calls,
                "audit_report": report.to_json_dict(),
            },
            subject_id=state.run_id,
            producer="test.mira",
        )
        return state.model_copy(update={"findings": findings})


@dataclass(frozen=True, slots=True)
class _Execution:
    """All local handles needed to inspect one completed or parked test Run."""

    run: Run
    store: LocalRunStore
    artifact_store: LocalArtifactStore
    graph: CompiledStateGraph
    thy: _ReworkThy
    mira: _SequenceMira


def _finding(run_id: str | None = None) -> AuditFinding:
    """Create a finding mapped by Core to the modeling rework phase."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id or new_id("run"),
        control_id="REWORK-MODELING",
        framework=Framework.METHODOLOGY,
        title="Model evidence needs correction",
        finding="The model evidence must be regenerated before closeout.",
        severity=Severity.MEDIUM,
        confidence=0.95,
    )


def _policy(decision: Decision) -> Policy:
    """Build a policy with one explicit, bounded rework control."""
    return Policy(
        name="e2e-rework",
        version="1.0",
        finding_rules=(
            FindingRule(
                id="REWORK-CONTROL",
                decision=decision,
                reason="The model evidence can be corrected by re-running modeling.",
                control_ids=("REWORK-MODELING",),
            ),
        ),
        findings_default_decision=Decision.PASS,
    )


def _execute(
    tmp_path: Path,
    rounds: tuple[tuple[AuditFinding, ...], ...],
    *,
    decision: Decision = Decision.WARNING,
    approver: Approver | None = None,
    fixed_artifact: bool = False,
    max_reopens: int = 2,
) -> tuple[_Execution, RuntimeState]:
    """Run the composed graph against local JSONL, artifacts and checkpoints."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Produce a governed model analysis.",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system(), payload={"configuration": {"test": "rework"}})
    event_log = RunEventLog(store, run.id)
    # These tests model a named operator answering the rework review. Wrap the convenience
    # approvers so Gate does not classify the answer as automation merely because the callable is
    # the test helper `auto_approve`/`auto_reject`.
    human = Actor(kind=ActorKind.HUMAN, id="rework-reviewer", authenticated=True)
    gate = Gate(
        PolicyEngine(_policy(decision)),
        event_log,
        approver=None if approver is None else partial(approver),
        human=human if approver is not None else None,
    )
    artifact_store = LocalArtifactStore(tmp_path / "artifacts" / run.id, run.id)
    bound_rounds = tuple(
        tuple(finding.model_copy(update={"run_id": run.id}) for finding in round_findings)
        for round_findings in rounds
    )
    deps = SubgraphDeps(
        event_log=event_log,
        gate=gate,
        artifact_store=artifact_store,
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    thy = _ReworkThy(fixed_artifact=fixed_artifact)
    mira = _SequenceMira(bound_rounds)
    graph = build_runtime_graph(
        thy,
        mira,
        checkpointer=StateCheckpointer(LocalCheckpointRepository(tmp_path / "checkpoints")),
        deps=deps,
        controller=RunController(store),
        assurance=partial(write_run_assurance, run, store),
    )
    initial = initial_runtime_state(run).model_copy(update={"max_reopens": max_reopens})
    raw = graph.invoke(initial, {"configurable": {"thread_id": run.id}})
    execution = _Execution(run, store, artifact_store, graph, thy, mira)
    return execution, RuntimeState.model_validate(raw)


def _latest_report(execution: _Execution) -> AuditReport:
    """Read the latest persisted report from the authoritative event chain."""
    event = next(
        event
        for event in reversed(execution.store.events(execution.run.id))
        if event.type is EventType.AUDIT_COMPLETED
    )
    return AuditReport.model_validate(event.payload["audit_report"])


def _latest_decision(execution: _Execution) -> PolicyDecision:
    """Read the latest findings decision from the authoritative event chain."""
    event = next(
        event
        for event in reversed(execution.store.events(execution.run.id))
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    )
    return PolicyDecision.model_validate(event.payload)


def _bundle(execution: _Execution) -> AssuranceBundle:
    """Build the complete bundle from the latest exact report and every persisted input."""
    events = execution.store.events(execution.run.id)
    artifacts = tuple(
        artifact for _, artifact in sorted(execution.artifact_store.manifest().items())
    )
    return build_assurance(
        execution.run.id,
        _latest_report(execution),
        _latest_decision(execution),
        run=execution.store.get(execution.run.id),
        events=events,
        artifacts=artifacts,
        decisions=tuple(
            PolicyDecision.model_validate(event.payload)
            for event in events
            if event.type is EventType.POLICY_DECISION
        ),
        approvals=tuple(event for event in events if event.type is EventType.HUMAN_APPROVAL),
        manifest_sha256=artifact_manifest_sha256(execution.artifact_store),
        review_events=events,
    )


def test_run_without_blocking_findings_reaches_pass(tmp_path: Path) -> None:
    """A clean audit takes the PASS path and closes through RunController."""
    execution, state = _execute(tmp_path, ((),))

    assert state.decision is not None
    assert state.decision.decision is Decision.PASS
    assert execution.mira.calls == 1
    projection = RunController(execution.store).current_state(execution.run.id)
    assert projection.stage is RunStage.REPORTING
    assert projection.condition is RunCondition.TERMINAL
    assert projection.outcome is RunOutcome.COMPLETED


def test_core_rework_invokes_the_same_canonical_mira_flow_again(tmp_path: Path) -> None:
    """The production MIRA adapter reuses its canonical flow for the second audit."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Audit the model twice.",
        model_route_policy=TEST_ROUTE_POLICY,
    )
    event_log = InMemoryEventLog(run.id)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(PolicyEngine(load_default_policy()), event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    mira = MiraSubgraph(run, default_activity_profile(run), load_default_packs(), Actor.system())
    state = mira.preflight(initial_runtime_state(run), deps=deps)
    mira.invoke(state, deps=deps)
    mira.invoke(state, deps=deps)

    completed = [event for event in event_log.events() if event.type is EventType.AUDIT_COMPLETED]
    assert len(completed) == 2
    assert [event.payload["audit_revision"] for event in completed] == [1, 2]
    assert (
        sum(event.type is EventType.RISK_ASSESSMENT_RECORDED for event in event_log.events()) == 1
    )


def test_mira_preflight_repeats_only_for_changed_profile_content(tmp_path: Path) -> None:
    """A changed activity-profile snapshot reruns preflight, while an equal one is reused."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Detect profile changes.",
        model_route_policy=TEST_ROUTE_POLICY,
    )
    event_log = InMemoryEventLog(run.id)
    deps = SubgraphDeps(
        event_log=event_log,
        gate=Gate(PolicyEngine(load_default_policy()), event_log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path / "records"),
        usage_ledger=UsageLedger(),
    )
    profile = default_activity_profile(run)
    current = [profile]
    mira = MiraSubgraph(
        run,
        profile,
        load_default_packs(),
        Actor.system(),
        profile_resolver=lambda: current[0],
    )
    state = initial_runtime_state(run)

    mira.preflight(state, deps=deps)
    mira.preflight(state, deps=deps)
    current[0] = profile.model_copy(update={"purpose": "A changed governed purpose."})
    mira.preflight(state, deps=deps)

    assert (
        sum(event.type is EventType.RISK_ASSESSMENT_RECORDED for event in event_log.events()) == 2
    )


def test_correctable_finding_reopens_runs_thy_and_audits_again(tmp_path: Path) -> None:
    """A warning finding preserves lineage and reaches a fresh second PASS audit."""
    finding = _finding(new_id("run"))
    # Rebind the finding after creating the Run inside the helper.
    execution, state = _execute(tmp_path, ((finding,), ()))
    events = execution.store.events(execution.run.id)

    assert state.decision is not None
    assert state.decision.decision is Decision.PASS
    assert execution.thy.calls == 2
    assert execution.mira.calls == 2
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in events) == 2
    started = next(event for event in events if event.type is EventType.REWORK_STARTED)
    assert started.payload["policy_decision_id"] in {
        event.payload["id"] for event in events if event.type is EventType.POLICY_DECISION
    }
    assert any(
        event.type is EventType.RUN_TRANSITIONED and event.payload["command"] == "reopen"
        for event in events
    )
    manifest = execution.artifact_store.manifest()
    assert len(manifest) == 2
    assert any(not artifact.valid for artifact in manifest.values())
    assert execution.artifact_store.verify() == []
    assert verify_events(events).valid


def test_human_approval_authorizes_rework_but_is_not_the_policy_decision(
    tmp_path: Path,
) -> None:
    """A REQUIRE decision reopens only after an explicit recorded approval."""
    finding = _finding()
    execution, _ = _execute(
        tmp_path,
        ((finding,), ()),
        decision=Decision.REQUIRE_HUMAN_REVIEW,
        approver=auto_approve,
    )
    events = execution.store.events(execution.run.id)

    assert any(
        event.type is EventType.HUMAN_APPROVAL and event.payload.get("approved") is True
        for event in events
    )
    assert any(event.type is EventType.REWORK_STARTED for event in events)
    assert (
        RunController(execution.store).current_state(execution.run.id).outcome
        is RunOutcome.COMPLETED
    )


def test_human_rejection_blocks_without_reopening(tmp_path: Path) -> None:
    """A rejected REQUIRE decision never becomes a rework signal."""
    finding = _finding()
    execution, _ = _execute(
        tmp_path,
        ((finding,), ()),
        decision=Decision.REQUIRE_HUMAN_REVIEW,
        approver=auto_reject,
    )
    events = execution.store.events(execution.run.id)
    projection = RunController(execution.store).current_state(execution.run.id)

    assert not any(event.type is EventType.REWORK_STARTED for event in events)
    assert projection.condition is RunCondition.TERMINAL
    assert projection.outcome is RunOutcome.BLOCKED


def test_rework_without_progress_escalates_to_human_review(tmp_path: Path) -> None:
    """Repeated findings and identical evidence stop the correction loop."""
    finding = _finding()
    same_finding_with_new_identity = finding.model_copy(update={"id": new_id("finding")})
    execution, _ = _execute(
        tmp_path,
        ((finding,), (same_finding_with_new_identity,)),
        fixed_artifact=True,
    )
    events = execution.store.events(execution.run.id)
    projection = RunController(execution.store).current_state(execution.run.id)

    assert sum(event.type is EventType.REWORK_STARTED for event in events) == 1
    assert any(event.type is EventType.REWORK_ESCALATED for event in events)
    assert projection.condition is RunCondition.WAITING
    assert projection.wait_reason is not None
    assert projection.wait_reason.value == "approval"


def test_rework_budget_exhaustion_escalates_without_a_second_reopen(tmp_path: Path) -> None:
    """The Core budget refuses the second reopen and records a human escalation."""
    finding = _finding()
    same_finding_with_new_identity = finding.model_copy(update={"id": new_id("finding")})
    execution, state = _execute(
        tmp_path,
        ((finding,), (same_finding_with_new_identity,)),
        max_reopens=1,
    )
    events = execution.store.events(execution.run.id)

    assert state.reopen_count == 1
    assert (
        sum(
            event.type is EventType.RUN_TRANSITIONED and event.payload["command"] == "reopen"
            for event in events
        )
        == 1
    )
    assert any(event.type is EventType.REWORK_ESCALATED for event in events)


def test_modified_artifact_makes_the_previous_audit_stale(tmp_path: Path) -> None:
    """The freshness control detects bytes changed after the persisted audit snapshot."""
    execution, _ = _execute(tmp_path, ((),))
    # The store publishes immutable content objects, so the bytes an audit verifies live at the
    # recorded artifact uri, not under the logical name.
    recorded = execution.artifact_store.get("analysis.md")
    assert recorded is not None
    artifact_path = tmp_path / "artifacts" / execution.run.id / recorded.uri
    artifact_path.write_text("tampered", encoding="utf-8", newline="\n")
    service = AuditFreshnessService(
        execution.store,
        lambda _run_id: execution.artifact_store,
        policy_sha256=_latest_report(execution).policy_sha256 or "",
        graph_definition_hash=canonical_graph_definition_hash(),
    )

    assessment = service.assess(execution.run.id)
    assert assessment is not None
    assert assessment.control.status is ControlStatus.FAILED


def test_assurance_bundle_is_complete_and_tamper_evident(tmp_path: Path) -> None:
    """The final bundle verifies against the chain and rejects altered report or event content."""
    execution, _ = _execute(tmp_path, ((),))
    bundle = _bundle(execution)
    events = execution.store.events(execution.run.id)
    artifacts = tuple(
        artifact for _, artifact in sorted(execution.artifact_store.manifest().items())
    )

    assert bundle.run is not None
    assert bundle.configuration == {"configuration": {"test": "rework"}}
    assert bundle.activity_profile is not None
    assert bundle.risk_assessment is not None
    assert bundle.pack_bindings
    assert bundle.verified_events
    assert bundle.event_head_hash == events[-1].hash
    assert bundle.lineage
    assert verify_assurance_bundle(bundle, events=events, artifacts=artifacts).valid

    altered_report = bundle.model_copy(
        update={"report": bundle.report.model_copy(update={"summary": {"tampered": 1}})}
    )
    assert not verify_assurance_bundle(altered_report, events=events, artifacts=artifacts).valid

    altered_configuration = bundle.model_copy(
        update={"configuration": {"tampered": True}, "bundle_sha256": None}
    )
    altered_configuration = altered_configuration.model_copy(
        update={"bundle_sha256": altered_configuration.content_sha256}
    )
    assert not verify_assurance_bundle(
        altered_configuration,
        events=events,
        artifacts=artifacts,
    ).valid

    altered_event = events[0].model_copy(update={"payload": {"tampered": True}})
    assert not verify_assurance_bundle(
        bundle,
        events=(altered_event, *events[1:]),
        artifacts=artifacts,
    ).valid


def test_completing_a_run_produces_the_assurance_bundle_export(tmp_path: Path) -> None:
    """Completion, not a read, is what produces the bundle (ASSUR-01)."""
    execution, _ = _execute(tmp_path, ((),))

    payload = execution.store.read_export(execution.run.id, ASSURANCE_BUNDLE_EXPORT)

    assert payload is not None
    bundle = AssuranceBundle.model_validate_json(canonical_json(payload))
    assert bundle.run_id == execution.run.id
    assert bundle.report == _latest_report(execution)
    assert bundle.decision == _latest_decision(execution)
    assert verify_assurance_bundle(
        bundle,
        events=execution.store.events(execution.run.id),
        store=execution.artifact_store,
    ).valid


def test_a_run_parked_for_human_review_produces_no_assurance_bundle(tmp_path: Path) -> None:
    """A Run that never completes has nothing to assure, and writes no export."""
    execution, state = _execute(
        tmp_path,
        ((_finding(),),),
        decision=Decision.REQUIRE_HUMAN_REVIEW,
    )

    assert state.decision is not None
    assert state.decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert execution.store.read_export(execution.run.id, ASSURANCE_BUNDLE_EXPORT) is None


def test_the_assurance_export_is_rewritten_identically_on_a_repeated_completion(
    tmp_path: Path,
) -> None:
    """The writer is idempotent, so a redelivered completion cannot change the bundle."""
    execution, _ = _execute(tmp_path, ((),))
    first = execution.store.read_export(execution.run.id, ASSURANCE_BUNDLE_EXPORT)

    execution.graph.invoke(None, {"configurable": {"thread_id": execution.run.id}})

    assert execution.store.read_export(execution.run.id, ASSURANCE_BUNDLE_EXPORT) == first


def test_restart_reconstructs_checkpoint_and_run_from_persisted_evidence(tmp_path: Path) -> None:
    """A new graph instance reads the same terminal state without CLI-owned state."""
    execution, state = _execute(tmp_path, ((_finding(),), ()))
    config: RunnableConfig = {"configurable": {"thread_id": execution.run.id}}
    restarted = execution.graph.get_state(config)
    restored = RuntimeState.model_validate(restarted.values)
    projection = RunController(LocalRunStore(tmp_path / "runs")).current_state(execution.run.id)

    assert restored.run_id == execution.run.id
    assert restored.reopen_count == state.reopen_count == 1
    assert projection.condition is RunCondition.TERMINAL
    assert projection.outcome is RunOutcome.COMPLETED
