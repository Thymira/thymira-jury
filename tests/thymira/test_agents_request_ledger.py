"""Behavioral tests for provider request capture and independent replay."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel

from thymira.agents import LedgerProvider, RequestLedger, RequestLedgerError
from thymira.agents.llm import (
    LiteLLMProvider,
    LLMCallError,
    LLMMessage,
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
    ScriptedProvider,
)
from thymira.events import EventLog, InMemoryEventLog
from thymira.mira.checks import RequestReplayError, replay_request_ledger
from thymira.schemas import (
    Actor,
    Event,
    EventType,
    ProviderMessage,
    ReasoningMetadata,
    ReasoningStatus,
    ResponseChunk,
    ResponseOutcome,
    ResponseTerminal,
    new_id,
)


def _log() -> InMemoryEventLog:
    """Create a fresh event log for one test Run."""
    return InMemoryEventLog(new_id("run"))


class _Result(BaseModel):
    """Minimal schema for the structured request path."""

    answer: str


@pytest.fixture
def no_ambient_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct dispatch refuses an ambient endpoint, so the gateway double must not inherit one."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)


def _fake_litellm_provider(
    ledger: RequestLedger,
    capture: list[dict[str, Any]],
    *,
    completion_error: Exception | None = None,
) -> LiteLLMProvider:
    """Build a provider around a gateway capture double without importing a network client."""
    message = SimpleNamespace(content="answer", reasoning_content=None, tool_calls=())
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=2,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
        model="test-model",
        id="response-1",
    )

    class _Gateway:
        model_cost: ClassVar[dict[str, float]] = {}
        exceptions = SimpleNamespace(
            AuthenticationError=type("AuthenticationError", (Exception,), {}),
            PermissionDeniedError=type("PermissionDeniedError", (Exception,), {}),
            RateLimitError=type("RateLimitError", (Exception,), {}),
            APIConnectionError=type("APIConnectionError", (Exception,), {}),
            Timeout=type("Timeout", (Exception,), {}),
            ServiceUnavailableError=type("ServiceUnavailableError", (Exception,), {}),
            BadRequestError=type("BadRequestError", (Exception,), {}),
            NotFoundError=type("NotFoundError", (Exception,), {}),
        )

        @staticmethod
        def completion(**kwargs: Any) -> object:
            capture.append(kwargs)
            if completion_error is not None:
                raise completion_error
            return response

    provider = object.__new__(LiteLLMProvider)
    provider._litellm = _Gateway()
    provider.model = "test-model"
    # Direct dispatch with nothing declared: the double exercises the ledger, not the proxy seam.
    provider.proxy_endpoint = None
    provider._declared_safe_endpoints = frozenset[str]()
    provider.provider_name = "openai"
    provider.reasoning_effort = "high"
    provider.applies_reasoning_effort = True
    provider.timeout_s = 17.0
    provider._request_ledger = ledger
    provider._request_owner_id = "agent_test"
    provider._request_attempt = 0
    provider._previous_request_id = None
    return provider


def test_recording_provider_replays_the_effective_free_text_gateway_request() -> None:
    """The ledger records one owned request before the scripted provider answers."""
    log = _log()
    ledger = RequestLedger(log)
    provider = LedgerProvider(
        ScriptedProvider(["hello"], model="test-model"), ledger, owner_id="agent_test"
    )

    provider.complete("question", system="instructions")

    report = replay_request_ledger(tuple(log.events()))
    assert len(report.requests) == 1
    assert report.requests[0].gateway.model == "test-model"
    assert [message.content for message in report.requests[0].gateway.messages] == [
        "instructions",
        "question",
    ]
    assert report.responses[0].message is not None
    assert report.responses[0].message.content == "hello"
    assert report.responses[0].source_event_seqs == (3,)


def test_litellm_capture_observes_effective_settings_before_gateway_dispatch() -> None:
    """The LiteLLM seam records timeout/effort after resolution and before the gateway double."""
    log = _log()
    capture: list[dict[str, Any]] = []
    provider = _fake_litellm_provider(RequestLedger(log), capture)

    provider.complete("question", system="instructions")

    assert capture == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "question"},
            ],
            "reasoning_effort": "high",
            "timeout": 17.0,
        }
    ]
    request = replay_request_ledger(tuple(log.events())).requests[0].gateway
    assert request.settings == {"reasoning_effort": "high", "timeout": 17.0}


def test_anthropic_prompt_caching_is_both_dispatched_and_recorded() -> None:
    """The durable request must include the cache setting that LiteLLM actually receives."""
    log = _log()
    capture: list[dict[str, Any]] = []
    provider = _fake_litellm_provider(RequestLedger(log), capture)
    provider.model = "anthropic/claude-sonnet-4-5"
    provider.provider_name = "anthropic"

    provider.complete("question", system="instructions")

    expected = {"type": "ephemeral"}
    assert capture[0]["cache_control"] == expected
    request = replay_request_ledger(tuple(log.events())).requests[0].gateway
    assert request.settings["cache_control"] == expected


def test_parallel_tool_call_setting_is_both_dispatched_and_recorded() -> None:
    """The A3 replay guard is part of the effective gateway request, not wrapper-only state."""
    log = _log()
    capture: list[dict[str, Any]] = []
    provider = _fake_litellm_provider(RequestLedger(log), capture)

    provider.complete_turn(
        [LLMMessage(role="user", content="write one file")],
        parallel_tool_calls=False,
    )

    assert capture[0]["parallel_tool_calls"] is False
    request = replay_request_ledger(tuple(log.events())).requests[0].gateway
    assert request.settings["parallel_tool_calls"] is False


@pytest.mark.usefixtures("no_ambient_endpoint")
def test_litellm_reports_a_gateway_failure_without_a_litellm_exceptions_namespace() -> None:
    """A gateway double that raises reports its own error, not a missing `exceptions` attribute."""
    log = _log()
    capture: list[dict[str, Any]] = []
    provider = _fake_litellm_provider(
        RequestLedger(log), capture, completion_error=RuntimeError("gateway is down")
    )
    # The double otherwise carries an `exceptions` namespace for the other gateway-error tests;
    # this test specifically proves the fallback when a gateway double exposes none.
    provider._litellm.exceptions = None

    with pytest.raises(LLMCallError, match="call to the LLM provider failed") as failure:
        provider.complete("question", system="instructions")

    assert isinstance(failure.value.__cause__, RuntimeError)
    assert str(failure.value.__cause__) == "gateway is down"


def test_recording_provider_captures_structured_schema_and_tool_turn() -> None:
    """The structured and tool-capable provider paths retain schemas as effective inputs."""
    log = _log()
    ledger = RequestLedger(log)
    provider = LedgerProvider(
        ScriptedProvider([{"answer": "ok"}], model="test-model"), ledger, owner_id="agent_test"
    )

    provider.complete_structured("question", schema=_Result, system="rules")

    request = replay_request_ledger(tuple(log.events())).requests[0].gateway
    assert request.output_schema is not None
    assert request.output_schema["title"] == "_Result"
    assert request.messages == (
        ProviderMessage(role="system", content="rules"),
        ProviderMessage(role="user", content="question"),
    )


def test_multiple_system_messages_keep_the_exact_ordered_gateway_surface() -> None:
    """System role boundaries are retained instead of being joined into one prompt string."""
    log = _log()
    ledger = RequestLedger(log)
    messages = (
        {"role": "system", "content": "system-a"},
        {"role": "user", "content": "question"},
        {"role": "system", "content": "system-b"},
    )

    ledger.record_gateway_request("test-model", messages, {}, owner_id="agent_test")

    report = replay_request_ledger(tuple(log.events()))
    assert report.requests[0].gateway.messages == tuple(
        ProviderMessage.model_validate(message) for message in messages
    )
    header = next(
        event.payload for event in log.events() if event.type.value == "model.input_header_revised"
    )
    assert header["system_messages"] == [
        {"role": "system", "content": "system-a", "tool_call_id": None, "tool_calls": []},
        {"role": "system", "content": "system-b", "tool_call_id": None, "tool_calls": []},
    ]


def test_changed_effective_settings_create_a_header_revision() -> None:
    """A setting change gets a new owned header with a reason and remains replayable."""
    log = _log()
    ledger = RequestLedger(log)
    first = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {"timeout": 10},
        owner_id="agent_test",
        reason="initial settings",
    )
    second = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {"timeout": 20},
        owner_id="agent_test",
        reason="increase timeout after provider policy change",
    )

    report = replay_request_ledger(tuple(log.events()))

    assert first.request.attempt == 1
    assert second.request.attempt == 1
    assert len(report.requests) == 2
    headers = [
        event.payload for event in log.events() if event.type.value == "model.input_header_revised"
    ]
    assert [header["revision"] for header in headers] == [1, 2]
    assert headers[1]["reason"] == "increase timeout after provider policy change"


def test_retry_identity_and_tool_results_survive_independent_replay() -> None:
    """Retries reference the prior request and tool-call messages/results stay in the ledger."""
    log = _log()
    ledger = RequestLedger(log)
    provider = LedgerProvider(
        ScriptedProvider(
            [LLMToolCall(id="call-1", name="inspect", arguments={"path": "data.csv"}), "done"],
            model="test-model",
        ),
        ledger,
        owner_id="agent_test",
    )
    tool = LLMToolDefinition(
        name="inspect",
        description="Inspect one file",
        parameters_json_schema={"type": "object"},
    )
    first = provider.complete_turn(
        (LLMMessage(role="user", content="inspect the data"),), tools=(tool,)
    )
    assert first.tool_calls
    provider.complete_turn(
        (
            LLMMessage(role="user", content="inspect the data"),
            LLMMessage(role="assistant", tool_calls=first.tool_calls),
            LLMMessage(role="tool", content="rows=4", tool_call_id="call-1"),
        ),
        tools=(tool,),
    )

    report = replay_request_ledger(tuple(log.events()))

    assert report.requests[1].request.attempt == 2
    assert report.requests[1].request.retry_of == report.requests[0].request.id
    assert report.requests[1].gateway.tools[0].name == "inspect"
    assert report.requests[1].gateway.messages[-1] == ProviderMessage(
        role="tool", content="rows=4", tool_call_id="call-1"
    )
    assert report.responses[0].message is not None
    assert report.responses[0].message.tool_calls[0].name == "inspect"
    assert report.responses[1].message is not None
    assert report.responses[1].message.content == "done"


def test_empty_surface_replacement_is_replayable() -> None:
    """An empty replacement removes all prior context and remains a durable operation."""
    log = _log()
    ledger = RequestLedger(log)
    ledger.append_surface(
        (ProviderMessage(role="user", content="obsolete"),), owner_id="agent_test"
    )
    ledger.replace_surface((), owner_id="agent_test", reason="remove obsolete context")

    report = replay_request_ledger(tuple(log.events()))

    assert report.requests == ()
    assert [event.payload["operation"] for event in log.events()] == ["append", "replace"]


def test_response_chunks_replay_in_source_order_after_shuffled_delivery() -> None:
    """Replay uses provider source sequence rather than event arrival order."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {},
        owner_id="agent_test",
    )
    ledger.record_response(
        lease,
        LLMResponse(text="A", provider="test", model="test-model"),
        source_sequence=1,
        complete=False,
    )
    ledger.record_response(
        lease,
        LLMResponse(text="B", provider="test", model="test-model"),
        source_sequence=0,
        complete=False,
    )
    ledger.record_terminal(lease, ResponseOutcome.SUCCESS, chunk_count=2)

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.message is not None
    assert response.message.content == "BA"
    assert response.source_event_seqs == (4, 3)


