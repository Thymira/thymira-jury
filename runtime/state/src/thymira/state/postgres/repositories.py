"""PostgreSQL implementations of ``RunRepository`` and ``SessionRepository``.

Each record's authoritative form is its canonical JSON ``document``; the ``project_id``,
``status`` and ``created_at`` columns are projections used only to filter and to order. Cursor
pagination reproduces the ``(created_at, id)`` order of the local backends, with ``id`` compared
under the ``C`` collation so PostgreSQL orders the ASCII ids exactly as Python does.
"""

from __future__ import annotations

import base64
import json
from binascii import Error as BinasciiError
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from thymira.events import canonical_json
from thymira.schemas import Run, Session
from thymira.state.postgres import tables
from thymira.state.postgres._serde import dump_document, load_document
from thymira.state.repositories import Page

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Connection, Engine

    from thymira.schemas import Id, RunStatus


def _validate_limit(limit: int) -> None:
    """Reject unusable page sizes, matching the local repositories."""
    if limit < 1:
        msg = "limit must be at least 1"
        raise ValueError(msg)


def encode_cursor(created_at: datetime, record_id: str) -> str:
    """Encode a ``(created_at, id)`` ordering key as an opaque, URL-safe cursor."""
    payload = {"created_at": created_at.isoformat(), "id": record_id}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode and validate a repository cursor."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        timestamp = datetime.fromisoformat(data["created_at"])
        record_id = data["id"]
    except (
        BinasciiError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        msg = "invalid repository cursor"
        raise ValueError(msg) from exc
    if not isinstance(record_id, str):
        msg = "invalid repository cursor"
        raise TypeError(msg)
    return timestamp, record_id


def _upsert(conn: Connection, table: Table, values: dict[str, object]) -> None:
    """Insert a row, replacing every non-key column when the id already exists."""
    statement = pg_insert(table).values(**values)
    updated = {name: statement.excluded[name] for name in values if name != "id"}
    conn.execute(statement.on_conflict_do_update(index_elements=["id"], set_=updated))


def upsert_run(conn: Connection, run: Run) -> None:
    """Persist a run row on the given connection."""
    _upsert(
        conn,
        tables.runs,
        {
            "id": run.id,
            "project_id": run.project_id,
            "session_id": run.session_id,
            "status": run.status.value,
            "created_at": run.created_at,
            "document": dump_document(run),
        },
    )


def upsert_session(conn: Connection, session: Session) -> None:
    """Persist a session row on the given connection."""
    _upsert(
        conn,
        tables.sessions,
        {
            "id": session.id,
            "project_id": session.project_id,
            "client": session.client,
            "created_at": session.created_at,
            "document": dump_document(session),
        },
    )


def _paginate(
    engine: Engine,
    table: Table,
    *,
    conditions: list[sa.ColumnElement[bool]],
    limit: int,
    cursor: str | None,
) -> tuple[list[str], str | None]:
    """Return one ordered page of stored documents and the cursor for the next page.

    Reconstruction into contract models is left to the caller so this stays untyped over the
    document body; only the ``(created_at, id)`` ordering columns drive the cursor.
    """
    _validate_limit(limit)
    where = list(conditions)
    if cursor is not None:
        after_at, after_id = decode_cursor(cursor)
        where.append(
            sa.or_(
                table.c.created_at > after_at,
                sa.and_(table.c.created_at == after_at, table.c.id.collate("C") > after_id),
            )
        )
    statement = sa.select(table.c.document, table.c.created_at, table.c.id)
    if where:
        statement = statement.where(sa.and_(*where))
    statement = statement.order_by(table.c.created_at.asc(), table.c.id.collate("C").asc()).limit(
        limit + 1
    )
    with engine.connect() as conn:
        rows = conn.execute(statement).mappings().all()
    page_rows = rows[:limit]
    documents = [row["document"] for row in page_rows]
    next_cursor = (
        encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["id"])
        if len(rows) > limit and page_rows
        else None
    )
    return documents, next_cursor


class PgRunRepository:
    """Persist and query immutable Run records in PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, run: Run) -> Run:
        """Persist ``run`` and return it."""
        with self._engine.begin() as conn:
            upsert_run(conn, run)
        return run

    def get(self, run_id: Id) -> Run | None:
        """Return a run by id, if present."""
        statement = sa.select(tables.runs.c.document).where(tables.runs.c.id == run_id)
        with self._engine.connect() as conn:
            document = conn.execute(statement).scalar_one_or_none()
        return None if document is None else Run.model_validate(load_document(document))

    def list(
        self,
        *,
        project_id: Id | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """Return runs ordered by creation time and id."""
        conditions: list[sa.ColumnElement[bool]] = []
        if project_id is not None:
            conditions.append(tables.runs.c.project_id == project_id)
        if status is not None:
            conditions.append(tables.runs.c.status == status.value)
        documents, next_cursor = _paginate(
            self._engine, tables.runs, conditions=conditions, limit=limit, cursor=cursor
        )
        items = tuple(Run.model_validate(load_document(document)) for document in documents)
        return Page(items=items, next_cursor=next_cursor)


class PgSessionRepository:
    """Persist and query immutable Session records in PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, session: Session) -> Session:
        """Persist ``session`` and return it."""
        with self._engine.begin() as conn:
            upsert_session(conn, session)
        return session

    def get(self, session_id: Id) -> Session | None:
        """Return a session by id, if present."""
        statement = sa.select(tables.sessions.c.document).where(tables.sessions.c.id == session_id)
        with self._engine.connect() as conn:
            document = conn.execute(statement).scalar_one_or_none()
        return None if document is None else Session.model_validate(load_document(document))

    def list(
        self,
        *,
        project_id: Id | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Session]:
        """Return sessions ordered by creation time and id."""
        conditions: list[sa.ColumnElement[bool]] = []
        if project_id is not None:
            conditions.append(tables.sessions.c.project_id == project_id)
        documents, next_cursor = _paginate(
            self._engine, tables.sessions, conditions=conditions, limit=limit, cursor=cursor
        )
        items = tuple(Session.model_validate(load_document(document)) for document in documents)
        return Page(items=items, next_cursor=next_cursor)


__all__ = ["PgRunRepository", "PgSessionRepository", "upsert_run", "upsert_session"]
