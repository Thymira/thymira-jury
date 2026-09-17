"""Fail-closed tests for the assurance bundle's full evidence inventory (ASSUR-01).

A final bundle carries the complete Run inventory -- configuration, profile, risk assessment,
pack bindings, the verified event chain, artifacts, decisions and approvals. Every one of those
indexes must stay an exact view of the evidence the Run recorded: a bundle that disagrees with
its own chain is not assurance, so it cannot be built at all. These tests assert one rejection
per invariant, and that the assembly path derives the indexes rather than trusting a caller.
"""

from __future__ import annotations

import pytest

from thymira.events import InMemoryEventLog, canonical_json
from thymira.mira import AssuranceBundle, assemble_run_assurance, build_assurance
from thymira.mira.assurance import latest_audit_report, latest_findings_decision
from thymira.mira.checks import AuditReport
from thymira.schemas import (
    ActivityProfile,
    Actor,
    Artifact,
    ArtifactKind,
    AuditFinding,
    Decision,
    Event,
    EventType,
    Framework,
    PackBinding,
    PolicyDecision,
    RiskAssessment,
    RiskLevel,
    Run,
    Severity,
    new_id,
)


def _profile(run_id: str) -> ActivityProfile:
    """Build the activity profile MIRA preflight records for a Run."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run_id,
        purpose="Prioritise credit applications for human review.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
    )


def _assessment(profile: ActivityProfile) -> RiskAssessment:
    """Build the inherent-risk assessment recorded beside the profile."""
    return RiskAssessment(
        id=new_id("assessment"),
        run_id=profile.run_id,
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        assessor=Actor.system(),
        subject_kind="activity_profile",
        subject_id=profile.id,
        risk_level=RiskLevel.LOW,
        activity_category="credit_scoring_support",
        confidence=0.9,
        justification="No regulated decisioning is in scope.",
        assessment_method="base-risk",
        assessment_method_version="1.0",
    )


def _binding(profile: ActivityProfile) -> PackBinding:
    """Build the pack binding recorded for the profile version."""
    return PackBinding(
        id=new_id("binding"),
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        pack_id=new_id("pack"),
        pack_version="1.0",
        applicability_rules_version="1.0",
        jurisdiction="ES",
        applicable=False,
        rationale="Out of scope for this activity profile.",
    )


def _run(run_id: str) -> Run:
    """Build the Run record embedded in a complete bundle."""
    return Run(
        id=run_id,
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Produce a governed model analysis.",
    )


def _decision(run_id: str) -> PolicyDecision:
    """Build the findings decision the bundle replays."""
    return PolicyDecision(
        id=new_id("decision"),
        run_id=run_id,
        subject_kind="findings",
        subject_id="findings",
        decision=Decision.PASS,
        rule_id="default",
        reason="no blocking findings",
        policy_name="credit-risk@1.0",
        policy_sha256="d" * 64,
    )


def _finding(run_id: str) -> AuditFinding:
    """Build one finding so the review and traceability indexes are non-empty."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="A1",
        framework=Framework.METHODOLOGY,
        title="Split evidence is incomplete",
        finding="The split evidence does not name a random state.",
        severity=Severity.LOW,
        confidence=0.9,
    )


class _Recorded:
    """One Run's recorded evidence, in the shape a completed Run leaves behind."""

    def __init__(self) -> None:
        self.run_id = new_id("run")
        self.run = _run(self.run_id)
        log = InMemoryEventLog(self.run_id)
        started = log.append(
            EventType.RUN_STARTED,
            Actor.system(),
            {"configuration": {"seed": 7}},
            subject_id=self.run_id,
        )
        self.profile = _profile(self.run_id)
        self.assessment = _assessment(self.profile)
        self.binding = _binding(self.profile)
        log.append(
            EventType.AUDIT_STARTED,
            Actor.system(),
            {
                "activity_profile": self.profile.to_json_dict(),
                "risk_assessment": self.assessment.to_json_dict(),
                "pack_binding": self.binding.to_json_dict(),
            },
            subject_id=self.run_id,
        )
        self.decision = _decision(self.run_id)
        log.append(
            EventType.POLICY_DECISION,
            Actor.system(),
            self.decision.to_json_dict(),
            subject_id=self.run_id,
        )
        self.report = AuditReport(
            run_id=self.run_id,
            status="passed",
            controls=(),
            findings=(_finding(self.run_id),),
            terminal_hash=started.hash,
        )
        log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {"status": self.report.status, "audit_report": self.report.to_json_dict()},
            subject_id=self.run_id,
        )
        self.events = tuple(log.events())

    def bundle(self) -> AssuranceBundle:
        """Assemble the bundle exactly as the composition does at Run completion."""
        assembled = assemble_run_assurance(self.run_id, events=self.events, run=self.run)
        assert assembled is not None
        return assembled

    def rebuild(self, **overrides: object) -> AssuranceBundle:
        """Rebuild the bundle with one index replaced, so a single invariant is exercised."""
        payload = self.bundle().to_json_dict()
        payload.update(overrides)
        payload["bundle_sha256"] = None
        return AssuranceBundle.model_validate_json(canonical_json(payload))


