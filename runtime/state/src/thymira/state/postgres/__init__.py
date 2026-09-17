"""PostgreSQL implementations of the runtime-state repository protocols (RA-STATE-04).

This FINAL-tier backend sits behind the same ``RunRepository``, ``SessionRepository``,
``RecordRepository``, ``EventStore``, ``CheckpointRepository`` and ``UnitOfWork`` protocols as the
local JSON/JSONL backend (ADR-0010). Nothing above ``thymira.state`` learns which backend it is
talking to; substitutability is proven by running the RA-STATE-05 conformance suite against both.

Importing this package pulls in SQLAlchemy, psycopg and Alembic; ``thymira.state`` itself never
imports it, so the local backend stays dependency-light.
"""

from thymira.state.postgres.checkpoints import PgCheckpointRepository
from thymira.state.postgres.config import PostgresSettings, create_engine
from thymira.state.postgres.events import PgEventLog, PgEventStore
from thymira.state.postgres.lifecycle import PgLifecycleRepository, PgRunHandle
from thymira.state.postgres.records import PgRecordRepository
from thymira.state.postgres.repositories import PgRunRepository, PgSessionRepository
from thymira.state.postgres.schema import alembic_config, downgrade_all, run_migrations
from thymira.state.postgres.tables import metadata
from thymira.state.postgres.unit_of_work import PgUnitOfWork

__all__ = [
    "PgCheckpointRepository",
    "PgEventLog",
    "PgEventStore",
    "PgLifecycleRepository",
    "PgRecordRepository",
    "PgRunHandle",
    "PgRunRepository",
    "PgSessionRepository",
    "PgUnitOfWork",
    "PostgresSettings",
    "alembic_config",
    "create_engine",
    "downgrade_all",
    "metadata",
    "run_migrations",
]
