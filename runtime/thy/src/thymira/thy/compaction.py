"""Context compaction: shadow old surface events behind one audited summary (THY-25).

ADR-0006/ADR-0007: the *log* is the complete hash-chained record; the *surface* is the subset a
model is shown. Compaction never deletes an event -- it appends a single ``context.compacted``
event naming the seqs it shadowed, so ``verify_events`` keeps passing (no chained record is
touched) while ``current_surface`` returns fewer events. The append is atomic by construction:
one write, no opening event, no bracket (ADR-0007), so a crash before it leaves the surface
untouched and a crash after it leaves a complete compaction.

Two integrity rules follow the ADRs exactly:

- **Only the safe summary projection is persisted.** The summariser is asked for a structured
  :class:`CompactionSummary` whose one field is the summary; the provider's raw completion (which
  may carry chain-of-thought) is never written to the log. ADR-0004's rule is satisfied by
  construction, not by discipline.
- **The envelope records which model hid what.** ``provider``, ``model``, ``tier`` and the
  measured token counts travel in the payload, the way ``PolicyDecision.policy_sha256`` lets an
  auditor replay a policy decision.

The token estimate here is a declared heuristic (~4 characters per token), never a provider
measurement -- ADR-0005 idea 4, "measurement declares its confidence". :mod:`thymira.thy.budget`
(THY-26) reuses :func:`estimate_tokens` so the size a prompt is judged by and the size compaction
shadows down to are the same unit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING

from pydantic import Field

from thymira.agents.context_recovery import (
    MODEL_OUTPUT_CHAR_LIMIT,
    ContextCheckpoint,
    build_source_checkpoint,
    prune_head_marker_tail,
    recover_checkpoint,
    render_checkpoint,
)
from thymira.agents.llm.base import LLMConfigurationError, LLMStructuredOutputError
from thymira.agents.llm.litellm_provider import LiteLLMProvider
from thymira.agents.llm.routing import UNCONFIGURED_MODEL, ModelTier, Role, choose
from thymira.agents.prompt_framing import frame_untrusted
from thymira.agents.request_ledger import RequestLedger, instrument_provider
from thymira.agents.route_policy import enforce_model_route
from thymira.events import (
    SHADOWED_SEQS_KEY,
    canonical_json,
    current_surface,
    scrub_credentials,
    sha256_text,
)
from thymira.schemas import EventSurface, EventType, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents.llm.base import (
        BaseModelT,
        LLMMessage,
        LLMProvider,
        LLMResponse,
        LLMToolDefinition,
    )
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.usage import ConcurrentRunUsage, RunUsage, RunUsageReservation
    from thymira.events import EventLog
    from thymira.schemas import Actor, Event, ModelRoutePolicy

_DEFAULT_CHARS_PER_TOKEN = 4
"""Characters per token for the provider-free estimate; a declared heuristic, not a measurement."""

SUMMARY_TEXT_KEY = "text"
"""Payload key the compaction summary is stored under -- the key ``PromptBuilder`` renders, so the
summary reaches a later prompt in place of the events it shadowed."""

ENVELOPE_KEY = "envelope"
"""Payload key of the summarisation envelope (provider, model, tier, measured token counts)."""

COMPACTION_SYSTEM_PROMPT = (
    "You compact the history of an audited data-science run. Summarise the prior steps below "
    "faithfully and concisely so a later step needs less context. Output only the summary; never "
    "include private reasoning."
)

_RUNTIME_CONTEXT_FORM = "runtime_context"
"""The superseding runtime snapshot may be folded into the durable source checkpoint."""


class CompactionSummary(ThymiraModel):
    """The safe projection of a compaction: a faithful summary of the shadowed events.

    Only this field is ever persisted. The schema deliberately cannot carry the model's raw
    completion or its chain-of-thought, so ADR-0004's "chain-of-thought is never persisted" rule
    holds by construction rather than by the caller remembering to strip it.
    """

    summary: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class CompactionContext:
    """What :func:`compact_surface` needs to summarise a call and record it as evidence.

    Groups the provider, evidence and accounting dependencies so the function keeps a small
    ``(events, budget, ctx)`` signature, mirroring
    :class:`thymira.agents.PromptProvenanceContext`. ``choice`` is the routing decision for the
    summariser call; its ``model``/``tier_applied`` become the envelope's ``model``/``tier`` so an
    auditor sees which model produced the summary. ``record_model_usage`` charges even a returned
    response whose structured summary is rejected before the atomic compaction event exists.
    """

    provider: LLMProvider | None
    choice: ModelChoice | None
    event_log: EventLog
    actor: Actor
    request_ledger: RequestLedger | None = None
    request_owner_id: str = "thy-compaction"
    request_digest: str | None = None
    request_tokens: int | None = None
    primary_request: str | None = None
    current_work: str | None = None
    route_policy: ModelRoutePolicy | None = None
    usage: RunUsage | None = None
    concurrent_usage: ConcurrentRunUsage | None = None
    before_model_selection: Callable[[], None] | None = None
    before_model_call: Callable[[ModelChoice], None] | None = None
    record_model_usage: Callable[[LLMResponse], None] | None = None


class _CompactionProjectionProvider:
    """Expose only safe structured compaction projections to the durable request ledger."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self.provider_name = provider.provider_name
        self.model = provider.model

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        """Forward the unused conversational provider seam."""
        return self._provider.complete_turn(
            messages, tools=tools, parallel_tool_calls=parallel_tool_calls
        )

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        """Forward the unused free-text provider seam."""
        return self._provider.complete(prompt, system=system)

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        """Replace raw returned text with the validated projection before ledger capture."""
        try:
            validated, response = self._provider.complete_structured(
                prompt, schema=schema, system=system
            )
        except LLMStructuredOutputError as exc:
            if exc.response is None:
                raise
            safe_response = exc.response.model_copy(
                update={
                    "text": "[REDACTED:INVALID_STRUCTURED_RESPONSE]",
                    "metadata": {"schema": schema.__name__},
                }
            )
            raise LLMStructuredOutputError(str(exc), response=safe_response) from None
        safe_response = response.model_copy(
            update={
                "text": validated.model_dump_json(),
                "metadata": {"schema": schema.__name__},
            }
        )
        return validated, safe_response


