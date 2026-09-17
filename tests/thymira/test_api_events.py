"""Integration tests for the Run event history and SSE API boundary.

The FastAPI application, local persistence and event log are real. The streaming subscription
is replaced with a finite test subscription when testing HTTP framing; the polling implementation
is exercised with a deterministic in-memory store and sleeper.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import PollingEventSubscription, build_default_deps, create_app
from thymira.api.export_redaction import ExportRedactionMiddleware
from thymira.events import EventLog, InMemoryEventLog
from thymira.schemas import Actor, Event, EventType, Id, new_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

    from thymira.api.deps import RuntimeDeps

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record Run submissions without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one dispatch request."""

    def resume(self, run_id: Id) -> None:
        """Record one resume request."""


class _FiniteSubscription:
    """Yield a fixed event set so an HTTP stream can terminate deterministically."""

    def __init__(self, events: tuple[Event, ...]) -> None:
        self._events = events

    async def subscribe(self, run_id: Id, *, since: int = -1) -> AsyncIterator[Event]:
        """Yield events for ``run_id`` after the requested cursor."""
        for event in self._events:
            if event.run_id == run_id and event.seq > since:
                yield event


class _SnapshotStore:
    """Expose successive event snapshots to a polling subscription."""

    def __init__(self, snapshots: tuple[tuple[Event, ...], ...]) -> None:
        self._snapshots = snapshots
        self._index = 0

    def read(self, run_id: Id) -> list[Event]:
        """Return the current snapshot for the requested Run."""
        del run_id
        return list(self._snapshots[self._index])

    def open(self, run_id: Id) -> EventLog:
        """Declare the unused EventStore operation for structural typing."""
        del run_id
        raise NotImplementedError

    def advance(self) -> None:
        """Expose the next snapshot to the following poll."""
        self._index = min(self._index + 1, len(self._snapshots) - 1)


class _AdvanceOnSleep:
    """Advance a deterministic store instead of sleeping in a test."""

    def __init__(self, store: _SnapshotStore) -> None:
        self._store = store

    async def __call__(self, _delay: float) -> None:
        """Advance the event snapshot when polling yields control."""
        self._store.advance()


def _deps(tmp_path: Path) -> RuntimeDeps:
    """Build local API dependencies for one configured temporary project."""
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    return build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=TEST_CREDENTIAL,
    )


def _run_with_events(deps: RuntimeDeps, count: int = 2) -> tuple[Id, tuple[Event, ...]]:
    """Create one Run and append deterministic child events to its log."""
    if deps.project_resolution is None:
        raise AssertionError("test dependencies must resolve a project")
    session = deps.session_service.resolve(
        project_id=deps.project_resolution.project_id,
        client="test",
    )
    run = deps.run_service.create_run(
        session.id,
        "Analyze the dataset",
        actor=Actor.system(),
        workspace=deps.project_resolution.workspace,
    )
    log = deps.event_store.open(run.id)
    for _ in range(count):
        log.append(EventType.AGENT_STARTED, Actor.system(), {"source": "test"})
    events = tuple(deps.event_store.read(run.id))
    return run.id, events


def test_get_events_returns_ordered_pages_after_cursor(tmp_path: Path) -> None:
    """The JSON event endpoint returns bounded, ordered pages after ``after_seq``."""
    deps = _deps(tmp_path)
    run_id, events = _run_with_events(deps, count=2)
    client = authenticated_client(create_app(deps))

    response = client.get(f"/runs/{run_id}/events", params={"after_seq": 0, "limit": 1})

    expected = events[1].to_json_dict()
    expected.pop("hash", None)
    expected.pop("prev_hash", None)
    expected["projection"] = "redacted"
    expected["source_hash"] = events[1].hash
    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "items": [expected],
        "next_after_seq": events[1].seq,
        "has_more": True,
    }


def test_event_api_redacts_the_copy_but_canonical_reader_keeps_exact_pii(tmp_path: Path) -> None:
    """The API response is an export; the injected event store remains the evidence oracle."""
    deps = _deps(tmp_path)
    run_id, _events = _run_with_events(deps, count=0)
    log = deps.event_store.open(run_id)
    canonical = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "send the result to alice@example.com", "metric": 0.8123456789012345},
    )
    client = authenticated_client(create_app(deps))

    response = client.get(f"/runs/{run_id}/events")

    assert response.status_code == 200
    exported = next(item for item in response.json()["items"] if item["seq"] == canonical.seq)
    assert exported["payload"]["text"] == "send the result to [REDACTED:EMAIL]"
    assert exported["payload"]["metric"] == canonical.payload["metric"]
    assert exported["projection"] == "redacted"
    assert exported["source_hash"] == canonical.hash
    assert "hash" not in exported
    assert "prev_hash" not in exported
    assert deps.event_store.read(run_id)[-1].payload["text"] == canonical.payload["text"]


def test_get_events_rejects_unknown_run_and_invalid_cursor(tmp_path: Path) -> None:
    """The event endpoint exposes stable errors for scope and cursor failures."""
    deps = _deps(tmp_path)
    run_id, _events = _run_with_events(deps)
    client = authenticated_client(create_app(deps))

    unknown = client.get(f"/runs/{new_id('run')}/events")
    invalid = client.get(f"/runs/{run_id}/events", params={"limit": 0})

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "run_not_found"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "validation_error"


