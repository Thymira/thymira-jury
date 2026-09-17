"""Persist the complete ordered discovery list beside a bounded glob/grep/list_files rendering.

Glob, grep and list_files may show a caller a bounded window of what they found, but the
*complete* ordered result -- every match, in the tool's documented order, plus which files a
search had to skip -- is always saved as a manifest-hashed :class:`~thymira.schemas.Artifact`
through the :class:`~thymira.state.ArtifactStore` (F3.2). MIRA control A32
(``thymira.mira.checks.discovery_evidence``) is the independent reader: it never imports this
module, re-declares :data:`DISCOVERY_SCHEMA` itself and recomputes the relationship between the
persisted list and what the caller was actually shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json, sha256_text
from thymira.schemas import ArtifactKind, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.tools.models import ToolInvocation

DISCOVERY_SCHEMA = "thymira.discovery/1"
DISCOVERY_PERSISTENCE_FAILURE = "FS_DISCOVERY_PERSISTENCE"
_MAX_FAILURE_DETAIL = 256
MAX_DISCOVERY_MATCHES = 100_000
MAX_DISCOVERY_ARTIFACT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveryPersistence:
    """What persisting one discovery call produced: an artifact id, or an honest failure."""

    artifact_id: str | None
    failure: str | None = None


def discovery_persistence_error(failure: str | None) -> str:
    """Return a stable, bounded error for a discovery artifact persistence failure."""
    detail = failure or "artifact store returned no artifact"
    return f"{DISCOVERY_PERSISTENCE_FAILURE}: {detail[:_MAX_FAILURE_DETAIL]}"


def persist_discovery(
    invocation: ToolInvocation,
    *,
    tool: str,
    query: dict[str, Any],
    order: str,
    matches: Sequence[Any],
    rendered_text: str,
    rendered_locations: Sequence[Any],
    skipped: Sequence[str] = (),
) -> DiscoveryPersistence:
    """Save the complete ordered match list as a manifest-hashed ``LOG`` artifact.

    A fresh store-relative name every call (``discovery/<tool>/<new_id>.json``) -- never the
    same name twice -- so calls never archive over each other and the Tool Manager's before/after
    artifact diff always sees exactly one new artifact per call: one ``artifact.created`` event
    carrying the name and sha256 A32 needs. ``kind=LOG`` is the honest kind for retained tool
    output, and keeps this artifact out of the modelling-artifact oracles that filter on
    ``kind != "log"``.

    ``rendered_text`` is the bounded rendering's own text (the joined, character-capped lines a
    caller was actually shown, *not* including any footer built afterwards) -- the same text a
    caller's ``ToolResult.result_sha256`` is pinned to, so ``rendered.sha256`` here and
    ``tool.completed.result_sha256`` are the same digest of the same text, recomputed
    independently by each producer rather than copied from one to the other.

    A store that raises leaves the caller with a bounded diagnostic rendering and a failed result.
    A discovery call without its authoritative artifact is not a successful completion.
    """
    if len(matches) > MAX_DISCOVERY_MATCHES:
        return DiscoveryPersistence(
            artifact_id=None,
            failure=f"discovery result exceeds {MAX_DISCOVERY_MATCHES} matches",
        )
    payload = {
        "schema": DISCOVERY_SCHEMA,
        "tool": tool,
        "query": query,
        "order": order,
        "total": len(matches),
        "matches": list(matches),
        "skipped": list(skipped),
        "rendered": {
            "count": len(rendered_locations),
            "truncated": len(rendered_locations) < len(matches),
            "sha256": sha256_text(rendered_text),
            "locations": list(rendered_locations),
        },
    }
    encoded = canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_DISCOVERY_ARTIFACT_BYTES:
        return DiscoveryPersistence(
            artifact_id=None,
            failure=(f"discovery artifact exceeds {MAX_DISCOVERY_ARTIFACT_BYTES} serialized bytes"),
        )
    name = f"discovery/{tool}/{new_id('artifact')}.json"
    try:
        artifact = invocation.artifact_store.save_json(
            name,
            payload,
            produced_by=invocation.agent_id,
            kind=ArtifactKind.LOG,
        )
    except (OSError, ValueError, TypeError) as exc:
        detail = " ".join(str(exc).split())[:_MAX_FAILURE_DETAIL]
        failure = f"{type(exc).__name__}: {detail or 'artifact store rejected the artifact'}"
        return DiscoveryPersistence(artifact_id=None, failure=failure)
    return DiscoveryPersistence(artifact_id=artifact.id)


__all__ = [
    "DISCOVERY_PERSISTENCE_FAILURE",
    "DISCOVERY_SCHEMA",
    "MAX_DISCOVERY_ARTIFACT_BYTES",
    "MAX_DISCOVERY_MATCHES",
    "DiscoveryPersistence",
    "discovery_persistence_error",
    "persist_discovery",
]
