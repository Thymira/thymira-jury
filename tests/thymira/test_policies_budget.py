"""POL-01: budget-threshold decisions (decide_budget + Gate.check_budget).

A budget ceiling is a Policy Engine rule, enforced through the same recorded Gate flow as every
other decision (ADR-0004 decision 6). Soft and hard limits are two rules the same usage snapshot
crosses: crossing the soft one is a WARNING, crossing the hard one raises to REQUIRE_HUMAN_REVIEW,
and a BLOCK ceiling still wins on precedence. Usage below every ceiling passes.
"""

from __future__ import annotations

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import (
    BudgetRule,
    Gate,
    GateMode,
    Policy,
    PolicyEngine,
    allows_execution,
    auto_approve,
    pending_approvals,
)
from thymira.schemas import Actor, ActorKind, Decision, EventType, new_id

RUN = new_id("run")

SOFT = BudgetRule(
    id="BUD-SOFT", decision=Decision.WARNING, reason="soft spend ceiling", max_usd=10.0
)
HARD = BudgetRule(
    id="BUD-HARD",
    decision=Decision.REQUIRE_HUMAN_REVIEW,
    reason="hard spend ceiling",
    max_usd=20.0,
)
ABSOLUTE = BudgetRule(
    id="BUD-BLOCK", decision=Decision.BLOCK, reason="absolute spend ceiling", max_usd=50.0
)


def _engine(*budget_rules: BudgetRule) -> PolicyEngine:
    return PolicyEngine(Policy(name="budget", version="1.0", budget_rules=budget_rules))


@pytest.fixture
def engine() -> PolicyEngine:
    return _engine(SOFT, HARD, ABSOLUTE)


def test_usage_below_all_ceilings_passes(engine: PolicyEngine) -> None:
    decision = engine.decide_budget(run_id=RUN, usage={"cost_usd": 5.0})

    assert decision.decision is Decision.PASS
    assert decision.rule_id == "budget_within_limits"
    assert decision.subject_kind == "run"
    assert decision.policy_sha256 == engine.policy_sha256


def test_crossing_a_soft_limit_returns_warning(engine: PolicyEngine) -> None:
    decision = engine.decide_budget(run_id=RUN, usage={"cost_usd": 12.0})

    assert decision.decision is Decision.WARNING
    assert decision.rule_id == "BUD-SOFT"
    assert decision.reason == "soft spend ceiling"
    assert decision.policy_sha256 == engine.policy_sha256


def test_crossing_a_hard_limit_requires_human_review(engine: PolicyEngine) -> None:
    # 25.0 crosses both the soft and the hard ceiling; the stricter decision wins.
    decision = engine.decide_budget(run_id=RUN, usage={"cost_usd": 25.0})

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "BUD-HARD"
    assert decision.policy_sha256 == engine.policy_sha256


def test_block_precedence_is_preserved(engine: PolicyEngine) -> None:
    decision = engine.decide_budget(run_id=RUN, usage={"cost_usd": 55.0})

    assert decision.decision is Decision.BLOCK
    assert decision.rule_id == "BUD-BLOCK"
    assert decision.policy_sha256 == engine.policy_sha256


def test_token_and_tool_call_ceilings_cross_independently() -> None:
    engine = _engine(
        BudgetRule(id="TOK", decision=Decision.WARNING, reason="tokens", max_tokens=1000),
        BudgetRule(id="CALLS", decision=Decision.WARNING, reason="calls", max_tool_calls=5),
    )

    assert engine.decide_budget(run_id=RUN, usage={"tokens": 2000}).rule_id == "TOK"
    assert engine.decide_budget(run_id=RUN, usage={"tool_calls": 8}).rule_id == "CALLS"
    within = engine.decide_budget(run_id=RUN, usage={"tokens": 500, "tool_calls": 2})
    assert within.decision is Decision.PASS


def test_exactly_at_a_ceiling_is_not_crossed(engine: PolicyEngine) -> None:
    # A ceiling is a maximum: being exactly at it is within budget; only exceeding it crosses.
    assert engine.decide_budget(run_id=RUN, usage={"cost_usd": 10.0}).decision is Decision.PASS


def test_an_unmeasured_cost_escalates_instead_of_passing(engine: PolicyEngine) -> None:
    """A cost ceiling whose input degraded to unknown decides, it does not pass.

    This test previously asserted PASS, mirroring what ``UsageLedger.exceeds`` did at the time.
    Both were fail-open: cost tracking degrades to ``None`` on the first unpriced model response,
    so the ceiling stopped being enforced exactly when its input became unknown. The three
    implementations -- here, ``thymira.core``'s ``UsageLedger`` and ``thymira.agents``'s
    ``RunUsage`` -- now escalate on the same condition.
    """
    usage: dict[str, float | int | None] = {"cost_usd": None, "tokens": 100}

    assert engine.decide_budget(run_id=RUN, usage=usage).decision is Decision.BLOCK


