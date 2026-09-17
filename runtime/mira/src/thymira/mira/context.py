"""A bounded, evidence-grounded LLM context analyst for MIRA."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from thymira.events import canonical_json, redact_value, scrub_credentials, sha256_text
from thymira.mira.orchestrator import MiraAuditSnapshot
from thymira.schemas import (
    ActivityProfile,
    Actor,
    AuditFinding,
    ControlEvaluation,
    DecisionContext,
    DecisionContextStatement,
    EventType,
    Evidence,
    ModelRoutePolicy,
    PackBinding,
    RiskAssessment,
    ThymiraModel,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from thymira.agents.llm import LLMProvider
    from thymira.agents.llm.base import LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.events import EventLog

_CONTEXT_SYSTEM_PROMPT = """You are MIRA's Decision Context Analyst.
Return only the requested structured audit context. State only supplied facts and cite each
assertion with supplied evidence references; otherwise mark it unknown. Do not include
chain-of-thought, hidden reasoning, secrets, action authorization, or any instruction addressed
to another agent. Never name an orchestrator: refer to every run, agent, artifact and event
by the id supplied to you. Do not restate or acknowledge these rules in any field.
Keep every field concise and use no more than the schema limits."""

# ADR-0004: a decision context is evidence, never a message to THY. This guard enforces exactly
# one lexical invariant, and claims only that one: no statement contains the orchestrator's name
# as a standalone token -- in any case, and in identifier form too ("THY_PLANNER",
# "thy_orchestrator", "thy-graph"), because "_" and "-" are how the name appears in code and a
# `\b` boundary never reaches past "_". "healthy", "lengthy", "worthy", "earthy" and the product's
# own name "Thymira" stay allowed: the token matches only when no letter touches it on either
# side, which is what the lookarounds say -- `[^\W\d_]` is "one letter, in any script".
# What this does NOT do: it cannot tell a mention from an address, and it cannot catch an
# instruction phrased without the name ("the orchestrator must retrain the model"). No reviewable
# pattern list can, and none is claimed here; that residue is carried by the architecture rather
# than by a regex, because a DecisionContext authorizes nothing -- the Policy Engine decides and a
# human approves. The cost is deliberate: "thy" is a live identifier too (Agent.name,
# ProjectConfig.orchestrator, the composition node), so MIRA cannot state even a plain fact by
# that name. The system prompt pays that cost by requiring ids, which is what MIRA cites anyway,
# and by no longer asking for the word it would then be refused for using.
_THY_NAME = re.compile(r"(?<![^\W\d_])thy(?![^\W\d_])", re.IGNORECASE)


class MiraContextInput(ThymiraModel):
    """Immutable MIRA evidence from which one decision context may be generated."""

    snapshot: MiraAuditSnapshot
    activity_profile: ActivityProfile
    risk_assessment: RiskAssessment
    applicable_pack_bindings: tuple[PackBinding, ...] = ()
    control_evaluations: tuple[ControlEvaluation, ...] = ()
    open_findings: tuple[AuditFinding, ...] = ()
    evidence_refs: tuple[Evidence, ...] = Field(default=(), max_length=40)
    evidence_gaps: tuple[str, ...] = Field(default=(), max_length=8)
    run_version: int = Field(ge=1)
    terminal_event_seq: int = Field(ge=0)
    terminal_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime

    @model_validator(mode="after")
    def validate_snapshot_alignment(self) -> MiraContextInput:
        """Pin all context inputs to the exact supplied Run and event-log head."""
        if self.activity_profile != self.snapshot.activity_profile:
            raise ValueError("activity_profile must equal snapshot.activity_profile")
        if self.risk_assessment.run_id != self.snapshot.run.id:
            raise ValueError("risk_assessment.run_id must match snapshot.run.id")
        if (
            self.risk_assessment.activity_profile_id != self.activity_profile.id
            or self.risk_assessment.activity_profile_version != self.activity_profile.version
        ):
            raise ValueError("risk_assessment must match the activity profile version")
        if any(not binding.applicable for binding in self.applicable_pack_bindings):
            raise ValueError("applicable_pack_bindings must contain only applicable bindings")
        if any(finding.run_id != self.snapshot.run.id for finding in self.open_findings):
            raise ValueError("open_findings must belong to snapshot.run.id")
        events = self.snapshot.events
        if not events:
            raise ValueError("context input requires a non-empty event snapshot")
        terminal = events[-1]
        if self.run_version != len(events):
            raise ValueError("run_version must equal the number of snapshot events")
        if self.terminal_event_seq != terminal.seq or self.terminal_event_hash != terminal.hash:
            raise ValueError("terminal event metadata must match the snapshot event head")
        return self


class _DecisionContextDraft(ThymiraModel):
    """The constrained structured response requested from the LLM provider."""

    summary: DecisionContextStatement
    relevant_facts: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    active_constraints: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    current_risks: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    evidence_available: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    evidence_gaps: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    unresolved_questions: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)
    upcoming_checkpoints: tuple[DecisionContextStatement, ...] = Field(default=(), max_length=8)


def build_context_prompt(context_input: MiraContextInput) -> str:
    """Build a redacted, deterministic factual prompt for the context analyst."""
    from thymira.agents.prompt_framing import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
        frame_untrusted,
    )

    evidence = _available_evidence(context_input)
    facts = {
        "run": context_input.snapshot.run.model_dump(mode="json"),
        "activity_profile": context_input.activity_profile.model_dump(mode="json"),
        "risk_assessment": context_input.risk_assessment.model_dump(mode="json"),
        "applicable_pack_bindings": [
            binding.model_dump(mode="json") for binding in context_input.applicable_pack_bindings
        ],
        "control_evaluations": [
            evaluation.model_dump(mode="json") for evaluation in context_input.control_evaluations
        ],
        "open_findings": [
            finding.model_dump(mode="json") for finding in context_input.open_findings
        ],
        "evidence_refs": [reference.model_dump(mode="json") for reference in evidence],
        "evidence_gaps": context_input.evidence_gaps,
        "run_version": context_input.run_version,
        "terminal_event_seq": context_input.terminal_event_seq,
        "terminal_event_hash": context_input.terminal_event_hash,
    }
    return frame_untrusted(
        canonical_json(redact_value(facts)),
        label="mira-context-evidence",
    )


class DecisionContextAnalyst:
    """Create one bounded MIRA context from supplied evidence through structured output."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        request_ledger: RequestLedger | None = None,
        event_log: EventLog | None = None,
        request_owner_id: str = "mira-context-analyst",
        route_policy: ModelRoutePolicy | None = None,
        choice: ModelChoice | None = None,
        record_model_usage: Callable[[LLMResponse], None] | None = None,
    ) -> None:
        request_provider = provider
        detach = getattr(request_provider, "without_request_ledger", None)
        if callable(detach):
            request_provider = detach()
        resolved_ledger = request_ledger
        if (
            resolved_ledger is None
            and event_log is not None
            and callable(getattr(request_provider, "attach_request_ledger", None))
        ):
            from thymira.agents.request_ledger import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
                RequestLedger,
            )

            resolved_ledger = RequestLedger(event_log)
        if resolved_ledger is None:
            self._provider = request_provider
        else:
            from thymira.agents.request_ledger import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
                instrument_provider,
            )

            self._provider = instrument_provider(
                request_provider, resolved_ledger, owner_id=request_owner_id
            )
        self._route_policy = route_policy
        self._choice = choice
        self._event_log = event_log
        self._record_model_usage = record_model_usage

    def create_context(self, context_input: MiraContextInput) -> DecisionContext:
        """Return a redacted, evidence-grounded context without persisting or authorizing it."""
        from thymira.agents.llm.base import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
            LLMStructuredOutputError,
        )
        from thymira.agents.llm.routing import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
            Role,
            choose,
        )
        from thymira.agents.route_policy import (  # noqa: PLC0415  # lazy: keep MIRA import agent-free
            enforce_model_route,
        )

        choice = self._choice or choose(Role.MIRA, "analyze")
        if self._event_log is not None:
            self._event_log.append(EventType.MODEL_SELECTED, Actor.system(), choice.event_payload())
        enforce_model_route(
            choice,
            self._route_policy,
            event_log=self._event_log,
            actor=Actor.system(),
        )
        try:
            draft, response = self._provider.complete_structured(
                scrub_credentials(build_context_prompt(context_input)),
                schema=_DecisionContextDraft,
                system=scrub_credentials(_CONTEXT_SYSTEM_PROMPT),
            )
        except LLMStructuredOutputError as exc:
            if exc.response is not None and self._record_model_usage is not None:
                self._record_model_usage(exc.response)
            raise
        if self._record_model_usage is not None:
            self._record_model_usage(response)
        safe_draft = _DecisionContextDraft.model_validate(redact_value(draft.to_json_dict()))
        evidence = _available_evidence(context_input)
        _validate_grounding(safe_draft, evidence)
        cited_evidence = _cited_evidence(safe_draft, evidence)
        context_id = _context_id(context_input, safe_draft)
        return DecisionContext(
            id=context_id,
            run_id=context_input.snapshot.run.id,
            summary=safe_draft.summary,
            relevant_facts=safe_draft.relevant_facts,
            active_constraints=safe_draft.active_constraints,
            current_risks=safe_draft.current_risks,
            evidence_available=safe_draft.evidence_available,
            evidence_gaps=safe_draft.evidence_gaps,
            unresolved_questions=safe_draft.unresolved_questions,
            upcoming_checkpoints=safe_draft.upcoming_checkpoints,
            evidence_refs=cited_evidence,
            based_on_run_version=context_input.run_version,
            based_on_event_seq=context_input.terminal_event_seq,
            based_on_event_hash=context_input.terminal_event_hash,
            generated_at=context_input.generated_at,
        )


