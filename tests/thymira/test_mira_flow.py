"""Unit tests for MIRA's one canonical gate-less audit flow."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from thymira.events import InMemoryEventLog, verify_events
from thymira.mira import (
    AuditAgentOutput,
    AuditAgentSpec,
    MiraAuditFlow,
    MiraAuditFlowConfig,
    MiraAuditOrchestrator,
    MiraGraphInput,
    canonical_graph_definition_hash,
    load_default_packs,
)
from thymira.mira.checks import AuditMode, AuditReport, ControlStatus, assess_audit_freshness
from thymira.schemas import (
    ActivityProfile,
    Actor,
    AuditFinding,
    EventType,
    Evidence,
    Framework,
    PlanBoard,
    Run,
    Severity,
    new_id,
)
from thymira.state import LocalArtifactStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from thymira.schemas import Event


NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def _input_with_open_run(
    *, frameworks: tuple[Framework, ...] = ()
) -> tuple[MiraGraphInput, InMemoryEventLog]:
    """Build an auditable input whose open lifecycle yields deterministic finding A2."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Audit the local credit-risk evidence.",
    )
    log = InMemoryEventLog(run.id)
    started = log.append(
        EventType.RUN_STARTED,
        Actor.system(),
        {"run_environment": {"python": "3.13"}},
    )
    assert started.hash is not None
    profile = ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run.id,
        purpose="Prioritise credit applications for human review.",
        affected_population="Credit applicants in Spain.",
        decision_effect="Changes review order but never grants or denies credit.",
        autonomy="Recommendation only.",
        human_oversight="A credit analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=("credit_history",),
        potential_consequences=("An urgent application could be reviewed too late.",),
        evidence_refs=(Evidence(kind="event", ref="seq:0", sha256=started.hash),),
    )
    return (
        MiraGraphInput(
            run=run,
            activity_profile=profile,
            events=tuple(log.events()),
            frameworks=frameworks,
            audit_mode=AuditMode.FINAL,
            audited_at=NOW,
        ),
        log,
    )


def _flow(
    log: InMemoryEventLog,
    *,
    specs: tuple[AuditAgentSpec, ...] = (),
    run_agent: Callable[[AuditAgentSpec, str, tuple[Event, ...], AuditReport], AuditAgentOutput]
    | None = None,
) -> MiraAuditFlow:
    """Build the canonical flow with the only dependencies this test needs."""
    return MiraAuditFlow(
        MiraAuditFlowConfig(
            orchestrator=MiraAuditOrchestrator(load_default_packs(), Actor.system()),
            event_log=log,
            specs=specs,
            run_agent=run_agent,
        )
    )


def _spec(name: str, framework: Framework) -> AuditAgentSpec:
    """Build a minimal audit-agent declaration for flow selection tests."""
    return AuditAgentSpec(
        name=name,
        framework=framework,
        task_kinds=("audit_judgement",),
        max_turns=1,
        system_prompt="Return candidate findings only.",
    )