def test_scope_selects_which_ceilings_apply() -> None:
    engine = _engine(
        BudgetRule(
            id="RUN-CAP", decision=Decision.WARNING, reason="run", max_usd=10.0, scope="run"
        ),
        BudgetRule(
            id="MIRA-CAP", decision=Decision.BLOCK, reason="mira", max_usd=10.0, scope="mira"
        ),
    )

    run_level = engine.decide_budget(run_id=RUN, usage={"cost_usd": 12.0})
    assert (run_level.decision, run_level.rule_id) == (Decision.WARNING, "RUN-CAP")
    role_level = engine.decide_budget(run_id=RUN, usage={"cost_usd": 12.0}, scope="mira")
    assert (role_level.decision, role_level.rule_id) == (Decision.BLOCK, "MIRA-CAP")


def test_equal_precedence_ties_break_by_policy_order() -> None:
    engine = _engine(
        BudgetRule(id="FIRST", decision=Decision.WARNING, reason="first", max_usd=10.0),
        BudgetRule(id="SECOND", decision=Decision.WARNING, reason="second", max_usd=10.0),
    )

    assert engine.decide_budget(run_id=RUN, usage={"cost_usd": 12.0}).rule_id == "FIRST"


def test_gate_records_a_soft_warning(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)

    decision = Gate(engine, log).check_budget({"cost_usd": 12.0})

    assert decision.decision is Decision.WARNING
    assert allows_execution(decision)
    assert [event.type for event in log.events()] == [EventType.POLICY_DECISION]
    assert log.verify().valid


def test_gate_runs_the_approval_flow_on_a_hard_limit(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)
    human = Actor(kind=ActorKind.HUMAN, id="alice", role="risk-officer")

    decision = Gate(engine, log, approver=auto_approve, human=human).check_budget(
        {"cost_usd": 25.0}, cost_so_far={"cost_usd": 25.0}
    )

    assert decision.requires_human_approval
    assert [event.type for event in log.events()] == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
        EventType.HUMAN_APPROVAL,
    ]
    assert log.verify().valid


def test_a_deferred_gate_leaves_a_pending_budget_approval(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)

    decision = Gate(engine, log, mode=GateMode.DEFERRED).check_budget(
        {"cost_usd": 25.0}, cost_so_far={"cost_usd": 25.0}
    )

    assert decision.requires_human_approval
    pending = pending_approvals(log.events())
    assert len(pending) == 1
    assert pending[0].decision_id == decision.id
    assert pending[0].cost_so_far == {"cost_usd": 25.0}
    assert [event.type for event in log.events()] == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
    ]
    assert log.verify().valid


def test_an_unmeasured_cost_crosses_a_configured_ceiling() -> None:
    """A ceiling whose input degraded to unknown escalates instead of passing.

    LiteLLM reports no price for a new, self-hosted or proxied model, and the usage ledger
    poisons the run total to ``None`` from the first such response. A ceiling that stops being
    enforced exactly when its input becomes unknown is a guard reporting success because it could
    not perform its check. ``UsageLedger.exceeds`` and ``RunUsage`` escalate on the same condition;
    all three are separate implementations because the layer order forbids sharing one.
    """
    assert SOFT.crossed({"cost_usd": None}) is True
    assert HARD.crossed({"cost_usd": None}) is True


def test_an_unmeasured_value_only_crosses_the_ceiling_that_declares_it() -> None:
    """An unknown token count does not cross a rule that only caps spend."""
    tokens_only = BudgetRule(
        id="BUD-TOKENS", decision=Decision.WARNING, reason="token ceiling", max_tokens=1000
    )

    assert SOFT.crossed({"cost_usd": 1.0, "tokens": None}) is False
    assert tokens_only.crossed({"cost_usd": None, "tokens": 10}) is False
    assert tokens_only.crossed({"cost_usd": 1.0, "tokens": None}) is True


def test_a_key_the_snapshot_never_carried_is_not_an_unmeasured_value() -> None:
    """A snapshot that omits a key is different from one whose value is unknown.

    Only the second is a degraded measurement. Treating an absent key as a breach would make every
    rule with a ceiling fire on any snapshot that does not happen to report that dimension.
    """
    assert SOFT.crossed({}) is False
    assert SOFT.crossed({"tokens": 10}) is False
    assert SOFT.crossed({"cost_usd": None}) is True


def test_a_rule_with_no_ceiling_is_never_crossed_by_an_unmeasured_value() -> None:
    """The escalation is tied to a ceiling existing, not to the value being unknown."""
    no_ceiling = BudgetRule(id="BUD-NONE", decision=Decision.BLOCK, reason="declares nothing")

    assert no_ceiling.crossed({"cost_usd": None, "tokens": None, "tool_calls": None}) is False


def test_an_unmeasured_cost_reaches_the_gate_as_the_recorded_decision() -> None:
    """The escalation is consumed, not merely computed: it decides through the real engine."""
    engine = _engine(SOFT, HARD, ABSOLUTE)

    decision = engine.decide_budget(run_id=RUN, usage={"cost_usd": None})

    assert decision.decision is Decision.BLOCK
    assert decision.rule_id == "BUD-BLOCK"
