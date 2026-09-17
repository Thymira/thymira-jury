"""PostgreSQL implementation of the ``UnitOfWork`` transaction boundary.

Unlike the local backend, which emulates atomicity with file snapshots, this unit of work runs
every staged write inside a single database transaction: writes are executed eagerly on one
connection and the whole transaction commits on a clean exit or rolls back on any exception, so a
failed unit leaves no run, record or event behind.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from thymira.state.postgres.events import append_event_on
from thymira.state.postgres.records import upsert_record
from thymira.state.postgres.repositories import upsert_run, upsert_session

if TYPE_CHECKING:
    from typing import Any

    from sqlalchemy.engine import Connection, Engine

    from thymira.events import EventLog
    from thymira.schemas import Actor, EventType, Run, Session
    from thymira.state.records import Record


class PgUnitOfWork(AbstractContextManager["PgUnitOfWork"]):
    """Group run, session, child-record and event writes into one database transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._conn: Connection | None = None

    def __enter__(self) -> PgUnitOfWork:
        """Open a connection; the transaction starts with the first write (commit as you go)."""
        self._conn = self._engine.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Commit the transaction on a clean exit, or roll it back on any exception."""
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            if exc_type is not None:
                conn.rollback()
            else:
                conn.commit()
        finally:
            conn.close()

    def _connection(self) -> Connection:
        """Return the active transaction connection, or fail if used outside the scope."""
        if self._conn is None:
            msg = "PgUnitOfWork used outside an active transaction"
            raise RuntimeError(msg)
        return self._conn

    def save_run(self, run: Run) -> None:
        """Stage a run write."""
        upsert_run(self._connection(), run)

    def save_session(self, session: Session) -> None:
        """Stage a session write."""
        upsert_session(self._connection(), session)

    def save_record(self, record: Record) -> None:
        """Stage a child-record write."""
        upsert_record(self._connection(), record)

    def append_event(
        self,
        log: EventLog,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
    ) -> None:
        """Stage an event append within the transaction, chained to the run's current head."""
        append_event_on(self._connection(), log.run_id, type, actor, payload, subject_id=subject_id)


__all__ = ["PgUnitOfWork"]
