"""Integration tests for RA-API-10: bearer-token authentication and route permissions.

Real: the FastAPI application (``create_app``) and the local runtime dependency graph
(``build_default_deps``) over ``tmp_path``, built with a configured ``BearerTokenRegistry`` so
authorization is enforced. Faked: the execution dispatcher, so nothing here invokes a model or
graph. These tests pin the RA-API-10 seam -- a real principal behind ``resolve_actor`` -- and the
least-privilege permission separation (baseline sections 19, 22): an unauthenticated write is 401,
a read-only principal writing is 403, and a valid principal's work carries the authenticated actor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from thymira.api import (
    BearerTokenRegistry,
    build_default_deps,
    create_app,
    principal_for_role,
)
from thymira.schemas import ActorKind, EventType, Id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from thymira.api.permissions import PrincipalResolver

pytestmark = pytest.mark.integration

_SCIENTIST_TOKEN = "token-scientist"  # noqa: S105 - test-only bearer token, not a secret.
_OFFICER_TOKEN = "token-officer"  # noqa: S105 - test-only bearer token, not a secret.
_VIEWER_TOKEN = "token-viewer"  # noqa: S105 - test-only bearer token, not a secret.


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one create dispatch."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Record one resume dispatch."""
        del run_id


def _auth_client(tmp_path: Path) -> TestClient:
    """Build an API client whose composition root enforces bearer-token authorization."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    registry = BearerTokenRegistry(
        {
            _SCIENTIST_TOKEN: principal_for_role("mario", "data-scientist"),
            _OFFICER_TOKEN: principal_for_role("val", "risk-officer"),
            _VIEWER_TOKEN: principal_for_role("obs", "viewer"),
        }
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=registry,
    )
    return TestClient(create_app(deps))


def _bearer(token: str) -> dict[str, str]:
    """Build the Authorization header carrying a bearer token."""
    return {"Authorization": f"Bearer {token}"}


def test_the_api_composition_refuses_an_optional_resolver_escape(tmp_path: Path) -> None:
    """A caller cannot opt back into anonymous API behavior by passing ``None`` explicitly."""
    with pytest.raises(TypeError, match="principal_resolver is required"):
        build_default_deps(
            tmp_path / "runtime",
            principal_resolver=cast("PrincipalResolver", None),
        )


def test_unauthenticated_write_is_rejected_with_401(tmp_path: Path) -> None:
    """An unauthenticated write returns 401 with a bearer challenge and a stable error code."""
    client = _auth_client(tmp_path)

    response = client.post("/runs", json={"prompt": "Analyze"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthenticated"


def test_read_only_principal_cannot_create_a_run(tmp_path: Path) -> None:
    """A read-only principal POSTing /runs returns 403."""
    client = _auth_client(tmp_path)

    response = client.post("/runs", json={"prompt": "Analyze"}, headers=_bearer(_VIEWER_TOKEN))

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "forbidden"


def test_valid_principal_creates_a_run_and_events_carry_the_authenticated_actor(
    tmp_path: Path,
) -> None:
    """A valid principal succeeds and the emitted events carry the authenticated actor."""
    client = _auth_client(tmp_path)

    response = client.post("/runs", json={"prompt": "Analyze"}, headers=_bearer(_SCIENTIST_TOKEN))

    assert response.status_code == 201
    run_id = response.json()["id"]
    app = cast("FastAPI", client.app)
    started = app.state.runtime_deps.event_store.read(run_id)[0]
    assert started.type is EventType.RUN_STARTED
    assert started.actor.id == "mario"
    assert started.actor.role == "data-scientist"
    assert started.actor.authenticated is True
    assert started.actor.kind is ActorKind.HUMAN


def test_an_unrecognized_bearer_token_is_rejected_with_401(tmp_path: Path) -> None:
    """A well-formed but unknown bearer token is a 401, distinct from a missing token."""
    client = _auth_client(tmp_path)

    response = client.post("/runs", json={"prompt": "Analyze"}, headers=_bearer("not-a-token"))

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_a_non_bearer_authorization_scheme_is_rejected_with_401(tmp_path: Path) -> None:
    """Only the bearer scheme authenticates; a basic-auth header is unauthenticated."""
    client = _auth_client(tmp_path)

    response = client.post(
        "/runs",
        json={"prompt": "Analyze"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_read_only_principal_may_read(tmp_path: Path) -> None:
    """Least privilege grants a viewer read access even though it cannot write."""
    client = _auth_client(tmp_path)

    response = client.get("/runs", headers=_bearer(_VIEWER_TOKEN))

    assert response.status_code == 200


def test_write_principal_may_not_approve(tmp_path: Path) -> None:
    """Permission separation: a data scientist writes runs but cannot answer a human review."""
    client = _auth_client(tmp_path)
    run_id = client.post(
        "/runs", json={"prompt": "Analyze"}, headers=_bearer(_SCIENTIST_TOKEN)
    ).json()["id"]

    response = client.post(
        f"/runs/{run_id}/approve",
        json={},
        headers=_bearer(_SCIENTIST_TOKEN),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_reads_also_require_authentication(tmp_path: Path) -> None:
    """With auth configured, an unauthenticated read is a 401 like an unauthenticated write."""
    client = _auth_client(tmp_path)

    response = client.get("/runs")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