def _finding(run_id: str, control_id: str) -> AuditFinding:
    """Build one grounded candidate finding for an injected audit agent."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        agent_id=new_id("agent"),
        control_id=control_id,
        framework=Framework.EU_AI_ACT,
        title="Audit candidate",
        finding="The applicable audit agent found a concern.",
        severity=Severity.MEDIUM,
        confidence=0.9,
        evidence=(Evidence(kind="event", ref="seq:0"),),
    )


def test_flow_records_the_definition_hash_of_the_flow_it_executes(tmp_path: Path) -> None:
    """The persisted report names the canonical flow, never a removed wrapper graph."""
    graph_input, log = _input_with_open_run()
    store = LocalArtifactStore(tmp_path / "artifacts", graph_input.run.id)
    store.save_json("run_environment.json", {"python": "3.13"}, produced_by=new_id("agent"))
    flow = MiraAuditFlow(
        MiraAuditFlowConfig(
            orchestrator=MiraAuditOrchestrator(load_default_packs(), Actor.system()),
            event_log=log,
            artifact_store=store,
        )
    )

    result = flow.run(graph_input)
    flow.complete()

    completed = next(event for event in log.events() if event.type is EventType.AUDIT_COMPLETED)
    report = AuditReport.model_validate(completed.payload["audit_report"])
    assert report.graph_definition_hash == canonical_graph_definition_hash()
    assert report.terminal_hash == log.events()[-2].hash
    assert assess_audit_freshness(report, log.events()).status is ControlStatus.PASSED
    assert result.audit_report.run_id == graph_input.run.id
    assert verify_events(log.events()).valid


def test_flow_consumes_board_replay_and_records_read_only_evidence() -> None:
    """MIRA replays supplied plan/DAG evidence and records the derived facts in its own log."""
    graph_input, log = _input_with_open_run()
    plan = PlanBoard(run_id=graph_input.run.id)
    enriched = graph_input.model_copy(update={"plan_history": (plan,)})

    flow = _flow(log)
    flow.preflight(enriched)
    replay_events = [event for event in log.events() if event.type is EventType.MIRA_BOARD_REPLAYED]
    assert len(replay_events) == 1
    assert replay_events[0].payload == {
        "plan_revision": 0,
        "blocked_count": 0,
        "blocked_cause": None,
        "blocked": False,
        "runnable_work_ids": [],
    }


def test_new_flow_continues_the_audit_revision_from_the_event_log() -> None:
    """A fresh flow instance writes the next revision after a persisted audit completion."""
    graph_input, log = _input_with_open_run()
    first_flow = _flow(log)
    persisted_preflight = first_flow.preflight(graph_input)
    first_flow.audit(graph_input)
    first_flow.complete()

    resumed_input = graph_input.model_copy(update={"events": tuple(log.events())})
    second_flow = _flow(log)
    second_flow.restore_preflight(resumed_input, persisted_preflight)
    second_flow.audit(resumed_input)
    second_flow.complete()

    revisions = [
        event.payload["audit_revision"]
        for event in log.events()
        if event.type is EventType.AUDIT_COMPLETED
    ]
    assert revisions == [1, 2]


def test_flow_runs_only_specs_for_declared_frameworks() -> None:
    """A project declaration limits model calls to its applicable audit-agent framework."""
    graph_input, log = _input_with_open_run(frameworks=(Framework.EU_AI_ACT,))
    applicable = _spec("euaiact", Framework.EU_AI_ACT)
    excluded = _spec("methodology", Framework.METHODOLOGY)
    calls: list[str] = []

    def run_agent(
        spec: AuditAgentSpec,
        run_id: str,
        _events: tuple[Event, ...],
        _report: AuditReport,
    ) -> AuditAgentOutput:
        calls.append(spec.name)
        return AuditAgentOutput.from_spec(spec, findings=(_finding(run_id, "AGENT-EU-001"),))

    result = _flow(log, specs=(applicable, excluded), run_agent=run_agent).run(graph_input)

    assert calls == ["euaiact"]
    assert "AGENT-EU-001" in {finding.control_id for finding in result.audit_findings}


def test_flow_uses_project_frameworks_for_a26_without_a_matching_roster() -> None:
    """A26 asserts over project governance even when no audit-agent spec covers it."""
    graph_input, log = _input_with_open_run(frameworks=(Framework.EU_AI_ACT,))

    result = _flow(log).run(graph_input)

    a26 = next(control for control in result.audit_report.controls if control.control_id == "A26")
    assert a26.status is ControlStatus.FAILED
    assert "EU-AI-ACT-ART-" in a26.detail


def test_flow_skips_inapplicable_specs_without_requiring_a_runner() -> None:
    """An excluded roster entry cannot require a runner or produce a model call."""
    graph_input, log = _input_with_open_run(frameworks=(Framework.EU_AI_ACT,))

    result = _flow(log, specs=(_spec("methodology", Framework.METHODOLOGY),)).run(graph_input)

    assert result.audit_report.run_id == graph_input.run.id
    assert not any(event.type is EventType.AGENT_STARTED for event in log.events())


def test_flow_rejects_an_input_for_another_event_log() -> None:
    """The flow never writes audit evidence into a Run other than its input Run."""
    graph_input, _input_log = _input_with_open_run()
    other_log = InMemoryEventLog(new_id("run"))

    with pytest.raises(ValueError, match="input run_id"):
        _flow(other_log).run(graph_input)


def test_flow_discards_unknown_enrichment_findings(caplog: pytest.LogCaptureFixture) -> None:
    """An enrichment hallucination is logged and cannot add a new authority-owned finding."""
    graph_input, log = _input_with_open_run(frameworks=(Framework.INTERNAL,))
    spec = _spec("reg_evidence", Framework.INTERNAL)

    def run_agent(
        _specification: AuditAgentSpec,
        run_id: str,
        _events: tuple[Event, ...],
        _report: AuditReport,
    ) -> AuditAgentOutput:
        return AuditAgentOutput.from_spec(
            spec,
            findings=(
                _finding(run_id, "HALLUCINATED-001").model_copy(
                    update={"framework": Framework.INTERNAL}
                ),
            ),
        )

    caplog.set_level("WARNING", logger="thymira.mira.flow")
    result = _flow(log, specs=(spec,), run_agent=run_agent).run(graph_input)

    assert "unknown finding id" in caplog.text
    assert "HALLUCINATED-001" not in {finding.control_id for finding in result.audit_findings}


def test_importing_mira_does_not_load_agents_tools_or_the_runner() -> None:
    """A fresh import preserves MIRA's lazy audit-agent boundary."""
    script = "\n".join(
        (
            "import sys",
            "import thymira.mira",
            "assert 'thymira.mira.agents.runner' not in sys.modules",
            "assert not any(name.startswith('thymira.agents') for name in sys.modules)",
            "assert not any(name.startswith('thymira.tools') for name in sys.modules)",
        )
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
