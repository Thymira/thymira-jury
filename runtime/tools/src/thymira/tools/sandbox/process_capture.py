"""Cross-platform subprocess execution with a combined in-memory output budget.

Every run fills a :class:`~thymira.tools.sandbox.termination.ProcessObservation` the caller
supplies, on all three exit paths, so the deadline verdict and the reap outcome survive a
timeout and an overflow as evidence rather than being reconstructed from an exit code
afterwards.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO, cast

from thymira.tools.sandbox.reaper import reap_process_tree
from thymira.tools.sandbox.termination import REAP_UNBOUNDED, ProcessObservation

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

DEFAULT_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
"""Default combined byte ceiling for stdout and stderr."""

_READ_BYTES = 64 * 1024
_POLL_SECONDS = 0.005
_CLEANUP_SECONDS = 1.0


class OutputLimitExceeded(Exception):  # noqa: N818  # accepted public API names the condition
    """Raised after terminating a process whose combined output exceeded its budget."""

    def __init__(self, *, stdout: str, stderr: str, limit_bytes: int) -> None:
        super().__init__(f"subprocess output exceeded {limit_bytes} bytes")
        self.stdout = stdout
        self.stderr = stderr
        self.limit_bytes = limit_bytes


@dataclass(slots=True)
class _Capture:
    """Retain two streams under one combined raw-byte budget."""

    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    exceeded: bool = False

    def add(self, stream: str, chunk: bytes) -> None:
        """Retain only bytes inside the shared budget and record overflow."""
        remaining = self.limit - self.total
        kept = chunk[:remaining]
        if kept:
            target = self.stdout if stream == "stdout" else self.stderr
            target.extend(kept)
            self.total += len(kept)
        if len(chunk) > remaining:
            self.exceeded = True

    def text(self) -> tuple[str, str]:
        """Decode bounded bytes with universal-newline behavior."""
        return _decode(self.stdout), _decode(self.stderr)


def run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    observation: ProcessObservation | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> subprocess.CompletedProcess[str]:
    """Run an argv while bounding combined stdout and stderr before accumulation.

    POSIX children start in a new session so timeout and overflow terminate their process group.
    Windows nonblocking pipes keep capture memory and handles bounded, but termination covers the
    direct child only; full descendant confinement belongs to the sandbox backend.

    ``observation`` is filled on every exit path -- the clean return, the two bounded exceptions
    and an unexpected escape -- so a caller always holds the deadline and reap facts of the run
    it just made, whatever shape its outcome took. ``clock`` is injected so a test can reach the
    race where a child's exit is confirmed only after its deadline has already passed.
    """
    _validate_inputs(command, timeout_s, output_limit_bytes)
    notes = ProcessObservation() if observation is None else observation
    notes.deadline_s = float(timeout_s)
    started = clock()
    process = subprocess.Popen(  # noqa: S603  # explicit argv, never a shell
        command,
        cwd=cwd,
        env=env,
        executable=executable,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
        start_new_session=os.name == "posix",
    )
    notes.pid = process.pid
    pipes = {
        "stdout": cast("BinaryIO", process.stdout),
        "stderr": cast("BinaryIO", process.stderr),
    }
    try:
        for pipe in pipes.values():
            os.set_blocking(pipe.fileno(), False)
        return _capture_process(
            process,
            pipes=pipes,
            command=command,
            timeout_s=timeout_s,
            output_limit_bytes=output_limit_bytes,
            observation=notes,
            clock=clock,
            started=started,
        )
    except BaseException:
        if process.returncode is None:
            _terminate(process, notes)
            _wait_bounded(process, notes)
        raise
    finally:
        if notes.duration_s is None:
            _record_deadline(notes, clock() - started, timeout_s)
        notes.child_exit_code = process.returncode
        for pipe in pipes.values():
            pipe.close()


def _record_deadline(observation: ProcessObservation, elapsed: float, timeout_s: float) -> None:
    """Record how long the run took and whether its deadline had already passed.

    A deadline that passed makes the run a timeout even when the child's own exit code is zero:
    the verdict comes from the parent's clock, never from what the child reported (F6.8).
    """
    observation.duration_s = elapsed
    if elapsed > timeout_s:
        observation.deadline_exceeded = True
    observation.timed_out = observation.timed_out or observation.deadline_exceeded


def _validate_inputs(command: list[str], timeout_s: float, output_limit_bytes: int) -> None:
    if not command:
        raise ValueError("subprocess command must not be empty")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("subprocess timeout must be a number")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("subprocess timeout must be positive")
    if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int):
        raise TypeError("subprocess output limit must be an integer")
    if output_limit_bytes <= 0:
        raise ValueError("subprocess output limit must be positive")


def _capture_process(
    process: subprocess.Popen[bytes],
    *,
    pipes: dict[str, BinaryIO],
    command: list[str],
    timeout_s: float,
    output_limit_bytes: int,
    observation: ProcessObservation,
    clock: Callable[[], float],
    started: float,
) -> subprocess.CompletedProcess[str]:
    """Drain one started process and translate its bounded outcome."""
    capture = _Capture(output_limit_bytes)
    deadline = started + timeout_s
    timed_out = False
    while pipes or process.poll() is None:
        progressed = _drain_once(pipes, capture)
        if capture.exceeded:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            timed_out = True
            break
        if not progressed:
            time.sleep(min(remaining, _POLL_SECONDS))
    observation.timed_out = timed_out
    if capture.exceeded or timed_out:
        _terminate(process, observation)
    return_code = _wait_bounded(process, observation)
    observation.child_exit_code = return_code
    # The loop above can leave normally in the very iteration where the child closes both pipes
    # and exits, with the deadline already behind it -- nothing inside it re-reads the clock once
    # ``pipes`` is empty and ``poll()`` returns a status. Reading the clock here, after the exit
    # is confirmed, is what keeps a deadline that passed visible even over a zero exit (F6.8).
    _record_deadline(observation, clock() - started, timeout_s)
    if return_code is None:
        raise OSError("subprocess exit could not be confirmed")

    stdout, stderr = capture.text()
    if capture.exceeded:
        raise OutputLimitExceeded(stdout=stdout, stderr=stderr, limit_bytes=output_limit_bytes)
    if observation.timed_out:
        raise subprocess.TimeoutExpired("subprocess", timeout_s, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, return_code, stdout, stderr)


def _drain_once(pipes: dict[str, BinaryIO], capture: _Capture) -> bool:
    """Read at most one fixed-size chunk from each currently open pipe."""
    progressed = False
    for stream, pipe in tuple(pipes.items()):
        try:
            chunk = os.read(pipe.fileno(), _READ_BYTES)
        except BlockingIOError:
            continue
        if chunk:
            capture.add(stream, chunk)
            progressed = True
        else:
            pipe.close()
            del pipes[stream]
            progressed = True
    return progressed


def _terminate(process: subprocess.Popen[bytes], observation: ProcessObservation) -> None:
    """Reap the child's whole tree under a bound, and record how far the reap reached."""
    outcome = reap_process_tree(process, timeout_s=_CLEANUP_SECONDS)
    observation.reap = outcome.reap
    observation.tree_scope = outcome.tree_scope
    observation.reap_deadline_s = _CLEANUP_SECONDS
    observation.reap_duration_s = outcome.duration_s
    observation.signalled_after_reap = outcome.signalled_after_reap


def _wait_bounded(process: subprocess.Popen[bytes], observation: ProcessObservation) -> int | None:
    """Collect the child's exit status without extending cleanup indefinitely."""
    try:
        return process.wait(timeout=_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate(process, observation)
        try:
            return process.wait(timeout=_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            observation.reap = REAP_UNBOUNDED
            return None


def _decode(raw: bytes | bytearray) -> str:
    """Decode arbitrary child bytes deterministically and apply universal newlines."""
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


__all__ = [
    "DEFAULT_OUTPUT_LIMIT_BYTES",
    "OutputLimitExceeded",
    "ProcessObservation",
    "run_bounded_process",
]
