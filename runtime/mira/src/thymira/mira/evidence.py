"""Run-scoped evidence projections and bounded artifact excerpts for MIRA."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Protocol

from thymira.events import canonical_json, redact_value, sha256_text, verify_events
from thymira.mira.preflight import EvidenceObservation
from thymira.schemas import Event, EventType, Evidence

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Artifact
    from thymira.state import ArtifactStore


_SHA256_HEX = frozenset("0123456789abcdef")
_SHA256_LENGTH = 64


class EvidenceReadError(ValueError):
    """Base error for a rejected or unavailable MIRA evidence read."""


class EvidenceReferenceError(EvidenceReadError):
    """Raised when an evidence reference is not a safe artifact reference for this Run."""


class EvidenceIntegrityError(EvidenceReadError):
    """Raised when stored artifact metadata or bytes fail verification."""


class EvidenceLimitError(EvidenceReadError):
    """Raised when an excerpt exceeds the reader's byte, character, or count bound."""


class EvidenceContentError(EvidenceReadError):
    """Raised when an artifact cannot be represented as bounded UTF-8 text."""


@dataclass(frozen=True, slots=True)
class EvidenceExcerpt:
    """A bounded, hash-pinned artifact excerpt supplied by a read-only boundary."""

    evidence: Evidence
    expected_sha256: str
    actual_sha256: str
    text: str
    truncated: bool


class EvidenceReader(Protocol):
    """Read an already-authorised artifact excerpt without any write capability."""

    def read_excerpt(self, evidence: Evidence, *, max_chars: int) -> EvidenceExcerpt:
        """Return one bounded excerpt for the supplied evidence reference."""
        ...


class ArtifactStoreEvidenceReader:
    """Read only hash-pinned, text artifacts belonging to one Run."""

    def __init__(
        self,
        store: ArtifactStore,
        run_id: str,
        *,
        max_bytes: int = 64 * 1024,
        max_excerpts: int = 8,
    ) -> None:
        if max_bytes <= 0 or max_excerpts <= 0:
            raise ValueError("evidence reader limits must be positive")
        self._store = store
        self._run_id = run_id
        self._max_bytes = max_bytes
        self._max_excerpts = max_excerpts
        self._excerpt_count = 0

    def read_excerpt(self, evidence: Evidence, *, max_chars: int) -> EvidenceExcerpt:
        """Read and redact one bounded artifact excerpt after verifying its identity and hash.

        Raises:
            EvidenceReadError: If the reference is not a permitted artifact or its content cannot
                be verified and bounded safely.
        """
        expected_sha256 = _validate_excerpt_request(
            evidence, max_chars, self._excerpt_count, self._max_excerpts
        )
        artifact = self._resolve_artifact(evidence.ref)
        self._validate_artifact(evidence, expected_sha256, artifact)
        self._excerpt_count += 1
        data, actual_sha256 = self._load_verified_bytes(artifact)
        safe_text = _redacted_text(artifact, data)
        truncated = len(safe_text) > max_chars
        text = safe_text[:max_chars]
        return EvidenceExcerpt(
            evidence=evidence,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            text=text,
            truncated=truncated,
        )

    def _validate_artifact(
        self, evidence: Evidence, expected_sha256: str, artifact: Artifact
    ) -> None:
        """Check Run ownership, validity, identity and the byte bound before loading content."""
        if artifact.run_id != self._run_id:
            raise EvidenceReferenceError(
                f"artifact {evidence.ref!r} belongs to another Run, not {self._run_id!r}"
            )
        if not artifact.valid:
            raise EvidenceReferenceError(f"artifact {artifact.name!r} is invalidated")
        if expected_sha256 != artifact.sha256:
            raise EvidenceIntegrityError(
                f"artifact {artifact.name!r} sha256 does not match the evidence reference"
            )
        if artifact.size_bytes > self._max_bytes:
            raise EvidenceLimitError(
                f"artifact {artifact.name!r} is {artifact.size_bytes} bytes; "
                f"maximum is {self._max_bytes}"
            )

    def _load_verified_bytes(self, artifact: Artifact) -> tuple[bytes, str]:
        """Load one manifest entry and verify its stored size and content digest."""
        try:
            data = self._store.load_bytes(artifact.name)
        except (KeyError, OSError, ValueError) as exc:
            raise EvidenceReadError(f"could not read artifact {artifact.name!r}: {exc}") from exc
        if len(data) > self._max_bytes:
            raise EvidenceLimitError(
                f"artifact {artifact.name!r} exceeds the {self._max_bytes}-byte read limit"
            )
        if len(data) != artifact.size_bytes:
            raise EvidenceIntegrityError(
                f"artifact {artifact.name!r} size does not match its manifest metadata"
            )
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != artifact.sha256:
            raise EvidenceIntegrityError(
                f"artifact {artifact.name!r} bytes fail sha256 verification"
            )
        return data, actual_sha256

    def _resolve_artifact(self, reference: str) -> Artifact:
        """Resolve an artifact id or safe store-relative name within the current Run."""
        by_id = next(
            (artifact for artifact in self._store.manifest().values() if artifact.id == reference),
            None,
        )
        if by_id is not None:
            return by_id
        _validate_reference_path(reference)
        artifact = self._store.get(reference)
        if artifact is None:
            raise EvidenceReferenceError(
                f"artifact {reference!r} is not registered for Run {self._run_id!r}"
            )
        return artifact


