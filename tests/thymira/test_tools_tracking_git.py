"""Regression tests for the tracker id filter and the git status format argument.

Fast lane, no network. The git tests drive real ``git`` through the same sandbox the tool uses,
mirroring the existing git-tool tests in ``test_tools``; the tracker tests exercise the local
file-backed :class:`MlflowTracker` directly.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.test_tools import _context
from thymira.policies import ToolCapability
from thymira.schemas import SandboxMode
from thymira.tools import Tool, ToolManager, ToolRegistry
from thymira.tools.builtins import CompareModels, GitStatus
from thymira.tools.mlflow import MlflowTracker

if TYPE_CHECKING:
    from pathlib import Path


def _init_repo(workspace: Path) -> None:
    """Create an empty repository with one untracked file under ``workspace``."""
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "untracked.txt").write_text("hello\n", encoding="utf-8")


def test_query_runs_treats_an_empty_run_id_as_no_match_not_every_run(tmp_path: Path) -> None:
    """An empty tracker id must select nothing, never fall through to every run in the store."""
    tracker = MlflowTracker(tmp_path / "workspace")
    first = tracker.start_run("experiment-a")
    second = tracker.start_run("experiment-b")

    # Sanity: an unfiltered query does return every run, so the store is genuinely populated.
    assert {run.run_id for run in tracker.query_runs()} == {first, second}

    assert tracker.query_runs(run_id="") == ()


def test_compare_models_guard_fires_for_an_empty_run_id(tmp_path: Path) -> None:
    """With the tracker fixed, an empty id resolves to no run and re-arms the missing-id guard."""
    context = _context(
        tmp_path, capability=ToolCapability(id="compare_models", external_effects=())
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    tracker = MlflowTracker(context.workspace)
    real = tracker.start_run("candidate")
    tracker.log_metric(real, "accuracy", 0.8)
    tracker.end_run(real)

    with pytest.raises(ValueError, match="unknown tracker run"):
        CompareModels().execute(context.for_tool(), {"run_ids": ["", real], "metric": "accuracy"})


def test_git_status_returns_porcelain_format_by_default(tmp_path: Path) -> None:
    """The default, ``porcelain=True``, still returns the stable machine-readable format."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="git_status", external_effects=()),
        development=True,
    )
    _init_repo(context.workspace)
    manager = ToolManager(
        ToolRegistry((cast("Tool", GitStatus(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(context, "git_status")

    assert execution.result.success is True
    assert "?? untracked.txt" in execution.result.stdout
    assert "Untracked files:" not in execution.result.stdout


def test_git_status_honours_a_request_for_the_human_readable_long_format(tmp_path: Path) -> None:
    """Setting ``porcelain=False`` returns Git's long format instead of being ignored."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="git_status", external_effects=()),
        development=True,
    )
    _init_repo(context.workspace)
    manager = ToolManager(
        ToolRegistry((cast("Tool", GitStatus(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(context, "git_status", {"porcelain": False})

    assert execution.result.success is True
    assert "Untracked files:" in execution.result.stdout
    assert not execution.result.stdout.startswith("??")
