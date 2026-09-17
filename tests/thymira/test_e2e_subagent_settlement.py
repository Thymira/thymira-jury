"""Settlements survive the process that wrote them: read back from bytes on disk (F7.2/G.3).

A composed THY pass writes its chain to `runs/<id>/events.jsonl`. The run is then closed and the
file re-opened with a **separately constructed** `JsonlEventLog`, so every assertion below is made
against persisted bytes rather than against a producer's return value: a fresh reader answers
"which child settled with what", the G.3 ordering holds on the re-read chain, and MIRA's A31
grades the replayed history.

Fast lane, not `integration`: the composed graph runs in process with a `ScriptedProvider`, needs
no server, no subprocess and no Docker, and finishes well under a second.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import load_agent_specs
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.settlement import (
    DIAGNOSTICS_LIMIT,
    build_settlement,
    delegation_key,
    record_settlement,
    select_canonical,
    settlements_from_events,
)
from thymira.events import JsonlEventLog, read_events, verify_log
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import (
    Actor,
    EventType,
    ModelRoutePolicy,
    Run,
    StopReason,
    Task,
    TaskStatus,
    new_id,
)
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyState

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from thymira.agents import AgentCatalog
    from thymira.schemas import Event, SubagentResult

_GOOD = "profile the good dataset"
_BAD = "profile the broken dataset"
_BLOCKED = "merge both profiles"

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct THY graph provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _catalog(tmp_path: Path) -> AgentCatalog:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data.yaml").write_text(
        """\
name: data
role: agent
task_kinds: [analyze]
max_turns: 2
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _plan_task(task_id: str, instruction: str, *, depends_on: tuple[str, ...] = ()) -> AgentTask:
    return AgentTask(
        id=task_id,
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction=instruction,
        depends_on=depends_on,
    )


def _run_a_pass(tmp_path: Path) -> tuple[Path, str, int]:
    """Run a real THY pass to disk: one completed child, one that fails, one stopped task."""
    run = Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )
    log_path = tmp_path / "runs" / run.id / "events.jsonl"
    log = JsonlEventLog(log_path, run.id)
    catalog = _catalog(tmp_path / "specs")
    # One valid output for t1, then two invalid ones so t2 exhausts its output-validation
    # retries; t3 depends on t2 and is therefore stopped without ever being delegated.
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=42, columns=("age",)),
            "not valid json",
            "still not valid json",
        ]
    )
    plan = (
        _plan_task("t1", _GOOD),
        _plan_task("t2", _BAD),
        _plan_task("t3", _BLOCKED, depends_on=("t2",)),
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))
    assert [outcome.status for outcome in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
    ]
    return log_path, run.id, len(log.events())


def _message_seq_by_task(events: Sequence[Event]) -> dict[str, int]:
    return {
        event.payload["task_id"]: event.seq
        for event in events
        if event.type is EventType.AGENT_MESSAGE and "task_id" in event.payload
    }


def _by_reason(settlements: Sequence[SubagentResult]) -> dict[StopReason, SubagentResult]:
    return {settlement.stop_reason: settlement for settlement in settlements}


def test_a_fresh_reader_reconstructs_which_child_settled_with_what(tmp_path: Path) -> None:
    log_path, run_id, written = _run_a_pass(tmp_path)

    # A separately constructed reader over the persisted bytes -- no producer object involved.
    reopened = JsonlEventLog(log_path, run_id)
    verification = verify_log(log_path)
    assert verification.valid
    assert verification.event_count == written == len(reopened.events())

    settlements = settlements_from_events(read_events(log_path))
    assert len(settlements) == 3
    by_reason = _by_reason(settlements)
    assert set(by_reason) == {StopReason.COMPLETED, StopReason.FAILED, StopReason.STOPPED}
    assert by_reason[StopReason.COMPLETED].objective == _GOOD
    assert '"row_count":42' in (by_reason[StopReason.COMPLETED].result_json or "")
    assert by_reason[StopReason.FAILED].objective == _BAD
    assert by_reason[StopReason.FAILED].result_json is None
    assert by_reason[StopReason.STOPPED].objective == _BLOCKED
    assert by_reason[StopReason.STOPPED].diagnostics == "a dependency did not complete"
    assert {settlement.diagnostics_limit for settlement in settlements} == {DIAGNOSTICS_LIMIT}