def _prepare_model_call(
    ctx: CompactionContext,
) -> tuple[LLMProvider, ModelChoice, RequestLedger]:
    """Authorize and instrument one compaction request immediately before dispatch."""
    if ctx.before_model_selection is not None:
        ctx.before_model_selection()
    if ctx.usage is not None and ctx.concurrent_usage is None:
        ctx.usage.ensure_request_available()
    choice = ctx.choice or choose(Role.THY, "summarize", requested_tier=ModelTier.FAST)
    ledger = ctx.request_ledger or RequestLedger(ctx.event_log)
    evidence_log = ledger.event_log
    evidence_log.append(EventType.MODEL_SELECTED, ctx.actor, choice.event_payload())
    enforce_model_route(
        choice,
        ctx.route_policy,
        event_log=evidence_log,
        actor=ctx.actor,
    )
    if ctx.before_model_call is not None:
        ctx.before_model_call(choice)
    provider = ctx.provider
    if provider is None:
        if choice.model == UNCONFIGURED_MODEL:
            raise LLMConfigurationError(
                "the compacting model choice is unavailable; configure a THY model route"
            )
        provider = LiteLLMProvider(model=choice.model)
    detach_ledger = getattr(provider, "without_request_ledger", None)
    if callable(detach_ledger):
        provider = detach_ledger()
    elif callable(getattr(provider, "attach_request_ledger", None)):
        raise LLMConfigurationError(
            "the compaction provider can capture requests internally but cannot detach its "
            "existing ledger safely"
        )
    active_provider = instrument_provider(
        _CompactionProjectionProvider(provider), ledger, owner_id=ctx.request_owner_id
    )
    return active_provider, choice, ledger


def _charge_model_response(
    ctx: CompactionContext,
    response: LLMResponse,
    reservation: RunUsageReservation | None = None,
) -> None:
    """Charge one completed provider response into Core and THY exactly once each."""
    if reservation is not None:
        reservation.charge(response)
    if ctx.record_model_usage is not None:
        ctx.record_model_usage(response)
    if ctx.usage is not None:
        ctx.usage.charge(response)


def estimate_tokens(text: str) -> int:
    """A deterministic, provider-free token estimate (~4 characters per token).

    Declared, not measured (ADR-0005 idea 4): a real tokenizer replaces this later without moving
    the seam. Empty text costs zero tokens.
    """
    if not text:
        return 0
    return ceil(len(text) / _DEFAULT_CHARS_PER_TOKEN)


