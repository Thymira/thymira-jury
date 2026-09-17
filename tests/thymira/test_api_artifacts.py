"""Integration tests for the Run artifact inventory and verified artifact content routes.

Real: the FastAPI application, the local runtime composition and each Run's ``LocalArtifactStore``
under ``tmp_path``. Faked: the execution dispatcher, so nothing here invokes a model or a graph.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import (
    ArtifactContentResponse,
    ArtifactListResponse,
    build_default_deps,
    create_app,
)
from thymira.api.routes import artifacts as artifact_routes
from thymira.schemas import Id, new_id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from thymira.state import ArtifactStore

pytestmark = pytest.mark.integration

# Standard base64 that the export redactor recognises as an AWS access key id. Decoded, the bytes
# begin with NUL, so the content route treats them as binary and must withhold them rather than
# serve a redacted, corrupted body.
_REDACTABLE_BASE64 = "AKIAABCDEFGHIJKLMNOP"


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


def _new_run(client: TestClient) -> tuple[Id, ArtifactStore]:
    """Create one Run and open the artifact store the API reads it from."""
    run_id = client.post("/runs", json={"prompt": "Profile the data"}).json()["id"]
    app = cast("FastAPI", client.app)
    return run_id, app.state.runtime_deps.artifact_store_factory(run_id)


def test_list_artifacts_returns_every_manifest_entry_oldest_first(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    first = store.save_text("reports/eda.md", "# EDA", produced_by=run_id)
    second = store.save_json("metrics.json", {"rows": 998}, produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts")

    assert response.status_code == 200
    body = ArtifactListResponse.model_validate(response.json())
    assert body.run_id == run_id
    assert [artifact.id for artifact in body.items] == [first.id, second.id]


def test_list_artifacts_keeps_a_superseded_revision_as_an_invalid_entry(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    store.save_text("reports/eda.md", "draft", produced_by=run_id)
    current = store.save_text("reports/eda.md", "final", produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts")

    items = ArtifactListResponse.model_validate(response.json()).items
    assert len(items) == 2
    assert [artifact.id for artifact in items if artifact.valid] == [current.id]


def test_get_artifact_returns_digest_verified_text_content(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    artifact = store.save_text("reports/eda.md", "# EDA\n\n998 rows.", produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    assert response.status_code == 200
    body = ArtifactContentResponse.model_validate(response.json())
    assert body.encoding == "utf-8"
    assert body.content == "# EDA\n\n998 rows."
    assert body.redacted is False
    assert body.projection == "redacted"
    assert hashlib.sha256(body.content.encode("utf-8")).hexdigest() == body.artifact.sha256


def test_get_artifact_redacts_a_credential_inside_text_content(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    source = "curl -H 'Authorization: Bearer abcdefghijklmnop' http://api"
    artifact = store.save_text("logs/request.txt", source, produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    body = ArtifactContentResponse.model_validate(response.json())
    assert body.redacted is True
    assert body.content is not None
    assert "abcdefghijklmnop" not in body.content
    assert body.artifact.sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_get_artifact_returns_binary_content_as_standard_base64(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    artifact = store.save_bytes(
        "figures/plot.png", data, produced_by=run_id, media_type="image/png"
    )

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    body = ArtifactContentResponse.model_validate(response.json())
    assert body.encoding == "base64"
    assert body.content is not None
    assert base64.b64decode(body.content) == data
    assert body.withheld_reason is None


def test_get_artifact_withholds_binary_content_that_redaction_would_alter(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    data = base64.b64decode(_REDACTABLE_BASE64)
    artifact = store.save_bytes("models/weights.bin", data, produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    assert response.status_code == 200
    body = ArtifactContentResponse.model_validate(response.json())
    assert body.encoding == "base64"
    assert body.content is None
    assert body.withheld_reason is not None


def test_get_artifact_refuses_bytes_that_no_longer_match_the_manifest(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    artifact = store.save_text("reports/eda.md", "original", produced_by=run_id)
    blob = tmp_path / "runtime" / "artifacts" / run_id / artifact.uri
    blob.write_bytes(b"tampered")

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    assert response.status_code == 409
    assert response.json()["code"] == "artifact_tampered"


def test_get_artifact_refuses_a_superseded_revision(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    draft = store.save_text("reports/eda.md", "draft", produced_by=run_id)
    store.save_text("reports/eda.md", "final", produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts/{draft.id}")

    assert response.status_code == 409
    assert response.json()["code"] == "artifact_unavailable"


def test_get_artifact_refuses_content_above_the_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifact_routes, "MAX_ARTIFACT_CONTENT_BYTES", 4)
    client = _client(tmp_path)
    run_id, store = _new_run(client)
    artifact = store.save_text("reports/eda.md", "longer than four bytes", produced_by=run_id)

    response = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    assert response.status_code == 413
    assert response.json()["code"] == "artifact_too_large"


def test_get_artifact_returns_404_for_an_unrecorded_artifact(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, _store = _new_run(client)

    response = client.get(f"/runs/{run_id}/artifacts/{new_id('artifact')}")

    assert response.status_code == 404
    assert response.json()["code"] == "artifact_not_found"


def test_get_artifact_rejects_an_identifier_that_is_not_an_artifact(tmp_path: Path) -> None:
    client = _client(tmp_path)
    run_id, _store = _new_run(client)

    response = client.get(f"/runs/{run_id}/artifacts/{new_id('run')}")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_artifact_id"