def build_evidence_observations(
    run_id: str,
    events: Sequence[Event],
    store: ArtifactStore | None,
) -> tuple[EvidenceObservation, ...]:
    """Project the authoritative Run log, artifact manifest, and tool results into observations."""
    event_tuple = tuple(events)
    chain = verify_events(event_tuple)
    observations: list[EvidenceObservation] = [
        EvidenceObservation(
            evidence=Evidence(
                kind="event",
                ref=f"seq:{event.seq}",
                sha256=event.hash,
                note=event.type.value,
            ),
            integrity_verified=chain.valid,
            observed_at=event.ts,
        )
        for event in event_tuple
    ]
    if store is not None:
        problems = set(store.verify())
        for name, artifact in sorted(store.manifest().items()):
            if artifact.run_id != run_id:
                continue
            observations.append(
                EvidenceObservation(
                    evidence=Evidence(
                        kind="artifact",
                        ref=name,
                        sha256=artifact.sha256,
                        note=redact_value(
                            f"id={artifact.id}; kind={artifact.kind.value}; "
                            f"size_bytes={artifact.size_bytes}; produced_by={artifact.produced_by}"
                        ),
                    ),
                    integrity_verified=(
                        artifact.valid
                        and f"missing artifact: {name}" not in problems
                        and f"modified artifact: {name}" not in problems
                    ),
                    observed_at=artifact.created_at,
                )
            )
    observations.extend(_tool_observations(run_id, event_tuple))
    return tuple(observations)


def artifact_manifest_sha256(store: ArtifactStore | None) -> str | None:
    """Return a stable digest of a Run's complete registered artifact manifest."""
    if store is None:
        return None
    manifest = {
        name: artifact.to_json_dict() for name, artifact in sorted(store.manifest().items())
    }
    return sha256_text(canonical_json(manifest))


def _tool_observations(run_id: str, events: Sequence[Event]) -> tuple[EvidenceObservation, ...]:
    """Project recorded tool results without treating a result hash as verified content."""
    observations: list[EvidenceObservation] = []
    for event in events:
        if event.run_id != run_id or event.type is not EventType.TOOL_COMPLETED:
            continue
        payload = event.payload
        reference = _tool_reference(payload.get("tool_call_id"), event.seq)
        digest = payload.get("result_sha256")
        safe_digest = digest if isinstance(digest, str) and _is_sha256(digest) else None
        tool_name = payload.get("tool")
        status = payload.get("status")
        observations.append(
            EvidenceObservation(
                evidence=Evidence(
                    kind="tool_call",
                    ref=reference,
                    sha256=safe_digest,
                    note=redact_value(f"tool={tool_name!s}; status={status!s}"),
                ),
                integrity_verified=None,
                observed_at=event.ts,
            )
        )
    return tuple(observations)


def _validate_reference_path(reference: str) -> None:
    """Reject rooted, escaping, or device-shaped artifact names before store lookup."""
    posix_reference = reference.replace("\\", "/")
    if PureWindowsPath(reference).anchor or PurePosixPath(posix_reference).is_absolute():
        raise EvidenceReferenceError(
            f"artifact reference must be relative to the Run store: {reference!r}"
        )
    parts = PurePosixPath(posix_reference).parts
    if not parts or any(part in {"", ".."} for part in parts) or any(":" in part for part in parts):
        raise EvidenceReferenceError(f"artifact reference is not a safe store path: {reference!r}")


def _is_sha256(value: str) -> bool:
    """Return whether a string is a lowercase SHA-256 digest."""
    return len(value) == _SHA256_LENGTH and all(character in _SHA256_HEX for character in value)


def _tool_reference(value: object, seq: int) -> str:
    """Choose a safe recorded tool-call id, falling back to its event sequence."""
    if isinstance(value, str) and value:
        return value
    return f"seq:{seq}"


def _validate_excerpt_request(
    evidence: Evidence, max_chars: int, excerpt_count: int, max_excerpts: int
) -> str:
    """Validate the identity and budget of one requested artifact excerpt."""
    if evidence.kind != "artifact":
        raise EvidenceReferenceError(
            f"MIRA evidence reader accepts artifact references, got {evidence.kind!r}"
        )
    if evidence.sha256 is None:
        raise EvidenceIntegrityError("artifact evidence must include its sha256")
    if max_chars <= 0:
        raise EvidenceLimitError("artifact excerpt max_chars must be positive")
    if excerpt_count >= max_excerpts:
        raise EvidenceLimitError(
            f"artifact excerpt limit exceeded for Run: maximum is {max_excerpts}"
        )
    return evidence.sha256


def _redacted_text(artifact: Artifact, data: bytes) -> str:
    """Decode and redact verified artifact bytes before returning them to an audit agent."""
    try:
        raw_text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceContentError(
            f"artifact {artifact.name!r} is not UTF-8 text and cannot be exposed to MIRA"
        ) from exc
    safe_text = redact_value(raw_text)
    if not isinstance(safe_text, str):
        raise EvidenceContentError(f"artifact {artifact.name!r} did not produce text")
    return safe_text


__all__ = [
    "ArtifactStoreEvidenceReader",
    "EvidenceContentError",
    "EvidenceExcerpt",
    "EvidenceIntegrityError",
    "EvidenceLimitError",
    "EvidenceReadError",
    "EvidenceReader",
    "EvidenceReferenceError",
    "artifact_manifest_sha256",
    "build_evidence_observations",
]
