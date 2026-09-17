"""Regulatory-evidence enrichment: resolvable citations, never fabricated ones.

The regulatory-evidence agent strengthens the assurance bundle's ``evidence_index`` by attaching,
to each finding another audit agent emitted, the knowledge-base source that grounds it. The model
proposes only a ``source_id`` -- :class:`_ProposedCitation` deliberately carries no digest, so a
model has no channel to assert a hash. The runtime resolves every proposed ``source_id`` against
the knowledge base itself through :class:`CitationIndex`, and stamps the authoritative location and
``sha256`` from the resolved, hash-verified chunk. A ``source_id`` the knowledge base does not
contain resolves to nothing and is dropped, not fabricated: a citation cannot enter the record by
being asserted, only by resolving. The optional run event or artifact a finding concerns is
likewise verified against the supplied :class:`~thymira.mira.audit_io.AuditInput` before it is
attached, so no reference the enricher adds is unverifiable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from thymira.agents.llm.base import LLMStructuredOutputError
from thymira.agents.llm.routing import Role
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import frame_untrusted
from thymira.events import canonical_json, current_surface, redact_value
from thymira.mira.agents.runner import (
    build_audit_tools,
    runtime_skill_event_payload,
    runtime_skill_instructions,
    validate_audit_agent_context,
)
from thymira.mira.kb import verifies_sha256
from thymira.schemas import (
    AuditFinding,
    EventType,
    Evidence,
    TaskStatus,
    ThymiraModel,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from thymira.mira.agents.runner import AuditAgentContext
    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.audit_io import AuditInput
    from thymira.mira.kb import RegulationChunk

_MAX_PROJECTION_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class CitationIndex:
    """A ``source_id`` -> hash-verified :class:`RegulationChunk` index: the sole citation authority.

    A citation is authoritative only when it resolves here. :meth:`resolve` builds the external
    :class:`~thymira.schemas.Evidence` from the resolved chunk's own ``source_id``, ``location``
    and ``sha256`` -- never from anything a model supplied -- so the enricher cannot mint a digest,
    and an unknown ``source_id`` yields ``None``.
    """

    _by_source_id: dict[str, RegulationChunk]

    @classmethod
    def from_chunks(cls, chunks: Iterable[RegulationChunk]) -> CitationIndex:
        """Index chunks by ``source_id``, recomputing each digest before it is trusted.

        Raises:
            ValueError: If a chunk's ``sha256`` does not verify, or two chunks share a source_id.
        """
        index: dict[str, RegulationChunk] = {}
        for chunk in chunks:
            if not verifies_sha256(chunk):
                raise ValueError(
                    f"cannot index a citation whose sha256 does not verify: {chunk.source_id!r}"
                )
            if chunk.source_id in index:
                raise ValueError(f"duplicate citation source_id: {chunk.source_id!r}")
            index[chunk.source_id] = chunk
        return cls(index)

    def sources(self) -> tuple[RegulationChunk, ...]:
        """Return the indexed chunks in ``source_id`` order for a deterministic catalog."""
        return tuple(self._by_source_id[source_id] for source_id in sorted(self._by_source_id))

    def resolve(self, source_id: str) -> Evidence | None:
        """Return authoritative external evidence for ``source_id``, or ``None`` when unknown."""
        chunk = self._by_source_id.get(source_id)
        if chunk is None:
            return None
        return Evidence(
            kind="external",
            ref=f"{chunk.source_id}/{chunk.location}",
            sha256=chunk.sha256,
        )


@dataclass(frozen=True, slots=True)
class RegulatoryEvidenceResult:
    """The outcome of one enrichment pass over another agent's findings.

    ``findings`` is every input finding, each carrying its resolved external citations where a
    proposal resolved; ``enriched`` is the subset that gained at least one external citation; and
    ``dropped_citations`` is every proposed ``source_id`` the knowledge base could not resolve.
    """

    findings: tuple[AuditFinding, ...]
    enriched: tuple[AuditFinding, ...]
    dropped_citations: tuple[str, ...]


class _ProposedCitation(ThymiraModel):
    """One citation a model proposes: a ``source_id`` and, at most, the run ref it concerns.

    It carries no ``sha256`` and no source text: a model can name a source to resolve, never mint
    one. The runtime supplies the authoritative digest from the resolved chunk. ``note`` is
    accepted only as an ignored compatibility field; model prose never enters evidence.
    """

    finding_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    concerns_kind: Literal["event", "artifact", "experiment"] | None = None
    concerns_ref: str | None = None
    note: str | None = None


class _CitationProposals(ThymiraModel):
    """The structured proposals returned by the regulatory-evidence agent."""

    citations: tuple[_ProposedCitation, ...] = ()


def enrich_findings(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    findings: Sequence[AuditFinding],
    index: CitationIndex,
    context: AuditAgentContext,
) -> RegulatoryEvidenceResult:
    """Enrich other agents' findings with resolvable citations, dropping unresolvable ones.

    The agent proposes citations by ``source_id`` only; every proposal is resolved against
    ``index`` and dropped when the knowledge base does not contain it. The external evidence
    attached to a finding always carries the resolved chunk's own location and ``sha256``.

    Raises:
        ValueError: If the context or the findings do not belong to the audited Run.
        LLMStructuredOutputError: If structured output cannot be validated within ``max_turns``.
    """
    validate_audit_agent_context(spec, audit_input, context)
    if any(finding.run_id != audit_input.run_id for finding in findings):
        raise ValueError("every finding to enrich must belong to the audited run")

    proposals = _propose_citations(spec, audit_input, tuple(findings), index, context)
    return _resolve_proposals(tuple(findings), proposals, index, audit_input)


def _propose_citations(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    findings: tuple[AuditFinding, ...],
    index: CitationIndex,
    context: AuditAgentContext,
) -> tuple[_ProposedCitation, ...]:
    """Run the agent once and return its raw, still-unresolved citation proposals."""
    prompt = _build_proposal_projection(audit_input, findings, index)
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
        output_schema=_CitationProposals,
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
    agent: PydanticAgent[None, _CitationProposals] = PydanticAgent(
        model=model,
        output_type=_CitationProposals,
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
        message = f"MIRA regulatory-evidence agent {spec.name!r} exhausted structured-output"
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
    return result.output.citations


def _resolve_proposals(
    findings: tuple[AuditFinding, ...],
    proposals: tuple[_ProposedCitation, ...],
    index: CitationIndex,
    audit_input: AuditInput,
) -> RegulatoryEvidenceResult:
    """Resolve every proposal against the knowledge base, dropping the citations it cannot back."""
    valid_ids = {finding.id for finding in findings}
    by_finding: defaultdict[str, list[_ProposedCitation]] = defaultdict(list)
    for citation in proposals:
        if citation.finding_id in valid_ids:
            by_finding[citation.finding_id].append(citation)

    all_findings: list[AuditFinding] = []
    enriched: list[AuditFinding] = []
    dropped: list[str] = []
    for finding in findings:
        added: list[Evidence] = []
        for citation in by_finding.get(finding.id, ()):
            external = index.resolve(citation.source_id)
            if external is None:
                dropped.append(citation.source_id)
                continue
            # The model may carry a prose note for its own reasoning, but it cannot establish
            # evidence by quoting caller-controlled text. Keep only the index-owned reference and
            # digest; findings can cite the source without copying raw notes into the report.
            added.append(external)
            concerns = _concerns_evidence(citation, audit_input)
            if concerns is not None:
                added.append(concerns)
        if added:
            merged = _deduplicate((*finding.evidence, *added))
            updated = finding.model_copy(update={"evidence": merged})
            all_findings.append(updated)
            enriched.append(updated)
        else:
            all_findings.append(finding)
    return RegulatoryEvidenceResult(
        findings=tuple(all_findings),
        enriched=tuple(enriched),
        dropped_citations=tuple(dropped),
    )


def _concerns_evidence(citation: _ProposedCitation, audit_input: AuditInput) -> Evidence | None:
    """Return the run ref the finding concerns only when it exists in the audited Run."""
    kind = citation.concerns_kind
    ref = citation.concerns_ref
    if kind is None or ref is None:
        return None
    if kind == "event" and ref in {f"seq:{event.seq}" for event in audit_input.events}:
        return Evidence(kind="event", ref=ref)
    if kind == "artifact" and ref in audit_input.artifact_names:
        return Evidence(kind="artifact", ref=ref)
    if kind == "experiment" and ref in audit_input.experiment_ids:
        return Evidence(kind="experiment", ref=ref)
    return None


def _deduplicate(evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    """Return distinct evidence in first-seen order, matching the assurance index's key."""
    unique: dict[tuple[str, str, str | None, str | None], Evidence] = {}
    for reference in evidence:
        key = (reference.kind, reference.ref, reference.sha256, reference.note)
        unique.setdefault(key, reference)
    return tuple(unique.values())


