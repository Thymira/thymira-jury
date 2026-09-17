"""Unit tests for the Docker integration prerequisite probe."""

from __future__ import annotations

import subprocess

from tests.thymira import docker_support


def test_docker_daemon_error_reports_missing_cli(monkeypatch) -> None:
    """The probe reports a missing Docker executable."""
    monkeypatch.setattr(docker_support.shutil, "which", lambda _name: None)

    assert docker_support.docker_daemon_error() == "Docker CLI is not installed."


def test_docker_daemon_error_reports_unreachable_daemon(monkeypatch) -> None:
    """The probe reports the daemon's stderr when the CLI cannot connect."""
    monkeypatch.setattr(docker_support.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_support.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["docker", "info"], returncode=1, stdout="", stderr="daemon unavailable\n"
        ),
    )

    assert docker_support.docker_daemon_error() == (
        "Docker daemon is unavailable (daemon unavailable)."
    )


def test_docker_daemon_error_accepts_ready_daemon(monkeypatch) -> None:
    """The probe returns no error when Docker info succeeds."""
    monkeypatch.setattr(docker_support.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_support.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["docker", "info"], returncode=0, stdout="Server Version: 28\n", stderr=""
        ),
    )

    assert docker_support.docker_daemon_error() is None


def test_docker_daemon_error_reports_probe_timeout(monkeypatch) -> None:
    """The probe reports a timeout instead of failing the test setup."""
    monkeypatch.setattr(docker_support.shutil, "which", lambda _name: "/usr/bin/docker")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["docker", "info"], timeout=5)

    monkeypatch.setattr(docker_support.subprocess, "run", raise_timeout)

    assert docker_support.docker_daemon_error() == (
        "Docker daemon did not respond within 5 seconds."
    )
