"""Bounded, no-follow workspace trees and journaled publication.

The quota backend uses this module on the trusted host side.  The worker never imports it and
never receives the publication paths.  Keeping the walk independent from the helper's walk gives
the runtime two observations of the tree before a bounded tree becomes live.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from thymira.state import replace_with_retry

MAX_TREE_ENTRIES = 100_000
MAX_TREE_DEPTH = 64
_CHUNK = 1024 * 1024
_WINDOWS_LOCK_BYTE = 1
_WINDOWS_DEVICE_PREFIX_LENGTH = 4
_DIGEST_LENGTH = 64
_GUARDS: dict[str, threading.RLock] = {}
_HANDLES: dict[str, tuple[Any, int]] = {}
_GUARDS_MUTEX = threading.Lock()


class WorkspaceTreeError(RuntimeError):
    """A workspace cannot be represented by the portable regular-file tree."""


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One portable tree entry, with content identity for regular files."""

    relative_path: str
    kind: str
    executable: bool
    size: int
    sha256: str | None

    def payload(self) -> dict[str, Any]:
        """Return deterministic, JSON-safe entry facts."""
        return {
            "path": self.relative_path,
            "kind": self.kind,
            "executable": self.executable,
            "size": self.size,
            "sha256": self.sha256,
        }

    def identity_payload(self) -> dict[str, Any]:
        """Return content identity facts stable across host and Linux permission mappings."""
        payload = self.payload()
        payload.pop("executable")
        return payload


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """The bounded digest and accounting facts for one workspace tree."""

    root: Path
    entries: tuple[TreeEntry, ...]
    logical_bytes: int
    tree_sha256: str

    @property
    def entry_count(self) -> int:
        """Return the number of entries, including directories."""
        return len(self.entries)


def _portable_component(component: str) -> None:
    """Reject names whose meaning differs across supported host/container filesystems."""
    if not component or component in {".", ".."}:
        raise WorkspaceTreeError("workspace contains an empty or traversal component")
    if component.endswith((".", " ")) or ":" in component:
        raise WorkspaceTreeError(f"workspace name is not portable: {component!r}")
    folded = component.rstrip(" .").casefold()
    if folded in {"con", "prn", "aux", "nul"}:
        raise WorkspaceTreeError(f"workspace name is a Windows device name: {component!r}")
    if (
        len(folded) == _WINDOWS_DEVICE_PREFIX_LENGTH
        and folded[:3] in {"com", "lpt"}
        and folded[3].isdigit()
    ):
        raise WorkspaceTreeError(f"workspace name is a Windows device name: {component!r}")


def _relative(root: Path, path: Path) -> str:
    """Return a stable POSIX relative path and validate each component."""
    relative = path.relative_to(root)
    for component in relative.parts:
        _portable_component(component)
    return "/".join(relative.parts)


def _is_reparse(mode: int, path: Path) -> bool:
    """Detect Windows reparse points without importing Windows-only modules on POSIX."""
    if stat.S_ISLNK(mode):
        return True
    attributes = getattr(os.stat_result, "st_file_attributes", None)
    if attributes is None:
        return False
    try:
        value = path.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(value & 0x400)


def _executable(mode: int) -> bool:
    """Return a portable executable marker for host and Linux helper observations.

    Docker Desktop exposes a Windows bind mount with Linux executable bits on every regular
    member, while Win32 ``stat`` reports those bits from the host ACL.  Treating every Windows
    member as executable makes the two trusted observations compare the same canonical tree;
    POSIX hosts retain their actual user-execute bit.
    """
    return os.name == "nt" or bool(mode & stat.S_IXUSR)


