"""Connection and pool configuration for the PostgreSQL backend, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import create_engine as sa_create_engine

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Engine

_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 10
_DEFAULT_POOL_TIMEOUT = 30.0


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Return an integer environment value, falling back to ``default`` when unset."""
    raw = env.get(key)
    return default if raw is None or not raw.strip() else int(raw)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    """Return a float environment value, falling back to ``default`` when unset."""
    raw = env.get(key)
    return default if raw is None or not raw.strip() else float(raw)


def _env_bool(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    """Return a boolean environment value from the usual truthy spellings."""
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """A SQLAlchemy DSN plus pool parameters for the PostgreSQL backend.

    The DSN uses the ``postgresql+psycopg`` driver (psycopg 3). ``from_env`` accepts a whole DSN
    in ``THYMIRA_POSTGRES_DSN`` or assembles one from the ``THYMIRA_POSTGRES_*`` parts; secrets
    stay in the environment and never in code.
    """

    dsn: str
    pool_size: int = _DEFAULT_POOL_SIZE
    max_overflow: int = _DEFAULT_MAX_OVERFLOW
    pool_timeout: float = _DEFAULT_POOL_TIMEOUT
    pool_pre_ping: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PostgresSettings:
        """Build settings from ``THYMIRA_POSTGRES_*`` environment variables.

        Args:
            env: Environment mapping to read; defaults to ``os.environ``.

        Returns:
            The resolved settings.
        """
        source = os.environ if env is None else env
        dsn = source.get("THYMIRA_POSTGRES_DSN")
        if not dsn or not dsn.strip():
            host = source.get("THYMIRA_POSTGRES_HOST", "localhost")
            port = source.get("THYMIRA_POSTGRES_PORT", "5432")
            user = source.get("THYMIRA_POSTGRES_USER", "thymira")
            password = source.get("THYMIRA_POSTGRES_PASSWORD", "")
            database = source.get("THYMIRA_POSTGRES_DB", "thymira")
            dsn = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"
        return cls(
            dsn=dsn,
            pool_size=_env_int(source, "THYMIRA_POSTGRES_POOL_SIZE", _DEFAULT_POOL_SIZE),
            max_overflow=_env_int(source, "THYMIRA_POSTGRES_MAX_OVERFLOW", _DEFAULT_MAX_OVERFLOW),
            pool_timeout=_env_float(source, "THYMIRA_POSTGRES_POOL_TIMEOUT", _DEFAULT_POOL_TIMEOUT),
            pool_pre_ping=_env_bool(source, "THYMIRA_POSTGRES_POOL_PRE_PING", default=True),
        )


def create_engine(settings: PostgresSettings | None = None) -> Engine:
    """Create a pooled SQLAlchemy engine for the PostgreSQL backend.

    Args:
        settings: Explicit settings; when omitted they are read from the environment.

    Returns:
        A configured, pooled :class:`~sqlalchemy.engine.Engine`.
    """
    resolved = settings or PostgresSettings.from_env()
    return sa_create_engine(
        resolved.dsn,
        pool_size=resolved.pool_size,
        max_overflow=resolved.max_overflow,
        pool_timeout=resolved.pool_timeout,
        pool_pre_ping=resolved.pool_pre_ping,
        future=True,
    )


__all__ = ["PostgresSettings", "create_engine"]
