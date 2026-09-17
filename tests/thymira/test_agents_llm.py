"""LLM provider contract for ``thymira.agents``: LiteLLM implementation and the test double.

Never touches the network: every test that constructs :class:`LiteLLMProvider` injects a fake
``litellm`` module into ``sys.modules`` so the lazy ``import litellm`` inside the constructor
resolves to the fake, exactly as the legacy suite monkeypatched ``litellm.completion``.
"""

from __future__ import annotations

import os
import sys
import traceback
import types
from typing import TYPE_CHECKING, Any, get_args

import pytest
from pydantic import BaseModel, ConfigDict

from thymira.agents import (
    LiteLLMProvider,
    LLMCallError,
    LLMConfigurationError,
    LLMMessage,
    LLMResponse,
    LLMStructuredOutputError,
    LLMToolCall,
    LLMToolDefinition,
    ScriptedProvider,
    get_provider,
    routed_model,
)
from thymira.agents.llm.litellm_provider import (
    ALLOWED_REASONING_EFFORT,
    DEFAULT_MODEL_TIMEOUT_S,
    MODEL_ENV_VAR,
    MODEL_TIMEOUT_ENV_VAR,
    REASONING_EFFORT_ENV_VAR,
)
from thymira.agents.llm.routing import Role, provider_for
from thymira.events import InMemoryEventLog
from thymira.schemas import ModelRoutePolicy, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Fakes: a stand-in ``litellm`` module and a schema for structured output
# ---------------------------------------------------------------------------


class _FakeLiteLLMError(Exception):
    """Base for the fake ``litellm.exceptions`` hierarchy used in the tests."""

    def __init__(self, message: str = "", **_: object) -> None:
        super().__init__(message)


class AuthenticationError(_FakeLiteLLMError):
    pass


class PermissionDeniedError(_FakeLiteLLMError):
    pass


class RateLimitError(_FakeLiteLLMError):
    pass


class APIConnectionError(_FakeLiteLLMError):
    pass


class Timeout(_FakeLiteLLMError):  # noqa: N818  # mirrors litellm.exceptions.Timeout by name
    pass


class ServiceUnavailableError(_FakeLiteLLMError):
    pass


class BadRequestError(_FakeLiteLLMError):
    pass


class NotFoundError(_FakeLiteLLMError):
    pass


class Choice(BaseModel):
    """A small structured-output schema for the tests."""

    model_config = ConfigDict(extra="forbid")

    value: int


