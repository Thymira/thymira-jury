"""Container-backed sandbox for environments where Docker is available."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox.base import ResolvedExecutionSpec, SandboxRun, StagedInput
from thymira.tools.sandbox.container_lifecycle import run_container_lifecycle
from thymira.tools.sandbox.environment import apply_runtime_owned, scrub_environment
from thymira.tools.sandbox.private_storage import container_private_storage
from thymira.tools.sandbox.process_capture import DEFAULT_OUTPUT_LIMIT_BYTES
from thymira.tools.sandbox.quota_workspace import run_quota_workspace
from thymira.tools.sandbox.staged_inputs import stage_inputs_on_host
from thymira.tools.sandbox.termination import (
    CONTROL_CHANNEL_NONE,
    OUTCOME_UNAVAILABLE,
    TerminationEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Argv-safety for values that may originate from an environment variable at the composition
# root (``THYMIRA_SANDBOX_IMAGE``/``_MEMORY``/``_CPUS``): a value must look like the docker
# argument it claims to be, so a hostile or malformed setting can never be read as a flag by
# the ``docker create`` invocation these values feed.
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_MEMORY_RE = re.compile(r"^([0-9]+)([bkmgKMG]?)$")
_CPUS_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_MAX_NUMERIC_TEXT = 128
_CPUS_FRACTION_DIGITS = 9

_NO_RUNTIME = TerminationEvidence(
    outcome=OUTCOME_UNAVAILABLE,
    exit_source="runtime_refusal",
    control_channel=CONTROL_CHANNEL_NONE,
    control_channel_validated=False,
)
"""No container was created, so the daemon carried no exit marker for this call."""


def _positive_memory(value: str) -> bool:
    """Return whether a Docker-style memory value is bounded and greater than zero."""
    if len(value) > _MAX_NUMERIC_TEXT:
        return False
    match = _MEMORY_RE.fullmatch(value)
    return match is not None and any(character != "0" for character in match.group(1))


def _positive_cpus(value: str) -> bool:
    """Return whether a decimal CPU value is bounded and representable as nanocpus."""
    if len(value) > _MAX_NUMERIC_TEXT or _CPUS_RE.fullmatch(value) is None:
        return False
    whole, _, fraction = value.partition(".")
    if len(fraction) > _CPUS_FRACTION_DIGITS:
        return False
    return any(character != "0" for character in whole + fraction)


def _host_user_args() -> list[str]:
    """Return a non-root uid/gid, preserving a non-root POSIX host identity."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return ["--user", "999:999"]
    uid, gid = getuid(), getgid()
    if uid <= 0 or gid <= 0:
        return ["--user", "999:999"]
    return ["--user", f"{uid}:{gid}"]