def test_reasoning_text_is_never_written_but_metadata_is_replayable() -> None:
    """Reasoning presence is hashable evidence while the content remains absent from events."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {},
        owner_id="agent_test",
    )
    ledger.record_response(
        lease,
        LLMResponse(
            text="answer",
            provider="test",
            model="test-model",
            reasoning_output_tokens=3,
            metadata={"reasoning_text": "SECRET_REASONING"},
        ),
    )

    events_json = " ".join(event.model_dump_json() for event in log.events())
    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert "SECRET_REASONING" not in events_json
    assert response.reasoning_status is ReasoningStatus.PRESENT


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(
            text="answer",
            provider="test",
            model="test-model",
            reasoning_output_tokens=4,
            metadata={
                "reasoning": {
                    "status": "present",
                    "token_count": 4,
                    "digest": "COT_LEDGER_LEAK_9F3",
                }
            },
        ),
        LLMResponse(
            text="answer",
            provider="test",
            model="test-model",
            reasoning_output_tokens=4,
            reasoning=ReasoningMetadata(
                status=ReasoningStatus.PRESENT,
                token_count=4,
                digest="COT_LEDGER_LEAK_9F3",
            ),
        ),
    ],
    ids=["metadata-dict", "typed-metadata"],
)
def test_invalid_reasoning_digest_is_dropped_before_event_append(
    response: LLMResponse,
) -> None:
    """Untrusted digest text cannot enter the durable response payload through either API."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {},
        owner_id="agent_test",
    )

    ledger.record_response(lease, response)

    events_json = " ".join(event.model_dump_json() for event in log.events())
    response_event = next(
        event for event in log.events() if event.type.value == "model.response_chunk"
    )
    assert "COT_LEDGER_LEAK_9F3" not in events_json
    assert response_event.payload["reasoning"] == {
        "status": "invalid",
        "token_count": 4,
        "digest": None,
    }
    assert replay_request_ledger(tuple(log.events())).responses[0].reasoning_status is (
        ReasoningStatus.INVALID
    )


