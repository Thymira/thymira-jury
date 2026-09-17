"""Bounded Docker protocol helpers for the workspace quota backend.

This module is the imperative Docker edge of the quota backend.  It owns argv construction,
bounded daemon calls, identity inspection, and exact cleanup; tree validation and publication stay
in the sibling modules so the quota state machine can be tested against small seams.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.tools.sandbox.process_capture import OutputLimitExceeded, run_bounded_process

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


VOLUME_RE = re.compile(r"^thymira-quota-[0-9a-f]{32}$")
HELPER_RE = re.compile(r"^thymira-quota-helper-[0-9a-f]{32}$")
MAX_PROTOCOL_BYTES = 128 * 1024
CLEANUP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class VolumeFacts:
    """Daemon facts required before a volume can be mounted by a worker."""

    name: str
    inspected: bool
    options: str


def parse_helper_protocol(raw: str) -> dict[str, Any] | None:
    """Parse one bounded helper response and reject trailing protocol data."""
    if len(raw.encode(errors="replace")) > MAX_PROTOCOL_BYTES:
        return None
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def run_docker_command(
    command: list[str], *, cwd: Path, timeout_s: float, limit: int
) -> subprocess.CompletedProcess[str]:
    """Run a Docker client operation under a bounded output and time budget."""
    return run_bounded_process(command, cwd=cwd, timeout_s=timeout_s, output_limit_bytes=limit)


def inspect_quota_volume(
    docker: str,
    name: str,
    *,
    cwd: Path,
    timeout_s: float,
    expected_options: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_docker_command,
) -> VolumeFacts | None:
    """Require local driver, exact generated identity and the requested tmpfs options."""
    try:
        result = runner(
            [docker, "volume", "inspect", "--format", "{{json .}}", name],
            cwd=cwd,
            timeout_s=timeout_s,
            limit=MAX_PROTOCOL_BYTES,
        )
    except (OSError, OutputLimitExceeded, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        document = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(document, list):
        document = document[0] if len(document) == 1 else None
    if not isinstance(document, dict):
        return None
    labels = document.get("Labels")
    options = document.get("Options")
    if (
        document.get("Name") != name
        or document.get("Driver") != "local"
        or not isinstance(labels, dict)
        or labels.get("thymira.execution") != name
        or not isinstance(options, dict)
        or options.get("type") != "tmpfs"
        or options.get("device") != "tmpfs"
        or options.get("o") != expected_options
    ):
        return None
    return VolumeFacts(name=name, inspected=True, options=expected_options)


def build_container_command(
    *,
    name: str,
    image: str,
    memory: str,
    cpus: str,
    pids_limit: int,
    runtime: str | None,
    network: str = "none",
    mounts: list[str],
    user: tuple[int, int],
    env: Mapping[str, str] | None = None,
    argv: list[str],
) -> list[str]:
    """Build fixed helper/worker Docker security controls."""
    command = [
        "docker",
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--entrypoint",
        "",
        "--log-driver",
        "none",
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108  # container-private scratch mount
        "--user",
        f"{user[0]}:{user[1]}",
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--cpus",
        cpus,
        "--pids-limit",
        str(pids_limit),
    ]
    for mount in mounts:
        command.extend(["--volume", mount])
    if runtime is not None:
        command.extend(["--runtime", runtime])
    if env is not None:
        for key, value in env.items():
            command.extend(["--env", f"{key}={value}"])
    command.extend(["--workdir", "/workspace", image, *argv])
    return command


def inspect_worker_mount(  # noqa: PLR0911  # each malformed daemon fact fails closed
    docker: str,
    name: str,
    volume: str,
    *,
    writable: bool,
    cwd: Path,
    timeout_s: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_docker_command,
) -> bool:
    """Read back the worker mount identity and reject any writable host source."""
    try:
        result = runner(
            [docker, "inspect", "--format", "{{json .}}", name],
            cwd=cwd,
            timeout_s=timeout_s,
            limit=MAX_PROTOCOL_BYTES,
        )
    except (OSError, OutputLimitExceeded, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        document = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    mounts = document.get("Mounts")
    if not isinstance(mounts, list) or not all(isinstance(mount, dict) for mount in mounts):
        return False
    workspace_mounts = [mount for mount in mounts if mount.get("Destination") == "/workspace"]
    if len(workspace_mounts) != 1:
        return False
    mount = workspace_mounts[0]
    return (
        mount.get("Type") == "volume"
        and mount.get("Name") == volume
        and mount.get("Source")
        and mount.get("RW") is writable
        and all(other.get("Type") != "bind" for other in mounts)
    )


def remove_generated_container(
    docker: str,
    name: str,
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_docker_command,
) -> tuple[bool, str]:
    """Force-remove one exact generated container identity."""
    if not HELPER_RE.fullmatch(name) and not name.startswith("thymira-sandbox-"):
        return False, "refusing to remove an unowned container identity"
    try:
        result = runner(
            [docker, "rm", "--force", name],
            cwd=cwd,
            timeout_s=CLEANUP_SECONDS,
            limit=MAX_PROTOCOL_BYTES,
        )
    except (OSError, OutputLimitExceeded, subprocess.TimeoutExpired) as exc:
        return False, f"container cleanup failed: {type(exc).__name__}"
    return result.returncode == 0, result.stderr or "container cleanup failed"


def cleanup_quota_resources(
    docker: str,
    helper: str,
    volume: str,
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_docker_command,
    remove_container: Callable[..., tuple[bool, str]] = remove_generated_container,
) -> tuple[bool, bool, str]:
    """Remove exact helper and volume identities under separate bounded commands."""
    errors: list[str] = []
    helper_removed = False
    volume_removed = False
    if HELPER_RE.fullmatch(helper):
        helper_removed, helper_error = remove_container(docker, helper, cwd=cwd, runner=runner)
        if not helper_removed:
            errors.append(helper_error)
    if VOLUME_RE.fullmatch(volume):
        try:
            result = runner(
                [docker, "volume", "rm", volume],
                cwd=cwd,
                timeout_s=CLEANUP_SECONDS,
                limit=MAX_PROTOCOL_BYTES,
            )
            volume_removed = result.returncode == 0
            if not volume_removed:
                errors.append(result.stderr or "volume cleanup failed")
        except (OSError, OutputLimitExceeded, subprocess.TimeoutExpired) as exc:
            errors.append(f"volume cleanup failed: {type(exc).__name__}")
    return helper_removed, volume_removed, "\n".join(errors)


__all__ = [
    "CLEANUP_SECONDS",
    "HELPER_RE",
    "MAX_PROTOCOL_BYTES",
    "VOLUME_RE",
    "VolumeFacts",
    "build_container_command",
    "cleanup_quota_resources",
    "inspect_quota_volume",
    "inspect_worker_mount",
    "parse_helper_protocol",
    "remove_generated_container",
    "run_docker_command",
]
