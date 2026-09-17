"""Run-scoped accounting for requests, tokens and model cost."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from thymira.agents import UsageLimits

_UsageValue = int | float | None


class UsageLedger:
    """Accumulate measured run usage and compare it with shared limits."""

    def __init__(self) -> None:
        self._requests = 0
        self._tokens = 0
        self._tool_calls = 0
        self._cost_usd: float | None = 0.0

    def charge(self, *, requests: int, tokens: int, cost_usd: float | None) -> None:
        """Add one measured usage entry to the ledger.

        An unknown cost makes the accumulated cost unknown, while request and token counts
        remain available for auditing and budget checks.
        """
        self._validate_nonnegative("requests", requests)
        self._validate_nonnegative("tokens", tokens)
        if cost_usd is not None:
            self._validate_nonnegative("cost_usd", cost_usd)
        self._requests += requests
        self._tokens += tokens
        if self._cost_usd is None or cost_usd is None:
            self._cost_usd = None
        else:
            self._cost_usd += cost_usd

    def charge_tool(self) -> None:
        """Record one authorized tool attempt before its implementation runs."""
        self._tool_calls += 1

    def snapshot(self) -> dict[str, _UsageValue]:
        """Return the current usage in the shape accepted by approval requests."""
        return {
            "requests": self._requests,
            "tokens": self._tokens,
            "tool_calls": self._tool_calls,
            "cost_usd": self._cost_usd,
        }

    def exceeds(self, limits: UsageLimits) -> tuple[str, ...]:
        """Return the configured model-usage limits currently exceeded.

        A configured `max_cost_usd` is reported as exceeded both when the measured total is above
        it and when the total is unknown: one unpriced charge poisons the running cost for the
        rest of the run (see `charge`), and from then on the ceiling cannot be shown to hold. A
        budget guard that cannot perform its check escalates rather than reporting the ledger
        clean, so cost tracking degrading to unknown never silently satisfies a cap.

        The unknown case is reported under the same `"max_cost_usd"` name as a measured breach, so
        that every caller testing membership or truthiness escalates without knowing about it;
        callers that need to tell "over budget" from "cannot tell" apart read
        `snapshot()["cost_usd"] is None`, which carries that distinction losslessly and is the
        value already handed to the Gate as `cost_so_far`.

        `RunUsage._enforce` in `thymira.agents.usage` applies the identical rule by raising
        `UsageLimitExceededError`; the two stay separate implementations because a shared helper
        would have to live in `agents` and be imported upwards by `core`, across the layer
        boundary. Change one and change the other.
        """
        exceeded: list[str] = []
        if limits.max_cost_usd is not None and (
            self._cost_usd is None or self._cost_usd > limits.max_cost_usd
        ):
            exceeded.append("max_cost_usd")
        if limits.max_requests is not None and self._requests > limits.max_requests:
            exceeded.append("max_requests")
        if limits.max_tokens is not None and self._tokens > limits.max_tokens:
            exceeded.append("max_tokens")
        if limits.max_tool_calls is not None and self._tool_calls > limits.max_tool_calls:
            exceeded.append("max_tool_calls")
        return tuple(exceeded)

    @staticmethod
    def _validate_nonnegative(name: str, value: int | float) -> None:
        """Reject invalid negative accounting values before changing the ledger."""
        if value < 0:
            msg = f"{name} must be non-negative"
            raise ValueError(msg)


class UsageLedgerRegistry:
    """Hand out one :class:`UsageLedger` per Run, so a rebuilt graph keeps the Run's totals.

    A Run is executed by more than one compiled graph: the execution dispatcher builds one to
    submit the Run and another every time it resumes, and every park -- a tool call waiting for a
    human -- costs one rebuild. A ledger constructed inside the graph factory therefore started at
    zero on the pass after each park, and the Policy Engine's budget decisions saw a Run that had
    spent nothing: any configured ceiling could be walked past simply by parking.

    Usage is measured, not recorded: the event log carries per-agent tokens and cost but no
    request count, so a ledger cannot be folded back out of `events.jsonl` without losing a
    dimension. Keeping the one ledger the Run has been charging is exact, writes nothing new and
    invents no event type. It lives as long as the composition root that owns the factory, which
    is the process that also owns the inline dispatcher; a Run resumed by a *different* process
    starts a fresh ledger, and its budget decisions then see only that process's usage.
    """

    def __init__(self) -> None:
        self._ledgers: dict[str, UsageLedger] = {}

    def for_run(self, run_id: str) -> UsageLedger:
        """Return this Run's ledger, creating it the first time the Run is seen."""
        return self._ledgers.setdefault(run_id, UsageLedger())


__all__ = ["UsageLedger", "UsageLedgerRegistry"]
