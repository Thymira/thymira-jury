"""Bound oversized tool output while preserving the complete result as an artifact."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from thymira.events import redact
from thymira.schemas import ArtifactKind
from thymira.tools.results import ProcessToolValue

if TYPE_CHECKING:
    from thymira.tools.models import ToolContext, ToolResult


def spill_result(
    result: ToolResult,
    context: ToolContext,
    *,
    max_inline_bytes: int,
    produced_by: str | None = None,
) -> ToolResult:
    """Spill oversized stdout and stderr to log artifacts without making a tool call fail.

    `spill_result` is the trust boundary where captured process output becomes persistent, so it
    redacts here rather than in the artifact store, whose contract is byte fidelity: redacting a
    dataset or a model there would corrupt the data and make the manifest sha256 meaningless as a
    chain of custody. Each oversized field is redacted exactly once with
    :func:`thymira.events.redact`, and the inline head, the stored `ArtifactKind.LOG` artifact and
    any digest are all derived from that same redacted text -- never from the raw text, and never
    sliced before redaction.

    ``result_sha256`` means a digest a verifier can recompute from stored evidence:

    * If the tool already set ``result_sha256``, it is left untouched. A tool's claim about its own
      result is evidence; discarding it without a trace is a defect this function once had.
    * If the tool set none and exactly one stream spilled, it is set to the sha256 of the redacted
      text that was stored -- the same value the artifact manifest holds -- so anyone can recompute
      it from the artifact.
    * If the tool set none and both streams spilled, it is left ``None``. There is no canonical
      single text; the two artifacts each carry their own sha256 in the manifest and in their
      `artifact.created` events, and a fabricated digest is worse than no digest.

    Do not reintroduce a digest over a private concatenation of the streams (``b"".join(...)``):
    nothing in the repository can recompute it, so it is not evidence.

    A store that raises is swallowed for that field -- a failed spill must never make the tool call
    fail. The field then keeps its full inline value, and no artifact id or digest is recorded for
    it.
    """
    if max_inline_bytes < 1:
        raise ValueError("max_inline_bytes must be positive")

    owner = produced_by or context.agent_id
    artifact_ids = list(result.artifact_ids)
    stored_shas: list[str] = []
    updated = result
    for field in ("stdout", "stderr"):
        value = getattr(updated, field)
        if len(value.encode("utf-8")) <= max_inline_bytes:
            continue
        # Redact once at the top of the spill branch; the inline head, the stored artifact and the
        # digest below are all derived from this one redacted text.
        redacted = redact(value)
        redacted_bytes = redacted.encode("utf-8")
        name = f"tool-results/{owner}/{field}.log"
        try:
            artifact = context.artifact_store.save_text(
                name,
                redacted,
                produced_by=owner,
                kind=ArtifactKind.LOG,
                media_type="text/plain; charset=utf-8",
            )
        except (OSError, ValueError, TypeError):
            continue
        marker = f"\n[output spilled to artifact {artifact.id}]\n"
        marker_bytes = marker.encode("utf-8")
        if len(marker_bytes) >= max_inline_bytes:
            inline = marker_bytes[:max_inline_bytes].decode("utf-8", errors="ignore")
        else:
            head = redacted_bytes[: max_inline_bytes - len(marker_bytes)].decode(
                "utf-8", errors="ignore"
            )
            inline = head + marker
        updated = replace(updated, **{field: inline})
        if updated.value is not None:
            value_updates = {"text": updated.stdout}
            if isinstance(updated.value, ProcessToolValue):
                value_updates[field] = inline
            updated = replace(updated, value=updated.value.model_copy(update=value_updates))
        artifact_ids.append(artifact.id)
        stored_shas.append(artifact.sha256)

    if not stored_shas:
        return updated
    # Never overwrite a digest the tool set. Otherwise, a single spilled stream has one canonical
    # stored text whose manifest sha256 a verifier can recompute; two streams have none.
    if result.result_sha256 is None and len(stored_shas) == 1:
        return replace(updated, artifact_ids=tuple(artifact_ids), result_sha256=stored_shas[0])
    return replace(updated, artifact_ids=tuple(artifact_ids))


__all__ = ["spill_result"]
