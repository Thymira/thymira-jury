"""The LLM provider contract.

The rest of the system knows only this protocol. Whether OpenAI or Anthropic sits behind it,
the signature is the same, and tests inject their own doubles without adding simulated
providers to production code.

Traceability: whoever calls a provider must record the exact credential-scrubbed prompt in the
canonical local provenance artifact and the redacted response metadata in the trace, never the
model's internal reasoning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from thymira.schemas import ReasoningMetadata

if TYPE_CHECKING:
    from collections.abc import Sequence

BaseModelT = TypeVar("BaseModelT", bound=BaseModel)


class LLMToolCall(BaseModel):
    """One tool call requested by a model response."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]


class LLMMessage(BaseModel):
    """Provider-neutral message in one agent conversation."""

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


class LLMToolDefinition(BaseModel):
    """Provider-neutral description of one callable agent tool."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str | None = None
    parameters_json_schema: dict[str, Any]


class LLMConfigurationError(RuntimeError):
    """The LiteLLM SDK is missing or a required local setting (e.g. the model id) is absent."""


class LLMCallError(RuntimeError):
    """The remote call failed (authentication, rate limit, network, or an invalid request)."""


class LLMTimeoutError(LLMCallError):
    """The provider did not answer before its configured timeout."""


class LLMStructuredOutputError(RuntimeError):
    """A structured output failed, optionally after the provider returned a response.

    ``response`` is present only when the gateway returned content that can be charged and
    audited. Pre-call failures, such as an unsupported response schema, leave it as ``None``.
    The response is deliberately not included in the exception message so logging ``str(error)``
    cannot expose model content. Providers must also suppress raw JSON/Pydantic validation causes,
    because their tracebacks can reproduce the returned value.
    """

    def __init__(self, message: str, *, response: LLMResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


class LLMResponse(BaseModel):
    """An immutable record of one completion and its accounting metadata.

    Attributes:
        text: The model's textual answer (JSON text for structured calls).
        provider: The real provider resolved from the model id ("openai", "anthropic", ...);
            "test"/"scripted" appear only in tests.
        model: The model that actually answered (may differ from the one requested).
        input_tokens: Prompt tokens reported by the provider, or 0 when unknown. Inclusive:
            providers count cache hits inside this total, so `cached_input_tokens` is a subset.
        output_tokens: Completion tokens reported by the provider, or 0 when unknown. Inclusive
            in the same way: reasoning tokens are billed as output and counted here.
        cached_input_tokens: Prompt tokens served from the provider's cache, 0 when it reports none.
        reasoning_output_tokens: Output tokens spent reasoning, 0 when the model does not reason.
        cost_usd: Estimated cost in USD, or None when the model's price is unknown.
        metadata: Free-form, non-secret accounting fields for the trace.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    cost_usd: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: tuple[LLMToolCall, ...] = ()
    reasoning: ReasoningMetadata = Field(default_factory=ReasoningMetadata)


class LLMProvider(Protocol):
    """The only contract the rest of the system knows about an LLM backend."""

    provider_name: str
    model: str

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        """Return one response, optionally constraining whether tools may be called in parallel."""
        ...

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        """Return a free-text completion for ``prompt`` (with an optional ``system`` message)."""
        ...

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        """Return a completion validated against ``schema`` plus the raw response record.

        A raised :class:`LLMStructuredOutputError` carries its received response when a provider
        call completed but local validation failed, allowing callers to charge that attempt.
        """
        ...
