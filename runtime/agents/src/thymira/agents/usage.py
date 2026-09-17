"""RunUsage / UsageLimits: shared budget charged per delegated call (THY-07).

ADR-0004 dec.6: budgets are shared across a run and enforced at a hard ceiling. `UsageLimits` is
**the single definition of that type for the whole runtime** — `RA-CORE-03`'s `UsageLedger`
imports it from here, never the other way, because `thymira.core` sits above `thymira.agents` in
the layer order. If `RA-CORE-03` lands first and already created this module, extend it; the type
is never redefined in `runtime/core`.

`RunUsage` is a mutable, run-scoped accumulator — not a `ThymiraModel`: it exists to be shared and
charged into repeatedly across every delegated call in a run (`AgentContext.usage`, threaded
through many `AgentRunner.run()` calls), which a frozen contract cannot do without a new instance
per charge. `.charge()` takes the real `LLMResponse` a provider returned, never a token estimate,
so cost tracking reflects what the run actually spent; `.charge_tool()` counts one tool call. Both
raise `UsageLimitExceededError` the moment a configured ceiling is crossed, so the caller's next
call never happens — MVP enforces only the hard ceiling; a soft-limit WARNING before it is a
Policy Engine rule (P4, FINAL) reading this same accumulator, not a change to this type.

**Unknown cost is a breach, not a pass.** `LLMResponse.cost_usd` is `None` whenever LiteLLM has no
price for the model — every new, self-hosted or proxied model. Coercing that to `0.0` would make
`max_cost_usd` a ceiling that can never be reached, so `cost_usd` degrades to `None` instead and a
*configured* cap over an unknown total raises: a ceiling that can no longer be shown to hold has
not been shown to hold. This is the same rule `thymira.core.usage.UsageLedger` applies, stated
twice on purpose. The two are **not** merged into a shared helper because `thymira.core` sits
*above* `thymira.agents` in the layer order — a shared helper would have to live here or below and
be imported upward, coupling two members across a layer boundary for a three-line rule — and
because they react differently in kind: the ledger *reports* which limits are breached, this type
*enforces* by raising. Change the rule here and you must change it there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Condition
from typing import TYPE_CHECKING

from thymira.schemas import ThymiraModel

if TYPE_CHECKING:
    from thymira.agents.llm.base import LLMResponse


class UsageLimits(ThymiraModel):
    """A run's hard ceilings. A field left `None` is unenforced."""

    max_cost_usd: float | None = None
    max_requests: int | None = None
    max_tokens: int | None = None
    max_tool_calls: int | None = None


class UsageLimitExceededError(RuntimeError):
    """A charge crossed one of `UsageLimits`' configured ceilings."""


