"""Provider request capture at the effective gateway boundary.

The ledger is a producer-side adapter. It records the model-visible header, ordered message
surface, and the canonical gateway arguments before dispatch, then records response chunks after a
provider returns or a safe terminal outcome before a provider failure escapes. The replay
implementation deliberately lives in MIRA and has its own fold.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from thymira.agents.llm.base import (
    LLMCallError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    Actor,
    Event,
    EventType,
    ProviderGatewayRequest,
    ProviderMessage,
    ProviderRequest,
    ProviderToolCall,
    ProviderToolDefinition,
    ReasoningMetadata,
    ReasoningStatus,
    RequestFactOwner,
    RequestHeader,
    ResponseChunk,
    ResponseOutcome,
    ResponseTerminal,
    RuntimeSkillRequestBinding,
    SurfaceOperation,
    new_id,
)

if TYPE_CHECKING:
    from thymira.agents.llm.base import (
        BaseModelT,
        LLMMessage,
        LLMProvider,
        LLMResponse,
        LLMToolDefinition,
    )
    from thymira.events import EventLog

_OWNER_FACTS = ("model", "system", "settings", "tools", "output_schema", "messages")
_HEADER_DIGEST = "header_sha256"
_SURFACE_OPERATION = "surface_operation"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class RequestLedgerError(ValueError):
    """The request ledger cannot prove a provider input or output."""


def _error_digest(error: BaseException) -> str:
    """Hash only an exception type for safe terminal evidence."""
    kind = f"{type(error).__module__}.{type(error).__qualname__}"
    return sha256_text(kind)


def _failure_kind(error: BaseException) -> tuple[ResponseOutcome, str]:
    """Classify a provider failure without persisting its message."""
    if isinstance(error, asyncio.CancelledError):
        return ResponseOutcome.CANCEL, "provider_cancelled"
    if isinstance(error, (LLMTimeoutError, TimeoutError)):
        return ResponseOutcome.TIMEOUT, "provider_timeout"
    if isinstance(error, LLMStructuredOutputError):
        return ResponseOutcome.PARSE_FAILURE, "structured_parse_failure"
    if isinstance(error, LLMCallError):
        return ResponseOutcome.PROVIDER_ERROR, "provider_error"
    return ResponseOutcome.UNKNOWN, "provider_unknown"


@dataclass(frozen=True, slots=True)
class RequestLease:
    """The durable request identity returned immediately before provider dispatch."""

    request: ProviderRequest
    event: Event


def _json_value(value: object, *, path: str = "value") -> Any:
    """Convert a provider argument to a JSON-shaped value or refuse it."""
    if value is None or isinstance(value, str | int | bool | float):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, type) and issubclass(value, BaseModel):
        return value.model_json_schema()
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            rendered[key] = _json_value(item, path=f"{path}.{key}")
        return rendered
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item, path=f"{path}[]") for item in value]
    raise TypeError(f"{path} contains unsupported provider value {type(value).__name__}")


def _tool_call(value: object) -> ProviderToolCall:
    """Translate an OpenAI or provider-neutral tool call into the shared record."""
    if not isinstance(value, Mapping):
        raise TypeError("provider tool call must be an object")
    function = value.get("function")
    if isinstance(function, Mapping):
        name = function.get("name", "")
        arguments = function.get("arguments", "{}")
    else:
        name = value.get("name", "")
        arguments = value.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise RequestLedgerError("provider tool-call arguments are not JSON") from exc
    if not isinstance(arguments, Mapping):
        raise RequestLedgerError("provider tool-call arguments must be an object")
    return ProviderToolCall(
        id=str(value.get("id", "")),
        name=str(name),
        arguments=dict(_json_value(arguments, path="tool_call.arguments")),
    )


def _message(value: object) -> ProviderMessage:
    """Translate one actual provider message into a closed shared record."""
    if not isinstance(value, Mapping):
        raise TypeError("provider message must be an object")
    raw_calls = value.get("tool_calls", ())
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, str | bytes | bytearray):
        raise TypeError("provider message tool_calls must be a sequence")
    return ProviderMessage(
        role=value.get("role", ""),
        content=value.get("content", "") or "",
        tool_call_id=value.get("tool_call_id"),
        tool_calls=tuple(_tool_call(call) for call in raw_calls),
    )


def _tool_definition(value: object) -> ProviderToolDefinition:
    """Translate one actual provider tool schema into the shared record."""
    if not isinstance(value, Mapping):
        raise TypeError("provider tool definition must be an object")
    function = value.get("function")
    if not isinstance(function, Mapping):
        raise TypeError("provider tool definition function must be an object")
    parameters = function.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise TypeError("provider tool definition parameters must be an object")
    return ProviderToolDefinition(
        name=str(function.get("name", "")),
        description=function.get("description"),
        parameters=dict(_json_value(parameters, path="tool.parameters")),
    )


def _schema_from_kwargs(value: object) -> dict[str, Any] | None:
    """Project LiteLLM's response-format argument into the effective JSON schema."""
    if value is None:
        return None
    projected = _json_value(value, path="response_format")
    if not isinstance(projected, dict):
        raise TypeError("response_format must project to an object")
    return projected


