"""THY <-> MIRA evaluator-optimizer rework loop: a policy.decision reopens a phase (THY-24).

The rework loop is budgeted and never infinite: a persistent rework signal reopens a reopenable
phase (PREPARATION/MODELING) up to `max_reopens` times, re-running the plan each time, then stops
with a budgeted, justified refusal instead of looping; a signal naming a non-reopenable phase is
refused without ever reopening. THY's reopen rules mirror `thymira.core.phases` exactly -- this
test locks that mirror so the two can never drift (THY may not import core; the layer order forbids
it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import AgentCatalog, load_agent_specs
from thymira.agents.llm.base import BaseModelT, LLMCallError, LLMResponse
from thymira.core.phases import PHASE_ORDER as CORE_PHASE_ORDER
from thymira.core.phases import REOPENABLE_PHASES as CORE_REOPENABLE_PHASES
from thymira.core.phases import Phase
from thymira.core.phases import can_reopen as core_can_reopen
from thymira.events import InMemoryEventLog
from thymira.schemas import Decision, EventType, PolicyDecision, Run, new_id
from thymira.thy import ReworkSignal
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyState
from thymira.thy.nodes.rework import PHASE_ORDER, REOPENABLE_PHASES, can_reopen

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.agents.llm.base import LLMMessage, LLMToolDefinition


class _ConstantProvider:
    """Return one fixed structured output for every call, so a re-run never exhausts a script."""

    provider_name = "test"
    model = "scripted"

    def __init__(self, output: BaseModel) -> None:
        self._output = output

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModelT], system: str = ""
    ) -> tuple[BaseModelT, LLMResponse]:
        del prompt, system
        validated = schema.model_validate(self._output.model_dump())
        return validated, LLMResponse(text=validated.model_dump_json(), provider="test", model="s")

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        raise LLMCallError("complete is not used in these tests")

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        del messages, tools, parallel_tool_calls
        raise LLMCallError("complete_turn is not used in these tests")


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def _data_agent_catalog(tmp_path: Path) -> AgentCatalog:
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
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _plan() -> tuple[AgentTask, ...]:
    return (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )


def _rework_signal(run: Run, *, from_phase: str, target_phase: str) -> ReworkSignal:
    decision = PolicyDecision(
        id=new_id("decision"),
        run_id=run.id,
        subject_kind="findings",
        subject_id="findings",
        decision=Decision.REQUIRE_HUMAN_REVIEW,
        rule_id="MIRA-REWORK",
        reason="audit found data leakage; the preparation phase must be redone",
        policy_name="test@1.0",
        policy_sha256="a" * 64,
    )
    return ReworkSignal(
        decision=decision,
        from_phase=from_phase,
        target_phase=target_phase,
        justification="MIRA flagged leakage in preparation; redo it",
    )


def _starts(log: InMemoryEventLog) -> int:
    return len([e for e in log.events() if e.type is EventType.AGENT_STARTED])


@pytest.mark.parametrize("target_phase", ["preparation", "modeling"])
def test_a_reopenable_phase_is_reopened_the_tasks_re_run_and_the_budget_stops_the_loop(
    tmp_path: Path, target_phase: str
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = _ConstantProvider(DataProfileOutput(row_count=1, columns=("a",)))
    signal = _rework_signal(run, from_phase="evaluation", target_phase=target_phase)
    graph = build_thy_graph(catalog, log, provider=provider)

    start = ThyState(run=run, plan=_plan(), rework=signal, max_reopens=2)
    final_state = ThyState.model_validate(graph.invoke(start))

    # The phase was reopened up to the budget: two reopens, so the one-task plan ran three times.
    assert final_state.reopen_count == 2
    assert _starts(log) == 3
    # Then the loop stopped with a budgeted, justified refusal -- never an endless loop.
    assert final_state.rework is None
    assert final_state.rework_refusal is not None
    assert "budget" in final_state.rework_refusal
    # A refusal is not a failure: the run still finished at Summarize.
    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None


@pytest.mark.parametrize(
    ("from_phase", "target_phase", "expected"),
    [
        ("evaluation", "reporting", "cannot be reopened"),
        ("evaluation", "understanding", "cannot be reopened"),
        ("preparation", "modeling", "does not precede"),
    ],
)
def test_a_non_reopenable_target_is_refused_and_never_reopened(
    tmp_path: Path, from_phase: str, target_phase: str, expected: str
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = _ConstantProvider(DataProfileOutput(row_count=1, columns=("a",)))
    signal = _rework_signal(run, from_phase=from_phase, target_phase=target_phase)
    graph = build_thy_graph(catalog, log, provider=provider)

    start = ThyState(run=run, plan=_plan(), rework=signal, max_reopens=2)
    final_state = ThyState.model_validate(graph.invoke(start))

    # Never reopened: the plan ran exactly once, and the refusal names why.
    assert final_state.reopen_count == 0
    assert _starts(log) == 1
    assert final_state.rework is None
    assert final_state.rework_refusal is not None
    assert expected in final_state.rework_refusal
    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None


def test_a_run_with_no_rework_signal_is_unaffected(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = _ConstantProvider(DataProfileOutput(row_count=1, columns=("a",)))
    graph = build_thy_graph(catalog, log, provider=provider)

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=_plan())))

    assert final_state.reopen_count == 0
    assert final_state.rework_refusal is None
    assert _starts(log) == 1
    assert final_state.phase is ThyPhase.SUMMARIZE


def test_thy_reopen_rules_mirror_core_phases() -> None:
    """THY reimplements `can_reopen` locally (it may not import core); lock the mirror by value."""
    assert {phase.value for phase in CORE_REOPENABLE_PHASES} == REOPENABLE_PHASES
    assert tuple(phase.value for phase in CORE_PHASE_ORDER) == PHASE_ORDER
    cases = [
        ("evaluation", "preparation", 0, 2),
        ("evaluation", "preparation", 2, 2),
        ("evaluation", "reporting", 0, 2),
        ("preparation", "modeling", 0, 2),
        ("reporting", "modeling", 1, 3),
    ]
    for current, target, reopen_count, max_reopens in cases:
        thy = can_reopen(
            current, target, reopen_count=reopen_count, max_reopens=max_reopens, justification="j"
        )
        core = core_can_reopen(
            Phase(current),
            Phase(target),
            reopen_count=reopen_count,
            max_reopens=max_reopens,
            justification="j",
        )
        assert thy[0] == core[0]
