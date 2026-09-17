"""Event subscription seams for the Thymira HTTP boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from thymira.schemas import EventType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from thymira.schemas import Event, Id
    from thymira.state import EventStore

_DEFAULT_POLL_INTERVAL = 0.25
Sleeper = Callable[[float], Awaitable[None]]


def _is_terminal(event: Event) -> bool:
    """Return whether an event records the terminal outcome of a Run."""
    if event.type in {EventType.RUN_COMPLETED, EventType.RUN_FAILED}:
        return True
    if event.type is EventType.RUN_TRANSITIONED and event.payload.get("outcome") is not None:
        return True
    return event.type.value == "turn.ended" and _has_turn_end_reason(event.payload)


def _has_turn_end_reason(payload: dict[str, object]) -> bool:
    """Recognise only the closed fields needed to stop a transport after a settled turn."""
    turn_id = payload.get("turn_id")
    run_id = payload.get("run_id")
    claimed_input_ids = payload.get("claimed_input_ids")
    work_ids = payload.get("work_ids")
    step_count = payload.get("step_count")
    reason = payload.get("end_reason")
    return (
        isinstance(turn_id, str)
        and bool(turn_id)
        and isinstance(run_id, str)
        and bool(run_id)
        and isinstance(claimed_input_ids, list)
        and all(isinstance(value, str) and value for value in claimed_input_ids)
        and isinstance(work_ids, list)
        and all(isinstance(value, str) and value for value in work_ids)
        and isinstance(step_count, int)
        and not isinstance(step_count, bool)
        and step_count >= 0
        and reason in {"completed", "rejected", "failed", "cancelled", "paused", "unknown"}
        and "cause" in payload
        and _has_failure_cause(payload["cause"])
    )


def _has_failure_cause(value: object) -> bool:
    """Validate the closed failure-cause wire shape before treating a turn as settled."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    required_text = ("code", "phase", "exception_type", "message")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required_text):
        return False
    effects = value.get("effects_may_have_occurred")
    if not isinstance(effects, bool):
        return False
    caused_by_id = value.get("caused_by_id")
    return caused_by_id is None or (isinstance(caused_by_id, str) and bool(caused_by_id))


async def _sleep(delay: float) -> None:
    """Yield to the event loop before the next polling attempt."""
    await asyncio.sleep(delay)


class EventSubscription(Protocol):
    """Stream new events for one Run from a caller-selected sequence."""

    def subscribe(self, run_id: Id, *, since: int = -1) -> AsyncIterator[Event]:
        """Return an asynchronous iterator of events after ``since``."""
        ...


@dataclass(frozen=True, slots=True)
class PollingEventSubscription:
    """Tail an EventStore by polling it until a Run reaches a terminal event."""

    event_store: EventStore
    poll_interval: float = _DEFAULT_POLL_INTERVAL
    sleeper: Sleeper = _sleep

    def __post_init__(self) -> None:
        """Validate the polling interval used by the asynchronous tail."""
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

    async def subscribe(self, run_id: Id, *, since: int = -1) -> AsyncIterator[Event]:
        """Yield every new event and stop after a terminal Run outcome."""
        if since < -1:
            raise ValueError("since must be greater than or equal to -1")

        next_seq = since + 1
        while True:
            # EventStore implementations are append-only, but a transport or a backend may return
            # a stale, reordered or partially observed snapshot.  Never expose a later sequence
            # while an earlier one is missing: the next poll is the repair opportunity.  Sorting
            # and collapsing by sequence also makes a replay deterministic when a push backend
            # redelivers one event.
            by_sequence = {
                event.seq: event
                for event in sorted(self.event_store.read(run_id), key=lambda item: item.seq)
            }
            gap = False
            for event in by_sequence.values():
                if event.seq < next_seq:
                    continue
                if event.seq > next_seq:
                    gap = True
                    break
                yield event
                next_seq = event.seq + 1
                if _is_terminal(event):
                    return
            if gap:
                await self.sleeper(self.poll_interval)
                continue
            await self.sleeper(self.poll_interval)


__all__ = ["EventSubscription", "PollingEventSubscription"]
