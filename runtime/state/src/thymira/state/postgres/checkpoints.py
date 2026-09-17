"""PostgreSQL implementation of ``CheckpointRepository`` for LangGraph state blobs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from thymira.schemas import utc_now
from thymira.state.postgres import tables

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class PgCheckpointRepository:
    """Store one latest checkpoint per ``(thread_id, checkpoint_ns)`` coordinate as a byte blob."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, thread_id: str, checkpoint_ns: str, checkpoint: bytes) -> None:
        """Persist a checkpoint, replacing any previous blob for the coordinate."""
        statement = pg_insert(tables.checkpoints).values(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint=checkpoint,
            updated_at=utc_now(),
        )
        statement = statement.on_conflict_do_update(
            index_elements=["thread_id", "checkpoint_ns"],
            set_={
                "checkpoint": statement.excluded["checkpoint"],
                "updated_at": statement.excluded["updated_at"],
            },
        )
        with self._engine.begin() as conn:
            conn.execute(statement)

    def get(self, thread_id: str, checkpoint_ns: str) -> bytes | None:
        """Return the checkpoint for one coordinate, if present."""
        statement = sa.select(tables.checkpoints.c.checkpoint).where(
            tables.checkpoints.c.thread_id == thread_id,
            tables.checkpoints.c.checkpoint_ns == checkpoint_ns,
        )
        with self._engine.connect() as conn:
            blob = conn.execute(statement).scalar_one_or_none()
        return None if blob is None else bytes(blob)

    def list(self, thread_id: str, checkpoint_ns: str) -> tuple[str, ...]:
        """Return ``(checkpoint_ns,)`` when a blob exists for the coordinate, else ``()``."""
        return (checkpoint_ns,) if self.get(thread_id, checkpoint_ns) is not None else ()


__all__ = ["PgCheckpointRepository"]
