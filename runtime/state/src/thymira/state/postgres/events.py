"""PostgreSQL implementation of ``EventStore`` and its hash-chained ``EventLog``.

Every append reads and verifies the complete chain for the run *inside the caller's transaction*,
builds the next event with the public :mod:`thymira.events` primitives (source credential scrubbing,
then :func:`~thymira.events.hash_event`), and inserts one row. Documents are stored as canonical
JSON text, so a reloaded chain is byte-identical and :func:`~thymira.events.verify_events` still
passes. PII remains intact in the canonical evidence; known credential values are scrubbed before
the event is constructed. A transaction-scoped advisory lock keyed on the run serialises concurrent
appenders so two of them never claim the same ``seq`` or validate a prefix that another append can
change.

The event envelope constants below mirror the defaults in :mod:`thymira.events`; keep them in
step with that package's producer and schema version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from thymira.events import (
    GENESIS_HASH,
    canonical_json,
    deserialize_event,
    hash_event,
    scrub_credentials_value,
    validate_event_type,
    verify_events,
)
from thymira.schemas import (
    EVENT_LOG_FORMAT_VERSION,
    Actor,
    Event,
    EventSurface,
    EventType,
    new_id,
    utc_now,
)
from thymira.state.postgres import tables
from thymira.state.postgres._serde import load_document

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from thymira.events import VerificationResult
    from thymira.schemas import Id

_PRODUCER = "thymira.events"
_PRODUCER_VERSION = "0.3"


def build_event(
    run_id: str,
    seq: int,
    prev_hash: str,
    type: EventType,  # noqa: A002  # `type` is the contract field name
    actor: Actor,
    payload: dict[str, Any] | None,
    *,
    subject_id: str | None,
    producer: str,
    producer_version: str,
    correlation_id: str | None,
    causation_id: str | None,
    authorization_context_sha256: str | None,
    surface: EventSurface,
) -> Event:
    """Build the next chained event, source-scrubbing credentials and computing its hash.

    This mirrors the construction in :mod:`thymira.events` using only that package's public
    primitives, so a PostgreSQL-stored chain hashes and verifies identically to the file backend.
    """
    validate_event_type(type)
    draft = Event(
        event_id=new_id("event"),
        run_id=run_id,
        seq=seq,
        type=type,
        schema_version=EVENT_LOG_FORMAT_VERSION,
        ts=utc_now(),
        actor=actor,
        producer=producer,
        producer_version=producer_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
        authorization_context_sha256=authorization_context_sha256,
        surface=surface,
        payload=scrub_credentials_value(dict(payload or {})),
        subject_id=subject_id,
        prev_hash=prev_hash,
    )
    return draft.model_copy(update={"hash": hash_event(draft)})


def _lock_run(conn: Connection, run_id: str) -> None:
    """Take a transaction-scoped advisory lock so appends to one run serialise."""
    conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:run), 0)"), {"run": run_id})


def _load_chain(conn: Connection, run_id: str) -> list[Event]:
    """Read and verify a run's complete chain before allowing an append."""
    statement = (
        sa.select(tables.events.c.document)
        .where(tables.events.c.run_id == run_id)
        .order_by(tables.events.c.seq.asc())
    )
    documents = conn.execute(statement).scalars().all()
    events = [deserialize_event(load_document(document)) for document in documents]
    verification = verify_events(events)
    if not verification.valid:
        msg = f"cannot append to invalid event chain for run {run_id}: {verification.error}"
        raise ValueError(msg)
    return events


def _read_events(conn: Connection, run_id: str) -> list[Event]:
    """Return every event of a run in sequence order."""
    statement = (
        sa.select(tables.events.c.document)
        .where(tables.events.c.run_id == run_id)
        .order_by(tables.events.c.seq.asc())
    )
    documents = conn.execute(statement).scalars().all()
    return [deserialize_event(load_document(document)) for document in documents]


def _insert_event(conn: Connection, event: Event) -> None:
    """Insert one built event as an append-only row."""
    conn.execute(
        tables.events.insert().values(
            run_id=event.run_id,
            seq=event.seq,
            event_id=event.event_id,
            type=event.type.value,
            prev_hash=event.prev_hash,
            hash=event.hash,
            document=canonical_json(event.to_json_dict()),
        )
    )


def append_event_on(
    conn: Connection,
    run_id: str,
    type: EventType,  # noqa: A002  # `type` is the contract field name
    actor: Actor,
    payload: dict[str, Any] | None = None,
    *,
    subject_id: str | None = None,
    producer: str = _PRODUCER,
    producer_version: str = _PRODUCER_VERSION,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    authorization_context_sha256: str | None = None,
    surface: EventSurface = EventSurface.LOG_ONLY,
    lock_run: bool = True,
) -> Event:
    """Append one event to a run's chain on an existing connection and return it.

    ``lock_run`` is disabled only by the lifecycle owner facade after it has acquired the
    connection-level advisory lock for the Run.  Taking a second transaction-level lock on the
    same key from a pooled connection would wait on that deliberately held owner connection.
    """
    if lock_run:
        _lock_run(conn, run_id)
    existing = _load_chain(conn, run_id)
    head = existing[-1] if existing else None
    seq = 0 if head is None else head.seq + 1
    prev_hash = GENESIS_HASH if head is None else (head.hash or GENESIS_HASH)
    event = build_event(
        run_id,
        seq,
        prev_hash,
        type,
        actor,
        payload,
        subject_id=subject_id,
        producer=producer,
        producer_version=producer_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
        authorization_context_sha256=authorization_context_sha256,
        surface=surface,
    )
    _insert_event(conn, event)
    return event


class PgEventLog:
    """A hash-chained event log for one run, persisted in PostgreSQL."""

    def __init__(self, engine: Engine, run_id: str) -> None:
        self._engine = engine
        self.run_id = run_id

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
        producer: str = _PRODUCER,
        producer_version: str = _PRODUCER_VERSION,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append an event with PII intact, persist it, and return it with its hash."""
        with self._engine.begin() as conn:
            return append_event_on(
                conn,
                self.run_id,
                type,
                actor,
                payload,
                subject_id=subject_id,
                producer=producer,
                producer_version=producer_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
                authorization_context_sha256=authorization_context_sha256,
                surface=surface,
            )

    def events(self) -> list[Event]:
        """Every event appended so far, in order."""
        with self._engine.connect() as conn:
            return _read_events(conn, self.run_id)

    def verify(self) -> VerificationResult:
        """Recompute the whole chain from PostgreSQL."""
        return verify_events(self.events())


class PgEventStore:
    """Open and read one hash-chained event log per run, backed by PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def open(self, run_id: Id) -> PgEventLog:
        """Open a log for ``run_id`` (rows are created lazily on the first append)."""
        return PgEventLog(self._engine, run_id)

    def read(self, run_id: Id) -> list[Event]:
        """Read all events for ``run_id``."""
        with self._engine.connect() as conn:
            return _read_events(conn, run_id)


__all__ = ["PgEventLog", "PgEventStore", "append_event_on", "build_event"]