def _render(event: Event) -> str:
    """The text ``PromptBuilder`` would render for ``event`` (its ``text`` payload, or empty)."""
    text = str(event.payload.get(SUMMARY_TEXT_KEY) or "")
    if event.type is EventType.CONTEXT_COMPACTED:
        raw_checkpoint = event.payload.get("checkpoint")
        if isinstance(raw_checkpoint, dict):
            try:
                checkpoint = ContextCheckpoint.model_validate(raw_checkpoint)
            except (TypeError, ValueError):
                checkpoint = None
            if checkpoint is not None:
                text = f"{render_checkpoint(checkpoint, max_chars=240)}\nCompaction summary: {text}"
    return prune_head_marker_tail(text, MODEL_OUTPUT_CHAR_LIMIT).text


def estimate_events_tokens(events: Sequence[Event]) -> int:
    """Estimated tokens the rendered surface of ``events`` occupies in a prompt."""
    return sum(estimate_tokens(_render(event)) for event in events)


_PAIR_ID_KEYS = {
    "subject_id",
    "task_id",
    "tool_call_id",
    "decision_id",
    "policy_decision_id",
    "approval_id",
    "authorization_context_sha256",
    "approval_scope_sha256",
    "tool_intent_sha256",
    "evidence_id",
    "artifact_id",
    "evidence_ids",
    "artifact_ids",
    "finding_ids",
    "input_artifact_ids",
}


def _pair_ids(event: Event) -> set[str]:
    """Return identity values that bind a call to its result/evidence/approval."""
    values: set[str] = set()
    if isinstance(event.subject_id, str):
        values.add(f"subject:{event.subject_id}")
    for key in ("authorization_context_sha256", "causation_id"):
        value = getattr(event, key, None)
        if isinstance(value, str):
            values.add(f"{key}:{value}")

    def walk(payload: object) -> None:
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if key in _PAIR_ID_KEYS:
                    if isinstance(value, str):
                        values.add(f"{key}:{value}")
                    elif isinstance(value, list):
                        values.update(f"{key}:{item}" for item in value if isinstance(item, str))
                walk(value)
        elif isinstance(payload, list):
            for item in payload:
                walk(item)

    walk(event.payload)
    return values


