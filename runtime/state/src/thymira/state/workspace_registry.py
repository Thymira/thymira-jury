"""Canonical workspace identities and metadata-only registry operations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from thymira.events import canonical_json
from thymira.schemas import Id, WorkspaceRegistration
from thymira.state._atomic import fsync_directory, replace_with_retry
from thymira.state.settings import _SettingsFileLock


def canonical_workspace_path(workspace: Path) -> Path:
    """Resolve one workspace path without following a missing final component."""
    return Path(workspace).expanduser().resolve(strict=False)


def workspace_identity(workspace: Path) -> str:
    """Return the stable, platform-normalized identity string for ``workspace``."""
    path = canonical_workspace_path(workspace)
    value = path.as_posix()
    return os.path.normcase(value)


def workspace_id(workspace: Path) -> str:
    """Return the deterministic registry id for a canonical workspace path."""
    return f"workspace_{hashlib.sha256(workspace_identity(workspace).encode()).hexdigest()[:32]}"


class LocalWorkspaceRegistry:
    """Persist workspace metadata while leaving the registered user's files untouched.

    The registry stores only canonical paths and project ids in its own state directory. Deleting
    a registration removes that metadata record; it never recursively deletes the workspace.
    """

    _DOCUMENT = "registry.json"
    _LOCK = ".registry.lock"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / self._DOCUMENT
        self._lock_path = self.root / self._LOCK

    def register(self, workspace: Path, project_id: Id) -> WorkspaceRegistration:
        """Register a workspace by canonical identity and return its metadata."""
        canonical = canonical_workspace_path(workspace)
        identity = workspace_identity(canonical)
        with _SettingsFileLock(self._lock_path):
            document = self._read_document()
            existing = next(
                (record for record in document.values() if record.identity == identity), None
            )
            record = (
                existing.model_copy(update={"project_id": project_id})
                if existing is not None
                else WorkspaceRegistration(
                    workspace_id=workspace_id(canonical),
                    identity=identity,
                    workspace=identity,
                    project_id=project_id,
                )
            )
            candidate = {**document, record.workspace_id: record}
            self._write_document(candidate)
            return record

    def get(self, workspace: Path) -> WorkspaceRegistration | None:
        """Return a registration by canonical workspace identity."""
        identity = workspace_identity(workspace)
        with _SettingsFileLock(self._lock_path):
            records = self._read_document().values()
            return next(
                (record for record in records if record.identity == identity),
                None,
            )

    def list(self, *, project_id: Id | None = None) -> tuple[WorkspaceRegistration, ...]:
        """List registrations in deterministic id order."""
        with _SettingsFileLock(self._lock_path):
            records = tuple(self._read_document().values())
        if project_id is not None:
            records = tuple(record for record in records if record.project_id == project_id)
        return tuple(sorted(records, key=lambda record: record.workspace_id))

    def delete(self, workspace_or_id: Path | str) -> bool:
        """Remove registry metadata, preserving every file under the workspace path."""
        with _SettingsFileLock(self._lock_path):
            document = self._read_document()
            if isinstance(workspace_or_id, Path):
                target = workspace_id(workspace_or_id)
            elif workspace_or_id.startswith("workspace_"):
                target = workspace_or_id
            else:
                target = workspace_id(Path(workspace_or_id))
            if target not in document:
                return False
            candidate = {key: value for key, value in document.items() if key != target}
            self._write_document(candidate)
            return True

    def remove(self, workspace_or_id: Path | str) -> bool:
        """Compatibility alias for :meth:`delete`."""
        return self.delete(workspace_or_id)

    def _read_document(self) -> dict[str, WorkspaceRegistration]:
        """Read and validate the registry document."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            records = _parse_records(raw)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"workspace registry is invalid: {exc}") from exc
        if any(key != record.workspace_id for key, record in records.items()):
            raise ValueError("workspace registry key disagrees with workspace_id")
        return records

    def _write_document(self, records: dict[str, WorkspaceRegistration]) -> None:
        """Atomically publish a complete registry snapshot and verify its read-back."""
        payload = {
            "version": 1,
            "workspaces": {key: record.to_json_dict() for key, record in records.items()},
        }
        temporary = self._path.with_name(f".{self._DOCUMENT}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            replace_with_retry(temporary, self._path)
        finally:
            if temporary.exists():
                temporary.unlink()
        if self._read_document() != records:
            raise ValueError("workspace registry read-back differs from the committed snapshot")
        fsync_directory(self.root)


def _parse_record(item: tuple[object, object]) -> tuple[str, WorkspaceRegistration]:
    """Parse one persisted registry entry with a key matching its record id."""
    key, value = item
    if not isinstance(key, str):
        raise TypeError("workspace registry key must be a string")
    return key, WorkspaceRegistration.model_validate(value)


def _parse_records(raw: object) -> dict[str, WorkspaceRegistration]:
    """Parse the registry document's workspace mapping."""
    if not isinstance(raw, dict):
        raise TypeError("workspace registry must be a JSON object")
    if raw.get("version") != 1:
        raise ValueError("unsupported workspace registry version")
    values = raw.get("workspaces")
    if not isinstance(values, dict):
        raise TypeError("workspaces must be a JSON object")
    return dict(_parse_record(item) for item in values.items())


__all__ = [
    "LocalWorkspaceRegistry",
    "canonical_workspace_path",
    "workspace_id",
    "workspace_identity",
]