def _fake_response(
    *,
    content: str | None,
    tool_calls: list[types.SimpleNamespace] | None = None,
    model: str = "gpt-test",
    response_id: str = "resp-test",
    prompt_tokens: int = 5,
    completion_tokens: int = 3,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> types.SimpleNamespace:
    message = types.SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(finish_reason="stop", index=0, message=message)
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=(
            None if cached_tokens is None else types.SimpleNamespace(cached_tokens=cached_tokens)
        ),
        completion_tokens_details=(
            None
            if reasoning_tokens is None
            else {"reasoning_tokens": reasoning_tokens}  # some providers arrive as a mapping
        ),
    )
    return types.SimpleNamespace(id=response_id, model=model, choices=[choice], usage=usage)


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install a fake ``litellm`` module and return it so tests can rebind its callables."""
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(REASONING_EFFORT_ENV_VAR, raising=False)
    monkeypatch.delenv(MODEL_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    module = types.ModuleType("litellm")
    module.exceptions = types.SimpleNamespace(  # type: ignore[attr-defined]
        AuthenticationError=AuthenticationError,
        PermissionDeniedError=PermissionDeniedError,
        RateLimitError=RateLimitError,
        APIConnectionError=APIConnectionError,
        Timeout=Timeout,
        ServiceUnavailableError=ServiceUnavailableError,
        BadRequestError=BadRequestError,
        NotFoundError=NotFoundError,
    )
    module.model_cost = {}  # type: ignore[attr-defined]
    module.api_base = None  # type: ignore[attr-defined]
    # The real `get_supported_openai_params` answers per model; the fake says every model takes
    # `reasoning_effort` unless a test rebinds it, so the forwarding tests stay about forwarding.
    module.utils = types.SimpleNamespace(  # type: ignore[attr-defined]
        get_supported_openai_params=lambda **_kw: ["reasoning_effort"]
    )
    module.completion = lambda **_kw: _fake_response(content="hello")  # type: ignore[attr-defined]
    module.completion_cost = lambda **_kw: 0.0  # type: ignore[attr-defined]
    module.supports_response_schema = lambda **_kw: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


# ---------------------------------------------------------------------------
# Construction and model configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_provider"),
    [
        ("gpt-4o-mini", "openai"),
        ("anthropic/claude-sonnet-4-5", "anthropic"),
        ("groq/llama-3.3", "groq"),
    ],
)
def test_provider_label_derived_from_model_prefix(
    fake_litellm: types.ModuleType, model: str, expected_provider: str
) -> None:
    provider = LiteLLMProvider(model=model)
    assert provider.model == model
    assert provider.provider_name == expected_provider


def test_model_comes_from_env_var_when_argument_missing(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MODEL_ENV_VAR, "anthropic/claude-sonnet-4-5")
    provider = LiteLLMProvider(model=None)
    assert provider.model == "anthropic/claude-sonnet-4-5"
    assert provider.provider_name == "anthropic"


def test_argument_overrides_env_var(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MODEL_ENV_VAR, "anthropic/claude-sonnet-4-5")
    provider = LiteLLMProvider(model="gpt-4o-mini")
    assert provider.model == "gpt-4o-mini"


def test_missing_model_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    with pytest.raises(LLMConfigurationError, match="THYMIRA_MODEL"):
        LiteLLMProvider(model=None)


def test_missing_litellm_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(LLMConfigurationError, match="LiteLLM"):
        LiteLLMProvider(model="gpt-test")


# ---------------------------------------------------------------------------
# complete(): messages, text, tokens, provider/model, metadata
# ---------------------------------------------------------------------------


def test_complete_builds_system_and_user_messages(fake_litellm: types.ModuleType) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    LiteLLMProvider(model="gpt-test").complete("hi", system="be terse")

    assert captured["model"] == "gpt-test"
    assert captured["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]


def test_explicit_proxy_endpoint_reaches_every_litellm_completion_form(
    fake_litellm: types.ModuleType,
) -> None:
    """The installed provider applies one resolved endpoint to free, tool and schema calls."""
    endpoint = "https://gateway.example.test/v1"
    captured: list[dict[str, Any]] = []

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.append(kwargs)
        content = '{"value": 1}' if "response_format" in kwargs else "answer"
        return _fake_response(content=content)

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    provider = LiteLLMProvider(
        model="gpt-test",
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
    )

    provider.complete("free")
    provider.complete_turn([LLMMessage(role="user", content="tools")])
    provider.complete_structured("structured", schema=Choice)

    assert len(captured) == 3
    assert [call["api_base"] for call in captured] == [endpoint] * 3


def test_proxy_endpoint_is_not_added_without_explicit_configuration(
    fake_litellm: types.ModuleType,
) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    LiteLLMProvider(model="gpt-test").complete("direct")

    assert "api_base" not in captured


def test_direct_provider_refuses_an_ambient_openai_endpoint(
    fake_litellm: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_BASE", "https://ambient.invalid/v1")

    with pytest.raises(LLMConfigurationError, match="ambient OPENAI"):
        LiteLLMProvider(model="gpt-test")


def test_direct_provider_refuses_a_mutable_litellm_endpoint(
    fake_litellm: types.ModuleType,
) -> None:
    fake_litellm.api_base = "https://ambient.invalid/v1"  # type: ignore[attr-defined]

    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        LiteLLMProvider(model="gpt-test")


def test_explicit_proxy_wins_without_mutating_ambient_sdk_or_environment(
    fake_litellm: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://gateway.example.test/v1"
    ambient = "https://ambient.invalid/v1"
    captured: dict[str, Any] = {}
    fake_litellm.api_base = ambient  # type: ignore[attr-defined]
    monkeypatch.setenv("OPENAI_API_BASE", ambient)

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    LiteLLMProvider(
        model="gpt-test",
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
    ).complete("explicit")

    assert captured["api_base"] == endpoint
    assert fake_litellm.api_base == ambient  # type: ignore[attr-defined]
    assert os.getenv("OPENAI_API_BASE") == ambient


def test_proxy_endpoint_is_shared_by_both_installed_factories(
    fake_litellm: types.ModuleType,
) -> None:
    endpoint = "https://gateway.example.test/v1"
    route_policy = ModelRoutePolicy.from_routes(("gpt-test",), authority="test")
    provider = get_provider(
        model="gpt-test",
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
        route_policy=route_policy,
    )
    routed, _choice = provider_for(
        Role.AGENT,
        "code",
        model="gpt-test",
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
        route_policy=route_policy,
    )

    assert isinstance(provider, LiteLLMProvider)
    assert isinstance(routed, LiteLLMProvider)
    assert provider.proxy_endpoint == endpoint
    assert routed.proxy_endpoint == endpoint


def test_both_installed_factories_refuse_an_undeclared_proxy_endpoint(
    fake_litellm: types.ModuleType,
) -> None:
    route_policy = ModelRoutePolicy.from_routes(("gpt-test",), authority="test")
    with pytest.raises(LLMConfigurationError, match="declared safe"):
        get_provider(
            model="gpt-test",
            proxy_endpoint="https://unlisted.example.test/v1",
            route_policy=route_policy,
        )
    with pytest.raises(LLMConfigurationError, match="declared safe"):
        provider_for(
            Role.AGENT,
            "code",
            model="gpt-test",
            proxy_endpoint="https://unlisted.example.test/v1",
            route_policy=route_policy,
        )


def test_both_installed_factories_refuse_an_ambient_sdk_endpoint(
    fake_litellm: types.ModuleType,
) -> None:
    fake_litellm.api_base = "https://ambient.invalid/v1"  # type: ignore[attr-defined]
    route_policy = ModelRoutePolicy.from_routes(("gpt-test",), authority="test")

    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        get_provider(model="gpt-test", route_policy=route_policy)
    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        provider_for(Role.AGENT, "code", model="gpt-test", route_policy=route_policy)


def test_routed_model_default_path_forwards_the_declared_proxy(
    fake_litellm: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://gateway.example.test/v1"
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "gpt-test")
    log = InMemoryEventLog(new_id("run"))
    model = routed_model(
        Role.AGENT,
        "select_tool",
        log,
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
        route_policy=ModelRoutePolicy.from_routes(("gpt-test",), authority="test"),
    )

    from pydantic_ai import Agent

    Agent(model=model).run_sync("say hi")

    assert captured["api_base"] == endpoint


def test_routed_model_default_path_refuses_proxy_without_a_declaration(
    fake_litellm: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = "https://gateway.example.test/v1"
    called = False

    def never(**_kwargs: Any) -> types.SimpleNamespace:
        nonlocal called
        called = True
        raise AssertionError("an undeclared endpoint must fail before the gateway")

    fake_litellm.completion = never  # type: ignore[attr-defined]
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "gpt-test")
    model = routed_model(
        Role.AGENT,
        "select_tool",
        InMemoryEventLog(new_id("run")),
        proxy_endpoint=endpoint,
        route_policy=ModelRoutePolicy.from_routes(("gpt-test",), authority="test"),
    )

    from pydantic_ai import Agent

    with pytest.raises(LLMConfigurationError, match="declared safe"):
        Agent(model=model).run_sync("must refuse")

    assert called is False


def test_routed_model_default_path_refuses_an_ambient_sdk_endpoint(
    fake_litellm: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_litellm.api_base = "https://ambient.invalid/v1"  # type: ignore[attr-defined]
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "gpt-test")
    model = routed_model(
        Role.AGENT,
        "select_tool",
        InMemoryEventLog(new_id("run")),
        route_policy=ModelRoutePolicy.from_routes(("gpt-test",), authority="test"),
    )

    from pydantic_ai import Agent

    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        Agent(model=model).run_sync("must refuse")


def test_complete_omits_system_message_when_empty(fake_litellm: types.ModuleType) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    LiteLLMProvider(model="gpt-test").complete("hi")

    assert captured["messages"] == [{"role": "user", "content": "hi"}]


def test_complete_forwards_reasoning_effort_from_env(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    monkeypatch.setenv(REASONING_EFFORT_ENV_VAR, "LOW")

    LiteLLMProvider(model="gpt-test").complete("hi")

    assert captured["reasoning_effort"] == "low"


def test_complete_omits_reasoning_effort_when_unset(fake_litellm: types.ModuleType) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    LiteLLMProvider(model="gpt-test").complete("hi")

    assert "reasoning_effort" not in captured


@pytest.mark.parametrize("effort", ["turbo", "max", "default"])
def test_invalid_reasoning_effort_raises_configuration_error(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    """`max` and `default` look plausible, but LiteLLM rejects both: catch it here, not in a 400."""
    monkeypatch.setenv(REASONING_EFFORT_ENV_VAR, effort)
    with pytest.raises(LLMConfigurationError, match="THYMIRA_REASONING_EFFORT"):
        LiteLLMProvider(model="gpt-test")


def test_complete_applies_the_default_timeout_when_unset(
    fake_litellm: types.ModuleType,
) -> None:
    """Bug-hunt H10: a bare completion() call had nothing bounding a stalled connection."""
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    LiteLLMProvider(model="gpt-test").complete("hi")

    assert captured["timeout"] == DEFAULT_MODEL_TIMEOUT_S


def test_complete_forwards_timeout_from_env(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    monkeypatch.setenv(MODEL_TIMEOUT_ENV_VAR, "45")

    LiteLLMProvider(model="gpt-test").complete("hi")

    assert captured["timeout"] == 45.0


@pytest.mark.parametrize("value", ["not-a-number", "0", "-5"])
def test_invalid_timeout_raises_configuration_error(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(MODEL_TIMEOUT_ENV_VAR, value)
    with pytest.raises(LLMConfigurationError, match="THYMIRA_MODEL_TIMEOUT_S"):
        LiteLLMProvider(model="gpt-test")


def test_the_accepted_efforts_are_exactly_the_ones_litellm_accepts() -> None:
    """The alarm for a pinned-LiteLLM upgrade that adds, drops or renames a level.

    Restating LiteLLM's list is the only way this can drift, and a drift is silent: an effort we
    accept but LiteLLM does not becomes a 400 on a live call, and one LiteLLM accepts but we do
    not is simply unreachable.
    """
    from litellm.types.llms.openai import REASONING_EFFORT

    assert frozenset(get_args(REASONING_EFFORT)) == ALLOWED_REASONING_EFFORT


def test_reasoning_effort_is_dropped_for_a_model_that_does_not_take_it(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One effort is configured for the whole process but reaches every tier's model.

    LiteLLM raises `UnsupportedParamsError` rather than ignoring the parameter, which this module
    reports as `LLMCallError` and `routed_model` turns into a `ModelRetry` — so sending it blindly
    would burn a FAST-tier agent's whole retry budget and read as a model failure.
    """
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content="answer")

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]
    fake_litellm.utils.get_supported_openai_params = lambda **_kw: ["temperature"]
    monkeypatch.setenv(REASONING_EFFORT_ENV_VAR, "high")

    response = LiteLLMProvider(model="gpt-test").complete("hi")

    assert "reasoning_effort" not in captured
    # Dropped, not silently: the response says both what was asked for and that it did not apply.
    assert response.metadata["reasoning_effort"] == "high"
    assert response.metadata["reasoning_effort_applied"] is False


