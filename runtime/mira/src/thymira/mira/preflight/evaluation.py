"""Pure deterministic evaluators for MIRA preflight evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from thymira.mira.preflight.risk_model import (
    MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX,
    RISK_CONFIDENCE_THRESHOLD,
    RiskAssessmentModelContext,
    RiskJudgment,
    classify_risk,
)
from thymira.schemas import (
    ActivityProfile,
    Actor,
    ControlEvaluation,
    ControlEvaluationStatus,
    Evidence,
    PackBinding,
    RiskAssessment,
    RiskLevel,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.mira.preflight.models import (
        BaseRiskMethod,
        EvidenceObservation,
        PackControl,
        ReviewedPack,
    )


class BaseRiskEvaluator:
    """Apply one reviewed base-risk method without considering controls or mitigations."""

    def __init__(
        self,
        method: BaseRiskMethod,
        assessor: Actor,
        *,
        model_context: RiskAssessmentModelContext | None = None,
    ) -> None:
        self._method = method
        self._assessor = assessor
        self._model_context = model_context

    def evaluate(
        self,
        profile: ActivityProfile,
        *,
        assessment_id: str,
        assessed_at: datetime,
    ) -> RiskAssessment:
        """Return inherent risk for one exact profile version.

        Missing required profile facts produce an unknown assessment instead of a guessed risk.
        """
        missing = tuple(
            fact
            for fact in self._method.required_profile_facts
            if not _has_profile_fact(profile, fact)
        )
        if missing:
            return _assessment(
                profile,
                self._method,
                self._assessor,
                assessment_id,
                assessed_at,
                risk_level=RiskLevel.UNKNOWN,
                activity_category="unknown",
                confidence=0.0,
                missing_information=missing,
                justification=(
                    "Required profile facts are missing; inherent risk cannot be classified."
                ),
            )
        judgment = classify_risk(profile, self._method, self._model_context)
        if judgment is not None:
            model_assessment = _model_assessment(
                profile,
                self._method,
                self._assessor,
                assessment_id,
                assessed_at,
                judgment,
            )
            if model_assessment is not None:
                return model_assessment
        for rule in self._method.rules:
            if _matches_risk_rule(
                profile, rule.sensitive_attributes_any, rule.requires_no_sensitive_attributes
            ):
                return _assessment(
                    profile,
                    self._method,
                    self._assessor,
                    assessment_id,
                    assessed_at,
                    risk_level=rule.risk_level,
                    activity_category=rule.category,
                    confidence=1.0,
                    justification=f"Matched reviewed base-risk rule {rule.id}.",
                )
        return _assessment(
            profile,
            self._method,
            self._assessor,
            assessment_id,
            assessed_at,
            risk_level=RiskLevel.UNKNOWN,
            activity_category="unknown",
            confidence=0.0,
            missing_information=("sensitive_attributes",),
            justification=("No reviewed base-risk rule matches the declared sensitive attributes."),
        )


def _model_assessment(
    profile: ActivityProfile,
    method: BaseRiskMethod,
    assessor: Actor,
    assessment_id: str,
    assessed_at: datetime,
    judgment: RiskJudgment,
) -> RiskAssessment | None:
    """Convert a valid model judgement, retaining uncertainty as an UNKNOWN assessment."""
    allowed_pairs = {(rule.risk_level, rule.category) for rule in method.rules}
    missing_information = tuple(
        fact for fact in judgment.missing_information if fact in _MODEL_PROFILE_FACTS
    )
    uncertain = (
        judgment.risk_level is RiskLevel.UNKNOWN
        or bool(judgment.missing_information)
        or judgment.confidence < RISK_CONFIDENCE_THRESHOLD
    )
    if uncertain:
        missing = missing_information or ("risk_classification",)
        confidence = min(judgment.confidence, RISK_CONFIDENCE_THRESHOLD - 0.01)
        return _assessment(
            profile,
            method,
            assessor,
            assessment_id,
            assessed_at,
            risk_level=RiskLevel.UNKNOWN,
            activity_category="unknown",
            confidence=confidence,
            missing_information=missing,
            justification=(
                f"{MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX} with sufficient confidence: "
                f"{judgment.justification}"
            ),
        )
    if (judgment.risk_level, judgment.activity_category) not in allowed_pairs:
        return None
    return _assessment(
        profile,
        method,
        assessor,
        assessment_id,
        assessed_at,
        risk_level=judgment.risk_level,
        activity_category=judgment.activity_category,
        confidence=judgment.confidence,
        justification=(
            "MIRA's model selected reviewed risk category "
            f"{judgment.activity_category}: {judgment.justification}"
        ),
    )


class ApplicabilityResolver:
    """Bind a reviewed pack to one profile version through direct factual rules."""

    def resolve(
        self,
        profile: ActivityProfile,
        pack: ReviewedPack,
        *,
        binding_id: str,
        evaluated_as_of: datetime,
    ) -> PackBinding:
        """Return a reproducible applicability binding and its activating or rejecting facts."""
        facts: list[str] = []
        applicable = True
        if "*" not in pack.jurisdictions:
            if profile.jurisdiction not in pack.jurisdictions:
                applicable = False
                facts.append(f"jurisdiction {profile.jurisdiction} is not covered")
            else:
                facts.append(f"jurisdiction {profile.jurisdiction} is covered")
        else:
            facts.append("all jurisdictions are covered")
        if pack.required_any_data_categories:
            matched = sorted(set(profile.data_categories) & set(pack.required_any_data_categories))
            if not matched:
                applicable = False
                facts.append("no required data category is declared")
            else:
                facts.extend(f"data category {category} is declared" for category in matched)
        rationale = (
            f"Pack {pack.name}@{pack.version} applies to the declared profile facts."
            if applicable
            else (f"Pack {pack.name}@{pack.version} does not apply to the declared profile facts.")
        )
        return PackBinding(
            id=binding_id,
            activity_profile_id=profile.id,
            activity_profile_version=profile.version,
            pack_id=pack.id,
            pack_version=pack.version,
            applicability_rules_version=pack.applicability_rules_version,
            evaluated_as_of=evaluated_as_of,
            jurisdiction=profile.jurisdiction,
            applicable=applicable,
            rationale=rationale,
            activating_facts=tuple(facts),
            evidence_refs=profile.evidence_refs,
        )


class GenericPackControlRunner:
    """Evaluate simple reviewed evidence controls; it never makes an authorization decision."""

    def evaluate(
        self,
        binding: PackBinding,
        pack: ReviewedPack,
        control: PackControl,
        observations: Sequence[EvidenceObservation],
        *,
        evaluation_id: str,
        evaluated_at: datetime,
    ) -> ControlEvaluation:
        """Evaluate one applicable pack control for evidence presence, integrity, and freshness.

        A control declaring no required evidence kinds cannot be evaluated and is never satisfied:
        it is reported as ``REQUIRES_HUMAN_REVIEW`` so one malformed control becomes a finding
        instead of aborting the audit of every other control and pack.
        """
        if not binding.applicable:
            raise ValueError("cannot evaluate controls for an inapplicable pack")
        if binding.pack_id != pack.id or binding.pack_version != pack.version:
            raise ValueError("binding does not match the reviewed pack version")
        if control not in pack.controls:
            raise ValueError("control is not declared by the reviewed pack")
        selected = tuple(
            observation
            for observation in observations
            if observation.evidence.kind in control.required_evidence_kinds
        )
        present_kinds = {observation.evidence.kind for observation in selected}
        required_kinds = set(control.required_evidence_kinds)
        if not required_kinds:
            status = ControlEvaluationStatus.REQUIRES_HUMAN_REVIEW
            evidence: tuple[Evidence, ...] = ()
        elif not required_kinds.issubset(present_kinds):
            status = ControlEvaluationStatus.MISSING_EVIDENCE
            evidence = ()
        elif any(observation.integrity_verified is False for observation in selected):
            status = ControlEvaluationStatus.FAILED
            evidence = tuple(observation.evidence for observation in selected)
        elif any(
            observation.integrity_verified is None or observation.evidence.sha256 is None
            for observation in selected
        ):
            status = ControlEvaluationStatus.UNVERIFIED
            evidence = tuple(observation.evidence for observation in selected)
        elif any(
            observation.observed_at < evaluated_at - timedelta(days=control.max_age_days)
            for observation in selected
        ):
            status = ControlEvaluationStatus.STALE_EVIDENCE
            evidence = tuple(observation.evidence for observation in selected)
        else:
            status = ControlEvaluationStatus.SATISFIED
            evidence = tuple(observation.evidence for observation in selected)
        return ControlEvaluation(
            id=evaluation_id,
            pack_binding_id=binding.id,
            pack_id=pack.id,
            pack_version=pack.version,
            control_id=control.id,
            status=status,
            evidence=evidence,
            evaluated_at=evaluated_at,
        )


def _assessment(
    profile: ActivityProfile,
    method: BaseRiskMethod,
    assessor: Actor,
    assessment_id: str,
    assessed_at: datetime,
    *,
    risk_level: RiskLevel,
    activity_category: str,
    confidence: float,
    justification: str,
    missing_information: tuple[str, ...] = (),
) -> RiskAssessment:
    """Construct one complete inherent-risk record without control or mitigation fields."""
    return RiskAssessment(
        id=assessment_id,
        run_id=profile.run_id,
        activity_profile_id=profile.id,
        activity_profile_version=profile.version,
        assessor=assessor,
        subject_kind="activity",
        subject_id=profile.activity_id,
        assessed_at=assessed_at,
        risk_level=risk_level,
        activity_category=activity_category,
        confidence=confidence,
        missing_information=missing_information,
        justification=justification,
        evidence=profile.evidence_refs,
        assessment_method=method.id,
        assessment_method_version=method.version,
    )


_UNDECLARED_PROFILE_VALUES = frozenset(
    {
        "",
        "undeclared",
        "unknown",
        "not declared",
        "not specified",
        "unspecified",
        "not provided",
        "tbd",
        "n/a",
        "na",
        "no declarado",
        "desconocido",
        "sin declarar",
        "sin especificar",
    }
)

_MODEL_PROFILE_FACTS = frozenset(
    {
        "purpose",
        "affected_population",
        "decision_effect",
        "autonomy",
        "human_oversight",
        "jurisdiction",
        "data_categories",
        "sensitive_attributes",
        "potential_consequences",
    }
)


def _has_profile_fact(profile: ActivityProfile, fact: str) -> bool:
    """Return whether a reviewed profile fact contains declared evidence."""
    value = getattr(profile, fact, ())
    if isinstance(value, str):
        return _is_declared_profile_value(value)
    return bool(value) and any(_is_declared_profile_value(item) for item in value)


def _is_declared_profile_value(value: str) -> bool:
    """Reject placeholders so conservative profiles cannot look evidenced."""
    return value.strip().casefold() not in _UNDECLARED_PROFILE_VALUES


def _matches_risk_rule(
    profile: ActivityProfile,
    sensitive_attributes_any: tuple[str, ...],
    requires_no_sensitive_attributes: bool,
) -> bool:
    """Match one fixed sensitive-attribute rule without scoring or text interpretation."""
    if requires_no_sensitive_attributes:
        return not profile.sensitive_attributes
    if "*" in sensitive_attributes_any:
        return bool(profile.sensitive_attributes)
    return bool(set(profile.sensitive_attributes) & set(sensitive_attributes_any))
