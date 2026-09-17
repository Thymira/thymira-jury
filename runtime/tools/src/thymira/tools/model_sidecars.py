"""Bounded, regular-file protocols for model-tool child exchange."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from pydantic import BaseModel, ValidationError

from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from thymira.state import ArtifactStore


MAX_MODEL_ARTIFACT_BYTES = 256 * 1024 * 1024
"""Largest model artifact a model-executing tool will stage or publish."""

MAX_SIDECAR_BYTES = 8 * 1024 * 1024
"""Largest JSON control record a model-executing child may return."""


def load_bounded_artifact(
    store: ArtifactStore, name: str, *, max_bytes: int = MAX_MODEL_ARTIFACT_BYTES, label: str
) -> bytes:
    """Load a registered artifact only when its recorded and actual size is bounded.

    The manifest size is checked before the store read, and the returned bytes are checked again
    after it. The second check protects callers from a stale or tampered manifest; callers still
    validate any content they interpret at the child boundary.
    """
    _validate_limit(max_bytes)
    artifact = store.get(name)
    if artifact is None or not artifact.valid:
        raise ToolExecutionError(f"{label} is missing or invalid")
    if artifact.size_bytes > max_bytes:
        raise ToolExecutionError(f"{label} exceeds {max_bytes} bytes")
    try:
        data = store.load_bytes(name)
    except (OSError, KeyError, ValueError) as exc:
        raise ToolExecutionError(f"{label} is missing or invalid") from exc
    if len(data) > max_bytes:
        raise ToolExecutionError(f"{label} exceeds {max_bytes} bytes")
    return data


def read_bounded_file(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Read a bounded regular file without accepting links or a replaced path.

    ``lstat`` and the descriptor ``fstat`` make the check independent of a child replacing a
    sidecar between the size check and the read. ``O_NOFOLLOW`` is used on platforms that expose
    it; descriptor identity and the reparse check cover Windows, where it is unavailable.
    """
    _validate_limit(max_bytes)
    source = Path(path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ToolExecutionError(f"{label} is missing or invalid") from exc
    _require_regular(before, source, label)
    if before.st_size > max_bytes:
        raise ToolExecutionError(f"{label} exceeds {max_bytes} bytes")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ToolExecutionError(f"{label} is missing or invalid") from exc
    try:
        current = os.fstat(descriptor)
        _require_regular(current, source, label)
        if not _same_file(before, current):
            _raise_replaced(label)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            data = handle.read(max_bytes + 1)
    except ToolExecutionError:
        raise
    except OSError as exc:
        raise ToolExecutionError(f"{label} is missing or invalid") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(data) > max_bytes:
        raise ToolExecutionError(f"{label} exceeds {max_bytes} bytes")
    return data


def read_bounded_json[Model: BaseModel](
    path: Path, model: type[Model], *, max_bytes: int, label: str
) -> Model:
    """Read and strictly validate a bounded JSON sidecar."""
    raw = read_bounded_file(path, max_bytes=max_bytes, label=label)
    try:
        return model.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise ToolExecutionError(f"{label} is missing or malformed") from exc


def _validate_limit(max_bytes: int) -> None:
    """Reject a nonsensical protocol limit before opening any file."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("bounded file limit must be a positive integer")


def _require_regular(metadata: os.stat_result, path: Path, label: str) -> None:
    """Require a regular, non-reparse file from either a path or an open descriptor."""
    if not stat.S_ISREG(metadata.st_mode):
        raise ToolExecutionError(f"{label} is not a regular file")
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if getattr(metadata, "st_file_attributes", 0) & reparse:
        raise ToolExecutionError(f"{label} must not be a reparse point")
    if path.is_symlink() or path.is_junction():
        raise ToolExecutionError(f"{label} must not be a link")


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    """Return whether two stat records identify the same opened file."""
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _raise_replaced(label: str) -> NoReturn:
    """Raise the stable error for a sidecar replaced between validation and opening."""
    raise ToolExecutionError(f"{label} changed while being read")


__all__ = [
    "MAX_MODEL_ARTIFACT_BYTES",
    "MAX_SIDECAR_BYTES",
    "load_bounded_artifact",
    "read_bounded_file",
    "read_bounded_json",
]
