"""Acceptance tests for the CLI Run event history and streaming view.

Real boundary: the Typer CLI and ApiClient event parsing. Faked boundary: the Thymira API,
through deterministic ``httpx.MockTransport`` responses and streams.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import (
    ApiClient,
    ApiDomainError,
    ApiResponseError,
    EnqueueReceiptView,
    EventView,
    is_terminal_event,
    terminal_exit_code,
)
from thymira.cli.shutdown import ShutdownRequestedError, bounded_signal_shutdown

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"


class _InterruptedStream(httpx.SyncByteStream):
    """Deliver one SSE frame and then simulate an interrupted connection."""

    def __init__(self, content: bytes) -> None:
        self._content = content

    def __iter__(self) -> Iterator[bytes]:
        """Yield the first frame before the simulated transport error."""
        yield self._content
        raise httpx.ReadError("stream interrupted")

    def close(self) -> None:
        """Satisfy the synchronous byte-stream protocol."""


def _event_payload(seq: int, event_type: str, actor: str) -> dict[str, object]:
    return {
        "event_id": f"event_{seq:032x}",
        "run_id": RUN_ID,
        "seq": seq,
        "type": event_type,
        "schema_version": "0.3",
        "ts": "2026-08-22T10:14:05Z",
        "actor": {"kind": "system", "id": actor, "role": None, "authenticated": True},
        "surface": "log_only",
        "producer": "test",
        "producer_version": "test-v1",
        "payload": {},
        "prev_hash": "0" * 64,
        "hash": "1" * 64,
    }


def _sse_frame(payload: dict[str, object]) -> bytes:
    return (
        f"id: {payload['seq']}\n"
        f"event: {payload['type']}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode()


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_events_reads_all_buffered_pages() -> None:
    """The events command follows JSON cursors until it has the complete history."""
    first = _event_payload(0, "run.started", "system")
    second = _event_payload(1, "agent.started", "data")

    def handler(request: httpx.Request) -> httpx.Response:
        after_seq = request.url.params["after_seq"]
        if after_seq == "-1":
            return httpx.Response(
                200,
                json={"run_id": RUN_ID, "items": [first], "next_after_seq": 0, "has_more": True},
                request=request,
            )
        assert after_seq == "0"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [second], "next_after_seq": None, "has_more": False},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Events for" in result.output
    assert "run.started" in result.output
    assert "agent.started" in result.output
    assert "data" in result.output


def test_events_honours_the_since_cursor() -> None:
    """The since option maps to the JSON history cursor."""
    event = _event_payload(5, "tool.completed", "run_python")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["after_seq"] == "4"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [event], "next_after_seq": None, "has_more": False},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID, "--since", "4"], handler)

    assert result.exit_code == 0
    assert "tool.completed" in result.output
    assert "run_python" in result.output


def test_events_history_rejects_a_sequence_gap() -> None:
    """The plain CLI history path fails instead of rendering sequence two after sequence zero."""
    later = _event_payload(2, "agent.completed", "system")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["after_seq"] == "-1"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [later], "next_after_seq": None, "has_more": False},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID], handler)

    assert result.exit_code == 1
    assert "unrepaired sequence gap" in result.output
    assert "agent.completed" not in result.output


def test_events_history_rejects_a_nonfinal_page_without_cursor_progress() -> None:
    """A duplicate-only page cannot keep a history reader looping forever."""
    duplicate = _event_payload(0, "run.started", "system")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [duplicate], "next_after_seq": 0, "has_more": True},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID], handler)

    assert result.exit_code == 1
    assert "made no cursor progress" in result.output
    assert [request.url.params["after_seq"] for request in requests] == ["-1", "0"]


def test_events_history_rejects_a_duplicate_only_final_page_after_progress() -> None:
    """A final duplicate page cannot hide a missing sequence after an earlier nonfinal page."""
    duplicate = _event_payload(0, "run.started", "system")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "run_id": RUN_ID,
                    "items": [duplicate],
                    "next_after_seq": 0,
                    "has_more": True,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "run_id": RUN_ID,
                "items": [duplicate],
                "next_after_seq": None,
                "has_more": False,
            },
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID], handler)

    assert result.exit_code == 1
    assert "no cursor progress" in result.output
    assert [request.url.params["after_seq"] for request in requests] == ["-1", "0"]


def test_events_follow_renders_server_sent_events() -> None:
    """The follow option requests and renders the Server-Sent Event stream."""
    event = _event_payload(0, "run.completed", "system")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["follow"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            content=_sse_frame(event),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID, "--follow"], handler)

    assert result.exit_code == 0
    assert "run.completed" in result.output
    assert "system" in result.output


def test_events_follow_reconnects_after_an_interrupted_stream() -> None:
    """A follow stream reconnects from the last rendered SSE sequence identifier."""
    first = _event_payload(0, "run.started", "system")
    second = _event_payload(1, "run.completed", "system")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=_InterruptedStream(_sse_frame(first)),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        assert request.headers["last-event-id"] == "0"
        return httpx.Response(
            200,
            content=_sse_frame(first) + _sse_frame(second),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    result = _invoke_with_api(["events", RUN_ID, "--follow"], handler)

    assert result.exit_code == 0
    assert result.output.count("run.started") == 1
    assert result.output.count("run.completed") == 1
    assert len(requests) == 2


def test_receipt_wait_does_not_treat_a_generic_run_terminal_as_turn_completion() -> None:
    """A receipt observer must have the correlated lifecycle turn-end proof."""
    completed = _event_payload(0, "run.completed", "system")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_frame(completed),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiResponseError, match="receipt's turn ended"):
            client.wait_for_enqueue(
                EnqueueReceiptView(
                    input_id="input_4f28c60d5d5a4fe5b14c5e68931f1234",
                    run_id=RUN_ID,
                    accepted=True,
                    duplicate=False,
                    durable_at="2026-08-22T10:14:05Z",
                )
            )
    finally:
        client.close()


def test_events_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before a history request is attempted."""
    result = runner.invoke(app, ["events", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def _turn_ended_payload(
    *,
    run_id: str,
    input_ids: list[str] | None = None,
    work_ids: list[str] | None = None,
    end_reason: str = "completed",
) -> dict[str, object]:
    """Build the strict lifecycle turn-end payload used by transport tests."""
    return {
        "turn_id": "task_4f28c60d5d5a4fe5b14c5e68931f1234",
        "run_id": run_id,
        "claimed_input_ids": [] if input_ids is None else input_ids,
        "work_ids": [] if work_ids is None else work_ids,
        "step_count": 1,
        "end_reason": end_reason,
        "cause": None,
    }


def test_receipt_follow_rejects_a_sequence_gap_during_history_repair() -> None:
    """Receipt observation cannot settle after a paged history skips an earlier sequence."""
    later = _event_payload(2, "agent.completed", "system")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_sse_frame(later),
                headers={
                    "content-type": "text/event-stream",
                    "x-thymira-redacted-stream": "event-projection-v1",
                },
                request=request,
            )
        assert request.url.params.get("follow") is None
        assert request.url.params["after_seq"] == "-1"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [later], "next_after_seq": None, "has_more": False},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiResponseError, match="unrepaired sequence gap"):
            client.wait_for_enqueue(
                EnqueueReceiptView(
                    input_id="input_4f28c60d5d5a4fe5b14c5e68931f1234",
                    run_id=RUN_ID,
                    accepted=True,
                    duplicate=False,
                    durable_at="2026-08-22T10:14:05Z",
                )
            )
    finally:
        client.close()

    assert len(requests) == 2


