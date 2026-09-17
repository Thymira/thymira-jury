"""Integration tests for the Run planning endpoint.

The FastAPI application, policy Gate and local event persistence are real. The dispatcher is a
small recording fake because execution is outside this endpoint's boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import build_default_deps, create_app
from thymira.schemas import EventType, Id, TodoItem

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Keep the Run creation boundary active without executing it."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Keep the dispatcher protocol complete for the API composition root."""
        del run_id


def _client(tmp_path: Path) -> TestClient:
    """Build an API client backed by one temporary configured project."""
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


def test_plan_returns_ordered_phases_and_records_approval_request(tmp_path: Path) -> None:
    """Planning exposes the core phase order and the Gate evidence for sign-off."""
    client = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "Plan the analysis"}).json()

    response = client.post(f"/runs/{run['id']}/plan")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run["id"],
        "phases": ["understanding", "preparation", "modeling", "evaluation", "reporting"],
    }
    app = cast("FastAPI", client.app)
    events = app.state.runtime_deps.event_store.read(run["id"])
    assert [event.type for event in events[-2:]] == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
    ]


def test_plan_rejects_unknown_run(tmp_path: Path) -> None:
    """Planning an unknown Run returns the same scoped problem as Run inspection."""
    client = _client(tmp_path)

    response = client.post("/runs/run_0123456789abcdef0123456789abcdef/plan")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_plan_rejects_malformed_run_id(tmp_path: Path) -> None:
    """Planning a non-Run identifier returns the stable validation problem."""
    client = _client(tmp_path)

    response = client.post("/runs/project_0123456789abcdef0123456789abcdef/plan")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_run_id"


def test_get_plan_returns_verified_durable_board_and_artifact(tmp_path: Path) -> None:
    """GET /plan reads the latest board and verifies its content-addressed artifact."""
    client = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "Plan the analysis"}).json()
    app = cast("FastAPI", client.app)
    app.state.runtime_deps.goal_board.publish_plan(
        run["id"],
        [TodoItem(ordinal=0, text="profile the data")],
        artifact_content="profile the data",
    )

    response = client.get(f"/runs/{run['id']}/plan")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run["id"]
    assert body["revision"] == 1
    assert body["items"][0]["text"] == "profile the data"
    assert body["artifact"]["advisory"] is True
    assert body["artifact"]["content"] == "profile the data"


def test_get_plan_returns_404_before_any_durable_plan_is_published(tmp_path: Path) -> None:
    """A Run without a published board has a stable read-only 404."""
    client = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "No plan yet"}).json()

    response = client.get(f"/runs/{run['id']}/plan")

    assert response.status_code == 404
    assert response.json()["code"] == "plan_not_found"
