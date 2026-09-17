"""The Coding/Execution agent: AgentSpec + CodeResult output schema (THY-15)."""

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
from thymira.schemas import EventType, ModelRoutePolicy, SandboxMode, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import CODING_AGENT_SPEC, CodeResult, ThyAgentKind, coding_agent_catalog
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import (
    ExportPdf,
    GitCommit,
    GitDiff,
    ReadFile,
    RunNotebook,
    RunPython,
    WriteFile,
)

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


def test_coding_agent_spec_name_matches_thy_agent_kind_coding() -> None:
    """Execute resolves a delegated task via `catalog.get(agent_task.agent.value)`."""
    assert CODING_AGENT_SPEC.name == ThyAgentKind.CODING.value


def test_coding_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = coding_agent_catalog()

    assert catalog.get("coding") is CODING_AGENT_SPEC
    assert catalog.output_schema("coding") is CodeResult
    prompt = catalog.system_prompt("coding")
    assert prompt
    normalized_prompt = " ".join(prompt.split())
    assert "every quantitative value the report will cite" in normalized_prompt
    assert 'register each JSON file as `kind="metrics"`' in normalized_prompt


def test_coding_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (
            cast("Tool", WriteFile()),
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", ReadFile()),
            cast("Tool", ExportPdf()),
            cast("Tool", RunNotebook(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", GitDiff()),
            cast("Tool", GitCommit()),
        )
    )

    tools = build_agent_tools(CODING_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {
        "write_file",
        "run_python",
        "read_file",
        "export_pdf",
        "run_notebook",
    }


def test_coding_agent_writes_runs_and_reads_back_through_real_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (
            cast("Tool", WriteFile()),
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", ReadFile()),
            cast("Tool", ExportPdf()),
            cast("Tool", RunNotebook(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", GitDiff()),
        )
    )
    script = (
        "from pathlib import Path\n"
        "print('done')\n"
        "Path('result.txt').write_text('42', encoding='utf-8')\n"
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-1",
                name="write_file",
                arguments={
                    "path": "script.py",
                    "content": script,
                    "description": "Write the result-printing script",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="run_python",
                arguments={"code": script, "description": "Run the result-printing script"},
            ),
            LLMToolCall(id="call-3", name="read_file", arguments={"path": "result.txt"}),
            LLMToolCall(
                id="call-4",
                name="final_result",
                arguments={"stdout": "done\n", "exit_code": 0, "artifacts": ["result.txt"]},
            ),
        ]
    )
    task = Task(
        id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="Run the script."
    )

    result = AgentRunner().run(
        CODING_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=coding_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    assert result.output == CodeResult(stdout="done\n", exit_code=0, artifacts=("result.txt",))
    assert (context.workspace / "result.txt").read_text(encoding="utf-8") == "42"
    event_types = [event.type for event in context.event_log.events()]
    assert EventType.TOOL_STARTED in event_types
    assert EventType.TOOL_COMPLETED in event_types
