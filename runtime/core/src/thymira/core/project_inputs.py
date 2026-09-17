"""Project inputs an operator edits between Runs: ``.thymira/context.md`` and declared datasets.

Every Run re-reads both when it starts. Inspect registers each dataset ``config.yaml`` declares
into the Run's own artifact store, recording the ``source_path`` that makes it a registered source
MIRA's A18 accepts, and copies it into the Run workspace; the risk interview, THY's plan and every
delegated step read the context document. Editing them here therefore changes what the next Run
sees. A Run that already registered a dataset keeps its own content-addressed copy, so its evidence
never changes underneath it.

Every write is atomic (a temporary file beside the target, then a replace), confined to the
project directory, and validated before it lands. The context document is compare-and-set on its
sha256, so two editors cannot silently overwrite each other. A dataset must be a CSV or Parquet
file that registration can read, and it is declared only when the whole configuration still
validates as a :class:`~thymira.schemas.ProjectConfig`.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from thymira.schemas import DatasetConfig, ProjectConfig
from thymira.tools.datasets import MAX_DATASET_BYTES, dataset_columns, describe_dataset_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import BufferedWriter

THYMIRA_DIRECTORY = ".thymira"
CONTEXT_FILE = "context.md"
CONFIG_FILE = "config.yaml"
DATASET_DIRECTORY = "data"
DATASET_SUFFIXES = frozenset({".csv", ".parquet"})

MAX_CONTEXT_BYTES = 256 * 1024
"""Largest context document accepted; every model request in a Run carries it."""

MAX_DATASET_UPLOAD_BYTES = MAX_DATASET_BYTES
"""An upload may be as large as registration will read, and no larger."""

REDACTION_MARKER = "[REDACTED:"
"""The prefix of every export-redaction mask; a save carrying one would write a mask over data."""

_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY_SECONDS = 0.05
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ProjectInputError(ValueError):
    """A project input change was refused; the message names the rule it broke."""


class ProjectInputConflictError(ProjectInputError):
    """The context document changed after the caller read it."""


class ProjectInputNotFoundError(ProjectInputError):
    """The project declares no dataset with the requested name."""


class ProjectInputTooLargeError(ProjectInputError):
    """An input is larger than its bound."""


@dataclass(frozen=True, slots=True)
class ContextDocument:
    """The project's context document as stored: its text and the sha256 of its bytes."""

    text: str
    sha256: str
    exists: bool