class ContainerSandbox:
    """Run argv in a hardened Docker container and report observed enforcement."""

    def __init__(
        self,
        *,
        image: str = "thymira:dev",
        memory: str = "1g",
        cpus: str = "1.0",
        pids_limit: int = 128,
        seccomp_profile: str = "default",
        runtime: str | None = None,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
        workspace_quota_bytes: int | None = None,
    ) -> None:
        if seccomp_profile == "unconfined":
            raise ValueError("seccomp profile must not be unconfined")
        if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int):
            raise TypeError("output_limit_bytes must be an integer")
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if not _IMAGE_RE.fullmatch(image):
            raise ValueError(f"sandbox image is not argv-safe: {image!r}")
        if not _positive_memory(memory):
            raise ValueError(f"sandbox memory is not argv-safe: {memory!r}")
        if not _positive_cpus(cpus):
            raise ValueError(f"sandbox cpus is not argv-safe: {cpus!r}")
        if isinstance(pids_limit, bool) or not isinstance(pids_limit, int):
            raise TypeError("pids_limit must be an integer")
        if pids_limit <= 0:
            raise ValueError("pids_limit must be positive")
        if workspace_quota_bytes is not None and (
            isinstance(workspace_quota_bytes, bool) or workspace_quota_bytes <= 0
        ):
            raise ValueError("workspace_quota_bytes must be positive")
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.seccomp_profile = seccomp_profile
        self.runtime = runtime
        self.output_limit_bytes = output_limit_bytes
        self.workspace_quota_bytes = workspace_quota_bytes

    def run(
        self,
        argv: list[str],
        *,
        workspace: Path,
        mode: SandboxMode,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
    ) -> SandboxRun:
        """Execute argv in Docker or return UNUSABLE when execution cannot be confirmed."""
        if not argv:
            raise ValueError("sandbox argv must not be empty")
        if timeout_s <= 0:
            raise ValueError("sandbox timeout_s must be positive")
        if not isinstance(mode, SandboxMode):
            raise TypeError("sandbox mode must be a SandboxMode")
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"sandbox workspace does not exist: {workspace}")

        name = f"thymira-sandbox-{uuid4().hex}"
        # Resolved before the docker probe below: even the docker-missing refusal records what
        # this backend's own configuration would have applied.
        command, spec = self._resolve_execution(name, root, mode, env, argv)
        docker = shutil.which("docker")
        if docker is None:
            return SandboxRun(
                stdout="",
                stderr="container runtime unavailable: docker was not found",
                exit_code=125,
                mode=mode,
                enforcement=SandboxEnforcement.UNUSABLE,
                spec=spec,
                cleanup_confirmed=None,
                termination=_NO_RUNTIME,
                timed_out=False,
            )
        if self.workspace_quota_bytes is not None:
            return run_quota_workspace(
                docker,
                image=self.image,
                workspace=root,
                argv=argv,
                mode=mode,
                timeout_s=timeout_s,
                env=env,
                memory=self.memory,
                cpus=self.cpus,
                pids_limit=self.pids_limit,
                runtime=self.runtime,
                requested_bytes=self.workspace_quota_bytes,
                output_limit_bytes=self.output_limit_bytes,
                staged_inputs=staged_inputs,
            )
        command[0] = docker
        with stage_inputs_on_host(root, staged_inputs):
            outcome = run_container_lifecycle(
                docker,
                command,
                name=name,
                cwd=root,
                timeout_s=timeout_s,
                output_limit_bytes=self.output_limit_bytes,
            )
        return SandboxRun(
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            exit_code=outcome.exit_code,
            mode=mode,
            enforcement=(
                SandboxEnforcement.PARTIAL
                if outcome.child_confirmed
                else SandboxEnforcement.UNUSABLE
            ),
            spec=spec,
            cleanup_confirmed=outcome.cleanup_confirmed,
            termination=outcome.termination,
            timed_out=outcome.timed_out,
        )

    def _resolve_execution(
        self,
        name: str,
        root: Path,
        mode: SandboxMode,
        env: Mapping[str, str] | None,
        argv: list[str],
    ) -> tuple[list[str], ResolvedExecutionSpec]:
        """Build the hardened Docker create command and the spec it commits this call to.

        Computed together so the argv actually run and the spec recorded as evidence can never
        diverge. ``command[0]`` is a placeholder (replaced with the resolved ``docker`` path once
        the caller has confirmed it exists); every other element -- and every field of the
        returned spec -- comes from this backend's own configuration, never from the caller.
        """
        mount_mode = "ro" if mode is SandboxMode.READ_ONLY else "rw"
        network_mode = "bridge" if mode is SandboxMode.DANGER_FULL_ACCESS else "none"
        mount_source = root.as_posix() if os.name == "nt" else str(root)
        workspace_mount = f"{mount_source}:/workspace:{mount_mode}"
        # Runtime-owned last, so a caller cannot point the child's temporary storage anywhere
        # but the container's own per-run tmpfs (F6.2/F6.8).
        scrubbed = apply_runtime_owned(scrub_environment(env), container_private_storage())
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
            network_mode,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            *self._seccomp_args(),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108  # container-owned mount
            *_host_user_args(),
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--volume",
            workspace_mount,
            "--workdir",
            "/workspace",
        ]
        if self.runtime is not None:
            command.extend(["--runtime", self.runtime])
        for key, value in scrubbed.values.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend([self.image, *argv])
        spec = ResolvedExecutionSpec(
            backend="container",
            image=self.image,
            workspace_mount=workspace_mount,
            network=network_mode,
            memory=self.memory,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            environment_names=tuple(sorted(scrubbed.values)),
            excluded_environment_names=scrubbed.excluded,
            unenforced=("rlimits", "workspace_quota"),
            output_limit_bytes=self.output_limit_bytes,
            workspace_quota_bytes=self.workspace_quota_bytes,
        )
        return command, spec

    def _seccomp_args(self) -> list[str]:
        """Return a custom seccomp profile or preserve Docker's built-in default."""
        if not self.seccomp_profile or self.seccomp_profile == "default":
            return []
        return ["--security-opt", f"seccomp={self.seccomp_profile}"]


__all__ = ["ContainerSandbox"]