def test_replay_marks_inconsistent_reasoning_metadata_invalid() -> None:
    """A present status without a digest cannot masquerade as verified reasoning evidence."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model",
        [{"role": "user", "content": "question"}],
        {},
        owner_id="agent_test",
    )
    ledger.record_response(
        lease,
        LLMResponse(
            text="answer",
            provider="test",
            model="test-model",
            reasoning=ReasoningMetadata(status=ReasoningStatus.PRESENT),
        ),
    )

    assert replay_request_ledger(tuple(log.events())).responses[0].reasoning_status is (
        ReasoningStatus.INVALID
    )


def test_failed_request_persistence_prevents_provider_dispatch() -> None:
    """A failure while appending request evidence occurs before the provider is called."""

    class FailingLog:
        run_id = new_id("run")

        def events(self) -> list[Any]:
            return []

        def append(self, *_args: Any, **_kwargs: Any) -> object:
            raise OSError("storage unavailable")

    provider = ScriptedProvider(["must not run"])
    recording = LedgerProvider(
        provider,
        RequestLedger(cast("EventLog", FailingLog())),
        owner_id="agent_test",
    )

    with pytest.raises(OSError, match="storage unavailable"):
        recording.complete("question")

    assert provider.calls == []


def test_missing_owner_is_refused_before_request_append() -> None:
    """A request without a durable owner cannot be recorded."""
    log = _log()
    ledger = RequestLedger(log)

    with pytest.raises(RequestLedgerError, match="owner"):
        ledger.record_gateway_request(
            "test-model",
            [{"role": "user", "content": "question"}],
            {},
            owner_id="",
        )

    assert log.events() == []


def test_request_replay_refuses_a_surface_with_an_unknown_operation() -> None:
    """The independent reader does not guess a malformed operation vocabulary."""
    log = _log()
    ledger = RequestLedger(log)
    event = ledger.append_surface((), owner_id="agent_test")
    forged = event.model_copy(update={"payload": {**event.payload, "operation": "merge"}})

    with pytest.raises(RequestReplayError):
        replay_request_ledger((forged,))


def test_actual_litellm_gateway_success_records_a_terminal_chunk() -> None:
    """The effective LiteLLM gateway call yields one source chunk and final proof."""
    log = _log()
    provider = _fake_litellm_provider(RequestLedger(log), [])

    provider.complete("question")

    report = replay_request_ledger(tuple(log.events()))
    response = report.responses[0]
    assert response.outcome is ResponseOutcome.SUCCESS
    assert response.terminal_event_seq == response.source_event_seqs[0]
    assert report.in_flight == ()
    response_event = next(
        event for event in log.events() if event.type.value == "model.response_chunk"
    )
    assert response_event.payload["ledger_version"] == "0.2"
    assert response_event.payload["terminal"] == {
        "ledger_version": "0.2",
        "outcome": "success",
        "chunk_count": 1,
        "error_code": None,
        "error_digest": None,
    }


def test_wrapped_provider_error_is_durable_before_exception() -> None:
    """A provider failure appends an error terminal without a phantom assistant message."""
    log = _log()
    provider = LedgerProvider(ScriptedProvider([]), RequestLedger(log), owner_id="agent_test")

    with pytest.raises(LLMCallError, match="ran out of responses"):
        provider.complete("question")

    report = replay_request_ledger(tuple(log.events()))
    response = report.responses[0]
    assert response.outcome is ResponseOutcome.PROVIDER_ERROR
    assert response.message is None
    assert response.source_event_seqs == ()
    marker = log.events()[-1]
    assert marker.payload["message"] is None
    assert marker.payload["terminal"]["chunk_count"] == 0
    assert "ran out of responses" not in marker.model_dump_json()


def test_actual_litellm_timeout_is_classified_without_exception_text() -> None:
    """The actual LiteLLM adapter records timeout outcome before raising its typed error."""

    class _TimeoutError(Exception):
        pass

    class _Exceptions:
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
        RateLimitError = type("RateLimitError", (Exception,), {})
        APIConnectionError = type("APIConnectionError", (Exception,), {})
        Timeout = _TimeoutError
        ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        BadRequestError = type("BadRequestError", (Exception,), {})
        NotFoundError = type("NotFoundError", (Exception,), {})

    class _Gateway:
        model_cost: ClassVar[dict[str, float]] = {}
        exceptions = _Exceptions

        @staticmethod
        def completion(**_kwargs: Any) -> object:
            raise _TimeoutError("gateway timed out")

    log = _log()
    provider = object.__new__(LiteLLMProvider)
    provider._litellm = _Gateway()
    provider.model = "test-model"
    provider.proxy_endpoint = None
    provider._declared_safe_endpoints = frozenset[str]()
    provider.provider_name = "openai"
    provider.reasoning_effort = None
    provider.applies_reasoning_effort = False
    provider.timeout_s = 17.0
    provider._request_ledger = RequestLedger(log)
    provider._request_owner_id = "agent_test"
    provider._request_attempt = 0
    provider._previous_request_id = None

    with pytest.raises(LLMTimeoutError, match="timed out"):
        provider.complete("question")

    report = replay_request_ledger(tuple(log.events()))
    assert report.responses[0].outcome is ResponseOutcome.TIMEOUT
    assert "gateway timed out" not in log.events()[-1].model_dump_json()


def test_actual_litellm_cancellation_is_durable_and_reraised() -> None:
    """The actual LiteLLM adapter records cancellation before preserving it for its caller."""

    class _Gateway:
        model_cost: ClassVar[dict[str, float]] = {}

        @staticmethod
        def completion(**_kwargs: Any) -> object:
            raise asyncio.CancelledError

    log = _log()
    provider = _fake_litellm_provider(RequestLedger(log), [])
    provider._litellm = _Gateway()

    with pytest.raises(asyncio.CancelledError):
        provider.complete("question")

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.outcome is ResponseOutcome.CANCEL
    assert response.message is None


def test_actual_litellm_unknown_base_exception_is_durable_and_reraised() -> None:
    """An unknown BaseException from the gateway is safely classified before it escapes."""

    class _OpaqueAbort(BaseException):
        pass

    log = _log()
    provider = _fake_litellm_provider(RequestLedger(log), [])

    def fail(**_kwargs: Any) -> object:
        raise _OpaqueAbort("gateway private detail")

    provider._litellm.completion = fail

    with pytest.raises(_OpaqueAbort):
        provider.complete("question")

    report = replay_request_ledger(tuple(log.events()))
    response = report.responses[0]
    assert response.outcome is ResponseOutcome.UNKNOWN
    assert response.message is None
    assert "gateway private detail" not in log.events()[-1].model_dump_json()


def test_actual_litellm_structured_parse_failure_is_terminal() -> None:
    """The effective LiteLLM response parser closes a malformed response in the ledger."""

    class _Gateway:
        model_cost: ClassVar[dict[str, float]] = {}
        supports_response_schema = staticmethod(lambda **_kwargs: True)

        @staticmethod
        def completion(**_kwargs: Any) -> object:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))],
                usage=SimpleNamespace(
                    prompt_tokens=2,
                    completion_tokens=1,
                    prompt_tokens_details=None,
                    completion_tokens_details=None,
                ),
                model="test-model",
                id="response-parse",
            )

    log = _log()
    provider = object.__new__(LiteLLMProvider)
    provider._litellm = _Gateway()
    provider.model = "test-model"
    provider.proxy_endpoint = None
    provider._declared_safe_endpoints = frozenset[str]()
    provider.provider_name = "openai"
    provider.reasoning_effort = None
    provider.applies_reasoning_effort = False
    provider.timeout_s = 17.0
    provider._request_ledger = RequestLedger(log)
    provider._request_owner_id = "agent_test"
    provider._request_attempt = 0
    provider._previous_request_id = None

    with pytest.raises(LLMStructuredOutputError, match="not valid JSON"):
        provider.complete_structured("question", schema=_Result)

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.outcome is ResponseOutcome.PARSE_FAILURE
    assert response.message is not None
    assert response.message.content == "not-json"
    assert response.source_event_seqs == (3,)
    assert response.input_tokens == 2
    assert response.output_tokens == 1


def test_wrapped_structured_parse_failure_is_terminal() -> None:
    """A structured-output parse failure is durable and independently replayable."""
    log = _log()
    provider = LedgerProvider(
        ScriptedProvider(["not-json"]), RequestLedger(log), owner_id="agent_test"
    )

    with pytest.raises(LLMStructuredOutputError):
        provider.complete_structured("question", schema=_Result)

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.outcome is ResponseOutcome.PARSE_FAILURE
    assert response.message is not None
    assert response.message.content == "not-json"
    assert response.source_event_seqs == (3,)
    assert response.input_tokens == 0
    assert response.output_tokens == 0


def test_wrapped_provider_cancellation_is_durable_and_reraised() -> None:
    """Cancellation records a terminal outcome while preserving the cancellation exception."""

    class _CancelledProvider(ScriptedProvider):
        def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
            raise asyncio.CancelledError

    log = _log()
    provider = LedgerProvider(
        _CancelledProvider(["never used"]), RequestLedger(log), owner_id="agent_test"
    )

    with pytest.raises(asyncio.CancelledError):
        provider.complete("question")

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.outcome is ResponseOutcome.CANCEL


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("stop"), SystemExit("exit")],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_wrapped_provider_base_exception_is_durable_and_reraised(
    failure: BaseException,
) -> None:
    """Base exceptions get best-effort safe evidence without being swallowed."""

    class _InterruptingProvider(ScriptedProvider):
        def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
            raise failure

    log = _log()
    provider = LedgerProvider(_InterruptingProvider([]), RequestLedger(log), owner_id="agent_test")

    with pytest.raises(type(failure)):
        provider.complete("question")

    response = replay_request_ledger(tuple(log.events())).responses[0]
    assert response.outcome is ResponseOutcome.UNKNOWN
    assert response.message is None


def test_wrapped_provider_unknown_base_exception_is_durable_and_reraised() -> None:
    """A custom BaseException from a wrapped provider gets a final unknown outcome."""

    class _OpaqueAbort(BaseException):
        pass

    class _AbortingProvider(ScriptedProvider):
        def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
            raise _OpaqueAbort("wrapped private detail")

    log = _log()
    provider = LedgerProvider(_AbortingProvider([]), RequestLedger(log), owner_id="agent_test")

    with pytest.raises(_OpaqueAbort):
        provider.complete("question")

    report = replay_request_ledger(tuple(log.events()))
    response = report.responses[0]
    assert response.outcome is ResponseOutcome.UNKNOWN
    assert response.message is None
    assert "wrapped private detail" not in log.events()[-1].model_dump_json()


def test_base_exception_is_preserved_when_terminal_recording_fails() -> None:
    """A terminal write failure never replaces the original provider BaseException."""

    class _OpaqueAbort(BaseException):
        pass

    class _AbortingProvider(ScriptedProvider):
        def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
            raise _OpaqueAbort("original provider detail")

    class _BrokenLedger(RequestLedger):
        def record_terminal(self, *args: Any, **kwargs: Any) -> Event:
            raise RuntimeError("ledger write detail")

    log = _log()
    provider = LedgerProvider(_AbortingProvider([]), _BrokenLedger(log), owner_id="agent_test")

    with pytest.raises(_OpaqueAbort) as caught:
        provider.complete("question")

    assert isinstance(caught.value.__cause__, RuntimeError)
    report = replay_request_ledger(tuple(log.events()))
    assert report.responses == ()
    assert len(report.in_flight) == 1
    assert "original provider detail" not in log.events()[-1].model_dump_json()
    assert "ledger write detail" not in log.events()[-1].model_dump_json()


def test_replay_distinguishes_in_flight_from_explicit_unknown() -> None:
    """Absence of a terminal is in-flight, while unknown is a final classified outcome."""
    pending_log = _log()
    pending_ledger = RequestLedger(pending_log)
    pending_lease = pending_ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    pending_ledger.record_response(
        pending_lease,
        LLMResponse(text="partial", provider="test", model="test-model"),
        complete=False,
    )

    pending = replay_request_ledger(tuple(pending_log.events()))
    assert pending.responses == ()
    assert pending.in_flight == (str(pending_lease.request.id),)

    unknown_log = _log()
    unknown_ledger = RequestLedger(unknown_log)
    unknown_lease = unknown_ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    unknown_ledger.record_terminal(
        unknown_lease, ResponseOutcome.UNKNOWN, error_code="provider_unknown"
    )

    unknown = replay_request_ledger(tuple(unknown_log.events()))
    assert unknown.responses[0].outcome is ResponseOutcome.UNKNOWN
    assert unknown.in_flight == ()


def test_replay_preserves_proven_partial_response_before_error() -> None:
    """A terminal provider error proves the chunks received before the failure."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    ledger.record_response(
        lease,
        LLMResponse(text="partial", provider="test", model="test-model"),
        complete=False,
    )
    ledger.record_terminal(
        lease,
        ResponseOutcome.PROVIDER_ERROR,
        chunk_count=1,
        error_code="provider_error",
    )

    report = replay_request_ledger(tuple(log.events()))
    response = report.responses[0]
    assert response.message is not None
    assert response.message.content == "partial"
    assert response.source_event_seqs == (3,)
    assert response.outcome is ResponseOutcome.PROVIDER_ERROR
    assert report.in_flight == ()


