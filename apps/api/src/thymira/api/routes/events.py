"""Run event history and Server-Sent Events routes for the MVP API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.routes._scope import _scoped_run, _validate_run_id
from thymira.api.schemas import EventPage, EventProjection
from thymira.events import canonical_json, redact_export
from thymira.schemas import Id  # noqa: TC001  # FastAPI inspects this route annotation at runtime.

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from thymira.api.subscription import EventSubscription
    from thymira.schemas import Event

router = APIRouter(prefix="/runs", tags=["events"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_AFTER_SEQ = Query(default=-1, ge=-1)
_LIMIT = Query(default=100, ge=1, le=1000)
_SINCE = Query(default=None, ge=-1)
_FOLLOW = Query(default=False)


def _stream_cursor(
    after_seq: int,
    since: int | None,
    last_event_id: str | None,
) -> int:
    """Resolve the last delivered sequence used to start an SSE stream."""
    if last_event_id not in (None, ""):
        try:
            cursor = int(last_event_id)
        except ValueError as exc:
            raise problem(
                422,
                "invalid_last_event_id",
                "Last-Event-ID must be an integer.",
            ) from exc
        if cursor < -1:
            raise problem(
                422,
                "invalid_last_event_id",
                "Last-Event-ID must be greater than -1.",
            )
        return cursor
    return after_seq if since is None else since


def _event_projection(event: Event) -> EventProjection:
    """Build the redacted API view while retaining the canonical hash as source metadata."""
    payload = redact_export(event.to_json_dict())
    if not isinstance(payload, dict):  # pragma: no cover - Event.to_json_dict is a mapping
        raise TypeError("event serialization did not produce a mapping")
    payload.pop("hash", None)
    payload.pop("prev_hash", None)
    payload["projection"] = "redacted"
    payload["source_hash"] = event.hash
    return EventProjection.model_validate(payload)


def _sse_frame(event: Event) -> str:
    """Encode one contract Event as one SSE message."""
    payload = canonical_json(_event_projection(event).model_dump(mode="json"))
    return f"id: {event.seq}\nevent: {event.type.value}\ndata: {payload}\n\n"


def _ordered_unique(events: list[Event]) -> tuple[Event, ...]:
    """Return one deterministic observation for every sequence in a history snapshot."""
    by_sequence = {event.seq: event for event in events}
    return tuple(by_sequence[seq] for seq in sorted(by_sequence))


async def _stream_events(
    subscription: EventSubscription,
    run_id: Id,
    since: int,
) -> AsyncIterator[str]:
    """Adapt an injected EventSubscription to the text format expected by SSE clients."""
    async for event in subscription.subscribe(run_id, since=since):
        yield _sse_frame(event)


@router.get("/{run_id}/events", response_model=None, dependencies=[_REQUIRE_READ])
def get_events(
    run_id: Id,
    *,
    after_seq: int = _AFTER_SEQ,
    limit: int = _LIMIT,
    follow: bool = _FOLLOW,
    since: int | None = _SINCE,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    accept: str | None = Header(default=None),
    deps: RuntimeDeps = _DEPS,
) -> EventPage | StreamingResponse:
    """Read a Run's events as JSON or follow new events using SSE."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    wants_stream = follow or (accept is not None and "text/event-stream" in accept.casefold())
    if wants_stream:
        cursor = _stream_cursor(after_seq, since, last_event_id)
        return StreamingResponse(
            _stream_events(deps.event_subscription, run_id, cursor),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Thymira-Redacted-Stream": "event-projection-v1",
            },
        )

    events = [event for event in deps.event_store.read(run_id) if event.seq > after_seq]
    selected = _ordered_unique(events)[: limit + 1]
    has_more = len(selected) > limit
    items = tuple(_event_projection(event) for event in selected[:limit])
    return EventPage(
        run_id=run_id,
        items=items,
        next_after_seq=items[-1].seq if has_more else None,
        has_more=has_more,
    )


__all__ = ["router"]
