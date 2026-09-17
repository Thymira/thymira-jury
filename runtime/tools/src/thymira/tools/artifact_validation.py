"""Independent content validation for artifacts whose MIME participates in completion."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from thymira.schemas import ArtifactKind
from thymira.tools.artifact_media import infer_artifact_media_type

MAX_GENERIC_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

_REPORT_EXTENSIONS = frozenset({"md", "markdown", "txt", "pdf", "csv", "json"})
"""Extensions MIRA's A18 control (``thymira.mira.checks.report``) can classify and audit."""

_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/x-ipynb+json",
    "text/csv",
    "text/markdown",
    "text/plain",
}
_IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}


def media_type_base(value: str | None) -> str | None:
    """Return a canonical MIME base, rejecting blank or malformed declared values."""
    if value is None:
        return None
    base = value.partition(";")[0].strip().casefold()
    if not base or base.count("/") != 1 or any(character.isspace() for character in base):
        raise ValueError("media type must be a non-empty type/subtype value")
    return base


def resolve_artifact_media_type(name: str, declared: str | None) -> str | None:
    """Resolve a canonical MIME without allowing metadata to contradict a known suffix."""
    inferred = infer_artifact_media_type(name)
    declared_base = media_type_base(declared)
    if inferred is not None and declared_base is not None and inferred != declared_base:
        raise ValueError(
            f"declared media type {declared_base!r} conflicts with {name!r} ({inferred})"
        )
    return inferred or declared_base


def validate_report_kind(name: str, kind: ArtifactKind) -> None:
    """Reject registering a REPORT whose extension MIRA's A18 control cannot classify.

    A18 (``thymira.mira.checks.report._report_family``) only recognises Markdown, plain text,
    PDF, CSV and JSON REPORT artifacts; anything else fails the run's audit after the fact with
    an unreadable ``unsupported extension`` finding. Reject it here instead, at the tool call
    that would register it, so the producing agent gets an actionable error immediately.
    """
    if kind is not ArtifactKind.REPORT:
        return
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.casefold().removeprefix(".")
    if suffix not in _REPORT_EXTENSIONS:
        raise ValueError(
            f"cannot register {name!r} as a REPORT: extension {suffix or '<none>'!r} is not "
            f"one of {sorted(_REPORT_EXTENSIONS)}, which MIRA's A18 control can audit"
        )


def artifact_read_limit(media_type: str | None) -> int:
    """Return the pre-read ceiling for one artifact of the given canonical MIME."""
    base = media_type_base(media_type)
    if base == "application/pdf":
        return MAX_PDF_BYTES
    if base in _IMAGE_FORMATS:
        return MAX_IMAGE_BYTES
    return MAX_GENERIC_ARTIFACT_BYTES


def validate_artifact_bytes(name: str, data: bytes, media_type: str | None) -> None:
    """Validate bounded bytes independently of the producer that labelled the artifact."""
    base = media_type_base(media_type)
    limit = artifact_read_limit(base)
    if len(data) > limit:
        label = base or "unknown"
        raise ValueError(f"artifact {name!r} exceeds the {limit}-byte limit for {label}")
    if base == "application/pdf":
        _validate_pdf(name, data)
    elif base in _IMAGE_FORMATS:
        _validate_image(name, data, expected_format=_IMAGE_FORMATS[base])
    elif base in _TEXT_MEDIA_TYPES:
        _validate_text(name, data, media_type=base)


def _validate_pdf(name: str, data: bytes) -> None:
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"artifact {name!r} is not a valid PDF")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) < 1:
            raise ValueError(f"artifact {name!r} is not a readable PDF")
    except PdfReadError as exc:
        raise ValueError(f"artifact {name!r} is not a valid PDF") from exc


def _validate_image(name: str, data: bytes, *, expected_format: str) -> None:
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != expected_format:
                raise ValueError(f"artifact {name!r} is not a valid {expected_format}")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    f"artifact {name!r} exceeds the {MAX_IMAGE_PIXELS}-pixel image limit"
                )
            image.verify()
    except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError) as exc:
        raise ValueError(f"artifact {name!r} is not a valid {expected_format}") from exc


def _validate_text(name: str, data: bytes, *, media_type: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"artifact {name!r} is not valid UTF-8 {media_type}") from exc
    if media_type not in {"application/json", "application/x-ipynb+json"}:
        return
    try:
        json.loads(text, parse_constant=_reject_non_finite_json)
    except ValueError as exc:
        raise ValueError(f"artifact {name!r} is not valid JSON") from exc


def _reject_non_finite_json(value: str) -> None:
    """Reject non-standard JSON constants accepted by the stdlib decoder."""
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


__all__ = [
    "MAX_GENERIC_ARTIFACT_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_PDF_BYTES",
    "artifact_read_limit",
    "media_type_base",
    "resolve_artifact_media_type",
    "validate_artifact_bytes",
    "validate_report_kind",
]
