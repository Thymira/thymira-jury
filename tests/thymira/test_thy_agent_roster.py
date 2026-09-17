"""Specialist roster: ThyGraph dispatches to a FINAL agent through the composed catalog (THY-32)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.agents import LLMToolCall, ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.policies import (
    ActionRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    auto_approve,
)
from thymira.schemas import (
    Decision,
    EventType,
    ModelRoutePolicy,
    Run,
    SandboxMode,
    TaskStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.thy import (
    DATA_QUALITY_AGENT_SPEC,
    STATISTICS_AGENT_SPEC,
    VISUALIZATION_AGENT_SPEC,
    AgentTask,
    PlanOutput,
    StatsResult,
    ThyAgentKind,
    ThyInput,
    ThyPhase,
    ThyState,
    build_thy_graph,
    full_agent_catalog,
    run_thy,
)
from thymira.tools import Tool, ToolContext, ToolRegistry, register_dataset
from thymira.tools.builtins import RunPython, RunStatistics

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct build_thy_graph/run_thy provider seams to one code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_PLAN_PASSES = Policy(
    name="plan-passes",
    version="1.0",
    action_rules=(
        ActionRule(
            id="TEST-PLAN",
            action_types=("plan.proposed",),
            decision=Decision.PASS,
            reason="test policy: THY's plan needs no review",
        ),
    ),
)

_STATISTICS_INSTRUCTION = "Test whether value differs between groups A and B."


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def _scripted_statistics_plan() -> ScriptedProvider:
    """A plan naming the statistics agent, then that agent's tool call and its StatsResult."""
    plan = PlanOutput(
        tasks=(
            AgentTask(
                id="t1",
                agent=ThyAgentKind.STATISTICS,
                phase=ThyPhase.EXECUTE,
                instruction=_STATISTICS_INSTRUCTION,
            ),
        )
    )
    return ScriptedProvider(
        [
            plan,
            LLMToolCall(
                id="call-1",
                name="run_statistics",
                arguments={
                    "dataset": "scores",
                    "value_column": "value",
                    "group_column": "grp",
                    "test": "welch_t",
                    "description": "Compare values across score groups",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="final_result",
                arguments={
                    "test": "welch_t",
                    "statistic": -9.0,
                    "p_value": 0.012,
                    "effect_size": "large",
                    "assumptions": ("independence", "equal variance not assumed"),
                    "caveats": ("only two values per group",),
                },
            ),
        ]
    )


def _scenario(tmp_path: Path) -> tuple[Run, InMemoryEventLog, Gate, ToolContext, ToolRegistry]:
    """Build the run, shared log, plan-passing gate, tool context and registry for a graph run."""
    run = _run()
    log = InMemoryEventLog(run.id)
    gate = Gate(
        PolicyEngine(development_policy().merged_with(_PLAN_PASSES)), log, approver=auto_approve
    )
    context = ToolContext(
        run_id=run.id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=gate,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "scores.csv"
    source.write_text("value,grp\n1,A\n2,A\n10,B\n12,B\n", encoding="utf-8")
    register_dataset(context.artifact_store, source, "scores", produced_by=context.agent_id)
    registry = ToolRegistry(
        (
            cast("Tool", RunStatistics()),
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
        )
    )
    return run, log, gate, context, registry


def test_thy_agent_kind_values_match_the_shipped_final_spec_names() -> None:
    assert ThyAgentKind.STATISTICS.value == STATISTICS_AGENT_SPEC.name
    assert ThyAgentKind.DATA_QUALITY.value == DATA_QUALITY_AGENT_SPEC.name
    assert ThyAgentKind.VISUALIZATION.value == VISUALIZATION_AGENT_SPEC.name


def test_full_agent_catalog_holds_every_mvp_and_shipped_final_specialist() -> None:
    catalog = full_agent_catalog()

    assert set(catalog.names()) >= {
        "data",
        "coding",
        "experiment",
        "statistics-agent",
        "data-quality-agent",
        "visualization-agent",
        "ml-agent",
        "research-agent",
    }
    # Every reachable FINAL kind resolves to a spec with a prompt and an output schema.
    for kind in (ThyAgentKind.STATISTICS, ThyAgentKind.DATA_QUALITY, ThyAgentKind.VISUALIZATION):
        assert catalog.get(kind.value).name == kind.value
        assert catalog.system_prompt(kind.value)
        assert catalog.output_schema(kind.value)


def test_the_graph_dispatches_a_planned_statistics_task_to_the_final_agent(tmp_path: Path) -> None:
    run, log, gate, context, registry = _scenario(tmp_path)
    catalog = full_agent_catalog()
    graph = build_thy_graph(
        catalog,
        log,
        provider=_scripted_statistics_plan(),
        gate=gate,
        tool_registry=registry,
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )

    raw_result = graph.invoke(ThyState(run=run))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.error is None
    assert final_state.phase is ThyPhase.SUMMARIZE
    assert len(final_state.completed) == 1
    assert final_state.completed[0].agent is ThyAgentKind.STATISTICS

    # The delegated Task reached COMPLETED with a StatsResult as its output.
    message = final_state.agent_messages[0]
    assert message.status is TaskStatus.COMPLETED
    assert message.summary is not None
    stats = StatsResult.model_validate_json(message.summary)
    assert stats.test == "welch_t"
    assert stats.statistic == -9.0

    # AgentRunner ran the FINAL agent for real, through the Tool Manager -- not a stubbed path.
    completed = [
        event
        for event in log.events()
        if event.type is EventType.AGENT_COMPLETED
        and event.payload.get("agent") == "statistics-agent"
    ]
    assert len(completed) == 1
    assert completed[0].payload["status"] == TaskStatus.COMPLETED.value
    tool_completions = [
        event.payload["tool"] for event in log.events() if event.type is EventType.TOOL_COMPLETED
    ]
    assert "run_statistics" in tool_completions
    decisions = [
        event.payload["decision"]
        for event in log.events()
        if event.type is EventType.POLICY_DECISION
    ]
    assert Decision.PASS.value in decisions


def test_run_thy_completes_the_statistics_run_through_the_composed_catalog(tmp_path: Path) -> None:
    run, log, gate, context, registry = _scenario(tmp_path)
    catalog = full_agent_catalog()

    output = run_thy(
        ThyInput(run=run),
        catalog,
        log,
        provider=_scripted_statistics_plan(),
        gate=gate,
        tool_registry=registry,
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )

    assert output.run_id == run.id
    assert output.error is None
    # The statistics report artifact the FINAL agent produced was folded onto the run's output.
    assert len(output.artifact_ids) >= 1
