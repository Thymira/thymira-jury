"""Alembic-driven schema management for the PostgreSQL backend.

The migrations live beside this module in ``migrations/``; ``run_migrations`` drives Alembic
programmatically so the same code path materialises the schema in production and in tests, which
is what proves the migrations actually build a working schema.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(dsn: str) -> Config:
    """Return an Alembic config bound to the packaged migrations and ``dsn``."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def run_migrations(dsn: str) -> None:
    """Upgrade the database at ``dsn`` to the latest schema revision."""
    command.upgrade(alembic_config(dsn), "head")


def downgrade_all(dsn: str) -> None:
    """Roll the database at ``dsn`` back to an empty schema."""
    command.downgrade(alembic_config(dsn), "base")


__all__ = ["alembic_config", "downgrade_all", "run_migrations"]