def test_every_settlement_precedes_the_parents_message_on_the_persisted_chain(
    tmp_path: Path,
) -> None:
    """G.3, on bytes: the evidence was durable before the parent could report success.

    A task the orchestrator stopped has a settlement but no `agent.message`: THY has never
    rendered a skipped task into the model-visible history and this slice does not start, so the
    ordering is asserted where a rendering exists and the absence is asserted where it does not.
    """
    log_path, _run_id, _written = _run_a_pass(tmp_path)
    events = read_events(log_path)

    message_seq = _message_seq_by_task(events)
    settled = {
        event.payload["task_id"]: event
        for event in events
        if event.type is EventType.SUBAGENT_SETTLED
    }
    assert len(settled) == 3
    rendered = [task_id for task_id in settled if task_id in message_seq]
    assert len(rendered) == 2
    for task_id in rendered:
        assert settled[task_id].seq < message_seq[task_id]
    unrendered = [settled[task_id] for task_id in settled if task_id not in message_seq]
    assert [event.payload["stop_reason"] for event in unrendered] == [StopReason.STOPPED.value]


def test_mira_grades_the_replayed_history_and_a31_passes(tmp_path: Path) -> None:
    log_path, run_id, _written = _run_a_pass(tmp_path)
    events = read_events(log_path)

    report = audit_run(AuditContext(run_id=run_id, events=events))
    a31 = next(control for control in report.controls if control.control_id == "A31")

    assert a31.status is ControlStatus.PASSED, a31.detail
    assert "A31" not in {
        control.control_id for control in report.controls if control.status is ControlStatus.FAILED
    }


def test_mira_accepts_repeated_instructions_as_distinct_delegations(tmp_path: Path) -> None:
    """A repeated instruction still identifies two separate planned child delegations."""
    run = Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )
    log_path = tmp_path / "runs" / run.id / "events.jsonl"
    log = JsonlEventLog(log_path, run.id)
    catalog = _catalog(tmp_path / "specs")
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=42, columns=("age",)),
            "not valid json",
            "still not valid json",
        ]
    )
    plan = (
        _plan_task("t1", "profile the dataset"),
        _plan_task("t2", "train something"),
        _plan_task("t3", "profile the dataset", depends_on=("t2",)),
    )

    final_state = ThyState.model_validate(
        build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY).invoke(
            ThyState(run=run, plan=plan)
        )
    )

    assert [outcome.status for outcome in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
    ]
    settlements = settlements_from_events(read_events(log_path))
    assert len(settlements) == 3
    assert len({settlement.delegation_key for settlement in settlements}) == 3
    report = audit_run(AuditContext(run_id=run.id, events=read_events(log_path)))
    a31 = next(control for control in report.controls if control.control_id == "A31")
    assert a31.status is ControlStatus.PASSED, a31.detail


def test_a_settlement_written_before_a_crash_is_still_readable(tmp_path: Path) -> None:
    """A pass dying after settling leaves the record on disk; A31 allows the crash window.

    The settlement is the durable record; the parent's `agent.message` is not. Abandoning the
    process between the two is exactly the window this ordering exists for, so the absent parent
    message is intentionally allowed until a durable inbox recovery reader exists.
    """
    run_id = new_id("run")
    log_path = tmp_path / "runs" / run_id / "events.jsonl"
    log = JsonlEventLog(log_path, run_id)
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it")
    key = delegation_key(
        run_id=run_id,
        task_id=task.id,
        agent_id=task.agent_id,
        parent_agent="thy",
        agent="data",
        objective=task.objective,
        delegation_depth=1,
    )
    log.append(
        EventType.AGENT_STARTED,
        Actor.system(),
        {
            "agent": "data",
            "task_id": task.id,
            "objective": task.objective,
            "parent_agent": "thy",
            "delegation_key": key,
            "delegation_depth": 1,
        },
        subject_id=task.agent_id,
    )
    log.append(
        EventType.AGENT_COMPLETED,
        Actor.system(),
        {
            "agent": "data",
            "task_id": task.id,
            "status": TaskStatus.FAILED.value,
            "stop_reason": StopReason.FAILED.value,
            "parent_agent": "thy",
            "delegation_key": key,
            "delegation_depth": 1,
        },
        subject_id=task.agent_id,
    )
    settlement = build_settlement(
        task=task,
        parent_agent="thy",
        agent="data",
        delegation_depth=1,
        key=key,
        stop_reason=StopReason.FAILED,
        diagnostics="the process died here",
    )
    record_settlement(log, settlement.result, Actor.system())
    # The pass is abandoned here: no `agent.message` is ever appended.

    events = read_events(log_path)
    assert verify_log(log_path).valid
    recovered = settlements_from_events(events)
    assert select_canonical(recovered) == settlement.result
    assert recovered[0].diagnostics == "the process died here"

    a31 = next(
        control
        for control in audit_run(AuditContext(run_id=run_id, events=events)).controls
        if control.control_id == "A31"
    )
    # The settlement did not vanish; what is missing is the parent's own rendering of it.
    assert a31.status is ControlStatus.PASSED
    assert not any(event.type is EventType.AGENT_MESSAGE for event in events)