def test_an_unresolvable_model_does_not_fail_provider_construction(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tuning knob must never be the reason a Run cannot start."""

    def explode(**_kw: Any) -> list[str]:
        raise RuntimeError("unknown model")

    fake_litellm.utils.get_supported_openai_params = explode
    monkeypatch.setenv(REASONING_EFFORT_ENV_VAR, "high")

    provider = LiteLLMProvider(model="vendor-x/made-up")

    assert provider.reasoning_effort == "high"
    assert provider.applies_reasoning_effort is False


def test_an_applied_effort_is_recorded_on_the_response(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It changes both the answer and the bill, so it belongs in the response's own record."""
    monkeypatch.setenv(REASONING_EFFORT_ENV_VAR, "xhigh")

    response = LiteLLMProvider(model="gpt-test").complete("hi")

    assert response.metadata["reasoning_effort"] == "xhigh"
    assert response.metadata["reasoning_effort_applied"] is True


def test_no_effort_leaves_no_trace_in_the_response_metadata(
    fake_litellm: types.ModuleType,
) -> None:
    response = LiteLLMProvider(model="gpt-test").complete("hi")

    assert "reasoning_effort" not in response.metadata
    assert "reasoning_effort_applied" not in response.metadata


def test_complete_returns_text_and_usage(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="the answer", prompt_tokens=17, completion_tokens=9
    )

    result = LiteLLMProvider(model="gpt-test").complete("hi")

    assert isinstance(result, LLMResponse)
    assert result.text == "the answer"
    assert result.input_tokens == 17
    assert result.output_tokens == 9


def test_complete_turn_sends_history_and_tools_and_parses_tool_calls(
    fake_litellm: types.ModuleType,
) -> None:
    captured: dict[str, Any] = {}
    tool_call = types.SimpleNamespace(
        id="call-1",
        type="function",
        function=types.SimpleNamespace(name="echo", arguments='{"value":"hi"}'),
    )

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content=None, tool_calls=[tool_call])

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    response = LiteLLMProvider(model="gpt-test").complete_turn(
        [
            LLMMessage(role="system", content="Use tools."),
            LLMMessage(role="user", content="say hi"),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=(LLMToolCall(id="call-0", name="echo", arguments={}),),
            ),
            LLMMessage(role="tool", content="previous result", tool_call_id="call-0"),
        ],
        tools=(
            LLMToolDefinition(
                name="echo",
                description="Echo a value.",
                parameters_json_schema={"type": "object"},
            ),
        ),
    )

    assert captured["messages"] == [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "say hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-0",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "previous result", "tool_call_id": "call-0"},
    ]
    assert captured["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a value.",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert response.tool_calls == (
        LLMToolCall(id="call-1", name="echo", arguments={"value": "hi"}),
    )


def test_complete_handles_empty_content(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(content=None)  # type: ignore[attr-defined]
    result = LiteLLMProvider(model="gpt-test").complete("hi")
    assert result.text == ""


def test_complete_records_requested_vs_responded_model(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="answer", model="gpt-test-2026-08-11"
    )

    result = LiteLLMProvider(model="gpt-test").complete("hi")

    assert result.provider == "openai"
    assert result.model == "gpt-test-2026-08-11"
    assert result.metadata["model_requested"] == "gpt-test"
    assert result.metadata["integration"] == "litellm"
    assert result.metadata["api"] == "completion"
    assert result.metadata["response_id"] == "resp-test"
    assert result.metadata["cost_source"] == "litellm.completion_cost"


# ---------------------------------------------------------------------------
# Cost: known vs unknown model
# ---------------------------------------------------------------------------


def test_cost_computed_for_known_model(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="answer", model="gpt-known"
    )
    fake_litellm.model_cost = {"gpt-known": {}}  # type: ignore[attr-defined]
    fake_litellm.completion_cost = lambda **_kw: 0.1234  # type: ignore[attr-defined]

    result = LiteLLMProvider(model="gpt-known").complete("hi")

    assert result.cost_usd == pytest.approx(0.1234)
    assert result.metadata["cost_known"] is True


def test_cost_is_none_for_unknown_model(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="answer", model="mystery"
    )
    fake_litellm.model_cost = {}  # type: ignore[attr-defined]

    def fail_if_called(**_kw: Any) -> float:
        raise AssertionError("completion_cost must not be called for an unknown model")

    fake_litellm.completion_cost = fail_if_called  # type: ignore[attr-defined]

    result = LiteLLMProvider(model="mystery").complete("hi")

    assert result.cost_usd is None
    assert result.metadata["cost_known"] is False


def test_cost_matches_model_cost_without_provider_prefix(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="answer", model="claude-sonnet-4-5"
    )
    fake_litellm.model_cost = {"claude-sonnet-4-5": {}}  # type: ignore[attr-defined]
    fake_litellm.completion_cost = lambda **_kw: 0.5  # type: ignore[attr-defined]

    result = LiteLLMProvider(model="anthropic/claude-sonnet-4-5").complete("hi")

    assert result.cost_usd == pytest.approx(0.5)
    assert result.metadata["cost_known"] is True


def test_cost_becomes_none_when_completion_cost_raises(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="answer", model="gpt-known"
    )
    fake_litellm.model_cost = {"gpt-known": {}}  # type: ignore[attr-defined]

    def raise_not_mapped(**_kw: Any) -> float:
        raise RuntimeError("This model isn't mapped yet")

    fake_litellm.completion_cost = raise_not_mapped  # type: ignore[attr-defined]

    result = LiteLLMProvider(model="gpt-known").complete("hi")

    assert result.cost_usd is None
    assert result.metadata["cost_known"] is False


# ---------------------------------------------------------------------------
# Remote call error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_factory", "expected_match"),
    [
        (lambda: AuthenticationError("bad key"), "authentication"),
        (lambda: RateLimitError("slow down"), "rate limit"),
        (lambda: APIConnectionError("no route"), "network"),
        (lambda: BadRequestError("nonsense"), "invalid request"),
        (lambda: RuntimeError("something odd"), "failed"),
    ],
)
def test_complete_maps_provider_errors_to_call_error(
    fake_litellm: types.ModuleType, exc_factory: Any, expected_match: str
) -> None:
    def fail(**_kw: Any) -> types.SimpleNamespace:
        raise exc_factory()

    fake_litellm.completion = fail  # type: ignore[attr-defined]

    with pytest.raises(LLMCallError, match=expected_match):
        LiteLLMProvider(model="gpt-test").complete("hi")


def test_call_error_does_not_leak_configured_api_key(
    fake_litellm: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-not-a-real-key-0123456789"  # noqa: S105
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fail(**_kw: Any) -> types.SimpleNamespace:
        raise AuthenticationError("invalid credentials")

    fake_litellm.completion = fail  # type: ignore[attr-defined]

    with pytest.raises(LLMCallError) as excinfo:
        LiteLLMProvider(model="gpt-test").complete("hi")

    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# complete_structured(): always validated locally
# ---------------------------------------------------------------------------


def test_complete_structured_returns_validated_instance(fake_litellm: types.ModuleType) -> None:
    captured: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> types.SimpleNamespace:
        captured.update(kwargs)
        return _fake_response(content='{"value": 7}')

    fake_litellm.completion = fake_completion  # type: ignore[attr-defined]

    parsed, response = LiteLLMProvider(model="gpt-test").complete_structured(
        "pick", schema=Choice, system="plan"
    )

    assert captured["response_format"] is Choice
    assert isinstance(parsed, Choice)
    assert parsed.value == 7
    assert response.metadata["api"] == "completion.response_format"
    assert response.metadata["schema"] == "Choice"


def test_complete_structured_raises_on_invalid_json(fake_litellm: types.ModuleType) -> None:
    raw_response = "not-json-private-marker"
    fake_litellm.completion = lambda **_kw: _fake_response(content=raw_response)  # type: ignore[attr-defined]

    with pytest.raises(LLMStructuredOutputError, match="not valid JSON") as exc_info:
        LiteLLMProvider(model="gpt-test").complete_structured("pick", schema=Choice)

    assert raw_response not in str(exc_info.value)
    assert exc_info.value.response is not None
    assert exc_info.value.response.text == raw_response
    assert exc_info.value.response.input_tokens == 5
    assert exc_info.value.response.output_tokens == 3


def test_complete_structured_raises_on_empty_content(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(content=None)  # type: ignore[attr-defined]

    with pytest.raises(LLMStructuredOutputError, match="not valid JSON"):
        LiteLLMProvider(model="gpt-test").complete_structured("pick", schema=Choice)


def test_complete_structured_raises_on_schema_mismatch(fake_litellm: types.ModuleType) -> None:
    raw_response = '{"value":"schema-private-marker"}'
    fake_litellm.completion = lambda **_kw: _fake_response(content=raw_response)  # type: ignore[attr-defined]

    with pytest.raises(LLMStructuredOutputError, match="schema") as exc_info:
        LiteLLMProvider(model="gpt-test").complete_structured("pick", schema=Choice)

    assert "schema-private-marker" not in str(exc_info.value)
    rendered_traceback = "".join(traceback.format_exception(exc_info.value))
    assert "schema-private-marker" not in rendered_traceback
    assert "does not satisfy the expected schema" in rendered_traceback
    assert exc_info.value.response is not None
    assert exc_info.value.response.text == raw_response


def test_complete_structured_rejects_extra_fields(fake_litellm: types.ModuleType) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content='{"value": 7, "extra": 1}'
    )

    with pytest.raises(LLMStructuredOutputError, match="schema"):
        LiteLLMProvider(model="gpt-test").complete_structured("pick", schema=Choice)


def test_complete_structured_raises_before_call_when_unsupported(
    fake_litellm: types.ModuleType,
) -> None:
    def never(**_kw: Any) -> types.SimpleNamespace:
        raise AssertionError("completion must not be called when structured output is unsupported")

    fake_litellm.completion = never  # type: ignore[attr-defined]
    fake_litellm.supports_response_schema = lambda **_kw: False  # type: ignore[attr-defined]

    with pytest.raises(LLMStructuredOutputError, match="does not support") as exc_info:
        LiteLLMProvider(model="gpt-test").complete_structured("pick", schema=Choice)

    assert exc_info.value.response is None


# ---------------------------------------------------------------------------
# The single production factory
# ---------------------------------------------------------------------------


def test_get_provider_returns_litellm_provider(fake_litellm: types.ModuleType) -> None:
    provider = get_provider(
        model="gpt-test",
        route_policy=ModelRoutePolicy.from_routes(("gpt-test",), authority="test"),
    )
    assert isinstance(provider, LiteLLMProvider)
    assert provider.model == "gpt-test"


# ---------------------------------------------------------------------------
# ScriptedProvider: deterministic double for other tests
# ---------------------------------------------------------------------------


def test_scripted_provider_returns_text_in_sequence() -> None:
    provider = ScriptedProvider(["first", "second"])
    assert provider.complete("a").text == "first"
    assert provider.complete("b", system="s").text == "second"
    assert provider.calls == [
        {"prompt": "a", "system": "", "tools": []},
        {"prompt": "b", "system": "s", "tools": []},
    ]
    assert provider.provider_name == "test"
    assert provider.model == "scripted"


def test_scripted_provider_serialises_non_string_items() -> None:
    provider = ScriptedProvider([{"value": 1}, Choice(value=2)])
    assert provider.complete("a").text == '{"value": 1}'
    assert provider.complete("b").text == '{"value":2}'


def test_scripted_provider_returns_a_tool_call_for_a_turn() -> None:
    call = LLMToolCall(id="call-1", name="echo", arguments={"value": "hi"})
    provider = ScriptedProvider([call])

    response = provider.complete_turn(
        [
            LLMMessage(role="system", content="Use tools."),
            LLMMessage(role="user", content="say hi"),
        ],
        tools=(
            LLMToolDefinition(
                name="echo",
                description="Echo a value.",
                parameters_json_schema={"type": "object"},
            ),
        ),
    )

    assert response.text == ""
    assert response.tool_calls == (call,)
    assert provider.calls == [
        {
            "prompt": "say hi",
            "system": "Use tools.",
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo a value.",
                    "parameters_json_schema": {"type": "object"},
                }
            ],
        }
    ]