def _owners(owner_id: str, owners: Mapping[str, str] | None) -> tuple[RequestFactOwner, ...]:
    """Validate and close the owner map for all model-visible input facts."""
    resolved = dict.fromkeys(_OWNER_FACTS, owner_id)
    if owners is not None:
        for fact, value in owners.items():
            if fact not in _OWNER_FACTS:
                raise RequestLedgerError(f"unknown request fact owner {fact!r}")
            resolved[fact] = value
    if any(not isinstance(value, str) or not value.strip() for value in resolved.values()):
        raise RequestLedgerError("every provider input fact requires a non-empty owner")
    return tuple(RequestFactOwner(fact=fact, owner_id=resolved[fact]) for fact in _OWNER_FACTS)


def _runtime_skill_binding(
    evidence: Mapping[str, object] | None,
) -> RuntimeSkillRequestBinding | None:
    """Validate the selected projection binding supplied for one gateway request."""
    if not evidence:
        return None
    expected = {
        "orchestrator": evidence.get("runtime_skill_orchestrator"),
        "selection_id": evidence.get("runtime_skill_selection_id"),
        "manifest_sha256": evidence.get("runtime_skill_manifest_sha256"),
        "dispatch_id": evidence.get("runtime_skill_dispatch_id"),
        "projection_sha256": evidence.get("runtime_skill_projection_sha256"),
        "projection_size_bytes": evidence.get("runtime_skill_projection_size_bytes"),
        "selection_event_id": evidence.get("runtime_skill_selection_event_id"),
        "selection_event_hash": evidence.get("runtime_skill_selection_event_hash"),
    }
    if any(value is None for value in expected.values()):
        raise RequestLedgerError(
            "runtime skill request evidence must include the complete selection binding"
        )
    try:
        return RuntimeSkillRequestBinding.model_validate(expected)
    except ValueError as exc:
        raise RequestLedgerError("runtime skill request evidence is invalid") from exc


def _reasoning(response: LLMResponse) -> ReasoningMetadata:
    """Derive safe reasoning metadata while dropping any reasoning text."""
    provider_token_count = max(0, response.reasoning_output_tokens)
    metadata = response.metadata
    raw = metadata.get("reasoning")
    typed = response.reasoning
    raw_status: object = typed.status
    raw_tokens: object = (
        provider_token_count if typed.status is ReasoningStatus.ABSENT else typed.token_count
    )
    raw_digest: object = typed.digest
    if isinstance(raw, Mapping):
        raw_status = raw.get("status", raw_status)
        raw_tokens = raw.get("token_count", provider_token_count)
        raw_digest = raw.get("digest", raw.get("sha256"))

    status = (
        raw_status
        if isinstance(raw_status, ReasoningStatus)
        else ReasoningStatus(raw_status)
        if isinstance(raw_status, str) and raw_status in {item.value for item in ReasoningStatus}
        else ReasoningStatus.INVALID
    )
    token_count = raw_tokens if isinstance(raw_tokens, int) and raw_tokens >= 0 else 0
    malformed_tokens = not isinstance(raw_tokens, int) or raw_tokens < 0
    digest = raw_digest if isinstance(raw_digest, str) else None
    malformed_digest = raw_digest is not None and not _SHA256_HEX.fullmatch(str(raw_digest))
    invalid = malformed_tokens or malformed_digest or status is ReasoningStatus.INVALID
    if "reasoning_text" in metadata:
        text = metadata.get("reasoning_text")
        if isinstance(text, str):
            calculated = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest is not None and digest != calculated:
                invalid = True
            else:
                digest = calculated
                status = ReasoningStatus.PRESENT
        else:
            invalid = True
    elif token_count > 0 and status is ReasoningStatus.ABSENT:
        invalid = True
    if status is ReasoningStatus.ABSENT and digest is not None:
        invalid = True
    if status is ReasoningStatus.PRESENT and digest is None:
        invalid = True
    if invalid:
        # Never carry an unverified provider value into the event payload.  The count remains the
        # provider's safe numeric count so MIRA can distinguish malformed metadata from absence.
        return ReasoningMetadata(status=ReasoningStatus.INVALID, token_count=provider_token_count)
    if token_count != provider_token_count:
        return ReasoningMetadata(status=ReasoningStatus.INVALID, token_count=provider_token_count)
    return ReasoningMetadata(status=status, token_count=provider_token_count, digest=digest)


