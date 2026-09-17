"""Checkpoint persistence seam for LangGraph state."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Protocol, runtime_checkable

from thymira.events import secure_directory, secure_file
from thymira.state._atomic import replace_with_retry

_RESERVED_WINDOWS_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)


@runtime_checkable
class CheckpointRepository(Protocol):
    """Store opaque checkpoint blobs keyed by the composite ``(thread_id, checkpoint_ns)`` identity.

    A LangGraph thread can hold more than one namespace at once -- the root namespace (``""``)
    and one namespace per nested subgraph task (``"<node>:<task_id>"``, joined and terminated
    with LangGraph's own ``"|"``/``":"`` separators). Every operation below therefore takes both
    halves of the identity as required positional parameters: a defaulted keyword would leave a
    caller that forgets the namespace silently pointed at the wrong blob.
    """

    def put(self, thread_id: str, checkpoint_ns: str, checkpoint: bytes) -> None:
        """Persist a checkpoint for one ``(thread_id, checkpoint_ns)`` coordinate."""
        ...

    def get(self, thread_id: str, checkpoint_ns: str) -> bytes | None:
        """Return the checkpoint for one coordinate, if present."""
        ...

    def list(self, thread_id: str, checkpoint_ns: str) -> tuple[str, ...]:
        """Return ``(checkpoint_ns,)`` when a blob exists for the coordinate, else ``()``."""
        ...


def _namespace_segment(checkpoint_ns: str) -> str:
    """Return a filename-safe, bounded-length digest for ``checkpoint_ns``.

    LangGraph joins nested subgraph namespaces with ``"|"`` and ``":"`` (both reserved in Windows
    filenames), and the root namespace is ``""``, which cannot be a filename at all. Hashing is
    load-bearing, not cosmetic: it is the only encoding that accepts every namespace LangGraph can
    produce while keeping the path short enough for ``MAX_PATH`` under pytest tmp dirs.
    """
    return hashlib.sha256(checkpoint_ns.encode("utf-8")).hexdigest()[:32]


class LocalCheckpointRepository:
    """Store one latest checkpoint per ``(thread_id, checkpoint_ns)`` coordinate as a byte blob.

    Layout: ``<root>/checkpoints/<thread_id>/<namespace-digest>.bin``. ``thread_id`` stays a
    validated plain name (unchanged from before this coordinate was namespaced); the namespace
    segment is a hash rather than a raw path segment because it must also accept the empty root
    namespace and LangGraph's reserved separator characters.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "checkpoints"
        secure_directory(self._root)

    def _path(self, thread_id: str, checkpoint_ns: str) -> Path:
        """Return a contained path for the ``(thread_id, checkpoint_ns)`` coordinate."""
        if (
            not thread_id
            or Path(thread_id).name != thread_id
            or PureWindowsPath(thread_id).name != thread_id
            or PureWindowsPath(thread_id).anchor
            or thread_id != thread_id.rstrip(". ")
            or ":" in thread_id
            or thread_id.partition(".")[0].casefold() in _RESERVED_WINDOWS_STEMS
        ):
            msg = "thread_id must be a non-empty plain name"
            raise ValueError(msg)
        return self._root / thread_id / f"{_namespace_segment(checkpoint_ns)}.bin"

    def put(self, thread_id: str, checkpoint_ns: str, checkpoint: bytes) -> None:
        """Persist a checkpoint atomically.

        The blob is written to a per-call temporary file in the same per-thread directory (a
        unique name, so two writers for one coordinate never share a temp path) and then moved
        onto the target with one atomic replace that tolerates the transient Windows file lock
        (:func:`replace_with_retry`). LangGraph writes a checkpoint after every superstep, so this
        move is on the hot path of every composed Run; a temporary left behind by a failed replace
        is always removed.
        """
        path = self._path(thread_id, checkpoint_ns)
        secure_directory(path.parent)
        descriptor, raw_tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.stem}.", suffix=".bin.tmp"
        )
        tmp = Path(raw_tmp)
        try:
            # Establish the ACL/mode while the temporary is still empty; checkpoint bytes can
            # contain a model-visible prompt and must never cross an unverified file boundary.
            secure_file(tmp)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(checkpoint)
            replace_with_retry(tmp, path)
            secure_file(path)
        finally:
            tmp.unlink(missing_ok=True)

    def get(self, thread_id: str, checkpoint_ns: str) -> bytes | None:
        """Return the checkpoint for one coordinate, if present."""
        path = self._path(thread_id, checkpoint_ns)
        if path.exists():
            secure_file(path)
        return path.read_bytes() if path.exists() else None

    def list(self, thread_id: str, checkpoint_ns: str) -> tuple[str, ...]:
        """Return ``(checkpoint_ns,)`` when a blob exists for the coordinate, else ``()``."""
        return (checkpoint_ns,) if self.get(thread_id, checkpoint_ns) is not None else ()


__all__ = ["CheckpointRepository", "LocalCheckpointRepository"]