def _build_proposal_projection(
    audit_input: AuditInput,
    findings: tuple[AuditFinding, ...],
    index: CitationIndex,
) -> str:
    """Return a redacted, deterministic projection of findings, the Run, and the citable sources."""
    projection = {
        "run_id": audit_input.run_id,
        "findings": [
            {
                "finding_id": finding.id,
                "framework": finding.framework.value,
                "control_id": finding.control_id,
                "title": finding.title,
            }
            for finding in findings
        ],
        "source_catalog": [
            {
                "source_id": chunk.source_id,
                "framework": chunk.framework.value,
                "location": chunk.location,
            }
            for chunk in index.sources()
        ],
        "events": [
            {"seq": event.seq, "type": event.type.value, "payload": redact_value(event.payload)}
            for event in current_surface(audit_input.events)
        ],
        "artifact_names": audit_input.artifact_names,
        "experiment_ids": audit_input.experiment_ids,
    }
    rendered = canonical_json(redact_value(projection))
    try:
        return frame_untrusted(
            rendered, label="mira-regulatory-evidence", max_chars=_MAX_PROJECTION_CHARS
        )
    except ValueError:
        return (
            "MIRA regulatory evidence omitted: the configured budget cannot fit a complete "
            "safety frame."
        )


__all__ = [
    "CitationIndex",
    "RegulatoryEvidenceResult",
    "enrich_findings",
]
