"""The Statistics agent: AgentSpec + StatsResult output schema (THY-18)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.agents import (
    AgentContext,
    AgentRunner,
    LLMToolCall,
    ScriptedProvider,
    build_agent_tools,
)
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve
from thymira.schemas import ModelRoutePolicy, SandboxMode, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import STATISTICS_AGENT_SPEC, StatsResult, statistics_agent_catalog
from thymira.tools import Tool, ToolContext, ToolRegistry, register_dataset
from thymira.tools.builtins import RunPython, RunStatistics

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _tool_context(tmp_path: Path, *, run_id: str) -> ToolContext:
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_statistics_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = statistics_agent_catalog()

    assert catalog.get("statistics-agent") is STATISTICS_AGENT_SPEC
    assert catalog.output_schema("statistics-agent") is StatsResult
    assert catalog.system_prompt("statistics-agent")


def test_statistics_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (
            cast("Tool", RunStatistics()),
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
        )
    )

    tools = build_agent_tools(STATISTICS_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {"run_statistics", "run_python"}


def test_statistics_agent_runs_a_real_test_against_a_registered_dataset(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
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
    provider = ScriptedProvider(
        [
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
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Test whether value differs between groups A and B.",
    )

    result = AgentRunner().run(
        STATISTICS_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=statistics_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    output = cast("StatsResult", result.output)
    assert output.test == "welch_t"
    assert output.assumptions == ("independence", "equal variance not assumed")