def test_scripted_provider_records_the_offered_tool_definitions() -> None:
    """A harness records what the model saw: complete_turn keeps the offered tool schemas."""
    provider = ScriptedProvider([LLMToolCall(id="c1", name="glob", arguments={})])

    provider.complete_turn(
        [LLMMessage(role="user", content="find files")],
        tools=(
            LLMToolDefinition(
                name="glob",
                description="Find files.",
                parameters_json_schema={"type": "object", "required": ["pattern"]},
            ),
            LLMToolDefinition(
                name="read_file", description=None, parameters_json_schema={"type": "object"}
            ),
        ),
    )

    (recorded,) = provider.calls
    assert [tool["name"] for tool in recorded["tools"]] == ["glob", "read_file"]
    assert recorded["tools"][0]["parameters_json_schema"]["required"] == ["pattern"]


def test_scripted_provider_records_no_tools_for_complete() -> None:
    """`complete`/`complete_structured` take no tools, so their call records an empty list."""
    provider = ScriptedProvider(["done", {"value": 1}])

    provider.complete("go")
    provider.complete_structured("go", schema=Choice)

    assert [call["tools"] for call in provider.calls] == [[], []]


def test_scripted_provider_structured_from_dict() -> None:
    provider = ScriptedProvider([{"value": 3}])
    parsed, response = provider.complete_structured("go", schema=Choice)
    assert isinstance(parsed, Choice)
    assert parsed.value == 3
    assert response.metadata["schema"] == "Choice"


