"""Bounded process-tree reap with a PID-identity rule.

Two properties the criteria name (F6.4, F6.8) and one hazard they name explicitly:

* **Bounded.** A reap either reaches quiescence inside its own deadline or is recorded as
  ``unbounded``. Cleanup never extends a Run indefinitely, and never silently reports success it
  did not observe.
* **Tree-wide.** A child that spawned descendants is reaped with them: the POSIX backend signals
  the session ``start_new_session`` created, and Windows drives ``taskkill /T`` over the process
  tree. Where neither is available the outcome is ``unavailable`` -- declared, never claimed.
* **Identity-checked.** *A pid is a reusable number, not an identity.* Once a process has been
  waited on, the operating system is free to hand its number to anything at all, so a handle that
  already reports an exit status is never signalled again: the reap is skipped and recorded as
  ``skipped_reaped``. The POSIX branch this replaces called ``os.killpg(process.pid, SIGKILL)``
  unconditionally -- on a reaped pid that is a signal aimed at whoever now holds the number.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from thymira.tools.sandbox.termination import (
    REAP_QUIESCED,
    REAP_SKIPPED_REAPED,
    REAP_UNAVAILABLE,
    REAP_UNBOUNDED,
    TREE_SCOPE_DIRECT_CHILD,
    TREE_SCOPE_NONE,
    TREE_SCOPE_PROCESS_GROUP,
    TREE_SCOPE_PROCESS_TREE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ReapOutcome", "Reapable", "reap_process_tree"]


class Reapable(Protocol):
    """The part of :class:`subprocess.Popen` a reap needs, and nothing more."""

    pid: int

    def poll(self) -> int | None:
        """Return the child's exit status, or ``None`` while it is still running."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the child, raising :class:`subprocess.TimeoutExpired` past ``timeout``."""
        ...

    def kill(self) -> None:
        """Terminate the direct child."""
        ...


@dataclass(frozen=True, slots=True)
class ReapOutcome:
    """What one reap did, and whether the tree actually went quiet."""

    reap: str
    tree_scope: str
    signalled_after_reap: bool
    duration_s: float


def reap_process_tree(
    process: Reapable,
    *,
    timeout_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> ReapOutcome:
    """Signal a live child's whole tree and wait, bounded, for it to go quiet.

    Returns without signalling anything when ``process`` already reports an exit status: the
    number it carries may belong to an unrelated process by now (see the module docstring).
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError("reap timeout_s must be a positive number")
    started = clock()
    deadline = started + float(timeout_s)
    if process.poll() is not None:
        return ReapOutcome(REAP_SKIPPED_REAPED, TREE_SCOPE_NONE, False, clock() - started)
    scope, tree_reached = _signal_tree(process, timeout_s=max(0.0, deadline - clock()))
    try:
        process.wait(timeout=max(0.0, deadline - clock()))
    except subprocess.TimeoutExpired:
        return ReapOutcome(REAP_UNBOUNDED, scope, False, clock() - started)
    duration = clock() - started
    if duration > timeout_s:
        return ReapOutcome(REAP_UNBOUNDED, scope, False, duration)
    reap = REAP_QUIESCED if tree_reached else REAP_UNAVAILABLE
    return ReapOutcome(reap, scope, False, duration)


def _signal_tree(process: Reapable, *, timeout_s: float) -> tuple[str, bool]:
    """Signal every process the child leads, reporting how far the signal reached.

    The POSIX branch stays inline under the ``os.name`` test rather than moving to its own
    helper: that test is what tells a type checker running on Windows that ``os.killpg`` and
    ``signal.SIGKILL`` exist on this path at all.
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            # The group is gone, or this child was not started in its own session. The direct
            # child is still ours to end, and the scope says how far the reap actually reached.
            _kill_direct(process)
            return TREE_SCOPE_DIRECT_CHILD, False
        return TREE_SCOPE_PROCESS_GROUP, True
    return _signal_windows_tree(process, timeout_s=timeout_s)


def _signal_windows_tree(process: Reapable, *, timeout_s: float) -> tuple[str, bool]:
    """Drive ``taskkill /T`` over the tree, falling back to the direct child when it is absent.

    ``taskkill`` runs inside cleanup, so it is bounded, argv-only, never a shell, and its own
    output is discarded: nothing it does may raise past this reap or extend it. A Job Object
    would not need the extra process, but it is roughly forty lines of ``ctypes`` against this
    bounded call plus an honest ``unavailable`` fallback.
    """
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        _kill_direct(process)
        return TREE_SCOPE_DIRECT_CHILD, False
    try:
        result = subprocess.run(  # noqa: S603  # explicit argv, never a shell
            [taskkill, "/F", "/T", "/PID", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _kill_direct(process)
        return TREE_SCOPE_DIRECT_CHILD, False
    if result.returncode != 0:
        _kill_direct(process)
        return TREE_SCOPE_DIRECT_CHILD, False
    _kill_direct(process)
    return TREE_SCOPE_PROCESS_TREE, True


def _kill_direct(process: Reapable) -> None:
    """End the direct child, ignoring a race where it has just exited on its own."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        return
