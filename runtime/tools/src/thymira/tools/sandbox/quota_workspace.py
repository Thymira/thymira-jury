"""Docker local-driver tmpfs workspace quota execution.

The host process owns the Docker lifecycle.  A fixed helper keeps the quota volume mounted while
the untrusted worker runs, then validates and exports the bounded tree.  The worker receives one
volume mount only; it never receives a writable host bind or a Docker socket.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox.base import (
    ResolvedExecutionSpec,
    SandboxRun,
    StagedInput,
    WorkspaceQuotaEvidence,
    validate_staged_inputs,
)
from thymira.tools.sandbox.container_lifecycle import run_container_lifecycle
from thymira.tools.sandbox.environment import apply_runtime_owned, scrub_environment
from thymira.tools.sandbox.private_storage import container_private_storage
from thymira.tools.sandbox.process_capture import (
    DEFAULT_OUTPUT_LIMIT_BYTES,
    OutputLimitExceeded,
)
from thymira.tools.sandbox.quota_docker import (
    MAX_PROTOCOL_BYTES as _MAX_PROTOCOL_BYTES,
)
from thymira.tools.sandbox.quota_docker import (
    build_container_command as _create_common,
)
from thymira.tools.sandbox.quota_docker import (
    cleanup_quota_resources as _cleanup,
)
from thymira.tools.sandbox.quota_docker import (
    inspect_quota_volume as _inspect_volume,
)
from thymira.tools.sandbox.quota_docker import (
    inspect_worker_mount as _worker_inspection,
)
from thymira.tools.sandbox.quota_docker import (
    parse_helper_protocol as _protocol,
)
from thymira.tools.sandbox.quota_docker import (
    remove_generated_container as _remove_container,
)
from thymira.tools.sandbox.quota_docker import (
    run_docker_command as _run_command,
)
from thymira.tools.sandbox.termination import (
    CONTROL_CHANNEL_NONE,
    OUTCOME_UNAVAILABLE,
    REAP_QUIESCED,
    TerminationEvidence,
)
from thymira.tools.sandbox.workspace_tree import (
    MAX_TREE_ENTRIES,
    TreeEntry,
    WorkspaceLock,
    WorkspaceTreeError,
    publish_tree,
    recover_publication,
    snapshot_tree,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


_NO_RUNTIME = TerminationEvidence(
    outcome=OUTCOME_UNAVAILABLE,
    exit_source="runtime_refusal",
    control_channel=CONTROL_CHANNEL_NONE,
    control_channel_validated=False,
)


def _host_identity() -> tuple[int, int]:
    """Return a non-root uid/gid suitable for the Linux helper volume."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return 999, 999
    uid, gid = getuid(), getgid()
    return (uid, gid) if uid > 0 and gid > 0 else (999, 999)


def _path_mount(path: Path, destination: str, mode: str) -> str:
    """Build a Docker bind argument without allowing the caller to choose its mode."""
    source = path.resolve().as_posix() if os.name == "nt" else str(path.resolve())
    return f"{source}:{destination}:{mode}"


def _volume_options(requested: int, uid: int, gid: int, inode_limit: int) -> str:
    """Return one argv-safe local-driver tmpfs option string."""
    # Docker Desktop's local tmpfs driver ignores uid/gid on a Windows host.  Its private volume
    # therefore needs a world-writable root for the capability-dropped worker; native Linux keeps
    # the design's non-root, owner-only volume permissions.
    mode = "0777" if os.name == "nt" else "0700"
    return f"size={requested},uid={uid},gid={gid},mode={mode},nr_inodes={inode_limit}"


def _docker_memory_bytes(value: str) -> int:
    """Return the exact byte ceiling Docker derives from a validated memory setting."""
    suffix = value[-1].lower()
    multiplier = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}.get(suffix)
    if multiplier is None:
        return int(value)
    return int(value[:-1]) * multiplier


def _docker_cpus_nano(value: str) -> int:
    """Return the exact NanoCPUs value Docker derives from a validated CPU setting."""
    whole, _, fraction = value.partition(".")
    return int(whole) * 1_000_000_000 + int(fraction.ljust(9, "0") or "0")


