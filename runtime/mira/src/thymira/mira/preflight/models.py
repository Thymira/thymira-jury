"""Small, reviewed data definitions for deterministic MIRA preflight packs."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from thymira.schemas import Evidence, RiskLevel, ThymiraModel

_PROFILE_FACTS = frozenset({"data_categories", "potential_consequences"})


class RiskRule(ThymiraModel):
    """One ordered inherent-risk category rule based on declared sensitive attributes."""

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    risk_level: RiskLevel
    sensitive_attributes_any: tuple[str, ...] = ()
    requires_no_sensitive_attributes: bool = False

    @model_validator(mode="after")
    def validate_matcher(self) -> RiskRule:
        """Require exactly one simple sensitive-attribute matcher."""
        if self.requires_no_sensitive_attributes == bool(self.sensitive_attributes_any):
            raise ValueError("risk rule requires exactly one sensitive-attribute matcher")
        return self


class BaseRiskMethod(ThymiraModel):
    """Versioned, reviewed input requirements and ordered risk-category rules."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    required_profile_facts: tuple[str, ...] = ()
    rules: tuple[RiskRule, ...] = ()

    @model_validator(mode="after")
    def validate_required_facts(self) -> BaseRiskMethod:
        """Reject a pack that names profile facts this small evaluator does not understand."""
        unknown = set(self.required_profile_facts) - _PROFILE_FACTS
        if unknown:
            raise ValueError(f"unknown required profile facts: {sorted(unknown)}")
        return self


class PackControl(ThymiraModel):
    """One declarative control requiring fresh, integrity-checked evidence of given kinds.

    ``required_evidence_kinds`` is mandatory and non-empty: a control that requires no evidence
    cannot be evaluated, so a pack declaring one is rejected here, at load time, rather than
    producing an unevaluable control in the middle of an audit.
    """

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    required_evidence_kinds: tuple[str, ...] = Field(min_length=1)
    max_age_days: int = Field(gt=0)


class ReviewedPack(ThymiraModel):
    """A fixed, versioned MIRA pack with simple applicability and evidence controls."""

    id: str = Field(pattern=r"^pack_[0-9a-f]{32}$")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    applicability_rules_version: str = Field(min_length=1)
    jurisdictions: tuple[str, ...] = ("*",)
    required_any_data_categories: tuple[str, ...] = ()
    risk_method: BaseRiskMethod | None = None
    controls: tuple[PackControl, ...] = ()


class EvidenceObservation(ThymiraModel):
    """The independently observed integrity and age of one evidence reference.

    ``integrity_verified`` is ``True`` when an external verifier confirmed the referenced hash,
    ``False`` when it found a mismatch, and ``None`` when no such verification is available.
    """

    evidence: Evidence
    integrity_verified: bool | None = None
    observed_at: datetime
