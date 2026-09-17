"""Offline LiteLLM provider fixture for request-ledger isolation tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

from thymira.agents.llm import LiteLLMProvider


class _Gateway:
    """Return fixed LiteLLM-shaped responses without network access."""

    model_cost: ClassVar[dict[str, float]] = {}
    supports_response_schema = staticmethod(lambda **_kwargs: True)

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._responses = responses
        self._index = 0

    def completion(self, **_kwargs: Any) -> object:
        """Return the next response in LiteLLM's minimal response shape."""
        if self._index >= len(self._responses):
            msg = "offline gateway received more calls than scripted responses"
            raise AssertionError(msg)
        content = self._responses[self._index]
        self._index += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=2,
                completion_tokens=1,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
            model="test-model",
            id=f"response-{self._index}",
        )


def native_provider(*contents: str) -> LiteLLMProvider:
    """Build a LiteLLM provider over an offline gateway returning responses in order."""
    provider = object.__new__(LiteLLMProvider)
    provider._litellm = _Gateway(contents)
    provider.model = "test-model"
    provider.proxy_endpoint = None
    provider._declared_safe_endpoints = frozenset[str]()
    provider.provider_name = "openai"
    provider.reasoning_effort = None
    provider.applies_reasoning_effort = False
    provider.timeout_s = 17.0
    provider._request_ledger = None
    provider._request_owner_id = None
    provider._request_attempt = 0
    provider._previous_request_id = None
    provider._runtime_skill_evidence = None
    return provider
