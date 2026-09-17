"""Tests for the core run usage ledger.

Paired suite: `tests/thymira/test_agents_usage.py` covers `RunUsage` in
`runtime/agents/src/thymira/agents/usage.py`, which enforces the *same* rule -- a configured cost
cap plus an unknown cost is a breach, never a pass -- by raising `UsageLimitExceededError` instead
of naming the limit. The two implementations stay separate on purpose (a shared helper would have
to sit in `agents` and be imported upwards by `core`, across the layer boundary), so any change to
the rule here needs the mirrored change there, and vice versa.
"""

from __future__ import annotations

import pytest

from thymira.agents import UsageLimits
from thymira.core import UsageLedger, UsageLedgerRegistry
from thymira.events import InMemoryEventLog
from thymira.policies import ApprovalRequest, Gate, PolicyEngine, load_policy_stack
from thymira.schemas import new_id


def test_usage_ledger_keeps_counts_when_one_cost_is_unknown() -> None:
    """Unknown model cost does not erase measured request and token totals."""
    ledger = UsageLedger()

    ledger.charge(requests=1, tokens=500, cost_usd=0.02)
    ledger.charge(requests=1, tokens=700, cost_usd=None)

    assert ledger.snapshot() == {"requests": 2, "tokens": 1200, "tool_calls": 0, "cost_usd": None}


def test_usage_ledger_reports_each_breached_model_limit() -> None:
    """The ledger names every configured model-usage limit that has been exceeded."""
    ledger = UsageLedger()
    ledger.charge(requests=2, tokens=1200, cost_usd=0.20)

    limits = UsageLimits(max_cost_usd=0.10, max_requests=1, max_tokens=1000)

    assert ledger.exceeds(limits) == ("max_cost_usd", "max_requests", "max_tokens")


def test_usage_ledger_reports_nothing_while_every_known_limit_holds() -> None:
    """Measured usage below every configured ceiling breaches nothing."""
    ledger = UsageLedger()
    ledger.charge(requests=1, tokens=500, cost_usd=0.01)

    limits = UsageLimits(max_cost_usd=0.10, max_requests=5, max_tokens=1000)

    assert ledger.exceeds(limits) == ()


def test_usage_ledger_reports_cost_cap_breached_once_cost_becomes_unknown() -> None:
    """A configured cost cap plus an unknown total is a breach, not a pass.

    One unpriced charge degrades the ledger's cost to `None` for the rest of the run. The ceiling
    can no longer be shown to hold, and a guard that cannot perform its check escalates rather
    than reporting the ledger clean.
    """
    ledger = UsageLedger()
    ledger.charge(requests=1, tokens=500, cost_usd=0.01)
    ledger.charge(requests=1, tokens=500, cost_usd=None)

    limits = UsageLimits(max_cost_usd=1000.0, max_requests=100, max_tokens=100_000)

    assert ledger.snapshot()["cost_usd"] is None
    assert ledger.exceeds(limits) == ("max_cost_usd",)


def test_usage_ledger_ignores_unknown_cost_when_no_cost_cap_is_configured() -> None:
    """The escalation is tied to a cap existing, not to the cost being unknown."""
    ledger = UsageLedger()
    ledger.charge(requests=1, tokens=500, cost_usd=None)

    limits = UsageLimits(max_requests=100, max_tokens=100_000)

    assert ledger.snapshot()["cost_usd"] is None
    assert ledger.exceeds(limits) == ()


def test_usage_ledger_reports_unknown_cost_cap_alongside_other_breached_limits() -> None:
    """An unknown cost is named next to the limits that are measurably breached."""
    ledger = UsageLedger()
    ledger.charge(requests=2, tokens=1200, cost_usd=None)

    limits = UsageLimits(max_cost_usd=10.0, max_requests=1, max_tokens=1000)

    assert ledger.exceeds(limits) == ("max_cost_usd", "max_requests", "max_tokens")


@pytest.mark.parametrize(
    ("requests", "tokens", "cost_usd"),
    [(-1, 0, 0.0), (0, -1, 0.0), (0, 0, -0.01)],
)
def test_usage_ledger_rejects_negative_accounting_values(
    requests: int, tokens: int, cost_usd: float
) -> None:
    """Invalid usage cannot change the ledger."""
    ledger = UsageLedger()

    with pytest.raises(ValueError, match="non-negative"):
        ledger.charge(requests=requests, tokens=tokens, cost_usd=cost_usd)

    assert ledger.snapshot() == {"requests": 0, "tokens": 0, "tool_calls": 0, "cost_usd": 0.0}


def test_usage_snapshot_flows_into_gate_approval_request() -> None:
    """A ledger snapshot is preserved as the approval request's cost context."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    seen: list[dict[str, int | float | None] | None] = []
    ledger = UsageLedger()

    def approver(request: ApprovalRequest) -> bool:
        """Capture the request context without making an authorization decision."""
        seen.append(request.cost_so_far)
        return True

    ledger.charge(requests=2, tokens=1000, cost_usd=0.05)
    gate = Gate(PolicyEngine(load_policy_stack("credit_risk")), log, approver=approver)

    gate.check_action(
        subject_kind="run",
        subject_id=run_id,
        action_type="final_decision",
        cost_so_far=ledger.snapshot(),
    )

    assert seen == [{"requests": 2, "tokens": 1000, "tool_calls": 0, "cost_usd": 0.05}]


def test_unknown_cost_reaches_the_gate_approval_request_as_unknown() -> None:
    """The reviewer sees the unknown cost the ledger reports as a breached cap.

    `thymira.core.graph.compose` hands the Gate `usage_ledger.snapshot()` as `cost_so_far`, so the
    escalation `exceeds()` raises has a consumer: the human resolving the approval request reads
    `cost_usd: None` rather than a number that understates what the run spent.
    """
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    seen: list[dict[str, int | float | None] | None] = []
    ledger = UsageLedger()

    def approver(request: ApprovalRequest) -> bool:
        """Capture the request context without making an authorization decision."""
        seen.append(request.cost_so_far)
        return True

    ledger.charge(requests=1, tokens=600, cost_usd=0.05)
    ledger.charge(requests=1, tokens=400, cost_usd=None)
    gate = Gate(PolicyEngine(load_policy_stack("credit_risk")), log, approver=approver)

    gate.check_action(
        subject_kind="run",
        subject_id=run_id,
        action_type="final_decision",
        cost_so_far=ledger.snapshot(),
    )

    assert ledger.exceeds(UsageLimits(max_cost_usd=1000.0)) == ("max_cost_usd",)
    assert seen == [{"requests": 2, "tokens": 1000, "tool_calls": 0, "cost_usd": None}]


def test_the_registry_hands_one_run_the_same_ledger_every_time() -> None:
    """A Run's totals live in one ledger, so a graph rebuilt for it keeps what it has spent."""
    registry = UsageLedgerRegistry()

    registry.for_run("run_a").charge(requests=1, tokens=10, cost_usd=0.01)
    registry.for_run("run_a").charge_tool()

    assert registry.for_run("run_a").snapshot() == {
        "requests": 1,
        "tokens": 10,
        "tool_calls": 1,
        "cost_usd": 0.01,
    }


def test_the_registry_keeps_two_runs_apart() -> None:
    """One Run's spending is never charged to another."""
    registry = UsageLedgerRegistry()

    registry.for_run("run_a").charge(requests=1, tokens=10, cost_usd=0.01)

    assert registry.for_run("run_b").snapshot() == {
        "requests": 0,
        "tokens": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
    }