@dataclass(frozen=True, slots=True)
class DeclaredDataset:
    """One dataset ``config.yaml`` declares, and what is on disk at its path."""

    name: str
    path: str
    target: str | None
    present: bool
    size_bytes: int | None
    modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class DatasetUploadResult:
    """A declared dataset after an upload, with the shape registration will capture."""

    dataset: DeclaredDataset
    rows: int
    columns: tuple[str, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock_for(directory: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(directory, threading.Lock())


def _replace(source: Path, target: Path) -> None:
    """Move ``source`` over ``target``, retrying the sharing violations Windows reports."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            source.replace(target)
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_SECONDS)
        else:
            return


def _atomic_write(target: Path, data: bytes) -> None:
    """Publish ``data`` at ``target`` through a flushed temporary file and one replace."""
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _header_comments(text: str) -> str:
    """Return the comment block that opens a YAML file, so a rewrite keeps it."""
    lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#") and line.strip():
            break
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n\n" if lines else ""


def _dataset_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Copy the raw ``datasets`` entries of a loaded configuration."""
    raw = data.get("datasets") or []
    if not isinstance(raw, list):
        raise ProjectInputError("config.yaml 'datasets' must be a list")
    return [dict(item) for item in raw if isinstance(item, dict)]


def _validate_name(name: str) -> None:
    try:
        DatasetConfig(name=name, path=f"{DATASET_DIRECTORY}/dataset.csv")
    except ValidationError as exc:
        msg = (
            "a dataset name uses lowercase letters, digits, '_' and '-', and starts with a "
            f"letter or a digit: {name!r}"
        )
        raise ProjectInputError(msg) from exc


class DatasetUpload:
    """One dataset upload in progress: stream chunks into a staging file, then commit or abort."""

    def __init__(
        self,
        staging: Path,
        filename: str,
        publish: Callable[[Path, str, str | None], DatasetUploadResult],
    ) -> None:
        self._staging = staging
        self._filename = filename
        self._publish = publish
        self._handle: BufferedWriter | None = staging.open("xb")
        self._size = 0

    def write(self, chunk: bytes) -> None:
        """Append one chunk, refusing the upload once it passes the size bound."""
        if self._handle is None:
            raise ProjectInputError("the upload is already closed")
        self._size += len(chunk)
        if self._size > MAX_DATASET_UPLOAD_BYTES:
            msg = f"a dataset upload may not exceed {MAX_DATASET_UPLOAD_BYTES} bytes"
            raise ProjectInputTooLargeError(msg)
        self._handle.write(chunk)

    def commit(self, *, target: str | None = None) -> DatasetUploadResult:
        """Check, publish and declare the staged file; the staging file never outlives the call."""
        try:
            self._close()
            if self._size == 0:
                raise ProjectInputError("the uploaded file is empty")
            return self._publish(self._staging, self._filename, target)
        finally:
            self.abort()

    def abort(self) -> None:
        """Discard whatever was staged; safe to call more than once."""
        self._close()
        self._staging.unlink(missing_ok=True)

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class ProjectInputs:
    """Read and change one project's context document and declared datasets."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = Path(project_dir).resolve()
        self._config_path = self._project_dir / THYMIRA_DIRECTORY / CONFIG_FILE
        self._context_path = self._project_dir / THYMIRA_DIRECTORY / CONTEXT_FILE
        self._lock = _lock_for(self._project_dir)

    @property
    def project_dir(self) -> Path:
        """The directory that contains ``.thymira`` and every declared dataset path."""
        return self._project_dir

    # -- context document ----------------------------------------------------------------------

    def read_context(self) -> ContextDocument:
        """Return the context document, or an empty, absent one when the project has none."""
        if not self._context_path.is_file():
            return ContextDocument(text="", sha256=_sha256(b""), exists=False)
        data = self._context_path.read_bytes()
        return ContextDocument(text=data.decode("utf-8"), sha256=_sha256(data), exists=True)

    def write_context(self, text: str, *, expected_sha256: str) -> ContextDocument:
        """Replace the context document when it still has the digest the caller read.

        Raises:
            ProjectInputTooLargeError: The text is over :data:`MAX_CONTEXT_BYTES`.
            ProjectInputError: The text carries an export-redaction mask.
            ProjectInputConflictError: The stored document no longer has ``expected_sha256``.
        """
        data = text.encode("utf-8")
        if len(data) > MAX_CONTEXT_BYTES:
            msg = f"the context document may not exceed {MAX_CONTEXT_BYTES} bytes"
            raise ProjectInputTooLargeError(msg)
        if REDACTION_MARKER in text:
            msg = (
                "the text contains an export-redaction mask; edit .thymira/context.md directly "
                "so the values behind the masks are not overwritten"
            )
            raise ProjectInputError(msg)
        with self._lock:
            if self.read_context().sha256 != expected_sha256:
                msg = "the context document changed after it was read; reload it before saving"
                raise ProjectInputConflictError(msg)
            self._context_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._context_path, data)
        return ContextDocument(text=text, sha256=_sha256(data), exists=True)

    # -- declared datasets ---------------------------------------------------------------------

    def datasets(self) -> tuple[DeclaredDataset, ...]:
        """Return every declared dataset, in declaration order."""
        _, _, config = self._load_config()
        return tuple(self._describe(dataset) for dataset in config.datasets)

    def begin_upload(self, name: str, filename: str) -> DatasetUpload:
        """Start streaming a CSV or Parquet file that will be declared under ``name``.

        Raises:
            ProjectInputError: The name is not a valid dataset name or the file is neither CSV
                nor Parquet.
        """
        _validate_name(name)
        suffix = PurePosixPath(filename.replace("\\", "/")).suffix.casefold()
        if suffix not in DATASET_SUFFIXES:
            raise ProjectInputError("datasets must be CSV or Parquet files")
        directory = self._contained(self._project_dir / DATASET_DIRECTORY)
        directory.mkdir(parents=True, exist_ok=True)
        staging = directory / f".upload-{uuid.uuid4().hex}{suffix}"
        return DatasetUpload(staging, filename, partial(self._publish, name, suffix))

    def set_target(self, name: str, target: str | None) -> DeclaredDataset:
        """Set, or with ``None`` clear, the column a declared dataset predicts.

        Raises:
            ProjectInputNotFoundError: No dataset is declared under ``name``.
            ProjectInputError: ``target`` is not a column of the dataset file.
        """
        with self._lock:
            text, data, config = self._load_config()
            declared = self._declared(config, name)
            if target is not None:
                source = self._resolve_declared(declared.path)
                try:
                    columns = dataset_columns(source)
                except (OSError, ValueError) as exc:
                    raise ProjectInputError(f"the dataset file cannot be read: {exc}") from exc
                if target not in columns:
                    msg = f"target {target!r} is not a column of {declared.path}"
                    raise ProjectInputError(msg)
            entries = _dataset_entries(data)
            for entry in entries:
                if entry.get("name") != name:
                    continue
                if target is None:
                    entry.pop("target", None)
                else:
                    entry["target"] = target
            _atomic_write(self._config_path, self._render(text, data, entries))
            updated = next(entry for entry in entries if entry.get("name") == name)
        return self._describe(DatasetConfig.model_validate(updated))

    def remove_dataset(self, name: str) -> tuple[DeclaredDataset, ...]:
        """Stop declaring a dataset. Its file stays, because a past Run's evidence names it.

        Raises:
            ProjectInputNotFoundError: No dataset is declared under ``name``.
        """
        with self._lock:
            text, data, config = self._load_config()
            self._declared(config, name)
            entries = [entry for entry in _dataset_entries(data) if entry.get("name") != name]
            _atomic_write(self._config_path, self._render(text, data, entries))
        return self.datasets()

    # -- internals -----------------------------------------------------------------------------

    def _publish(
        self, name: str, suffix: str, staging: Path, filename: str, target: str | None
    ) -> DatasetUploadResult:
        """Validate a staged upload, move it to its declared path and declare it."""
        try:
            summary = describe_dataset_file(staging)
        except (OSError, ValueError) as exc:
            reason = str(exc).replace(staging.name, filename)
            raise ProjectInputError(
                f"the file cannot be registered as a dataset: {reason}"
            ) from exc
        if target is not None and target not in summary.columns:
            raise ProjectInputError(f"target {target!r} is not a column of {filename}")
        with self._lock:
            text, data, config = self._load_config()
            existing = next((dataset for dataset in config.datasets if dataset.name == name), None)
            relative = f"{DATASET_DIRECTORY}/{name}{suffix}"
            if existing is not None and PurePosixPath(existing.path).suffix.casefold() == suffix:
                relative = existing.path
            entry: dict[str, Any] = {"name": name, "path": relative}
            kept = existing.target if existing is not None else None
            chosen = target if target is not None else kept
            if chosen is not None and chosen in summary.columns:
                entry["target"] = chosen
            entries = [item for item in _dataset_entries(data) if item.get("name") != name]
            position = next(
                (
                    index
                    for index, item in enumerate(_dataset_entries(data))
                    if item.get("name") == name
                ),
                len(entries),
            )
            entries.insert(position, entry)
            rendered = self._render(text, data, entries)
            destination = self._resolve_declared(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _replace(staging, destination)
            _atomic_write(self._config_path, rendered)
        dataset = self._describe(DatasetConfig.model_validate(entry))
        return DatasetUploadResult(dataset=dataset, rows=summary.rows, columns=summary.columns)

    def _load_config(self) -> tuple[str, dict[str, Any], ProjectConfig]:
        try:
            text = self._config_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ProjectInputError("the project has no .thymira/config.yaml") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ProjectInputError(f"config.yaml is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectInputError("config.yaml must contain a mapping")
        try:
            config = ProjectConfig.model_validate(data)
        except ValidationError as exc:
            raise ProjectInputError(f"config.yaml is invalid: {exc}") from exc
        return text, data, config

    @staticmethod
    def _render(text: str, data: dict[str, Any], entries: list[dict[str, Any]]) -> bytes:
        """Render a configuration with ``entries`` as its datasets, refusing an invalid result."""
        updated = {**data, "datasets": entries}
        try:
            ProjectConfig.model_validate(updated)
        except ValidationError as exc:
            raise ProjectInputError(f"the change would make config.yaml invalid: {exc}") from exc
        body = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)
        return (_header_comments(text) + body).encode("utf-8")

    @staticmethod
    def _declared(config: ProjectConfig, name: str) -> DatasetConfig:
        for dataset in config.datasets:
            if dataset.name == name:
                return dataset
        raise ProjectInputNotFoundError(f"the project declares no dataset named {name!r}")

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self._project_dir):
            raise ProjectInputError(f"{path} leaves the project directory")
        return resolved

    def _resolve_declared(self, relative: str) -> Path:
        parts = PurePosixPath(relative.replace("\\", "/")).parts
        return self._contained(self._project_dir.joinpath(*parts))

    def _describe(self, dataset: DatasetConfig) -> DeclaredDataset:
        parts = PurePosixPath(dataset.path.replace("\\", "/")).parts
        path = self._project_dir.joinpath(*parts)
        try:
            stat = path.stat() if path.is_file() else None
        except OSError:
            stat = None
        return DeclaredDataset(
            name=dataset.name,
            path=dataset.path,
            target=dataset.target,
            present=stat is not None,
            size_bytes=stat.st_size if stat is not None else None,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC) if stat is not None else None,
        )


__all__ = [
    "CONFIG_FILE",
    "CONTEXT_FILE",
    "DATASET_DIRECTORY",
    "DATASET_SUFFIXES",
    "MAX_CONTEXT_BYTES",
    "MAX_DATASET_UPLOAD_BYTES",
    "ContextDocument",
    "DatasetUpload",
    "DatasetUploadResult",
    "DeclaredDataset",
    "ProjectInputConflictError",
    "ProjectInputError",
    "ProjectInputNotFoundError",
    "ProjectInputTooLargeError",
    "ProjectInputs",
]
