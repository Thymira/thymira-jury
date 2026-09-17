"""Versioned risk assessments: evidence for policy, never an authorization."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from thymira.schemas.actor import Actor
from thymira.schemas.audit import Evidence
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.ids import Id


class RiskLevel(StrEnum):
    """Risk level stated by an assessment."""

    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskAssessment(ThymiraModel):
    """An immutable assessment of inherent risk for one activity-profile version.

    Mitigations and control outcomes are intentionally absent. They belong to their own evidence
    records and must not lower the inherent-risk result represented here.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: Id
    run_id: Id | None = None
    activity_profile_id: Id
    activity_profile_version: int = Field(ge=1)
    assessor: Actor
    subject_kind: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    schema_version: str = Field(default="0.4", min_length=1)
    assessed_at: datetime = Field(default_factory=utc_now)
    risk_level: RiskLevel
    activity_category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: tuple[str, ...] = ()
    justification: str = Field(min_length=1)
    evidence: tuple[Evidence, ...] = ()
    assessment_method: str = Field(min_length=1)
    assessment_method_version: str = Field(min_length=1)
    supersedes_id: Id | None = None

    @model_validator(mode="after")
    def validate_supersession(self) -> RiskAssessment:
        """Reject a record that claims to supersede itself."""
        if self.supersedes_id == self.id:
            raise ValueError("supersedes_id cannot equal id")
        return self
