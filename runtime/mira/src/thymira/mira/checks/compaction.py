"""The compaction audit control A27 (CMP-01).

ADR-0006 makes compaction an *appended* record: a ``context.compacted`` event names, under
``shadowed_seqs``, the model-visible events it hid from the model's surface while leaving the
hash chain whole. ADR-0006 decision 5 defers to MIRA the control that checks a compaction is
structurally sound, and ADR-0007 withdrew the bracket half of it, so a compaction is a single
atomic append with no unterminated state to detect.

This control folds every ``context.compacted`` event and fails when one of three structural
guarantees is broken:

- **cannot shadow forward** -- a compaction may only shadow events that *precede* it
  (``derive_surface`` already ignores a forward seq, so a forged payload hides nothing, but the
  presence of such a seq is evidence of a malformed or adversarial record and is a finding);
- **well-formed ``shadowed_seqs``** -- the payload names a non-empty list of integer seqs and
  nothing else (a non-integer entry is dropped by the fold rather than trusted, and its presence
  is reported here);
- **a recorded summarisation envelope** -- provider, model, tier and token counts, so an auditor
  can reconstruct *which model hid what* (ADR-0006 decision 5), the way ``policy_sha256`` lets an
  auditor replay a policy decision.

The compaction *fidelity* question -- does the persisted summary faithfully represent what it
replaced -- is the separate audit agent CMP-02; this control only guarantees the structure CMP-02
relies on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import SHADOWED_SEQS_KEY, derive_surface, shadowed_seqs
from thymira.mira.checks.models import ControlStatus
from thymira.schemas import EventType, SurfaceState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event

#: Payload key of a ``context.compacted`` event carrying the summarisation envelope.
COMPACTION_ENVELOPE_KEY = "envelope"

#: Envelope fields that must be non-empty strings (ADR-0006 decision 5: provider/model/tier).
ENVELOPE_TEXT_FIELDS: tuple[str, ...] = ("provider", "model", "tier")

#: Envelope fields that must be non-negative integer token counts.
ENVELOPE_TOKEN_FIELDS: tuple[str, ...] = ("input_tokens", "output_tokens")


def check_compaction_integrity(events: Sequence[Event]) -> tuple[ControlStatus, str]:
    """Fold ``context.compacted`` events and verify their structural guarantees.

    Args:
        events: the run's events in log order.

    Returns:
        ``NOT_APPLICABLE`` when the run recorded no compaction; ``FAILED`` with the offending
        seqs when a compaction shadows forward, carries a malformed ``shadowed_seqs`` payload, or
        omits the summarisation envelope; ``PASSED`` when every compaction is well-formed.
    """
    compactions = [event for event in events if event.type is EventType.CONTEXT_COMPACTED]
    if not compactions:
        return ControlStatus.NOT_APPLICABLE, "no context.compacted events"
    problems: list[str] = []
    for event in compactions:
        problems.extend(_shadowed_seqs_problems(event))
        problems.extend(_envelope_problems(event))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    surface = derive_surface(events)
    shadowed = sum(1 for state in surface.values() if state is SurfaceState.SHADOWED)
    return (
        ControlStatus.PASSED,
        f"{len(compactions)} well-formed compaction(s); {shadowed} event(s) shadowed",
    )


def _shadowed_seqs_problems(event: Event) -> list[str]:
    """Reject a malformed ``shadowed_seqs`` payload or a seq at/after the compaction's own."""
    raw = event.payload.get(SHADOWED_SEQS_KEY)
    if not isinstance(raw, list | tuple):
        return [f"seq {event.seq}: shadowed_seqs is missing or not a list"]
    cleaned = shadowed_seqs(event)
    if len(cleaned) != len(raw):
        return [f"seq {event.seq}: shadowed_seqs contains a non-integer entry"]
    if not cleaned:
        return [f"seq {event.seq}: shadowed_seqs names no events to shadow"]
    forward = sorted(seq for seq in cleaned if seq >= event.seq)
    if forward:
        return [
            f"seq {event.seq}: cannot shadow forward, names seq(s) {forward} at or after itself"
        ]
    return []


def _envelope_problems(event: Event) -> list[str]:
    """Reject a missing or incomplete summarisation envelope."""
    envelope = event.payload.get(COMPACTION_ENVELOPE_KEY)
    if not isinstance(envelope, dict):
        return [f"seq {event.seq}: missing summarisation envelope"]
    missing = [field for field in ENVELOPE_TEXT_FIELDS if not _is_nonempty_str(envelope.get(field))]
    missing += [
        field for field in ENVELOPE_TOKEN_FIELDS if not _is_nonnegative_int(envelope.get(field))
    ]
    if missing:
        return [f"seq {event.seq}: summarisation envelope missing {sorted(missing)}"]
    return []


def _is_nonempty_str(value: object) -> bool:
    """Return whether ``value`` is a non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _is_nonnegative_int(value: object) -> bool:
    """Return whether ``value`` is a non-negative integer (booleans excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "COMPACTION_ENVELOPE_KEY",
    "ENVELOPE_TEXT_FIELDS",
    "ENVELOPE_TOKEN_FIELDS",
    "check_compaction_integrity",
]
