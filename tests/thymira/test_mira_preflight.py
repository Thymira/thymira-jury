"""Unit tests for deterministic MIRA preflight rules and evidence controls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from thymira.mira import MiraAuditOrchestrator, MiraAuditSnapshot
from thymira.mira.preflight import (
    ApplicabilityResolver,
    BaseRiskEvaluator,
    EvidenceObservation,
    GenericPackControlRunner,
    PackControl,
    ReviewedPack,
    load_default_pack,
    load_default_packs,
    load_pack,
    pack_from_dict,
)
from thymira.schemas import (
    ActivityProfile,
    Actor,
    ControlEvaluationStatus,
    Evidence,
    PackBinding,
    RiskAssessment,
    RiskLevel,
    Run,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
SHA = "a" * 64


def _profile(
    *,
    version: int = 1,
    run_id: str | None = None,
    data_categories: tuple[str, ...] = ("credit_history",),
    sensitive_attributes: tuple[str, ...] = (),
    potential_consequences: tuple[str, ...] = ("Delayed review of a credit application.",),
) -> ActivityProfile:
    """Build a valid activity profile with targeted preflight facts."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=version,
        run_id=run_id or new_id("run"),
        purpose="Prioritise credit applications for human review.",
        affected_population="Credit applicants.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=data_categories,
        sensitive_attributes=sensitive_attributes,
        potential_consequences=potential_consequences,
        evidence_refs=(Evidence(kind="event", ref="seq:0", sha256=SHA),),
    )


def _packs() -> dict[str, ReviewedPack]:
    """Return the two reviewed preflight packs by stable name."""
    return {pack.name: pack for pack in load_default_packs()}


def _risk(profile: ActivityProfile) -> RiskAssessment:
    """Evaluate a profile with the reviewed base-risk method."""
    method = _packs()["methodology-base"].risk_method
    assert method is not None
    return BaseRiskEvaluator(method, Actor.system()).evaluate(
        profile, assessment_id=new_id("assessment"), assessed_at=NOW
    )


def test_base_risk_reports_unknown_when_profile_facts_are_missing() -> None:
    assessment = _risk(_profile(data_categories=(), potential_consequences=()))

    assert assessment.risk_level is RiskLevel.UNKNOWN
    assert assessment.missing_information == ("data_categories", "potential_consequences")
    assert assessment.confidence == 0.0


def test_base_risk_rejects_undeclared_placeholders_as_profile_evidence() -> None:
    """A conservative placeholder must not permit MIRA to infer a risk level."""
    assessment = _risk(
        _profile(
            data_categories=("undeclared",),
            potential_consequences=("not specified",),
        )
    )

    assert assessment.risk_level is RiskLevel.UNKNOWN
    assert assessment.missing_information == ("data_categories", "potential_consequences")


@pytest.mark.parametrize(
    ("sensitive_attributes", "expected_level", "expected_category"),
    [
        ((), RiskLevel.LOW, "declared_no_sensitive_data"),
        (("financial_vulnerability",), RiskLevel.MEDIUM, "sensitive_data"),
        (("disability",), RiskLevel.HIGH, "special_category_data"),
    ],
)
def test_base_risk_uses_explicit_reviewed_categories(
    sensitive_attributes: tuple[str, ...], expected_level: RiskLevel, expected_category: str
) -> None:
    assessment = _risk(_profile(sensitive_attributes=sensitive_attributes))

    assert assessment.risk_level is expected_level
    assert assessment.activity_category == expected_category
    assert assessment.missing_information == ()
    assert assessment.justification.startswith("Matched reviewed base-risk rule")


def test_applicability_resolver_binds_applicable_and_nonapplicable_packs() -> None:
    profile = _profile()
    resolver = ApplicabilityResolver()
    packs = _packs()

    applicable = resolver.resolve(
        profile,
        packs["credit-governance"],
        binding_id=new_id("binding"),
        evaluated_as_of=NOW,
    )
    not_applicable = resolver.resolve(
        _profile(data_categories=("income",)),
        packs["credit-governance"],
        binding_id=new_id("binding"),
        evaluated_as_of=NOW,
    )

    assert applicable.applicable is True
    assert "data category credit_history is declared" in applicable.activating_facts
    assert not_applicable.applicable is False
    assert "no required data category is declared" in not_applicable.activating_facts


