"""Core enforcement of MIRA audit-freshness evidence at Run continuation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from thymira.core import (
    AuditFreshnessService,
    RunAuditStaleError,
    RunController,
    RunEventLog,
    RunService,
    RunTransitionKind,
    SessionService,
    rework_signal_from_decision,
)
from thymira.events import verify_events
from thymira.mira import canonical_graph_definition_hash
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.policies import (
    FindingRule,
    Gate,
    GateMode,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
)
from thymira.schemas import (
    Actor,
    AuditFinding,
    Decision,
    EventType,
    Framework,
    PolicyDecision,
    Run,
    Severity,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRunStore, LocalSessionRepository

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.core import ExecutionDispatcher


class _RecordingDispatcher:
    """Record whether a stale continuation ever reaches the execution dispatcher."""

    def __init__(self) -> None:
        self.resumed: list[str] = []

    def submit(self, _run_id: str) -> None:
        """The test never submits a new Run."""

    def resume(self, run_id: str) -> None:
        """Record an attempted continuation."""
        self.resumed.append(run_id)


@dataclass(frozen=True, slots=True)
class _ReworkToolReview:
    """A Run parked on a tool review during an authorised post-audit rework."""

    run: Run
    store: LocalRunStore
    controller: RunController
    log: RunEventLog
    engine: PolicyEngine
    service: RunService
    freshness: AuditFreshnessService
    decision: PolicyDecision
    dispatcher: _RecordingDispatcher
    artifact_root: Path


def _rework_tool_review(
    tmp_path: Path,
    *,
    authorized_reopen: bool = True,
    reopen_source_hash: str | None = None,
    later_reopen_source_hash: str | None = None,
    graph_definition_hash: str | None = None,
    policy_sha256: str | None = None,
    corrupt_artifact: bool = False,
) -> _ReworkToolReview:
    """Build audit -> optional authorised reopen -> deferred tool review through real services."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Repair the audited analysis with a review-gated tool.",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    controller.advance(run.id, RunTransitionKind.BEGIN_EXECUTION)
    controller.advance(run.id, RunTransitionKind.BEGIN_AUDIT)
    engine = PolicyEngine(
        Policy(
            name="rework-tool-review",
            version="1.0",
            finding_rules=(
                FindingRule(
                    id="REWORK-PREPARATION",
                    decision=Decision.WARNING,
                    reason="correct the preparation finding",
                    min_severity=Severity.MEDIUM,
                ),
            ),
        )
    )
    log = RunEventLog(store, run.id)
    artifact_root = tmp_path / "artifacts" / run.id
    artifacts = LocalArtifactStore(artifact_root, run.id)
    report = AuditReport(
        run_id=run.id,
        status="passed_with_warnings",
        controls=(),
        terminal_hash=store.events(run.id)[-1].hash,
        graph_definition_hash=canonical_graph_definition_hash(),
        policy_sha256=engine.policy_sha256,
        manifest_sha256=artifact_manifest_sha256(artifacts),
    )
    log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"status": report.status, "audit_report": report.model_dump(mode="json")},
        subject_id=run.id,
    )
    if authorized_reopen:
        finding = AuditFinding(
            id=new_id("finding"),
            run_id=run.id,
            control_id="A11",
            framework=Framework.INTERNAL,
            title="Repair data preparation",
            finding="The preparation evidence needs correction.",
            severity=Severity.MEDIUM,
            confidence=1.0,
        )
        rework_decision = Gate(engine, log).review_findings((finding,))
        translation = rework_signal_from_decision(rework_decision, (finding,))
        assert translation.signal is not None
        controller.reopen(
            run.id,
            translation.signal,
            reopen_count=0,
            max_reopens=2,
            source_audit_terminal_hash=reopen_source_hash or report.terminal_hash,
        )
        if later_reopen_source_hash is not None:
            controller.advance(run.id, RunTransitionKind.BEGIN_AUDIT)
            controller.reopen(
                run.id,
                translation.signal,
                reopen_count=1,
                max_reopens=2,
                source_audit_terminal_hash=later_reopen_source_hash,
            )
        artifact = artifacts.save_text(
            "analysis.txt",
            "post-audit rework evidence",
            produced_by=new_id("agent"),
        )
        log.append(
            EventType.ARTIFACT_CREATED,
            Actor.system(),
            {"name": artifact.name, "sha256": artifact.sha256},
            subject_id=artifact.id,
        )
    decision = Gate(engine, log, mode=GateMode.DEFERRED).check_capability(
        subject_id=new_id("tool"),
        capability=ToolCapability(id="echo", external_effects=()),
        risk=RiskProfile(),
        summary="echo",
        details={
            "tool": "echo",
            "arguments": {"value": "x"},
            "tool_intent_sha256": "0" * 64,
        },
    )
    assert decision.requires_human_approval
    log.append(
        EventType.TOOL_DENIED,
        Actor(kind="tool", id="echo"),
        {"tool": "echo", "reason": "waiting for a human"},
        subject_id=decision.subject_id,
    )
    controller.park_for_review(run.id, decision)
    if corrupt_artifact:
        # The store publishes immutable content objects, so the bytes an audit verifies live at
        # the recorded artifact uri, not under the logical name.
        recorded = artifacts.get("analysis.txt")
        assert recorded is not None
        (artifact_root / recorded.uri).write_text("tampered", encoding="utf-8", newline="\n")

    def artifact_store_factory(run_id: str) -> LocalArtifactStore:
        return LocalArtifactStore(tmp_path / "artifacts" / run_id, run_id)

    freshness = AuditFreshnessService(
        store,
        artifact_store_factory,
        policy_sha256=policy_sha256 or engine.policy_sha256,
        graph_definition_hash=graph_definition_hash or canonical_graph_definition_hash,
    )
    dispatcher = _RecordingDispatcher()
    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=cast("ExecutionDispatcher", dispatcher),
        policy_sha256=engine.policy_sha256,
        graph_definition_hash=canonical_graph_definition_hash,
        gate_factory=lambda run_id: Gate(engine, RunEventLog(store, run_id)),
        audit_freshness=freshness,
    )
    return _ReworkToolReview(
        run,
        store,
        controller,
        log,
        engine,
        service,
        freshness,
        decision,
        dispatcher,
        artifact_root,
    )


