"""SQLAlchemy table definitions for the PostgreSQL runtime-state backend (baseline S19).

Every record is stored twice over: the authoritative copy is the model's canonical JSON in a
``document`` text column, which round-trips byte-for-byte (so a reloaded record equals the one
that was written and an event's hash chain still verifies), while the columns beside it are
projections used only for filtering and ordering. Nothing above ``thymira.state`` reads these
tables; consumers depend on the repository protocols, never on this module.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    Table,
    Text,
)

metadata = MetaData()
"""The single ``MetaData`` the Alembic baseline migration materialises."""

# Lifecycle records have their own metadata so revision 0001 remains a stable baseline.  Revision
# 0002 creates this metadata for databases that already ran the baseline; keeping the two sets
# separate also makes an upgrade's scope explicit instead of relying on ``create_all`` to infer it.
lifecycle_metadata = MetaData()


sessions = Table(
    "sessions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("client", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_sessions_project_created", "project_id", "created_at", "id"),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_runs_project_created", "project_id", "created_at", "id"),
    Index("ix_runs_status", "status"),
)


def _record_table(name: str) -> Table:
    """Return a child-record table keyed by id and ordered by an insertion ``ordinal``.

    Args:
        name: The table name (``agents``, ``tasks`` and the rest).

    Returns:
        A :class:`~sqlalchemy.Table` with the shared child-record shape.
    """
    return Table(
        name,
        metadata,
        Column("ordinal", BigInteger, Identity(), nullable=False, unique=True),
        Column("id", Text, primary_key=True),
        Column("run_id", Text, nullable=False),
        Column("document", Text, nullable=False),
        Index(f"ix_{name}_run_ordinal", "run_id", "ordinal"),
    )


agents = _record_table("agents")
tasks = _record_table("tasks")
tool_calls = _record_table("tool_calls")
experiments = _record_table("experiments")
audit_findings = _record_table("audit_findings")
policy_decisions = _record_table("policy_decisions")

events = Table(
    "events",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    Column("event_id", Text, nullable=False, unique=True),
    Column("type", Text, nullable=False),
    Column("prev_hash", Text, nullable=False),
    Column("hash", Text, nullable=False),
    Column("document", Text, nullable=False),
)

checkpoints = Table(
    "checkpoints",
    metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True),
    Column("checkpoint", LargeBinary, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def _lifecycle_document_table(name: str) -> Table:
    """Return a lifecycle table whose typed document remains authoritative."""
    return Table(
        name,
        lifecycle_metadata,
        Column("id", Text, primary_key=True),
        Column("run_id", Text, nullable=False),
        Column("document", Text, nullable=False),
    )


lifecycle_publications = Table(
    "lifecycle_publications",
    lifecycle_metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False, unique=True),
    Column("session_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("session_linked", Boolean, nullable=False),
    Column("staged_at", DateTime(timezone=True), nullable=False),
    Column("committed_at", DateTime(timezone=True), nullable=True),
    Column("document", Text, nullable=False),
)

lifecycle_controls = Table(
    "lifecycle_controls",
    lifecycle_metadata,
    Column("ordinal", BigInteger, Identity(), nullable=False, unique=True),
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("requested_lane", Text, nullable=False),
    Column("effective_lane", Text, nullable=True),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("proof_id", Text, nullable=False, unique=True),
    Column("authority_digest", Text, nullable=False),
    Column("enqueued_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_lifecycle_controls_run_ordinal", "run_id", "ordinal"),
    Index("ix_lifecycle_controls_run_lane_state", "run_id", "requested_lane", "state"),
)

lifecycle_work = Table(
    "lifecycle_work",
    lifecycle_metadata,
    Column("ordinal", BigInteger, Identity(), nullable=False, unique=True),
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempt", BigInteger, nullable=False),
    Column("claim_owner", Text, nullable=True),
    Column("claim_token", Text, nullable=True),
    Column("claim_expires_at", DateTime(timezone=True), nullable=True),
    Column("dispatch_state", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("result_document", Text, nullable=True),
    Column("document", Text, nullable=False),
    Index("ix_lifecycle_work_run_ordinal", "run_id", "ordinal"),
    Index("ix_lifecycle_work_claimable", "state", "claim_expires_at"),
)

lifecycle_outbox = Table(
    "lifecycle_outbox",
    lifecycle_metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("work_id", Text, nullable=True),
    Column("input_id", Text, nullable=True),
    Column("published", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_lifecycle_outbox_pending", "published", "created_at"),
)

lifecycle_owner_epochs = Table(
    "lifecycle_owner_epochs",
    lifecycle_metadata,
    Column("run_id", Text, primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("epoch", BigInteger, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("acquired_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=True),
    Column("release_reason", Text, nullable=True),
)

lifecycle_turns = _lifecycle_document_table("lifecycle_turns")
lifecycle_outcomes = Table(
    "lifecycle_outcomes",
    lifecycle_metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("outcome_type", Text, nullable=False),
    Column("source_outcome_id", Text, nullable=True),
    Column("document", Text, nullable=False),
)
lifecycle_terminal_bindings = Table(
    "lifecycle_terminal_bindings",
    lifecycle_metadata,
    Column("run_id", Text, primary_key=True),
    Column("document", Text, nullable=False),
)


__all__ = [
    "agents",
    "audit_findings",
    "checkpoints",
    "events",
    "experiments",
    "lifecycle_controls",
    "lifecycle_metadata",
    "lifecycle_outbox",
    "lifecycle_outcomes",
    "lifecycle_owner_epochs",
    "lifecycle_publications",
    "lifecycle_terminal_bindings",
    "lifecycle_turns",
    "lifecycle_work",
    "metadata",
    "policy_decisions",
    "runs",
    "sessions",
    "tasks",
    "tool_calls",
]