@pytest.fixture
def recorded() -> _Recorded:
    """Provide one Run's recorded evidence."""
    return _Recorded()


def test_assemble_derives_every_index_from_the_recorded_evidence(recorded: _Recorded) -> None:
    """The assembly path reads the Run's own events; a caller supplies no index."""
    bundle = recorded.bundle()

    assert bundle.run == recorded.run
    assert bundle.configuration == {"configuration": {"seed": 7}}
    assert bundle.activity_profile == recorded.profile
    assert bundle.risk_assessment == recorded.assessment
    assert bundle.pack_bindings == (recorded.binding,)
    assert bundle.decisions == (recorded.decision,)
    assert bundle.approvals == ()
    assert bundle.verified_events == recorded.events
    assert bundle.event_head_hash == recorded.events[-1].hash
    assert bundle.terminal_hash == recorded.report.terminal_hash
    assert bundle.bundle_sha256 is not None


def test_assemble_returns_nothing_without_an_audit_report() -> None:
    """A Run that recorded no report has nothing to assure."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {})

    assert assemble_run_assurance(log.run_id, events=log.events()) is None


def test_assemble_returns_nothing_without_a_findings_decision(recorded: _Recorded) -> None:
    """A recorded audit without a Gate decision is not yet assurable."""
    without_decision = tuple(
        event for event in recorded.events if event.type is not EventType.POLICY_DECISION
    )

    assert assemble_run_assurance(recorded.run_id, events=without_decision) is None


def test_latest_audit_report_skips_lifecycle_markers(recorded: _Recorded) -> None:
    """An audit.completed event without a report payload is a marker, not evidence."""
    marker = Event(
        event_id=new_id("event"),
        run_id=recorded.run_id,
        seq=99,
        type=EventType.AUDIT_COMPLETED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.core",
        producer_version="0.1",
        payload={"status": "completed"},
    )

    assert latest_audit_report((*recorded.events, marker)) == recorded.report


def test_latest_audit_report_rejects_a_malformed_payload(recorded: _Recorded) -> None:
    """A malformed persisted report fails closed instead of assembling a bundle."""
    broken = Event(
        event_id=new_id("event"),
        run_id=recorded.run_id,
        seq=99,
        type=EventType.AUDIT_COMPLETED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.mira",
        producer_version="0.1",
        payload={"audit_report": {"run_id": recorded.run_id}},
    )

    with pytest.raises(ValueError, match="malformed"):
        latest_audit_report((*recorded.events, broken))


def test_latest_findings_decision_rejects_a_malformed_payload(recorded: _Recorded) -> None:
    """A malformed persisted decision fails closed instead of assembling a bundle."""
    broken = Event(
        event_id=new_id("event"),
        run_id=recorded.run_id,
        seq=99,
        type=EventType.POLICY_DECISION,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.policies",
        producer_version="0.1",
        payload={"id": "not-a-decision"},
    )

    with pytest.raises(ValueError, match="malformed"):
        latest_findings_decision((*recorded.events, broken), recorded.run_id)


def test_latest_findings_decision_ignores_another_runs_decision(recorded: _Recorded) -> None:
    """Only the scoped Run's findings decision is assurable."""
    other = _decision(new_id("run"))
    log = InMemoryEventLog(recorded.run_id)
    foreign = log.append(EventType.POLICY_DECISION, Actor.system(), other.to_json_dict())

    assert (
        latest_findings_decision((*recorded.events, foreign), recorded.run_id) == recorded.decision
    )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("run", "run.id must match the bundle run_id"),
        ("activity_profile", "activity_profile must belong to the bundle Run"),
        ("risk_assessment", "risk_assessment must belong to the bundle Run"),
    ],
)
def test_the_inventory_must_belong_to_the_bundle_run(
    recorded: _Recorded,
    field: str,
    message: str,
) -> None:
    """A record from another Run can never be indexed as this Run's evidence."""
    foreign = new_id("run")
    other_profile = _profile(foreign)
    replacements = {
        "run": _run(foreign).to_json_dict(),
        "activity_profile": other_profile.to_json_dict(),
        "risk_assessment": _assessment(other_profile).to_json_dict(),
    }

    with pytest.raises(ValueError, match=message):
        recorded.rebuild(**{field: replacements[field]})