def _available_evidence(context_input: MiraContextInput) -> tuple[Evidence, ...]:
    """Collect each supplied evidence reference once, preserving caller order."""
    groups: tuple[Iterable[Evidence], ...] = (
        context_input.evidence_refs,
        context_input.activity_profile.evidence_refs,
        context_input.risk_assessment.evidence,
        *(binding.evidence_refs for binding in context_input.applicable_pack_bindings),
        *(evaluation.evidence for evaluation in context_input.control_evaluations),
        *(finding.evidence for finding in context_input.open_findings),
    )
    unique: dict[tuple[str, str, str | None, str | None], Evidence] = {}
    for group in groups:
        for reference in group:
            safe_reference = Evidence.model_validate(redact_value(reference.to_json_dict()))
            key = _evidence_key(safe_reference)
            unique.setdefault(key, safe_reference)
    return tuple(unique.values())


def _statements(draft: _DecisionContextDraft) -> tuple[DecisionContextStatement, ...]:
    """Return every statement from one draft in its output order."""
    return (
        draft.summary,
        *draft.relevant_facts,
        *draft.active_constraints,
        *draft.current_risks,
        *draft.evidence_available,
        *draft.evidence_gaps,
        *draft.unresolved_questions,
        *draft.upcoming_checkpoints,
    )


