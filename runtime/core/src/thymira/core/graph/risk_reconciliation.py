"""Reconcile MIRA's preflight risk proposal with later reviewed Core evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import canonical_json
from thymira.mira.evidence import build_evidence_observations
from thymira.mira.flow import MiraGraphInput
from thymira.mira.preflight import MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX
from thymira.policies import RiskProfile
from thymira.schemas import (
    ActorKind,
    EventType,
    RiskAssessment,
    RiskLevel,
    approval_decision_id,
    approval_names_decision,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from thymira.mira.checks import AuditMode
    from thymira.mira.flow import MiraAuditFlow
    from thymira.mira.orchestrator import MiraAuditResult, MiraPreflightResult
    from thymira.schemas import ActivityProfile, DagBoard, Event, Framework, PlanBoard, Run
    from thymira.state import ArtifactStore


_MISSING_RISK_CLASSIFICATION = ("risk_classification",)
_MISSING_RISK_FINDING = "MIRA-RISK-001"
RISK_UNCERTAINTY_REVIEW_SUMMARY = (
    "MIRA could not classify inherent risk with sufficient confidence after one attempt; "
    "human review is required."
)
_CLASSIFIED_ACTIVITY_CATEGORIES = frozenset(
    {
        "data_analysis",
        "model_development",
        "decision_support",
        "automated_decision",
        "deployment",
    }
)


def audit_with_reviewed_risk_classification(
    flow: MiraAuditFlow,
    preflight: MiraPreflightResult,
    *,
    run: Run,
    profile: ActivityProfile,
    events: tuple[Event, ...],
    artifact_store: ArtifactStore,
    frameworks: tuple[Framework, ...],
    audit_mode: AuditMode,
    audited_at: datetime,
    current_risk: RiskProfile | None,
    uncertainty_reviewed: bool,
    plan_history: tuple[PlanBoard, ...] = (),
    dag_board: DagBoard | None = None,
) -> MiraAuditResult:
    """Build and run one MIRA audit with the evidence-backed preflight projection."""
    graph_input = MiraGraphInput(
        run=run,
        activity_profile=profile,
        events=events,
        evidence_observations=build_evidence_observations(run.id, events, artifact_store),
        frameworks=frameworks,
        audit_mode=audit_mode,
        audited_at=audited_at,
        plan_history=plan_history,
        dag_board=dag_board,
    )
    audit_preflight = reconcile_reviewed_risk_classification(
        preflight,
        current_risk,
        events,
        profile,
        uncertainty_reviewed=uncertainty_reviewed,
    )
    flow.restore_preflight(graph_input, audit_preflight)
    return flow.audit(graph_input)


def reconcile_reviewed_risk_classification(
    preflight: MiraPreflightResult,
    current_risk: RiskProfile | None,
    events: Sequence[Event],
    profile: ActivityProfile,
    *,
    uncertainty_reviewed: bool,
) -> MiraPreflightResult:
    """Stop carrying a resolved synthetic risk-uncertainty finding into the final audit.

    The original immutable MIRA assessment remains in the event log. Only its proposal finding is
    retired, and only when the current RuntimeState exactly matches a later Core level/category
    classification linked to that assessment and a human has accepted the uncertainty review.
    Every malformed or mismatched fact fails closed by returning the original preflight result.
    """
    assessment = preflight.risk_assessment
    if (
        not uncertainty_reviewed
        or current_risk is None
        or assessment.missing_information != _MISSING_RISK_CLASSIFICATION
        or not assessment.justification.startswith(MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX)
    ):
        return preflight
    if not risk_uncertainty_review_resolved(events, assessment, profile):
        return preflight
    assessment_seq = _recorded_assessment_seq(events, assessment)
    if assessment_seq is None:
        return preflight
    classified = _latest_linked_classification(events, assessment, profile, assessment_seq)
    if classified is None or classified != current_risk or not _classifies_risk(classified):
        return preflight
    findings = tuple(
        finding
        for finding in preflight.audit_findings
        if finding.control_id != _MISSING_RISK_FINDING
    )
    if len(findings) == len(preflight.audit_findings):
        return preflight
    return preflight.model_copy(update={"audit_findings": findings})


def risk_uncertainty_review_resolved(
    events: Sequence[Event], assessment: RiskAssessment, profile: ActivityProfile
) -> bool:
    """Return whether a human approved this exact assessment and profile-version scope."""
    if (
        assessment.run_id != profile.run_id
        or assessment.activity_profile_id != profile.id
        or assessment.activity_profile_version != profile.version
    ):
        return False
    assessment_seq = _recorded_assessment_seq(events, assessment)
    if assessment_seq is None:
        return False
    requested = _latest_scoped_review_request(events, assessment, profile, assessment_seq)
    if requested is None:
        return False
    decision_id = approval_decision_id(requested.payload)
    if decision_id is None:
        return False
    return any(
        event.seq > requested.seq
        and event.run_id == profile.run_id
        and event.type is EventType.HUMAN_APPROVAL
        and event.subject_id == requested.subject_id
        and approval_names_decision(event.payload, decision_id)
        and event.payload.get("approved") is True
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
        and event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        for event in events
    )


def _latest_scoped_review_request(
    events: Sequence[Event],
    assessment: RiskAssessment,
    profile: ActivityProfile,
    assessment_seq: int,
) -> Event | None:
    """Find the latest review request bound to the exact immutable risk evidence."""
    for event in reversed(events):
        if (
            event.seq <= assessment_seq
            or event.run_id != profile.run_id
            or event.type is not EventType.HUMAN_APPROVAL_REQUESTED
            or event.subject_id != profile.run_id
            or event.actor.kind is not ActorKind.SYSTEM
            or not event.actor.authenticated
            or event.payload.get("summary") != RISK_UNCERTAINTY_REVIEW_SUMMARY
        ):
            continue
        scope = event.payload.get("request_payload")
        if not isinstance(scope, dict):
            continue
        if (
            scope.get("risk_assessment_id") == assessment.id
            and scope.get("activity_profile_id") == profile.id
            and scope.get("activity_profile_version") == profile.version
        ):
            return event
    return None


def _recorded_assessment_seq(events: Sequence[Event], assessment: RiskAssessment) -> int | None:
    """Return the sequence of MIRA's exact immutable assessment record."""
    for event in reversed(events):
        if (
            event.type is not EventType.RISK_ASSESSMENT_RECORDED
            or event.producer != "thymira.mira"
            or event.subject_id != assessment.id
        ):
            continue
        value = event.payload.get("risk_assessment")
        if not isinstance(value, dict):
            return None
        try:
            recorded = RiskAssessment.model_validate_json(canonical_json(value))
        except (TypeError, ValueError):
            return None
        return event.seq if recorded == assessment else None
    return None


def _latest_linked_classification(
    events: Sequence[Event],
    assessment: RiskAssessment,
    profile: ActivityProfile,
    assessment_seq: int,
) -> RiskProfile | None:
    """Validate the latest Core classification for the assessment's exact profile version."""
    for event in reversed(events):
        if event.type is not EventType.RISK_CLASSIFIED:
            continue
        if (
            event.payload.get("activity_profile_id") != profile.id
            or event.payload.get("activity_profile_version") != profile.version
        ):
            continue
        if (
            event.seq <= assessment_seq
            or event.producer != "thymira.core"
            or event.subject_id != profile.id
            or event.payload.get("mira_risk_assessment_id") != assessment.id
        ):
            return None
        value = event.payload.get("risk_profile")
        if not isinstance(value, dict):
            return None
        try:
            return RiskProfile.model_validate(value)
        except (TypeError, ValueError):
            return None
    return None


def _classifies_risk(profile: RiskProfile) -> bool:
    """Return whether Core supplied the level/category pair MIRA could not select."""
    try:
        level = RiskLevel(profile.risk_level)
    except ValueError:
        return False
    category = profile.activity_category.strip()
    return level is not RiskLevel.UNKNOWN and category in _CLASSIFIED_ACTIVITY_CATEGORIES