def test_replay_rejects_missing_chunk_sequence() -> None:
    """A terminal count proves a gap is invalid even when event arrival is hash-valid."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    ledger.record_response(
        lease,
        LLMResponse(text="only-one", provider="test", model="test-model"),
        source_sequence=1,
        complete=False,
    )
    ledger.record_terminal(lease, ResponseOutcome.SUCCESS, chunk_count=2)

    with pytest.raises(RequestReplayError, match="source sequence is incomplete"):
        replay_request_ledger(tuple(log.events()))


def test_replay_rejects_duplicate_chunk_sequence() -> None:
    """A duplicate source sequence cannot satisfy a terminal chunk count."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    ledger.record_response(
        lease,
        LLMResponse(text="A", provider="test", model="test-model"),
        complete=False,
    )
    ledger.record_response(
        lease,
        LLMResponse(text="B", provider="test", model="test-model"),
        complete=False,
    )
    ledger.record_terminal(lease, ResponseOutcome.SUCCESS, chunk_count=2)

    with pytest.raises(RequestReplayError, match="source sequence is incomplete"):
        replay_request_ledger(tuple(log.events()))


def test_replay_rejects_orphan_terminal_marker() -> None:
    """A validly hashed terminal marker for no request is still unusable evidence."""
    log = _log()
    orphan = ResponseChunk(
        response_id=new_id("response"),
        request_id=new_id("request"),
        source_sequence=0,
        terminal=ResponseTerminal(
            outcome=ResponseOutcome.UNKNOWN,
            chunk_count=0,
            error_code="provider_unknown",
        ),
    )
    log.append(
        EventType.MODEL_RESPONSE_CHUNK,
        Actor.system(),
        orphan.to_json_dict(),
        correlation_id=str(orphan.request_id),
        causation_id=str(new_id("request")),
    )

    with pytest.raises(RequestReplayError, match="references no request"):
        replay_request_ledger(tuple(log.events()))


