"""Deterministic hashing primitives shared by the event log and the artifact store.

Every hash in Thymira rests on ``canonical_json``: two equal structures must serialise to
exactly the same string regardless of key order, or the chain would break spuriously.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from thymira.schemas import AuthorizationContext, Event

_CHUNK = 64 * 1024

PDF_EXPORT_EXECUTION_VERSION = 1
"""Current canonical binding version for an ``export_pdf`` artifact pair."""


def canonical_json(data: Any) -> str:
    """Serialise ``data`` canonically: sorted keys, compact separators, UTF-8 kept verbatim."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    """Hex SHA-256 of a string encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, streamed in chunks so large artifacts do not load into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_event(event: Event) -> str:
    """Hash of an event's content — everything except its own ``hash`` field."""
    return sha256_text(canonical_json(event.hashable_dict()))


def hash_authorization_context(context: AuthorizationContext) -> str:
    """Hash only the stable, authorization-semantic content of a context."""
    return sha256_text(canonical_json(context.semantic_dict()))


def hash_pdf_export_execution(
    *,
    version: int,
    tool: str,
    source_name: str,
    source_sha256: str,
    output_name: str,
    output_sha256: str,
) -> str:
    """Bind one PDF export to its exact source, output, tool, and contract version."""
    return sha256_text(
        canonical_json(
            {
                "output": {"name": output_name, "sha256": output_sha256},
                "source": {"name": source_name, "sha256": source_sha256},
                "tool": tool,
                "version": version,
            }
        )
    )
