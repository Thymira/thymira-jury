"""Shared Docker daemon prerequisite checks for integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

_DOCKER_PROBE_TIMEOUT_S = 5.0
_QUOTA_IMAGE_ENV = "THYMIRA_QUOTA_TEST_IMAGE"
# Built from commit 93f8c70b with `docker build --pull=false -t thymira:quota-93f8c70 .`;
# Docker Server 29.7.2 reported image ID
# sha256:edacf5731006ef875a25f37e0d395b1d05c780e5175a2491c12f0311b4ad0d1f.
_DEFAULT_QUOTA_IMAGE = "thymira:quota-93f8c70"


def docker_daemon_error() -> str | None:
    """Return a diagnostic when Docker cannot serve commands, or ``None`` when ready."""
    docker = shutil.which("docker")
    if docker is None:
        return "Docker CLI is not installed."
    try:
        result = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Docker daemon did not respond within 5 seconds."
    except OSError as exc:
        return f"Docker daemon is unavailable: {exc}"
    if result.returncode == 0:
        return None
    detail = result.stderr.strip().splitlines()
    suffix = f" ({detail[0]})" if detail else ""
    return f"Docker daemon is unavailable{suffix}."


def require_docker_daemon() -> None:
    """Skip a Docker integration test when the CLI or daemon is unavailable."""
    reason = docker_daemon_error()
    if reason is not None:
        pytest.skip(reason)


def require_sandbox_image() -> None:
    """Skip absent Docker/image prerequisites, without concealing execution failures."""
    require_docker_daemon()
    result = subprocess.run(
        ["docker", "image", "ls", "--filter", "reference=thymira:dev", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        timeout=_DOCKER_PROBE_TIMEOUT_S,
        check=True,
    )
    if not result.stdout.strip():
        pytest.skip("ContainerSandbox needs the local thymira:dev image; run just docker-build")


def require_quota_sandbox_image() -> str:
    """Return the explicitly configured quota image, skipping only when it is absent.

    ``THYMIRA_QUOTA_TEST_IMAGE`` lets a reviewer select a rebuilt image without changing the
    checked-in test contract. The default is the recorded repair image above; rebuilding it is
    reproducible with the command in the module constant's comment.
    """
    require_docker_daemon()
    image = os.environ.get(_QUOTA_IMAGE_ENV, _DEFAULT_QUOTA_IMAGE).strip()
    if not image:
        pytest.skip(f"{_QUOTA_IMAGE_ENV} must name a Docker image")
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=_DOCKER_PROBE_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Quota integration tests need image {image!r}; build it with "
            f"`docker build --pull=false -t {image} .` or set {_QUOTA_IMAGE_ENV}"
        )
    return image


__all__ = [
    "docker_daemon_error",
    "require_docker_daemon",
    "require_quota_sandbox_image",
    "require_sandbox_image",
]
