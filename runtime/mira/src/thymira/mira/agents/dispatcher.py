"""Explicit dispatch from shipped MIRA specs to their audit implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents.llm.routing import ModelChoice
from thymira.mira.agents.compaction_fidelity import audit_compaction_fidelity
from thymira.mira.agents.reg_evidence import enrich_findings
from thymira.mira.agents.runner import run_audit_agent
from thymira.mira.audit_io import AuditAgentOutput
from thymira.schemas import EventType

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.mira.agents.reg_evidence import CitationIndex
    from thymira.mira.agents.runner import AuditAgentContext
    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.audit_io import AuditInput
    from thymira.schemas import Event


REGULATORY_EVIDENCE_AGENT = "reg_evidence"
"""Shipped spec name for the regulatory-evidence enrichment implementation."""

COMPACTION_FIDELITY_AGENT = "compaction_fidelity"
"""Shipped spec name for the compaction-fidelity implementation."""


def dispatch_audit_agent(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
    *,
    citation_index: CitationIndex | None = None,
) -> AuditAgentOutput:
    """Run a shipped specialist or the generic audit runner as its explicit fallback.

    The dispatcher owns no authorization. Every branch receives the same immutable input and
    ``AuditAgentContext``; specialist tool calls, when declared, use the runner's shared Tool
    Manager adapter. The specialized branches return the same ``AuditAgentOutput`` contract as the
    generic runner, and the model choice is recovered from the ``model.selected`` event emitted by
    ``routed_model`` rather than preselected here.
    """
    event_start = len(context.event_log.events())
    handler = _SPECIALIZED_DISPATCH.get(spec.name)
    output = (
        run_audit_agent(spec, audit_input, context)
        if handler is None
        else handler(spec, audit_input, context, citation_index)
    )
    if output.model_choice is not None:
        return output
    choice = _last_model_choice(context.event_log.events()[event_start:])
    return output if choice is None else output.model_copy(update={"model_choice": choice})


def _run_regulatory_evidence(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
    citation_index: CitationIndex | None,
) -> AuditAgentOutput:
    """Run the citation enricher over the candidates accumulated by this audit pass."""
    if citation_index is None:
        raise ValueError("regulatory-evidence dispatch requires a citation index")
    report = audit_input.report
    findings = report.findings if report is not None else ()
    result = enrich_findings(spec, audit_input, findings, citation_index, context)
    return AuditAgentOutput(agent_name=spec.name, findings=result.enriched)


def _run_compaction_fidelity(
    spec: AuditAgentSpec,
    audit_input: AuditInput,
    context: AuditAgentContext,
    citation_index: CitationIndex | None,
) -> AuditAgentOutput:
    """Run the bespoke compaction comparison and normalize its candidate findings."""
    del citation_index
    result = audit_compaction_fidelity(spec, audit_input, context)
    return AuditAgentOutput.from_spec(spec, findings=result.findings)


def _last_model_choice(events: Sequence[Event]) -> ModelChoice | None:
    """Return the last router selection emitted during one dispatched agent call."""
    selected = [event for event in events if event.type is EventType.MODEL_SELECTED]
    if not selected:
        return None
    payload = selected[-1].payload
    return ModelChoice.model_validate(
        {field: payload[field] for field in ModelChoice.model_fields if field in payload}
    )


_SPECIALIZED_DISPATCH: dict[
    str,
    Callable[
        [AuditAgentSpec, AuditInput, AuditAgentContext, CitationIndex | None], AuditAgentOutput
    ],
] = {
    REGULATORY_EVIDENCE_AGENT: _run_regulatory_evidence,
    COMPACTION_FIDELITY_AGENT: _run_compaction_fidelity,
}


__all__ = [
    "COMPACTION_FIDELITY_AGENT",
    "REGULATORY_EVIDENCE_AGENT",
    "dispatch_audit_agent",
]
