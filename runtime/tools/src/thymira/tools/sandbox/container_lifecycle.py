"""Private Docker create, start, inspect, and cleanup lifecycle."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from thymira.tools.sandbox.process_capture import OutputLimitExceeded, run_bounded_process
from thymira.tools.sandbox.termination import (
    CONTROL_CHANNEL_CONTAINER_STATE,
    OUTCOME_COMPLETED,
    OUTCOME_OUTPUT_EXCEEDED,
    OUTCOME_TIMED_OUT,
    OUTCOME_UNAVAILABLE,
    REAP_QUIESCED,
    REAP_UNBOUNDED,
    TREE_SCOPE_CONTAINER,
    TerminationEvidence,
)

if TYPE_CHECKING:
    from pathlib import Path

_CLEANUP_SECONDS = 5.0
"""The bound on the removal that reaps the container, and with it every process inside it."""

_LIFECYCLE_OUTPUT_LIMIT_BYTES = 1024 * 1024
"""The bound on the daemon's own metadata, kept separate from the child's output budget.

``output_limit_bytes`` is what the *child* may print -- it is the model-facing stream, and a
runtime may set it low. The create, inspect and remove stages print runtime metadata nobody
chose the size of: a full ``docker inspect`` document is several kilobytes, so charging it to the
child's budget made a small budget turn every container execution into a start-unconfirmed
refusal. Both are still bounded; they are simply not the same bound.
"""


@dataclass(frozen=True, slots=True)
class ContainerOutcome:
    """Observed child output, or a fail-closed runtime refusal."""

    stdout: str
    stderr: str
    exit_code: int
    child_confirmed: bool
    cleanup_confirmed: bool = True
    termination: TerminationEvidence | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _LifecycleUnavailableError(Exception):
    """A bounded Docker-client failure that never renders its command argv."""

    stage: str
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    """Whether this stage ran out of time rather than failing for some other reason.

    A start that outlives the deadline used to collapse into the same anonymous 125/UNUSABLE
    shape a missing image produces. It is a different fact, and the criterion that timeouts stay
    visible (F6.8) applies to the container backend exactly as it does to the local one.
    """

    overflow: bool = False
    """Whether this stage was stopped by the combined output budget rather than by time."""


@dataclass(slots=True)
class _LifecycleNotes:
    """Facts one lifecycle observed, filled in as its stages run."""

    timed_out: bool = False
    overflow: bool = False
    exit_code: int | None = None
    probe: dict[str, Any] | None = None
    duration_s: float | None = None
    reap_duration_s: float | None = None


def _remaining(deadline: float) -> float:
    """Return the positive time left under one lifecycle deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("docker lifecycle", timeout=0)
    return remaining


def _run(
    command: list[str], *, cwd: Path, deadline: float, output_limit_bytes: int
) -> subprocess.CompletedProcess[str]:
    """Run one Docker lifecycle command under the shared deadline."""
    return run_bounded_process(
        command,
        cwd=cwd,
        timeout_s=_remaining(deadline),
        output_limit_bytes=output_limit_bytes,
    )


def _inspected(raw: str) -> dict[str, Any] | None:
    """Parse the daemon's inspect document, or ``None`` when it is not a JSON mapping."""
    try:
        document: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def _probe_facts(document: dict[str, Any]) -> dict[str, Any] | None:
    """Return what the daemon itself says this container was, or ``None`` if it will not say.

    A *live* profile of the container the run actually used (F6.4): the network mode, CPU, memory
    and pid ceilings, the root filesystem's writability and the mounts, read back from the daemon
    rather than echoed from the ``docker create`` argv this runtime built. MIRA grades it against
    the recorded specification; a producer that built one argv and recorded another is caught by
    the disagreement.

    Fail closed: any field missing or of the wrong shape means no probe at all, never a partly
    guessed one. An absent probe is missing evidence, which is a different verdict from a probe
    that disagrees.
    """
    host = document.get("HostConfig")
    mounts = document.get("Mounts")
    state = document.get("State")
    if not isinstance(host, dict) or not isinstance(mounts, list) or not isinstance(state, dict):
        return None
    network = host.get("NetworkMode")
    memory = host.get("Memory")
    cpus_nano = host.get("NanoCpus")
    pids_limit = host.get("PidsLimit")
    read_only = host.get("ReadonlyRootfs")
    oom_killed = state.get("OOMKilled")
    if (
        not isinstance(network, str)
        or isinstance(memory, bool)
        or not isinstance(memory, int)
        or isinstance(cpus_nano, bool)
        or not isinstance(cpus_nano, int)
        or cpus_nano <= 0
        or (
            pids_limit is not None
            and (isinstance(pids_limit, bool) or not isinstance(pids_limit, int))
        )
        or not isinstance(read_only, bool)
        or not isinstance(oom_killed, bool)
    ):
        return None
    recorded_mounts = _mount_facts(mounts)
    if recorded_mounts is None:
        return None
    return {
        "network": network,
        "memory_bytes": memory,
        "cpus_nano": cpus_nano,
        "pids_limit": pids_limit,
        "read_only_rootfs": read_only,
        "mounts": recorded_mounts,
        "oom_killed": oom_killed,
    }


