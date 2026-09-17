"""The ``ArtifactStore`` protocol: the interface the runtime uses to persist run outputs.

An artifact store maps logical names (``"metrics.json"``) to content-addressed files and records
each one as a :class:`~thymira.schemas.Artifact` (sha256, size, lineage, validity). Every backend
-- the local filesystem first, object storage later -- implements this protocol so the rest of the
runtime never depends on where bytes actually live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from thymira.schemas import Artifact, ArtifactKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ArtifactWrite:
    """One member of an atomic artifact publication batch.

    ``input_artifact_names`` may name another member of the same batch. The store resolves those
    names to the exact artifact ids allocated by that publication before making any member
    visible, which lets a derived artifact carry verifiable lineage to its source sidecar.
    """

    name: str
    data: bytes
    kind: ArtifactKind = ArtifactKind.OTHER
    media_type: str | None = None
    input_artifact_ids: tuple[str, ...] = ()
    input_artifact_names: tuple[str, ...] = ()
    execution_key: str | None = None


@runtime_checkable
class ArtifactStore(Protocol):
    """Persist, retrieve, verify and invalidate the artifacts a run produces."""

    def save_bytes(
        self,
        name: str,
        data: bytes,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Write ``data`` under ``name`` and register it, returning the recorded artifact.

        Args:
            name: Logical, store-relative name (forward slashes for sub-paths).
            data: The raw bytes to persist.
            produced_by: Id of the agent or tool call that produced the artifact.
            kind: What the artifact is, independently of its format.
            media_type: Optional MIME type.
            preserve_history: When true and ``name`` already exists, the previous revision is
                archived (kept in the manifest as an invalid artifact) before a new immutable
                content object is published for the logical name.

        The returned artifact URI identifies the immutable content object and may differ from the
        logical ``name`` used by later reads.
        """
        ...

    def save_batch(
        self,
        attachments: Mapping[str, bytes],
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Validate and publish a complete attachment batch atomically."""
        ...

    def save_artifact_batch(
        self,
        writes: Sequence[ArtifactWrite],
        *,
        produced_by: str,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Publish heterogeneous artifacts and their lineage in one atomic commit."""
        ...

    def save_text(
        self,
        name: str,
        text: str,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Persist ``text`` as UTF-8 bytes; see :meth:`save_bytes`."""
        ...

    def save_json(
        self,
        name: str,
        data: Any,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Persist ``data`` as canonical JSON so equal values hash alike; see :meth:`save_bytes`."""
        ...

    def load_bytes(self, name: str) -> bytes:
        """Return the raw bytes stored under ``name``."""
        ...

    def load_bytes_bounded(self, name: str, max_bytes: int) -> bytes:
        """Return bytes only when the stored file fits within ``max_bytes``."""
        ...

    def load_text(self, name: str) -> str:
        """Return the UTF-8 text stored under ``name``."""
        ...

    def load_json(self, name: str) -> Any:
        """Return the JSON value stored under ``name``."""
        ...

    def exists(self, name: str) -> bool:
        """Return whether ``name`` is registered, still valid and present on disk."""
        ...

    def get(self, name: str) -> Artifact | None:
        """Return the artifact recorded under ``name``, or ``None`` if it is unknown."""
        ...

    def set_lineage(
        self,
        name: str,
        *,
        input_artifact_ids: Sequence[str],
        execution_key: str,
    ) -> Artifact:
        """Attach reproducibility lineage to ``name`` and return the updated artifact.

        Raises:
            KeyError: If ``name`` is not registered.
        """
        ...

    def invalidate(self, names: Iterable[str], reason: str) -> list[str]:
        """Mark each still-valid name invalid with ``reason``; return the names actually changed."""
        ...

    def list_active(self) -> list[Artifact]:
        """Return the artifacts that are valid and present on disk."""
        ...

    def manifest(self) -> dict[str, Artifact]:
        """Return a copy of the full manifest, active and archived entries alike."""
        ...

    def verify(self) -> list[str]:
        """Return a list of problems (missing or modified files); empty when the store is intact."""
        ...
