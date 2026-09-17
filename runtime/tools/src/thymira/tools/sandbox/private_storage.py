"""Private temporary storage for one sandboxed execution.

A child that writes a scratch file into the host's shared temp directory leaves it there for the
next Run, for another tenant of the same machine, and for anything else that can read that
directory. It is also the easiest way to smuggle state between two calls that are supposed to
share nothing. So each execution gets storage of its own, and the three names an interpreter
actually consults -- ``TMPDIR``, ``TEMP`` and ``TMP`` -- are set by the runtime, after the
caller's environment has been scrubbed, so a caller value can never redirect them (F6.2, F6.8).

The container's root filesystem is also read-only (``--read-only``), so a library that writes a
config or cache directory under a hardcoded home path -- matplotlib's ``~/.config/matplotlib``,
notably -- fails outright instead of merely leaking state. ``MPLCONFIGDIR`` is pointed at the same
private directory for the same reason: it is the one name matplotlib actually consults, and this
runtime has no other use for it.

The local backend makes a fresh directory per execution, ``0o700`` where the platform has POSIX
modes, and removes it afterwards; whether the removal actually happened is recorded, never
assumed. The container already has private storage the platform destroys with the container: the
per-run ``/tmp`` tmpfs mounted ``rw,noexec,nosuid``, so there these names simply point at it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "PRIVATE_TEMP_NAMES",
    "PrivateStorage",
    "container_private_storage",
    "private_execution_storage",
]

PRIVATE_TEMP_NAMES: tuple[str, ...] = ("TMPDIR", "TEMP", "TMP")
"""The three names an interpreter consults for temporary storage, on every supported platform."""

_MATPLOTLIB_CONFIG_NAME = "MPLCONFIGDIR"
"""The name matplotlib consults for its writable config/cache directory."""

_CONTAINER_TEMP = "/tmp"  # noqa: S108  # the container's own per-run tmpfs, not a host path

_PREFIX = "thy-"
"""Kept short deliberately: a long prefix plus a nested temporary name hits MAX_PATH on Windows."""


@dataclass(slots=True)
class PrivateStorage:
    """One execution's own temporary directory, and whether it was actually removed."""

    path: Path
    removed: bool = False

    def environment(self) -> dict[str, str]:
        """Return the runtime-owned temporary-storage and matplotlib-config names for the child."""
        return dict.fromkeys((*PRIVATE_TEMP_NAMES, _MATPLOTLIB_CONFIG_NAME), str(self.path))


@contextmanager
def private_execution_storage() -> Iterator[PrivateStorage]:
    """Provide a private temporary directory for one execution and remove it afterwards.

    Removal never raises: a failure to clean up is a fact to record (``removed``), not a reason
    to fail a call that already ran. ``removed`` is checked against the filesystem rather than
    inferred from the removal call returning, so a directory a child still holds open on Windows
    is reported honestly.
    """
    directory = Path(tempfile.mkdtemp(prefix=_PREFIX))
    if os.name == "posix":
        directory.chmod(0o700)
    storage = PrivateStorage(directory)
    try:
        yield storage
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        storage.removed = not directory.exists()


def container_private_storage() -> dict[str, str]:
    """Return the temporary-storage and matplotlib-config names pointing at the per-run tmpfs."""
    return dict.fromkeys((*PRIVATE_TEMP_NAMES, _MATPLOTLIB_CONFIG_NAME), _CONTAINER_TEMP)
