"""The runtime-owned LLM proxy endpoint resolver fails closed."""

from __future__ import annotations

import os

import pytest
from pydantic_ai import Agent

from thymira.agents import (
    LiteLLMProvider,
    LLMCallError,
    LLMConfigurationError,
    get_provider,
    resolve_proxy_endpoint,
    routed_model,
)
from thymira.agents.llm.routing import Role, provider_for
from thymira.events import InMemoryEventLog
from thymira.schemas import ModelRoutePolicy, new_id

SAFE_HTTPS = "https://gateway.example.test/v1"


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        " https://gateway.example.test/v1",
        "https://gateway.example.test/v 1",
        "https://gateway.example.test/v1?token=secret",
        "https://user:password@gateway.example.test/v1",
        "http://gateway.example.test/v1",
        "https://gateway.example.test:bad/v1",
    ],
)
def test_proxy_resolution_refuses_invalid_or_undeclared_endpoints(endpoint: str) -> None:
    with pytest.raises(LLMConfigurationError):
        resolve_proxy_endpoint(endpoint, declared_safe_endpoints=(SAFE_HTTPS,))


def test_proxy_resolution_requires_an_exact_runtime_declaration() -> None:
    with pytest.raises(LLMConfigurationError, match="declared safe"):
        resolve_proxy_endpoint(
            "https://other.example.test/v1", declared_safe_endpoints=(SAFE_HTTPS,)
        )


def test_proxy_resolution_allows_declared_loopback_http_for_owned_test_gateway() -> None:
    endpoint = "http://127.0.0.1:8123/v1"
    assert resolve_proxy_endpoint(endpoint, declared_safe_endpoints=(endpoint,)) == endpoint


def test_proxy_resolution_omits_proxy_when_no_endpoint_was_requested() -> None:
    assert resolve_proxy_endpoint(None, declared_safe_endpoints=(SAFE_HTTPS,)) is None


def test_pinned_litellm_resolves_ambient_environment_but_runtime_refuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    ambient = "https://ambient.invalid/v1"
    monkeypatch.setattr(litellm, "api_base", None)
    monkeypatch.setenv("OPENAI_API_BASE", ambient)

    assert litellm.main._resolve_openai_api_base(None) == ambient
    with pytest.raises(LLMConfigurationError, match="ambient OPENAI"):
        LiteLLMProvider(model="gpt-test")


def test_actual_installed_factories_refuse_a_mutable_sdk_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "api_base", "https://ambient.invalid/v1")
    route_policy = ModelRoutePolicy.from_routes(("gpt-test",), authority="code-owned-tests")

    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        get_provider(model="gpt-test", route_policy=route_policy)
    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        provider_for(Role.AGENT, "code", model="gpt-test", route_policy=route_policy)


def test_actual_routed_default_refuses_a_mutable_sdk_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "api_base", "https://ambient.invalid/v1")
    monkeypatch.setenv("THYMIRA_MODEL_FAST", "gpt-test")
    route_policy = ModelRoutePolicy.from_routes(("gpt-test",), authority="code-owned-tests")
    model = routed_model(
        Role.AGENT, "select_tool", InMemoryEventLog(new_id("run")), route_policy=route_policy
    )

    with pytest.raises(LLMConfigurationError, match="mutable LiteLLM"):
        Agent(model=model).run_sync("must refuse")


def test_actual_sdk_explicit_endpoint_reaches_completion_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    endpoint = "https://gateway.example.test/v1"
    ambient = "https://ambient.invalid/v1"
    captured: dict[str, object] = {}
    monkeypatch.setattr(litellm, "api_base", ambient)
    monkeypatch.setenv("OPENAI_API_BASE", ambient)

    def fail_without_network(**kwargs: object) -> object:
        captured.update(kwargs)
        raise RuntimeError("network intentionally disabled")

    monkeypatch.setattr(litellm, "completion", fail_without_network)
    provider = LiteLLMProvider(
        model="gpt-test",
        proxy_endpoint=endpoint,
        declared_safe_endpoints=(endpoint,),
    )

    with pytest.raises(LLMCallError, match="failed"):
        provider.complete("no network")

    assert captured["api_base"] == endpoint
    assert litellm.api_base == ambient
    assert os.environ["OPENAI_API_BASE"] == ambient
