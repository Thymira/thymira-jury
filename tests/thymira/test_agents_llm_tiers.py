"""Model tiers: orchestrators (THY, MIRA) and sub-agents resolve their model independently."""

from __future__ import annotations

import pytest

from thymira.agents.llm import (
    MODEL_ENV_VAR,
    ROLE_ENV_VARS,
    LLMConfigurationError,
    get_provider,
    resolve_model,
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (*ROLE_ENV_VARS.values(), MODEL_ENV_VAR):
        monkeypatch.delenv(variable, raising=False)


def test_each_role_reads_its_own_variable_then_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("THYMIRA_ORCHESTRATOR_MODEL", "anthropic/frontier-example")
    monkeypatch.setenv("THYMIRA_AGENT_MODEL", "openai/cheap-example")
    monkeypatch.setenv("THYMIRA_MODEL", "fallback-example")

    assert resolve_model("orchestrator") == "anthropic/frontier-example"
    assert resolve_model("agent") == "openai/cheap-example"
    monkeypatch.delenv("THYMIRA_AGENT_MODEL")
    assert resolve_model("agent") == "fallback-example"
    assert resolve_model("agent", model="explicit") == "explicit"


def test_unconfigured_role_raises_a_precise_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert resolve_model("orchestrator") is None
    with pytest.raises(LLMConfigurationError, match="THYMIRA_ORCHESTRATOR_MODEL"):
        get_provider(role="orchestrator")
