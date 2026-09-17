"""The model router: tasks map to tiers, roles have floors, every choice is explainable."""

from __future__ import annotations

import os

import pytest

from thymira.agents.llm import routing
from thymira.agents.llm.routing import RANK, ModelTier, Role, choose, model_for, set_model_overrides

ALL_VARS = (
    "THYMIRA_MODEL",
    "THYMIRA_ORCHESTRATOR_MODEL",
    "THYMIRA_AGENT_MODEL",
    "THYMIRA_THY_MODEL",
    "THYMIRA_MIRA_MODEL",
    "THYMIRA_MODEL_FRONTIER",
    "THYMIRA_MODEL_STANDARD",
    "THYMIRA_MODEL_FAST",
    "THYMIRA_THY_MIN_TIER",
    "THYMIRA_MIRA_MIN_TIER",
    "THYMIRA_AGENT_MIN_TIER",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for variable in ALL_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("THYMIRA_MODEL_FRONTIER", "vendor-a/frontier")
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "vendor-b/standard")
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "vendor-b/fast")
    return monkeypatch


def test_each_orchestrator_can_run_on_its_own_frontier_model(env: pytest.MonkeyPatch) -> None:
    env.setenv("THYMIRA_THY_MODEL", "vendor-a/frontier-for-thy")
    env.setenv("THYMIRA_MIRA_MODEL", "vendor-c/frontier-for-mira")
    assert choose(Role.THY, "plan").model == "vendor-a/frontier-for-thy"
    assert choose(Role.MIRA, "audit_judgement").model == "vendor-c/frontier-for-mira"
    # A sub-agent's frontier work uses the tier model, never an orchestrator's override.
    assert choose(Role.AGENT, "synthesize").model == "vendor-a/frontier"


def test_mechanical_tasks_go_to_the_fast_tier_and_are_recorded(env: pytest.MonkeyPatch) -> None:
    choice = choose(Role.AGENT, "select_tool")
    assert choice.tier_applied is ModelTier.FAST
    assert choice.model == "vendor-b/fast"
    payload = choice.event_payload()
    assert payload["task"] == "select_tool"
    assert payload["reason"] == "select_tool: tier table"


def test_a_requested_downgrade_is_clamped_to_the_role_floor(env: pytest.MonkeyPatch) -> None:
    # MIRA may ask for a cheap summary, but its floor is STANDARD by default.
    choice = choose(Role.MIRA, "summarize", requested_tier=ModelTier.FAST)
    assert choice.tier_requested is ModelTier.FAST
    assert choice.tier_applied is ModelTier.STANDARD
    assert "raised to the mira floor" in choice.reason
    # THY's floor is FAST: the same request goes through as asked.
    assert (
        choose(Role.THY, "summarize", requested_tier=ModelTier.FAST).tier_applied is ModelTier.FAST
    )
    # Floors are configuration too.
    env.setenv("THYMIRA_THY_MIN_TIER", "frontier")
    assert choose(Role.THY, "summarize").tier_applied is ModelTier.FRONTIER


def test_an_env_floor_may_not_lower_a_role_below_its_default(env: pytest.MonkeyPatch) -> None:
    # An operator may harden a floor, never soften one: MIRA's audit must not run cheaper than
    # the work it audits, whatever THYMIRA_MIRA_MIN_TIER says.
    env.setenv("THYMIRA_MIRA_MIN_TIER", "fast")
    assert routing.floor_for(Role.MIRA) is ModelTier.STANDARD
    choice = choose(Role.MIRA, "summarize")
    assert choice.tier_applied is ModelTier.STANDARD
    assert choice.model == "vendor-b/standard"


def test_an_env_floor_may_still_raise_a_role_above_its_default(env: pytest.MonkeyPatch) -> None:
    env.setenv("THYMIRA_AGENT_MIN_TIER", "frontier")
    assert routing.floor_for(Role.AGENT) is ModelTier.FRONTIER
    choice = choose(Role.AGENT, "summarize")
    assert choice.tier_applied is ModelTier.FRONTIER
    assert choice.model == "vendor-a/frontier"


