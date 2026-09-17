"""API integration evidence for the runtime-owned durable settings consumer.

Real inside the boundary: FastAPI and the local settings store. No external service or model is
used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.agents.llm.routing import MODEL_OVERRIDES_NAMESPACE, ModelTier, Role, model_for
from thymira.api import (
    BearerTokenRegistry,
    build_default_deps,
    create_app,
    principal_for_role,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_WRITER_TOKEN = "settings-writer"  # noqa: S105 - test-only bearer token, not a secret.
_VIEWER_TOKEN = "settings-viewer"  # noqa: S105 - test-only bearer token, not a secret.


def _client(tmp_path: Path) -> TestClient:
    """Build one authenticated admin API client over an isolated local settings root.

    F13.3 forbids an anonymous API composition (``build_default_deps`` now requires a
    ``principal_resolver``), so this durable-storage evidence authenticates like every other API
    test instead of relying on the permissive default the original slice was written against.
    """
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)
    return authenticated_client(create_app(deps))


def _configured_client(tmp_path: Path) -> TestClient:
    """Build a settings API client with the real bearer resolver configured."""
    resolver = BearerTokenRegistry(
        {
            _WRITER_TOKEN: principal_for_role("settings-writer", "data-scientist"),
            _VIEWER_TOKEN: principal_for_role("settings-viewer", "viewer"),
        }
    )
    return TestClient(create_app(build_default_deps(tmp_path, principal_resolver=resolver)))


def _bearer(token: str) -> dict[str, str]:
    """Build an Authorization header carrying one test bearer token."""
    return {"Authorization": f"Bearer {token}"}


def test_settings_api_publishes_and_reads_a_cas_snapshot(tmp_path: Path) -> None:
    """A settings mutation is durable through the API composition root and readable after reopen."""
    with _client(tmp_path) as client:
        response = client.put(
            "/settings/runtime",
            json={
                "values": {"mode": "safe"},
                "expected_revision": 0,
                "secret_refs": {"api_key": {"source": "env", "name": "THYMIRA_API_KEY"}},
            },
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 1

        fetched = client.get("/settings/runtime")
        assert fetched.status_code == 200
        assert fetched.json()["values"] == {"mode": "safe"}
        assert fetched.json()["secret_refs"]["api_key"]["name"] == "THYMIRA_API_KEY"

    with _client(tmp_path) as client:
        assert client.get("/settings/runtime").json()["revision"] == 1


def test_settings_api_honors_configured_principal_permissions(tmp_path: Path) -> None:
    """The settings route enforces the configured resolver's read/write separation."""
    with _configured_client(tmp_path) as client:
        assert client.get("/settings").status_code == 401
        viewer = client.put(
            "/settings/runtime",
            json={"values": {"mode": "safe"}},
            headers=_bearer(_VIEWER_TOKEN),
        )
        assert viewer.status_code == 403
        writer = client.put(
            "/settings/runtime",
            json={"values": {"mode": "safe"}},
            headers=_bearer(_WRITER_TOKEN),
        )
        assert writer.status_code == 200


def test_settings_api_reports_a_stale_cas_without_overwriting_current_snapshot(
    tmp_path: Path,
) -> None:
    """A stale API writer receives the current safe snapshot and leaves it unchanged."""
    with _client(tmp_path) as client:
        assert client.put("/settings/runtime", json={"values": {"mode": "safe"}}).status_code == 200
        conflict = client.put(
            "/settings/runtime",
            json={"values": {"mode": "unsafe"}, "expected_revision": 0},
        )

        assert conflict.status_code == 409
        assert conflict.json()["code"] == "settings_conflict"
        assert conflict.json()["details"]["current"]["values"] == {"mode": "safe"}
        assert client.get("/settings/runtime").json()["values"] == {"mode": "safe"}


@pytest.mark.parametrize(
    "values",
    [
        {"provider": {"api_key": "raw-secret"}},
        {"credentials": [{"token": "raw-secret"}]},
    ],
)
def test_settings_api_rejects_raw_nested_credentials(
    tmp_path: Path, values: dict[str, object]
) -> None:
    """The API cannot persist credential-shaped nested values in the settings document."""
    with _client(tmp_path) as client:
        response = client.put("/settings/runtime", json={"values": values})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_settings"
    assert not (tmp_path / "settings" / "settings.json").exists()


def test_settings_api_put_to_models_namespace_takes_effect_on_the_next_routed_call(
    tmp_path: Path,
) -> None:
    """A model-choice override written through the settings API needs no restart to take hold.

    ``routing.model_for`` is called directly (not through a real provider) because this repo's
    tests never make a network call; the composition-root wiring under test is exactly the part
    between "the PUT succeeded" and "routing's own cache changed," which does not require one.
    """
    with _client(tmp_path) as client:
        assert model_for(Role.AGENT, ModelTier.FAST) is None

        response = client.put(
            f"/settings/{MODEL_OVERRIDES_NAMESPACE}",
            json={"values": {"THYMIRA_MODEL_FAST": "override/live-fast"}},
        )

        assert response.status_code == 200
        assert model_for(Role.AGENT, ModelTier.FAST) == "override/live-fast"


def test_settings_api_startup_reloads_a_persisted_model_override(tmp_path: Path) -> None:
    """A model override a prior process persisted survives this process's own restart."""
    with _client(tmp_path) as client:
        client.put(
            f"/settings/{MODEL_OVERRIDES_NAMESPACE}",
            json={"values": {"THYMIRA_MODEL_FAST": "override/persisted-fast"}},
        )

    # A fresh build_default_deps call over the same root simulates the API process restarting;
    # nothing here re-sends the PUT.
    with _client(tmp_path):
        assert model_for(Role.AGENT, ModelTier.FAST) == "override/persisted-fast"
