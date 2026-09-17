"""Bounded subprocess capture tests."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.tools.sandbox.process_capture import OutputLimitExceeded, run_bounded_process

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def test_bounded_process_preserves_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    completed = run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        timeout_s=5,
    )

    assert completed.returncode == 7
    assert completed.stdout == "out\n"
    assert completed.stderr == "err\n"


def test_bounded_process_limits_combined_raw_output(tmp_path: Path) -> None:
    with pytest.raises(OutputLimitExceeded) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'a' * 6000); os.write(2, b'b' * 6000)",
            ],
            cwd=tmp_path,
            timeout_s=5,
            output_limit_bytes=8_000,
        )

    error = raised.value
    assert len(error.stdout.encode()) + len(error.stderr.encode()) == 8_000
    assert error.limit_bytes == 8_000


def test_bounded_process_timeout_carries_only_bounded_output(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import os,time; os.write(1, b'ready\\r\\n'); time.sleep(60)",
            ],
            cwd=tmp_path,
            timeout_s=1.0,
            output_limit_bytes=100,
        )

    assert raised.value.stdout == "ready\n"
    assert raised.value.stderr == ""


def test_bounded_process_keeps_deadline_after_child_closes_both_pipes(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(60)"],
            cwd=tmp_path,
            timeout_s=0.2,
            output_limit_bytes=100,
        )


def test_bounded_process_allows_completion_after_child_closes_both_pipes(tmp_path: Path) -> None:
    completed = run_bounded_process(
        [
            sys.executable,
            "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(0.1); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        timeout_s=2,
    )

    assert completed.returncode == 7


def test_bounded_process_cleans_up_when_nonblocking_pipe_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_setup(_fd: int, _blocking: bool) -> None:
        raise OSError("set-blocking failed")

    monkeypatch.setattr("thymira.tools.sandbox.process_capture.os.set_blocking", fail_setup)
    started = time.monotonic()

    with pytest.raises(OSError, match="set-blocking failed"):
        run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            timeout_s=5,
        )

    assert time.monotonic() - started < 3


def test_bounded_process_cleans_up_when_pipe_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_read(_fd: int, _size: int) -> bytes:
        raise OSError("pipe read failed")

    monkeypatch.setattr("thymira.tools.sandbox.process_capture.os.read", fail_read)
    started = time.monotonic()

    with pytest.raises(OSError, match="pipe read failed"):
        run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            timeout_s=5,
        )

    assert time.monotonic() - started < 3


def test_bounded_process_never_reports_success_without_a_confirmed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "thymira.tools.sandbox.process_capture._wait_bounded",
        lambda _process, _observation: None,
    )

    with pytest.raises(OSError, match="exit could not be confirmed"):
        run_bounded_process(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            timeout_s=5,
        )


@pytest.mark.parametrize(
    ("command", "timeout_s", "limit", "message"),
    [([], 1.0, 1, "command"), (["tool"], 0.0, 1, "timeout"), (["tool"], 1.0, 0, "limit")],
)
def test_bounded_process_rejects_invalid_boundaries(
    tmp_path: Path, command: list[str], timeout_s: float, limit: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run_bounded_process(command, cwd=tmp_path, timeout_s=timeout_s, output_limit_bytes=limit)


@pytest.mark.parametrize(
    ("timeout_s", "limit", "error"),
    [(float("nan"), 1, ValueError), (1.0, True, TypeError), (1.0, 1.5, TypeError)],
)
def test_bounded_process_rejects_non_finite_or_non_integer_limits(
    tmp_path: Path, timeout_s: float, limit: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        run_bounded_process(
            ["unreachable"],
            cwd=tmp_path,
            timeout_s=timeout_s,
            output_limit_bytes=cast("Any", limit),
        )


def test_bounded_process_replaces_invalid_bytes_without_exceeding_raw_budget(
    tmp_path: Path,
) -> None:
    completed = run_bounded_process(
        [sys.executable, "-c", "import os; os.write(1, bytes([255]) * 4)"],
        cwd=tmp_path,
        timeout_s=5,
        output_limit_bytes=4,
    )

    assert completed.stdout == "\ufffd" * 4
