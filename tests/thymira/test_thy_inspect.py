"""Inspect node + project-context loader (THY-10)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from thymira.agents import AgentCatalog
from thymira.events import InMemoryEventLog
from thymira.schemas import Framework, Run, new_id
from thymira.state import LocalArtifactStore
from thymira.thy.context import load_project_context
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import ThyPhase, ThyState
from thymira.thy.nodes.inspect import inspect_node

if TYPE_CHECKING:
    import pytest

_CREDIT_RISK_DIR = Path(__file__).resolve().parents[2] / "examples" / "credit-risk"

_TINY_CSV = "colour,size,label\nred,1,yes\nblue,2,no\ngreen,3,yes\nred,4,no\n"


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def test_load_project_context_reads_config_and_context_for_credit_risk() -> None:
    context = load_project_context(_CREDIT_RISK_DIR)

    assert context.config is not None
    assert context.config.project.domain == "credit_risk"
    assert context.config.governance.frameworks == (Framework.EU_AI_ACT, Framework.CREDIT_RISK)
    assert [dataset.name for dataset in context.config.datasets] == ["german_credit"]
    assert context.config.datasets[0].target == "is_high_risk"
    assert "credit-risk" in context.context_md


def test_a_project_without_thymira_degrades_to_an_empty_but_valid_context(tmp_path: Path) -> None:
    context = load_project_context(tmp_path)

    assert context.config is None
    assert context.context_md == ""


def test_inspect_node_seeds_thy_state_with_domain_and_frameworks() -> None:
    log = InMemoryEventLog(new_id("run"))
    graph = build_thy_graph(AgentCatalog(), log, project_dir=_CREDIT_RISK_DIR)

    raw_result = graph.invoke(ThyState(run=_run()))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.domain == "credit_risk"
    assert final_state.governance_frameworks == (Framework.EU_AI_ACT, Framework.CREDIT_RISK)
    assert final_state.project_context is not None
    assert "credit-risk" in final_state.project_context


def test_inspect_node_degrades_gracefully_for_a_project_without_thymira(tmp_path: Path) -> None:
    log = InMemoryEventLog(new_id("run"))
    graph = build_thy_graph(AgentCatalog(), log, project_dir=tmp_path)

    raw_result = graph.invoke(ThyState(run=_run()))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.domain is None
    assert final_state.governance_frameworks == ()
    assert final_state.project_context is None


def _project(tmp_path: Path, *, csv_text: str | None = _TINY_CSV) -> Path:
    """Write a project that declares one dataset; `csv_text=None` declares a file that is absent."""
    project_dir = tmp_path / "project"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / ".thymira" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "domain": "credit_risk"},
                "datasets": [
                    {"name": "applications", "path": "data/applications.csv", "target": "label"}
                ],
            }
        ),
        encoding="utf-8",
    )
    if csv_text is not None:
        (project_dir / "data").mkdir()
        (project_dir / "data" / "applications.csv").write_text(
            csv_text, encoding="utf-8", newline="\n"
        )
    return project_dir


def test_inspect_registers_a_declared_dataset_into_the_run(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)

    raw = inspect_node(_project(tmp_path), artifact_store=store)(ThyState(run=run))
    state = ThyState.model_validate(raw)

    assert state.error is None
    assert [d.name for d in state.datasets] == ["applications"]
    assert state.datasets[0].target == "label"
    names = {artifact.name: artifact for artifact in store.list_active()}
    assert names["datasets/applications.csv"].produced_by == run.id
    schema = store.load_json("datasets/applications.schema.json")
    assert schema["row_count"] == 4
    assert schema["columns"] == ["colour", "size", "label"]


def test_inspect_does_not_register_a_dataset_twice_on_resume(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    node = inspect_node(_project(tmp_path), artifact_store=store)

    first = ThyState.model_validate(node(ThyState(run=run)))
    first_artifact = store.get("datasets/applications.csv")
    assert first_artifact is not None
    second = ThyState.model_validate(node(ThyState(run=run)))
    second_artifact = store.get("datasets/applications.csv")

    assert [d.name for d in second.datasets] == [d.name for d in first.datasets] == ["applications"]
    # The manifest still names one artifact under this key -- the second call is a genuine no-op,
    # not a second write that a name-keyed count would fail to distinguish from the first.
    assert second_artifact is not None
    assert second_artifact.id == first_artifact.id
    assert not any(name.startswith(".history/") for name in store.manifest())
    assert store.verify() == []


def test_inspect_halts_the_run_when_a_declared_dataset_is_missing(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)

    state = ThyState.model_validate(
        inspect_node(_project(tmp_path, csv_text=None), artifact_store=store)(ThyState(run=run))
    )

    assert state.error == (
        "dataset 'applications' declared in .thymira/config.yaml is missing: data/applications.csv"
    )
    assert state.datasets == ()
    assert store.list_active() == []


def test_inspect_reports_why_a_declared_dataset_could_not_be_registered(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    project_dir = _project(tmp_path, csv_text="a,b\n1,2\n3\n")

    state = ThyState.model_validate(
        inspect_node(project_dir, artifact_store=store)(ThyState(run=run))
    )

    assert state.error is not None
    assert state.error.startswith("dataset 'applications' could not be registered: ")
    assert "lines 3" in state.error


def test_inspect_records_an_os_error_from_registration_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` is recorded on `ThyState.error` like any other registration failure.

    A missing permission or a store failure is never raised uncaught -- covering
    `_register_declared`'s `OSError` arm alongside its existing `ValueError` handling.
    """
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr("thymira.thy.nodes.inspect.register_dataset", _raise)

    state = ThyState.model_validate(
        inspect_node(_project(tmp_path), artifact_store=store)(ThyState(run=run))
    )

    assert state.error == "dataset 'applications' could not be registered: disk gone"


def test_inspect_keeps_the_datasets_it_registered_before_one_fails(tmp_path: Path) -> None:
    run = _run()
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    project_dir = tmp_path / "project"
    (project_dir / ".thymira").mkdir(parents=True)
    (project_dir / ".thymira" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "domain": "credit_risk"},
                "datasets": [
                    {"name": "first", "path": "data/first.csv"},
                    {"name": "second", "path": "data/second.csv"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (project_dir / "data").mkdir()
    (project_dir / "data" / "first.csv").write_text(_TINY_CSV, encoding="utf-8", newline="\n")

    state = ThyState.model_validate(
        inspect_node(project_dir, artifact_store=store)(ThyState(run=run))
    )

    assert [d.name for d in state.datasets] == ["first"]
    assert state.error is not None
    assert "second" in state.error
    assert "is missing" in state.error


def test_inspect_without_an_artifact_store_registers_nothing_and_still_seeds_the_state(
    tmp_path: Path,
) -> None:
    state = ThyState.model_validate(inspect_node(_project(tmp_path))(ThyState(run=_run())))

    assert state.error is None
    assert state.datasets == ()
    assert state.domain == "credit_risk"


def test_the_graph_ends_after_a_failed_intake_without_planning(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    graph = build_thy_graph(
        AgentCatalog(), log, project_dir=_project(tmp_path, csv_text=None), artifact_store=store
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run)))

    assert final_state.error is not None
    assert "is missing" in final_state.error
    assert final_state.plan == ()
    assert final_state.phase is ThyPhase.INSPECT