def test_artifacts_must_belong_to_the_bundle_run(recorded: _Recorded) -> None:
    """An artifact from another Run cannot enter this Run's manifest."""
    foreign = Artifact(
        id=new_id("artifact"),
        run_id=new_id("run"),
        name="model.pkl",
        kind=ArtifactKind.MODEL,
        uri="artifacts/model.pkl",
        sha256="a" * 64,
        size_bytes=1,
        produced_by=new_id("task"),
    )

    with pytest.raises(ValueError, match="artifacts must belong to the bundle Run"):
        recorded.rebuild(artifacts=[foreign.to_json_dict()])


def test_decisions_must_belong_to_the_bundle_run(recorded: _Recorded) -> None:
    """The decision index is scoped to the Run the bundle describes."""
    with pytest.raises(ValueError, match="decisions must belong to the bundle Run"):
        recorded.rebuild(decisions=[_decision(new_id("run")).to_json_dict()])


def test_decisions_must_include_the_bundle_decision(recorded: _Recorded) -> None:
    """A decision index that omits the replayed decision cannot be reconciled."""
    other = _decision(recorded.run_id)

    with pytest.raises(ValueError, match="decisions must include the bundle decision"):
        recorded.rebuild(decisions=[other.to_json_dict()])


def test_flow_version_must_match_the_reports_graph_hash(recorded: _Recorded) -> None:
    """The flow version pins the exact audit graph that produced the report."""
    report = recorded.report.model_copy(update={"graph_definition_hash": "e" * 64})

    with pytest.raises(ValueError, match="flow_version must match"):
        build_assurance(recorded.run_id, report, recorded.decision, flow_version="f" * 64)


def test_policy_version_must_match_the_decision_policy(recorded: _Recorded) -> None:
    """The policy version names the exact policy the decision was taken under."""
    with pytest.raises(ValueError, match="policy_version must match"):
        recorded.rebuild(policy_version="other-policy@2.0")


def test_manifest_sha256_must_match_the_reports_manifest_hash(recorded: _Recorded) -> None:
    """The bundle manifest digest is the audit's, never a later recomputation."""
    report = recorded.report.model_copy(update={"manifest_sha256": "a" * 64})

    with pytest.raises(ValueError, match="manifest_sha256 must match"):
        build_assurance(recorded.run_id, report, recorded.decision, manifest_sha256="b" * 64)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("configuration", {"tampered": True}, "configuration does not match"),
        ("activity_profile", None, "activity_profile does not match"),
        ("risk_assessment", None, "risk_assessment does not match"),
        ("pack_bindings", [], "pack_bindings do not match"),
    ],
)
def test_derived_indexes_must_match_the_embedded_chain(
    recorded: _Recorded,
    field: str,
    value: object,
    message: str,
) -> None:
    """Each derived index stays an exact view of the embedded events."""
    with pytest.raises(ValueError, match=message):
        recorded.rebuild(**{field: value})


def test_the_embedded_event_chain_must_be_intact(recorded: _Recorded) -> None:
    """An altered event breaks the chain, and a broken chain cannot be assured."""
    altered = recorded.events[1].model_copy(update={"payload": {"tampered": True}})
    events = [event.to_json_dict() for event in (recorded.events[0], altered, *recorded.events[2:])]

    with pytest.raises(ValueError, match="valid event chain"):
        recorded.rebuild(verified_events=events)


def test_the_event_head_hash_must_be_the_chain_head(recorded: _Recorded) -> None:
    """The head hash names the last event the bundle embeds."""
    with pytest.raises(ValueError, match="event_head_hash must equal"):
        recorded.rebuild(event_head_hash="a" * 64)


def test_verified_events_must_belong_to_the_bundle_run(recorded: _Recorded) -> None:
    """The embedded chain is this Run's, never another's."""
    foreign = InMemoryEventLog(new_id("run"))
    foreign.append(EventType.RUN_STARTED, Actor.system(), {})
    events = [event.to_json_dict() for event in foreign.events()]

    with pytest.raises(ValueError, match="verified events must belong"):
        recorded.rebuild(verified_events=events)


def test_lineage_must_cover_every_bundled_artifact(recorded: _Recorded) -> None:
    """A lineage index that names an artifact the manifest does not hold is inconsistent."""
    stray = {
        "artifact_id": new_id("artifact"),
        "input_artifact_ids": [],
        "execution_key": None,
        "valid": True,
        "superseded_by": None,
    }

    with pytest.raises(ValueError, match="lineage must contain one entry"):
        recorded.rebuild(lineage=[stray])