class RequestLedger:
    """Append request and response evidence for one event-log-backed Run."""

    def __init__(self, event_log: EventLog, *, actor: Actor | None = None) -> None:
        """Create a ledger over the supplied authoritative event log."""
        self.event_log = event_log
        self.actor = actor or Actor.system()
        self._lock = threading.RLock()

    def _latest_header(self) -> tuple[RequestHeader, Event] | None:
        """Read the newest valid header revision from the durable log."""
        for event in reversed(self.event_log.events()):
            if event.type is not EventType.MODEL_INPUT_HEADER_REVISED:
                continue
            payload = dict(event.payload)
            payload.pop(_HEADER_DIGEST, None)
            try:
                header = RequestHeader.model_validate(payload)
            except ValueError as exc:
                raise RequestLedgerError(f"invalid request header event {event.event_id}") from exc
            expected = event.payload.get(_HEADER_DIGEST)
            if expected != sha256_text(canonical_json(header.to_json_dict())):
                raise RequestLedgerError(f"request header digest mismatch at {event.event_id}")
            return header, event
        return None

    @staticmethod
    def _header(
        *,
        header_id: str,
        revision: int,
        reason: str,
        model: str,
        system: str,
        system_messages: tuple[ProviderMessage, ...],
        settings: dict[str, Any],
        tools: tuple[ProviderToolDefinition, ...],
        output_schema: dict[str, Any] | None,
        owners: tuple[RequestFactOwner, ...],
    ) -> RequestHeader:
        """Build one fully typed header without an untyped expansion dictionary."""
        return RequestHeader(
            id=header_id,
            revision=revision,
            reason=reason,
            model=model,
            system=system,
            system_messages=system_messages,
            settings=settings,
            tools=tools,
            output_schema=output_schema,
            owners=owners,
        )

    def replace_surface(
        self,
        messages: Sequence[ProviderMessage],
        *,
        owner_id: str,
        reason: str = "replace effective provider message surface",
    ) -> Event:
        """Record a complete surface replacement, including an intentionally empty one."""
        operation = SurfaceOperation(
            operation="replace",
            messages=tuple(messages),
            owner_id=owner_id,
            reason=reason,
        )
        return self.event_log.append(
            EventType.MODEL_INPUT_SURFACE_UPDATED,
            self.actor,
            operation.to_json_dict(),
            producer="thymira.agents.request_ledger",
            producer_version="0.1",
        )

    def append_surface(
        self,
        messages: Sequence[ProviderMessage],
        *,
        owner_id: str,
        reason: str = "append provider message surface",
    ) -> Event:
        """Record an ordered surface append with an explicit owner."""
        operation = SurfaceOperation(
            operation="append",
            messages=tuple(messages),
            owner_id=owner_id,
            reason=reason,
        )
        return self.event_log.append(
            EventType.MODEL_INPUT_SURFACE_UPDATED,
            self.actor,
            operation.to_json_dict(),
            producer="thymira.agents.request_ledger",
            producer_version="0.1",
        )

    def record_gateway_request(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        kwargs: Mapping[str, Any],
        *,
        owner_id: str,
        attempt: int = 1,
        retry_of: str | None = None,
        owners: Mapping[str, str] | None = None,
        runtime_skill_evidence: Mapping[str, object] | None = None,
        reason: str = "record effective provider request",
    ) -> RequestLease:
        """Record one request atomically when several provider calls share a Run ledger."""
        with self._lock:
            return self._record_gateway_request(
                model,
                messages,
                kwargs,
                owner_id=owner_id,
                attempt=attempt,
                retry_of=retry_of,
                owners=owners,
                runtime_skill_evidence=runtime_skill_evidence,
                reason=reason,
            )

    def _record_gateway_request(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        kwargs: Mapping[str, Any],
        *,
        owner_id: str,
        attempt: int = 1,
        retry_of: str | None = None,
        owners: Mapping[str, str] | None = None,
        runtime_skill_evidence: Mapping[str, object] | None = None,
        reason: str = "record effective provider request",
    ) -> RequestLease:
        """Persist effective gateway input before dispatch and return its durable identity."""
        if not isinstance(model, str) or not model.strip():
            raise RequestLedgerError("provider request model requires a non-empty value")
        if attempt < 1:
            raise RequestLedgerError("provider request attempt must be at least one")
        if retry_of is not None and not isinstance(retry_of, str):
            raise RequestLedgerError("retry_of must be a request id")
        all_messages = tuple(_message(message) for message in messages)
        system_messages = tuple(message for message in all_messages if message.role == "system")
        system = "\n".join(message.content for message in system_messages)
        raw_tools = kwargs.get("tools", ())
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, str | bytes | bytearray):
            raise TypeError("provider tools must be a sequence")
        tools = tuple(_tool_definition(tool) for tool in raw_tools)
        output_schema = _schema_from_kwargs(kwargs.get("response_format"))
        settings = {
            str(key): _json_value(value, path=f"settings.{key}")
            for key, value in kwargs.items()
            if key not in {"tools", "response_format"}
        }
        owner_records = _owners(owner_id, owners)
        runtime_skill = _runtime_skill_binding(runtime_skill_evidence)
        latest = self._latest_header()
        if latest is not None:
            previous, previous_event = latest
            candidate = self._header(
                header_id=previous.id,
                revision=previous.revision,
                reason=reason,
                model=model,
                system=system,
                system_messages=system_messages,
                settings=settings,
                tools=tools,
                output_schema=output_schema,
                owners=owner_records,
            )
            previous_facts = previous.model_dump(mode="json", exclude={"id", "revision", "reason"})
            candidate_facts = candidate.model_dump(
                mode="json", exclude={"id", "revision", "reason"}
            )
            if previous_facts == candidate_facts:
                header = previous
                header_event = previous_event
            else:
                header = self._header(
                    header_id=new_id("header"),
                    revision=previous.revision + 1,
                    reason=reason,
                    model=model,
                    system=system,
                    system_messages=system_messages,
                    settings=settings,
                    tools=tools,
                    output_schema=output_schema,
                    owners=owner_records,
                )
                header_event = self.event_log.append(
                    EventType.MODEL_INPUT_HEADER_REVISED,
                    self.actor,
                    {
                        **header.to_json_dict(),
                        _HEADER_DIGEST: sha256_text(canonical_json(header.to_json_dict())),
                    },
                    subject_id=header.id,
                    producer="thymira.agents.request_ledger",
                    producer_version="0.1",
                )
        else:
            header = self._header(
                header_id=new_id("header"),
                revision=1,
                reason=reason,
                model=model,
                system=system,
                system_messages=system_messages,
                settings=settings,
                tools=tools,
                output_schema=output_schema,
                owners=owner_records,
            )
            header_event = self.event_log.append(
                EventType.MODEL_INPUT_HEADER_REVISED,
                self.actor,
                {
                    **header.to_json_dict(),
                    _HEADER_DIGEST: sha256_text(canonical_json(header.to_json_dict())),
                },
                subject_id=header.id,
                producer="thymira.agents.request_ledger",
                producer_version="0.1",
            )
        surface_event = self.replace_surface(
            all_messages,
            owner_id=next(owner.owner_id for owner in owner_records if owner.fact == "messages"),
        )
        gateway = ProviderGatewayRequest(
            model=model,
            messages=all_messages,
            tools=tools,
            settings=settings,
            output_schema=output_schema,
        )
        request = ProviderRequest(
            id=new_id("request"),
            attempt=attempt,
            retry_of=retry_of,
            header_event_id=header_event.event_id,
            surface_event_ids=(surface_event.event_id,),
            owners=owner_records,
            gateway=gateway,
            gateway_sha256=sha256_text(canonical_json(gateway.to_json_dict())),
            runtime_skill=runtime_skill,
        )
        request_event = self.event_log.append(
            EventType.MODEL_REQUEST_RECORDED,
            self.actor,
            request.to_json_dict(),
            subject_id=request.id,
            correlation_id=request.id,
            producer="thymira.agents.request_ledger",
            producer_version="0.1",
        )
        return RequestLease(request=request, event=request_event)

    def record_response(
        self,
        lease: RequestLease,
        response: LLMResponse,
        *,
        source_sequence: int = 0,
        complete: bool = True,
        chunk_count: int | None = None,
        outcome: ResponseOutcome = ResponseOutcome.SUCCESS,
        error_code: str | None = None,
        error_digest: str | None = None,
    ) -> Event:
        """Record one response chunk atomically on the shared Run ledger."""
        with self._lock:
            return self._record_response(
                lease,
                response,
                source_sequence=source_sequence,
                complete=complete,
                chunk_count=chunk_count,
                outcome=outcome,
                error_code=error_code,
                error_digest=error_digest,
            )

    def _record_response(
        self,
        lease: RequestLease,
        response: LLMResponse,
        *,
        source_sequence: int = 0,
        complete: bool = True,
        chunk_count: int | None = None,
        outcome: ResponseOutcome = ResponseOutcome.SUCCESS,
        error_code: str | None = None,
        error_digest: str | None = None,
    ) -> Event:
        """Record one response chunk, terminalising it when the call is complete."""
        if source_sequence < 0:
            raise RequestLedgerError("response source sequence must be non-negative")
        if chunk_count is not None and chunk_count < 1:
            raise RequestLedgerError("a successful response requires at least one chunk")
        if not complete and chunk_count is not None:
            raise RequestLedgerError("an incomplete response cannot declare a chunk count")
        if not complete and outcome is not ResponseOutcome.SUCCESS:
            raise RequestLedgerError("an incomplete response cannot declare a terminal outcome")
        resolved_count = chunk_count if chunk_count is not None else source_sequence + 1
        message = ProviderMessage(
            role="assistant",
            content=response.text,
            tool_calls=tuple(
                ProviderToolCall(id=call.id, name=call.name, arguments=call.arguments)
                for call in response.tool_calls
            ),
        )
        chunk = ResponseChunk(
            response_id=new_id("response"),
            request_id=lease.request.id,
            source_sequence=source_sequence,
            message=message,
            input_tokens=max(0, response.input_tokens),
            output_tokens=max(0, response.output_tokens),
            cached_input_tokens=max(0, response.cached_input_tokens),
            reasoning_output_tokens=max(0, response.reasoning_output_tokens),
            reasoning=_reasoning(response),
            terminal=(
                ResponseTerminal(
                    outcome=outcome,
                    chunk_count=resolved_count,
                    error_code=error_code,
                    error_digest=error_digest,
                )
                if complete
                else None
            ),
        )
        return self.event_log.append(
            EventType.MODEL_RESPONSE_CHUNK,
            self.actor,
            chunk.to_json_dict(),
            subject_id=chunk.response_id,
            correlation_id=lease.request.id,
            causation_id=lease.event.event_id,
            producer="thymira.agents.request_ledger",
            producer_version="0.1",
        )

    def record_terminal(
        self,
        lease: RequestLease,
        outcome: ResponseOutcome,
        *,
        chunk_count: int = 0,
        error_code: str | None = None,
        error_digest: str | None = None,
    ) -> Event:
        """Record a terminal outcome atomically on the shared Run ledger."""
        with self._lock:
            return self._record_terminal(
                lease,
                outcome,
                chunk_count=chunk_count,
                error_code=error_code,
                error_digest=error_digest,
            )

    def _record_terminal(
        self,
        lease: RequestLease,
        outcome: ResponseOutcome,
        *,
        chunk_count: int = 0,
        error_code: str | None = None,
        error_digest: str | None = None,
    ) -> Event:
        """Record a terminal outcome without a phantom assistant message."""
        if chunk_count < 0:
            raise RequestLedgerError("response chunk count must be non-negative")
        terminal = ResponseTerminal(
            outcome=outcome,
            chunk_count=chunk_count,
            error_code=error_code,
            error_digest=error_digest,
        )
        marker = ResponseChunk(
            response_id=new_id("response"),
            request_id=lease.request.id,
            source_sequence=chunk_count,
            terminal=terminal,
        )
        return self.event_log.append(
            EventType.MODEL_RESPONSE_CHUNK,
            self.actor,
            marker.to_json_dict(),
            subject_id=marker.response_id,
            correlation_id=lease.request.id,
            causation_id=lease.event.event_id,
            producer="thymira.agents.request_ledger",
            producer_version="0.1",
        )


