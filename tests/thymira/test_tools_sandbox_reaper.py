"""Bounded tree reap with PID-identity protection (F6.4, F6.8).

Both nodes drive real processes. The identity node deliberately arranges the hazard the criterion
names -- the recorded pid now belongs to a process this run never started -- and proves the
reaper refuses to signal it; an unrelated live process is the oracle, since it either survives or
it does not.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest

from thymira.schemas import SandboxMode
from thymira.tools.sandbox import LocalSubprocessSandbox
from thymira.tools.sandbox import reaper as reaper_module
from thymira.tools.sandbox.reaper import reap_process_tree

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


class _AlreadyReaped:
    """A handle whose recorded pid belongs to a process this run never started.

    Exactly the PID-reuse hazard F6.4 names: the run's child was reaped, the OS handed its
    number to something else, and a reaper that trusted the recorded number alone would signal a
    stranger. ``poll()`` reports the status the run already collected.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.signals: list[str] = []

    def poll(self) -> int | None:
        return 0

    def wait(self, timeout: float | None = None) -> int:  # matches Popen.wait
        return 0

    def kill(self) -> None:
        self.signals.append("kill")


class _LiveChild:
    """Minimal live handle for exercising the Windows tree command result."""

    pid = 999999

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        """Report a child that remains live until the direct fallback kills it."""
        return None

    def wait(self, timeout: float | None = None) -> int:
        """Return an exit status for the reaper protocol."""
        return 0

    def kill(self) -> None:
        """Record the direct-child fallback."""
        self.killed = True


class _ClockedLiveChild:
    """Live handle whose wait advances the injected cleanup clock."""

    pid = 424242

    def __init__(self, advance: Any, *, wait_factor: float = 1.0, wait_extra: float = 0.0) -> None:
        self._advance = advance
        self._wait_factor = wait_factor
        self._wait_extra = wait_extra

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self._advance(float(timeout or 0.0) * self._wait_factor + self._wait_extra)
        return 0

    def kill(self) -> None:
        return None


def test_the_reaper_never_signals_a_pid_it_has_already_reaped() -> None:
    sentinel = subprocess.Popen(  # explicit argv, never a shell
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = _AlreadyReaped(sentinel.pid)
    try:
        outcome = reap_process_tree(handle, timeout_s=1.0)  # type: ignore[arg-type]

        assert outcome.reap == "skipped_reaped"
        assert outcome.signalled_after_reap is False
        assert handle.signals == []
        time.sleep(0.2)
        assert sentinel.poll() is None, "the reaper signalled a process it never started"
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)


def test_windows_tree_reap_reports_direct_scope_when_taskkill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero taskkill status cannot be recorded as a successful tree reap."""
    child = _LiveChild()
    calls: list[list[str]] = []

    def failed_taskkill(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 128, "", "process not found")

    monkeypatch.setattr(reaper_module.shutil, "which", lambda _name: "taskkill")
    monkeypatch.setattr(reaper_module.subprocess, "run", failed_taskkill)

    scope, reached = reaper_module._signal_windows_tree(child, timeout_s=1.0)

    assert calls == [["taskkill", "/F", "/T", "/PID", "999999"]]
    assert scope == "direct_child"
    assert reached is False
    assert child.killed is True


@pytest.mark.parametrize(
    ("signal_elapsed", "wait_factor", "wait_extra", "expected_reap"),
    [
        (0.2, 0.5, 0.0, "quiesced"),
        (1.0, 0.0, 0.0, "quiesced"),
        (0.2, 1.0, 0.1, "unbounded"),
        (1.1, 0.0, 0.0, "unbounded"),
    ],
    ids=["inside", "exact-boundary", "late-wait", "late-signal"],
)
def test_windows_tree_reap_uses_final_clock_for_the_global_deadline(
    monkeypatch: pytest.MonkeyPatch,
    signal_elapsed: float,
    wait_factor: float,
    wait_extra: float,
    expected_reap: str,
) -> None:
    """The tree command and final wait share one bound, including their final observed time."""
    elapsed = [0.0]
    child = _ClockedLiveChild(
        lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
        wait_factor=wait_factor,
        wait_extra=wait_extra,
    )

    def slow_taskkill(_argv: list[str], **kwargs: Any) -> object:
        elapsed[0] += signal_elapsed
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(reaper_module.os, "name", "nt")
    monkeypatch.setattr(reaper_module.shutil, "which", lambda _name: "taskkill")
    monkeypatch.setattr(reaper_module.subprocess, "run", slow_taskkill)

    outcome = reap_process_tree(
        child,
        timeout_s=1.0,
        clock=lambda: elapsed[0],  # type: ignore[arg-type]
    )

    assert outcome.reap == expected_reap
    if expected_reap == "quiesced":
        assert outcome.duration_s <= 1.0
    else:
        assert outcome.duration_s > 1.0


_TREE_SCRIPT = """
import subprocess
import sys
import time

target = sys.argv[1]
code = (
    "import time\\n"
    "while True:\\n"
    "    open(%r, 'a').write('x')\\n"
    "    time.sleep(0.05)\\n"
) % target
subprocess.Popen([sys.executable, "-c", code])
print("spawned", flush=True)
time.sleep(60)
"""


def test_a_timed_out_run_reaps_the_whole_tree_inside_its_bound(tmp_path: Path) -> None:
    """A grandchild that outlives its parent is still reaped, and quiescence is recorded."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "grandchild.log"
    script = workspace / "tree.py"
    script.write_text(_TREE_SCRIPT, encoding="utf-8", newline="\n")

    run = LocalSubprocessSandbox().run(
        ["python", str(script), str(target)],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=1.0,
    )

    assert run.exit_code == 124
    termination = run.termination
    assert termination is not None
    assert termination.reap in {"quiesced", "unavailable"}
    assert termination.reap_deadline_s is not None
    if termination.reap == "unavailable":
        pytest.skip(f"this platform offers no tree reap: {termination.tree_scope}")
    assert termination.tree_scope in {"process_group", "process_tree"}
    time.sleep(0.4)
    first = target.stat().st_size if target.exists() else 0
    time.sleep(0.4)
    assert (target.stat().st_size if target.exists() else 0) == first, (
        "a grandchild outlived the reaped run"
    )


def test_a_live_child_is_reaped_and_its_quiescence_recorded(tmp_path: Path) -> None:
    process = subprocess.Popen(  # explicit argv, never a shell
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
    )
    try:
        outcome = reap_process_tree(process, timeout_s=5.0)

        assert outcome.reap == "quiesced"
        assert outcome.signalled_after_reap is False
        assert process.poll() is not None
    finally:
        if process.poll() is None:  # pragma: no cover  # only on a reaper regression
            process.kill()
        process.wait(timeout=10)


def test_reap_process_tree_rejects_a_non_positive_bound() -> None:
    handle: Any = _AlreadyReaped(os.getpid())
    with pytest.raises(ValueError, match="timeout_s"):
        reap_process_tree(handle, timeout_s=0)


__all__: list[str] = []
