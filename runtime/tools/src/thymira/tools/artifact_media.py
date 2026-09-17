"""Deterministic MIME inference for artifacts published by runtime tools."""

from __future__ import annotations

from pathlib import PurePosixPath

_TEXT_MEDIA_TYPES_BY_SUFFIX = {
    ".csv": "text/csv",
    ".ipynb": "application/x-ipynb+json",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
}

_BINARY_MEDIA_TYPES_BY_SUFFIX = {
    ".joblib": "application/octet-stream",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".pkl": "application/octet-stream",
    ".png": "image/png",
}


def infer_artifact_media_type(name: str) -> str | None:
    """Return the stable MIME type for a common run-output suffix, when known."""
    suffix = _suffix(name)
    return _TEXT_MEDIA_TYPES_BY_SUFFIX.get(suffix) or _BINARY_MEDIA_TYPES_BY_SUFFIX.get(suffix)


def infer_text_artifact_media_type(name: str) -> str | None:
    """Return a MIME type only when a UTF-8 text writer can truthfully produce it."""
    return _TEXT_MEDIA_TYPES_BY_SUFFIX.get(_suffix(name))


def _suffix(name: str) -> str:
    """Normalize either platform's separators before reading a suffix."""
    return PurePosixPath(name.replace("\\", "/")).suffix.casefold()


__all__ = ["infer_artifact_media_type", "infer_text_artifact_media_type"]
