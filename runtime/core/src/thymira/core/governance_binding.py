"""Evidence binding for terminal findings decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json
from thymira.mira.checks import AuditReport
from thymira.schemas import AuditFinding, Event, EventType, PolicyDecision

if TYPE_CHECKING:
    from collections.abc import Sequence


def policy_decision_binding(decision: PolicyDecision) -> dict[str, Any]:
    """Return the decision identity fields a terminal transition must carry."""
    return {
        "policy_decision_id": decision.id,
        "policy_sha256": decision.policy_sha256,
        "finding_ids": list(decision.finding_ids),
    }


def _policy_decision_event(run_id: str, decision: PolicyDecision, events: Sequence[Event]) -> Event:
    """Find the one canonical event that records the decision being closed."""
    matching = [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION and event.payload.get("id") == decision.id
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"run {run_id}: final policy decision {decision.id} is not uniquely recorded"
        )
    decision_event = matching[0]
    if decision_event.run_id != run_id:
        raise RuntimeError(f"run {run_id}: policy decision event belongs to another Run")
    return decision_event


def _latest_audit_report(run_id: str, events: Sequence[Event]) -> tuple[Event, AuditReport]:
    """Find the latest typed audit report, rejecting an incomplete audit marker."""
    for candidate in reversed(events):
        if candidate.type is not EventType.AUDIT_COMPLETED:
            continue
        if candidate.run_id != run_id:
            raise RuntimeError(f"run {run_id}: audit event belongs to another Run")
        raw_report = candidate.payload.get("audit_report")
        if not isinstance(raw_report, dict):
            raise RuntimeError(  # noqa: TRY004  # malformed audit evidence aborts the graph
                f"run {run_id}: latest audit.completed has no audit report"
            )
        try:
            report = AuditReport.model_validate(raw_report)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"run {run_id}: latest audit.completed has a malformed audit report"
            ) from exc
        if report.run_id != run_id:
            raise RuntimeError(f"run {run_id}: audit report belongs to another Run")
        return candidate, report
    raise RuntimeError(f"run {run_id}: final findings have no audit.completed report")


def _validate_audit_findings(
    run_id: str,
    report: AuditReport,
    decision: PolicyDecision,
    findings: Sequence[AuditFinding] | None,
) -> None:
    """Ensure the report and the policy Gate saw the same finding snapshot."""
    report_finding_ids = tuple(finding.id for finding in report.findings)
    if tuple(decision.finding_ids) != report_finding_ids:
        raise RuntimeError(f"run {run_id}: policy decision findings do not match the audit report")
    if findings is None:
        return
    finding_ids = tuple(finding.id for finding in findings)
    if finding_ids != report_finding_ids:
        raise RuntimeError(f"run {run_id}: audit findings do not match the Gate snapshot")
    report_json = [finding.model_dump(mode="json") for finding in report.findings]
    findings_json = [finding.model_dump(mode="json") for finding in findings]
    if canonical_json(report_json) != canonical_json(findings_json):
        raise RuntimeError(f"run {run_id}: audit finding payload changed before terminal closure")


def _audit_binding_metadata(
    run_id: str, report_event: Event, events: Sequence[Event]
) -> dict[str, Any]:
    """Validate and return the report revision and snapshot hash for the terminal transition."""
    revision = report_event.payload.get("audit_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise RuntimeError(f"run {run_id}: audit.completed has no valid audit revision")
    report_position = next(index for index, event in enumerate(events) if event is report_event)
    expected_revision = sum(
        event.type is EventType.AUDIT_COMPLETED for event in events[: report_position + 1]
    )
    if revision != expected_revision:
        raise RuntimeError(f"run {run_id}: audit.completed revision does not match its history")
    return {"audit_revision": revision}


def _audit_terminal_hash(
    run_id: str, report: AuditReport, report_event: Event, events: Sequence[Event]
) -> str:
    """Validate the report's snapshot pointer and return its hash."""
    terminal_hash = report.terminal_hash
    if terminal_hash is None:
        raise RuntimeError(f"run {run_id}: audit report has no terminal hash")
    snapshot = next((event for event in events if event.hash == terminal_hash), None)
    if snapshot is None or snapshot.seq >= report_event.seq:
        raise RuntimeError(f"run {run_id}: audit report terminal hash is not a preceding event")
    return terminal_hash


def final_governance_binding(
    run_id: str,
    decision: PolicyDecision,
    events: Sequence[Event],
    findings: Sequence[AuditFinding] | None = None,
) -> dict[str, Any]:
    """Return the binding for one exact findings decision and its preceding MIRA report.

    ``findings`` is the Core checkpoint snapshot when the composition owns it. A caller that is
    resolving an already persisted approval may omit that snapshot; the report and decision still
    have to agree, and the event chain remains the authority for the selected audit.
    """
    if decision.run_id != run_id or decision.subject_kind != "findings":
        raise ValueError("a final governance binding requires a findings decision for this Run")
    decision_event = _policy_decision_event(run_id, decision, events)
    report_event, report = _latest_audit_report(run_id, events)
    if report_event.seq >= decision_event.seq:
        raise RuntimeError(f"run {run_id}: audit report must precede its policy decision")
    _validate_audit_findings(run_id, report, decision, findings)
    if report.policy_sha256 != decision.policy_sha256:
        raise RuntimeError(f"run {run_id}: audit and policy snapshots use different policy hashes")
    metadata = _audit_binding_metadata(run_id, report_event, events)
    metadata["audit_terminal_hash"] = _audit_terminal_hash(run_id, report, report_event, events)
    return {
        **policy_decision_binding(decision),
        **metadata,
        "audit_completed_seq": report_event.seq,
    }


__all__ = ["final_governance_binding", "policy_decision_binding"]
