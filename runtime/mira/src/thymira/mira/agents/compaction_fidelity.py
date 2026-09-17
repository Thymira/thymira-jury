"""Compaction fidelity audit agent (CMP-02): does the summary faithfully represent what it hid?

ADR-0006 makes compaction an *appended* record. A ``context.compacted`` event shadows the
model-visible events it hid (``shadowed_seqs``), records the summarising model's envelope, and
persists only the **safe summary projection** -- never the raw provider output, which satisfies
ADR-0004's chain-of-thought rule by construction. CMP-01's deterministic control (A27) guarantees
the *structure* of that record; ADR-0006 decision 5 defers the remaining question to an audit
agent: *does the persisted summary faithfully represent what it replaced?*

This agent answers exactly that. For each ``context.compacted`` event it recovers, by seq, the
events the compaction shadowed -- which :func:`thymira.events.current_surface` deliberately hides
from the model, so a bespoke projection is required rather than the generic runner's surface
projection -- and shows the model the persisted summary beside those recovered events. The model
proposes a fidelity gap when the summary omits or misrepresents a material shadowed fact (a
shadowed tool failure the summary drops, say, or reports as a success). It never sees raw provider
output, because none is persisted.

"LLM proposes, code authorizes" applies to the evidence as much as to the decision: the model may
only point at a compaction that exists and at seqs that compaction genuinely shadowed. The runtime
recovers the compaction's actual shadowed seqs from the log and builds the finding's Evidence from
those, dropping any seq the model invented -- so a fidelity finding always references real shadowed
events, and a proposal about a non-existent compaction is dropped rather than minted into a finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from thymira.agents.llm.base import LLMStructuredOutputError
from thymira.agents.llm.routing import Role
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import frame_untrusted
from thymira.events import canonical_json, redact_value, shadowed_seqs
from thymira.mira.agents.runner import (
    build_audit_tools,
    runtime_skill_event_payload,
    runtime_skill_instructions,
    validate_audit_agent_context,
)
from thymira.mira.checks import COMPACTION_ENVELOPE_KEY
from thymira.mira.finding_safety import safe_finding
from thymira.schemas import (
    AuditFinding,
    Event,
    EventType,
    Evidence,
    Severity,
    TaskStatus,
    ThymiraModel,
    new_id,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.mira.agents.runner import AuditAgentContext
    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.audit_io import AuditInput

#: Payload key of a ``context.compacted`` event carrying the persisted safe-summary projection.
#: CMP-01 defined the ``envelope`` and ``shadowed_seqs`` keys; this is the summary those hid.
COMPACTION_SUMMARY_KEY = "summary"

#: Stable control id every compaction-fidelity finding carries (an audit agent, not a CONTROLS id).
COMPACTION_FIDELITY_CONTROL_ID = "COMPACTION-FIDELITY"

_MAX_PROJECTION_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class CompactionFidelityResult:
    """The candidate findings one compaction-fidelity pass produced over a Run's compactions."""

    findings: tuple[AuditFinding, ...]


class _CompactionFidelityGap(ThymiraModel):
    """One fidelity gap a model proposes about a single ``context.compacted`` event.

    The model names ``compaction_seq`` (which must exist) and, in ``omitted_seqs``, the shadowed
    events whose material facts the summary drops or misrepresents. It never supplies the finding's
    identity or its evidence digests: the runtime resolves both.
    """

    compaction_seq: int
    omitted_seqs: tuple[int, ...] = ()
    title: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    recommendation: str | None = None


class _CompactionFidelityResponse(ThymiraModel):
    """The structured response returned by the compaction-fidelity agent."""

    gaps: tuple[_CompactionFidelityGap, ...] = ()


def audit_compaction_fidelity(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
) -> CompactionFidelityResult:
    """Judge whether each compaction's summary faithfully represents the events it shadowed.

    The agent is a no-op when the Run recorded no compaction: there is nothing whose fidelity to
    check, so no model is called. Otherwise the model reasons over a bespoke projection that pairs
    each compaction's persisted summary with the events it shadowed (recovered by seq), and every
    proposed gap is resolved against the log before it becomes a finding.

    Raises:
        ValueError: If the context does not belong to the audited Run.
        LLMStructuredOutputError: If structured output cannot be validated within ``max_turns``.
    """
    validate_audit_agent_context(spec, audit_input, context)

    compactions = tuple(
        event for event in audit_input.events if event.type is EventType.CONTEXT_COMPACTED
    )
    if not compactions:
        return CompactionFidelityResult(findings=())

    by_seq = {event.seq: event for event in audit_input.events}
    gaps = _propose_gaps(spec, audit_input, compactions, by_seq, context)
    return _resolve_gaps(spec, gaps, compactions, by_seq, audit_input, context)


