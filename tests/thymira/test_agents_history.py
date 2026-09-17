"""Agent conversation compaction without losing current workspace state or failure evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import development_policy
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentRunner,
    AgentSpec,
    LLMMessage,
    LLMToolCall,
    ScriptedProvider,
)
from thymira.agents.llm.routing import Role
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve
from thymira.schemas import EventType, ModelRoutePolicy, Task, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import WriteFile

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")
_OLD_CONTENT = "old report line\n" * 1_000
_NEW_CONTENT = "current report line\n" * 1_000
_COMPACTED_CONTENT = (
    "[content omitted: superseded by a later successful write_file to the same path]"
)


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _run_two_writes(
    tmp_path: Path, *, second_path: str
) -> tuple[list[LLMMessage], InMemoryEventLog]:
    """Run two write turns and capture the exact history shown to the final model turn."""
    run_id = new_id("run")
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Write the report and return its profile.",
    )
    log = InMemoryEventLog(run_id)
    spec = AgentSpec(
        name="coding",
        role=Role.AGENT,
        task_kinds=("code",),
        tool_allowlist=("write_file",),
        max_turns=3,
        max_depth=1,
        system_prompt_ref="prompts/coding.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    catalog = AgentCatalog(
        (spec,),
        system_prompts={spec.name: "You are a coding agent."},
        output_schemas={spec.name: DataProfileOutput},
    )
    captured: dict[str, list[LLMMessage]] = {}

    def finish(messages: Sequence[LLMMessage]) -> LLMToolCall:
        captured["messages"] = list(messages)
        return LLMToolCall(
            id="call-final",
            name="final_result",
            arguments={"row_count": 1, "columns": ["value"]},
        )

    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-old",
                name="write_file",
                arguments={
                    "path": "report.md",
                    "content": _OLD_CONTENT,
                    "description": "Write the first report version.",
                },
            ),
            LLMToolCall(
                id="call-new",
                name="write_file",
                arguments={
                    "path": second_path,
                    "content": _NEW_CONTENT,
                    "description": "Write the replacement report version.",
                },
            ),
            finish,
        ]
    )
    tool_context = ToolContext(
        run_id=run_id,
        agent_id=task.agent_id,
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    AgentRunner().run(
        spec,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=catalog,
            event_log=log,
            provider=provider,
            tool_registry=ToolRegistry((cast("Tool", WriteFile()),)),
            tool_context=tool_context,
        ),
    )

    return captured["messages"], log


def _write_arguments(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Collect provider-visible write arguments in conversation order."""
    return [
        call.arguments
        for message in messages
        for call in message.tool_calls
        if call.name == "write_file"
    ]


def test_runner_compacts_only_the_content_replaced_by_a_successful_later_write(
    tmp_path: Path,
) -> None:
    """The model sees the current file in full while the event log retains both real writes."""
    messages, log = _run_two_writes(tmp_path, second_path="report.md")

    writes = _write_arguments(messages)
    assert [write["content"] for write in writes] == [_COMPACTED_CONTENT, _NEW_CONTENT]
    started_contents = [
        event.payload["arguments"]["content"]
        for event in log.events()
        if event.type is EventType.TOOL_STARTED and event.payload.get("tool") == "write_file"
    ]
    assert started_contents == [_OLD_CONTENT, _NEW_CONTENT]


def test_runner_keeps_the_previous_content_when_the_later_write_failed(tmp_path: Path) -> None:
    """A refused replacement cannot erase the last version that actually reached the workspace."""
    messages, _log = _run_two_writes(tmp_path, second_path="../outside.md")

    writes = _write_arguments(messages)
    assert [write["content"] for write in writes] == [_OLD_CONTENT, _NEW_CONTENT]