@pytest.mark.parametrize("approved", [True, False])
def test_authorized_rework_tool_review_resumes_execution_without_reusing_old_audit(
    tmp_path: Path, approved: bool
) -> None:
    """Either tool answer resumes authorised rework without blessing the old audit."""
    rework = _rework_tool_review(tmp_path)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    gate = Gate(
        rework.engine,
        rework.log,
        approver=lambda _request: approved,
        human=reviewer,
    )

    continued = rework.service.resolve_approval(
        rework.run.id,
        gate=gate,
        actor=reviewer,
        note="reviewed",
    )

    events = rework.store.events(rework.run.id)
    assert continued.status.value == "RUNNING"
    assert rework.dispatcher.resumed == [rework.run.id]
    assert len([event for event in events if event.type is EventType.AUDIT_COMPLETED]) == 1
    resumed = next(
        event
        for event in reversed(events)
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
    )
    assert resumed.payload["resume_from"] == "execution"
    assert resumed.payload["approved"] is approved
    assert [
        event.payload["approved"]
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and event.payload.get("decision_id") == rework.decision.id
    ] == [approved]
    assessment = rework.freshness.assess(rework.run.id)
    assert assessment is not None
    assert not assessment.fresh
    assert "current artifact manifest differs" in assessment.control.detail
    assert not any(
        event.type is EventType.POLICY_DECISION
        and event.payload.get("rule_id") == "audit_freshness"
        and event.payload.get("decision") == Decision.PASS.value
        for event in events
    )
    assert verify_events(events).valid


def test_out_of_band_rework_tool_answer_then_plain_resume_continues_execution(
    tmp_path: Path,
) -> None:
    """A separately recorded tool answer can plain-resume the authorized rework pass."""
    rework = _rework_tool_review(tmp_path)
    reviewer = Actor(kind="human", id="reviewer", authenticated=True)
    Gate(
        rework.engine,
        rework.log,
        approver=lambda _request: True,
        human=reviewer,
    ).resolve_pending_approval(rework.decision, note="reviewed out of band")

    continued = rework.service.resume(rework.run.id, actor=reviewer)

    events = rework.store.events(rework.run.id)
    assert continued.status.value == "RUNNING"
    assert rework.dispatcher.resumed == [rework.run.id]
    assert len([event for event in events if event.type is EventType.AUDIT_COMPLETED]) == 1
    resumed = next(
        event
        for event in reversed(events)
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
    )
    assert resumed.payload["resume_from"] == "execution"
    assert [
        event.payload["approved"]
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and event.payload.get("decision_id") == rework.decision.id
    ] == [True]
    assert verify_events(events).valid


def test_rework_tool_review_without_matching_reopen_stays_blocked(tmp_path: Path) -> None:
    """A post-audit tool review cannot claim continuation without an exact reopen record."""
    rework = _rework_tool_review(tmp_path, reopen_source_hash="f" * 64)

    with pytest.raises(RunAuditStaleError, match="audit snapshot is stale"):
        rework.service.resolve_approval(
            rework.run.id,
            gate=Gate(rework.engine, rework.log, approver=auto_approve),
            actor=Actor.system(),
        )

    assert rework.controller.current_state(rework.run.id).state.outcome is not None
    assert rework.dispatcher.resumed == []


def test_rework_tool_review_does_not_fall_back_past_a_newer_unbacked_reopen(
    tmp_path: Path,
) -> None:
    """Only the latest post-audit reopen may back an expected rework continuation."""
    rework = _rework_tool_review(tmp_path, later_reopen_source_hash="f" * 64)

    with pytest.raises(RunAuditStaleError, match="audit snapshot is stale"):
        rework.service.resolve_approval(
            rework.run.id,
            gate=Gate(rework.engine, rework.log, approver=auto_approve),
            actor=Actor.system(),
        )

    assert rework.dispatcher.resumed == []


