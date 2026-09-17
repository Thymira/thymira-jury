"""The runtime-context snapshot the runner sends before every step (harness basics 1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents.runtime_context import MAX_PROJECT_CONTEXT_CHARS, runtime_context_text
from thymira.schemas import new_id
from thymira.state import LocalArtifactStore
from thymira.tools.datasets import register_dataset

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts", new_id("run"))


def test_snapshot_lists_registered_datasets_with_shape_and_first_columns(tmp_path: Path):
    store = _store(tmp_path)
    csv = tmp_path / "toy.csv"
    csv.write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8")
    register_dataset(store, csv, "toy", produced_by=new_id("agent"))

    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=store)

    assert text.startswith(
        "Current runtime context. This snapshot supersedes earlier runtime-context snapshots."
    )
    assert "Registered datasets: toy (2 rows, 3 columns: a, b, c)" in text
    assert "datasets/toy.csv (dataset)" not in text
    assert "toy.schema.json" not in text.split("Artifacts present:")[1]


def test_snapshot_names_the_declared_workspace_path_not_the_artifact_store_key(tmp_path: Path):
    """A coding agent receives the path that its filesystem tools can actually read."""
    store = _store(tmp_path)
    source = tmp_path / "applications.csv"
    source.write_text("credit_amount\n100\n", encoding="utf-8")
    register_dataset(store, source, "german_credit", produced_by=new_id("agent"))

    text = runtime_context_text(
        workspace=tmp_path / "workspace",
        artifact_store=store,
        workspace_dataset_paths=(("german_credit", "data/applications.csv"),),
    )

    assert "german_credit: data/applications.csv" in text
    assert "datasets/german_credit.csv (dataset)" not in text


def test_snapshot_says_none_and_how_to_fix_it_when_nothing_is_registered(tmp_path: Path):
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "Registered datasets: none — a dataset must be registered" in text
    assert "Artifacts present: none" in text


def test_snapshot_names_the_sandbox_mode_and_that_a_denial_is_policy(tmp_path: Path):
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "File sandbox: workspace-write" in text
    assert "a policy denial, not a bug in the code" in text


def test_snapshot_reports_installed_libraries_from_the_real_interpreter(tmp_path: Path):
    """Bug-hunt C4: a coding step must not discover the environment by crashing.

    `run_python` shares this interpreter, so what `importlib.metadata` resolves here is exactly
    what a subprocess it starts can import -- pandas is a hard dependency of `thymira-tools`
    (this test's own member), so it must always be listed as installed.
    """
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "Installed libraries (run_python's interpreter):" in text
    assert "pandas==" in text
    assert "matplotlib==" in text


def test_snapshot_omits_project_context_when_absent(tmp_path: Path):
    text = runtime_context_text(workspace=tmp_path / "ws", artifact_store=_store(tmp_path))

    assert "Project context" not in text


def test_snapshot_includes_the_projects_own_context_when_present(tmp_path: Path):
    """Bug-hunt H4: every delegated step gets this snapshot, not just THY's own Plan."""
    text = runtime_context_text(
        workspace=tmp_path / "ws",
        artifact_store=_store(tmp_path),
        project_context="This project scores credit-risk applications.",
    )

    assert (
        "Project context (.thymira/context.md):\nThis project scores credit-risk applications."
        in text
    )


def test_snapshot_truncates_an_overlong_project_context(tmp_path: Path):
    text = runtime_context_text(
        workspace=tmp_path / "ws",
        artifact_store=_store(tmp_path),
        project_context="x" * (MAX_PROJECT_CONTEXT_CHARS + 500),
    )

    assert f"{'x' * MAX_PROJECT_CONTEXT_CHARS} …[truncated]" in text
    assert "x" * (MAX_PROJECT_CONTEXT_CHARS + 1) not in text
