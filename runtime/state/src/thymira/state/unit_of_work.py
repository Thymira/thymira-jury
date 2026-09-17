"""Atomic write boundary used by core run lifecycle operations."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.events import JsonlEventLog, create_private_file, secure_directory, secure_file

if TYPE_CHECKING:
    from typing import Any

    from thymira.events import EventLog
    from thymira.schemas import Actor, EventType, Run, Session
    from thymira.state.records import LocalRecordRepository, Record
    from thymira.state.repositories import LocalRunRepository, LocalSessionRepository


@runtime_checkable
class UnitOfWork(Protocol):
    """Group run, child-record and event writes into one commit boundary."""

    def __enter__(self) -> UnitOfWork:
        """Start the write scope."""
        ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Commit on success and discard staged writes on failure."""
        ...

    def save_run(self, run: Run) -> None:
        """Stage a run write."""
        ...

    def save_session(self, session: Session) -> None:
        """Stage a session write."""
        ...

    def save_record(self, record: Record) -> None:
        """Stage a child-record write."""
        ...

    def append_event(
        self,
        log: EventLog,
        type: EventType,  # noqa: A002  # contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
    ) -> None:
        """Stage an event append, performed last during commit."""
        ...


@dataclass(frozen=True, slots=True)
class _EventWrite:
    """An event append deferred until a unit commits."""

    log: EventLog
    type: EventType
    actor: Actor
    payload: dict[str, Any] | None
    subject_id: str | None


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """The original bytes of one file touched by a local transaction."""

    path: Path
    existed: bool
    contents: bytes | None

    @classmethod
    def capture(cls, path: Path) -> _FileSnapshot:
        """Capture one file before a local commit begins."""
        target = Path(path)
        secure_directory(target.parent)
        if target.exists():
            secure_file(target)
        if target.exists() and not target.is_file():
            msg = f"transaction target is not a file: {target}"
            raise TypeError(msg)
        return cls(
            path=target,
            existed=target.exists(),
            contents=target.read_bytes() if target.exists() else None,
        )

    def restore(self) -> None:
        """Restore only this transaction's file, leaving unrelated files untouched."""
        if self.existed:
            if self.contents is None:
                msg = f"missing snapshot contents for {self.path}"
                raise RuntimeError(msg)
            secure_directory(self.path.parent)
            if self.path.exists():
                secure_file(self.path)
                self.path.write_bytes(self.contents)
                secure_file(self.path)
            else:
                create_private_file(self.path, self.contents)
        elif self.path.exists():
            secure_file(self.path)
            self.path.unlink()
        else:
            secure_directory(self.path.parent)
            create_private_file(self.path, self.contents or b"")


@dataclass(frozen=True, slots=True)
class _LogSnapshot:
    """The in-memory chain cursor of one event log captured before a local commit.

    ``JsonlEventLog.append`` advances the log object's sequence and previous-hash *and* writes its
    file. Rolling the file back without rewinding these leaves the object ahead of the now-empty
    (or restored) file, so the next append writes a stale ``seq``/``prev_hash`` and the hash chain
    can never verify again. Snapshotting the cursor makes the object roll back with its file.
    """

    log: JsonlEventLog
    seq: int
    prev_hash: str

    @classmethod
    def capture(cls, log: JsonlEventLog) -> _LogSnapshot:
        """Capture one event log's chain cursor before a local commit begins."""
        seq = log._seq  # noqa: SLF001  # the log caches its chain cursor privately
        prev_hash = log._prev_hash  # noqa: SLF001  # rewound together with the file on rollback
        return cls(log=log, seq=seq, prev_hash=prev_hash)

    def restore(self) -> None:
        """Rewind the log's chain cursor to the captured values after a failed commit."""
        self.log._seq = self.seq  # noqa: SLF001  # paired with the file snapshot restore
        self.log._prev_hash = self.prev_hash  # noqa: SLF001  # keeps the next append chained


