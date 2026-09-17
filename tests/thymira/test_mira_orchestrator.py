"""Unit tests for the sequential deterministic MIRA audit orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime

from thymira.events import InMemoryEventLog
from thymira.mira import MiraAuditOrchestrator, MiraAuditSnapshot, load_default_packs
from thymira.mira.checks import ControlStatus
from thymira.mira.orchestrator import _deduplicate_findings
from thymira.mira.preflight import EvidenceObservation, PackControl
from thymira.policies import Gate, PolicyEngine, load_policy_stack
from thymira.schemas import (
    ActionKind,
    ActivityProfile,
    Actor,
    AuditFinding,
    ControlEvaluationStatus,
    Decision,
    EventType,
    Evidence,
    Framework,
    RiskLevel,
    Run,
    Severity,
    new_id,
)

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
SHA = "a" * 64


def _run_and_events(
    *, policy_sha256: str | None = None, decision_policy_sha256: str | None = None
) -> tuple[Run, InMemoryEventLog]:
    """Build a closed, provenance-complete Run with a valid event chain."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    started_payload = {"run_environment": {"python": "3.13"}}
    if policy_sha256 is not None:
        started_payload["policy_sha256"] = policy_sha256
    log.append(EventType.RUN_STARTED, Actor.system(), started_payload)
    if decision_policy_sha256 is not None:
        log.append(
            EventType.POLICY_DECISION,
            Actor.system(),
            {"decision": Decision.PASS.value, "policy_sha256": decision_policy_sha256},
        )
    log.append(EventType.RUN_COMPLETED, Actor.system(), {})
    return (
        Run(
            id=run_id,
            project_id=new_id("project"),
            session_id=new_id("session"),
            prompt="Audit the credit-risk evidence.",
        ),
        log,
    )


def _snapshot(
    *,
    data_categories: tuple[str, ...] = ("credit_history",),
    potential_consequences: tuple[str, ...] = ("Delayed review of an application.",),
    observations: tuple[EvidenceObservation, ...] | None = None,
    policy_sha256: str | None = None,
    decision_policy_sha256: str | None = None,
) -> MiraAuditSnapshot:
    """Build a snapshot with optional declared evidence observations."""
    run, log = _run_and_events(
        policy_sha256=policy_sha256, decision_policy_sha256=decision_policy_sha256
    )
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Prioritise credit applications for human review.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=data_categories,
        potential_consequences=potential_consequences,
        evidence_refs=(Evidence(kind="event", ref="seq:0", sha256=log.events()[0].hash),),
    )
    return MiraAuditSnapshot(
        run=run,
        activity_profile=profile,
        events=tuple(log.events()),
        evidence_observations=_complete_observations() if observations is None else observations,
        audited_at=NOW,
    )


def _complete_observations() -> tuple[EvidenceObservation, ...]:
    """Return the event and artifact evidence required by the two applicable packs."""
    return (
        EvidenceObservation(
            evidence=Evidence(kind="event", ref="seq:0", sha256=SHA),
            integrity_verified=True,
            observed_at=NOW,
        ),
        EvidenceObservation(
            evidence=Evidence(kind="artifact", ref=new_id("artifact"), sha256=SHA),
            integrity_verified=True,
            observed_at=NOW,
        ),
    )


def _orchestrator(*, duplicate_methodology_pack: bool = False) -> MiraAuditOrchestrator:
    """Build the sequential orchestrator with the reviewed default packs."""
    packs = load_default_packs()
    if duplicate_methodology_pack:
        packs = (*packs, packs[0])
    return MiraAuditOrchestrator(packs, Actor.system())


def test_orchestrator_returns_complete_happy_path_result() -> None:
    result = _orchestrator().audit(_snapshot())

    assert result.risk_assessment.risk_level is RiskLevel.LOW
    assert all(binding.applicable for binding in result.pack_bindings)
    assert all(
        evaluation.status is ControlEvaluationStatus.SATISFIED
        for evaluation in result.control_evaluations
    )
    assert result.audit_findings == ()
    assert result.action_intents == ()
    assert result.audit_report.control_set == "thymira-mira-audit@0.1"
    assert result.audit_report.status == "passed"


def test_orchestrator_proposes_review_when_information_is_insufficient() -> None:
    result = _orchestrator().audit(_snapshot(data_categories=(), potential_consequences=()))

    assert result.risk_assessment.risk_level is RiskLevel.UNKNOWN
    assert result.risk_assessment.missing_information == (
        "data_categories",
        "potential_consequences",
    )
    assert {finding.control_id for finding in result.audit_findings} >= {"MIRA-RISK-001"}
    assert all(intent.action_kind is ActionKind.REVIEW_FINDINGS for intent in result.action_intents)


def test_orchestrator_keeps_nonapplicable_pack_binding_without_running_its_controls() -> None:
    result = _orchestrator().audit(_snapshot(data_categories=("income",)))

    credit_binding = next(
        binding for binding in result.pack_bindings if binding.pack_id.endswith("02")
    )
    assert credit_binding.applicable is False
    assert {evaluation.control_id for evaluation in result.control_evaluations} == {
        "METHODOLOGY-EVIDENCE-001"
    }


def test_orchestrator_reports_missing_evidence_as_a_control_finding() -> None:
    result = _orchestrator().audit(_snapshot(observations=()))

    assert {evaluation.status for evaluation in result.control_evaluations} == {
        ControlEvaluationStatus.MISSING_EVIDENCE
    }
    assert {finding.control_id for finding in result.audit_findings} == {
        "METHODOLOGY-EVIDENCE-001",
        "CREDIT-OVERSIGHT-001",
    }


