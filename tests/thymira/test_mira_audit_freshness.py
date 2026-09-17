"""The passive audit-freshness control A25 (CTRL-AUDIT-FRESHNESS).

``assess_audit_freshness`` takes a finished :class:`AuditReport` and a (possibly longer) event
history and asks whether anything material happened after the snapshot the report pinned. It is
deliberately passive: it returns MIRA evidence only, never writing an event or touching a Run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import InMemoryEventLog
from thymira.mira.checks import (
    AuditContext,
    AuditReport,
    ControlStatus,
    assess_audit_freshness,
    audit_run,
)
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import Actor, ArtifactKind, EventType, new_id

from thymira.state import LocalArtifactStore  # isort: skip

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.schemas import Artifact


def _snapshot(tmp_path: Path) -> tuple[InMemoryEventLog, LocalArtifactStore, Artifact, AuditReport]:
    """A closed run that produced one artifact, plus the audit report pinning that state."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    system = Actor.system()
    tool_id = new_id("tool")
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    artifact = store.save_json(
        "metrics.json", {"auc": 0.8}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": artifact.name, "sha256": artifact.sha256},
        subject_id=artifact.id,
    )
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=tool_id)
    log.append(EventType.RUN_COMPLETED, system, {})
    report = audit_run(AuditContext(run_id, log.events(), store))
    return log, store, artifact, report


def _event_refs(result) -> set[str]:
    return {e.ref for e in result.evidence if e.kind == "event"}


def _artifact_refs(result) -> set[str]:
    return {e.ref for e in result.evidence if e.kind == "artifact"}


def test_a25_passes_an_unchanged_snapshot(tmp_path: Path) -> None:
    log, _store, _artifact, report = _snapshot(tmp_path)

    result = assess_audit_freshness(report, log.events())

    assert result.control_id == "A25"
    assert result.status is ControlStatus.PASSED


def test_a25_fails_when_later_material_evidence_appears(tmp_path: Path) -> None:
    log, _store, _artifact, report = _snapshot(tmp_path)
    later = log.append(
        EventType.MODEL_TRAINED, Actor.system(), {"seed": 1}, subject_id=new_id("artifact")
    )

    result = assess_audit_freshness(report, log.events())

    assert result.status is ControlStatus.FAILED
    assert f"seq:{later.seq}" in _event_refs(result)


def test_a25_fails_when_activity_context_changes_after_the_snapshot(tmp_path: Path) -> None:
    """A later risk-context fact cannot be hidden behind an otherwise unchanged event log."""
    log, _store, _artifact, report = _snapshot(tmp_path)
    later = log.append(
        EventType.ACTIVITY_PROFILE_ANSWERED,
        Actor.system(),
        {"field": "human_oversight", "answer": "A reviewer can override the result."},
    )

    result = assess_audit_freshness(report, log.events())

    assert result.status is ControlStatus.FAILED
    assert f"seq:{later.seq}" in _event_refs(result)


def test_a25_fails_when_a_snapshot_dependency_version_changes(tmp_path: Path) -> None:
    """Flow, policy and activity-profile changes invalidate the stored dependency snapshot."""
    log, _store, _artifact, report = _snapshot(tmp_path)
    snapshotted = report.model_copy(
        update={
            "graph_definition_hash": "graph-v1",
            "policy_sha256": "policy-v1",
            "activity_profile_id": "profile-v1",
            "activity_profile_version": 1,
        }
    )

    result = assess_audit_freshness(
        snapshotted,
        log.events(),
        current_graph_definition_hash="graph-v2",
        current_policy_sha256="policy-v2",
        current_activity_profile_id="profile-v2",
        current_activity_profile_version=2,
    )

    assert result.status is ControlStatus.FAILED
    assert "flow version" in result.detail
    assert "policy hash" in result.detail
    assert "activity profile" in result.detail


def test_a25_fails_when_a_referenced_artifact_is_later_invalidated(tmp_path: Path) -> None:
    log, store, artifact, report = _snapshot(tmp_path)
    later = log.append(
        EventType.ARTIFACT_INVALIDATED,
        Actor.system(),
        {"name": artifact.name, "reason": "an input changed"},
        subject_id=artifact.id,
    )

    result = assess_audit_freshness(report, log.events(), store=store)

    assert result.status is ControlStatus.FAILED
    assert f"seq:{later.seq}" in _event_refs(result)  # the later event reference
    assert artifact.name in _artifact_refs(result)  # and the artifact reference


def test_a25_fails_when_an_artifact_byte_changes(tmp_path: Path) -> None:
    """A changed artifact byte invalidates a report even without a later invalidation event."""
    log, store, artifact, report = _snapshot(tmp_path)
    snapshotted = report.model_copy(update={"manifest_sha256": artifact_manifest_sha256(store)})
    (tmp_path / "artifacts" / artifact.uri).write_text('{"auc": 0.7}', encoding="utf-8")

    result = assess_audit_freshness(snapshotted, log.events(), store=store)

    assert result.status is ControlStatus.FAILED
    assert "manifest" in result.detail


def test_a25_passes_when_only_governance_lifecycle_events_follow(tmp_path: Path) -> None:
    log, _store, _artifact, report = _snapshot(tmp_path)
    system = Actor.system()
    log.append(EventType.AUDIT_STARTED, system, {})
    log.append(EventType.POLICY_DECISION, system, {"decision": "PASS", "id": "d1"})
    log.append(EventType.AUDIT_COMPLETED, system, {})

    result = assess_audit_freshness(report, log.events())

    assert result.status is ControlStatus.PASSED


def test_a25_is_not_applicable_without_a_terminal_hash() -> None:
    report = AuditReport(run_id=new_id("run"), status="passed", controls=())

    result = assess_audit_freshness(report, [])

    assert result.status is ControlStatus.NOT_APPLICABLE


def test_a25_is_not_applicable_when_the_history_cannot_contain_the_snapshot(tmp_path: Path) -> None:
    _log, _store, _artifact, report = _snapshot(tmp_path)
    unrelated = InMemoryEventLog(new_id("run"))
    unrelated.append(EventType.RUN_STARTED, Actor.system(), {})

    result = assess_audit_freshness(report, unrelated.events())

    assert result.status is ControlStatus.NOT_APPLICABLE
