"""Initial runtime-state schema: sessions, runs, child records, events, checkpoints.

Revision ID: 0001
Revises:
Create Date: baseline S19.

The baseline migration materialises the whole :data:`thymira.state.postgres.tables.metadata`, so
the migration and the SQLAlchemy tables never drift: there is a single source of truth for the
schema.
"""

from __future__ import annotations

from alembic import op

from thymira.state.postgres.tables import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create every runtime-state table."""
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    """Drop every runtime-state table."""
    metadata.drop_all(op.get_bind())