class LedgerProvider:
    """Provider adapter for non-LiteLLM implementations such as ``ScriptedProvider``.

    LiteLLM is instrumented inside its own effective ``completion`` call. This adapter exists for
    injected providers and records the provider contract they actually receive, without claiming
    to observe settings that an unknown provider may add internally.
    """

    def __init__(self, provider: LLMProvider, ledger: RequestLedger, *, owner_id: str) -> None:
        """Create a recording adapter around one provider."""
        if not owner_id.strip():
            raise ValueError("request ledger owner_id must not be empty")
        if getattr(provider, "opaque_replay_state_required", False) is True:
            raise RequestLedgerError(
                "provider requires opaque replay state but no adapter owns that state"
            )
        self._provider = provider
        self._ledger = ledger
        self._owner_id = owner_id
        self._previous_request_id: str | None = None
        self._attempt = 0
        self._runtime_skill_evidence: Mapping[str, object] | None = None

    @property
    def provider_name(self) -> str:
        """Return the wrapped provider name."""
        return self._provider.provider_name

    @property
    def model(self) -> str:
        """Return the wrapped model identifier."""
        return self._provider.model

    @property
    def ledger(self) -> RequestLedger:
        """Return the durable ledger this adapter writes to."""
        return self._ledger

    @property
    def owner_id(self) -> str:
        """Return the owner attached to this adapter."""
        return self._owner_id

    def bind_runtime_skill_selection(
        self, selection_event: Event, evidence: Mapping[str, object]
    ) -> None:
        """Bind the next request(s) to the hash-chained selection event.

        A selection is made before the provider call and may cover several turns or retries. The
        request ledger assigns each turn its own ``ProviderRequest.id`` while this binding keeps
        every request tied to the same immutable selection. An empty evidence map clears a prior
        binding so an ordinary model call cannot inherit stale runtime-skill state.
        """
        if not evidence:
            self._runtime_skill_evidence = None
            return
        if selection_event.hash is None:
            raise RequestLedgerError("runtime skill selection event has no chain hash")
        self._runtime_skill_evidence = {
            **evidence,
            "runtime_skill_selection_event_id": str(selection_event.event_id),
            "runtime_skill_selection_event_hash": selection_event.hash,
        }

    @property
    def opaque_replay_state_required(self) -> bool:
        """Return the wrapped provider's explicit opaque-state requirement."""
        return bool(getattr(self._provider, "opaque_replay_state_required", False))

    def _record_failure(self, lease: RequestLease, error: BaseException) -> None:
        """Persist a returned parse failure or a response-less terminal outcome safely."""
        outcome, error_code = _failure_kind(error)
        try:
            if isinstance(error, LLMStructuredOutputError) and error.response is not None:
                self._ledger.record_response(
                    lease,
                    error.response,
                    outcome=outcome,
                    chunk_count=1,
                    error_code=error_code,
                    error_digest=_error_digest(error),
                )
            else:
                self._ledger.record_terminal(
                    lease,
                    outcome,
                    error_code=error_code,
                    error_digest=_error_digest(error),
                )
        except Exception as ledger_error:
            raise error from ledger_error

    def _record(
        self,
        messages: Sequence[Mapping[str, Any]],
        kwargs: Mapping[str, Any],
    ) -> RequestLease:
        """Persist one provider-contract request before invoking the wrapped provider."""
        self._attempt += 1
        lease = self._ledger.record_gateway_request(
            self.model,
            messages,
            kwargs,
            owner_id=self._owner_id,
            attempt=self._attempt,
            retry_of=self._previous_request_id,
            runtime_skill_evidence=self._runtime_skill_evidence,
        )
        self._previous_request_id = lease.request.id
        return lease

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        """Record and execute one conversational provider call."""
        rendered_messages = tuple(message.to_json_dict() for message in provider_messages(messages))
        rendered_tools = tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                },
            }
            for tool in tools
        )
        settings: dict[str, object] = {"tools": rendered_tools} if tools else {}
        if parallel_tool_calls is not None:
            settings["parallel_tool_calls"] = parallel_tool_calls
        lease = self._record(rendered_messages, settings)
        try:
            response = self._provider.complete_turn(
                messages,
                tools=tools,
                parallel_tool_calls=parallel_tool_calls,
            )
        except asyncio.CancelledError as exc:
            self._record_failure(lease, exc)
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._record_failure(lease, exc)
            raise
        except Exception as exc:
            self._record_failure(lease, exc)
            raise
        except BaseException as exc:  # preserve durable evidence for opaque aborts
            self._record_failure(lease, exc)
            raise
        self._ledger.record_response(lease, response)
        return response

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        """Record and execute one free-text provider call."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        lease = self._record(messages, {})
        try:
            response = self._provider.complete(prompt, system=system)
        except asyncio.CancelledError as exc:
            self._record_failure(lease, exc)
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._record_failure(lease, exc)
            raise
        except Exception as exc:
            self._record_failure(lease, exc)
            raise
        except BaseException as exc:  # preserve durable evidence for opaque aborts
            self._record_failure(lease, exc)
            raise
        self._ledger.record_response(lease, response)
        return response

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[BaseModelT],
        system: str = "",
    ) -> tuple[BaseModelT, LLMResponse]:
        """Record and execute one structured provider call."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        lease = self._record(messages, {"response_format": schema})
        try:
            parsed, response = self._provider.complete_structured(
                prompt, schema=schema, system=system
            )
        except asyncio.CancelledError as exc:
            self._record_failure(lease, exc)
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            self._record_failure(lease, exc)
            raise
        except Exception as exc:
            self._record_failure(lease, exc)
            raise
        except BaseException as exc:  # preserve durable evidence for opaque aborts
            self._record_failure(lease, exc)
            raise
        self._ledger.record_response(lease, response)
        return parsed, response


