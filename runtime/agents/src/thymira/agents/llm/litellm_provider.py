"""LiteLLM-backed provider: the single production implementation of :class:`LLMProvider`.

It never talks to the network at import time. The real provider and model are resolved from a
single model identifier in LiteLLM's own format: without a prefix OpenAI is assumed
(e.g. ``gpt-4o-mini``); with a ``"<provider>/<model>"`` prefix the call is routed elsewhere
(e.g. ``anthropic/claude-sonnet-4-5``). API keys (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``)
are read by LiteLLM directly from the environment; this module never touches or exposes them.

No fallback and no provider retry: a failed call is not retried on its own -- retrying a whole
reasoning step is `routed_model`'s `ModelRetry` seam. Native Anthropic calls request LiteLLM's
automatic ephemeral prompt caching explicitly before the effective request is recorded, so the
setting that reaches the provider is also part of the authoritative request evidence. Bug-hunt
H10 is right that a bare
``litellm.completion`` call would otherwise hang forever on a stalled connection with nothing to
bound it, which is a different gap than retrying: a request timeout is applied below so a stalled
call fails fast into that same seam instead of blocking a Run indefinitely).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from copy import copy
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast, get_args

from pydantic import ValidationError

from thymira.agents.llm.base import (
    BaseModelT,
    LLMCallError,
    LLMConfigurationError,
    LLMMessage,
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
)
from thymira.agents.llm.proxy import resolve_proxy_endpoint, with_proxy_endpoint
from thymira.schemas import ReasoningMetadata, ReasoningStatus, ResponseOutcome

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from thymira.agents.request_ledger import RequestLease, RequestLedger
    from thymira.schemas import Event


MODEL_ENV_VAR = "THYMIRA_MODEL"
"""Environment variable that supplies the model id when ``model=`` is not passed."""

REASONING_EFFORT_ENV_VAR = "THYMIRA_REASONING_EFFORT"
"""Optional effort passed through to LiteLLM as ``reasoning_effort``."""

MODEL_TIMEOUT_ENV_VAR = "THYMIRA_MODEL_TIMEOUT_S"
"""Optional per-request timeout, in seconds, passed to LiteLLM as ``timeout``."""

DEFAULT_MODEL_TIMEOUT_S = 120.0
"""Applied when :data:`MODEL_TIMEOUT_ENV_VAR` is unset -- long enough for a frontier reasoning
call under normal load, short enough that a stalled connection fails into `routed_model`'s
`ModelRetry` seam well within an interactive Run instead of hanging it (bug-hunt H10: a bare
``litellm.completion`` call has nothing bounding it at all)."""

ANTHROPIC_PROMPT_CACHE_CONTROL = {"type": "ephemeral"}
"""LiteLLM's top-level automatic prompt-caching request for native Anthropic models."""