def test_authorized_rework_continuation_still_blocks_graph_drift(tmp_path: Path) -> None:
    """A valid reopen does not waive graph-integrity checks on the original audit prefix."""
    rework = _rework_tool_review(tmp_path, graph_definition_hash="f" * 64)

    with pytest.raises(RunAuditStaleError, match="audit flow version differs"):
        rework.service.resolve_approval(
            rework.run.id,
            gate=Gate(rework.engine, rework.log, approver=auto_approve),
            actor=Actor.system(),
        )

    assert rework.dispatcher.resumed == []


def test_authorized_rework_continuation_still_blocks_policy_drift(tmp_path: Path) -> None:
    """A valid reopen does not waive policy-integrity checks on the original audit prefix."""
    rework = _rework_tool_review(tmp_path, policy_sha256="f" * 64)

    with pytest.raises(RunAuditStaleError, match="policy hash differs"):
        rework.service.resolve_approval(
            rework.run.id,
            gate=Gate(rework.engine, rework.log, approver=auto_approve),
            actor=Actor.system(),
        )

    assert rework.dispatcher.resumed == []


def test_authorized_rework_continuation_still_blocks_corrupt_artifact(tmp_path: Path) -> None:
    """Expected rework additions do not waive verification of current artifact bytes."""
    rework = _rework_tool_review(tmp_path, corrupt_artifact=True)

    with pytest.raises(RunAuditStaleError, match="artifact manifest verification failed"):
        rework.service.resolve_approval(
            rework.run.id,
            gate=Gate(rework.engine, rework.log, approver=auto_approve),
            actor=Actor.system(),
        )

    assert rework.dispatcher.resumed == []


def test_stale_audit_blocks_pending_approval_before_dispatch(tmp_path: Path) -> None:
    """A stale audit records a Gate BLOCK and RunController terminal block, without resuming."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Continue an audited run",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    controller = RunController(store)
    controller.advance(run.id, RunTransitionKind.START)
    engine = PolicyEngine(
        Policy(
            name="approval",
            version="1.0",
            finding_rules=(
                FindingRule(
                    id="REVIEW-HIGH",
                    decision=Decision.REQUIRE_HUMAN_REVIEW,
                    reason="high findings need a human",
                    min_severity=Severity.HIGH,
                ),
            ),
        )
    )
    event_log = RunEventLog(store, run.id)
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run.id,
        control_id="TEST-1",
        framework=Framework.INTERNAL,
        title="Review",
        finding="A human must review this result.",
        severity=Severity.HIGH,
        confidence=1.0,
    )
    pending = Gate(engine, event_log, mode=GateMode.DEFERRED).review_findings((finding,))
    controller.park_for_review(run.id, pending)
    report = AuditReport(
        run_id=run.id,
        status="passed",
        controls=(),
        terminal_hash=store.events(run.id)[0].hash,
        graph_definition_hash=canonical_graph_definition_hash(),
        policy_sha256=engine.policy_sha256,
    )
    event_log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"status": report.status, "audit_report": report.model_dump(mode="json")},
        subject_id=run.id,
    )
    event_log.append(EventType.TOOL_COMPLETED, Actor.system(), {"tool": "later"})

    def artifact_store_factory(run_id: str) -> LocalArtifactStore:
        """Build the empty artifact store for the test Run."""
        return LocalArtifactStore(tmp_path / "artifacts" / run_id, run_id)

    freshness = AuditFreshnessService(
        store,
        artifact_store_factory,
        policy_sha256=engine.policy_sha256,
        graph_definition_hash=canonical_graph_definition_hash,
    )
    dispatcher = _RecordingDispatcher()
    service = RunService(
        store,
        SessionService(LocalSessionRepository(tmp_path / "sessions")),
        dispatcher=cast("ExecutionDispatcher", dispatcher),
        policy_sha256=engine.policy_sha256,
        graph_definition_hash=canonical_graph_definition_hash,
        gate_factory=lambda run_id: Gate(engine, RunEventLog(store, run_id)),
        audit_freshness=freshness,
    )
    approval_gate = Gate(engine, event_log, approver=auto_approve)

    with pytest.raises(RunAuditStaleError, match="audit snapshot is stale"):
        service.resolve_approval(run.id, gate=approval_gate, actor=Actor.system())

    state = controller.current_state(run.id).state
    events = store.events(run.id)
    assert state.outcome is not None
    assert state.outcome.value == "blocked"
    assert dispatcher.resumed == []
    freshness_decision = next(
        event
        for event in reversed(events)
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("rule_id") == "audit_freshness"
    )
    assert freshness_decision.payload["decision"] == Decision.BLOCK.value
    assert any(event.type is EventType.RUN_TRANSITIONED for event in events)
    assert verify_events(events).valid
