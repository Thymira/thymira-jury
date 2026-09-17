"""Lineage-aware artifact invalidation for the runtime."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from thymira.state import ArtifactStore


def cascade_invalidation(
    store: ArtifactStore,
    changed_names: Iterable[str],
    downstream_graph: Mapping[str, Sequence[str]],
    reason: str,
) -> list[str]:
    """Invalidate every stored descendant of the changed artifact names.

    The changed names themselves are treated as the causes of invalidation. Their transitive
    downstream names are visited once, in graph order, and passed to the existing store seam in
    one call. The store preserves each invalidated artifact and its reason.
    """
    queue = deque(dict.fromkeys(changed_names))
    visited = set(queue)
    descendants: list[str] = []
    while queue:
        current = queue.popleft()
        for downstream in downstream_graph.get(current, ()):
            if downstream in visited:
                continue
            visited.add(downstream)
            descendants.append(downstream)
            queue.append(downstream)
    return store.invalidate(descendants, reason)


__all__ = ["cascade_invalidation"]