def _read_regular(path: Path, size: int, limit: int) -> str:
    """Hash one regular file while charging its logical size and detecting a race."""
    if size > limit:
        raise WorkspaceTreeError("workspace file exceeds its logical byte bound")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb", buffering=0) as handle:
            while True:
                chunk = handle.read(min(_CHUNK, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise WorkspaceTreeError("workspace file exceeds its logical byte bound")
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceTreeError(f"workspace file could not be read: {path.name!r}") from exc
    if after.st_size != size or not stat.S_ISREG(after.st_mode):
        raise WorkspaceTreeError("workspace changed while it was being hashed")
    return digest.hexdigest()


def snapshot_tree(  # noqa: PLR0912, PLR0915  # bounded validation keeps each rejection explicit
    root: Path,
    *,
    max_bytes: int,
    max_entries: int = MAX_TREE_ENTRIES,
    max_depth: int = MAX_TREE_DEPTH,
) -> TreeSnapshot:
    """Walk a regular tree without following links and compute a deterministic digest.

    The walk charges ``st_size`` rather than allocated blocks.  This makes sparse-file expansion
    and metadata-heavy trees fail closed before they are staged into a quota filesystem.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
        raise ValueError("max_entries must be positive")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth <= 0:
        raise ValueError("max_depth must be positive")
    root = Path(root)
    try:
        root_info = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceTreeError("workspace root could not be inspected") from exc
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info.st_mode, root):
        raise WorkspaceTreeError("workspace root is not a portable directory")
    root = root.resolve()
    if not root.is_dir():
        raise WorkspaceTreeError("workspace root is not a directory")
    entries: list[TreeEntry] = []
    logical_bytes = 0
    folded_paths: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise WorkspaceTreeError("workspace directory could not be read") from exc
        local_folded: set[str] = set()
        for child in children:
            path = Path(child.path)
            relative = _relative(root, path)
            folded = relative.casefold()
            if folded in folded_paths or folded in local_folded:
                raise WorkspaceTreeError(f"workspace contains a case-fold collision: {relative}")
            local_folded.add(folded)
            folded_paths.add(folded)
            if len(entries) >= max_entries:
                raise WorkspaceTreeError("workspace contains too many entries")
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceTreeError("workspace entry could not be inspected") from exc
            if _is_reparse(info.st_mode, path):
                raise WorkspaceTreeError(
                    f"workspace link or reparse point is not portable: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                depth = len(Path(relative).parts)
                if depth > max_depth:
                    raise WorkspaceTreeError("workspace nesting exceeds its depth bound")
                entries.append(TreeEntry(relative, "directory", _executable(info.st_mode), 0, None))
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceTreeError(f"workspace entry is not a regular file: {relative}")
            if getattr(info, "st_nlink", 1) != 1:
                raise WorkspaceTreeError(f"workspace hard links are not portable: {relative}")
            logical_bytes += info.st_size
            if logical_bytes > max_bytes:
                raise WorkspaceTreeError("workspace exceeds its logical byte bound")
            digest = _read_regular(path, info.st_size, max_bytes - (logical_bytes - info.st_size))
            entries.append(
                TreeEntry(relative, "file", _executable(info.st_mode), info.st_size, digest)
            )
    entries.sort(key=lambda entry: entry.relative_path)
    encoded = "".join(
        json.dumps(entry.identity_payload(), sort_keys=True, separators=(",", ":")) + "\n"
        for entry in entries
    ).encode()
    return TreeSnapshot(root, tuple(entries), logical_bytes, hashlib.sha256(encoded).hexdigest())


def copy_tree(snapshot: TreeSnapshot, destination: Path) -> TreeSnapshot:
    """Copy a previously validated tree into an empty directory with create-new semantics."""
    destination = Path(destination)
    if destination.exists():
        raise WorkspaceTreeError("tree destination must be empty")
    destination.mkdir(parents=True)
    for entry in snapshot.entries:
        target = destination / Path(entry.relative_path)
        if entry.kind == "directory":
            target.mkdir()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise WorkspaceTreeError(f"tree destination already contains {entry.relative_path}")
        source = snapshot.root / Path(entry.relative_path)
        with source.open("rb") as source_handle, target.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, _CHUNK)
        if entry.executable:
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return snapshot_tree(destination, max_bytes=max(snapshot.logical_bytes, 1))


class WorkspaceLock:
    """Cross-platform advisory lock stored beside, never inside, a writable workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace.parent / f".{self.workspace.name}.workspace.lock"
        self._handle: Any = None
        self._guard: threading.RLock | None = None

    def __enter__(self) -> WorkspaceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path)
        with _GUARDS_MUTEX:
            guard = _GUARDS.setdefault(key, threading.RLock())
        guard.acquire()
        self._guard = guard
        try:
            with _GUARDS_MUTEX:
                handle_and_count = _HANDLES.get(key)
                if handle_and_count is None:
                    handle = self.path.open("a+b")
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                    if os.name == "nt":
                        import msvcrt  # noqa: PLC0415  # platform-specific lock provider

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, _WINDOWS_LOCK_BYTE)
                    else:
                        import fcntl  # noqa: PLC0415  # platform-specific lock provider

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    _HANDLES[key] = (handle, 1)
                    self._handle = handle
                else:
                    handle, count = handle_and_count
                    _HANDLES[key] = (handle, count + 1)
                    self._handle = handle
        except BaseException:
            self._guard = None
            guard.release()
            raise
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self._handle is None or self._guard is None:
            return
        key = str(self.path)
        with _GUARDS_MUTEX:
            handle, count = _HANDLES[key]
            if count == 1:
                if os.name == "nt":
                    import msvcrt  # noqa: PLC0415  # platform-specific lock provider

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _WINDOWS_LOCK_BYTE)
                else:
                    import fcntl  # noqa: PLC0415  # platform-specific lock provider

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del _HANDLES[key]
            else:
                _HANDLES[key] = (handle, count - 1)
        self._handle = None
        self._guard.release()
        self._guard = None


