"""Unit tests for MIRA's immutable activity, applicability, and control contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from thymira.schemas import (
    ActivityProfile,
    Actor,
    ActorKind,
    AuditDisposition,
    AuthorizationDecision,
    ControlEvaluation,
    ControlEvaluationStatus,
    EventType,
    Evidence,
    PackBinding,
    RiskAssessment,
    RiskLevel,
    new_id,
)

SHA = "a" * 64
EVALUATED_AT = datetime(2026, 8, 25, 10, tzinfo=UTC)


def _profile(*, version: int = 1, supersedes_id: str | None = None) -> ActivityProfile:
    """Build a complete activity profile for contract validation tests."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=version,
        run_id=new_id("run"),
        purpose="Prioritise social-support applications for human review.",
        affected_population="Residents applying for municipal social support.",
        decision_effect="Changes the review order but not eligibility or award amounts.",
        autonomy="Recommends a priority order only.",
        human_oversight="A social worker can change or reject every recommendation.",
        jurisdiction="ES",
        data_categories=("household_income", "employment_status"),
        sensitive_attributes=("disability",),
        potential_consequences=("Delayed review of an urgent application.",),
        evidence_refs=(Evidence(kind="event", ref="seq:1", sha256=SHA),),
        supersedes_id=supersedes_id,
    )


def _binding(profile: ActivityProfile) -> PackBinding:
    """Bind a reviewed pack to a precise profile version."""
    return PackBinding(
        id=new_id("binding"),
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        pack_id=new_id("pack"),
        pack_version="2026.1",
        applicability_rules_version="2026.1",
        evaluated_as_of=EVALUATED_AT,
        jurisdiction=profile.jurisdiction,
        applicable=True,
        rationale="The profile affects natural persons in Spain.",
        activating_facts=("affected natural persons", "Spain"),
        evidence_refs=profile.evidence_refs,
    )


def _evaluation(
    binding: PackBinding,
    *,
    status: ControlEvaluationStatus,
    evidence: tuple[Evidence, ...] = (),
) -> ControlEvaluation:
    """Build one control evaluation under a reproducible binding."""
    return ControlEvaluation(
        id=new_id("control"),
        pack_binding_id=binding.id,
        pack_id=binding.pack_id,
        pack_version=binding.pack_version,
        control_id="HUMAN-OVERSIGHT-001",
        status=status,
        evidence=evidence,
        evaluated_at=EVALUATED_AT,
    )


def test_activity_profile_is_versioned_immutable_and_has_one_owner() -> None:
    profile = _profile()

    assert profile.version == 1
    with pytest.raises(ValidationError):
        profile.__setattr__("purpose", "Changed purpose")
    with pytest.raises(ValidationError, match="exactly one"):
        ActivityProfile.model_validate({**profile.model_dump(), "project_id": new_id("project")})
    with pytest.raises(ValidationError):
        ActivityProfile.model_validate({**profile.model_dump(), "unexpected": True})


def test_risk_assessment_is_linked_to_the_exact_profile_version() -> None:
    profile = _profile()
    assessment = RiskAssessment(
        id=new_id("assessment"),
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        assessor=Actor(kind=ActorKind.AGENT, id="mira-risk"),
        subject_kind="activity",
        subject_id=profile.activity_id,
        risk_level=RiskLevel.HIGH,
        activity_category="social_support_triage",
        confidence=0.8,
        missing_information=("Training data retention is not yet documented.",),
        justification="Prioritisation can delay urgent support reviews.",
        evidence=profile.evidence_refs,
        assessment_method="mira-inherent-risk",
        assessment_method_version="1.0",
    )

    assert assessment.activity_profile_id == profile.id
    assert assessment.activity_profile_version == profile.version
    assert "mitigations" not in RiskAssessment.model_fields
    with pytest.raises(ValidationError):
        RiskAssessment.model_validate({**assessment.model_dump(), "activity_profile_version": 0})


def test_pack_binding_pins_profile_pack_rules_and_time() -> None:
    profile = _profile()
    binding = _binding(profile)

    assert binding.activity_profile_id == profile.id
    assert binding.activity_profile_version == profile.version
    assert binding.evaluated_as_of == EVALUATED_AT
    assert binding.applicability_rules_version == "2026.1"


@pytest.mark.parametrize(
    "status",
    [
        ControlEvaluationStatus.SATISFIED,
        ControlEvaluationStatus.FAILED,
        ControlEvaluationStatus.STALE_EVIDENCE,
        ControlEvaluationStatus.UNVERIFIED,
    ],
)
def test_control_evaluation_requires_evidence_for_evidence_based_statuses(
    status: ControlEvaluationStatus,
) -> None:
    with pytest.raises(ValidationError, match="requires evidence"):
        _evaluation(_binding(_profile()), status=status)


@pytest.mark.parametrize(
    ("status", "evidence_required"),
    [
        (ControlEvaluationStatus.SATISFIED, True),
        (ControlEvaluationStatus.FAILED, True),
        (ControlEvaluationStatus.MISSING_EVIDENCE, False),
        (ControlEvaluationStatus.STALE_EVIDENCE, True),
        (ControlEvaluationStatus.UNVERIFIED, True),
        (ControlEvaluationStatus.REQUIRES_HUMAN_REVIEW, False),
    ],
)
def test_control_evaluation_accepts_every_closed_status(
    status: ControlEvaluationStatus, evidence_required: bool
) -> None:
    binding = _binding(_profile())
    evidence = (Evidence(kind="artifact", ref=new_id("artifact"), sha256=SHA),)

    evaluation = _evaluation(binding, status=status, evidence=evidence if evidence_required else ())

    assert evaluation.status is status


def test_control_evaluation_rejects_impossible_evidence_and_unknown_status() -> None:
    binding = _binding(_profile())
    evidence = (Evidence(kind="artifact", ref=new_id("artifact"), sha256=SHA),)

    with pytest.raises(ValidationError, match="cannot include evidence"):
        _evaluation(binding, status=ControlEvaluationStatus.MISSING_EVIDENCE, evidence=evidence)
    with pytest.raises(ValidationError):
        _evaluation(
            binding,
            status=cast("ControlEvaluationStatus", "unknown"),
        )


def test_supersession_requires_a_later_profile_version_and_never_self_references() -> None:
    first = _profile()
    successor = _profile(version=2, supersedes_id=first.id)

    assert successor.supersedes_id == first.id
    with pytest.raises(ValidationError, match="greater than 1"):
        _profile(version=1, supersedes_id=first.id)
    with pytest.raises(ValidationError, match="cannot equal id"):
        ActivityProfile.model_validate({**first.model_dump(), "supersedes_id": first.id})


def test_audit_disposition_and_authorization_decision_are_distinct_vocabularies() -> None:
    assert AuditDisposition.BLOCK.value == "BLOCK"
    assert AuthorizationDecision.DENY.value == "DENY"
    assert AuthorizationDecision.ALLOW.value == "ALLOW"
    assert not hasattr(AuthorizationDecision, "BLOCK")


def test_mira_contract_events_are_specific_to_the_new_records() -> None:
    assert {
        EventType.ACTIVITY_PROFILE_RECORDED,
        EventType.RISK_ASSESSMENT_RECORDED,
        EventType.PACK_BINDING_RECORDED,
        EventType.CONTROL_EVALUATION_RECORDED,
        EventType.MIRA_CONTEXT_CREATED,
    } == {
        EventType("activity_profile.recorded"),
        EventType("risk_assessment.recorded"),
        EventType("pack_binding.recorded"),
        EventType("control_evaluation.recorded"),
        EventType("mira.context_created"),
    }