def test_scripted_provider_structured_from_model_instance() -> None:
    provider = ScriptedProvider([Choice(value=4)])
    parsed, _ = provider.complete_structured("go", schema=Choice)
    assert parsed == Choice(value=4)


def test_scripted_provider_structured_from_json_string() -> None:
    provider = ScriptedProvider(['{"value": 5}'])
    parsed, _ = provider.complete_structured("go", schema=Choice)
    assert parsed.value == 5


def test_scripted_provider_structured_rejects_mismatch() -> None:
    provider = ScriptedProvider([{"wrong": 1}])
    with pytest.raises(LLMStructuredOutputError, match="Choice"):
        provider.complete_structured("go", schema=Choice)


def test_scripted_provider_structured_rejects_invalid_json_string() -> None:
    raw_response = "not-json-scripted-private-marker"
    provider = ScriptedProvider([raw_response])

    with pytest.raises(LLMStructuredOutputError) as exc_info:
        provider.complete_structured("go", schema=Choice)

    assert raw_response not in str(exc_info.value)
    rendered_traceback = "".join(traceback.format_exception(exc_info.value))
    assert raw_response not in rendered_traceback
    assert "scripted response does not satisfy Choice" in rendered_traceback
    assert exc_info.value.response is not None
    assert exc_info.value.response.text == raw_response


