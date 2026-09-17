"""Repository protocols and local JSON implementations for runs and sessions."""

from __future__ import annotations

import base64
import json
from binascii import Error as BinasciiError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from thymira.events import canonical_json, create_private_file, secure_directory, secure_file
from thymira.schemas import Run, RunStatus, Session
from thymira.state._atomic import replace_with_retry

if TYPE_CHECKING:
    from thymira.schemas import Id

RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class Page[RecordT]:
    """A stable page of records and an opaque cursor for the next page."""

    items: tuple[RecordT, ...]
    next_cursor: str | None = None


@runtime_checkable
class RunRepository(Protocol):
    """Persist and query immutable Run records."""

    def save(self, run: Run) -> Run:
        """Persist ``run`` and return it."""
        ...

    def get(self, run_id: Id) -> Run | None:
        """Return a run by id, if present."""
        ...

    def list(
        self,
        *,
        project_id: Id | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """Return runs ordered by creation time and id."""
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Persist and query immutable Session records."""

    def save(self, session: Session) -> Session:
        """Persist ``session`` and return it."""
        ...

    def get(self, session_id: Id) -> Session | None:
        """Return a session by id, if present."""
        ...

    def list(
        self,
        *,
        project_id: Id | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Session]:
        """Return sessions ordered by creation time and id."""
        ...


def _encode_cursor(record: Run | Session) -> str:
    """Encode the ordering key as an opaque, URL-safe cursor."""
    payload = {"created_at": record.created_at.isoformat(), "id": record.id}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")


_INVALID_CURSOR = "invalid repository cursor"


class InvalidCursorError(ValueError):
    """A pagination cursor the store cannot use, as one named failure.

    Every unusable cursor shape raises this single error rather than an incidental mix of
    exception types: undecodable base64/JSON, a missing or wrong-typed field, and -- the case
    that used to slip through -- a well-formed cursor carrying a *naive* (timezone-less)
    timestamp. The stored ordering key is timezone-aware (`thymira.schemas.utc_now`), so a naive
    cursor is *rejected* here rather than assumed to be UTC and left to raise `TypeError` during
    comparison: rejecting is the fail-safe reading, and silently coercing an ambiguous cursor is
    how two stores end up disagreeing about the same page. Subclasses `ValueError` so existing
    ``except ValueError`` handlers (the API returns 422) keep catching it.
    """


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode and validate a repository cursor, or raise `InvalidCursorError`.

    A naive (timezone-less) ``created_at`` is rejected, not interpreted as UTC (see
    `InvalidCursorError`).
    """
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
        raise InvalidCursorError(_INVALID_CURSOR) from exc
    if not isinstance(record_id, str) or timestamp.tzinfo is None:
        raise InvalidCursorError(_INVALID_CURSOR)
    return timestamp, record_id


def _validate_limit(limit: int) -> None:
    """Reject unusable page sizes."""
    if limit < 1:
        msg = "limit must be at least 1"
        raise ValueError(msg)


def _page(records: list[Run] | list[Session], *, limit: int, cursor: str | None) -> Page:
    """Filter a sorted record list after an optional cursor."""
    _validate_limit(limit)
    ordered = sorted(records, key=lambda record: (record.created_at, record.id))
    if cursor is not None:
        key = _decode_cursor(cursor)
        ordered = [record for record in ordered if (record.created_at, record.id) > key]
    selected = ordered[:limit]
    next_cursor = _encode_cursor(selected[-1]) if len(ordered) > limit else None
    return Page(items=tuple(selected), next_cursor=next_cursor)


class _JsonRepository:
    """Shared safe JSON-file operations for local repositories."""

    def __init__(self, root: Path, directory: str) -> None:
        self.root = Path(root) / directory
        secure_directory(self.root)

    @property
    def transaction_root(self) -> Path:
        """Return the directory a local transaction must restore on failure."""
        return self.root

    def _path(self, record_id: str) -> Path:
        """Return the path for a validated prefixed id."""
        if Path(record_id).name != record_id or not record_id:
            msg = f"invalid repository id: {record_id!r}"
            raise TypeError(msg)
        return self.root / f"{record_id}.json"

    def path_for(self, record_id: str) -> Path:
        """Return the persisted path for ``record_id`` without writing it."""
        return self._path(record_id)

    def _write(self, record_id: str, data: dict[str, object]) -> None:
        """Write one canonical JSON record with an atomic replace."""
        path = self._path(record_id)
        tmp_path = path.with_suffix(".json.tmp")
        secure_directory(path.parent)
        payload = (canonical_json(data) + "\n").encode("utf-8")
        if tmp_path.exists():
            secure_file(tmp_path)
            tmp_path.write_bytes(payload)
        else:
            create_private_file(tmp_path, payload)
        try:
            replace_with_retry(tmp_path, path)
            secure_file(path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _read(self, record_id: str) -> dict[str, object] | None:
        """Read one JSON record, returning ``None`` when absent."""
        path = self._path(record_id)
        if not path.exists():
            return None
        secure_file(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            msg = f"repository record {record_id!r} is not a JSON object"
            raise TypeError(msg)
        return value

    def _read_all(self) -> list[dict[str, object]]:
        """Read all JSON records in deterministic filename order."""
        values: list[dict[str, object]] = []
        for path in sorted(self.root.glob("*.json")):
            secure_file(path)
            values.append(json.loads(path.read_text(encoding="utf-8")))
        return values


class LocalRunRepository(_JsonRepository):
    """Filesystem-backed RunRepository using one canonical JSON file per run."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "runs")

    def save(self, run: Run) -> Run:
        """Persist ``run`` and return it."""
        self._write(run.id, run.to_json_dict())
        return run

    def get(self, run_id: Id) -> Run | None:
        """Return a run by id, if present."""
        data = self._read(run_id)
        return None if data is None else Run.model_validate(data)

    def list(
        self,
        *,
        project_id: Id | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """Return filtered runs in stable order."""
        runs = [Run.model_validate(data) for data in self._read_all()]
        if project_id is not None:
            runs = [run for run in runs if run.project_id == project_id]
        if status is not None:
            runs = [run for run in runs if run.status is status]
        return _page(runs, limit=limit, cursor=cursor)


class LocalSessionRepository(_JsonRepository):
    """Filesystem-backed SessionRepository using one canonical JSON file per session."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "sessions")

    def save(self, session: Session) -> Session:
        """Persist ``session`` and return it."""
        self._write(session.id, session.to_json_dict())
        return session

    def get(self, session_id: Id) -> Session | None:
        """Return a session by id, if present."""
        data = self._read(session_id)
        return None if data is None else Session.model_validate(data)

    def list(
        self,
        *,
        project_id: Id | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Session]:
        """Return filtered sessions in stable order."""
        sessions = [Session.model_validate(data) for data in self._read_all()]
        if project_id is not None:
            sessions = [session for session in sessions if session.project_id == project_id]
        return _page(sessions, limit=limit, cursor=cursor)


__all__ = [
    "InvalidCursorError",
    "LocalRunRepository",
    "LocalSessionRepository",
    "Page",
    "RunRepository",
    "SessionRepository",
]