def _mount_facts(mounts: list[Any]) -> list[dict[str, Any]] | None:
    """Return each mount's destination and writability, or ``None`` for an unreadable entry."""
    facts: list[dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            return None
        destination = mount.get("Destination")
        read_write = mount.get("RW")
        if not isinstance(destination, str) or not isinstance(read_write, bool):
            return None
        facts.append({"destination": destination, "read_write": read_write})
    return facts


def _valid_state(document: dict[str, Any], start_code: int) -> int | None:
    """Return a confirmed child's exit code from the inspect document's State."""
    state = document.get("State")
    if not isinstance(state, dict):
        return None
    exit_code = state.get("ExitCode")
    error = state.get("Error")
    started = state.get("StartedAt")
    finished = state.get("FinishedAt")
    if (
        state.get("Status") != "exited"
        or state.get("Running") is not False
        or not isinstance(state.get("OOMKilled"), bool)
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code != start_code
        or not isinstance(error, str)
        or error
        or not isinstance(started, str)
        or not isinstance(finished, str)
        or started in {"", "0001-01-01T00:00:00Z"}
        or finished in {"", "0001-01-01T00:00:00Z"}
    ):
        return None
    return exit_code


def _text(value: str | bytes | None) -> str:
    """Normalize bounded subprocess output without rendering a command."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _attempt(
    stage: str, command: list[str], *, cwd: Path, deadline: float, output_limit_bytes: int
) -> subprocess.CompletedProcess[str]:
    """Run one stage and replace exceptions with a command-free diagnostic."""
    try:
        return _run(command, cwd=cwd, deadline=deadline, output_limit_bytes=output_limit_bytes)
    except OutputLimitExceeded as exc:
        raise _LifecycleUnavailableError(
            f"docker {stage} output exceeded {exc.limit_bytes} bytes",
            stdout=exc.stdout,
            stderr=exc.stderr,
            overflow=True,
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise _LifecycleUnavailableError(
            f"docker {stage} timed out",
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            timed_out=True,
        ) from None
    except OSError as exc:
        raise _LifecycleUnavailableError(f"docker {stage} failed ({type(exc).__name__})") from None


def _state_error(raw: str) -> str:
    """Return Docker's bounded State.Error when the state was not confirmable."""
    document = _inspected(raw)
    if document is None:
        return ""
    state = document.get("State")
    if not isinstance(state, dict):
        return ""
    error = state.get("Error")
    return error if isinstance(error, str) else ""


def _failure(
    reason: str, *, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> ContainerOutcome:
    """Build the single fail-closed runtime-refusal shape."""
    detail = f"container execution unavailable: {reason}"
    if stderr:
        detail = f"{detail}: {stderr}"
    return ContainerOutcome(
        stdout=stdout,
        stderr=detail,
        exit_code=125,
        child_confirmed=False,
        timed_out=timed_out,
    )


def run_container_lifecycle(
    docker: str,
    create_command: list[str],
    *,
    name: str,
    cwd: Path,
    timeout_s: float,
    output_limit_bytes: int,
    cleanup: bool = True,
) -> ContainerOutcome:
    """Run under one execution deadline, then optionally allow bounded cleanup.

    The quota backend retains the worker briefly after inspection so its helper can export a
    still-mounted volume.  All existing callers keep the default cleanup behavior.
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_s
    notes = _LifecycleNotes()
    created = False
    outcome: ContainerOutcome | None = None
    try:
        created_result = _attempt(
            "create",
            create_command,
            cwd=cwd,
            deadline=deadline,
            output_limit_bytes=_LIFECYCLE_OUTPUT_LIMIT_BYTES,
        )
        if created_result.returncode != 0:
            outcome = _failure(
                "container creation was refused; build thymira:dev with just docker-build",
                stdout=created_result.stdout,
                stderr=created_result.stderr,
            )
        else:
            created = True
            outcome = _start_and_confirm(
                docker,
                name=name,
                cwd=cwd,
                deadline=deadline,
                output_limit_bytes=output_limit_bytes,
                notes=notes,
            )
    except _LifecycleUnavailableError as exc:
        notes.timed_out = exc.timed_out
        notes.overflow = exc.overflow
        outcome = _failure(exc.stage, stdout=exc.stdout, stderr=exc.stderr, timed_out=exc.timed_out)
    finally:
        # The execution deadline covers create/start/inspect, not the separately bounded remove.
        # Capture it before cleanup so slow daemon removal cannot make a healthy child look late.
        notes.duration_s = time.monotonic() - started_at
        cleanup_started = time.monotonic()
        if cleanup:
            try:
                removed = run_bounded_process(
                    [docker, "rm", "--force", name],
                    cwd=cwd,
                    timeout_s=_CLEANUP_SECONDS,
                    output_limit_bytes=_LIFECYCLE_OUTPUT_LIMIT_BYTES,
                )
            except (OSError, OutputLimitExceeded, subprocess.TimeoutExpired):
                removed = None
        else:
            removed = None
            if outcome is not None:
                outcome = replace(outcome, cleanup_confirmed=False)
        notes.reap_duration_s = time.monotonic() - cleanup_started
        if (
            cleanup
            and created
            and (removed is None or removed.returncode != 0)
            and outcome is not None
        ):
            # A cleanup failure is a separate fact from the execution outcome: the child's own
            # exit_code/child_confirmed keep reporting what was actually observed, and only
            # cleanup_confirmed and stderr record that the container itself could not be removed
            # (F6.6). Replacing the whole outcome here used to conflate the two.
            cleanup_error = removed.stderr if removed is not None else "cleanup command failed"
            outcome = replace(
                outcome,
                cleanup_confirmed=False,
                stderr=f"{outcome.stderr}\ncontainer cleanup failed: {cleanup_error}",
            )
    if outcome is None:
        raise RuntimeError("container lifecycle produced no outcome")
    return replace(
        outcome,
        termination=_termination(
            outcome,
            notes,
            timeout_s=timeout_s,
            duration_s=notes.duration_s,
        ),
    )


def _start_and_confirm(
    docker: str,
    *,
    name: str,
    cwd: Path,
    deadline: float,
    output_limit_bytes: int,
    notes: _LifecycleNotes,
) -> ContainerOutcome:
    """Start the created container and confirm its exit against the daemon's own record."""
    started = _attempt(
        "start",
        [docker, "start", "--attach", name],
        cwd=cwd,
        deadline=deadline,
        output_limit_bytes=output_limit_bytes,
    )
    inspected = _attempt(
        "inspect",
        [docker, "inspect", "--format", "{{json .}}", name],
        cwd=cwd,
        deadline=deadline,
        output_limit_bytes=_LIFECYCLE_OUTPUT_LIMIT_BYTES,
    )
    document = _inspected(inspected.stdout) if inspected.returncode == 0 else None
    exit_code = _valid_state(document, started.returncode) if document is not None else None
    if document is None or exit_code is None:
        return _failure(
            "container start could not be confirmed",
            stdout=started.stdout,
            stderr=_state_error(inspected.stdout) or inspected.stderr or started.stderr,
        )
    notes.exit_code = exit_code
    notes.probe = _probe_facts(document)
    return ContainerOutcome(
        stdout=started.stdout,
        stderr=started.stderr,
        exit_code=exit_code,
        child_confirmed=True,
    )


def _lifecycle_outcome(outcome: ContainerOutcome, notes: _LifecycleNotes) -> str:
    """Name what ended this lifecycle, keeping a timeout distinct from every other refusal."""
    if outcome.child_confirmed:
        return OUTCOME_COMPLETED
    if notes.timed_out:
        return OUTCOME_TIMED_OUT
    if notes.overflow:
        return OUTCOME_OUTPUT_EXCEEDED
    return OUTCOME_UNAVAILABLE


def _termination(
    outcome: ContainerOutcome,
    notes: _LifecycleNotes,
    *,
    timeout_s: float,
    duration_s: float | None,
) -> TerminationEvidence:
    """Publish what the daemon, not the child, said about this execution's end.

    The control channel is the container state the daemon reports, and it counts as validated
    only when :func:`_valid_state` found the inspected exit code equal to the attach return code
    -- two observers agreeing, rather than one being believed. The reap is the forced removal of
    the container, which takes every process inside it with it; it is addressed by this run's own
    unique container name, never by a pid, so PID reuse cannot make it reach anything else
    (F6.4).
    """
    return TerminationEvidence(
        outcome=_lifecycle_outcome(outcome, notes),
        exit_source=CONTROL_CHANNEL_CONTAINER_STATE,
        control_channel=CONTROL_CHANNEL_CONTAINER_STATE,
        control_channel_validated=outcome.child_confirmed,
        deadline_s=float(timeout_s),
        duration_s=duration_s,
        deadline_exceeded=notes.timed_out,
        timed_out=notes.timed_out,
        child_exit_code=notes.exit_code,
        reap=REAP_QUIESCED if outcome.cleanup_confirmed else REAP_UNBOUNDED,
        reap_deadline_s=_CLEANUP_SECONDS,
        reap_duration_s=notes.reap_duration_s,
        tree_scope=TREE_SCOPE_CONTAINER,
        signalled_after_reap=False,
        probe=notes.probe,
    )


__all__ = ["ContainerOutcome", "run_container_lifecycle"]