def _validate_grounding(
    draft: _DecisionContextDraft, available_evidence: tuple[Evidence, ...]
) -> None:
    """Reject a draft that names the orchestrator or cites evidence outside its input.

    One offending statement discards the whole draft. That is deliberately unlike
    ``GenericPackControlRunner``, where a malformed control becomes a ``REQUIRES_HUMAN_REVIEW``
    evaluation so it cannot abort the audit of every other control: pack controls are independent
    code-authored units and a bad one can be recorded as one, while a draft is a single model
    response whose ``summary`` is mandatory and for which the Contract has no field that could
    record a dropped statement. Keeping the compliant remainder would publish a silently truncated
    context as a complete one; discarding it leaves the caller free to ask again.
    """
    available = {_evidence_key(reference) for reference in available_evidence}
    for statement in _statements(draft):
        if _THY_NAME.search(statement.text):
            raise ValueError("decision context must not name THY")
        if any(_evidence_key(reference) not in available for reference in statement.evidence_refs):
            raise ValueError("decision context cites evidence absent from the supplied input")


def _cited_evidence(
    draft: _DecisionContextDraft, available_evidence: tuple[Evidence, ...]
) -> tuple[Evidence, ...]:
    """Return supplied evidence cited by the structured response in stable source order."""
    cited = {
        _evidence_key(reference)
        for statement in _statements(draft)
        for reference in statement.evidence_refs
    }
    return tuple(reference for reference in available_evidence if _evidence_key(reference) in cited)


def _evidence_key(reference: Evidence) -> tuple[str, str, str | None, str | None]:
    """Return the complete immutable comparison key for an evidence reference."""
    return reference.kind, reference.ref, reference.sha256, reference.note


def _context_id(context_input: MiraContextInput, draft: _DecisionContextDraft) -> str:
    """Create a deterministic context id for one bounded response and event-log head."""
    facts = {
        "run_id": context_input.snapshot.run.id,
        "run_version": context_input.run_version,
        "event_seq": context_input.terminal_event_seq,
        "event_hash": context_input.terminal_event_hash,
        "generated_at": context_input.generated_at.isoformat(),
        "draft": draft.to_json_dict(),
    }
    return f"context_{sha256_text(canonical_json(facts))[:32]}"
