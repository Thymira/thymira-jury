"""Durable compare-and-set persistence for goal and plan boards.

The repository stores one complete immutable :class:`~thymira.schemas.PlanBoard` projection per
Run.  It is deliberately independent of the canonical event log: the owner records a board
mutation event and updates this projection under the same owner boundary, while clients only use
this repository for read-back.  A board revision can never be overwritten by a stale writer.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import AbstractContextManager
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.events import canonical_json
from thymira.schemas import Id, PlanBoard
from thymira.state._atomic import replace_with_retry

if TYPE_CHECKING:
    from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class BoardConflictError(ValueError):
    """Raised when a board mutation was computed from a stale revision."""


class BoardPersistenceError(RuntimeError):
    """Raised when a board file cannot be decoded as a typed board."""


@runtime_checkable
class BoardRepository(Protocol):
    """Read and compare-and-set one durable board per Run."""

    def get(self, run_id: Id) -> PlanBoard | None:
        """Return the current board, or ``None`` before the first plan is published."""
        ...

    def save(self, board: PlanBoard, *, expected_revision: int) -> PlanBoard:
        """Persist ``board`` only when its predecessor has ``expected_revision``."""
        ...

    def history(self, run_id: Id) -> tuple[PlanBoard, ...]:
        """Return the append-only board snapshots for independent replay."""
        ...


class _FileLock(AbstractContextManager["_FileLock"]):
    """Take a short cross-process lock over one board file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        if self._path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        file_handle = handle
        if os.name == "nt":
            file_handle.seek(0)
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
        file_handle.close()


class LocalBoardRepository:
    """Store PlanBoard records as atomically replaced JSON files."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, run_id: Id) -> threading.Lock:
        """Return the in-process lock for ``run_id``."""
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _path(self, run_id: Id) -> Path:
        """Return the contained board path for ``run_id``."""
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be one plain non-empty identifier")
        path = self._root / f"{run_id}.json"
        if not path.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("board path escapes repository root")
        return path

    def _history_path(self, run_id: Id) -> Path:
        """Return the append-only snapshot history path for a Run."""
        return self._path(run_id).with_suffix(".history.jsonl")

    def get(self, run_id: Id) -> PlanBoard | None:
        """Read and validate the current board for ``run_id``."""
        snapshots = self._read_history(run_id)
        if snapshots:
            # History is authoritative; the JSON file is a recoverable latest projection and
            # may lag after a process dies between the two fsynced writes.
            return snapshots[-1]
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return PlanBoard.model_validate(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BoardPersistenceError(f"board {run_id} is not a valid PlanBoard") from exc

    def save(self, board: PlanBoard, *, expected_revision: int) -> PlanBoard:
        """Compare and set one board revision with an atomic replace."""
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        try:
            board = PlanBoard.model_validate(board.to_json_dict())
        except (TypeError, ValueError) as exc:
            raise BoardPersistenceError("cannot persist an invalid PlanBoard") from exc
        path = self._path(board.run_id)
        lock_path = path.with_suffix(".lock")
        with self._lock_for(board.run_id), _FileLock(lock_path):
            history = self._read_history(board.run_id)
            current = history[-1] if history else self._read_latest(board.run_id)
            if current is not None and not history and current.revision > 0:
                raise BoardPersistenceError(
                    f"board {board.run_id} has no complete revision history"
                )
            current_revision = 0 if current is None else current.revision
            if current is None and board.revision not in {expected_revision, expected_revision + 1}:
                raise BoardConflictError(
                    f"board {board.run_id}: expected initial revision {expected_revision}, "
                    f"record carries {board.revision}"
                )
            if current is not None and current_revision != expected_revision:
                raise BoardConflictError(
                    f"board {board.run_id}: expected revision {expected_revision}, "
                    f"current revision is {current_revision}"
                )
            if current is not None and board.revision <= current_revision:
                raise BoardConflictError("board revision must increase monotonically")
            if current is not None and board.revision != current_revision + 1:
                raise BoardConflictError("board revision must advance exactly once")
            predecessor = (
                None
                if current is None
                else sha256(canonical_json(current.to_json_dict()).encode("utf-8")).hexdigest()
            )
            board = board.model_copy(update={"predecessor_sha256": predecessor})
            # Append the authoritative record first.  ``get`` can recover from history if the
            # process dies before the replaceable latest projection is updated.
            history_path = self._history_path(board.run_id)
            with history_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(board.to_json_dict()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            payload = canonical_json(board.to_json_dict()) + "\n"
            temporary = path.with_name(f".{path.stem}.tmp")
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            try:
                with temporary.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                replace_with_retry(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return board

    def compare_and_set(self, board: PlanBoard, *, expected_revision: int) -> PlanBoard:
        """Alias for :meth:`save` used by CAS-oriented consumers."""
        return self.save(board, expected_revision=expected_revision)

    def read_plan(self, run_id: Id) -> PlanBoard | None:
        """Return the latest persisted plan for a read-only API route."""
        return self.get(run_id)

    def history(self, run_id: Id) -> tuple[PlanBoard, ...]:
        """Read and validate append-only snapshots for MIRA's independent replay."""
        return self._read_history(run_id)

    def _read_latest(self, run_id: Id) -> PlanBoard | None:
        """Read the replaceable latest projection without consulting history."""
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return PlanBoard.model_validate(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BoardPersistenceError(f"board {run_id} is not a valid PlanBoard") from exc

    def _read_history(self, run_id: Id) -> tuple[PlanBoard, ...]:
        """Read history and reject missing or non-contiguous persisted revisions."""
        history_path = self._history_path(run_id)
        if not history_path.is_file():
            return ()
        snapshots: list[PlanBoard] = []
        try:
            snapshots.extend(
                PlanBoard.model_validate(json.loads(line))
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BoardPersistenceError(f"board history {run_id} is invalid") from exc
        if any(snapshot.run_id != run_id for snapshot in snapshots):
            raise BoardPersistenceError(f"board history {run_id} contains another Run")
        if snapshots:
            if any(snapshot.plan_id != snapshots[0].plan_id for snapshot in snapshots):
                raise BoardPersistenceError(f"board history {run_id} contains another plan")
            if snapshots[0].revision != 1:
                raise BoardPersistenceError(
                    f"board history {run_id} does not begin at revision one"
                )
            if snapshots[0].predecessor_sha256 is not None:
                raise BoardPersistenceError(f"board history {run_id} has an invalid genesis")
            if any(
                current.revision != previous.revision + 1
                for previous, current in pairwise(snapshots)
            ):
                raise BoardPersistenceError(f"board history {run_id} has a missing revision")
            if any(
                current.predecessor_sha256
                != sha256(canonical_json(previous.to_json_dict()).encode("utf-8")).hexdigest()
                for previous, current in pairwise(snapshots)
            ):
                raise BoardPersistenceError(f"board history {run_id} has a broken predecessor")
        return tuple(snapshots)


__all__ = ["BoardConflictError", "BoardPersistenceError", "BoardRepository", "LocalBoardRepository"]