def instrument_provider(
    provider: LLMProvider,
    ledger: RequestLedger,
    *,
    owner_id: str,
) -> LLMProvider:
    """Attach one ledger to a provider through the canonical request boundary.

    ``LiteLLMProvider`` exposes ``attach_request_ledger`` so capture happens after LiteLLM has
    resolved its effective settings and schemas. Injected providers do not have that internal
    boundary, so they are wrapped by :class:`LedgerProvider`, which records the arguments they
    actually receive. Keeping this choice here gives every direct production caller one seam and
    prevents a second assembler from growing in THY or MIRA.
    """
    if not owner_id.strip():
        raise ValueError("request ledger owner_id must not be empty")
    if getattr(provider, "opaque_replay_state_required", False) is True:
        raise RequestLedgerError(
            "provider requires opaque replay state but no adapter owns that state"
        )
    attach = getattr(provider, "attach_request_ledger", None)
    if callable(attach):
        attach(ledger, owner_id=owner_id)
        return provider
    if isinstance(provider, LedgerProvider):
        if provider.ledger is ledger and provider.owner_id == owner_id:
            return provider
        raise RequestLedgerError("provider is already instrumented by another request ledger")
    return cast("LLMProvider", LedgerProvider(provider, ledger, owner_id=owner_id))


def provider_messages(messages: Sequence[LLMMessage]) -> tuple[ProviderMessage, ...]:
    """Convert the provider-neutral runtime messages into shared ledger records."""
    return tuple(
        ProviderMessage(
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            tool_calls=tuple(
                ProviderToolCall(id=call.id, name=call.name, arguments=call.arguments)
                for call in message.tool_calls
            ),
        )
        for message in messages
    )


__all__ = [
    "LedgerProvider",
    "RequestLease",
    "RequestLedger",
    "RequestLedgerError",
    "instrument_provider",
    "provider_messages",
]
