"""Cleanup and exit outcome stay two separate facts on a container run (F6.6)."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from thymira.tools.sandbox import container_lifecycle as lifecycle_module
from thymira.tools.sandbox.container_lifecycle import run_container_lifecycle

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_STATE = json.dumps(
    {
        "State": {
            "Status": "exited",
            "Running": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Error": "",
            "StartedAt": "2024-01-01T00:00:00Z",
            "FinishedAt": "2024-01-01T00:00:01Z",
        },
        "HostConfig": {
            "NetworkMode": "none",
            "Memory": 1073741824,
            "PidsLimit": 128,
            "ReadonlyRootfs": True,
        },
        "Mounts": [{"Destination": "/workspace", "RW": True}],
    }
)
"""The whole ``docker inspect`` document: the runtime asks for ``{{json .}}``, not ``.State``."""


def _completed(
    argv: list[str], *, returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _fake_run_bounded_process(cleanup_returncode: int, cleanup_stderr: str):
    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "create" in argv:
            return _completed(argv, returncode=0)
        if "start" in argv:
            return _completed(argv, returncode=0, stdout="child output")
        if "inspect" in argv:
            return _completed(argv, returncode=0, stdout=_STATE)
        if "rm" in argv:
            return _completed(argv, returncode=cleanup_returncode, stderr=cleanup_stderr)
        raise AssertionError(f"unexpected docker subcommand: {argv}")

    return _run


def test_failed_cleanup_preserves_the_child_exit_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "run_bounded_process",
        _fake_run_bounded_process(1, "container busy"),
    )

    outcome = run_container_lifecycle(
        "docker",
        ["docker", "create", "thymira:dev"],
        name="thymira-sandbox-test",
        cwd=tmp_path,
        timeout_s=10,
        output_limit_bytes=1_000_000,
    )

    assert outcome.exit_code == 0
    assert outcome.child_confirmed is True
    assert outcome.stdout == "child output"


def test_failed_cleanup_is_recorded_as_a_separate_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "run_bounded_process",
        _fake_run_bounded_process(1, "container busy"),
    )

    outcome = run_container_lifecycle(
        "docker",
        ["docker", "create", "thymira:dev"],
        name="thymira-sandbox-test",
        cwd=tmp_path,
        timeout_s=10,
        output_limit_bytes=1_000_000,
    )

    assert outcome.cleanup_confirmed is False
    assert "container cleanup failed" in outcome.stderr
    assert outcome.exit_code == 0


def test_confirmed_cleanup_is_recorded_on_a_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "run_bounded_process",
        _fake_run_bounded_process(0, ""),
    )

    outcome = run_container_lifecycle(
        "docker",
        ["docker", "create", "thymira:dev"],
        name="thymira-sandbox-test",
        cwd=tmp_path,
        timeout_s=10,
        output_limit_bytes=1_000_000,
    )

    assert outcome.cleanup_confirmed is True
    assert outcome.exit_code == 0
    assert outcome.child_confirmed is True


def test_lifecycle_timeout_is_recorded_separately_from_child_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Docker client deadline is explicit evidence rather than an exit-code convention."""

    def _timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("docker", 1)

    monkeypatch.setattr(lifecycle_module, "run_bounded_process", _timeout)

    outcome = run_container_lifecycle(
        "docker",
        ["docker", "create", "thymira:dev"],
        name="thymira-sandbox-timeout-test",
        cwd=tmp_path,
        timeout_s=10,
        output_limit_bytes=1_000_000,
    )

    assert outcome.exit_code == 125
    assert outcome.child_confirmed is False
    assert outcome.timed_out is True
