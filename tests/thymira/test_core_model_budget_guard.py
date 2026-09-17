"""The post-routing model guard records a role budget decision only when a rule declares it."""

from __future__ import annotations

from thymira.agents.llm.routing import Role, choose
from thymira.core import UsageLedger
from thymira.core.graph.adapters import _model_call_guard_for
from thymira.events import InMemoryEventLog
from thymira.policies import BudgetRule, Gate, Policy, PolicyEngine
from thymira.schemas import Decision, EventType, new_id


def _rule_ids(log: InMemoryEventLog) -> list[str]:
    return [
        event.payload["rule_id"]
        for event in log.events()
        if event.type is EventType.POLICY_DECISION
    ]


def test_without_a_role_budget_rule_the_guard_records_only_the_model_decision() -> None:
    log = InMemoryEventLog(new_id("run"))
    gate = Gate(PolicyEngine(Policy(name="none", version="1.0")), log)

    _model_call_guard_for(log, gate, UsageLedger())(choose(Role.AGENT, "code"))

    assert _rule_ids(log) == ["model_default"]


def test_with_a_role_budget_rule_the_guard_still_records_the_scoped_decision() -> None:
    log = InMemoryEventLog(new_id("run"))
    policy = Policy(
        name="agent-cap",
        version="1.0",
        budget_rules=(
            BudgetRule(
                id="AGENT-CAP",
                decision=Decision.BLOCK,
                reason="agent budget exhausted",
                max_tokens=10_000,
                scope=Role.AGENT.value,
            ),
        ),
    )
    gate = Gate(PolicyEngine(policy), log)

    _model_call_guard_for(log, gate, UsageLedger())(choose(Role.AGENT, "code"))

    assert _rule_ids(log) == ["budget_within_limits", "model_default"]


def test_declares_budget_scope_reflects_the_effective_policy() -> None:
    rule = BudgetRule(
        id="MIRA-CAP", decision=Decision.WARNING, reason="m", max_usd=1.0, scope="mira"
    )
    engine = PolicyEngine(Policy(name="mira-cap", version="1.0", budget_rules=(rule,)))

    assert engine.declares_budget_scope("mira")
    assert not engine.declares_budget_scope("agent")
