"""EventStore protocol and filesystem implementation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.events import EventLog, JsonlEventLog, secure_directory

if TYPE_CHECKING:
    from thymira.schemas import Event, Id


@runtime_checkable
class EventStore(Protocol):
    """Open and read one hash-chained event log per run."""

    def open(self, run_id: Id) -> EventLog:
        """Open a log, creating it when needed."""
        ...

    def read(self, run_id: Id) -> list[Event]:
        """Read all events for a run."""
        ...


class LocalEventStore:
    """Store each run event log at ``root/events/<run_id>.jsonl``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "events"
        secure_directory(self._root)

    def _path(self, run_id: Id) -> Path:
        """Return a contained event-log path."""
        if Path(run_id).name != run_id:
            msg = f"invalid run id: {run_id!r}"
            raise ValueError(msg)
        return self._root / f"{run_id}.jsonl"

    def open(self, run_id: Id) -> EventLog:
        """Open a hash-chained event log for ``run_id``."""
        path = self._path(run_id)
        # JsonlEventLog performs exclusive private creation itself, so a direct LocalEventStore
        # consumer has the same restrictive boundary before its first raw canonical write. The
        # package-level writer cannot import state permissions by design.
        return JsonlEventLog(path, run_id)

    def read(self, run_id: Id) -> list[Event]:
        """Read all events for ``run_id``."""
        return self.open(run_id).events()


__all__ = ["EventStore", "LocalEventStore"]