def test_follow_connects_before_page_and_replays_the_committed_history_once() -> None:
    """A live connection is established first, then its page closes the initial race window."""
    first = _event_payload(0, "run.started", "system")
    second = _event_payload(1, "agent.completed", "data")
    ended = _event_payload(2, "turn.ended", "system")
    ended["payload"] = _turn_ended_payload(run_id=RUN_ID)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_sse_frame(ended),
                headers={
                    "content-type": "text/event-stream",
                    "x-thymira-redacted-stream": "event-projection-v1",
                },
                request=request,
            )
        assert request.url.params["after_seq"] == "-1"
        return httpx.Response(
            200,
            json={
                "run_id": RUN_ID,
                "items": [first, second, ended],
                "next_after_seq": None,
                "has_more": False,
            },
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        received = list(client.stream_events(RUN_ID, follow=True))
    finally:
        client.close()

    assert [event.seq for event in received] == [0, 1, 2]
    assert len(requests) == 2
    assert requests[0].url.params["follow"] == "true"


def test_follow_fails_closed_when_json_history_returns_sse() -> None:
    """A JSON history request cannot treat an SSE response as an empty repair page."""
    streamed = _event_payload(1, "agent.started", "system")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_sse_frame(streamed),
                headers={
                    "content-type": "text/event-stream",
                    "x-thymira-redacted-stream": "event-projection-v1",
                },
                request=request,
            )
        assert request.url.params.get("follow") is None
        assert request.url.params["after_seq"] == "-1"
        return httpx.Response(
            200,
            content=_sse_frame(streamed),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiResponseError, match="invalid JSON response"):
            list(client.stream_events(RUN_ID, follow=True))
    finally:
        client.close()

    assert len(requests) == 2