def _propose_gaps(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    compactions: tuple[Event, ...],
    by_seq: dict[int, Event],
    context: AuditAgentContext,
) -> tuple[_CompactionFidelityGap, ...]:
    """Run the agent once over the compaction projection and return its raw proposals."""
    prompt = _build_projection(audit_input, compactions, by_seq)
    instructions = runtime_skill_instructions(spec, context)
    runtime_evidence = runtime_skill_event_payload(context)
    task_kind = spec.task_kinds[0]
    model = routed_model(
        Role.MIRA,
        task_kind,
        context.event_log,
        requested_tier=spec.tier,
        provider=context.provider,
        actor=context.actor,
        output_schema=_CompactionFidelityResponse,
        runtime_skill_evidence=runtime_evidence,
        before_model_selection=context.before_model_selection,
        before_model_call=context.before_model_call,
        record_model_usage=context.record_model_usage,
        route_policy=context.route_policy,
        request_ledger=context.request_ledger,
        request_owner_id=(
            context.runtime_skill_dispatch_id or f"{context.task_id}:{context.agent_id}"
        ),
    )
    agent: PydanticAgent[None, _CompactionFidelityResponse] = PydanticAgent(
        model=model,
        output_type=_CompactionFidelityResponse,
        instructions=instructions,
        retries=max(spec.max_turns - 1, 0),
        tools=build_audit_tools(spec, context),
    )
    context.event_log.append(
        EventType.AGENT_STARTED,
        context.actor,
        {
            "agent": spec.name,
            "agent_id": context.agent_id,
            "task_id": context.task_id,
            "framework": spec.framework.value,
        },
        subject_id=context.agent_id,
        producer="thymira.mira",
    )
    try:
        result = agent.run_sync(prompt, usage_limits=UsageLimits(request_limit=spec.max_turns))
    except (UnexpectedModelBehavior, UsageLimitExceeded):
        context.event_log.append(
            EventType.AGENT_COMPLETED,
            context.actor,
            {
                "agent": spec.name,
                "agent_id": context.agent_id,
                "task_id": context.task_id,
                "status": TaskStatus.FAILED.value,
                "max_turns": spec.max_turns,
            },
            subject_id=context.agent_id,
            producer="thymira.mira",
        )
        message = f"MIRA compaction-fidelity agent {spec.name!r} exhausted structured-output"
        raise LLMStructuredOutputError(message) from None

    context.event_log.append(
        EventType.AGENT_COMPLETED,
        context.actor,
        {
            "agent": spec.name,
            "agent_id": context.agent_id,
            "task_id": context.task_id,
            "status": TaskStatus.COMPLETED.value,
        },
        subject_id=context.agent_id,
        producer="thymira.mira",
    )
    return result.output.gaps


def _resolve_gaps(
    spec: AuditAgentSpec,
    gaps: tuple[_CompactionFidelityGap, ...],
    compactions: tuple[Event, ...],
    by_seq: dict[int, Event],
    audit_input: AuditInput,
    context: AuditAgentContext,
) -> CompactionFidelityResult:
    """Turn each proposal that names a real compaction into a finding with recovered evidence."""
    compaction_by_seq = {event.seq: event for event in compactions}
    findings: list[AuditFinding] = []
    for gap in gaps:
        compaction = compaction_by_seq.get(gap.compaction_seq)
        if compaction is None:
            # A proposal about a compaction that does not exist cannot be minted into a finding.
            continue
        actual = _recovered_shadowed_seqs(compaction, by_seq)
        omitted = tuple(seq for seq in gap.omitted_seqs if seq in actual)
        referenced = omitted or actual
        findings.append(_finding(spec, gap, compaction, referenced, audit_input, context))
    return CompactionFidelityResult(findings=tuple(findings))


def _recovered_shadowed_seqs(compaction: Event, by_seq: dict[int, Event]) -> tuple[int, ...]:
    """Return the seqs this compaction actually shadowed: named, recorded, and preceding it."""
    return tuple(seq for seq in shadowed_seqs(compaction) if seq < compaction.seq and seq in by_seq)


def _finding(
    spec: AuditAgentSpec,
    gap: _CompactionFidelityGap,
    compaction: Event,
    referenced_seqs: tuple[int, ...],
    audit_input: AuditInput,
    context: AuditAgentContext,
) -> AuditFinding:
    """Build one fidelity finding, stamping identity and evidence in code, not from the model."""
    evidence = _deduplicate(
        (
            Evidence(kind="event", ref=f"seq:{compaction.seq}"),
            *(Evidence(kind="event", ref=f"seq:{seq}") for seq in referenced_seqs),
        )
    )
    return safe_finding(
        AuditFinding(
            id=new_id("finding"),
            run_id=audit_input.run_id,
            agent_id=context.agent_id,
            control_id=COMPACTION_FIDELITY_CONTROL_ID,
            framework=spec.framework,
            title=gap.title,
            finding=gap.finding,
            severity=gap.severity,
            confidence=gap.confidence,
            evidence=evidence,
            recommendation=gap.recommendation,
            created_at=utc_now(),
        )
    )


def _deduplicate(evidence: Sequence[Evidence]) -> tuple[Evidence, ...]:
    """Return distinct evidence pointers in first-seen order."""
    unique: dict[tuple[str, str], Evidence] = {}
    for reference in evidence:
        unique.setdefault((reference.kind, reference.ref), reference)
    return tuple(unique.values())


def _build_projection(
    audit_input: AuditInput,
    compactions: tuple[Event, ...],
    by_seq: dict[int, Event],
) -> str:
    """Return a redacted, deterministic projection pairing each summary with its shadowed events."""
    projection = {
        "run_id": audit_input.run_id,
        "compactions": [
            {
                "compaction_seq": compaction.seq,
                "summary": compaction.payload.get(COMPACTION_SUMMARY_KEY),
                "envelope": compaction.payload.get(COMPACTION_ENVELOPE_KEY),
                "shadowed_events": [
                    {
                        "seq": seq,
                        "type": by_seq[seq].type.value,
                        "payload": by_seq[seq].payload,
                    }
                    for seq in _recovered_shadowed_seqs(compaction, by_seq)
                ],
            }
            for compaction in compactions
        ],
    }
    rendered = canonical_json(redact_value(projection))
    try:
        return frame_untrusted(
            rendered, label="mira-compaction-evidence", max_chars=_MAX_PROJECTION_CHARS
        )
    except ValueError:
        return (
            "MIRA compaction evidence omitted: the configured budget cannot fit a complete "
            "safety frame."
        )


__all__ = [
    "COMPACTION_FIDELITY_CONTROL_ID",
    "COMPACTION_SUMMARY_KEY",
    "CompactionFidelityResult",
    "audit_compaction_fidelity",
]