@dataclass
class RunUsage:
    """Mutable accumulator of what a run's delegated calls actually cost, charged as they happen."""

    limits: UsageLimits = field(default_factory=UsageLimits)
    cost_usd: float | None = 0.0
    """Total USD charged so far, or `None` once any charge could not be priced."""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens charged so far."""
        return self.input_tokens + self.output_tokens

    def ensure_request_available(self) -> None:
        """Reject a request that would exceed the configured request ceiling."""
        limit = self.limits.max_requests
        if limit is not None and self.requests >= limit:
            raise UsageLimitExceededError(f"max_requests exceeded: {self.requests + 1} > {limit}")

    def charge(self, response: LLMResponse) -> None:
        """Charge one model call's real cost and tokens.

        Raises:
            UsageLimitExceededError: The charge just applied crossed `max_cost_usd`,
                `max_requests` or `max_tokens`. The charge itself still stands — the call already
                happened — only the *next* one is prevented, by this exception propagating out of
                the caller.
        """
        self.add_cost(response.cost_usd)
        self.requests += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self._enforce()

    def add_cost(self, cost_usd: float | None) -> None:
        """Accumulate one charge's cost, degrading the total to unknown when it has no price.

        Separate from `charge` so a caller that merges an already-charged accumulator — THY's
        parallel fan-in (`thymira.thy.nodes.execute`) folds each isolated task's usage back into
        the run's shared one — inherits the unknown instead of dropping it. Merging does not
        re-enforce: the isolated task already enforced the same `limits` at charge time, and the
        next `charge` on this accumulator enforces the merged total.

        Args:
            cost_usd: The charge's cost in USD, or `None` when the model's price is unknown.
        """
        if self.cost_usd is None or cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd += cost_usd

    def charge_tool(self) -> None:
        """Charge one tool call.

        Raises:
            UsageLimitExceededError: The charge just applied crossed `max_tool_calls`.
        """
        self.tool_calls += 1
        self._enforce()

    def _enforce(self) -> None:
        self._enforce_cost()
        checks: tuple[tuple[int | None, int, str], ...] = (
            (self.limits.max_requests, self.requests, "max_requests"),
            (self.limits.max_tokens, self.total_tokens, "max_tokens"),
            (self.limits.max_tool_calls, self.tool_calls, "max_tool_calls"),
        )
        for limit, actual, name in checks:
            if limit is not None and actual > limit:
                msg = f"{name} exceeded: {actual} > {limit}"
                raise UsageLimitExceededError(msg)

    def _enforce_cost(self) -> None:
        """Enforce `max_cost_usd`, treating an unknown accumulated cost as a breach.

        Split out of the uniform `_enforce` loop because cost is the one ceiling whose measurement
        can go missing: the other three are counters this type increments itself and always knows.
        """
        limit = self.limits.max_cost_usd
        if limit is None:
            return
        cost = self.cost_usd
        if cost is None:
            msg = (
                "max_cost_usd exceeded: cost is unknown (a model call reported no price), "
                f"so the {limit} ceiling can no longer be shown to hold"
            )
            raise UsageLimitExceededError(msg)
        if cost > limit:
            msg = f"max_cost_usd exceeded: {cost} > {limit}"
            raise UsageLimitExceededError(msg)


class ConcurrentRunUsage:
    """Reserve and settle one run's provider calls safely across worker threads.

    Request slots are reserved atomically, so siblings cannot all observe the same remaining
    ``max_requests`` allowance. A provider response has no trustworthy token or cost upper bound
    before it arrives, so a finite token or cost ceiling permits only one unknown-size request in
    flight; unconstrained and request-count-only runs retain useful provider concurrency.

    This coordinator is execution-local rather than persisted. The wrapped :class:`RunUsage`
    remains the authoritative accumulator and is charged as soon as each provider response
    arrives, while task-local accumulators may still describe individual agent results.
    """

    def __init__(self, usage: RunUsage) -> None:
        self._usage = usage
        self._condition = Condition()
        self._in_flight = 0

    def reserve(self) -> RunUsageReservation:
        """Reserve capacity for one provider call, waiting only on a temporary contender."""
        with self._condition:
            while not self._has_dispatch_capacity():
                self._raise_if_capacity_is_exhausted()
                self._condition.wait()
            self._raise_if_capacity_is_exhausted()
            self._in_flight += 1
        return RunUsageReservation(self)

    def _has_dispatch_capacity(self) -> bool:
        request_limit = self._usage.limits.max_requests
        if request_limit is not None and self._usage.requests + self._in_flight >= request_limit:
            return False
        serial_unknown_size = (
            self._usage.limits.max_tokens is not None or self._usage.limits.max_cost_usd is not None
        )
        return not serial_unknown_size or self._in_flight == 0

    def _raise_if_capacity_is_exhausted(self) -> None:
        limits = self._usage.limits
        if limits.max_requests is not None and self._usage.requests >= limits.max_requests:
            raise UsageLimitExceededError(
                f"max_requests exceeded: {self._usage.requests + 1} > {limits.max_requests}"
            )
        if limits.max_tokens is not None and self._usage.total_tokens >= limits.max_tokens:
            raise UsageLimitExceededError(
                "max_tokens exceeded: no request capacity remains at "
                f"{self._usage.total_tokens} >= {limits.max_tokens}"
            )
        if limits.max_cost_usd is not None:
            cost = self._usage.cost_usd
            if cost is None:
                raise UsageLimitExceededError(
                    "max_cost_usd exceeded: cost is unknown, so no further request can be "
                    "authorised"
                )
            if cost >= limits.max_cost_usd:
                raise UsageLimitExceededError(
                    "max_cost_usd exceeded: no request capacity remains at "
                    f"{cost} >= {limits.max_cost_usd}"
                )

    def _charge(self, response: LLMResponse) -> None:
        with self._condition:
            try:
                self._usage.charge(response)
            finally:
                self._in_flight -= 1
                self._condition.notify_all()

    def _cancel(self) -> None:
        with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()


class RunUsageReservation:
    """A one-shot provider-call reservation owned by :class:`ConcurrentRunUsage`."""

    def __init__(self, coordinator: ConcurrentRunUsage) -> None:
        self._coordinator = coordinator
        self._active = True

    def charge(self, response: LLMResponse) -> None:
        """Settle the reservation with the response the provider actually returned."""
        if not self._active:
            raise RuntimeError("run usage reservation has already been settled")
        self._active = False
        self._coordinator._charge(response)  # noqa: SLF001  # this reservation is the coordinator's own handle

    def cancel(self) -> None:
        """Release a reservation when dispatch produced no billable response."""
        if not self._active:
            return
        self._active = False
        self._coordinator._cancel()  # noqa: SLF001  # this reservation is the coordinator's own handle


__all__ = [
    "ConcurrentRunUsage",
    "RunUsage",
    "RunUsageReservation",
    "UsageLimitExceededError",
    "UsageLimits",
]
