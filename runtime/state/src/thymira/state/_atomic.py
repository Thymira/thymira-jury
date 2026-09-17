"""Atomic local-file replacement hardened against the transient Windows file lock.

Every local store here writes to a temporary file and then moves it onto the target with an atomic
replace. On Windows that move raises ``PermissionError`` (``WinError`` 5, access denied; or 32,
sharing violation) when another handle holds the source or target for an instant -- a virus scanner,
the search indexer, or a concurrent reader such as LangGraph reading a checkpoint back while the
next superstep writes it. The failure is transient and load-dependent, so a bounded retry with a
growing backoff turns it into a brief wait instead of a spurious write failure. On POSIX the replace
does not raise this transiently, so the first attempt always succeeds and the retry path is unused.
"""

from __future__ import annotations

import os
import time
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_REPLACE_ATTEMPTS = 10
"""How many times an atomic replace is retried before the transient failure is surfaced."""

_REPLACE_BACKOFF_SECONDS = 0.02
"""Base backoff between replace attempts; it grows linearly with the attempt number."""


class CrossProcessFileLock(AbstractContextManager["CrossProcessFileLock"]):
    """Hold an exclusive advisory lock on one local state file across processes.

    The lock file is separate from the JSON document it protects, so replacing that document
    cannot release the writer lock halfway through a read-modify-write transaction. The lock is
    advisory on POSIX and mandatory for the opened byte on Windows; every Thymira writer uses the
    same path, which gives the local stores one serialisation boundary.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> CrossProcessFileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def replace_with_retry(source: Path, target: Path) -> None:
    """Atomically replace ``target`` with ``source``, tolerating a transient Windows file lock.

    Raises:
        PermissionError: If every attempt is denied (the lock did not clear within the budget).
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            source.replace(target)
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
        else:
            return


def fsync_directory(path: Path) -> None:
    """Flush a directory entry when the operating system exposes directory handles."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


__all__ = ["CrossProcessFileLock", "fsync_directory", "replace_with_retry"]