def _journal_write(path: Path, payload: dict[str, Any]) -> None:
    """Replace a journal file atomically within its parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _digest_at(path: Path, limit: int) -> TreeSnapshot:
    """Take a bounded snapshot while keeping the helper's publication code small."""
    return snapshot_tree(path, max_bytes=limit)


def _valid_digest(value: Any) -> bool:
    """Return whether a journal digest has the exact canonical SHA-256 shape."""
    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def recover_publication(  # noqa: PLR0912, PLR0915  # each crash state fails closed explicitly
    journal_path: Path, *, workspace: Path, max_bytes: int
) -> str:
    """Recover one exact journal state, or fail closed while preserving conflicting trees."""
    journal_path = Path(journal_path)
    workspace = Path(workspace).resolve()
    if journal_path.resolve().parent != workspace.parent:
        raise WorkspaceTreeError("workspace publication journal is outside the workspace sibling")
    if not journal_path.exists():
        return "none"
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        live = Path(payload["live"])
        incoming = Path(payload["incoming"])
        backup = Path(payload["backup"])
        version = payload["version"]
        old_digest = payload["old_digest"]
        new_digest = payload["new_digest"]
        state = payload["state"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkspaceTreeError("workspace publication journal is malformed") from exc
    if (
        version != 1
        or not _valid_digest(old_digest)
        or not _valid_digest(new_digest)
        or live.resolve() != workspace
        or incoming.parent != workspace.parent
        or backup.parent != workspace.parent
        or not incoming.name.startswith(f".{workspace.name}.incoming-")
        or not backup.name.startswith(f".{workspace.name}.backup-")
        or state not in {"prepared", "live_renamed", "published", "committed"}
    ):
        raise WorkspaceTreeError("workspace publication journal is unsafe")

    def plain_directory(path: Path) -> bool:
        """Reject symlinked publication generations before any recovery mutation."""
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and not _is_reparse(info.st_mode, path)

    def matching_directory(path: Path, digest: str) -> bool:
        """Require one exact bounded generation digest."""
        return plain_directory(path) and _digest_at(path, max_bytes).tree_sha256 == digest

    if state == "prepared":
        if (
            plain_directory(live)
            and matching_directory(live, old_digest)
            and plain_directory(incoming)
            and not backup.exists()
            and matching_directory(incoming, new_digest)
        ):
            shutil.rmtree(incoming)
            journal_path.unlink(missing_ok=True)
            return "recovered"
        raise WorkspaceTreeError("prepared workspace publication cannot be recovered safely")

    if state in {"published", "committed"}:
        if not matching_directory(live, new_digest):
            raise WorkspaceTreeError("published workspace digest cannot be recovered safely")
        if backup.exists() and not matching_directory(backup, old_digest):
            raise WorkspaceTreeError("published workspace backup cannot be recovered safely")
        if backup.exists():
            shutil.rmtree(backup)
        journal_path.unlink(missing_ok=True)
        return "recovered"

    if state == "live_renamed":
        if plain_directory(live):
            if not matching_directory(live, new_digest) or not matching_directory(
                backup, old_digest
            ):
                raise WorkspaceTreeError("renamed workspace publication conflicts with its journal")
            shutil.rmtree(backup)
            journal_path.unlink(missing_ok=True)
            return "recovered"
        if (
            matching_directory(incoming, new_digest)
            and matching_directory(backup, old_digest)
            and not live.exists()
        ):
            incoming.replace(live)
            _journal_write(journal_path, {**payload, "state": "published"})
            shutil.rmtree(backup)
            journal_path.unlink(missing_ok=True)
            return "recovered"
    raise WorkspaceTreeError("workspace publication cannot be recovered safely")


def publish_tree(
    workspace: Path,
    incoming: Path,
    *,
    expected_old_digest: str,
    expected_new_digest: str,
    journal_path: Path,
    execution_id: str,
    max_bytes: int,
) -> str:
    """Publish a validated incoming tree with journaled, serializable replacement."""
    workspace = Path(workspace).resolve()
    incoming = Path(incoming).resolve()
    if incoming == workspace:
        raise WorkspaceTreeError("incoming tree must differ from the live workspace")
    if (
        incoming.parent != workspace.parent
        or Path(journal_path).resolve().parent != workspace.parent
        or not incoming.is_dir()
    ):
        raise WorkspaceTreeError("incoming tree must be a sibling directory")
    current = snapshot_tree(workspace, max_bytes=max_bytes)
    if current.tree_sha256 != expected_old_digest:
        raise WorkspaceTreeError("workspace changed before publication")
    incoming_snapshot = snapshot_tree(incoming, max_bytes=max_bytes)
    if incoming_snapshot.tree_sha256 != expected_new_digest:
        raise WorkspaceTreeError("incoming tree digest changed before publication")
    if current.tree_sha256 == incoming_snapshot.tree_sha256:
        shutil.rmtree(incoming)
        return "committed"
    backup = workspace.parent / f".{workspace.name}.backup-{execution_id}"
    if backup.exists():
        raise WorkspaceTreeError("publication backup identity is already in use")
    journal = {
        "version": 1,
        "state": "prepared",
        "live": str(workspace),
        "incoming": str(incoming),
        "backup": str(backup),
        "old_digest": expected_old_digest,
        "new_digest": expected_new_digest,
    }
    _journal_write(Path(journal_path), journal)
    workspace.replace(backup)
    _journal_write(Path(journal_path), {**journal, "state": "live_renamed"})
    incoming.replace(workspace)
    _journal_write(Path(journal_path), {**journal, "state": "published"})
    if snapshot_tree(workspace, max_bytes=max_bytes).tree_sha256 != expected_new_digest:
        raise WorkspaceTreeError("published workspace digest does not match the export")
    _journal_write(Path(journal_path), {**journal, "state": "committed"})
    shutil.rmtree(backup)
    Path(journal_path).unlink(missing_ok=True)
    return "committed"


__all__ = [
    "MAX_TREE_DEPTH",
    "MAX_TREE_ENTRIES",
    "TreeEntry",
    "TreeSnapshot",
    "WorkspaceLock",
    "WorkspaceTreeError",
    "copy_tree",
    "publish_tree",
    "recover_publication",
    "snapshot_tree",
]
