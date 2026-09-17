"""Atomic filesystem records used by the local lifecycle repository."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json
from thymira.schemas import ControlInput, OutboxNotification, PreparedPublication
from thymira.state._atomic import replace_with_retry
from thymira.state._lifecycle_lock import FileLock
from thymira.state.lifecycle_errors import PublicationError

if TYPE_CHECKING:
    from collections.abc import Iterable


class LifecycleFiles:
    """Own private lifecycle paths and atomic JSON record operations."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.private = self.root / ".lifecycle"
        self.publications = self.private / "publications"
        self.controls = self.private / "controls"
        self.work = self.private / "work"
        self.outbox = self.private / "outbox"
        self.owners = self.private / "owners"
        self.locks = self.private / "locks"
        for directory in (
            self.publications,
            self.controls,
            self.work,
            self.outbox,
            self.owners,
            self.locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def publication_path(self, publication_id: str) -> Path:
        """Return the private path for one publication manifest."""
        return self.publications / f"{publication_id}.json"

    def control_path(self, input_id: str) -> Path:
        """Return the private path for one control input."""
        return self.controls / f"{input_id}.json"

    def work_path(self, work_id: str) -> Path:
        """Return the private path for one work item."""
        return self.work / f"{work_id}.json"

    def iter_controls(self) -> Iterable[ControlInput]:
        """Read durable control inputs in deterministic order."""
        for path in sorted(self.controls.glob("*.json")):
            value = self.read_json(path)
            if value is not None:
                yield ControlInput.model_validate(value)

    def parse_prepared(self, value: dict[str, Any]) -> PreparedPublication:
        """Validate the typed publication nested in a private manifest."""
        publication = value.get("publication")
        if not isinstance(publication, dict):
            raise TypeError("publication manifest has no typed publication")
        return PreparedPublication.model_validate(publication)

    def publication_for_run(self, run_id: str) -> dict[str, Any] | None:
        """Find a valid private publication manifest for a Run, failing closed on corruption."""
        found: dict[str, Any] | None = None
        for path in sorted(self.publications.glob("*.json")):
            value = self.read_json(path)
            if not isinstance(value, dict):
                raise PublicationError(f"publication manifest {path.name} is malformed")
            publication = value.get("publication")
            if not isinstance(publication, dict):
                raise PublicationError(f"publication manifest {path.name} has no publication")
            try:
                prepared = PreparedPublication.model_validate(publication)
            except ValueError as exc:
                raise PublicationError(f"publication manifest {path.name} is invalid") from exc
            if prepared.run.id == run_id:
                found = value
        return found

    def outbox_records(self) -> tuple[dict[str, Any], ...]:
        """Read durable broker notifications without consulting a broker."""
        records: list[dict[str, Any]] = []
        for path in sorted(self.outbox.glob("*.json")):
            value = self.read_json(path)
            if value is not None:
                records.append(value)
        return tuple(records)

    def mark_outbox_published(self, notification_id: str) -> OutboxNotification:
        """Durably mark one typed notification after a successful broker publish."""
        target = self.outbox / f"{notification_id}.json"
        with self._outbox_lock():
            value = self.read_json(target)
            if value is None:
                raise FileNotFoundError(f"outbox notification {notification_id} does not exist")
            try:
                notification = OutboxNotification.model_validate(value)
            except ValueError as exc:
                raise PublicationError("outbox notification is malformed") from exc
            if notification.published:
                return notification
            published = notification.model_copy(update={"published": True})
            self.write_json(target, published.to_json_dict())
            return published

    def _outbox_lock(self) -> Any:
        """Return the private outbox lock without coupling this file helper to state ownership."""
        return FileLock(self.locks / "outbox.lock")

    @staticmethod
    def read_json(path: Path) -> dict[str, Any] | None:
        """Read a JSON object, treating a missing or malformed private record as absent."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        """Fsync and atomically replace one private JSON record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        encoded = value if isinstance(value, str) else canonical_json(value)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            replace_with_retry(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        # A successful rename only makes the new name visible.  Read the record back before the
        # caller is told that a lifecycle fact is durable, and flush the containing directory on
        # platforms that expose directory fsync.  Windows does not permit opening a directory as
        # a file through the portable Python API; the flushed file handle plus readback is the
        # strongest portable guarantee there.
        try:
            expected = json.loads(encoded)
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OSError(f"lifecycle record {path} failed durable readback") from exc
        if observed != expected:
            raise OSError(f"lifecycle record {path} changed during durable readback")
        _fsync_parent(path)


def _fsync_parent(path: Path) -> None:
    """Flush a replaced record's parent directory where the operating system supports it."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def sha256_json(value: object) -> str:
    """Hash one canonical JSON value without exposing authority material."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = ["LifecycleFiles", "sha256_json"]
