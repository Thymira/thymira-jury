"""Deriving what a model currently sees from the immutable event log.

The *log* is the complete, hash-chained record of a run — what MIRA audits. The *surface* is the
subset a model is shown. Compaction never removes an event: it appends a ``context.compacted``
event naming the seqs it shadowed, so the chain stays whole while the model sees less.

Everything here folds the log; nothing is stored. An event is immutable once chained, so writing
a shadowing mark back onto the record would break the very chain it exists to protect — which is
why :class:`~thymira.schemas.SurfaceState` has a ``SHADOWED`` member and
:class:`~thymira.schemas.EventSurface` deliberately does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.schemas import EventSurface, EventType, SurfaceState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event

SHADOWED_SEQS_KEY = "shadowed_seqs"
"""Payload key of a ``context.compacted`` event listing the seqs that compaction shadowed."""


def shadowed_seqs(event: Event) -> list[int]:
    """The seqs one ``context.compacted`` event shadows.

    Payloads are plain JSON, so anything that is not an integer sequence number is ignored
    rather than trusted: a malformed payload must not silently hide evidence from the fold.
    """
    raw = event.payload.get(SHADOWED_SEQS_KEY, ())
    if not isinstance(raw, list | tuple):
        return []
    return [seq for seq in raw if isinstance(seq, int) and not isinstance(seq, bool)]


def derive_surface(events: Sequence[Event]) -> dict[int, SurfaceState]:
    """Classify every event by ``seq`` according to the model's current view.

    A ``LOG_ONLY`` event never reaches a model and stays ``LOG_ONLY``. A ``MODEL_VISIBLE`` event
    starts ``CURRENT`` and becomes ``SHADOWED`` once a later ``context.compacted`` event names
    its ``seq``. A compaction may only shadow events that precede it; a payload naming a later
    seq is ignored, so a forged or reordered payload cannot retroactively hide the future.

    Args:
        events: the run's events in log order.

    Returns:
        One :class:`~thymira.schemas.SurfaceState` per event ``seq``.
    """
    state: dict[int, SurfaceState] = {
        event.seq: (
            SurfaceState.CURRENT
            if event.surface is EventSurface.MODEL_VISIBLE
            else SurfaceState.LOG_ONLY
        )
        for event in events
    }
    for event in events:
        if event.type is not EventType.CONTEXT_COMPACTED:
            continue
        for seq in shadowed_seqs(event):
            if seq < event.seq and state.get(seq) is SurfaceState.CURRENT:
                state[seq] = SurfaceState.SHADOWED
    return state


def current_surface(events: Sequence[Event]) -> list[Event]:
    """The events a model is shown right now, in log order.

    This is what a prompt builder may read. Everything shadowed or log-only stays in the chain
    for the audit and is simply absent here.
    """
    state = derive_surface(events)
    return [event for event in events if state.get(event.seq) is SurfaceState.CURRENT]
