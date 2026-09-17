"""The provider-backed adversarial finding verifier (MIRA-03).

``thymira.mira.flow`` receives a :data:`~thymira.mira.verification.FindingVerifier` by injection
and never imports this module; this is its concrete, second-pass-judge implementation, which may
import ``thymira.agents`` (P2). It re-judges one candidate finding against a bounded, redacted
projection of the Run's audit evidence through the provider's structured-output path and
returns a :class:`~thymira.mira.verification.FindingVerdict`.

The verifier records no event and authorizes nothing: dropping an unsupported finding is a
conservative narrowing, and the flow only ever submits *agent-authored* candidates to it -- a
code-authored deterministic or preflight finding is never verified or dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents.llm.base import LLMStructuredOutputError
from thymira.agents.llm.routing import ModelChoice, Role, choose
from thymira.agents.prompt_framing import frame_untrusted
from thymira.agents.request_ledger import RequestLedger, instrument_provider
from thymira.agents.route_policy import enforce_model_route
from thymira.events import canonical_json, current_surface, redact_value, scrub_credentials
from thymira.mira.verification import FindingVerdict
from thymira.schemas import Actor, EventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.events import EventLog
    from thymira.mira.checks import AuditReport
    from thymira.mira.verification import FindingVerifier
    from thymira.schemas import AuditFinding, Event, ModelRoutePolicy

_DEFAULT_INSTRUCTIONS = (
    "You are MIRA's adversarial finding verifier. You are given one candidate audit finding and "
    "a curated projection of the run's audit evidence. Judge whether the recorded evidence "
    "supports the finding. Set supported=true only when the evidence substantiates it; set "
    "supported=false for a finding the evidence does not support. Never invent evidence."
)

_MAX_EVENTS = 40
"""How many curated audit events to show the judge, at most."""

_MAX_PROJECTION_CHARS = 12_000
"""How many characters of the rendered projection to show the judge, at most.

The events half is already bounded by :data:`_MAX_EVENTS`, but ``finding.evidence`` has no
per-finding bound, so a finding carrying a large evidence list would render an unbounded prompt.
This whole-projection cap -- the convention its siblings share (``reg_evidence``,
``compaction_fidelity``) -- backstops that, and the ``…`` marker records that content was dropped
so the judge can tell "no more evidence" from "more evidence I was not shown".
"""

_AUDIT_EVENT_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_TRANSITIONED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_DENIED,
        EventType.POLICY_DECISION,
        EventType.ARTIFACT_CREATED,
    }
)
"""Typed audit events that MIRA may inspect even when they are LOG_ONLY globally."""


def build_finding_verifier(
    provider: LLMProvider,
    *,
    instructions: str = _DEFAULT_INSTRUCTIONS,
    event_log: EventLog | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    request_ledger: RequestLedger | None = None,
    request_owner_id: str = "mira-finding-verifier",
    route_policy: ModelRoutePolicy | None = None,
) -> FindingVerifier:
    """Return a :data:`FindingVerifier` that judges each candidate through ``provider``.

    Args:
        provider: The structured-output LLM provider (a ``ScriptedProvider`` in tests).
        instructions: The system prompt handed to the judge.
        event_log: Optional log for the routed ``model.selected`` evidence.
        before_model_selection: Optional budget hook before routing.
        before_model_call: Optional Gate hook after routing and before invocation.
        record_model_usage: Optional hook for the measured provider response.
        request_ledger: Optional durable ledger for the effective provider request and response.
        request_owner_id: Durable owner of the model-visible verifier facts.
        route_policy: Immutable session allowlist checked before the structured provider call.

    Returns:
        A callable that maps a candidate finding and the run evidence to a
        :class:`~thymira.mira.verification.FindingVerdict`.
    """

    def verify(
        finding: AuditFinding,
        events: tuple[Event, ...],
        report: AuditReport,
    ) -> FindingVerdict:
        """Judge one candidate finding against curated audit evidence."""
        del report  # The curated report is available; this judge reasons over the finding+events.
        if before_model_selection is not None:
            before_model_selection()
        choice = choose(Role.MIRA, "audit_judgement")
        if event_log is not None:
            event_log.append(EventType.MODEL_SELECTED, Actor.system(), choice.event_payload())
        enforce_model_route(
            choice,
            route_policy,
            event_log=event_log,
            actor=Actor.system(),
        )
        if before_model_call is not None:
            before_model_call(choice)
        # This is a direct provider call, outside RoutedModel's final-message guard.  Scrub at
        # the source boundary so a caller-controlled instruction or evidence value can never
        # carry a known credential to the provider.
        prompt = scrub_credentials(_verification_prompt(finding, events))
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
            resolved_ledger = RequestLedger(event_log)
        active_provider = (
            instrument_provider(request_provider, resolved_ledger, owner_id=request_owner_id)
            if resolved_ledger is not None
            else request_provider
        )
        try:
            verdict, response = active_provider.complete_structured(
                prompt, schema=FindingVerdict, system=scrub_credentials(instructions)
            )
        except LLMStructuredOutputError as exc:
            if exc.response is not None and record_model_usage is not None:
                record_model_usage(exc.response)
            raise
        if record_model_usage is not None:
            record_model_usage(response)
        return verdict

    return verify


def _verification_prompt(finding: AuditFinding, events: tuple[Event, ...]) -> str:
    """Render a bounded, redacted projection of the finding and curated audit evidence."""
    audit_events = _select_verification_events(finding, events)
    projection = {
        "candidate_finding": {
            "control_id": finding.control_id,
            "framework": finding.framework.value,
            "title": finding.title,
            "finding": finding.finding,
            "severity": finding.severity.value,
            "evidence": [{"kind": item.kind, "ref": item.ref} for item in finding.evidence],
        },
        "audit_evidence_events": [
            {"seq": event.seq, "type": event.type.value, "payload": redact_value(event.payload)}
            for event in audit_events
        ],
    }
    rendered = canonical_json(redact_value(projection))
    try:
        return frame_untrusted(rendered, label="mira-verification", max_chars=_MAX_PROJECTION_CHARS)
    except ValueError:
        return (
            "MIRA verification evidence omitted: the configured budget cannot fit a complete "
            "safety frame."
        )


def _select_verification_events(
    finding: AuditFinding, events: tuple[Event, ...]
) -> tuple[Event, ...]:
    """Select bounded audit evidence while prioritising events cited by the finding.

    The global model surface deliberately keeps tool lifecycle events LOG_ONLY. MIRA's verifier
    still needs those authoritative events to judge a finding, so this private projection reads
    them directly from the log without changing what THY receives through ``current_surface``.
    """
    current = {event.seq for event in current_surface(events)}
    selected = tuple(
        event for event in events if event.seq in current or event.type in _AUDIT_EVENT_TYPES
    )
    referenced = {evidence.ref for evidence in finding.evidence if evidence.kind == "event"}
    cited_seqs = {event.seq for event in selected if f"seq:{event.seq}" in referenced}
    cited_events = tuple(event for event in selected if event.seq in cited_seqs)
    recent_events = tuple(event for event in selected if event.seq not in cited_seqs)
    remaining = max(_MAX_EVENTS - len(cited_events), 0)
    recent = recent_events[-remaining:] if remaining else ()
    return (cited_events[:_MAX_EVENTS] + recent)[:_MAX_EVENTS]


__all__ = ["build_finding_verifier"]