def test_unknown_http_media_type_fails_closed_before_body_forwarding() -> None:
    """A response cannot bypass the export boundary by announcing an unsupported media type."""

    async def unsafe_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"contact alice@example.com",
                "more_body": False,
            }
        )

    response = TestClient(ExportRedactionMiddleware(unsafe_app)).get("/")

    assert response.status_code == 500
    assert response.json() == {
        "code": "export_redaction_failed",
        "message": "The response could not be safely redacted.",
        "details": {},
    }
    assert "alice@example.com" not in response.content.decode()


def test_unguarded_sse_fails_closed_before_body_forwarding() -> None:
    """Only the events route's explicit projection sentinel may pass through as an SSE stream."""

    async def unsafe_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: alice@example.com\n\n",
                "more_body": False,
            }
        )

    response = TestClient(ExportRedactionMiddleware(unsafe_app)).get("/")

    assert response.status_code == 500
    assert "alice@example.com" not in response.content.decode()


def test_no_content_response_with_body_fails_closed_before_body_forwarding() -> None:
    """A 204 response is passed through only when it is actually empty."""

    async def unsafe_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"contact alice@example.com",
                "more_body": False,
            }
        )

    response = TestClient(ExportRedactionMiddleware(unsafe_app)).get("/")

    assert response.status_code == 500
    assert "alice@example.com" not in response.content.decode()


def test_sse_stream_uses_sequence_ids_and_reconnect_cursor(tmp_path: Path) -> None:
    """SSE frames carry event types and resume after the Last-Event-ID cursor."""
    deps = _deps(tmp_path)
    run_id, events = _run_with_events(deps, count=2)
    stream_deps = replace(deps, event_subscription=_FiniteSubscription(events))
    client = authenticated_client(create_app(stream_deps))

    response = client.get(
        f"/runs/{run_id}/events",
        params={"follow": "1"},
    )
    accept_only = client.get(
        f"/runs/{run_id}/events",
        headers={"Accept": "text/event-stream"},
    )
    resumed = client.get(
        f"/runs/{run_id}/events",
        params={"follow": "1"},
        headers={"Accept": "text/event-stream", "Last-Event-ID": str(events[1].seq - 1)},
    )
    since_resumed = client.get(
        f"/runs/{run_id}/events",
        params={"follow": "1", "since": events[1].seq - 1},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {events[0].seq}\n" in response.text
    assert f"event: {events[0].type.value}\n" in response.text
    assert f'"seq":{events[0].seq}' in response.text
    assert accept_only.status_code == 200
    assert f"id: {events[1].seq}\n" in resumed.text
    assert f"id: {events[0].seq}\n" not in resumed.text
    assert f"id: {events[1].seq}\n" in since_resumed.text
    assert f"id: {events[0].seq}\n" not in since_resumed.text


def test_sse_rejects_non_integer_last_event_id(tmp_path: Path) -> None:
    """An invalid reconnect cursor is rejected before a stream is opened."""
    deps = _deps(tmp_path)
    run_id, _events = _run_with_events(deps)
    client = authenticated_client(create_app(deps))

    response = client.get(
        f"/runs/{run_id}/events",
        params={"follow": "1"},
        headers={"Last-Event-ID": "not-a-sequence"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_last_event_id"


def test_polling_subscription_yields_event_added_after_cursor() -> None:
    """Polling observes an event appended after the initial snapshot without real sleeping."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    first = log.append(EventType.RUN_STARTED, Actor.system(), {})
    second = log.append(EventType.AGENT_STARTED, Actor.system(), {})
    store = _SnapshotStore(((first,), (first, second)))
    subscription = PollingEventSubscription(
        store,
        poll_interval=1.0,
        sleeper=_AdvanceOnSleep(store),
    )

    async def _receive() -> Event:
        async for received in subscription.subscribe(run_id, since=first.seq):
            return received
        raise AssertionError("subscription ended before the appended event")

    received = asyncio.run(_receive())

    assert received == second


def test_polling_subscription_repairs_a_snapshot_gap_before_yielding_later_events() -> None:
    """A partial read never exposes sequence two before sequence one is durable."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    first = log.append(EventType.RUN_STARTED, Actor.system(), {})
    second = log.append(EventType.AGENT_STARTED, Actor.system(), {})
    third = log.append(EventType.AGENT_COMPLETED, Actor.system(), {})
    store = _SnapshotStore(((first, third), (first, second, third)))
    subscription = PollingEventSubscription(
        store,
        poll_interval=1.0,
        sleeper=_AdvanceOnSleep(store),
    )

    async def _receive() -> list[Event]:
        received: list[Event] = []
        async for event in subscription.subscribe(run_id, since=first.seq):
            received.append(event)
            if len(received) == 2:
                return received
        raise AssertionError("subscription ended before the repaired events arrived")

    received = asyncio.run(_receive())

    assert received == [second, third]