def test_orchestrator_reports_manipulated_evidence_without_modifying_the_snapshot() -> None:
    event = EvidenceObservation(
        evidence=Evidence(kind="event", ref="seq:0", sha256=SHA),
        integrity_verified=False,
        observed_at=NOW,
    )
    artifact = EvidenceObservation(
        evidence=Evidence(kind="artifact", ref=new_id("artifact"), sha256=SHA),
        integrity_verified=True,
        observed_at=NOW,
    )
    snapshot = _snapshot(observations=(event, artifact))
    before_events = snapshot.events
    before_profile = snapshot.activity_profile

    result = _orchestrator().audit(snapshot)

    assert any(
        evaluation.status is ControlEvaluationStatus.FAILED
        for evaluation in result.control_evaluations
    )
    assert snapshot.events == before_events
    assert snapshot.activity_profile == before_profile


def test_orchestrator_deduplicates_pack_results_and_creates_bounded_review_intents() -> None:
    result = _orchestrator(duplicate_methodology_pack=True).audit(_snapshot(observations=()))

    assert len(result.pack_bindings) == 2
    assert len(result.audit_findings) == 2
    assert len(result.action_intents) == 2
    assert {intent.subject_id for intent in result.action_intents} == {
        finding.id for finding in result.audit_findings
    }
    assert all(intent.subject_kind == "findings" for intent in result.action_intents)


def test_deduplication_keeps_the_highest_severity_record_for_equivalent_findings() -> None:
    """An equivalent deterministic finding cannot be downgraded by a later agent candidate."""
    run_id = new_id("run")
    low_evidence = Evidence(kind="event", ref="seq:7", sha256="b" * 64)
    critical_evidence = Evidence(kind="event", ref="seq:7", sha256="a" * 64)
    low = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        agent_id=new_id("agent"),
        control_id="A20",
        framework=Framework.INTERNAL,
        title="Agent observation",
        finding="The feature leaks the target.",
        severity=Severity.LOW,
        confidence=0.2,
        evidence=(low_evidence,),
    )
    critical = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="A20",
        framework=Framework.INTERNAL,
        title="Deterministic leakage control",
        finding="The feature leaks the target.",
        severity=Severity.CRITICAL,
        confidence=1.0,
        evidence=(critical_evidence,),
    )

    deduplicated = _deduplicate_findings((low, critical))

    assert deduplicated == (critical,)
    assert deduplicated[0].confidence == 1.0
    assert deduplicated[0].evidence == (critical_evidence,)
    gate = Gate(PolicyEngine(load_policy_stack("credit_risk")), InMemoryEventLog(run_id))
    assert gate.review_findings(deduplicated).decision is Decision.BLOCK


def test_orchestrator_is_deterministic_for_the_same_snapshot() -> None:
    snapshot = _snapshot()
    orchestrator = _orchestrator()

    assert orchestrator.audit(snapshot) == orchestrator.audit(snapshot)


def test_run_deterministic_passes_the_expected_policy_snapshot_to_a16() -> None:
    """The deterministic orchestrator supplies the policy hash recorded when the Run started."""
    expected = "a" * 64
    snapshot = _snapshot(policy_sha256=expected, decision_policy_sha256="b" * 64)

    report = _orchestrator().run_deterministic(snapshot)

    a16 = next(control for control in report.controls if control.control_id == "A16")
    assert a16.status is ControlStatus.FAILED
    assert "expected policy snapshot" in a16.detail


def test_reordered_preflight_bindings_still_match_controls_by_pack_id() -> None:
    """Rehydrated bindings are paired with their reviewed pack by identity, not position."""
    snapshot = _snapshot()
    orchestrator = _orchestrator()
    preflight = orchestrator.run_governance_preflight(snapshot)
    reordered = preflight.model_copy(
        update={"pack_bindings": tuple(reversed(preflight.pack_bindings))}
    )

    result = orchestrator.run_evidence_audit(snapshot, reordered)

    assert {evaluation.pack_binding_id for evaluation in result.control_evaluations} == {
        binding.id for binding in preflight.pack_bindings
    }
    assert all(
        evaluation.pack_id
        == next(
            binding.pack_id
            for binding in reordered.pack_bindings
            if binding.id == evaluation.pack_binding_id
        )
        for evaluation in result.control_evaluations
    )


def test_evaluation_findings_include_the_pack_in_their_stable_identity() -> None:
    """Two packs declaring one control id retain distinct finding identities."""
    base = load_default_packs()[0]
    event_control = PackControl(
        id="SHARED-CONTROL-001",
        title="Shared event evidence",
        required_evidence_kinds=("event",),
        max_age_days=30,
    )
    artifact_control = event_control.model_copy(
        update={"title": "Shared artifact evidence", "required_evidence_kinds": ("artifact",)}
    )
    event_pack = base.model_copy(
        update={"id": "pack_" + "1" * 32, "name": "event-pack", "controls": (event_control,)}
    )
    artifact_pack = base.model_copy(
        update={
            "id": "pack_" + "2" * 32,
            "name": "artifact-pack",
            "controls": (artifact_control,),
        }
    )
    snapshot = _snapshot(
        observations=(
            EvidenceObservation(
                evidence=Evidence(kind="event", ref="seq:0", sha256=SHA),
                integrity_verified=False,
                observed_at=NOW,
            ),
            EvidenceObservation(
                evidence=Evidence(kind="artifact", ref="metrics.json", sha256=SHA),
                integrity_verified=False,
                observed_at=NOW,
            ),
        )
    )
    orchestrator = MiraAuditOrchestrator((event_pack, artifact_pack), Actor.system())

    result = orchestrator.audit(snapshot)

    shared = [
        finding for finding in result.audit_findings if finding.control_id == "SHARED-CONTROL-001"
    ]
    assert len(shared) == 2
    assert len({finding.id for finding in shared}) == 2