def test_applicability_pins_profile_and_pack_versions_and_date() -> None:
    pack = _packs()["credit-governance"]
    profile = _profile(version=2)

    binding = ApplicabilityResolver().resolve(
        profile,
        pack.model_copy(update={"version": "2.0", "applicability_rules_version": "2.0"}),
        binding_id=new_id("binding"),
        evaluated_as_of=NOW,
    )

    assert binding.activity_profile_version == 2
    assert binding.pack_version == "2.0"
    assert binding.applicability_rules_version == "2.0"
    assert binding.evaluated_as_of == NOW


def _binding_and_control() -> tuple[PackBinding, ReviewedPack, PackControl]:
    """Return an applicable methodology binding and its declarative evidence control."""
    profile = _profile()
    pack = _packs()["methodology-base"]
    binding = ApplicabilityResolver().resolve(
        profile, pack, binding_id=new_id("binding"), evaluated_as_of=NOW
    )
    return binding, pack, pack.controls[0]


def _observation(
    *, integrity_verified: bool | None = True, observed_at: datetime = NOW
) -> EvidenceObservation:
    """Build event evidence with controllable integrity and age."""
    return EvidenceObservation(
        evidence=Evidence(kind="event", ref="seq:1", sha256=SHA),
        integrity_verified=integrity_verified,
        observed_at=observed_at,
    )


@pytest.mark.parametrize(
    ("observations", "expected_status"),
    [
        ((_observation(),), ControlEvaluationStatus.SATISFIED),
        ((), ControlEvaluationStatus.MISSING_EVIDENCE),
        (
            (_observation(observed_at=NOW - timedelta(days=31)),),
            ControlEvaluationStatus.STALE_EVIDENCE,
        ),
        ((_observation(integrity_verified=None),), ControlEvaluationStatus.UNVERIFIED),
        ((_observation(integrity_verified=False),), ControlEvaluationStatus.FAILED),
    ],
)
def test_control_runner_checks_presence_integrity_and_freshness(
    observations: tuple[EvidenceObservation, ...], expected_status: ControlEvaluationStatus
) -> None:
    binding, pack, control = _binding_and_control()

    evaluation = GenericPackControlRunner().evaluate(
        binding,
        pack,
        control,
        observations,
        evaluation_id=new_id("control"),
        evaluated_at=NOW,
    )

    assert evaluation.status is expected_status


def _pack_with_a_control_requiring_nothing(
    pack: ReviewedPack,
) -> tuple[ReviewedPack, PackControl]:
    """Add a control that requires no evidence kinds, bypassing field validation."""
    control = pack.controls[0].model_copy(
        update={"id": "BROKEN-EVIDENCE-001", "required_evidence_kinds": ()}
    )
    return pack.model_copy(update={"controls": (*pack.controls, control)}), control


def test_pack_control_requires_at_least_one_evidence_kind() -> None:
    with pytest.raises(ValidationError, match="required_evidence_kinds"):
        PackControl.model_validate(
            {
                "id": "BROKEN-EVIDENCE-001",
                "title": "A control that requires no evidence at all",
                "max_age_days": 30,
            }
        )
    with pytest.raises(ValidationError, match="required_evidence_kinds"):
        PackControl(
            id="BROKEN-EVIDENCE-001",
            title="A control that requires no evidence at all",
            required_evidence_kinds=(),
            max_age_days=30,
        )


def test_loading_a_pack_whose_control_requires_no_evidence_names_the_field() -> None:
    data = {
        "id": "pack_000000000000000000000000000000ff",
        "name": "broken-pack",
        "version": "1.0",
        "applicability_rules_version": "1.0",
        "controls": [
            {
                "id": "BROKEN-EVIDENCE-001",
                "title": "A control that requires no evidence at all",
                "max_age_days": 30,
            }
        ],
    }

    with pytest.raises(ValidationError, match="required_evidence_kinds"):
        pack_from_dict(data)