def _worker_profile_is_confined(
    termination: TerminationEvidence | None,
    *,
    memory: str,
    cpus: str,
    pids_limit: int,
    writable: bool,
) -> bool:
    """Return whether Docker's own worker profile proves the configured boundary."""
    if termination is None or not termination.control_channel_validated:
        return False
    probe = termination.probe
    if not isinstance(probe, dict):
        return False
    return (
        termination.tree_scope == "container"
        and probe.get("network") == "none"
        and probe.get("memory_bytes") == _docker_memory_bytes(memory)
        and probe.get("cpus_nano") == _docker_cpus_nano(cpus)
        and probe.get("pids_limit") == pids_limit
        and probe.get("read_only_rootfs") is True
        and probe.get("mounts") == [{"destination": "/workspace", "read_write": writable}]
        and probe.get("oom_killed") is False
    )


def run_quota_workspace(  # noqa: PLR0912, PLR0915  # ordered backend state machine stays explicit
    docker: str,
    *,
    image: str,
    workspace: Path,
    argv: list[str],
    mode: SandboxMode,
    timeout_s: float,
    env: Mapping[str, str] | None,
    memory: str,
    cpus: str,
    pids_limit: int,
    runtime: str | None,
    requested_bytes: int,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
) -> SandboxRun:
    """Run a worker in a capability-proven tmpfs volume and publish its bounded tree."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"sandbox workspace does not exist: {workspace}")
    input_values = validate_staged_inputs(staged_inputs)
    input_logical_bytes, input_entries, input_tree_sha256 = _staged_input_facts(input_values)
    input_dir: Path | None = None
    # A manifest that is already larger than the requested capacity cannot enter the private
    # source mount: copying it would spend time and host disk before the quota refusal.  The
    # manifest facts still travel in the evidence so every named producer can prove that its
    # inputs were charged before a worker existed.
    if input_values and input_logical_bytes <= requested_bytes:
        input_dir = Path(tempfile.mkdtemp(prefix="thymira-quota-inputs-"))
        try:
            for item in input_values:
                target = input_dir.joinpath(*item.relative_path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(item.content)
        except BaseException:
            shutil.rmtree(input_dir, ignore_errors=True)
            raise
    execution_id = uuid4().hex
    volume = f"thymira-quota-{execution_id}"
    helper = f"thymira-quota-helper-{execution_id}"
    uid, gid = _host_identity()
    # Docker Desktop remounts the private local-driver volume as root:root even when its mount
    # options request uid/gid 999.  Keep the fixed helper and capability-dropped worker on the
    # same container identity so files staged by one remain writable to the other.  Native POSIX
    # hosts retain their non-root host/fallback identity.
    container_identity = (0, 0) if os.name == "nt" else (uid, gid)
    inode_limit = min(MAX_TREE_ENTRIES * 2, max(256, requested_bytes // 4096))
    options = _volume_options(requested_bytes, uid, gid, inode_limit)
    export_dir = root.parent / f".{root.name}.incoming-{execution_id}"
    journal = root.parent / f".{root.name}.quota-journal.json"
    source_snapshot = None
    final_facts: dict[str, Any] | None = None
    exported_snapshot = None
    input_snapshot = None
    observed_capacity = None
    observed_filesystem = None
    observed_inodes = None
    helper_created = False
    volume_created = False
    volume_inspected = False
    worker_mount_inspected = False
    environment_names: tuple[str, ...] = ()
    excluded_environment_names: tuple[str, ...] = ()
    child = None
    worker_name = f"thymira-sandbox-{execution_id}"
    publication = "failed"
    errors: list[str] = []
    cleanup = {"helper": False, "worker": False, "volume": False, "staging": False}
    if input_dir is not None:
        cleanup["inputs"] = False
    with WorkspaceLock(root):
        try:
            recovery = recover_publication(journal, workspace=root, max_bytes=requested_bytes)
            if recovery not in {"none", "recovered"}:
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "workspace publication recovery did not complete"
                )
            source_snapshot = snapshot_tree(root, max_bytes=requested_bytes)
            input_snapshot = (
                snapshot_tree(input_dir, max_bytes=requested_bytes)
                if input_dir is not None
                else None
            )
            staged_bytes = (
                input_snapshot.logical_bytes if input_snapshot is not None else input_logical_bytes
            )
            if input_snapshot is not None and (
                input_snapshot.tree_sha256 != input_tree_sha256
                or input_snapshot.entry_count != input_entries
            ):
                raise WorkspaceTreeError(  # noqa: TRY301  # independent input manifest guard
                    "private staged input observations disagree with the runtime manifest"
                )
            if source_snapshot.logical_bytes + staged_bytes > requested_bytes:
                # This is deliberately before volume creation and before a child starts.  For an
                # overlarge manifest ``input_dir`` is absent; the branch above still leaves the
                # measured bytes and digest in the final evidence.
                raise WorkspaceTreeError(  # noqa: TRY301  # quota refusal before child start
                    "source and staged inputs exceed the requested quota"
                )
            export_dir.mkdir()
            volume_result = _run_command(
                [
                    docker,
                    "volume",
                    "create",
                    "--driver",
                    "local",
                    "--label",
                    f"thymira.execution={volume}",
                    "--opt",
                    "type=tmpfs",
                    "--opt",
                    "device=tmpfs",
                    "--opt",
                    f"o={options}",
                    volume,
                ],
                cwd=root,
                timeout_s=timeout_s,
                limit=_MAX_PROTOCOL_BYTES,
            )
            if volume_result.returncode != 0:
                raise WorkspaceTreeError("quota tmpfs volume creation was refused")  # noqa: TRY301
            volume_created = True
            volume_inspected = (
                _inspect_volume(
                    docker, volume, cwd=root, timeout_s=timeout_s, expected_options=options
                )
                is not None
            )
            if not volume_inspected:
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "quota tmpfs volume inspection did not match request"
                )
            create = _create_common(
                name=helper,
                image=image,
                memory=memory,
                cpus=cpus,
                pids_limit=pids_limit,
                runtime=runtime,
                network="none",
                mounts=[
                    _path_mount(root, "/source", "ro"),
                    _path_mount(export_dir, "/export", "rw"),
                    f"{volume}:/workspace:rw",
                    *([_path_mount(input_dir, "/inputs", "ro")] if input_dir else []),
                ],
                user=container_identity,
                argv=[
                    "python",
                    "-I",
                    "-u",
                    "-B",
                    "-m",
                    "thymira.tools.sandbox.workspace_helper",
                    "hold",
                ],
            )
            create_result = _run_command(
                create, cwd=root, timeout_s=timeout_s, limit=_MAX_PROTOCOL_BYTES
            )
            if create_result.returncode != 0:
                raise WorkspaceTreeError("quota helper container creation was refused")  # noqa: TRY301
            helper_created = True
            start_result = _run_command(
                [docker, "start", helper], cwd=root, timeout_s=timeout_s, limit=_MAX_PROTOCOL_BYTES
            )
            if start_result.returncode != 0:
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    f"quota helper did not start: {start_result.stderr or start_result.stdout}"
                )
            stage_result = _run_command(
                [
                    docker,
                    "exec",
                    helper,
                    "python",
                    "-I",
                    "-u",
                    "-B",
                    "-m",
                    "thymira.tools.sandbox.workspace_helper",
                    "stage",
                    "--requested",
                    str(requested_bytes),
                    "--max-entries",
                    str(MAX_TREE_ENTRIES),
                    *(["--inputs", "/inputs"] if input_dir else []),
                    "--deadline-s",
                    str(max(0.1, min(timeout_s, 300.0))),
                ],
                cwd=root,
                timeout_s=timeout_s,
                limit=_MAX_PROTOCOL_BYTES,
            )
            if (
                stage_result.returncode != 0
                or not (stage := _protocol(stage_result.stdout))
                or "error" in stage
            ):
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "quota helper rejected the source staging: "
                    f"{stage_result.stderr or stage_result.stdout}"
                )
            source_facts = stage.get("source")
            probe = stage.get("probe")
            if not isinstance(source_facts, dict) or not isinstance(probe, dict):
                raise WorkspaceTreeError("quota stage evidence was incomplete")  # noqa: TRY301
            input_facts = stage.get("inputs")
            if input_dir is None:
                input_facts = input_facts or {"logical_bytes": 0, "entry_count": 0}
            else:
                if not isinstance(input_facts, dict):
                    raise WorkspaceTreeError(  # noqa: TRY301
                        "quota staged input evidence was incomplete"
                    )
                if input_snapshot is None:
                    raise WorkspaceTreeError(  # noqa: TRY301
                        "host staged input snapshot was not captured"
                    )
                if (
                    input_facts.get("tree_sha256") != input_snapshot.tree_sha256
                    or input_facts.get("logical_bytes") != input_snapshot.logical_bytes
                    or input_facts.get("entry_count") != input_snapshot.entry_count
                ):
                    raise WorkspaceTreeError(  # noqa: TRY301  # independent observation guard
                        "helper staged input observations disagree with the host"
                    )
            observed_capacity = probe.get("capacity_bytes")
            observed_filesystem = probe.get("filesystem")
            observed_inodes = probe.get("inode_capacity")
            if (
                not isinstance(observed_capacity, int)
                or isinstance(observed_capacity, bool)
                or observed_capacity <= 0
                or observed_capacity > requested_bytes
                or observed_filesystem != "tmpfs"
            ):
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "observed quota capacity did not satisfy the request"
                )
            source_after_stage = snapshot_tree(root, max_bytes=requested_bytes)
            if (
                source_after_stage.tree_sha256 != source_snapshot.tree_sha256
                or source_facts.get("tree_sha256") != source_snapshot.tree_sha256
                or source_facts.get("logical_bytes") != source_snapshot.logical_bytes
            ):
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "source tree changed while it was being staged: "
                    f"host_before={source_snapshot.tree_sha256} "
                    f"host_after={source_after_stage.tree_sha256} "
                    f"helper={source_facts.get('tree_sha256')} "
                    f"helper_bytes={source_facts.get('logical_bytes')} "
                    f"host_bytes={source_snapshot.logical_bytes}"
                )
            env_values = apply_runtime_owned(scrub_environment(env), container_private_storage())
            environment_names = tuple(sorted(env_values.values))
            excluded_environment_names = env_values.excluded
            mount_mode = "ro" if mode is SandboxMode.READ_ONLY else "rw"
            worker_create = _create_common(
                name=worker_name,
                image=image,
                memory=memory,
                cpus=cpus,
                pids_limit=pids_limit,
                runtime=runtime,
                network="bridge" if mode is SandboxMode.DANGER_FULL_ACCESS else "none",
                mounts=[f"{volume}:/workspace:{mount_mode}"],
                user=container_identity,
                env=env_values.values,
                argv=argv,
            )
            child = run_container_lifecycle(
                docker,
                worker_create,
                name=worker_name,
                cwd=root,
                timeout_s=timeout_s,
                output_limit_bytes=output_limit_bytes,
                cleanup=False,
            )
            worker_mount_inspected = _worker_inspection(
                docker,
                worker_name,
                volume,
                writable=mode is not SandboxMode.READ_ONLY,
                cwd=root,
                timeout_s=timeout_s,
            )
            if not worker_mount_inspected:
                raise WorkspaceTreeError("worker mount identity could not be confirmed")  # noqa: TRY301
            worker_removed, worker_error = _remove_container(docker, worker_name, cwd=root)
            cleanup["worker"] = worker_removed
            if not worker_removed:
                errors.append(worker_error)
            elif child.termination is not None:
                child = replace(
                    child,
                    cleanup_confirmed=True,
                    termination=replace(child.termination, reap=REAP_QUIESCED),
                )
            export_result = _run_command(
                [
                    docker,
                    "exec",
                    helper,
                    "python",
                    "-I",
                    "-u",
                    "-B",
                    "-m",
                    "thymira.tools.sandbox.workspace_helper",
                    "export",
                    "--requested",
                    str(requested_bytes),
                    "--max-entries",
                    str(MAX_TREE_ENTRIES),
                    *(["--inputs", "/inputs"] if input_dir else []),
                    "--deadline-s",
                    str(max(0.1, min(timeout_s, 300.0))),
                ],
                cwd=root,
                timeout_s=timeout_s,
                limit=_MAX_PROTOCOL_BYTES,
            )
            if (
                export_result.returncode != 0
                or not (export_facts := _protocol(export_result.stdout))
                or "error" in export_facts
            ):
                raise WorkspaceTreeError("quota helper rejected workspace export")  # noqa: TRY301
            final_facts = export_facts.get("final")
            exported_facts = export_facts.get("exported")
            probe_after = export_facts.get("probe_after")
            if (
                not isinstance(final_facts, dict)
                or not isinstance(exported_facts, dict)
                or not isinstance(probe_after, dict)
            ):
                raise WorkspaceTreeError("quota export evidence was incomplete")  # noqa: TRY301
            exported_snapshot = snapshot_tree(export_dir, max_bytes=requested_bytes)
            if (
                exported_snapshot.tree_sha256 != str(exported_facts.get("tree_sha256"))
                or exported_snapshot.logical_bytes > requested_bytes
                or probe_after.get("capacity_bytes") != observed_capacity
            ):
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "quota helper and host export observations disagree: "
                    f"host={exported_snapshot.tree_sha256} "
                    f"helper={exported_facts.get('tree_sha256')} "
                    f"host_bytes={exported_snapshot.logical_bytes} "
                    f"helper_bytes={exported_facts.get('logical_bytes')} "
                    f"capacity={probe_after.get('capacity_bytes')} "
                    f"expected_capacity={observed_capacity}"
                )
            if final_facts.get("tree_sha256") != exported_snapshot.tree_sha256:
                raise WorkspaceTreeError(  # noqa: TRY301  # state-machine guard
                    "host and helper final tree observations disagree: "
                    f"helper={final_facts.get('tree_sha256')} "
                    f"host={exported_snapshot.tree_sha256}"
                )
            publication = publish_tree(
                root,
                export_dir,
                expected_old_digest=source_snapshot.tree_sha256,
                expected_new_digest=exported_snapshot.tree_sha256,
                journal_path=journal,
                execution_id=execution_id,
                max_bytes=requested_bytes,
            )
        except (
            OSError,
            OutputLimitExceeded,
            subprocess.TimeoutExpired,
            WorkspaceTreeError,
            ValueError,
        ) as exc:
            errors.append(str(exc))
        finally:
            if child is not None and not cleanup["worker"]:
                worker_removed, worker_error = _remove_container(docker, worker_name, cwd=root)
                cleanup["worker"] = worker_removed
                if not worker_removed:
                    errors.append(worker_error)
            if helper_created or volume_created:
                helper_removed, volume_removed, cleanup_error = _cleanup(
                    docker, helper, volume, cwd=root
                )
                cleanup["helper"] = helper_removed
                cleanup["volume"] = volume_removed
                if cleanup_error:
                    errors.append(cleanup_error)
                if export_dir.exists():
                    try:
                        shutil.rmtree(export_dir)
                        cleanup["staging"] = True
                    except OSError as exc:
                        errors.append(f"staging cleanup failed: {type(exc).__name__}")
                elif publication in {"committed", "recovered"}:
                    cleanup["staging"] = True
            if input_dir is not None:
                try:
                    shutil.rmtree(input_dir)
                    cleanup["inputs"] = True
                except OSError as exc:
                    errors.append(f"staged input cleanup failed: {type(exc).__name__}")
            elif input_values:
                # An overlarge manifest was refused before private input allocation, so there is
                # nothing to remove; record that fact as successful cleanup rather than leaving a
                # false residue marker in the evidence.
                cleanup["inputs"] = True
    if child is None:
        child_exit = 125
        stdout = ""
        stderr = "quota backend unavailable: " + (errors[0] if errors else "no child was run")
        termination = _NO_RUNTIME
        child_confirmed = False
    else:
        child_exit = child.exit_code
        stdout = child.stdout
        stderr = child.stderr
        child_confirmed = child.child_confirmed
        termination = child.termination
    if errors:
        stderr = f"{stderr}\n" + "\n".join(errors)
    evidence = WorkspaceQuotaEvidence(
        mechanism="docker_local_tmpfs_volume",
        requested_bytes=requested_bytes,
        observed_capacity_bytes=observed_capacity,
        observed_filesystem=observed_filesystem,
        inode_limit=inode_limit,
        observed_inode_capacity=observed_inodes,
        source_logical_bytes=source_snapshot.logical_bytes if source_snapshot else None,
        staged_input_logical_bytes=input_logical_bytes if input_values else None,
        final_logical_bytes=final_facts.get("logical_bytes") if final_facts else None,
        exported_logical_bytes=exported_snapshot.logical_bytes if exported_snapshot else None,
        source_entries=source_snapshot.entry_count if source_snapshot else None,
        staged_input_entries=input_entries if input_values else None,
        final_entries=final_facts.get("entry_count") if final_facts else None,
        exported_entries=exported_snapshot.entry_count if exported_snapshot else None,
        source_tree_sha256=source_snapshot.tree_sha256 if source_snapshot else None,
        staged_input_tree_sha256=input_tree_sha256 if input_values else None,
        final_tree_sha256=final_facts.get("tree_sha256") if final_facts else None,
        exported_tree_sha256=exported_snapshot.tree_sha256 if exported_snapshot else None,
        volume_inspected=volume_inspected,
        worker_mount_inspected=worker_mount_inspected,
        publication=publication,
        cleanup=cleanup,
    )
    spec = ResolvedExecutionSpec(
        backend="container",
        image=image,
        workspace_mount=(
            f"volume:{hashlib_sha256(volume)}:/workspace:"
            f"{'ro' if mode is SandboxMode.READ_ONLY else 'rw'}"
        ),
        network="bridge" if mode is SandboxMode.DANGER_FULL_ACCESS else "none",
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        environment_names=environment_names,
        excluded_environment_names=excluded_environment_names,
        unenforced=(() if publication in {"committed", "recovered"} else ("workspace_quota",)),
        output_limit_bytes=output_limit_bytes,
        workspace_quota_bytes=requested_bytes,
    )
    full_confinement = (
        child_confirmed
        and not errors
        and mode is not SandboxMode.DANGER_FULL_ACCESS
        and volume_inspected
        and worker_mount_inspected
        and _worker_profile_is_confined(
            termination,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            writable=mode is not SandboxMode.READ_ONLY,
        )
        and publication in {"committed", "recovered"}
        and all(cleanup.values())
    )
    enforcement = (
        SandboxEnforcement.FULL
        if full_confinement
        else (
            SandboxEnforcement.PARTIAL
            if child_confirmed and not errors
            else SandboxEnforcement.UNUSABLE
        )
    )
    return SandboxRun(
        stdout=stdout,
        stderr=stderr,
        exit_code=child_exit,
        mode=mode,
        enforcement=enforcement,
        spec=spec,
        cleanup_confirmed=all(cleanup.values()) if child is not None else None,
        termination=termination,
        quota_evidence=evidence,
    )


def _staged_input_facts(inputs: tuple[StagedInput, ...]) -> tuple[int, int, str]:
    """Compute bounded manifest facts before any private input copy is attempted."""
    entries: dict[str, TreeEntry] = {}
    logical_bytes = 0
    for item in inputs:
        path = Path(item.relative_path)
        parts = path.parts
        for index in range(1, len(parts)):
            relative = "/".join(parts[:index])
            entries.setdefault(relative, TreeEntry(relative, "directory", False, 0, None))
        digest = hashlib.sha256(item.content).hexdigest()
        entries[item.relative_path] = TreeEntry(
            item.relative_path, "file", False, len(item.content), digest
        )
        logical_bytes += len(item.content)
    ordered = tuple(entries[key] for key in sorted(entries))
    encoded = "".join(
        json.dumps(entry.identity_payload(), sort_keys=True, separators=(",", ":")) + "\n"
        for entry in ordered
    ).encode()
    return logical_bytes, len(ordered), hashlib.sha256(encoded).hexdigest()


def hashlib_sha256(value: str) -> str:
    """Return a short execution-scoped identity digest without exposing the volume name."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


__all__ = ["run_quota_workspace"]
