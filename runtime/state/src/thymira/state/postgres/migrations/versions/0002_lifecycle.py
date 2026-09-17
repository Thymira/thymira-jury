"""Add the durable PostgreSQL lifecycle boundary.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-08.

Lifecycle state is separate from the original repository tables so a database that already ran the
baseline receives the owner, publication, inbox, work-board and outbox contract through a normal
Alembic upgrade.
"""

from __future__ import annotations

from alembic import op

from thymira.state.postgres.tables import lifecycle_metadata

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the durable lifecycle tables."""
    lifecycle_metadata.create_all(op.get_bind())


def downgrade() -> None:
    """Drop the durable lifecycle tables."""
    lifecycle_metadata.drop_all(op.get_bind())