class LocalUnitOfWork(AbstractContextManager["LocalUnitOfWork"]):
    """Stage local repository writes and commit them in deterministic order."""

    def __init__(
        self,
        run_repository: LocalRunRepository,
        record_repository: LocalRecordRepository,
        session_repository: LocalSessionRepository | None = None,
    ) -> None:
        self._run_repository = run_repository
        self._record_repository = record_repository
        self._session_repository = session_repository
        self._runs: list[Run] = []
        self._sessions: list[Session] = []
        self._records: list[Record] = []
        self._events: list[_EventWrite] = []

    def __enter__(self) -> LocalUnitOfWork:
        """Start a fresh write scope."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Commit staged writes only when the scope exits cleanly."""
        if exc_type is not None:
            self._clear()
            return
        snapshots = self._capture_snapshots()
        log_snapshots = self._capture_log_snapshots()
        try:
            for run in self._runs:
                self._run_repository.save(run)
            if self._session_repository is not None:
                for session in self._sessions:
                    self._session_repository.save(session)
            for record in self._records:
                self._record_repository.save(record)
            for event in self._events:
                event.log.append(
                    event.type,
                    event.actor,
                    event.payload,
                    subject_id=event.subject_id,
                )
        except BaseException:
            self._restore_snapshots(snapshots)
            self._restore_log_snapshots(log_snapshots)
            self._clear()
            raise
        self._clear()

    def save_run(self, run: Run) -> None:
        """Stage a run write."""
        self._runs.append(run)

    def save_record(self, record: Record) -> None:
        """Stage a child-record write."""
        self._records.append(record)

    def save_session(self, session: Session) -> None:
        """Stage a session write."""
        if self._session_repository is None:
            msg = "LocalUnitOfWork was created without a SessionRepository"
            raise RuntimeError(msg)
        self._sessions.append(session)

    def append_event(
        self,
        log: EventLog,
        type: EventType,  # noqa: A002  # contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
    ) -> None:
        """Stage an event append."""
        self._events.append(_EventWrite(log, type, actor, payload, subject_id))

    def _clear(self) -> None:
        """Drop staged writes after commit or rollback."""
        self._runs.clear()
        self._sessions.clear()
        self._records.clear()
        self._events.clear()

    def _capture_snapshots(self) -> tuple[_FileSnapshot, ...]:
        """Capture only files that this transaction can replace or append."""
        paths: list[Path] = []
        for run in self._runs:
            path = self._run_repository.path_for(run.id)
            paths.extend((path, path.with_suffix(".json.tmp")))
        if self._session_repository is not None:
            for session in self._sessions:
                path = self._session_repository.path_for(session.id)
                paths.extend((path, path.with_suffix(".json.tmp")))
        for record in self._records:
            path = self._record_repository.path_for(record)
            order_path = self._record_repository.order_path_for(record)
            paths.extend(
                (
                    path,
                    path.with_suffix(".json.tmp"),
                    order_path,
                    order_path.with_suffix(".json.tmp"),
                )
            )
        for event in self._events:
            if not isinstance(event.log, JsonlEventLog):
                msg = "LocalUnitOfWork requires JsonlEventLog event writes"
                raise TypeError(msg)
            paths.append(event.log.path)
        unique_paths = tuple(dict.fromkeys(Path(path) for path in paths))
        return tuple(_FileSnapshot.capture(path) for path in unique_paths)

    def _capture_log_snapshots(self) -> tuple[_LogSnapshot, ...]:
        """Capture the chain cursor of every event log this transaction appends to.

        ``append`` advances the log object's in-memory cursor as well as its file, so a file-only
        rollback would leave the object ahead of the restored file. One snapshot per distinct log
        object; ``_capture_snapshots`` has already refused any log that is not a ``JsonlEventLog``.
        """
        logs: dict[int, JsonlEventLog] = {}
        for event in self._events:
            if isinstance(event.log, JsonlEventLog):
                logs.setdefault(id(event.log), event.log)
        return tuple(_LogSnapshot.capture(log) for log in logs.values())

    @staticmethod
    def _restore_snapshots(snapshots: tuple[_FileSnapshot, ...]) -> None:
        """Restore affected files after a failed commit."""
        for snapshot in reversed(snapshots):
            snapshot.restore()

    @staticmethod
    def _restore_log_snapshots(snapshots: tuple[_LogSnapshot, ...]) -> None:
        """Rewind each event log's chain cursor after a failed commit."""
        for snapshot in snapshots:
            snapshot.restore()


__all__ = ["LocalUnitOfWork", "UnitOfWork"]
