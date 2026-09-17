"""Token budgeting: estimate a prompt, and compact before a call that would overflow (THY-26).

The trigger for THY-25. ADR-0005 idea 4 -- "measurement declares its confidence" -- is the house
rule here: every token count is a :class:`TokenMeasurement` that says whether it is a provider
``usage`` figure or a provider-free ``estimated`` heuristic, so a caller never mistakes one for the
other. Before a model call only the estimate exists (the prompt has not been sent yet); the
measured ``usage`` baseline is available afterwards, from the provider's own counts.

:class:`ContextBudget` compares the projected prompt against the model's context window and, when
it would overflow, triggers :func:`thymira.thy.compaction.compact_surface` *before* the call and
reassembles the prompt from the now-smaller surface. The estimate reuses
:func:`thymira.thy.compaction.estimate_tokens`, so the size the prompt is judged by and the size
compaction shadows down to are the same unit -- the reassembled prompt fits whenever there was
enough older surface to shadow. What is measured is the prompt as it is *sent*: the rendered
history reaches the model inside one untrusted-data frame, and that wrapper is subtracted from the
budget before compaction picks what to keep (:meth:`ContextBudget._compaction_target`). A retry is
allowed only when the compaction event is durable and the reassembled request is measurably
smaller; an unchanged request stops the budget guard.

Model ids are configuration, never names in code (repository convention), so ``window`` is supplied
by the caller from configuration rather than looked up from a hardcoded model table; ``model`` is
carried on the measurement purely as the label the estimate was sized for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from thymira.agents.prompt_framing import frame_overhead
from thymira.agents.prompts import EVENT_HISTORY_LABEL
from thymira.events import canonical_json, sha256_text
from thymira.thy.compaction import compact_surface, estimate_tokens

DEFAULT_CONTEXT_WINDOW = 180_000
"""Provider-neutral runtime cap for ordinary graph construction, not a model maximum.

