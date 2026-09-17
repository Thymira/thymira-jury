"""run_notebook: execute a workspace .ipynb in the sandbox and register the executed copy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, load_policy_stack
from thymira.schemas import ArtifactKind, Decision, EventType, SandboxMode, ToolCallStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import Tool, ToolContext, ToolInvocation, ToolManager, ToolRegistry
from thymira.tools.builtins.run_notebook import RunNotebook, RunNotebookValue
from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from pathlib import Path


def _notebook(*sources: str) -> str:
    """A minimal nbformat-4 notebook with one code cell per source string."""
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": source,
                }
                for source in sources
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def _invocation(tmp_path: Path) -> ToolInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def _tool() -> RunNotebook:
    # The local backend refuses confined modes; tests run it unconfined under tmp_path, exactly
    # as the run_python tests do.
    return RunNotebook(mode=SandboxMode.DANGER_FULL_ACCESS)


@pytest.mark.slow
def test_run_notebook_executes_cells_and_registers_source_and_executed_copy(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "analysis.ipynb").write_text(
        _notebook("x = 40 + 2\nprint(x)", "open('note.txt', 'w').write(str(x))"),
        encoding="utf-8",
    )

    result = _tool().execute(
        invocation, {"source_path": "analysis.ipynb", "description": "run the analysis"}
    )

    assert result.success, result.error
    assert isinstance(result.value, RunNotebookValue)
    assert result.value.path == "analysis.executed.ipynb"
    assert result.value.cells_executed == 2
    executed = json.loads(
        (invocation.workspace / "analysis.executed.ipynb").read_text(encoding="utf-8")
    )
    first_outputs = executed["cells"][0]["outputs"]
    assert any("42" in "".join(output.get("text", "")) for output in first_outputs)
    assert (invocation.workspace / "note.txt").read_text(encoding="utf-8") == "42"
    executed_artifact = invocation.artifact_store.get("analysis.executed.ipynb")
    assert executed_artifact is not None
    assert executed_artifact.kind is ArtifactKind.CODE
    assert executed_artifact.media_type == "application/x-ipynb+json"
    assert executed_artifact.id == result.value.artifact_id
    assert result.artifact_ids == (executed_artifact.id,)
    # The source is write_file's to register; run_notebook never supersedes its kind.
    assert invocation.artifact_store.get("analysis.ipynb") is None


@pytest.mark.slow
def test_run_notebook_reports_a_failing_cell_and_registers_nothing(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "broken.ipynb").write_text(
        _notebook("print('before')", "raise RuntimeError('cell exploded')"), encoding="utf-8"
    )

    result = _tool().execute(
        invocation, {"source_path": "broken.ipynb", "description": "run the broken notebook"}
    )

    assert result.success is False
    assert result.exit_code not in (None, 0)
    assert "cell exploded" in (result.error or "")
    assert result.artifact_ids == ()
    assert not (invocation.workspace / "broken.executed.ipynb").exists()


def test_run_notebook_refuses_paths_that_escape_the_workspace(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)

    with pytest.raises(ToolExecutionError):
        _tool().execute(invocation, {"source_path": "../outside.ipynb", "description": "x"})


def test_run_notebook_refuses_a_non_notebook_source(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "script.py").write_text("print(1)\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match=r"\.ipynb"):
        _tool().execute(invocation, {"source_path": "script.py", "description": "x"})


def test_run_notebook_refuses_to_overwrite_its_source(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.ipynb").write_text(_notebook("1"), encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="overwrite"):
        _tool().execute(
            invocation, {"source_path": "a.ipynb", "path": "a.ipynb", "description": "x"}
        )


def test_run_notebook_refuses_invalid_notebook_json(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.ipynb").write_text("{not json", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="not a valid notebook"):
        _tool().execute(invocation, {"source_path": "a.ipynb", "description": "x"})


def test_run_notebook_is_reviewed_like_run_python_under_the_base_policy(tmp_path: Path) -> None:
    """A notebook is code: GOV-008 asks a human before it runs, exactly as for run_python."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack("credit_risk")), log, approver=None),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="medium", activity_category="data_analysis", confidence=1.0
        ),
    )
    tool = cast("Tool", RunNotebook())

    execution = ToolManager(ToolRegistry((tool,))).execute(
        context, "run_notebook", {"source_path": "a.ipynb", "description": "x"}
    )

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is not None
    assert execution.pending_approval.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert execution.pending_approval.rule_id == "GOV-008"
    assert log.events()[-1].type is EventType.TOOL_DENIED


def test_run_notebook_is_a_registered_builtin_with_the_code_execution_capability() -> None:
    tools: dict[str, Any] = {tool.name: tool for tool in builtins_registry()}

    assert "run_notebook" in tools
    capability = tools["run_notebook"].capability
    assert "code_execution" in capability.risk_tags
    assert capability.side_effects == ("workspace_write",)
    assert capability.external_effects == ()