def _pair_groups(surface: Sequence[Event]) -> list[set[int]]:
    """Build connected groups of events that must be pruned together."""
    parents = list(range(len(surface)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    seen: dict[str, int] = {}
    for index, event in enumerate(surface):
        for identity in _pair_ids(event):
            if identity in seen:
                union(index, seen[identity])
            else:
                seen[identity] = index
    groups: dict[int, set[int]] = {}
    for index in range(len(surface)):
        groups.setdefault(find(index), set()).add(index)
    return list(groups.values())


def _select_shadowed(surface: Sequence[Event], budget: int) -> list[Event]:
    """The oldest current events to shadow so the newest that fit ``budget`` remain.

    Walks from the newest event backwards, keeping events while their running estimate fits
    ``budget``; everything older is returned as the set to shadow. The most recent ordinary event
    is never shadowed. A runtime-context snapshot is superseded by a later durable checkpoint when
    it cannot fit, because the checkpoint retains its source text and avoids duplicating a large
    transient snapshot beside the recovery record.
    """
    kept = 0
    kept_indices: set[int] = set()
    for index in range(len(surface) - 1, -1, -1):
        cost = estimate_tokens(_render(surface[index]))
        is_runtime_context = surface[index].payload.get("form") == _RUNTIME_CONTEXT_FORM
        is_newest = index == len(surface) - 1 and not is_runtime_context
        if not is_newest and kept + cost > budget:
            if is_runtime_context:
                continue
            break
        kept += cost
        kept_indices.add(index)
    candidates = set(range(len(surface))) - kept_indices
    # A pair that crosses the keep boundary remains wholly visible.  This can leave more context
    # than the nominal budget, but detaching a tool result from its call or approval is unsafe.
    for group in _pair_groups(surface):
        if group & candidates and group - candidates:
            candidates.difference_update(group)
    return [event for index, event in enumerate(surface) if index in candidates]


def _summarisation_prompt(shadowed: Sequence[Event]) -> str:
    """The user prompt handed to the summariser: the rendered text of the shadowed events."""
    body = "\n".join(text for event in shadowed if (text := _render(event)))
    return "Summarise these prior steps as evidence for a later runtime step.\n" + frame_untrusted(
        scrub_credentials(body), label="thy-compaction-history"
    )


def compact_surface(events: Sequence[Event], budget: int, ctx: CompactionContext) -> Event | None:
    """Summarise the oldest current surface events and append one ``context.compacted`` event.

    Budget-driven: the newest current events whose estimated size fits ``budget`` are kept; the
    older ones are summarised into a single safe projection and shadowed. The summary is produced
    first and the event appended last, so a crash before the append leaves the surface untouched
    and a crash after it leaves a complete, atomic compaction (ADR-0007) -- there is no opening
    event and no intermediate state.

    Only the summary and the summarisation envelope (provider, model, tier, measured token counts)
    are persisted; the provider's raw completion is never written to the log (ADR-0004/ADR-0006).
    The event is appended ``MODEL_VISIBLE`` and carries the summary under the key ``PromptBuilder``
    renders, so the model sees the summary in place of what was shadowed.

    Args:
        events: the run's events in log order (the full log, not a pre-filtered surface).
        budget: the estimated token size the kept current surface may occupy.
        ctx: the summariser, its routing decision, the log to append to, and the actor.

    Returns:
        The appended ``context.compacted`` event, or ``None`` when the surface holds fewer than two
        events or already fits ``budget`` -- nothing older can be shadowed, so nothing is written.
    """
    surface = current_surface(events)
    if len(surface) < 2:  # noqa: PLR2004  # a single event has nothing older to shadow
        return None
    shadowed = _select_shadowed(surface, budget)
    if not shadowed:
        return None
    active_provider, choice, _ledger = _prepare_model_call(ctx)
    reservation = ctx.concurrent_usage.reserve() if ctx.concurrent_usage is not None else None
    try:
        summary, response = active_provider.complete_structured(
            _summarisation_prompt(shadowed),
            schema=CompactionSummary,
            system=scrub_credentials(COMPACTION_SYSTEM_PROMPT),
        )
    except LLMStructuredOutputError as exc:
        if exc.response is not None:
            _charge_model_response(ctx, exc.response, reservation)
        elif reservation is not None:
            reservation.cancel()
        raise
    except BaseException:
        if reservation is not None:
            reservation.cancel()
        raise
    _charge_model_response(ctx, response, reservation)
    prior = recover_checkpoint(events)
    checkpoint = build_source_checkpoint(
        events,
        primary_request=ctx.primary_request or "",
        current_work=ctx.current_work or "",
        prior=prior,
    )
    source_text = "\n".join(_render(event) for event in shadowed)
    source_tokens = estimate_tokens(source_text)
    before_digest = sha256_text(
        canonical_json({"surface": [event.seq for event in surface], "text": source_text})
    )
    after_text = f"{checkpoint.continuation_header}\n{summary.summary}"
    after_digest = sha256_text(after_text)
    return ctx.event_log.append(
        EventType.CONTEXT_COMPACTED,
        ctx.actor,
        {
            SHADOWED_SEQS_KEY: [event.seq for event in shadowed],
            SUMMARY_TEXT_KEY: summary.summary,
            "checkpoint": checkpoint.model_dump(mode="json"),
            "checkpoint_digest": checkpoint.digest,
            "source_event_ids": [event.event_id for event in shadowed],
            "before_digest": before_digest,
            "after_digest": after_digest,
            "source_tokens": source_tokens,
            "summary_tokens": estimate_tokens(summary.summary),
            "reduced_tokens": max(source_tokens - estimate_tokens(after_text), 0),
            "request_digest": ctx.request_digest,
            "request_tokens": ctx.request_tokens,
            ENVELOPE_KEY: {
                "provider": active_provider.provider_name,
                "model": choice.model,
                "tier": choice.tier_applied.value,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": response.cost_usd,
            },
        },
        surface=EventSurface.MODEL_VISIBLE,
    )


__all__ = [
    "COMPACTION_SYSTEM_PROMPT",
    "ENVELOPE_KEY",
    "MODEL_OUTPUT_CHAR_LIMIT",
    "SUMMARY_TEXT_KEY",
    "CompactionContext",
    "CompactionSummary",
    "ContextCheckpoint",
    "build_source_checkpoint",
    "compact_surface",
    "estimate_events_tokens",
    "estimate_tokens",
    "prune_head_marker_tail",
    "recover_checkpoint",
]
