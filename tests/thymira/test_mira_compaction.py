"""The compaction audit control A27 (CMP-01).

ADR-0006 makes a ``context.compacted`` event an append that shadows earlier model-visible events
and records the summarising model's envelope. This control folds those events and fails when a
compaction shadows forward, carries a malformed ``shadowed_seqs`` payload, or omits the envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import AuditContext, ControlStatus, audit_run, check_compaction_integrity
from thymira.schemas import Actor, EventSurface, EventType, new_id

if TYPE_CHECKING:
    from thymira.schemas import Event

_ENVELOPE = {
    "provider": "litellm",
    "model": "test-model",
    "tier": "STANDARD",
    "input_tokens": 1200,
    "output_tokens": 240,
}


def _log_with_visible_events(count: int) -> InMemoryEventLog:
    """Return a chained log: ``run.started`` then ``count`` model-visible ``agent.message``."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system(), {"run_environment": {"python": "3.13"}})
    for index in range(count):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": f"visible step {index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    return log


def _compact(log: InMemoryEventLog, payload: dict[str, object]) -> Event:
    """Append one ``context.compacted`` event carrying ``payload`` (log-only, as in production)."""
    return log.append(
        EventType.CONTEXT_COMPACTED,
        Actor.system(),
        payload,
        surface=EventSurface.LOG_ONLY,
    )


def test_compaction_control_is_not_applicable_without_compaction() -> None:
    log = _log_with_visible_events(3)

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.NOT_APPLICABLE
    assert "no context.compacted" in detail


def test_compaction_control_passes_a_well_formed_compaction() -> None:
    log = _log_with_visible_events(3)
    _compact(log, {"shadowed_seqs": [1, 2], "envelope": dict(_ENVELOPE)})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.PASSED
    assert "1 well-formed compaction" in detail
    assert "2 event(s) shadowed" in detail
    assert verify_events(log.events()).valid


def test_compaction_control_fails_when_it_shadows_forward() -> None:
    log = _log_with_visible_events(3)
    own_seq = len(log.events())  # the seq the compaction is about to take
    _compact(log, {"shadowed_seqs": [1, own_seq], "envelope": dict(_ENVELOPE)})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.FAILED
    assert "cannot shadow forward" in detail


def test_compaction_control_fails_without_a_summarisation_envelope() -> None:
    log = _log_with_visible_events(3)
    _compact(log, {"shadowed_seqs": [1, 2]})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.FAILED
    assert "missing summarisation envelope" in detail


def test_compaction_control_fails_on_an_incomplete_envelope() -> None:
    log = _log_with_visible_events(3)
    envelope = {"provider": "litellm", "model": "test-model", "tier": "STANDARD"}
    _compact(log, {"shadowed_seqs": [1, 2], "envelope": envelope})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.FAILED
    assert "input_tokens" in detail
    assert "output_tokens" in detail


def test_compaction_control_fails_on_a_non_integer_shadowed_seq() -> None:
    log = _log_with_visible_events(3)
    _compact(log, {"shadowed_seqs": [1, "two"], "envelope": dict(_ENVELOPE)})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.FAILED
    assert "non-integer entry" in detail


def test_compaction_control_fails_when_shadowed_seqs_is_not_a_list() -> None:
    log = _log_with_visible_events(3)
    _compact(log, {"shadowed_seqs": "1,2", "envelope": dict(_ENVELOPE)})

    status, detail = check_compaction_integrity(log.events())

    assert status is ControlStatus.FAILED
    assert "missing or not a list" in detail


def test_compaction_control_is_registered_in_audit_run() -> None:
    """audit_run folds A27, so a well-formed compaction PASSES through the registered control."""
    log = _log_with_visible_events(3)
    _compact(log, {"shadowed_seqs": [1, 2], "envelope": dict(_ENVELOPE)})

    report = audit_run(AuditContext(log.run_id, log.events()))

    a27 = next(control for control in report.controls if control.control_id == "A27")
    assert a27.title == "Compaction integrity"
    assert a27.status is ControlStatus.PASSED
    assert "A27" not in {finding.control_id for finding in report.findings}