def test_the_recorded_reason_names_a_refused_and_an_accepted_env_floor(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("THYMIRA_MIRA_MIN_TIER", "fast")
    refused = choose(Role.MIRA, "summarize")
    assert "THYMIRA_MIRA_MIN_TIER" in refused.reason
    assert "REFUSED" in refused.reason
    assert "STANDARD" in refused.reason
    assert refused.event_payload()["reason"] == refused.reason

    env.setenv("THYMIRA_MIRA_MIN_TIER", "frontier")
    accepted = choose(Role.MIRA, "summarize")
    assert accepted.tier_applied is ModelTier.FRONTIER
    assert "THYMIRA_MIRA_MIN_TIER" in accepted.reason
    assert "raised" in accepted.reason
    assert "REFUSED" not in accepted.reason


@pytest.mark.parametrize("role", sorted(routing.DEFAULT_FLOORS, key=str))
@pytest.mark.parametrize("requested", list(ModelTier))
def test_no_role_floor_can_be_lowered_by_configuration(
    env: pytest.MonkeyPatch, role: Role, requested: ModelTier
) -> None:
    default = routing.DEFAULT_FLOORS[role]
    env.setenv(routing.ROLE_FLOOR_ENV_VARS[role], requested.value.lower())
    expected = requested if RANK[requested] > RANK[default] else default
    assert routing.floor_for(role) is expected
    # The cheapest task in the table can never drag the role under its mandatory floor.
    choice = choose(role, "format")
    assert RANK[choice.tier_applied] >= RANK[default]
    if RANK[requested] < RANK[default]:
        assert "REFUSED" in choice.reason


def test_fallback_chain_and_unconfigured_tier(env: pytest.MonkeyPatch) -> None:
    env.delenv("THYMIRA_MODEL_FAST")
    assert model_for(Role.AGENT, ModelTier.FAST) == "vendor-b/standard"
    env.delenv("THYMIRA_MODEL_STANDARD")
    env.setenv("THYMIRA_AGENT_MODEL", "vendor-b/agent")
    assert model_for(Role.AGENT, ModelTier.FAST) == "vendor-b/agent"
    env.delenv("THYMIRA_AGENT_MODEL")
    env.delenv("THYMIRA_MODEL_FRONTIER")
    assert model_for(Role.THY, ModelTier.FRONTIER) is None
    choice = choose(Role.THY, "plan")
    assert choice.model == "<unconfigured>"
    assert "no model configured for FRONTIER" in choice.reason


def test_unknown_task_kind_is_rejected(env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown task kind"):
        choose(Role.AGENT, "guess")
    assert set(routing.DEFAULT_TIER_BY_TASK) >= {"plan", "select_tool", "summarize", "code"}


def test_a_live_override_wins_over_the_environment(env: pytest.MonkeyPatch) -> None:
    # The settings-backed live override (apps/api's settings PUT refreshes this cache immediately)
    # is consulted before os.environ, so a UI-set model takes effect without a process restart.
    set_model_overrides({"THYMIRA_MODEL_FAST": "vendor-live/fast"})
    assert model_for(Role.AGENT, ModelTier.FAST) == "vendor-live/fast"
    assert choose(Role.AGENT, "select_tool").model == "vendor-live/fast"
    # The environment variable itself is untouched -- this is a resolution-order change in
    # routing's own lookup, never a mutation of the process environment.
    assert os.environ.get("THYMIRA_MODEL_FAST") == "vendor-b/fast"


def test_an_unset_override_key_falls_back_to_the_environment(env: pytest.MonkeyPatch) -> None:
    # Overriding one variable must not blind routing to every other -- only THYMIRA_MODEL_FAST is
    # overridden here, so THYMIRA_MODEL_STANDARD keeps resolving from the environment as before.
    set_model_overrides({"THYMIRA_MODEL_FAST": "vendor-live/fast"})
    assert model_for(Role.AGENT, ModelTier.STANDARD) == "vendor-b/standard"


def test_an_empty_override_value_does_not_shadow_the_environment(env: pytest.MonkeyPatch) -> None:
    # A blank string in the override map (e.g. an operator cleared the field in the UI without
    # removing the key) must not resolve to "" and hide a real, configured environment value.
    set_model_overrides({"THYMIRA_MODEL_FAST": "  "})
    assert model_for(Role.AGENT, ModelTier.FAST) == "vendor-b/fast"


def test_a_live_override_still_clears_through_the_reset_fixture(env: pytest.MonkeyPatch) -> None:
    # Proves the autouse `_reset_model_route_overrides` fixture (tests/conftest.py) actually runs
    # before this test: if a prior test's override leaked, THYMIRA_MODEL_FAST would already read
    # back as something other than the environment value this fixture sets up.
    assert model_for(Role.AGENT, ModelTier.FAST) == "vendor-b/fast"


def test_the_route_policy_gate_still_applies_to_an_overridden_model(
    env: pytest.MonkeyPatch,
) -> None:
    # A live override changes which model choose() proposes; it must never bypass the separate,
    # frozen allowlist enforce_model_route checks afterward (routing.provider_for). choose() alone
    # has no route policy to enforce, so this only pins that the override reaches ModelChoice
    # exactly like an environment-configured model would -- provider_for's own enforcement is
    # covered by tests/thymira/test_agents_llm.py's existing route-policy tests.
    set_model_overrides({"THYMIRA_MODEL_FAST": "vendor-untrusted/fast"})
    choice = choose(Role.AGENT, "select_tool")
    assert choice.model == "vendor-untrusted/fast"
    assert choice.tier_applied is ModelTier.FAST