def test_provider_replay_state_requirement_fails_closed() -> None:
    """An adapter that declares required opaque state cannot dispatch without an owner."""

    class _RequiresOpaque(ScriptedProvider):
        opaque_replay_state_required = True

    with pytest.raises(RequestLedgerError, match="opaque replay state"):
        LedgerProvider(_RequiresOpaque(["answer"]), RequestLedger(_log()), owner_id="agent_test")


def test_replay_refuses_legacy_response_payload_version() -> None:
    """The response reader does not guess the pre-terminal payload shape."""
    log = _log()
    ledger = RequestLedger(log)
    lease = ledger.record_gateway_request(
        "test-model", [{"role": "user", "content": "question"}], {}, owner_id="agent_test"
    )
    current = ResponseChunk(
        response_id=new_id("response"),
        request_id=lease.request.id,
        source_sequence=0,
        message=ProviderMessage(role="assistant", content="answer"),
        terminal=ResponseTerminal(outcome=ResponseOutcome.SUCCESS, chunk_count=1),
    )
    log.append(
        EventType.MODEL_RESPONSE_CHUNK,
        Actor.system(),
        {**current.to_json_dict(), "ledger_version": "0.1"},
        correlation_id=str(lease.request.id),
        causation_id=str(lease.event.event_id),
    )

    with pytest.raises(RequestReplayError, match="invalid provider response"):
        replay_request_ledger(tuple(log.events()))
