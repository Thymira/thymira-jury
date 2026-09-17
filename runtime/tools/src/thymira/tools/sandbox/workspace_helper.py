"""Fixed-command keeper operations for the local-driver tmpfs quota backend.

This module is intentionally independent of the host tree implementation.  It runs in a trusted
helper container with fixed mounts and no caller argv/environment.  The untrusted worker only sees
the ``/workspace`` volume and cannot reach either the source bind or the export directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

_CHUNK = 1024 * 1024
_PROTOCOL_VERSION = 1
_MAX_DEPTH = 64
_MOUNTINFO_PATH_FIELDS = 5
_WINDOWS_DEVICE_PREFIX_LENGTH = 4


class HelperError(RuntimeError):
    """A helper operation cannot produce trusted quota evidence."""


def _portable_name(name: str) -> None:
    """Reject names that are not portable between the host and Linux worker."""
    if not name or name in {".", ".."} or name.endswith((".", " ")) or ":" in name:
        raise HelperError(f"non-portable workspace name: {name!r}")
    folded = name.rstrip(" .").casefold()
    if folded in {"con", "prn", "aux", "nul"} or (
        len(folded) == _WINDOWS_DEVICE_PREFIX_LENGTH
        and folded[:3] in {"com", "lpt"}
        and folded[3].isdigit()
    ):
        raise HelperError(f"Windows device workspace name: {name!r}")


def _file_digest(path: Path, size: int, remaining: int) -> str:
    """Hash one regular file without charging allocated blocks."""
    if size > remaining:
        raise HelperError("workspace logical byte bound exceeded")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb", buffering=0) as handle:
        while chunk := handle.read(min(_CHUNK, remaining - total + 1)):
            total += len(chunk)
            if total > remaining:
                raise HelperError("workspace logical byte bound exceeded")
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if after.st_size != size or not stat.S_ISREG(after.st_mode):
        raise HelperError("workspace changed during helper hashing")
    return digest.hexdigest()


def _snapshot(  # bounded validation keeps each rejection explicit
    root: Path, max_bytes: int, max_entries: int
) -> dict[str, Any]:
    """Walk a helper mount with lstat semantics and return canonical tree facts."""
    root = Path(root)
    if not root.is_dir():
        raise HelperError("helper workspace mount is not a directory")
    pending = [(root, 0)]
    entries: list[dict[str, Any]] = []
    logical_bytes = 0
    folded: set[str] = set()
    while pending:
        directory, depth = pending.pop()
        children = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        for child in children:
            _portable_name(child.name)
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            folded_name = relative.casefold()
            if folded_name in folded:
                raise HelperError(f"case-fold collision: {relative}")
            folded.add(folded_name)
            if len(entries) >= max_entries:
                raise HelperError("workspace entry bound exceeded")
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise HelperError("workspace entry could not be inspected") from exc
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise HelperError(f"workspace entry is not a regular tree member: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 > _MAX_DEPTH:
                    raise HelperError("workspace nesting exceeds its depth bound")
                entries.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "executable": bool(info.st_mode & stat.S_IXUSR),
                        "size": 0,
                        "sha256": None,
                    }
                )
                pending.append((path, depth + 1))
                continue
            if getattr(info, "st_nlink", 1) != 1:
                raise HelperError(f"hard links are not portable: {relative}")
            logical_bytes += info.st_size
            if logical_bytes > max_bytes:
                raise HelperError("workspace logical byte bound exceeded")
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "executable": bool(info.st_mode & stat.S_IXUSR),
                    "size": info.st_size,
                    "sha256": _file_digest(
                        path, info.st_size, max_bytes - logical_bytes + info.st_size
                    ),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    encoded = "".join(
        json.dumps(
            {key: value for key, value in entry.items() if key != "executable"},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for entry in entries
    ).encode()
    return {
        "entries": entries,
        "entry_count": len(entries),
        "logical_bytes": logical_bytes,
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _open_relative(root: Path, relative: str) -> int:
    """Open a regular source file through no-follow directory descriptors when available."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | nofollow | cloexec
    if os.name == "nt":
        return os.open(root / relative, flags)
    root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
    components = relative.split("/")
    try:
        parent_fd = root_fd
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            return os.open(components[-1], flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except (NotImplementedError, TypeError):
        os.close(root_fd)
        return os.open(root / relative, flags)


def _copy_file(  # noqa: PLR0912  # bounded copy keeps each failure observable
    source: Path, target: Path, entry: dict[str, Any], *, deadline: float
) -> None:
    """Copy exactly the pinned bytes from an opened descriptor, with a time and size bound."""
    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise HelperError("helper snapshot has an invalid file size")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise HelperError(f"helper destination collision: {entry['path']}")
    target_created = False
    copied = False
    source_fd: int | None = None
    try:
        source_fd = _open_relative(source, str(entry["path"]))
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
            raise HelperError(f"source changed before bounded copy: {entry['path']}")
        if source_info.st_size != size:
            raise HelperError(f"source changed before bounded copy: {entry['path']}")
        with os.fdopen(source_fd, "rb", buffering=0) as source_handle:
            source_fd = None
            with target.open("xb") as target_handle:
                target_created = True
                digest = hashlib.sha256()
                remaining = size
                while remaining:
                    if time.monotonic() > deadline:
                        raise HelperError("bounded workspace copy deadline exceeded")
                    chunk = source_handle.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise HelperError(
                            f"source was truncated during bounded copy: {entry['path']}"
                        )
                    target_handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if source_handle.read(1):
                    raise HelperError(f"source grew during bounded copy: {entry['path']}")
                if digest.hexdigest() != entry.get("sha256"):
                    raise HelperError(
                        f"source content changed during bounded copy: {entry['path']}"
                    )
                target_handle.flush()
                os.fsync(target_handle.fileno())
                copied = True
        if entry.get("executable"):
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
    except OSError as exc:
        raise HelperError(f"bounded workspace copy failed: {type(exc).__name__}") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if target_created and not copied:
            with suppress(OSError):
                target.unlink(missing_ok=True)


def _copy(
    source: Path,
    destination: Path,
    snapshot: dict[str, Any],
    *,
    allow_existing: bool = False,
    deadline_s: float = 30.0,
) -> None:
    """Copy only pinned snapshot entries with bounded, descriptor-backed reads."""
    if deadline_s <= 0:
        raise HelperError("helper copy deadline must be positive")
    if destination.exists():
        if not destination.is_dir():
            raise HelperError("helper destination is not a directory")
        if not allow_existing and any(destination.iterdir()):
            raise HelperError("helper destination is not empty")
    else:
        destination.mkdir(parents=True)
    deadline = time.monotonic() + deadline_s
    for entry in snapshot["entries"]:
        target = destination / entry["path"]
        if entry["kind"] == "directory":
            if target.exists() or target.is_symlink():
                if not target.is_dir() or target.is_symlink():
                    raise HelperError(f"helper destination collision: {entry['path']}")
            else:
                target.mkdir()
            continue
        _copy_file(source, target, entry, deadline=deadline)


def _empty_snapshot() -> dict[str, Any]:
    """Return canonical facts for a source mount with no staged inputs."""
    return {
        "entries": [],
        "entry_count": 0,
        "logical_bytes": 0,
        "tree_sha256": hashlib.sha256(b"").hexdigest(),
    }


def _remove_inputs(workspace: Path, snapshot: dict[str, Any]) -> None:
    """Remove exactly the runtime-owned input paths before exporting child outputs."""
    for entry in reversed(snapshot["entries"]):
        path = workspace / entry["path"]
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HelperError(
                f"staged input could not be inspected before export: {entry['path']}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise HelperError(f"staged input was replaced by a link: {entry['path']}")
        try:
            if entry["kind"] == "file":
                if not stat.S_ISREG(info.st_mode):
                    raise HelperError(f"staged input changed kind before export: {entry['path']}")
                path.unlink()
            elif stat.S_ISDIR(info.st_mode):
                path.rmdir()
        except OSError as exc:
            if entry["kind"] == "directory":
                continue
            raise HelperError(
                f"staged input could not be removed before export: {entry['path']}"
            ) from exc


def _filesystem_type(path: Path) -> str | None:
    """Read the trusted mount type from proc mountinfo for ``path``."""
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    target = str(path.resolve())
    best_mount = ""
    best_type: str | None = None
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        right = after.split()
        if len(fields) < _MOUNTINFO_PATH_FIELDS or not right:
            continue
        mount_point = fields[4].replace("\\040", " ")
        if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(
            mount_point
        ) >= len(best_mount):
            best_mount, best_type = mount_point, right[0]
    return best_type


def _probe(path: Path, requested: int) -> dict[str, Any]:
    """Return trusted capacity, inode and filesystem facts for one mount."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        raise HelperError("quota mount capacity is unavailable on this platform")
    try:
        stats = statvfs(path)
    except OSError as exc:
        raise HelperError("quota mount capacity could not be observed") from exc
    capacity = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    if capacity <= 0 or capacity > requested:
        raise HelperError("observed quota capacity is absent or above the request")
    filesystem = _filesystem_type(path)
    if filesystem != "tmpfs":
        raise HelperError("quota mount is not tmpfs")
    return {
        "capacity_bytes": capacity,
        "free_bytes": free,
        "inode_capacity": stats.f_files,
        "inode_free": stats.f_favail,
        "filesystem": filesystem,
    }


def stage(
    source: Path,
    workspace: Path,
    *,
    requested: int,
    max_entries: int,
    inputs: Path | None = None,
    deadline_s: float = 30.0,
) -> dict[str, Any]:
    """Validate and stage the read-only source into the quota volume."""
    source_snapshot = _snapshot(source, requested, max_entries)
    input_snapshot = (
        _snapshot(inputs, requested, max_entries) if inputs is not None else _empty_snapshot()
    )
    probe = _probe(workspace, requested)
    charged_bytes = source_snapshot["logical_bytes"] + input_snapshot["logical_bytes"]
    if charged_bytes > probe["capacity_bytes"]:
        raise HelperError("source and staged inputs exceed the observed quota capacity")
    _copy(source, workspace, source_snapshot, deadline_s=deadline_s)
    if inputs is not None:
        _copy(inputs, workspace, input_snapshot, allow_existing=True, deadline_s=deadline_s)
    staged = _snapshot(workspace, requested, max_entries)
    if staged["logical_bytes"] != charged_bytes:
        raise HelperError("staged tree logical bytes do not match charged inputs")
    return {
        "version": _PROTOCOL_VERSION,
        "operation": "stage",
        "source": source_snapshot,
        "inputs": input_snapshot,
        "final": staged,
        "probe": probe,
    }


def export(
    workspace: Path,
    destination: Path,
    *,
    requested: int,
    max_entries: int,
    inputs: Path | None = None,
    deadline_s: float = 30.0,
) -> dict[str, Any]:
    """Validate and export the worker tree into the fresh host directory."""
    input_snapshot = (
        _snapshot(inputs, requested, max_entries) if inputs is not None else _empty_snapshot()
    )
    if inputs is not None:
        _remove_inputs(workspace, input_snapshot)
    final = _snapshot(workspace, requested, max_entries)
    probe_before = _probe(workspace, requested)
    _copy(workspace, destination, final, deadline_s=deadline_s)
    exported = _snapshot(destination, requested, max_entries)
    probe_after = _probe(workspace, requested)
    if exported["tree_sha256"] != final["tree_sha256"]:
        raise HelperError("exported tree digest does not match the worker tree")
    if probe_after != probe_before and (
        probe_after["capacity_bytes"] != probe_before["capacity_bytes"]
        or probe_after["filesystem"] != probe_before["filesystem"]
    ):
        raise HelperError("quota mount capacity drifted during export")
    return {
        "version": _PROTOCOL_VERSION,
        "operation": "export",
        "inputs": input_snapshot,
        "final": final,
        "exported": exported,
        "probe_before": probe_before,
        "probe_after": probe_after,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    hold = subparsers.add_parser("hold")
    hold.set_defaults(function=lambda _args: _hold())
    for operation in ("stage", "export"):
        command = subparsers.add_parser(operation)
        command.add_argument("--requested", type=int, required=True)
        command.add_argument("--max-entries", type=int, required=True)
        command.add_argument("--inputs", default=None)
        command.add_argument("--deadline-s", type=float, default=30.0)
        command.set_defaults(
            function=lambda args, operation=operation: _run_operation(operation, args)
        )
    return parser


def _hold() -> int:
    """Keep mounts alive until the lifecycle force-removes this helper."""
    while True:
        time.sleep(3600)


def _run_operation(operation: str, args: argparse.Namespace) -> int:
    """Dispatch a fixed operation and emit exactly one bounded JSON response."""
    if operation == "stage":
        result = stage(
            Path("/source"),
            Path("/workspace"),
            requested=args.requested,
            max_entries=args.max_entries,
            inputs=Path(args.inputs) if args.inputs else None,
            deadline_s=args.deadline_s,
        )
    else:
        result = export(
            Path("/workspace"),
            Path("/export"),
            requested=args.requested,
            max_entries=args.max_entries,
            inputs=Path(args.inputs) if args.inputs else None,
            deadline_s=args.deadline_s,
        )
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def main() -> int:
    """Run the fixed helper command."""
    try:
        args = _parser().parse_args()
        return args.function(args)
    except HelperError as exc:
        sys.stdout.write(json.dumps({"version": _PROTOCOL_VERSION, "error": str(exc)}) + "\n")
        sys.stdout.flush()
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by the Docker integration lane
    raise SystemExit(main())


__all__ = ["HelperError", "export", "main", "stage"]
