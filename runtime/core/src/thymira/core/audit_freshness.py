"""Core interpretation of MIRA's passive audit-freshness evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from thymira.mira.checks import AuditReport, ControlResult, ControlStatus, assess_audit_freshness
from thymira.schemas import EventType, Id, PolicyDecision

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.policies import Gate
    from thymira.schemas import Event
    from thymira.state import ArtifactStore, LocalRunStore


@dataclass(frozen=True, slots=True)
class AuditFreshnessAssessment:
    """A persisted report plus the evidence-only A25 result for its current dependencies."""

    report: AuditReport
    control: ControlResult

    @property
    def fresh(self) -> bool:
        """Whether the report is safe to reuse under the current evidence snapshot."""
        return self.control.status is ControlStatus.PASSED


class AuditFreshnessService:
    """Read and enforce the latest persisted audit without recalculating it."""

    def __init__(
        self,
        run_store: LocalRunStore,
        artifact_store_factory: Callable[[Id], ArtifactStore],
        *,
        policy_sha256: str | Callable[[], str],
        graph_definition_hash: str | Callable[[], str],
    ) -> None:
        self._run_store = run_store
        self._artifact_store_factory = artifact_store_factory
        self._policy_sha256 = policy_sha256
        self._graph_definition_hash = graph_definition_hash

    def assess(self, run_id: Id) -> AuditFreshnessAssessment | None:
        """Assess the latest persisted report, returning ``None`` when no audit exists."""
        events = self._run_store.events(run_id)
        completed = [event for event in events if event.type is EventType.AUDIT_COMPLETED]
        if not completed:
            return None
        completion = completed[-1]
        raw_report = completion.payload.get("audit_report")
        if not isinstance(raw_report, dict):
            raise TypeError("the persisted audit.completed event has no valid audit_report")
        report = AuditReport.model_validate(raw_report)
        if report.run_id != run_id:
            raise ValueError("the persisted audit report belongs to another Run")
        check_report = report.model_copy(
            update=self._legacy_snapshot_metadata(report, completion, events)
        )
        control = assess_audit_freshness(
            check_report,
            events,
            store=self._artifact_store_factory(run_id),
            current_graph_definition_hash=self._resolve(self._graph_definition_hash),
            current_policy_sha256=self._resolve(self._policy_sha256),
        )
        return AuditFreshnessAssessment(report=report, control=control)

    def authorize_reuse(self, run_id: Id, gate: Gate) -> AuditFreshnessAssessment | None:
        """Record the Policy Engine outcome for reusing an audit snapshot."""
        assessment = self.assess(run_id)
        if assessment is None:
            return None
        gate.check_audit_freshness(fresh=assessment.fresh, reason=assessment.control.detail)
        return assessment

    def authorize_continuation(
        self,
        run_id: Id,
        gate: Gate,
        *,
        parked_decision: PolicyDecision | None = None,
    ) -> AuditFreshnessAssessment | None:
        """Authorize reuse, or validate an audited Run's authorized rework continuation.

        A tool review reached after an exact Core ``reopen`` does not reuse the old report as a
        final finding. It may continue only when the report's original snapshot dependencies
        remain intact and the current artifact store still verifies; the resumed graph must then
        produce a new audit. Every other continuation keeps the ordinary full-history A25 check.
        """
        assessment = self.assess(run_id)
        if assessment is None:
            return None
        if parked_decision is not None and parked_decision.subject_kind == "tool_call":
            assessment = self._authorized_rework_assessment(run_id, parked_decision, assessment)
            if assessment is None:
                return None
        gate.check_audit_freshness(fresh=assessment.fresh, reason=assessment.control.detail)
        return assessment

    def _authorized_rework_assessment(
        self,
        run_id: Id,
        parked_decision: PolicyDecision,
        assessment: AuditFreshnessAssessment,
    ) -> AuditFreshnessAssessment | None:
        """Return no reuse check for valid rework, or failed evidence when integrity changed."""
        events = self._run_store.events(run_id)
        boundary = _authorized_rework_boundary(events, assessment.report, parked_decision)
        if boundary is None:
            return assessment
        completion = next(
            event
            for event in reversed(events[: boundary + 1])
            if event.type is EventType.AUDIT_COMPLETED
        )
        checked_report = assessment.report.model_copy(
            update=self._legacy_snapshot_metadata(assessment.report, completion, events)
        )
        original_snapshot = assess_audit_freshness(
            checked_report,
            events[: boundary + 1],
            current_graph_definition_hash=self._resolve(self._graph_definition_hash),
            current_policy_sha256=self._resolve(self._policy_sha256),
        )
        if original_snapshot.status is not ControlStatus.PASSED:
            return _failed_rework_assessment(
                assessment,
                f"original snapshot checks failed: {original_snapshot.detail}",
            )
        try:
            artifact_problems = self._artifact_store_factory(run_id).verify()
        except (OSError, ValueError) as exc:
            return _failed_rework_assessment(
                assessment,
                f"artifact manifest could not be verified: {exc}",
            )
        if artifact_problems:
            return _failed_rework_assessment(
                assessment,
                "artifact manifest verification failed: " + "; ".join(artifact_problems[:5]),
            )
        return None

    @staticmethod
    def _legacy_snapshot_metadata(
        report: AuditReport, completion: Event, events: list[Event]
    ) -> dict[str, object]:
        """Recover metadata from pre-freshness reports without changing their stored payload."""
        payload = completion.payload
        updates: dict[str, object] = {}
        if report.graph_definition_hash is None:
            graph_hash = payload.get("graph_definition_hash")
            if isinstance(graph_hash, str):
                updates["graph_definition_hash"] = graph_hash
        if report.policy_sha256 is None:
            policy_hash = next(
                (
                    event.payload.get("policy_sha256")
                    for event in events
                    if event.type is EventType.RUN_STARTED
                    and isinstance(event.payload.get("policy_sha256"), str)
                ),
                None,
            )
            if isinstance(policy_hash, str):
                updates["policy_sha256"] = policy_hash
        if report.activity_profile_id is None:
            profile_event = next(
                (event for event in reversed(events) if event.type is EventType.AUDIT_STARTED),
                None,
            )
            profile_payload = profile_event.payload if profile_event is not None else payload
            profile_id = profile_payload.get("activity_profile_id")
            profile_version = profile_payload.get("activity_profile_version")
            if isinstance(profile_id, str):
                updates["activity_profile_id"] = profile_id
            if isinstance(profile_version, int):
                updates["activity_profile_version"] = profile_version
        return updates

    @staticmethod
    def _resolve(value: str | Callable[[], str]) -> str:
        """Resolve a fixed or dynamic current dependency hash."""
        if isinstance(value, str):
            return value
        return cast("Callable[[], str]", value)()


def _authorized_rework_boundary(
    events: list[Event], report: AuditReport, parked_decision: PolicyDecision
) -> int | None:
    """Locate the exact report-backed reopen that precedes the parked tool decision."""
    completion_index = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if events[index].type is EventType.AUDIT_COMPLETED
        ),
        None,
    )
    if completion_index is None or report.terminal_hash is None:
        return None
    expected_decision = parked_decision.model_dump(mode="json")
    wait_index = next(
        (
            index
            for index in range(len(events) - 1, completion_index, -1)
            if events[index].type is EventType.RUN_TRANSITIONED
            and events[index].payload.get("command") == "wait_for_approval"
            and events[index].payload.get("policy_decision") == expected_decision
        ),
        None,
    )
    if wait_index is None:
        return None
    reopen_index = next(
        (
            index
            for index in range(wait_index - 1, completion_index, -1)
            if events[index].type is EventType.RUN_TRANSITIONED
            and events[index].payload.get("command") == "reopen"
        ),
        None,
    )
    if (
        reopen_index is None
        or events[reopen_index].payload.get("source_audit_terminal_hash") != report.terminal_hash
    ):
        return None
    return reopen_index


def _failed_rework_assessment(
    assessment: AuditFreshnessAssessment, reason: str
) -> AuditFreshnessAssessment:
    """Label a failed continuation check without presenting the old report as current."""
    control = assessment.control.model_copy(
        update={
            "status": ControlStatus.FAILED,
            "detail": f"authorized rework continuation refused: {reason}",
        }
    )
    return AuditFreshnessAssessment(report=assessment.report, control=control)


__all__ = ["AuditFreshnessAssessment", "AuditFreshnessService"]
