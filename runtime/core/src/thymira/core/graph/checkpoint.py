"""LangGraph checkpoint saver backed by the runtime state repository."""

from __future__ import annotations

import base64
import binascii
import json
from threading import RLock
from typing import TYPE_CHECKING, Any, TypedDict, cast

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
)

from thymira.events import canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from langchain_core.runnables import RunnableConfig

    from thymira.state import CheckpointRepository


class _StoredCheckpoint(TypedDict):
    """Serializable envelope persisted as one repository blob."""

    config: dict[str, Any]
    checkpoint: Checkpoint
    metadata: CheckpointMetadata
    parent_config: dict[str, Any] | None
    pending_writes: list[PendingWrite]


class StateCheckpointer(BaseCheckpointSaver[Any]):
    """Persist the latest LangGraph checkpoint for each ``(thread_id, checkpoint_ns)`` coordinate.

    A LangGraph thread can hold more than one checkpoint namespace at once -- the root namespace
    and one per nested subgraph task -- so every operation below is keyed on both halves of that
    composite identity, never on ``thread_id`` alone.
    """

    def __init__(self, repository: CheckpointRepository) -> None:
        super().__init__()
        self._repository = repository
        self._lock = RLock()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the latest checkpoint matching ``config`` or ``None``."""
        thread_id, checkpoint_ns = _checkpoint_key(config)
        with self._lock:
            payload = self._repository.get(thread_id, checkpoint_ns)
        if payload is None:
            return None
        stored = _decode(payload, self.serde)
        checkpoint_id = config["configurable"].get("checkpoint_id")
        stored_id = stored["config"]["configurable"].get("checkpoint_id")
        if checkpoint_id is not None and checkpoint_id != stored_id:
            return None
        return CheckpointTuple(
            config=cast("RunnableConfig", stored["config"]),
            checkpoint=stored["checkpoint"],
            metadata=stored["metadata"],
            parent_config=cast("RunnableConfig | None", stored["parent_config"]),
            pending_writes=stored["pending_writes"],
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002  # LangGraph fixes this API name.
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """Yield the latest checkpoint for a requested thread when it matches filters."""
        if config is None:
            return
        stored = self.get_tuple(config)
        if stored is None:
            return
        if before is not None:
            before_id = before["configurable"].get("checkpoint_id")
            stored_id = stored.config["configurable"].get("checkpoint_id")
            if isinstance(before_id, str) and stored_id == before_id:
                return
        if filter is not None and any(
            stored.metadata.get(key) != value for key, value in filter.items()
        ):
            return
        if limit is not None and limit <= 0:
            return
        yield stored

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Persist a checkpoint and return its configuration including its id."""
        del new_versions
        thread_id, checkpoint_ns = _checkpoint_key(config)
        checkpoint_id = checkpoint["id"]
        checkpoint_config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        parent_id = config["configurable"].get("checkpoint_id")
        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id is not None
            else None
        )
        stored: _StoredCheckpoint = {
            "config": checkpoint_config,
            "checkpoint": checkpoint,
            "metadata": metadata,
            "parent_config": parent_config,
            "pending_writes": [],
        }
        with self._lock:
            self._repository.put(thread_id, checkpoint_ns, _encode(stored, self.serde))
        return cast("RunnableConfig", checkpoint_config)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Append pending writes to the latest checkpoint for the ``(thread_id, checkpoint_ns)``.

        This appends to whichever checkpoint is latest within the coordinate; it does not match
        ``config["configurable"]["checkpoint_id"]`` the way LangGraph's own ``InMemorySaver``
        does. Doing so needs multi-checkpoint retention per namespace -- a different storage
        model and a separate slice; F8.7 names only the ``thread_id``/``checkpoint_ns``
        composite identity, so this gap is recorded as still open rather than half-fixed here.
        """
        del task_path
        thread_id, checkpoint_ns = _checkpoint_key(config)
        with self._lock:
            payload = self._repository.get(thread_id, checkpoint_ns)
            if payload is None:
                return
            stored = _decode(payload, self.serde)
            stored["pending_writes"].extend((task_id, channel, value) for channel, value in writes)
            self._repository.put(thread_id, checkpoint_ns, _encode(stored, self.serde))


def _checkpoint_key(config: RunnableConfig) -> tuple[str, str]:
    """Extract and validate the composite ``(thread_id, checkpoint_ns)`` LangGraph identity."""
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        msg = "LangGraph config must contain a configurable mapping"
        raise TypeError(msg)
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        msg = "LangGraph config requires a non-empty thread_id"
        raise ValueError(msg)
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        msg = "LangGraph config requires a str checkpoint_ns"
        raise TypeError(msg)
    return thread_id, checkpoint_ns


def _encode(value: _StoredCheckpoint, serde: Any) -> bytes:
    """Encode a checkpoint envelope as deterministic JSON containing typed payload bytes."""
    type_name, raw = serde.dumps_typed(value)
    envelope = {
        "serde_type": type_name,
        "payload": base64.b64encode(raw).decode("ascii"),
    }
    return canonical_json(envelope).encode("utf-8")


def _decode(payload: bytes, serde: Any) -> _StoredCheckpoint:
    """Decode and validate the checkpoint envelope stored by :func:`_encode`."""
    try:
        envelope = json.loads(payload.decode("utf-8"))
        type_name = envelope["serde_type"]
        raw = base64.b64decode(envelope["payload"], validate=True)
        value = serde.loads_typed((type_name, raw))
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        msg = "invalid LangGraph checkpoint payload"
        raise ValueError(msg) from exc
    if not isinstance(value, dict):
        msg = "LangGraph checkpoint payload must contain an object"
        raise TypeError(msg)
    return cast("_StoredCheckpoint", value)


__all__ = ["StateCheckpointer"]
