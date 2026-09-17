"""FastAPI composition-root integration tests for RA-API-01.

Real inside the boundary: the FastAPI app and local runtime dependency graph. No model provider,
external HTTP service, database or queue is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import RuntimeDeps, build_default_deps, create_app
from thymira.api import app as app_module
from thymira.api import deps as deps_module
from thymira.core import InlineDispatcher
from thymira.mira.kb import LocalRegulationStore
from thymira.mira.tools import SearchRegulation
from thymira.schemas import Decision, new_id
from thymira.thy.context import load_project_context

pytestmark = pytest.mark.integration

_CREDIT_RISK_WORKSPACE = Path(__file__).resolve().parents[2] / "examples" / "credit-risk"


def test_create_app_health_check_uses_injected_dependencies(tmp_path: Path) -> None:
    """The app factory stores explicit local dependencies and serves healthz."""
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)
    app = create_app(deps)

    response = authenticated_client(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app.state.runtime_deps is deps
    assert isinstance(deps, RuntimeDeps)
    assert isinstance(deps.dispatcher, InlineDispatcher)


def test_default_api_dependencies_include_the_local_mira_registry(tmp_path: Path) -> None:
    """The API composition supplies MIRA's explicit search registry without PostgreSQL."""
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)

    tools = tuple(deps.mira_tool_registry)
    assert [tool.name for tool in tools] == ["search_regulation"]
    assert isinstance(tools[0], SearchRegulation)
    assert isinstance(tools[0].store, LocalRegulationStore)


def test_create_app_registers_future_route_groups(tmp_path: Path) -> None:
    """The app exposes the stable route paths owned by later API tasks."""
    app = create_app(build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL))
    paths = set(app.openapi()["paths"])

    assert "/runs" in paths
    assert "/runs/{run_id}" in paths
    assert "/runs/{run_id}/events" in paths
    assert "/runs/{run_id}/audit" in paths
    assert "/runs/{run_id}/experiments" in paths


def test_gate_without_a_workspace_uses_the_bare_base_policy(tmp_path: Path) -> None:
    """No workspace means no project to resolve, so the Gate falls back to the packaged base."""
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)
    run_id = new_id("run")

    decision = deps.gate_factory(run_id).engine.decide_action(
        run_id=run_id, subject_kind="run", subject_id=run_id, action_type="plan.proposed"
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "default"


def test_gate_with_a_workspace_layers_the_projects_own_policy(tmp_path: Path) -> None:
    """A configured workspace's ``.thymira/policies.yaml`` overlay reaches the real Gate.

    Before this, ``build_default_deps`` always built the Gate's ``PolicyEngine`` from the bare
    packaged base policy, so a project's own action/finding rules -- loaded and unit tested
    against ``load_policy`` -- never actually reached a live Run's Gate calls. The credit-risk
    example's CRX-001 overlay rule (this same regression's fix) proves the wiring: without it,
    THY's own ``plan.proposed`` halts every real run before it touches the dataset.
    """
    deps = build_default_deps(
        tmp_path, workspace=_CREDIT_RISK_WORKSPACE, principal_resolver=TEST_CREDENTIAL
    )
    run_id = new_id("run")

    decision = deps.gate_factory(run_id).engine.decide_action(
        run_id=run_id, subject_kind="run", subject_id=run_id, action_type="plan.proposed"
    )

    assert decision.decision is Decision.PASS
    assert decision.rule_id == "CRX-001"


def test_thy_is_composed_over_the_project_root_not_the_thymira_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed graph gets the directory that *contains* ``.thymira``, not ``.thymira``.

    ``ProjectResolution.workspace`` is ``config.yaml``'s own directory, while Inspect
    (``load_project_context``) and every ``ToolContext`` append ``.thymira`` themselves. Composing
    on ``workspace`` therefore pointed Inspect at ``.thymira/.thymira`` -- so it silently loaded no
    domain, no ``context.md`` and no frameworks -- and sandboxed every delegated agent's file tool
    inside the config directory, where ``read_file("data/...")`` and ``run_python`` cannot reach
    the project's own dataset at all.
    """
    captured: dict[str, object] = {}

    def _never_built(run: object) -> object:
        raise AssertionError("this wiring test never builds the composed graph")

    def capturing_factory(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return _never_built

    monkeypatch.setattr(deps_module, "build_runtime_graph_factory", capturing_factory)
    build_default_deps(
        tmp_path, workspace=_CREDIT_RISK_WORKSPACE, principal_resolver=TEST_CREDENTIAL
    )

    project_dir = captured["project_dir"]
    assert isinstance(project_dir, Path)
    assert project_dir == _CREDIT_RISK_WORKSPACE.resolve()
    context = load_project_context(project_dir)
    assert context.config is not None
    assert context.context_md != ""


def test_stopping_the_server_drains_langfuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK batches, so whatever is still queued when the process stops is lost.

    That tail is the part a reviewer most wants, because a Run that failed failed at its end. The
    SDK's own `atexit` handler runs only on the orderly path, and a container that does not stop
    in time is killed; the lifespan is reached either way. `TestClient` runs it only as a context
    manager, which is why every other test in this file is unaffected.
    """
    drained: list[str] = []
    monkeypatch.setattr(app_module, "shutdown_tracing", lambda: drained.append("drained"))

    with authenticated_client(
        create_app(build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL))
    ) as client:
        assert client.get("/healthz").status_code == 200
        assert drained == []

    assert drained == ["drained"]
