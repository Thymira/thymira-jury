"""Deterministic source checkpoints and output pruning for context recovery.

The checkpoint is a source record, rather than a model-authored summary.  It has the eight
sections required by the DSH acceptance record and carries structured facts with a source sequence
and revision.  This lets recovery merge records deterministically without assuming that the last
piece of free text is semantically newer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import ceil
from typing import Any

from pydantic import Field, model_validator

from thymira.events import canonical_json, sha256_text
from thymira.schemas import EventType, ThymiraModel

CHECKPOINT_SECTION_NAMES: tuple[str, ...] = (
    "Primary Request and Intent",
    "Key Technical Concepts",
    "Files and Code",
    "Errors and Fixes",
    "Pending Jobs",
    "Current Work",
    "Next Step",
    "Critical Context",
)
"""The fixed source checkpoint vocabulary (F8.4)."""

_ID_KEYS = {
    "run_id",
    "task_id",
    "agent_id",
    "tool_call_id",
    "decision_id",
    "approval_id",
    "artifact_id",
    "event_id",
    "finding_id",
    "experiment_id",
    "checkpoint_id",
}
_CORRECTION_KEYS = {"correction", "user_correction", "user_corrections"}
_FACT_KEYS = {"pending_fact", "current_work", "next_step", "fact"}
_SHORT_MARKER_CHARS = 3
MODEL_OUTPUT_CHAR_LIMIT = 4_000
"""Maximum text copied from one durable event into a model-bound request."""


class CheckpointFact(ThymiraModel):
    """A structured fact whose identity and revision survive checkpoint merging."""

    key: str = Field(min_length=1)
    value: str
    revision: int = Field(ge=0)
    source_seq: int = Field(ge=0)
    kind: str = Field(default="fact", min_length=1)


class ContextCheckpoint(ThymiraModel):
    """The canonical, exactly-eight-section source checkpoint."""

    sections: dict[str, str]
    facts: tuple[CheckpointFact, ...] = ()
    source_seqs: tuple[int, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    revision: int = Field(default=0, ge=0)
    continuation_header: str = Field(min_length=1)

    @model_validator(mode="after")
    def _has_exact_sections(self) -> ContextCheckpoint:
        """Reject missing, extra, or duplicate section names at the contract boundary."""
        if set(self.sections) != set(CHECKPOINT_SECTION_NAMES):
            expected = ", ".join(CHECKPOINT_SECTION_NAMES)
            msg = f"context checkpoint must contain exactly these sections: {expected}"
            raise ValueError(msg)
        object.__setattr__(
            self,
            "sections",
            {name: self.sections[name] for name in CHECKPOINT_SECTION_NAMES},
        )
        return self

    @property
    def digest(self) -> str:
        """Return the stable digest of this source checkpoint."""
        return sha256_text(canonical_json(self.model_dump(mode="json")))


# The shorter name is useful at call sites and keeps the contract discoverable in either form.
SourceCheckpoint = ContextCheckpoint


class PrunedText(ThymiraModel):
    """The deterministic head-marker-tail result and its measured accounting."""

    text: str
    head: str
    marker: str
    tail: str
    omitted_chars: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def estimate_tokens(text: str) -> int:
    """Estimate tokens deterministically using four UTF-8 characters per token."""
    return ceil(len(text) / 4) if text else 0


def prune_head_marker_tail(
    text: str,
    max_chars: int,
    *,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> PrunedText:
    """Keep a deterministic head and tail around an explicit omission marker.

    The boundary is character based and therefore stable across runs.  The marker itself is
    counted against ``max_chars`` and the returned accounting uses the same estimate as context
    budgeting.  Empty or already-fitting inputs are returned unchanged.
    """
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    if len(text) <= max_chars:
        output = text
        return PrunedText(
            text=output,
            head=output,
            marker="",
            tail="",
            omitted_chars=0,
            input_tokens=estimate_tokens(text),
            output_tokens=estimate_tokens(output),
            input_sha256=sha256_text(text),
            output_sha256=sha256_text(output),
        )
    marker = f"\n[… omitted {len(text) - max_chars} characters …]\n"
    if len(marker) > max_chars:
        marker = "[…]" if max_chars >= _SHORT_MARKER_CHARS else "…" if max_chars else ""
    requested_head = requested_tail = 0
    for _ in range(3):
        available = max_chars - len(marker)
        requested_head = available // 2 if head_chars is None else max(0, head_chars)
        requested_tail = available - requested_head if tail_chars is None else max(0, tail_chars)
        if requested_head + requested_tail > available:
            scale = available / max(requested_head + requested_tail, 1)
            requested_head = int(requested_head * scale)
            requested_tail = available - requested_head
        omitted = len(text) - requested_head - requested_tail
        next_marker = f"\n[… omitted {omitted} characters …]\n"
        if len(next_marker) > max_chars:
            next_marker = "[…]" if max_chars >= _SHORT_MARKER_CHARS else "…" if max_chars else ""
        if len(next_marker) == len(marker):
            marker = next_marker
            break
        marker = next_marker
    head = text[:requested_head]
    tail = text[len(text) - requested_tail :] if requested_tail else ""
    output = f"{head}{marker}{tail}"
    return PrunedText(
        text=output,
        head=head,
        marker=marker,
        tail=tail,
        omitted_chars=len(text) - len(head) - len(tail),
        input_tokens=estimate_tokens(text),
        output_tokens=estimate_tokens(output),
        input_sha256=sha256_text(text),
        output_sha256=sha256_text(output),
    )


def _value_text(value: Any) -> str:
    """Render payload values without allowing nondeterministic object reprs."""
    if isinstance(value, str):
        return value
    return canonical_json(value)


def _walk_payload(value: Any) -> Iterable[tuple[str, str, str]]:
    """Yield structured identifiers and corrections from nested event payloads."""
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            normalized = str(child_key)
            if normalized in _ID_KEYS and isinstance(child, str):
                yield normalized, child, "identifier"
            elif normalized in _CORRECTION_KEYS:
                if isinstance(child, str):
                    yield normalized, child, "user_correction"
                elif isinstance(child, Sequence) and not isinstance(child, str):
                    for correction in child:
                        if isinstance(correction, str):
                            yield normalized, correction, "user_correction"
            elif normalized in _FACT_KEYS and isinstance(child, str):
                yield normalized, child, normalized
            yield from _walk_payload(child)
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for child in value:
            yield from _walk_payload(child)


def _event_facts(event: Any) -> list[CheckpointFact]:
    """Extract only explicit structured identity/correction facts from an event."""
    candidates: list[tuple[str, str, str]] = []
    if isinstance(event.subject_id, str):
        candidates.append(("subject_id", event.subject_id, "identifier"))
    candidates.extend(_walk_payload(event.payload))
    return [
        CheckpointFact(
            key=(
                f"user_correction:{sha256_text(value)[:16]}"
                if kind == "user_correction"
                else f"{key}:{value}"
                if kind == "identifier"
                else key
            ),
            value=value,
            revision=event.seq,
            source_seq=event.seq,
            kind=kind,
        )
        for key, value, kind in candidates
    ]


def _section_text(events: Sequence[Any], names: set[str]) -> str:
    """Render bounded source text for a section based on event kinds and payload keys."""
    parts: list[str] = []
    for event in events:
        if event.type.value in names or any(key in names for key in event.payload):
            text = event.payload.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def build_source_checkpoint(
    events: Sequence[Any],
    *,
    primary_request: str = "",
    user_corrections: Sequence[str | CheckpointFact] = (),
    current_work: str = "",
    pending_jobs: str = "",
    facts: Sequence[CheckpointFact] = (),
    prior: ContextCheckpoint | None = None,
) -> ContextCheckpoint:
    """Build the source checkpoint from durable events and explicit structured inputs.

    Text sections retain the complete source projection; ``PromptBuilder`` applies the deterministic
    model-bound limit when it renders a request. Identifiers and corrections also stay in ``facts``
    with their source sequence. A caller that changes a fact must provide a higher revision in a
    subsequent event; no semantic "last free-text paragraph wins" rule is used.
    """
    events = list(events)
    latest_seq = max((event.seq for event in events), default=0)
    source_seqs = tuple(event.seq for event in events)
    source_event_ids = tuple(event.event_id for event in events)
    event_text = "\n".join(
        str(event.payload.get("text"))
        for event in events
        if isinstance(event.payload.get("text"), str) and event.payload.get("text")
    )
    prior_current_work = prior.sections["Current Work"] if prior is not None else ""
    prior_pending_jobs = prior.sections["Pending Jobs"] if prior is not None else ""
    sections = {
        "Primary Request and Intent": primary_request,
        "Key Technical Concepts": _section_text(events, {"agent.message", "concepts"}),
        "Files and Code": _section_text(events, {"file", "files", "code", "path"}),
        "Errors and Fixes": _section_text(events, {"error", "errors", "fix", "failed"}),
        "Pending Jobs": pending_jobs
        or prior_pending_jobs
        or _section_text(events, {"pending", "approval", "decision_id"}),
        "Current Work": current_work or prior_current_work or event_text,
        "Next Step": "Continue from the latest durable event.",
        "Critical Context": "Recorded identifiers and revisions are retained in checkpoint facts.",
    }
    # The source checkpoint is the lossless durable projection.  PromptBuilder applies the
    # deterministic model-bound limit later; truncating here would make recovery unable to prove
    # that identifiers, corrections, and current work survived the compaction.
    source_sections = sections
    extracted_facts = [fact for event in events for fact in _event_facts(event)]
    extracted_facts.extend(facts)
    for correction in user_corrections:
        if isinstance(correction, CheckpointFact):
            extracted_facts.append(correction)
            continue
        extracted_facts.append(
            CheckpointFact(
                key=f"user_correction:{sha256_text(correction)[:16]}",
                value=correction,
                revision=latest_seq + 1,
                source_seq=latest_seq,
                kind="user_correction",
            )
        )
    if prior is not None:
        current = ContextCheckpoint(
            sections=source_sections,
            facts=tuple(extracted_facts),
            source_seqs=source_seqs,
            source_event_ids=source_event_ids,
            revision=max(latest_seq, prior.revision + 1),
            continuation_header=(
                f"Continue from source checkpoint revision {max(latest_seq, prior.revision + 1)}; "
                "the complete event log remains authoritative."
            ),
        )
        return merge_checkpoints(prior, current)
    return ContextCheckpoint(
        sections=source_sections,
        facts=tuple(extracted_facts),
        source_seqs=source_seqs,
        source_event_ids=source_event_ids,
        revision=latest_seq,
        continuation_header=(
            f"Continue from source checkpoint revision {latest_seq}; "
            "the complete event log remains authoritative."
        ),
    )


def merge_checkpoints(prior: ContextCheckpoint, current: ContextCheckpoint) -> ContextCheckpoint:
    """Merge checkpoints by structured identity and revision, deterministically."""
    selected: dict[str, CheckpointFact] = {}
    for fact in (*prior.facts, *current.facts):
        old = selected.get(fact.key)
        if old is None or (fact.revision, fact.source_seq, fact.value) >= (
            old.revision,
            old.source_seq,
            old.value,
        ):
            selected[fact.key] = fact
    sections = {
        name: current.sections[name] or prior.sections[name] for name in CHECKPOINT_SECTION_NAMES
    }
    return ContextCheckpoint(
        sections=sections,
        facts=tuple(selected[key] for key in sorted(selected)),
        source_seqs=tuple(dict.fromkeys((*prior.source_seqs, *current.source_seqs))),
        source_event_ids=tuple(dict.fromkeys((*prior.source_event_ids, *current.source_event_ids))),
        revision=max(prior.revision, current.revision),
        continuation_header=current.continuation_header,
    )


def recover_checkpoint(events: Sequence[Any]) -> ContextCheckpoint | None:
    """Recover and merge every valid checkpoint carried by compacted events."""
    recovered: ContextCheckpoint | None = None
    for event in events:
        if event.type is not EventType.CONTEXT_COMPACTED:
            continue
        raw = event.payload.get("checkpoint")
        if not isinstance(raw, Mapping):
            continue
        try:
            checkpoint = ContextCheckpoint.model_validate(raw)
        except (TypeError, ValueError):
            continue
        recovered = checkpoint if recovered is None else merge_checkpoints(recovered, checkpoint)
    return recovered


def render_checkpoint(checkpoint: ContextCheckpoint, *, max_chars: int | None = None) -> str:
    """Render a continuation header and source sections for a model prompt."""
    lines = [checkpoint.continuation_header]
    lines.extend(f"{name}: {checkpoint.sections[name]}" for name in CHECKPOINT_SECTION_NAMES)
    if checkpoint.facts:
        facts = ", ".join(f"{fact.key}={fact.value}" for fact in checkpoint.facts)
        lines.append(f"Checkpoint facts: {facts}")
    rendered = "\n".join(lines)
    return prune_head_marker_tail(rendered, max_chars).text if max_chars is not None else rendered


__all__ = [
    "CHECKPOINT_SECTION_NAMES",
    "MODEL_OUTPUT_CHAR_LIMIT",
    "CheckpointFact",
    "ContextCheckpoint",
    "PrunedText",
    "SourceCheckpoint",
    "build_source_checkpoint",
    "estimate_tokens",
    "merge_checkpoints",
    "prune_head_marker_tail",
    "recover_checkpoint",
    "render_checkpoint",
]
