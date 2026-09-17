"""The Data agent: AgentSpec + DataProfile output schema (THY-14)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from thymira.agents import (
    AgentContext,
    AgentRunner,
    LLMToolCall,
    ScriptedProvider,
    build_agent_tools,
)
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve, load_policy_stack
from thymira.schemas import ModelRoutePolicy, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import DATA_AGENT_SPEC, DataProfile, ThyAgentKind, data_agent_catalog
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import ListFiles, ProfileDataset, QuerySql, ReadFile, WriteFile
from thymira.tools.builtins.search import Glob

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
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_data_agent_spec_name_matches_thy_agent_kind_data() -> None:
    """Execute resolves a delegated task via `catalog.get(agent_task.agent.value)`."""
    assert DATA_AGENT_SPEC.name == ThyAgentKind.DATA.value


def test_data_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = data_agent_catalog()

    assert catalog.get("data") is DATA_AGENT_SPEC
    assert catalog.output_schema("data") is DataProfile
    assert catalog.system_prompt("data")


def test_data_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (
            cast("Tool", ReadFile()),
            cast("Tool", ListFiles()),
            cast("Tool", WriteFile()),
            cast("Tool", Glob()),
            cast("Tool", ProfileDataset()),
            cast("Tool", QuerySql()),
        )
    )

    tools = build_agent_tools(DATA_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {
        "glob",
        "read_file",
        "profile_dataset",
        "query_sql",
    }


def test_data_agent_profiles_a_real_csv_through_glob_and_read_file(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "credit.csv").write_text(
        "age,income,default\n25,30000,0\n40,,1\n55,52000,0\n", encoding="utf-8"
    )
    registry = ToolRegistry(
        (
            cast("Tool", ReadFile()),
            cast("Tool", Glob()),
            cast("Tool", ProfileDataset()),
            cast("Tool", QuerySql()),
        )
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="glob", arguments={"pattern": "*.csv"}),
            LLMToolCall(id="call-2", name="read_file", arguments={"path": "credit.csv"}),
            LLMToolCall(
                id="call-3",
                name="final_result",
                arguments={
                    "columns": ["age", "income", "default"],
                    "dtypes": {"age": "int", "income": "float", "default": "int"},
                    "missing": {"age": 0, "income": 1, "default": 0},
                    "target_candidates": ["default"],
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="Profile the dataset."
    )

    result = AgentRunner().run(
        DATA_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=data_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    assert result.output == DataProfile(
        columns=("age", "income", "default"),
        dtypes={"age": "int", "income": "float", "default": "int"},
        missing={"age": 0, "income": 1, "default": 0},
        target_candidates=("default",),
    )
