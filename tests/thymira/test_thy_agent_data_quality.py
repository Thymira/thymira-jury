"""The Data Quality agent: AgentSpec + DataQualityReport output schema (THY-20)."""

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
from thymira.thy import (
    DATA_QUALITY_AGENT_SPEC,
    DataQualityReport,
    data_quality_agent_catalog,
)
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import ReadFile, RunPython

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


def test_data_quality_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = data_quality_agent_catalog()

    assert catalog.get("data-quality-agent") is DATA_QUALITY_AGENT_SPEC
    assert catalog.output_schema("data-quality-agent") is DataQualityReport
    assert catalog.system_prompt("data-quality-agent")


def test_data_quality_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (cast("Tool", ReadFile()), cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)))
    )

    tools = build_agent_tools(DATA_QUALITY_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {"read_file", "run_python"}


def test_data_quality_agent_flags_a_leakage_risk_and_a_sensitive_attribute(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "credit.csv").write_text(
        "age,gender,approved_amount,default\n25,F,3000,0\n40,M,0,1\n55,F,5000,0\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(
        (cast("Tool", ReadFile()), cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)))
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="read_file", arguments={"path": "credit.csv"}),
            LLMToolCall(
                id="call-2",
                name="final_result",
                arguments={
                    "leakage_risks": (
                        "approved_amount is 0 exactly when default is 1, a post-outcome value",
                    ),
                    "imbalance": (),
                    "drift_signals": (),
                    "sensitive_attributes": ("gender", "age"),
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Flag data quality risks before this dataset is used to train a model.",
    )

    result = AgentRunner().run(
        DATA_QUALITY_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=data_quality_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    output = cast("DataQualityReport", result.output)
    assert output.leakage_risks
    assert "gender" in output.sensitive_attributes