def test_control_runner_never_satisfies_a_control_that_requires_no_evidence() -> None:
    binding, pack, _ = _binding_and_control()
    pack, control = _pack_with_a_control_requiring_nothing(pack)

    evaluation = GenericPackControlRunner().evaluate(
        binding,
        pack,
        control,
        (_observation(),),
        evaluation_id=new_id("control"),
        evaluated_at=NOW,
    )

    assert evaluation.status is ControlEvaluationStatus.REQUIRES_HUMAN_REVIEW
    assert evaluation.evidence == ()


def test_governance_preflight_does_not_evaluate_evidence_controls() -> None:
    """Risk and applicability are available before evidence-producing work starts."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Audit the credit-risk evidence.",
    )
    snapshot = MiraAuditSnapshot(
        run=run,
        activity_profile=_profile(run_id=run.id),
        audited_at=NOW,
    )

    result = MiraAuditOrchestrator(load_default_packs(), Actor.system()).run_governance_preflight(
        snapshot
    )

    assert result.risk_assessment.risk_level is RiskLevel.LOW
    assert len(result.pack_bindings) == 2
    assert result.audit_findings == ()


def test_evidence_audit_reports_a_malformed_control_without_aborting() -> None:
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Audit the credit-risk evidence.",
    )
    pack, control = _pack_with_a_control_requiring_nothing(_packs()["methodology-base"])
    snapshot = MiraAuditSnapshot(
        run=run,
        activity_profile=_profile(run_id=run.id),
        evidence_observations=(_observation(),),
        audited_at=NOW,
    )

    orchestrator = MiraAuditOrchestrator((pack,), Actor.system())
    preflight = orchestrator.run_governance_preflight(snapshot)
    result = orchestrator.run_evidence_audit(snapshot, preflight)

    statuses = {
        evaluation.control_id: evaluation.status for evaluation in result.control_evaluations
    }
    assert statuses["METHODOLOGY-EVIDENCE-001"] is ControlEvaluationStatus.SATISFIED
    assert statuses[control.id] is ControlEvaluationStatus.REQUIRES_HUMAN_REVIEW
    assert [finding.control_id for finding in result.audit_findings] == [control.id]


def test_preflight_evaluations_are_reproducible_given_fixed_inputs() -> None:
    profile = _profile()
    method = _packs()["methodology-base"].risk_method
    assert method is not None
    evaluator = BaseRiskEvaluator(method, Actor.system())
    assessment_id = new_id("assessment")

    first = evaluator.evaluate(profile, assessment_id=assessment_id, assessed_at=NOW)
    second = evaluator.evaluate(profile, assessment_id=assessment_id, assessed_at=NOW)

    assert first == second


# ------------------------------------------------- loading reviewed packs from local data


def test_load_pack_reads_one_reviewed_pack_from_a_json_file(tmp_path: Path) -> None:
    """A pack on disk is validated by the same contract as a packaged one."""
    packaged = _packs()["methodology-base"]
    path = tmp_path / "methodology-base.json"
    path.write_text(
        json.dumps(packaged.model_dump(mode="json")),
        encoding="utf-8",
        newline="\n",
    )

    assert load_pack(path) == packaged


def test_load_pack_rejects_a_file_that_is_not_json(tmp_path: Path) -> None:
    """Only reviewed JSON pack files are loadable; the extension is checked first."""
    path = tmp_path / "pack.yaml"
    path.write_text("name: methodology-base\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="unsupported pack file type"):
        load_pack(path)


def test_load_pack_rejects_json_that_is_not_a_mapping(tmp_path: Path) -> None:
    """A pack file must carry one mapping; a list is not a reviewed pack."""
    path = tmp_path / "pack.json"
    path.write_text("[]", encoding="utf-8", newline="\n")

    with pytest.raises(TypeError, match="mapping at the top level"):
        load_pack(path)


def test_load_default_pack_reports_an_unknown_packaged_name() -> None:
    """Only the reviewed packs shipped with MIRA can be loaded by name."""
    with pytest.raises(FileNotFoundError, match="no packaged MIRA pack"):
        load_default_pack("not-a-reviewed-pack")


def test_load_default_pack_loads_each_packaged_reviewed_pack() -> None:
    """The bounded default set is exactly the two packaged reviewed packs."""
    assert load_default_pack("methodology-base") == _packs()["methodology-base"]
    assert load_default_pack("credit-governance") == _packs()["credit-governance"]
