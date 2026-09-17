"""Finding-level expert review and Finding->Evidence->source traceability (ASSUR-02).

The assurance bundle gains a per-finding review state that transitions only on recorded
``human.approval`` events naming the exact finding, plus a traceability index resolving every
finding's evidence to the concrete Run/Artifact/Event/KB source it cites. A reviewed finding means
only that its evidence was reviewed -- never remediation, and never a change to a Run or decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira import (
    REVIEW_DISCLAIMER,
    FindingReviewState,
    build_assurance,
    derive_finding_reviews,
)
from thymira.mira.assurance import REVIEW_FINDING_ID_KEY, REVIEW_STATE_KEY
from thymira.mira.checks import AuditContext, AuditReport, ControlResult, ControlStatus, audit_run
from thymira.schemas import (
    Actor,
    AuditFinding,
    Decision,
    EventSurface,
    EventType,
    Evidence,
    Framework,
    PolicyDecision,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from thymira.schemas import Event

_TERMINAL_HASH = "c" * 64
_POLICY_SHA = "d" * 64


def _finding(run_id: str, control_id: str, evidence: tuple[Evidence, ...]) -> AuditFinding:
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id=control_id,
        framework=Framework.INTERNAL,
        title=f"Finding for {control_id}",
        finding="observed",
        severity=Severity.MEDIUM,
        confidence=1.0,
        evidence=evidence,
    )


def _report(run_id: str, findings: tuple[AuditFinding, ...]) -> AuditReport:
    return AuditReport(
        run_id=run_id,
        status="passed_with_warnings" if findings else "passed",
        controls=(
            ControlResult(
                control_id="A2",
                title="Run closed",
                status=ControlStatus.PASSED,
                severity=Severity.LOW,
                detail="checked",
            ),
        ),
        findings=findings,
        terminal_hash=_TERMINAL_HASH,
    )


def _decision(run_id: str) -> PolicyDecision:
    return PolicyDecision(
        id=new_id("decision"),
        run_id=run_id,
        subject_kind="findings",
        subject_id="findings",
        decision=Decision.WARNING,
        rule_id="default",
        reason="one warning finding",
        policy_name="credit-risk@1.0",
        policy_sha256=_POLICY_SHA,
    )


def _review_event(
    log: InMemoryEventLog,
    finding_id: str,
    state: FindingReviewState,
    *,
    rationale: str | None = None,
) -> Event:
    """Append one ``human.approval`` event naming a finding and its target review state."""
    payload: dict[str, object] = {
        REVIEW_FINDING_ID_KEY: finding_id,
        REVIEW_STATE_KEY: state.value,
        "approved": state is not FindingReviewState.REJECTED,
    }
    if rationale is not None:
        payload["rationale"] = rationale
    return log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=Actor.system().kind, id="risk-officer", authenticated=True),
        payload,
        surface=EventSurface.LOG_ONLY,
    )


def _two_finding_bundle_inputs() -> tuple[str, AuditReport, tuple[AuditFinding, AuditFinding]]:
    run_id = new_id("run")
    first = _finding(run_id, "A20", (Evidence(kind="event", ref="seq:7", sha256="a" * 64),))
    second = _finding(
        run_id, "A11", (Evidence(kind="artifact", ref="split.json", sha256="b" * 64),)
    )
    return run_id, _report(run_id, (first, second)), (first, second)


def test_unreviewed_bundle_reports_not_reviewed_and_the_disclaimer() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()

    bundle = build_assurance(run_id, report, _decision(run_id))

    states = bundle.review_state_by_finding
    assert states[findings[0].id] is FindingReviewState.NOT_REVIEWED
    assert states[findings[1].id] is FindingReviewState.NOT_REVIEWED
    assert bundle.review_summary["not_reviewed"] == 2
    assert bundle.review_summary["reviewed"] == 0
    markdown = bundle.to_markdown()
    assert REVIEW_DISCLAIMER in markdown
    assert bundle.disclaimer in markdown
    assert "not_reviewed" in markdown


def test_an_approval_marks_only_the_named_finding_reviewed() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    _review_event(log, findings[0].id, FindingReviewState.REVIEWED, rationale="evidence checked")

    bundle = build_assurance(run_id, report, _decision(run_id), review_events=log.events())

    states = bundle.review_state_by_finding
    assert states[findings[0].id] is FindingReviewState.REVIEWED
    assert states[findings[1].id] is FindingReviewState.NOT_REVIEWED
    assert bundle.review_summary["reviewed"] == 1
    assert bundle.review_summary["not_reviewed"] == 1
    reviewed = next(r for r in bundle.finding_reviews if r.finding_id == findings[0].id)
    assert reviewed.reviewed_by == "risk-officer"
    assert reviewed.rationale == "evidence checked"


def test_a_review_can_open_before_it_resolves() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    _review_event(log, findings[0].id, FindingReviewState.IN_REVIEW)
    _review_event(log, findings[0].id, FindingReviewState.REJECTED)

    bundle = build_assurance(run_id, report, _decision(run_id), review_events=log.events())

    assert bundle.review_state_by_finding[findings[0].id] is FindingReviewState.REJECTED


def test_a_review_event_for_another_finding_is_ignored() -> None:
    run_id, report, _findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    _review_event(log, new_id("finding"), FindingReviewState.REVIEWED)

    bundle = build_assurance(run_id, report, _decision(run_id), review_events=log.events())

    assert bundle.review_summary["not_reviewed"] == 2


def test_an_illegal_transition_raises() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    _review_event(log, findings[0].id, FindingReviewState.REVIEWED)
    _review_event(log, findings[0].id, FindingReviewState.IN_REVIEW)

    with pytest.raises(ValueError, match="illegal review transition"):
        build_assurance(run_id, report, _decision(run_id), review_events=log.events())


def test_an_unknown_review_state_raises() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {REVIEW_FINDING_ID_KEY: findings[0].id, REVIEW_STATE_KEY: "remediated"},
        surface=EventSurface.LOG_ONLY,
    )

    with pytest.raises(ValueError, match="unknown review state"):
        derive_finding_reviews(report.findings, log.events())


def test_a_decision_level_approval_does_not_review_any_finding() -> None:
    """A plain HITL approval (decision_id/approved, no finding_id) reviews nothing (ASSUR-02)."""
    run_id, report, _findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {"decision_id": new_id("decision"), "approved": True},
        surface=EventSurface.LOG_ONLY,
    )

    bundle = build_assurance(run_id, report, _decision(run_id), review_events=log.events())

    assert bundle.review_summary["not_reviewed"] == 2


def test_every_finding_resolves_to_at_least_one_traceable_source() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()

    bundle = build_assurance(run_id, report, _decision(run_id))

    assert bundle.untraceable_finding_ids == ()
    traces = {trace.finding_id: trace for trace in bundle.traceability}
    assert set(traces) == {findings[0].id, findings[1].id}
    for trace in traces.values():
        assert trace.run_id == run_id
        assert len(trace.sources) >= 1
    assert traces[findings[0].id].sources[0].source_kind == "event"
    assert traces[findings[1].id].sources[0].source_kind == "artifact"


def test_assurance_bundle_preserves_each_finding_s_control_evidence() -> None:
    """ASSUR-02 traces findings to the events their controls actually cite."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    for _ in range(46):
        log.append(EventType.AGENT_MESSAGE, system, {"text": "filler"})
    tool_id = new_id("tool")
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    log.append(EventType.TOOL_COMPLETED, system, {}, subject_id=tool_id)
    log.append(EventType.RUN_COMPLETED, system, {})
    report = audit_run(AuditContext(run_id, log.events()))

    bundle = build_assurance(run_id, report, _decision(run_id), events=log.events())
    finding_by_id = {finding.id: finding for finding in report.findings}

    assert report.findings
    for trace in bundle.traceability:
        expected = [e.ref for e in finding_by_id[trace.finding_id].evidence]
        assert [source.ref for source in trace.sources] == expected
        assert "seq:0" not in expected


def test_external_evidence_resolves_to_a_kb_source() -> None:
    run_id = new_id("run")
    citation = Evidence(kind="external", ref="req-EU-AI-ACT-ART-14", note="Article 14")
    finding = _finding(run_id, "EU-AI-ACT-ART-14", (citation,))
    report = _report(run_id, (finding,))

    bundle = build_assurance(run_id, report, _decision(run_id))

    source = bundle.traceability[0].sources[0]
    assert source.source_kind == "kb"
    assert source.ref == "req-EU-AI-ACT-ART-14"
    assert source.note == "Article 14"


def test_traceability_and_review_render_in_markdown() -> None:
    run_id, report, findings = _two_finding_bundle_inputs()
    log = InMemoryEventLog(run_id)
    _review_event(log, findings[0].id, FindingReviewState.REVIEWED)

    markdown = build_assurance(
        run_id, report, _decision(run_id), review_events=log.events()
    ).to_markdown()

    assert "## Finding review" in markdown
    assert "## Traceability" in markdown
    assert "reviewed" in markdown
    assert findings[0].id in markdown
    assert "split.json" in markdown