Raised from 128_000 (found live 2026-09-13): a Coding-agent report-compilation task under full
review friction (`coding-agent-max-turns-raised-for-review-friction.md`'s same unresolved
condition) failed with `end_reason=context_budget` at 132,077 estimated tokens -- over the old
128_000 cap but far under any currently configured model's real window (Sonnet 5 and Opus 5 are
1,000,000; Haiku 4.5, the smallest, is 200,000). The old cap was not protecting against a real
provider limit; it was a stale, disconnected-from-reality ceiling that a single long-running,
fully-gated task could reach well before the model itself would refuse the request. 180_000 keeps
a real margin under Haiku 4.5's 200,000-token window (the smallest window any configured tier can
resolve to) for the untrusted-data-frame wrapper this module already reserves against, while giving
a fully-gated task roughly 40% more room before the same failure recurs. This does not fix why a
resumed step cannot compact (see the same finding's note) -- it only widens the ceiling that gap
runs into.
"""

_COMPACTION_PROJECTION_RESERVE = 64
"""Estimated tokens reserved for the bounded checkpoint header and summary projection."""

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.agents.llm.base import LLMResponse
    from thymira.agents.prompts import AssembledPrompt, PromptBuilder, PromptEnvironment
    from thymira.agents.spec import AgentSpec
    from thymira.schemas import Event, Task
    from thymira.thy.compaction import CompactionContext


class MeasurementBaseline(StrEnum):
    """Where a token count came from -- a provider ``usage`` figure or an ``estimated`` heuristic.

    The confidence a measurement declares (ADR-0005 idea 4). ``USAGE`` is measured (the provider
    counted it); ``ESTIMATED`` is a provider-free heuristic computed before the call.
    """

    USAGE = "usage"
    ESTIMATED = "estimated"


@dataclass(frozen=True, slots=True)
class TokenMeasurement:
    """A token count that declares its own confidence.

    ``model`` is the label the count was sized for; it never changes the arithmetic (no model
    table lives in the code), it only records which model the figure describes.
    """

    tokens: int
    baseline: MeasurementBaseline
    model: str = ""

    @classmethod
    def from_usage(cls, response: LLMResponse, *, model: str = "") -> TokenMeasurement:
        """A measured prompt-token count from a provider response (baseline ``usage``)."""
        return cls(
            tokens=response.input_tokens,
            baseline=MeasurementBaseline.USAGE,
            model=model or response.model,
        )


def estimate_prompt_tokens(assembled: AssembledPrompt, model: str) -> TokenMeasurement:
    """Estimate the tokens ``assembled`` will occupy for ``model`` (baseline ``estimated``).

    A provider-free heuristic over the rendered system prefix and user body (see
    :func:`thymira.thy.compaction.estimate_tokens`). The result is tagged ``estimated`` and carries
    ``model`` so an auditor sees which model the estimate was sized for; the measured ``usage``
    baseline comes from the provider's own counts after the call, via
    :meth:`TokenMeasurement.from_usage`.
    """
    tokens = estimate_tokens(assembled.system) + estimate_tokens(assembled.user)
    return TokenMeasurement(tokens=tokens, baseline=MeasurementBaseline.ESTIMATED, model=model)


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    """The result of fitting a prompt to the budget.

    ``events`` is the surface source to send the call with (unchanged, or the post-compaction log);
    ``assembled`` is the prompt to send; ``measurement`` is its estimated size; ``compacted`` is the
    last ``context.compacted`` event when compaction ran, or ``None`` when the prompt already fit.
    ``progressed`` records durable measured reduction, ``retries`` counts compaction attempts, and
    ``stopped`` records that the prompt remained over budget or made no progress.
    """

    events: list[Event]
    assembled: AssembledPrompt
    measurement: TokenMeasurement
    compacted: Event | None
    progressed: bool = False
    retries: int = 0
    stopped: bool = False


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Guards one model call against overflowing the model's context window.

    ``window`` is the model's context size in tokens, supplied from configuration. ``reserve``
    holds back tokens for the model's own response so the prompt is not sized to the very edge of
    the window; the prompt must fit within :attr:`limit` (``window - reserve``).
    """

    window: int
    model: str = ""
    reserve: int = 0
    max_retries: int = 2

    def __post_init__(self) -> None:
        """Reject invalid runtime caps before a graph can dispatch a model call."""
        if isinstance(self.window, bool) or not isinstance(self.window, int) or self.window <= 0:
            raise ValueError("context window must be a positive finite integer")
        if isinstance(self.reserve, bool) or not isinstance(self.reserve, int) or self.reserve < 0:
            raise ValueError("context reserve must be a non-negative integer")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("context max_retries must be a non-negative integer")

    @property
    def limit(self) -> int:
        """The largest prompt, in estimated tokens, permitted before compaction is triggered."""
        return max(1, self.window - self.reserve)

    def measure(self, assembled: AssembledPrompt) -> TokenMeasurement:
        """The estimated size of ``assembled`` under this budget's model."""
        return estimate_prompt_tokens(assembled, self.model)

    def exceeds(self, assembled: AssembledPrompt) -> bool:
        """Whether ``assembled``'s projected size is over :attr:`limit`."""
        return self.measure(assembled).tokens > self.limit

    def fit(
        self,
        builder: PromptBuilder,
        spec: AgentSpec,
        task: Task,
        events: Sequence[Event],
        ctx: CompactionContext,
        *,
        environment: PromptEnvironment | None = None,
        selected_skill_names: Sequence[str] = (),
        runtime_skill_request_id: str | None = None,
        runtime_skill_dispatch_id: str | None = None,
        full_checkpoint_fidelity: bool = False,
    ) -> BudgetOutcome:
        """Assemble the prompt; compact only after durable, measured progress.

        Compaction happens *before* the model call: the returned :class:`BudgetOutcome` carries the
        prompt to send and the surface to send it with. A retry is allowed only after the event is
        durable and the reassembled request is smaller under the same estimate. When the prompt
        already fits, nothing is appended and ``compacted`` is ``None``.

        Args:
            builder: assembles a prompt from a spec, a task and a surface.
            spec: the agent whose system prefix is used.
            task: the task whose objective tails the prompt.
            events: the run's events in log order, the surface the prompt is built from.
            ctx: the compaction dependencies (summariser, routing choice, log, actor).
            environment: optional run-stable persona and tool guidance used for both measurements.
            selected_skill_names: runtime skill names threaded onto every rebuilt prompt so
                compaction never drops the caller's runtime-catalog selection.
            runtime_skill_request_id: threaded onto every rebuilt prompt, unchanged.
            runtime_skill_dispatch_id: threaded onto every rebuilt prompt, unchanged.
            full_checkpoint_fidelity: when true, the first attempt's checkpoint projection --
                before whether compaction is even needed is known -- retains every source section
                (``checkpoint_max_chars=None``) instead of this budget's bounded projection,
                matching :meth:`~thymira.agents.prompts.PromptBuilder.build`'s own contract for a
                resumed conversation. Every retry after compaction actually starts still bounds the
                checkpoint exactly like a non-resume call, regardless of this flag.

        Returns:
            The prompt to send, the surface to send it with, its estimated size, and the
            ``context.compacted`` event if one was appended. ``stopped`` may be true when no
            durable reduction was possible; callers must preserve that outcome rather than retrying
            unchanged.
        """
        first_attempt_checkpoint_chars = (
            None if full_checkpoint_fidelity else self._checkpoint_projection_chars
        )
        assembled = builder.build(
            spec,
            task,
            events,
            environment=environment,
            checkpoint_max_chars=first_attempt_checkpoint_chars,
            selected_skill_names=selected_skill_names,
            runtime_skill_request_id=runtime_skill_request_id,
            runtime_skill_dispatch_id=runtime_skill_dispatch_id,
        )
        before = estimate_prompt_tokens(assembled, self.model)
        if before.tokens <= self.limit:
            return BudgetOutcome(list(events), assembled, before, compacted=None)
        target = self._compaction_target(assembled, task)
        current_events = list(events)
        last_compacted: Event | None = None
        retries = 0
        progressed = False
        stopped = False
        while before.tokens > self.limit and retries < max(self.max_retries, 1):
            request_digest = sha256_text(
                canonical_json({"system": assembled.system, "user": assembled.user})
            )
            causal_ctx = replace(
                ctx,
                request_digest=request_digest,
                request_tokens=before.tokens,
                primary_request=task.objective,
                current_work=task.objective,
            )
            compacted = compact_surface(current_events, target, causal_ctx)
            last_compacted = compacted or last_compacted
            reassembled_events = ctx.event_log.events()
            reassembled = builder.build(
                spec,
                task,
                reassembled_events,
                environment=environment,
                checkpoint_max_chars=self._checkpoint_projection_chars,
                selected_skill_names=selected_skill_names,
                runtime_skill_request_id=runtime_skill_request_id,
                runtime_skill_dispatch_id=runtime_skill_dispatch_id,
            )
            after = estimate_prompt_tokens(reassembled, self.model)
            durable = (
                compacted is not None
                and any(event.event_id == compacted.event_id for event in reassembled_events)
                and compacted.payload.get("request_digest") == request_digest
                and compacted.payload.get("request_tokens") == before.tokens
            )
            measured_reduction = after.tokens < before.tokens
            attempt_progressed = durable and measured_reduction
            progressed = progressed or attempt_progressed
            retries += 1
            current_events = reassembled_events
            assembled = reassembled
            if not attempt_progressed:
                stopped = True
                break
            if after.tokens >= before.tokens:
                stopped = True
                break
            before = after
            if before.tokens <= self.limit:
                break
        if before.tokens > self.limit and retries >= max(self.max_retries, 1):
            stopped = True
        return BudgetOutcome(
            current_events,
            assembled,
            before,
            compacted=last_compacted,
            progressed=progressed,
            retries=retries,
            stopped=stopped,
        )

    def _compaction_target(self, assembled: AssembledPrompt, task: Task) -> int:
        """The surface-token budget for compaction: the limit less the fixed prompt overhead.

        Three things are present in every assembled prompt and cannot be compacted away: the
        system prefix, the task objective, and the untrusted-data frame
        :class:`~thymira.agents.PromptBuilder` wraps the rendered history in (one
        ``EVENT_HISTORY_LABEL`` frame, a fixed wrapper independent of how many events survive).
        All three are subtracted from the limit; what remains is how many tokens of history (kept
        events plus the new summary) may survive. The frame is *measured* through
        :func:`~thymira.agents.prompt_framing.frame_overhead` rather than estimated, so the size
        compaction shadows down to and the size of the prompt actually sent are the same unit --
        without it the surface would be trimmed to fit a prompt the builder never assembles.
        ``reserve`` is held back again so the summary the compaction adds does not push the
        reassembled prompt back over the window.
        """
        overhead = (
            estimate_tokens(assembled.system)
            + estimate_tokens(task.objective)
            + estimate_tokens(frame_overhead(EVENT_HISTORY_LABEL))
        )
        return max(1, self.limit - overhead - self.reserve - _COMPACTION_PROJECTION_RESERVE)

    @property
    def _checkpoint_projection_chars(self) -> int:
        """Bound a checkpoint projection by the same runtime cap used for the request.

        The durable event retains the complete source checkpoint. Only its model projection is
        shortened here, leaving enough room for the system prompt, task objective and summary when
        a small configured runtime cap is used.
        """
        return max(32, min(240, self.limit))


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "BudgetOutcome",
    "ContextBudget",
    "MeasurementBaseline",
    "TokenMeasurement",
    "estimate_prompt_tokens",
]
