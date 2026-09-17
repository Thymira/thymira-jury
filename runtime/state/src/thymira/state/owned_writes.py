"""Owner-checked facades for Run artifacts and checkpoints.

The underlying stores remain useful for read-only verification.  Graph composition receives these
facades when it is running under a live ``RunHandle`` so artifact manifests and checkpoint blobs
cannot outlive the process-owned Run lock by accepting a copied owner identity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.schemas import Artifact, ArtifactKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from thymira.state.artifact_store import ArtifactStore, ArtifactWrite
    from thymira.state.checkpoints import CheckpointRepository
    from thymira.state.lifecycle import RunHandle


class OwnedArtifactStore:
    """Delegate artifact writes only while the bound Run owner remains live."""

    def __init__(self, store: ArtifactStore, owner: RunHandle) -> None:
        self._store = store
        self._owner = owner

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
        """Save bytes after validating the live owner."""
        self._owner.assert_live()
        return self._store.save_bytes(
            name,
            data,
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

    def save_batch(
        self,
        attachments: Mapping[str, bytes],
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Save a complete attachment batch after validating the live owner."""
        self._owner.assert_live()
        return self._store.save_batch(
            attachments,
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

    def save_artifact_batch(
        self,
        writes: Sequence[ArtifactWrite],
        *,
        produced_by: str,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Save heterogeneous artifacts atomically after validating the live owner."""
        self._owner.assert_live()
        return self._store.save_artifact_batch(
            writes,
            produced_by=produced_by,
            preserve_history=preserve_history,
        )

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
        """Save text after validating the live owner."""
        self._owner.assert_live()
        return self._store.save_text(
            name,
            text,
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

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
        """Save JSON after validating the live owner."""
        self._owner.assert_live()
        return self._store.save_json(
            name,
            data,
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

    def set_lineage(
        self,
        name: str,
        *,
        input_artifact_ids: Sequence[str],
        execution_key: str,
    ) -> Artifact:
        """Set artifact lineage after validating the live owner."""
        self._owner.assert_live()
        return self._store.set_lineage(
            name,
            input_artifact_ids=input_artifact_ids,
            execution_key=execution_key,
        )

    def invalidate(self, names: Iterable[str], reason: str) -> list[str]:
        """Invalidate artifacts after validating the live owner."""
        self._owner.assert_live()
        return self._store.invalidate(names, reason)

    def load_bytes(self, name: str) -> bytes:
        """Read artifact bytes without requiring write ownership."""
        return self._store.load_bytes(name)

    def load_bytes_bounded(self, name: str, max_bytes: int) -> bytes:
        """Read bounded artifact bytes without requiring write ownership."""
        return self._store.load_bytes_bounded(name, max_bytes)

    def load_text(self, name: str) -> str:
        """Read artifact text without requiring write ownership."""
        return self._store.load_text(name)

    def load_json(self, name: str) -> Any:
        """Read artifact JSON without requiring write ownership."""
        return self._store.load_json(name)

    def exists(self, name: str) -> bool:
        """Check artifact presence without requiring write ownership."""
        return self._store.exists(name)

    def get(self, name: str) -> Artifact | None:
        """Read one artifact record without requiring write ownership."""
        return self._store.get(name)

    def list_active(self) -> list[Artifact]:
        """List active artifacts without requiring write ownership."""
        return self._store.list_active()

    def manifest(self) -> dict[str, Artifact]:
        """Read the artifact manifest without requiring write ownership."""
        return self._store.manifest()

    def verify(self) -> list[str]:
        """Verify artifact bytes without requiring write ownership."""
        return self._store.verify()


class OwnedCheckpointRepository:
    """Delegate checkpoint writes only while the bound Run owner remains live."""

    def __init__(self, repository: CheckpointRepository, owner: RunHandle) -> None:
        self._repository = repository
        self._owner = owner

    def put(self, thread_id: str, checkpoint_ns: str, checkpoint: bytes) -> None:
        """Persist a checkpoint after validating the live owner."""
        self._owner.assert_live()
        self._repository.put(thread_id, checkpoint_ns, checkpoint)

    def get(self, thread_id: str, checkpoint_ns: str) -> bytes | None:
        """Read a checkpoint without requiring write ownership."""
        return self._repository.get(thread_id, checkpoint_ns)

    def list(self, thread_id: str, checkpoint_ns: str) -> tuple[str, ...]:
        """List checkpoint namespaces without requiring write ownership."""
        return self._repository.list(thread_id, checkpoint_ns)


__all__ = ["OwnedArtifactStore", "OwnedCheckpointRepository"]
