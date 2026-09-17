"""Integration tests for RA-API-07: the delegate endpoint and the Tool API read surface.

Real: the FastAPI application (``create_app``) and the local runtime dependency graph
(``build_default_deps``) over ``tmp_path``. Faked: the execution dispatcher, so nothing here
invokes a model or graph. These tests pin the two additive surfaces RA-API-07 completes -- the
Agent-API ``delegate()`` method and the read side of the Tool API (baseline sections 6 and 7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import build_default_deps, create_app
from thymira.api.schemas import ToolDescriptor, ToolListResponse
from thymira.schemas import Id, Task, TaskStatus, new_id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one create dispatch."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Record one resume dispatch."""
        del run_id


def _client(tmp_path: Path) -> TestClient:
    """Build an API client backed by one temporary configured project workspace."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=TEST_CREDENTIAL,
    )
    return authenticated_client(create_app(deps))


def _create_run(client: TestClient) -> Id:
    """Create one Run and return its id."""
    return client.post("/runs", json={"prompt": "Analyze the dataset"}).json()["id"]


def test_delegate_creates_a_pending_task_linked_to_the_run(tmp_path: Path) -> None:
    """POST /delegate creates a Task in PENDING linked to the run."""
    client = _client(tmp_path)
    run_id = _create_run(client)

    response = client.post(
        f"/runs/{run_id}/delegate",
        json={"agent": "data-agent", "objective": "Profile the dataset"},
    )

    assert response.status_code == 201
    task = Task.model_validate(response.json())
    assert task.status is TaskStatus.PENDING
    assert task.run_id == run_id
    assert task.objective == "Profile the dataset"


def test_delegated_task_is_persisted_against_the_run(tmp_path: Path) -> None:
    """The delegated Task and its named sub-agent are stored under the run's records."""
    client = _client(tmp_path)
    run_id = _create_run(client)

    task = Task.model_validate(
        client.post(
            f"/runs/{run_id}/delegate",
            json={"agent": "coding-agent", "objective": "Fit a model"},
        ).json()
    )

    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    stored_tasks = deps.record_repository.list_for_run(run_id, "task")
    stored_agents = deps.record_repository.list_for_run(run_id, "agent")
    assert [record.id for record in stored_tasks] == [task.id]
    assert task.agent_id in {record.id for record in stored_agents}
    delegated_agent = next(record for record in stored_agents if record.id == task.agent_id)
    assert delegated_agent.name == "coding-agent"


def test_delegate_rejects_an_unknown_or_malformed_run(tmp_path: Path) -> None:
    """Delegate shares the run-scope contract: 404 for an unknown run, 422 for a non-run id."""
    client = _client(tmp_path)
    body = {"agent": "data-agent", "objective": "Profile the dataset"}

    unknown = client.post(f"/runs/{new_id('run')}/delegate", json=body)
    malformed = client.post(f"/runs/{new_id('project')}/delegate", json=body)

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "run_not_found"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_run_id"


def test_delegate_rejects_an_empty_agent_or_objective(tmp_path: Path) -> None:
    """A delegate body must name a sub-agent and an objective."""
    client = _client(tmp_path)
    run_id = _create_run(client)

    response = client.post(f"/runs/{run_id}/delegate", json={"agent": "", "objective": ""})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_get_tools_lists_the_registered_capabilities_with_risk_metadata(tmp_path: Path) -> None:
    """GET /tools lists the registered tool capabilities with their risk metadata."""
    client = _client(tmp_path)

    response = client.get("/tools")

    assert response.status_code == 200
    listing = ToolListResponse.model_validate(response.json())
    by_name = {tool.name: tool for tool in listing.items}
    # The Tool API surfaces the registry: names, descriptions and the risk metadata each tool
    # declares on its ToolCapability.
    assert "run_python" in by_name
    assert "read_file" in by_name
    run_python = by_name["run_python"]
    assert run_python.capability.risk_tags == ("code_execution",)
    assert run_python.capability.side_effects == ("workspace_write",)
    # A read-only tool declares no side effect, and the read surface reflects that faithfully.
    assert by_name["read_file"].capability.side_effects == ()
    assert by_name["read_file"].description


def test_get_tools_can_filter_by_a_declared_risk_tag(tmp_path: Path) -> None:
    """The Tool API narrows the listing to tools carrying a given risk tag."""
    client = _client(tmp_path)

    tagged = ToolListResponse.model_validate(
        client.get("/tools", params={"risk_tag": "code_execution"}).json()
    )

    names = {tool.name for tool in tagged.items}
    assert "run_python" in names
    assert "read_file" not in names
    assert all("code_execution" in tool.capability.risk_tags for tool in tagged.items)


def test_get_one_tool_returns_its_descriptor_and_input_schema(tmp_path: Path) -> None:
    """GET /tools/{name} returns one tool's descriptor, including its input JSON schema."""
    client = _client(tmp_path)

    response = client.get("/tools/read_file")

    assert response.status_code == 200
    descriptor = ToolDescriptor.model_validate(response.json())
    assert descriptor.name == "read_file"
    assert descriptor.capability.id == "read_file"
    assert descriptor.input_schema["properties"]["path"]


def test_get_unknown_tool_is_a_structured_404(tmp_path: Path) -> None:
    """An unregistered tool name is a stable problem+json 404."""
    client = _client(tmp_path)

    response = client.get("/tools/not_a_tool")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "tool_not_found"
