"""Local development execution without filesystem confinement."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox.environment import (
    ScrubbedEnvironment,
    apply_runtime_owned,
    scrub_environment,
)
from thymira.tools.sandbox.private_storage import PrivateStorage, private_execution_storage
from thymira.tools.sandbox.process_capture import (
    DEFAULT_OUTPUT_LIMIT_BYTES,
    OutputLimitExceeded,
    run_bounded_process,
)
from thymira.tools.sandbox.termination import (
    CONTROL_CHANNEL_NONE,
    CONTROL_CHANNEL_OS_WAIT,
    OUTCOME_COMPLETED,
    OUTCOME_OUTPUT_EXCEEDED,
    OUTCOME_TIMED_OUT,
    OUTCOME_UNAVAILABLE,
    REAP_UNBOUNDED,
    ProcessObservation,
    TerminationEvidence,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
from thymira.tools.sandbox.base import ResolvedExecutionSpec, SandboxRun, StagedInput
from thymira.tools.sandbox.staged_inputs import stage_inputs_on_host

_UNENFORCED = ("cpu", "filesystem", "memory", "network", "pids", "rlimits", "workspace_quota")

_MEMORY_RE = re.compile(r"^([0-9]+)([bkmgKMG]?)$")
_CPUS_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_MAX_NUMERIC_TEXT = 128
_CPUS_FRACTION_DIGITS = 9

_REFUSED = TerminationEvidence(
    outcome=OUTCOME_UNAVAILABLE,
    exit_source="runtime_refusal",
    control_channel=CONTROL_CHANNEL_NONE,
    control_channel_validated=False,
)
"""No child ran, so no channel carried an exit marker: the refusal is its own recorded outcome."""


def _observed(observation: ProcessObservation, outcome: str) -> TerminationEvidence:
    """Publish what the parent's own wait status and clock observed about this child.

    The control channel is the OS itself: the exit code comes from waiting on a process this
    backend started, never from a marker parsed out of the child's stdout, which is the one
    stream the model reads and the child fully controls.
    """
    return TerminationEvidence.from_observation(
        observation,
        outcome=outcome,
        exit_source=CONTROL_CHANNEL_OS_WAIT,
        control_channel=CONTROL_CHANNEL_OS_WAIT,
        control_channel_validated=observation.child_exit_code is not None,
    )


def _text(value: str | bytes | None) -> str:
    """Normalize bounded process output for a sandbox result."""
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


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


def _spec(
    root: Path,
    scrubbed: ScrubbedEnvironment,
    *,
    memory: str | None,
    cpus: str | None,
    pids_limit: int | None,
    output_limit_bytes: int,
    workspace_quota_bytes: int | None,
) -> ResolvedExecutionSpec:
    """Record what this backend actually applied, including the names it owns itself."""
    return ResolvedExecutionSpec(
        backend="local_subprocess",
        image=None,
        workspace_mount=str(root),
        network="host",
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        environment_names=tuple(sorted(scrubbed.values)),
        excluded_environment_names=scrubbed.excluded,
        unenforced=_UNENFORCED,
        output_limit_bytes=output_limit_bytes,
        workspace_quota_bytes=workspace_quota_bytes,
    )


def _with_cleanup(run: SandboxRun, storage: PrivateStorage) -> SandboxRun:
    """Report cleanup as what was observed: the private storage gone and the tree quiet.

    Kept separate from the exit outcome (F6.6): a directory that could not be removed, or a reap
    that never reached quiescence, never rewrites what the execution itself reported.
    """
    quiesced = run.termination is None or run.termination.reap != REAP_UNBOUNDED
    return replace(run, cleanup_confirmed=storage.removed and quiesced)


class LocalSubprocessSandbox:
    """Refuse confined modes and run explicit unconfined commands with a timeout."""

    def __init__(
        self,
        *,
        memory: str | None = None,
        cpus: str | None = None,
        pids_limit: int | None = None,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
        workspace_quota_bytes: int | None = None,
    ) -> None:
        """Configure the maximum captured output for one local child."""
        if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int):
            raise TypeError("output_limit_bytes must be an integer")
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if memory is not None and not _positive_memory(memory):
            raise ValueError(f"sandbox memory is not a positive argv-safe value: {memory!r}")
        if cpus is not None and not _positive_cpus(cpus):
            raise ValueError(f"sandbox cpus is not a positive argv-safe value: {cpus!r}")
        if pids_limit is not None and (isinstance(pids_limit, bool) or pids_limit <= 0):
            raise ValueError("pids_limit must be positive")
        if workspace_quota_bytes is not None and (
            isinstance(workspace_quota_bytes, bool) or workspace_quota_bytes <= 0
        ):
            raise ValueError("workspace_quota_bytes must be positive")
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
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
        """Run an explicit argv in ``workspace`` without shell expansion."""
        if not argv:
            raise ValueError("sandbox argv must not be empty")
        if timeout_s <= 0:
            raise ValueError("sandbox timeout_s must be positive")
        root = Path(workspace).resolve()
        if not root.is_dir():
            raise ValueError(f"sandbox workspace does not exist: {workspace}")
        if not isinstance(mode, SandboxMode):
            raise TypeError("sandbox mode must be a SandboxMode")
        scrubbed = scrub_environment(env)
        spec = _spec(
            root,
            scrubbed,
            memory=self.memory,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            output_limit_bytes=self.output_limit_bytes,
            workspace_quota_bytes=self.workspace_quota_bytes,
        )
        if self.workspace_quota_bytes is not None:
            return SandboxRun(
                stdout="",
                stderr="local cannot enforce a workspace quota",
                exit_code=125,
                mode=mode,
                enforcement=SandboxEnforcement.UNUSABLE,
                spec=spec,
                termination=_REFUSED,
            )
        if mode is not SandboxMode.DANGER_FULL_ACCESS:
            return SandboxRun(
                stdout="",
                stderr="local cannot enforce requested mode",
                exit_code=125,
                mode=mode,
                enforcement=SandboxEnforcement.UNUSABLE,
                spec=spec,
                termination=_REFUSED,
            )
        with private_execution_storage() as storage:
            # Applied after the scrub, so a caller's TMPDIR/TEMP/TMP is displaced by this
            # execution's own private directory rather than steering where the child writes.
            prepared = apply_runtime_owned(scrubbed, storage.environment())
            with stage_inputs_on_host(root, staged_inputs):
                run = self._execute(
                    argv, root=root, mode=mode, timeout_s=timeout_s, scrubbed=prepared
                )
        return _with_cleanup(run, storage)

    def _execute(
        self,
        argv: list[str],
        *,
        root: Path,
        mode: SandboxMode,
        timeout_s: float,
        scrubbed: ScrubbedEnvironment,
    ) -> SandboxRun:
        """Run one already-authorised argv and translate its bounded outcome."""
        spec = _spec(
            root,
            scrubbed,
            memory=self.memory,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            output_limit_bytes=self.output_limit_bytes,
            workspace_quota_bytes=self.workspace_quota_bytes,
        )
        clean_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        clean_env.update(scrubbed.values)
        # Preserve a virtual environment's bin directory: ``resolve`` would follow its Python
        # symlink to the base interpreter on POSIX. Applied last so a caller value (already
        # scrubbed above, since PATH is bootstrap-shaped) can never supersede it.
        interpreter_dir = str(Path(sys.executable).absolute().parent)
        existing_path = clean_env.get("PATH", "")
        clean_env["PATH"] = (
            f"{interpreter_dir}{os.pathsep}{existing_path}" if existing_path else interpreter_dir
        )
        # Forced, not inherited, and applied last so a caller value can never supersede it: the
        # child's own stdio must be UTF-8 regardless of the host console codepage, or a script
        # that prints a plain "café"/"≤" crashes the child with `UnicodeEncodeError` on Windows
        # before this tool ever sees a decode problem -- `process_capture._decode` already reads
        # the captured bytes as UTF-8, but that cannot help a child whose own `print` already
        # raised.
        clean_env["PYTHONIOENCODING"] = "utf-8"
        clean_env["PYTHONUTF8"] = "1"
        executable = sys.executable if argv[0] == "python" else None
        observation = ProcessObservation()
        try:
            completed = run_bounded_process(
                argv,
                cwd=root,
                timeout_s=timeout_s,
                output_limit_bytes=self.output_limit_bytes,
                env=clean_env,
                executable=executable,
                observation=observation,
            )
        except OutputLimitExceeded as exc:
            return SandboxRun(
                stdout=exc.stdout,
                stderr=f"{exc.stderr}\nsandbox output exceeded {exc.limit_bytes} bytes",
                exit_code=125,
                mode=mode,
                enforcement=SandboxEnforcement.PARTIAL,
                spec=spec,
                termination=_observed(observation, OUTCOME_OUTPUT_EXCEEDED),
            )
        except subprocess.TimeoutExpired as exc:
            # 124 is the runtime's own marker for "the deadline decided this", and it is reported
            # whatever the child's own exit code was: ``termination.child_exit_code`` keeps that
            # as a separate fact, so a child that exits zero past its deadline is recorded as a
            # timeout and never as a clean success (F6.8).
            return SandboxRun(
                stdout=_text(exc.stdout),
                stderr=f"{_text(exc.stderr)}\nsandbox timeout after {timeout_s}s",
                exit_code=124,
                mode=mode,
                enforcement=SandboxEnforcement.PARTIAL,
                spec=spec,
                termination=_observed(observation, OUTCOME_TIMED_OUT),
                timed_out=True,
            )
        except OSError as exc:
            return SandboxRun(
                stdout="",
                stderr=f"sandbox process unavailable: {type(exc).__name__}",
                exit_code=125,
                mode=mode,
                enforcement=SandboxEnforcement.UNUSABLE,
                spec=spec,
                termination=_observed(observation, OUTCOME_UNAVAILABLE),
            )
        return SandboxRun(
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            mode=mode,
            enforcement=SandboxEnforcement.PARTIAL,
            spec=spec,
            termination=_observed(observation, OUTCOME_COMPLETED),
        )


__all__ = ["LocalSubprocessSandbox"]
