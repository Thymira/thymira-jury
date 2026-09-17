"""Durable compare-and-set storage for one user settings layer."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from thymira.events import canonical_json
from thymira.schemas import SecretReference, SettingsSnapshot
from thymira.state._atomic import CrossProcessFileLock, fsync_directory, replace_with_retry


class SettingsError(RuntimeError):
    """Base error for the durable user settings store."""


class SettingsConflictError(SettingsError):
    """Raised when a compare-and-set update uses an old namespace revision."""

    def __init__(self, namespace: str, expected: int, current: SettingsSnapshot | None) -> None:
        self.namespace = namespace
        self.expected = expected
        self.current = current
        actual = 0 if current is None else current.revision
        super().__init__(
            f"settings namespace {namespace!r} changed: expected revision {expected}, "
            f"current revision is {actual}"
        )


class SettingsCorruptError(SettingsError):
    """Raised when the persisted settings document is malformed."""


_SettingsFileLock = CrossProcessFileLock


class LocalSettingsStore:
    """Persist user settings in one JSON document with namespace-level CAS.

    This store has one mutable user layer. It does not merge environment, project and user
    values, which would make the source of an effective setting ambiguous. Values are schema-led:
    non-secret JSON lives in ``values`` and operational secrets must be supplied as an explicit
    ``SecretReference`` in ``secret_refs``. Raw secret material is never converted to a marker or
    written to disk.

    Args:
        root: Directory that owns the settings document.
    """

    _DOCUMENT = "settings.json"
    _LOCK = ".settings.lock"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / self._DOCUMENT
        self._lock_path = self.root / self._LOCK
        self._guard = threading.RLock()

    def get(self, namespace: str) -> SettingsSnapshot | None:
        """Return one namespace snapshot without creating or rewriting the document."""
        _validate_namespace(namespace)
        with self._guard, _SettingsFileLock(self._lock_path):
            return self._read_document().get(namespace)

    def list(self) -> tuple[SettingsSnapshot, ...]:
        """Return all namespace snapshots in deterministic order."""
        with self._guard, _SettingsFileLock(self._lock_path):
            document = self._read_document()
            return tuple(document[name] for name in sorted(document))

    def put(
        self,
        namespace: str,
        values: Mapping[str, Any],
        *,
        expected_revision: int = 0,
        secret_refs: Mapping[str, SecretReference | Mapping[str, str]] | None = None,
    ) -> SettingsSnapshot:
        """Commit one namespace revision after an exact compare-and-set check.

        Raises:
            SettingsConflictError: if another writer advanced this namespace.
            ValueError: if the namespace, values or secret references are invalid.
        """
        _validate_namespace(namespace)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise TypeError("expected_revision must be an integer")
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        if not isinstance(values, Mapping):
            raise TypeError("settings values must be a mapping")
        refs = dict(secret_refs or {})
        with self._guard, _SettingsFileLock(self._lock_path):
            document = self._read_document()
            current = document.get(namespace)
            current_revision = 0 if current is None else current.revision
            if current_revision != expected_revision:
                raise SettingsConflictError(namespace, expected_revision, current)
            snapshot = SettingsSnapshot(
                namespace=namespace,
                revision=expected_revision + 1,
                values=dict(values),
                secret_refs=refs,
            )
            candidate = {**document, namespace: snapshot}
            self._write_document(candidate)
            return snapshot

    def delete(self, namespace: str, *, expected_revision: int) -> bool:
        """Delete one namespace with CAS, returning whether it existed."""
        _validate_namespace(namespace)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise TypeError("expected_revision must be an integer")
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        with self._guard, _SettingsFileLock(self._lock_path):
            document = self._read_document()
            current = document.get(namespace)
            current_revision = 0 if current is None else current.revision
            if current_revision != expected_revision:
                raise SettingsConflictError(namespace, expected_revision, current)
            if current is None:
                return False
            candidate = {name: value for name, value in document.items() if name != namespace}
            self._write_document(candidate)
            return True

    def _read_document(self) -> dict[str, SettingsSnapshot]:
        """Read and validate the whole settings document."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsCorruptError(f"cannot read {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsCorruptError("settings document must be a JSON object")
        if raw.get("version") != 1:
            raise SettingsCorruptError("unsupported settings document version")
        raw_namespaces = raw.get("namespaces")
        if not isinstance(raw_namespaces, dict):
            raise SettingsCorruptError("settings document namespaces must be a JSON object")
        document: dict[str, SettingsSnapshot] = {}
        try:
            document = dict(_parse_namespace(item) for item in raw_namespaces.items())
        except (TypeError, ValueError) as exc:
            raise SettingsCorruptError(f"settings document is invalid: {exc}") from exc
        return document

    def _write_document(self, document: Mapping[str, SettingsSnapshot]) -> None:
        """Atomically replace the settings document and prove its read-back."""
        payload = {"version": 1, "namespaces": {}}
        namespaces: dict[str, dict[str, Any]] = payload["namespaces"]
        for namespace, snapshot in document.items():
            namespaces[namespace] = snapshot.to_json_dict()
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
        readback = self._read_document()
        if readback != dict(document):
            raise SettingsError("settings document read-back differs from the committed snapshot")
        fsync_directory(self.root)


def _validate_namespace(namespace: str) -> None:
    """Validate a settings namespace through the persisted schema."""
    if not isinstance(namespace, str):
        raise TypeError("settings namespace must be a string")
    try:
        SettingsSnapshot(namespace=namespace, revision=0)
    except ValueError as exc:
        raise ValueError(f"invalid settings namespace {namespace!r}") from exc


def _parse_namespace(item: tuple[object, object]) -> tuple[str, SettingsSnapshot]:
    """Parse one persisted namespace entry with a key and snapshot that agree."""
    namespace, value = item
    if not isinstance(namespace, str):
        raise TypeError("namespace key must be a string")
    snapshot = SettingsSnapshot.model_validate(value)
    if snapshot.namespace != namespace:
        raise ValueError(f"namespace key {namespace!r} disagrees with its snapshot")
    return namespace, snapshot


__all__ = [
    "LocalSettingsStore",
    "SettingsConflictError",
    "SettingsCorruptError",
    "SettingsError",
]