def test_a_fresh_client_replays_from_the_durable_cursor() -> None:
    """A second client resumes from the API cursor without inheriting the first client's state."""
    first = _event_payload(0, "run.started", "system")
    ended = _event_payload(1, "turn.ended", "system")
    ended["payload"] = _turn_ended_payload(run_id=RUN_ID)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("follow") == "true"
        assert request.url.params["since"] == "0"
        return httpx.Response(
            200,
            content=_sse_frame(ended),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    first_client = ApiClient(
        "http://api.test",
        token=TEST_TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "run_id": RUN_ID,
                    "items": [first],
                    "next_after_seq": None,
                    "has_more": False,
                },
                request=request,
            )
        ),
    )
    try:
        assert [event.seq for event in first_client.stream_events(RUN_ID)] == [0]
    finally:
        first_client.close()

    second_client = ApiClient(
        "http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler)
    )
    try:
        replayed = list(
            second_client.stream_events(
                RUN_ID,
                since=0,
                follow=True,
                require_turn_end=True,
            )
        )
    finally:
        second_client.close()

    assert [event.seq for event in replayed] == [1]


def test_follow_repairs_a_gap_and_deduplicates_overlapping_stream_history() -> None:
    """A missing cursor range is paged and overlapping SSE observations are emitted once."""
    first = _event_payload(0, "run.started", "system")
    second = _event_payload(1, "agent.started", "data")
    streamed = _event_payload(2, "agent.completed", "stream")
    paged = _event_payload(2, "agent.completed", "page")
    ended = _event_payload(3, "turn.ended", "system")
    ended["payload"] = _turn_ended_payload(run_id=RUN_ID)
    requests: list[httpx.Request] = []

    def page(request: httpx.Request, items: list[dict[str, object]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": items, "next_after_seq": None, "has_more": False},
            request=request,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_sse_frame(streamed) + _sse_frame(ended),
                headers={
                    "content-type": "text/event-stream",
                    "x-thymira-redacted-stream": "event-projection-v1",
                },
                request=request,
            )
        if len(requests) == 2:
            assert request.url.params["after_seq"] == "-1"
            return page(request, [])
        assert request.url.params["after_seq"] == "-1"
        return page(request, [first, second, paged])

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        received = list(client.stream_events(RUN_ID, follow=True))
    finally:
        client.close()

    assert [event.seq for event in received] == [0, 1, 2, 3]
    assert received[2].actor == "page"
    assert len(requests) == 3


def test_turn_end_requires_the_canonical_lifecycle_payload() -> None:
    """A same-named event without lifecycle proof cannot close a headless observation."""
    event = EventView(seq=0, type="turn.ended", actor="system")
    assert not is_terminal_event(event)


def test_receipt_wait_skips_an_unrelated_turn_end_until_its_input_is_claimed() -> None:
    """A different settled turn cannot terminate observation for this receipt."""
    unrelated = _event_payload(0, "turn.ended", "system")
    unrelated["payload"] = _turn_ended_payload(run_id=RUN_ID)
    matching = _event_payload(1, "turn.ended", "system")
    matching["payload"] = _turn_ended_payload(
        run_id=RUN_ID,
        input_ids=["input_4f28c60d5d5a4fe5b14c5e68931f1234"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_frame(unrelated) + _sse_frame(matching),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        ended = client.wait_for_enqueue(
            EnqueueReceiptView(
                input_id="input_4f28c60d5d5a4fe5b14c5e68931f1234",
                run_id=RUN_ID,
                accepted=True,
                duplicate=False,
                durable_at="2026-08-22T10:14:05Z",
            )
        )
    finally:
        client.close()

    assert ended.seq == 1


def test_terminal_exit_code_comes_from_the_durable_turn_reason() -> None:
    """Headless callers receive stable process statuses from the lifecycle reason."""
    for reason, expected in (("completed", 0), ("rejected", 2), ("cancelled", 130), ("unknown", 1)):
        event = EventView(
            seq=0,
            type="turn.ended",
            actor="system",
            payload=_turn_ended_payload(run_id=RUN_ID, end_reason=reason),
        )
        assert terminal_exit_code(event) == expected


def test_signal_shutdown_restores_handlers_after_bounded_observation() -> None:
    """Signal interruption raises promptly and leaves the caller's handlers installed."""
    import signal

    before = signal.getsignal(signal.SIGINT)

    def trigger() -> None:
        handler = signal.getsignal(signal.SIGINT)
        if not callable(handler):
            raise TypeError("the test handler was not installed")
        cast("Callable[[int, FrameType | None], None]", handler)(int(signal.SIGINT), None)

    with pytest.raises(ShutdownRequestedError, match="signal"), bounded_signal_shutdown():
        trigger()
    assert signal.getsignal(signal.SIGINT) is before


def test_structured_domain_problem_stays_distinct_from_transport_failure() -> None:
    """A stable problem code is surfaced as a domain outcome rather than generic HTTP failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "code": "run_not_resumable",
                "message": "The Run cannot be resumed.",
                "details": {},
            },
            headers={"content-type": "application/problem+json"},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiDomainError) as raised:
            client.resume_run(RUN_ID)
    finally:
        client.close()
    assert raised.value.code == "run_not_resumable"
