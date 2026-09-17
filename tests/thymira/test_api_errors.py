"""Integration tests for API errors, idempotency and actor resolution.

The FastAPI application, local Run store and event log are real. The dispatcher is a recording
fake because these tests exercise the HTTP boundary, not graph execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import DEFAULT_ACTOR_ID, TEST_CREDENTIAL, authenticated_client
from thymira.api import build_default_deps, create_app
from thymira.schemas import ActorKind, EventType, Id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def __init__(self) -> None:
        self.submitted: list[Id] = []

    def submit(self, run_id: Id) -> None:
        """Record a new Run submission."""
        self.submitted.append(run_id)

    def resume(self, run_id: Id) -> None:
        """Implement the dispatcher protocol for the API composition root."""
        del run_id


def _client(tmp_path: Path) -> tuple[TestClient, _RecordingDispatcher]:
    """Build an API client with real local persistence and a recording dispatcher."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    dispatcher = _RecordingDispatcher()
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=dispatcher,
        principal_resolver=TEST_CREDENTIAL,
    )
    return authenticated_client(create_app(deps)), dispatcher


def test_repeating_idempotent_run_creation_returns_the_same_run(tmp_path: Path) -> None:
    """A retried create does not create or dispatch a second Run."""
    client, dispatcher = _client(tmp_path)
    headers = {"Idempotency-Key": "create-analysis-1"}

    first = client.post("/runs", json={"prompt": "Analyze"}, headers=headers)
    second = client.post("/runs", json={"prompt": "Analyze"}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert dispatcher.submitted == [first.json()["id"]]


def test_empty_idempotency_key_returns_problem_json(tmp_path: Path) -> None:
    """An empty idempotency key is rejected as invalid input."""
    client, _ = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"prompt": "Analyze"},
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "invalid_idempotency_key"


def test_run_creation_records_the_authenticated_principal(tmp_path: Path) -> None:
    """The Run event carries the authenticated principal, marked verified.

    An unverified ``X-Thymira-Actor`` header used to name who was acting; nothing checked it, so
    it was recorded ``authenticated=False``. It names nobody now -- the credential does -- and the
    event records a verified identity with its real role.
    """
    client, _ = _client(tmp_path)
    response = client.post(
        "/runs",
        json={"prompt": "Analyze"},
        headers={"X-Thymira-Actor": "somebody-else"},
    )
    run_id = response.json()["id"]
    app = cast("FastAPI", client.app)
    event = app.state.runtime_deps.event_store.read(run_id)[0]

    assert response.status_code == 201
    assert event.type is EventType.RUN_STARTED
    assert event.actor.kind is ActorKind.HUMAN
    assert event.actor.id == DEFAULT_ACTOR_ID
    assert event.actor.id != "somebody-else"
    assert event.actor.authenticated is True


def test_run_creation_never_records_the_system_actor_for_a_human_request(tmp_path: Path) -> None:
    """A request with no actor field is still a person's request, and is recorded as one."""
    client, _ = _client(tmp_path)
    response = client.post("/runs", json={"prompt": "Analyze"})
    app = cast("FastAPI", client.app)
    event = app.state.runtime_deps.event_store.read(response.json()["id"])[0]

    assert response.status_code == 201
    assert event.actor != app.state.runtime_deps.actor_factory()
    assert event.actor.authenticated is True


def test_forbidden_approval_returns_problem_json_conflict(tmp_path: Path) -> None:
    """An approval request for a Run without pending review is a structured conflict."""
    client, _ = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "Analyze"}).json()

    response = client.post(f"/runs/{run['id']}/approve", json={"actor": DEFAULT_ACTOR_ID})

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "approval_not_pending"