def _timeout_from_env() -> float:
    """Read :data:`MODEL_TIMEOUT_ENV_VAR`; the default when unset or blank.

    Raises:
        LLMConfigurationError: The variable is set to something that is not a positive number.
    """
    raw = os.getenv(MODEL_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MODEL_TIMEOUT_S
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise LLMConfigurationError(
            f"{MODEL_TIMEOUT_ENV_VAR}={raw!r} is not a number of seconds"
        ) from exc
    if seconds <= 0:
        raise LLMConfigurationError(f"{MODEL_TIMEOUT_ENV_VAR}={raw!r} must be a positive number")
    return seconds


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
"""The values LiteLLM accepts, mirroring its own ``REASONING_EFFORT`` literal.

Restating the list is what makes it able to drift from LiteLLM's, so it is not restated twice
here — :data:`ALLOWED_REASONING_EFFORT` is derived from this alias — and
``test_agents_llm.py`` pins it against LiteLLM's own literal so a pinned upgrade that adds or
renames a level fails a test rather than a production call. LiteLLM's ``completion()`` also
accepts ``"default"``; it is deliberately absent, because leaving the variable unset already
means "whatever the provider does by default".
"""

ALLOWED_REASONING_EFFORT: frozenset[str] = frozenset(get_args(ReasoningEffort))


def _reasoning_effort_from_env() -> ReasoningEffort | None:
    """Read ``THYMIRA_REASONING_EFFORT``; ``None`` when unset so LiteLLM keeps the provider default.

    Raises:
        LLMConfigurationError: The variable is set to a value LiteLLM / OpenAI do not accept.
    """
    raw = os.getenv(REASONING_EFFORT_ENV_VAR, "").strip().lower()
    if not raw:
        return None
    if raw not in ALLOWED_REASONING_EFFORT:
        allowed = ", ".join(sorted(ALLOWED_REASONING_EFFORT))
        raise LLMConfigurationError(
            f"{REASONING_EFFORT_ENV_VAR}={raw!r} is not a valid reasoning effort; "
            f"allowed: {allowed}"
        )
    return cast("ReasoningEffort", raw)


def _accepts_reasoning_effort(litellm: Any, model: str) -> bool:
    """Whether LiteLLM will accept ``reasoning_effort`` for ``model``.

    One effort is configured for the whole process, but Thymira routes by *tier*, so the same
    setting reaches a FRONTIER reasoning model and a FAST one that has no such parameter. LiteLLM
    does not ignore a parameter a model does not support: ``litellm.drop_params`` is False unless
    the deployment sets ``LITELLM_DROP_PARAMS``, so it raises ``UnsupportedParamsError`` — a
    ``BadRequestError``, which this module reports as ``LLMCallError``, which ``routed_model``
    turns into a ``ModelRetry``. Sending it blindly would therefore burn a whole agent's retry
    budget on every FAST-tier call and surface as a model failure rather than a misconfiguration.

    An unknown model id yields ``None`` rather than an error, and is treated as *not* accepting
    it: a tuning knob must never be the reason a Run fails. Whether it was actually applied is
    reported on every response (:meth:`LiteLLMProvider._effort_metadata`), so a dropped effort is
    visible rather than silent.
    """
    try:
        supported = litellm.utils.get_supported_openai_params(model=model)
    except Exception:  # noqa: BLE001  # an unresolvable model must not fail provider construction
        return False
    return bool(supported) and "reasoning_effort" in supported


class _UnraisedGatewayError(Exception):
    """Never raised: it stands in for a LiteLLM error class the injected gateway does not define."""


class _AbsentGatewayExceptions:
    """LiteLLM's error namespace, as seen through a gateway that does not expose one.

    ``self._litellm`` is an injection seam, so the gateway is not always the LiteLLM module: a
    test double answers ``completion`` and nothing else. Naming ``litellm.exceptions.X`` directly
    in an ``except`` clause is evaluated only once something is raised, so such a double works
    until the first failure and then reports ``AttributeError: no attribute 'exceptions'``
    instead of the error the gateway actually raised. Every lookup here answers with a class that
    is never raised, so that error reaches the safety net below as itself.
    """

    def __getattr__(self, name: str) -> type[BaseException]:
        return _UnraisedGatewayError


_ABSENT_GATEWAY_EXCEPTIONS = _AbsentGatewayExceptions()


def _gateway_exceptions(litellm: Any) -> Any:
    """The gateway's LiteLLM error namespace, or the stand-in when it exposes none."""
    exceptions = getattr(litellm, "exceptions", None)
    return _ABSENT_GATEWAY_EXCEPTIONS if exceptions is None else exceptions


class _Usage(NamedTuple):
    """One completion's token counts, as the provider counts them: inclusive.

    `input_tokens` contains `cached_input_tokens`, and `output_tokens` contains
    `reasoning_output_tokens`. That is what the bill is based on, so it is what the budget ledger
    charges; the trace splits them into exclusive buckets, which is a different question.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


def _attribute(source: Any, name: str) -> Any:
    """One nested member of a provider's usage object, whether it is an object or a mapping."""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _count(source: Any, name: str) -> int:
    """One token count, 0 when the provider left it absent or null."""
    value = _attribute(source, name)
    return int(value or 0)


def _provider_label(model: str) -> str:
    """Return the real provider from the model id, without any network call.

    LiteLLM assumes OpenAI for identifiers without a provider prefix; an explicit prefix such
    as ``"anthropic/"`` routes the call to another provider.
    """
    return model.split("/", 1)[0] if "/" in model else "openai"


class LiteLLMProvider:
    """The single production implementation of :class:`LLMProvider`, backed by LiteLLM."""

    opaque_replay_state_required = False

    def __init__(
        self,
        *,
        model: str | None = None,
        request_ledger: RequestLedger | None = None,
        request_owner_id: str | None = None,
        proxy_endpoint: str | None = None,
        declared_safe_endpoints: Collection[str] = (),
    ) -> None:
        """Resolve the model id and load LiteLLM lazily.

        Args:
            model: The model identifier. When None, ``THYMIRA_MODEL`` is used instead.
            request_ledger: Optional durable ledger for effective gateway request evidence.
            request_owner_id: Durable owner of facts supplied to the ledger.
            proxy_endpoint: An explicitly requested LiteLLM-compatible endpoint. It must be an
                exact member of ``declared_safe_endpoints``; ``None`` keeps direct dispatch.
            declared_safe_endpoints: Runtime-owned exact endpoint allowlist for ``proxy_endpoint``.

        Raises:
            LLMConfigurationError: If no model is configured, or the LiteLLM SDK is missing.
        """
        resolved = model or os.getenv(MODEL_ENV_VAR, "").strip()
        if not resolved:
            raise LLMConfigurationError(
                "No model configured. Pass model=... or set the THYMIRA_MODEL environment "
                "variable (e.g. 'gpt-4o-mini' or 'anthropic/claude-opus-5')."
            )
        resolved_proxy_endpoint = resolve_proxy_endpoint(
            proxy_endpoint, declared_safe_endpoints=declared_safe_endpoints
        )
        try:
            import litellm  # noqa: PLC0415  # litellm is imported lazily (slow, optional at import time)
        except ImportError as exc:
            raise LLMConfigurationError(
                "The LiteLLM SDK is not installed. Run: uv sync --all-packages"
            ) from exc
        self._litellm: Any = litellm
        self.model: str = resolved
        self._declared_safe_endpoints = frozenset(declared_safe_endpoints)
        self.proxy_endpoint = resolve_proxy_endpoint(
            resolved_proxy_endpoint,
            declared_safe_endpoints=self._declared_safe_endpoints,
            litellm_sdk=litellm,
        )
        self.provider_name: str = _provider_label(resolved)
        self.reasoning_effort: ReasoningEffort | None = _reasoning_effort_from_env()
        self.applies_reasoning_effort: bool = self.reasoning_effort is not None and (
            _accepts_reasoning_effort(litellm, resolved)
        )
        self.timeout_s: float = _timeout_from_env()
        self._request_ledger = request_ledger
        self._request_owner_id = request_owner_id
        self._request_attempt = 0
        self._previous_request_id: str | None = None
        self._runtime_skill_evidence: Mapping[str, object] | None = None

    def attach_request_ledger(self, ledger: RequestLedger, *, owner_id: str) -> None:
        """Attach the Run's request ledger before the next effective gateway call."""
        if not owner_id.strip():
            raise ValueError("request ledger owner_id must not be empty")
        if ledger is not self._request_ledger or owner_id != self._request_owner_id:
            self._request_attempt = 0
            self._previous_request_id = None
            self._runtime_skill_evidence = None
        self._request_ledger = ledger
        self._request_owner_id = owner_id

    def without_request_ledger(self) -> LiteLLMProvider:
        """Return an equivalent provider whose next request has no inherited ledger binding.

        The gateway/configuration handles are immutable for a request and can be shared, while the
        ledger owner, retry chain and runtime-skill selection are request-local mutable state.
        Copying before clearing those fields lets a specialised caller install a projecting ledger
        boundary without mutating a provider another graph node may still share.
        """
        detached = copy(self)
        vars(detached).update(
            _request_ledger=None,
            _request_owner_id=None,
            _request_attempt=0,
            _previous_request_id=None,
            _runtime_skill_evidence=None,
        )
        return detached

    def bind_runtime_skill_selection(
        self, selection_event: Event, evidence: Mapping[str, object]
    ) -> None:
        """Bind the next effective gateway requests to one catalog selection event."""
        if not evidence:
            self._runtime_skill_evidence = None
            return
        if selection_event.hash is None:
            raise ValueError("runtime skill selection event has no chain hash")
        self._runtime_skill_evidence = {
            **evidence,
            "runtime_skill_selection_event_id": str(selection_event.event_id),
            "runtime_skill_selection_event_hash": selection_event.hash,
        }

    @staticmethod
    def _build_messages(prompt: str, system: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    @staticmethod
    def _build_turn_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            rendered.append(item)
        return rendered

    @staticmethod
    def _build_tool_definitions(
        tools: Sequence[LLMToolDefinition],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @classmethod
    def _parse_tool_calls(cls, response: Any) -> tuple[LLMToolCall, ...]:
        message = response.choices[0].message
        parsed: list[LLMToolCall] = []
        for raw_call in cls._field(message, "tool_calls", ()) or ():
            function = cls._field(raw_call, "function", {})
            raw_arguments = cls._field(function, "arguments", "{}")
            try:
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    arguments = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LLMCallError("the model returned invalid tool-call arguments") from exc
            if not isinstance(arguments, dict):
                raise LLMCallError("the model returned non-object tool-call arguments")
            parsed.append(
                LLMToolCall(
                    id=str(cls._field(raw_call, "id", "")),
                    name=str(cls._field(function, "name", "")),
                    arguments=arguments,
                )
            )
        return tuple(parsed)

    def _effort_metadata(self) -> dict[str, Any]:
        """What this provider did with the configured effort, for the response record.

        Absent when nothing is configured, so the common case adds no noise; present with
        ``applied=False`` when the model does not take the parameter, so an operator can tell a
        dropped effort from one that was never set.
        """
        if self.reasoning_effort is None:
            return {}
        return {
            "reasoning_effort": self.reasoning_effort,
            "reasoning_effort_applied": self.applies_reasoning_effort,
        }

    @staticmethod
    def _error_digest(error: BaseException) -> str:
        """Hash only an exception type for safe terminal evidence."""
        kind = f"{type(error).__module__}.{type(error).__qualname__}"
        return hashlib.sha256(kind.encode("utf-8")).hexdigest()

    def _record_failure(
        self,
        lease: RequestLease | None,
        outcome: ResponseOutcome,
        error_code: str,
        error: BaseException,
    ) -> None:
        """Persist a safe terminal outcome, preserving the original error if storage fails."""
        ledger = self._request_ledger
        if lease is None or ledger is None:
            return
        try:
            ledger.record_terminal(
                lease,
                outcome,
                error_code=error_code,
                error_digest=self._error_digest(error),
            )
        except Exception as ledger_error:
            raise error from ledger_error

    def _record_response_failure(
        self,
        lease: RequestLease | None,
        response: LLMResponse,
        error_code: str,
        error: BaseException,
    ) -> None:
        """Persist returned model content with a safe parse-failure terminal."""
        ledger = self._request_ledger
        if lease is None or ledger is None:
            return
        try:
            ledger.record_response(
                lease,
                response,
                outcome=ResponseOutcome.PARSE_FAILURE,
                chunk_count=1,
                error_code=error_code,
                error_digest=self._error_digest(error),
            )
        except Exception as ledger_error:
            raise error from ledger_error

    @classmethod
    def _reasoning_metadata(cls, response: Any, usage: _Usage) -> ReasoningMetadata:
        """Summarise provider reasoning without retaining its text."""
        message = response.choices[0].message
        content = cls._field(message, "reasoning_content")
        if isinstance(content, str):
            return ReasoningMetadata(
                status=ReasoningStatus.PRESENT,
                token_count=usage.reasoning_output_tokens,
                digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        if usage.reasoning_output_tokens > 0:
            return ReasoningMetadata(
                status=ReasoningStatus.INVALID,
                token_count=usage.reasoning_output_tokens,
            )
        return ReasoningMetadata()

    def _call(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> tuple[Any, RequestLease | None]:
        litellm = self._litellm
        if self.provider_name == "anthropic":
            # Set this before `record_gateway_request`: a process-global LiteLLM flag would inject
            # it later inside the SDK and make the durable request differ from the one dispatched.
            kwargs.setdefault("cache_control", dict(ANTHROPIC_PROMPT_CACHE_CONTROL))
        if self.applies_reasoning_effort and self.reasoning_effort is not None:
            kwargs.setdefault("reasoning_effort", self.reasoning_effort)
        kwargs.setdefault("timeout", self.timeout_s)
        lease = None
        ledger = self._request_ledger
        if ledger is not None:
            self._request_attempt += 1
            owner_id = self._request_owner_id or ""
            lease = ledger.record_gateway_request(
                self.model,
                messages,
                kwargs,
                owner_id=owner_id,
                attempt=self._request_attempt,
                retry_of=self._previous_request_id,
                runtime_skill_evidence=getattr(self, "_runtime_skill_evidence", None),
            )
            self._previous_request_id = lease.request.id
        # Resolving the endpoint is a configuration decision, not a gateway call: it stays outside
        # the try so a refusal surfaces as `LLMConfigurationError` instead of being relabelled a
        # call failure by the safety net below. It runs after the ledger recorded the effective
        # request and before dispatch, so both orderings hold.
        resolved_proxy_endpoint = resolve_proxy_endpoint(
            self.proxy_endpoint,
            declared_safe_endpoints=self._declared_safe_endpoints,
            litellm_sdk=litellm,
        )
        errors = _gateway_exceptions(litellm)
        try:
            return (
                litellm.completion(
                    model=self.model,
                    messages=messages,
                    **with_proxy_endpoint(kwargs, resolved_proxy_endpoint),
                ),
                lease,
            )
        except asyncio.CancelledError as exc:
            self._record_failure(lease, ResponseOutcome.CANCEL, "provider_cancelled", exc)
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._record_failure(lease, ResponseOutcome.UNKNOWN, "provider_unknown", exc)
            raise
        except errors.Timeout as exc:
            self._record_failure(lease, ResponseOutcome.TIMEOUT, "provider_timeout", exc)
            raise LLMTimeoutError(f"the LLM provider timed out ({self.provider_name})") from exc
        except (
            errors.AuthenticationError,
            errors.PermissionDeniedError,
        ) as exc:
            self._record_failure(lease, ResponseOutcome.PROVIDER_ERROR, "provider_error", exc)
            raise LLMCallError(
                f"authentication failed with the LLM provider ({self.provider_name})"
            ) from exc
        except errors.RateLimitError as exc:
            self._record_failure(lease, ResponseOutcome.PROVIDER_ERROR, "provider_error", exc)
            raise LLMCallError(
                f"rate limit reached on the LLM provider ({self.provider_name})"
            ) from exc
        except (
            errors.APIConnectionError,
            errors.ServiceUnavailableError,
        ) as exc:
            self._record_failure(lease, ResponseOutcome.PROVIDER_ERROR, "provider_error", exc)
            raise LLMCallError(
                f"network error contacting the LLM provider ({self.provider_name})"
            ) from exc
        except (
            errors.BadRequestError,
            errors.NotFoundError,
        ) as exc:
            self._record_failure(lease, ResponseOutcome.PROVIDER_ERROR, "provider_error", exc)
            raise LLMCallError(
                f"invalid request to the LLM provider ({self.provider_name})"
            ) from exc
        except Exception as exc:  # safety net: LiteLLM has no single common base
            self._record_failure(lease, ResponseOutcome.PROVIDER_ERROR, "provider_error", exc)
            raise LLMCallError(f"call to the LLM provider failed ({self.provider_name})") from exc
        except BaseException as exc:  # preserve durable evidence for opaque aborts
            self._record_failure(lease, ResponseOutcome.UNKNOWN, "provider_unknown", exc)
            raise

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        """Return one conversational response, including any requested tool calls."""
        rendered_messages = self._build_turn_messages(messages)
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = self._build_tool_definitions(tools)
        if parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = parallel_tool_calls
        response, lease = self._call(rendered_messages, **kwargs)
        usage = self._usage(response)
        cost, cost_metadata = self._cost(response)
        try:
            tool_calls = self._parse_tool_calls(response)
        except LLMCallError as exc:
            failure = LLMResponse(
                text=response.choices[0].message.content or "",
                provider=self.provider_name,
                model=getattr(response, "model", None) or self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                cost_usd=cost,
                metadata={"integration": "litellm", "api": "completion.tools"},
                reasoning=self._reasoning_metadata(response, usage),
            )
            self._record_response_failure(lease, failure, "tool_call_parse_failure", exc)
            raise
        result = LLMResponse(
            text=response.choices[0].message.content or "",
            provider=self.provider_name,
            model=getattr(response, "model", None) or self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            cost_usd=cost,
            metadata={
                "integration": "litellm",
                "api": "completion.tools",
                "model_requested": self.model,
                "response_id": getattr(response, "id", None),
                **cost_metadata,
                **self._effort_metadata(),
            },
            tool_calls=tool_calls,
            reasoning=self._reasoning_metadata(response, usage),
        )
        ledger = self._request_ledger
        if lease is not None and ledger is not None:
            ledger.record_response(lease, result)
        return result

    def _cost(self, response: Any) -> tuple[float | None, dict[str, Any]]:
        litellm = self._litellm
        response_model = getattr(response, "model", None)
        # litellm.model_cost keys some models (verified with Anthropic's) without the
        # "<provider>/" prefix even though that is the id passed to completion(); both forms
        # are checked so a model with a known price is not reported as unknown.
        candidates = {value for value in (response_model, self.model) if value}
        candidates |= {value.split("/", 1)[1] for value in candidates if "/" in value}
        known = any(candidate in litellm.model_cost for candidate in candidates)
        cost: float | None = None
        if known:
            try:
                cost = float(litellm.completion_cost(completion_response=response))
            except Exception:  # noqa: BLE001  # records the failure and continues without a cost
                cost = None
                known = False
        return cost, {"cost_source": "litellm.completion_cost", "cost_known": known}

    @staticmethod
    def _usage(response: Any) -> _Usage:
        """Read what the provider reported, including the two nested counts when it has them.

        LiteLLM normalises providers onto OpenAI's shape, where cache hits live in
        ``usage.prompt_tokens_details.cached_tokens`` and reasoning in
        ``usage.completion_tokens_details.reasoning_tokens``. Both are *subsets* of the totals
        beside them, and splitting them into exclusive buckets is the trace's job, not this one's.
        """
        usage = getattr(response, "usage", None)
        return _Usage(
            input_tokens=_count(usage, "prompt_tokens"),
            output_tokens=_count(usage, "completion_tokens"),
            cached_input_tokens=_count(_attribute(usage, "prompt_tokens_details"), "cached_tokens"),
            reasoning_output_tokens=_count(
                _attribute(usage, "completion_tokens_details"), "reasoning_tokens"
            ),
        )

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        """Return a free-text completion for ``prompt`` (with an optional ``system`` message)."""
        messages = self._build_messages(prompt, system)
        response, lease = self._call(messages)
        text = response.choices[0].message.content or ""
        usage = self._usage(response)
        cost, cost_metadata = self._cost(response)
        result = LLMResponse(
            text=text,
            provider=self.provider_name,
            model=getattr(response, "model", None) or self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            cost_usd=cost,
            metadata={
                "integration": "litellm",
                "api": "completion",
                "model_requested": self.model,
                "response_id": getattr(response, "id", None),
                **cost_metadata,
                **self._effort_metadata(),
            },
            reasoning=self._reasoning_metadata(response, usage),
        )
        ledger = self._request_ledger
        if lease is not None and ledger is not None:
            ledger.record_response(lease, result)
        return result

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        """Request a Pydantic-validated output via ``response_format``.

        The returned JSON is always validated locally before it is handed back: free text is
        never silently accepted as a valid decision, and an invalid response is never padded
        with defaults.

        Raises:
            LLMStructuredOutputError: If the model does not support structured output, or the
                response is not valid JSON, or it does not satisfy ``schema``. When LiteLLM
                returned a response, the exception carries its usage and content for accounting
                and audit; a pre-call capability failure carries no response.
            LLMCallError: If the remote call fails.
        """
        litellm = self._litellm
        if not litellm.supports_response_schema(model=self.model):
            raise LLMStructuredOutputError(
                f"the model '{self.model}' does not support structured output in LiteLLM"
            )
        messages = self._build_messages(prompt, system)
        response, lease = self._call(messages, response_format=schema)
        content = response.choices[0].message.content
        usage = self._usage(response)
        cost, cost_metadata = self._cost(response)
        failure = LLMResponse(
            text=content if isinstance(content, str) else "",
            provider=self.provider_name,
            model=getattr(response, "model", None) or self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            cost_usd=cost,
            metadata={
                "integration": "litellm",
                "api": "completion.response_format",
                "model_requested": self.model,
                "response_id": getattr(response, "id", None),
                "schema": schema.__name__,
                **cost_metadata,
                **self._effort_metadata(),
            },
            reasoning=self._reasoning_metadata(response, usage),
        )
        try:
            raw = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            self._record_response_failure(lease, failure, "structured_parse_failure", exc)
            raise LLMStructuredOutputError(
                "the model response is not valid JSON", response=failure
            ) from None
        try:
            validated = schema.model_validate(raw)
        except ValidationError as exc:
            self._record_response_failure(lease, failure, "structured_parse_failure", exc)
            raise LLMStructuredOutputError(
                f"the response does not satisfy the expected schema {schema.__name__}",
                response=failure,
            ) from None

        llm_response = LLMResponse(
            text=validated.model_dump_json(),
            provider=self.provider_name,
            model=getattr(response, "model", None) or self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            cost_usd=cost,
            metadata={
                "integration": "litellm",
                "api": "completion.response_format",
                "model_requested": self.model,
                "response_id": getattr(response, "id", None),
                "schema": schema.__name__,
                **cost_metadata,
                **self._effort_metadata(),
            },
            reasoning=self._reasoning_metadata(response, usage),
        )
        ledger = self._request_ledger
        if lease is not None and ledger is not None:
            ledger.record_response(lease, llm_response)
        return validated, llm_response