def test_scripted_provider_raises_when_exhausted() -> None:
    provider = ScriptedProvider(["only one"])
    provider.complete("a")
    with pytest.raises(LLMCallError, match="ran out"):
        provider.complete("b")


def test_scripted_provider_structured_raises_when_exhausted() -> None:
    provider = ScriptedProvider([])
    with pytest.raises(LLMCallError, match="ran out"):
        provider.complete_structured("a", schema=Choice)


def test_a_callable_scripted_turn_answers_from_the_messages_it_is_shown() -> None:
    def echo(messages: Sequence[LLMMessage]) -> LLMToolCall:
        return LLMToolCall(id="t1", name="final_result", arguments={"seen": messages[-1].content})

    provider = ScriptedProvider([echo])

    response = provider.complete_turn([LLMMessage(role="user", content="hello")])

    assert response.tool_calls[0].arguments == {"seen": "hello"}
    assert provider.calls[0]["prompt"] == "hello"


def test_a_callable_scripted_item_is_refused_outside_complete_turn() -> None:
    provider = ScriptedProvider([lambda _messages: "never"])

    with pytest.raises(LLMCallError, match="complete_turn"):
        provider.complete("hi")


def test_the_cached_and_reasoning_counts_are_read_when_the_provider_reports_them(
    fake_litellm: types.ModuleType,
) -> None:
    """LiteLLM normalises providers onto OpenAI's shape, where both counts are nested and inclusive.

    They are carried separately from the totals that contain them: the budget ledger is charged the
    inclusive totals, because that is what the provider bills, while the trace needs them split.
    """
    fake_litellm.completion = lambda **_kw: _fake_response(  # type: ignore[attr-defined]
        content="the answer",
        prompt_tokens=17903,
        completion_tokens=188,
        cached_tokens=17817,
        reasoning_tokens=15,
    )

    result = LiteLLMProvider(model="gpt-test").complete("hi")

    assert result.input_tokens == 17903
    assert result.output_tokens == 188
    assert result.cached_input_tokens == 17817
    assert result.reasoning_output_tokens == 15


def test_a_provider_that_reports_no_breakdown_leaves_both_counts_at_zero(
    fake_litellm: types.ModuleType,
) -> None:
    fake_litellm.completion = lambda **_kw: _fake_response(content="ok")  # type: ignore[attr-defined]

    result = LiteLLMProvider(model="gpt-test").complete("hi")

    assert result.cached_input_tokens == 0
    assert result.reasoning_output_tokens == 0
