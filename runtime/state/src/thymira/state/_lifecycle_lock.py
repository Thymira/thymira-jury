"""Cross-process nonblocking local lifecycle locks."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from thymira.state.lifecycle_errors import OwnerBusyError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class FileLock(AbstractContextManager["FileLock"]):
    """Hold one nonblocking OS-backed exclusive lock for the context lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file: Any | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise OwnerBusyError(f"lifecycle lock is busy: {self.path.name}") from exc
        self._file = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._file
        self._file = None
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


__all__ = ["FileLock"]
