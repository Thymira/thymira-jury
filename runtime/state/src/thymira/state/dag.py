"""Durable persistence for the orchestrator-owned compare-and-set DAG."""

from __future__ import annotations

import json
import os
import threading
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Protocol, runtime_checkable

from thymira.events import canonical_json
from thymira.schemas import DagBoard, Id
from thymira.state._atomic import replace_with_retry
from thymira.state.board import BoardConflictError, _FileLock


@runtime_checkable
class DagRepository(Protocol):
    """Read and compare-and-set one orchestrator DAG per Run."""

    def get(self, run_id: Id) -> DagBoard | None:
        """Return the latest DAG snapshot for ``run_id``."""
        ...

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Persist a new DAG revision when the expected revision is current."""
        ...

    def history(self, run_id: Id) -> tuple[DagBoard, ...]:
        """Return the append-only topology snapshots for independent replay."""
        ...


class LocalDagRepository:
    """Persist DAG snapshots as fsynced, atomically replaced JSON files."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}

    def _path(self, run_id: Id) -> Path:
        """Return a contained path for a Run's DAG."""
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be one plain non-empty identifier")
        return self._root / f"{run_id}.json"

    def _history_path(self, run_id: Id) -> Path:
        """Return the append-only topology history path for a Run."""
        return self._path(run_id).with_suffix(".history.jsonl")

    def get(self, run_id: Id) -> DagBoard | None:
        """Read and validate a DAG snapshot."""
        snapshots = self._read_history(run_id)
        if snapshots:
            # History is authoritative; the JSON file is a recoverable latest projection.
            return snapshots[-1]
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            return DagBoard.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"DAG board {run_id} is invalid") from exc

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Compare and set a strictly newer DAG revision."""
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        try:
            board = DagBoard.model_validate(board.to_json_dict())
        except (TypeError, ValueError) as exc:
            raise ValueError("cannot persist an invalid DAG board") from exc
        path = self._path(board.run_id)
        lock_path = path.with_suffix(".lock")
        with self._guard:
            lock = self._locks.setdefault(board.run_id, threading.Lock())
        with lock, _FileLock(lock_path):
            history = self._read_history(board.run_id)
            current = history[-1] if history else self._read_latest(board.run_id)
            if current is not None and not history and current.revision > 0:
                raise ValueError(f"DAG {board.run_id} has no complete revision history")
            current_revision = 0 if current is None else current.revision
            if current is None and board.revision not in {expected_revision, expected_revision + 1}:
                raise BoardConflictError(
                    f"DAG {board.run_id}: expected initial revision {expected_revision}, "
                    f"record carries {board.revision}"
                )
            if current is not None and current_revision != expected_revision:
                raise BoardConflictError(
                    f"DAG {board.run_id}: expected revision {expected_revision}, "
                    f"current revision is {current_revision}"
                )
            if current is not None and board.revision <= current_revision:
                raise BoardConflictError("DAG revision must increase monotonically")
            if current is not None and board.revision != current_revision + 1:
                raise BoardConflictError("DAG revision must advance exactly once")
            predecessor = (
                None
                if current is None
                else sha256(canonical_json(current.to_json_dict()).encode("utf-8")).hexdigest()
            )
            board = board.model_copy(update={"predecessor_sha256": predecessor})
            # Append the authoritative record first.  A restart can derive the latest board from
            # history if the replaceable JSON projection was not written before process death.
            history_path = self._history_path(board.run_id)
            with history_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(board.to_json_dict()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary = path.with_name(f".{path.stem}.tmp")
            temporary.write_text(canonical_json(board.to_json_dict()) + "\n", encoding="utf-8")
            try:
                replace_with_retry(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return board

    def compare_and_set(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Alias for :meth:`save` used by CAS-oriented callers."""
        return self.save(board, expected_revision=expected_revision)

    def history(self, run_id: Id) -> tuple[DagBoard, ...]:
        """Read and validate append-only topology snapshots for MIRA replay."""
        return self._read_history(run_id)

    def _read_latest(self, run_id: Id) -> DagBoard | None:
        """Read the replaceable latest projection without consulting history."""
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            return DagBoard.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"DAG board {run_id} is invalid") from exc

    def _read_history(self, run_id: Id) -> tuple[DagBoard, ...]:
        """Read history and reject missing or non-contiguous persisted revisions."""
        history_path = self._history_path(run_id)
        if not history_path.is_file():
            return ()
        snapshots: list[DagBoard] = []
        try:
            snapshots.extend(
                DagBoard.model_validate(json.loads(line))
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"DAG history {run_id} is invalid") from exc
        if any(snapshot.run_id != run_id for snapshot in snapshots):
            raise ValueError(f"DAG history {run_id} contains another Run")
        if snapshots:
            if any(snapshot.board_id != snapshots[0].board_id for snapshot in snapshots):
                raise ValueError(f"DAG history {run_id} contains another board")
            if snapshots[0].revision != 1:
                raise ValueError(f"DAG history {run_id} does not begin at revision one")
            if snapshots[0].predecessor_sha256 is not None:
                raise ValueError(f"DAG history {run_id} has an invalid genesis")
            if any(
                current.revision != previous.revision + 1
                for previous, current in pairwise(snapshots)
            ):
                raise ValueError(f"DAG history {run_id} has a missing revision")
            if any(
                current.predecessor_sha256
                != sha256(canonical_json(previous.to_json_dict()).encode("utf-8")).hexdigest()
                for previous, current in pairwise(snapshots)
            ):
                raise ValueError(f"DAG history {run_id} has a broken predecessor")
        return tuple(snapshots)


LocalOrchestratorBoardRepository = LocalDagRepository


__all__ = ["DagRepository", "LocalDagRepository", "LocalOrchestratorBoardRepository"]
