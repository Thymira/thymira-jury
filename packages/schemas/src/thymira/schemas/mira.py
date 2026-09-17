"""Minimal immutable context, applicability, and control contracts for MIRA."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from thymira.schemas.audit import Evidence
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.ids import Id

_MAX_CONTEXT_JSON_LENGTH = 12_000


class ControlEvaluationStatus(StrEnum):
    """Closed outcome vocabulary for one control evaluated against its evidence."""

    SATISFIED = "satisfied"
    FAILED = "failed"
    MISSING_EVIDENCE = "missing_evidence"
    STALE_EVIDENCE = "stale_evidence"
    UNVERIFIED = "unverified"
    REQUIRES_HUMAN_REVIEW = "requires_human_review"


class ActivityProfile(ThymiraModel):
    """One immutable, versioned description of an activity under MIRA review."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: Id
    activity_id: Id
    version: int = Field(ge=1)
    project_id: Id | None = None
    run_id: Id | None = None
    purpose: str = Field(min_length=1)
    affected_population: str = Field(min_length=1)
    decision_effect: str = Field(min_length=1)
    autonomy: str = Field(min_length=1)
    human_oversight: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    data_categories: tuple[str, ...] = ()
    sensitive_attributes: tuple[str, ...] = ()
    potential_consequences: tuple[str, ...] = ()
    evidence_refs: tuple[Evidence, ...] = ()
    supersedes_id: Id | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_references(self) -> ActivityProfile:
        """Require one owner and a non-cyclic, incremented supersession when present."""
        if (self.project_id is None) == (self.run_id is None):
            raise ValueError("exactly one of project_id or run_id is required")
        if self.supersedes_id == self.id:
            raise ValueError("supersedes_id cannot equal id")
        if self.supersedes_id is not None and self.version == 1:
            raise ValueError("a superseding profile requires version greater than 1")
        return self


class PackBinding(ThymiraModel):
    """A reproducible determination that one reviewed pack applies to a profile version."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: Id
    activity_profile_id: Id
    activity_profile_version: int = Field(ge=1)
    pack_id: Id
    pack_version: str = Field(min_length=1)
    applicability_rules_version: str = Field(min_length=1)
    evaluated_as_of: datetime = Field(default_factory=utc_now)
    jurisdiction: str = Field(min_length=1)
    applicable: bool
    rationale: str = Field(min_length=1)
    activating_facts: tuple[str, ...] = ()
    evidence_refs: tuple[Evidence, ...] = ()
    supersedes_id: Id | None = None

    @model_validator(mode="after")
    def validate_supersession(self) -> PackBinding:
        """Reject a binding that claims to supersede itself."""
        if self.supersedes_id == self.id:
            raise ValueError("supersedes_id cannot equal id")
        return self


class ControlEvaluation(ThymiraModel):
    """An immutable evidence-based result for one control in an applicable pack version."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: Id
    pack_binding_id: Id
    pack_id: Id
    pack_version: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    status: ControlEvaluationStatus
    evidence: tuple[Evidence, ...] = ()
    evaluated_at: datetime = Field(default_factory=utc_now)
    supersedes_id: Id | None = None

    @model_validator(mode="after")
    def validate_evidence_and_supersession(self) -> ControlEvaluation:
        """Keep evidence-state combinations internally meaningful and non-cyclic."""
        if self.supersedes_id == self.id:
            raise ValueError("supersedes_id cannot equal id")
        if self.status is ControlEvaluationStatus.MISSING_EVIDENCE and self.evidence:
            raise ValueError("missing_evidence status cannot include evidence")
        evidence_required = {
            ControlEvaluationStatus.SATISFIED,
            ControlEvaluationStatus.FAILED,
            ControlEvaluationStatus.STALE_EVIDENCE,
            ControlEvaluationStatus.UNVERIFIED,
        }
        if self.status in evidence_required and not self.evidence:
            raise ValueError(f"{self.status.value} status requires evidence")
        return self


class DecisionContextStatement(ThymiraModel):
    """One bounded context assertion tied to evidence or explicitly marked as unknown."""

    text: str = Field(min_length=1, max_length=400)
    evidence_refs: tuple[Evidence, ...] = Field(default=(), max_length=8)
    unknown: bool = False

    @model_validator(mode="after")
    def validate_grounding(self) -> DecisionContextStatement:
        """Require evidence for a claim unless MIRA explicitly does not know it."""
        if not self.evidence_refs and not self.unknown:
            raise ValueError("a context statement requires evidence_refs or unknown=true")
        forbidden_markers = ("chain-of-thought", "chain of thought", "<think>")
        if any(marker in self.text.lower() for marker in forbidden_markers):
            raise ValueError("context statements must not contain chain-of-thought")
        return self


class DecisionContext(ThymiraModel):
    """An immutable, bounded MIRA context snapshot; it is evidence, never an instruction."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: Id
    run_id: Id
    summary: DecisionContextStatement
    relevant_facts: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    active_constraints: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    current_risks: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    evidence_available: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    evidence_gaps: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    unresolved_questions: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    upcoming_checkpoints: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    evidence_refs: tuple[Evidence, ...] = Field(default=(), max_length=40)
    based_on_run_version: int = Field(ge=1)
    based_on_event_seq: int = Field(ge=0)
    based_on_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_statement_evidence(self) -> DecisionContext:
        """Ensure every cited statement reference is listed in the context evidence set."""
        evidence = set(self.evidence_refs)
        statements = (
            self.summary,
            *self.relevant_facts,
            *self.active_constraints,
            *self.current_risks,
            *self.evidence_available,
            *self.evidence_gaps,
            *self.unresolved_questions,
            *self.upcoming_checkpoints,
        )
        if any(
            reference not in evidence
            for statement in statements
            for reference in statement.evidence_refs
        ):
            raise ValueError("every statement evidence reference must be listed in evidence_refs")
        if len(self.model_dump_json()) > _MAX_CONTEXT_JSON_LENGTH:
            raise ValueError("decision context exceeds the 12000-character limit")
        return self
