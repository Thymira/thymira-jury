"""The Visualization agent: AgentSpec + PlotResult output schema (THY-21)."""

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
from thymira.schemas import ArtifactKind, ModelRoutePolicy, SandboxMode, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import VISUALIZATION_AGENT_SPEC, PlotResult, visualization_agent_catalog
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import RunPython, WriteFile

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'


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


def test_visualization_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = visualization_agent_catalog()

    assert catalog.get("visualization-agent") is VISUALIZATION_AGENT_SPEC
    assert catalog.output_schema("visualization-agent") is PlotResult
    assert catalog.system_prompt("visualization-agent")


def test_visualization_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)), cast("Tool", WriteFile()))
    )

    tools = build_agent_tools(VISUALIZATION_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {"run_python", "write_file"}


def test_visualization_agent_produces_a_registered_plot_artifact(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)), cast("Tool", WriteFile()))
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-1",
                name="run_python",
                arguments={
                    "code": f"print({_SVG!r})",
                    "description": "Print the placeholder SVG plot",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="write_file",
                arguments={
                    "path": "plot.svg",
                    "content": _SVG,
                    "kind": "plot",
                    "description": "Write the placeholder SVG plot file",
                },
            ),
            LLMToolCall(
                id="call-3",
                name="final_result",
                arguments={
                    "plots": [
                        {
                            "artifact_id": "artifact_plot",
                            "caption": "An empty placeholder plot for the test fixture.",
                        }
                    ]
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Render a plot for the dataset.",
    )

    result = AgentRunner().run(
        VISUALIZATION_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=visualization_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    active = [
        artifact
        for artifact in context.artifact_store.list_active()
        if artifact.kind is ArtifactKind.PLOT and artifact.name == "plot.svg"
    ]
    assert len(active) == 1
    artifact = active[0]
    assert artifact.kind is ArtifactKind.PLOT
    assert artifact.sha256
    output = cast("PlotResult", result.output)
    assert output.plots[0].caption
