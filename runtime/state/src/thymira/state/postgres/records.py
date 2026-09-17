"""PostgreSQL implementation of ``RecordRepository`` for per-run child records.

Each record kind (agent, task, tool_call, experiment) has its own table; every row carries a
monotonic ``ordinal`` identity column so ``list_for_run`` returns records in insertion order,
matching the local backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from thymira.schemas import Agent, Experiment, Task, ToolCall
from thymira.state.postgres import tables
from thymira.state.postgres._serde import dump_document, load_document

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Connection, Engine

    from thymira.schemas import Id
    from thymira.state.records import Record

_KIND_TABLES: dict[str, Table] = {
    "agent": tables.agents,
    "task": tables.tasks,
    "tool_call": tables.tool_calls,
    "experiment": tables.experiments,
}
_KIND_MODELS: dict[str, type[Agent] | type[Task] | type[ToolCall] | type[Experiment]] = {
    "agent": Agent,
    "task": Task,
    "tool_call": ToolCall,
    "experiment": Experiment,
}


def _kind_of(record: Record) -> str:
    """Return the stable storage kind for a record instance."""
    if isinstance(record, Agent):
        return "agent"
    if isinstance(record, Task):
        return "task"
    if isinstance(record, ToolCall):
        return "tool_call"
    return "experiment"


def _resolve(
    kind: str,
) -> tuple[Table, type[Agent] | type[Task] | type[ToolCall] | type[Experiment]]:
    """Return the table and model for a record kind, rejecting unknown kinds."""
    if kind not in _KIND_TABLES:
        msg = f"unknown record kind: {kind!r}"
        raise ValueError(msg)
    return _KIND_TABLES[kind], _KIND_MODELS[kind]


def upsert_record(conn: Connection, record: Record) -> None:
    """Persist a child record on the given connection, preserving its ordinal on replace."""
    table = _KIND_TABLES[_kind_of(record)]
    statement = pg_insert(table).values(
        id=record.id, run_id=record.run_id, document=dump_document(record)
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "run_id": statement.excluded["run_id"],
                "document": statement.excluded["document"],
            },
        )
    )


class PgRecordRepository:
    """Persist Agent, Task, ToolCall and Experiment records keyed by run and record id."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, record: Record) -> Record:
        """Persist a child record and return it."""
        with self._engine.begin() as conn:
            upsert_record(conn, record)
        return record

    def get(self, run_id: Id, record_id: Id, kind: str) -> Record | None:
        """Return one child record, if present."""
        table, model = _resolve(kind)
        statement = sa.select(table.c.document).where(
            sa.and_(table.c.run_id == run_id, table.c.id == record_id)
        )
        with self._engine.connect() as conn:
            document = conn.execute(statement).scalar_one_or_none()
        return None if document is None else model.model_validate(load_document(document))

    def list_for_run(self, run_id: Id, kind: str) -> tuple[Record, ...]:
        """Return records for a run in insertion order."""
        table, model = _resolve(kind)
        statement = (
            sa.select(table.c.document)
            .where(table.c.run_id == run_id)
            .order_by(table.c.ordinal.asc())
        )
        with self._engine.connect() as conn:
            documents = conn.execute(statement).scalars().all()
        return tuple(model.model_validate(load_document(document)) for document in documents)


__all__ = ["PgRecordRepository", "upsert_record"]
