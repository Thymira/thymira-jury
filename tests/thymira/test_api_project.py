"""Integration tests for the project input routes: the context document and declared datasets.

Real: the FastAPI application, the local runtime composition and a project workspace under
``tmp_path``. Faked: the execution dispatcher, so nothing here invokes a model or a graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import (
    ProjectContextResponse,
    ProjectDatasetListResponse,
    ProjectDatasetUploadResponse,
    build_default_deps,
    create_app,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient

    from thymira.schemas import Id

pytestmark = pytest.mark.integration

CONFIG = (
    "project:\n"
    "  name: credit-risk\n"
    "  domain: credit_risk\n"
    "\n"
    "datasets:\n"
    "  - name: german_credit\n"
    "    path: data/applications.csv\n"
    "    target: is_high_risk\n"
)
APPLICATIONS = "age,income,is_high_risk\n30,1000,0\n45,2000,1\n"
OCTET_STREAM = {"Content-Type": "application/octet-stream"}


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Keep the Run creation boundary active without executing it."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Keep the dispatcher protocol complete for the API composition root."""
        del run_id


def _client(tmp_path: Path, *, configured: bool = True) -> TestClient:
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(CONFIG, encoding="utf-8", newline="\n")
    (workspace / "data").mkdir()
    (workspace / "data" / "applications.csv").write_text(
        APPLICATIONS, encoding="utf-8", newline="\n"
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace if configured else None,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=TEST_CREDENTIAL,
    )
    return authenticated_client(create_app(deps))


def test_the_context_round_trip_saves_with_the_digest_it_read(tmp_path: Path) -> None:
    client = _client(tmp_path)
    before = ProjectContextResponse.model_validate(client.get("/project/context").json())

    response = client.put(
        "/project/context",
        json={"text": "Goal: score applicants.\n", "expected_sha256": before.sha256},
    )

    assert response.status_code == 200
    assert (before.exists, ProjectContextResponse.model_validate(response.json()).exists) == (
        False,
        True,
    )
    assert client.get("/project/context").json()["text"] == "Goal: score applicants.\n"


def test_saving_the_context_with_a_stale_digest_is_a_conflict(tmp_path: Path) -> None:
    client = _client(tmp_path)
    stale = client.get("/project/context").json()["sha256"]
    client.put("/project/context", json={"text": "first\n", "expected_sha256": stale})

    response = client.put("/project/context", json={"text": "second\n", "expected_sha256": stale})

    assert response.status_code == 409
    assert response.json()["code"] == "context_conflict"


def test_a_context_with_personal_data_is_served_redacted_and_flagged(tmp_path: Path) -> None:
    client = _client(tmp_path)
    digest = client.get("/project/context").json()["sha256"]
    client.put(
        "/project/context", json={"text": "Owner: ana@example.com\n", "expected_sha256": digest}
    )

    body = client.get("/project/context").json()

    assert body["redacted"] is True
    assert "ana@example.com" not in body["text"]


def test_saving_a_context_that_carries_a_redaction_mask_is_refused(tmp_path: Path) -> None:
    client = _client(tmp_path)
    digest = client.get("/project/context").json()["sha256"]

    response = client.put(
        "/project/context", json={"text": "Owner: [REDACTED:EMAIL]\n", "expected_sha256": digest}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_context"


def test_uploading_a_csv_declares_it_for_the_next_run(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/project/datasets/loans",
        params={"filename": "loans.csv", "target": "defaulted"},
        content=b"amount,defaulted\n500,0\n900,1\n",
        headers=OCTET_STREAM,
    )

    assert response.status_code == 200
    upload = ProjectDatasetUploadResponse.model_validate(response.json())
    assert (upload.rows, upload.columns) == (2, ("amount", "defaulted"))
    assert upload.dataset.path == "data/loans.csv"
    listed = ProjectDatasetListResponse.model_validate(client.get("/project/datasets").json())
    assert [(item.name, item.present) for item in listed.items] == [
        ("german_credit", True),
        ("loans", True),
    ]


def test_uploading_a_file_that_is_not_csv_or_parquet_is_refused(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/project/datasets/notes",
        params={"filename": "notes.txt"},
        content=b"hello",
        headers=OCTET_STREAM,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_dataset"


def test_setting_a_target_that_is_not_a_column_is_refused(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.patch("/project/datasets/german_credit", json={"target": "salary"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_dataset"


def test_removing_a_dataset_undeclares_it(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.delete("/project/datasets/german_credit")

    assert response.status_code == 200
    assert ProjectDatasetListResponse.model_validate(response.json()).items == ()


def test_removing_a_dataset_the_project_does_not_declare_is_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.delete("/project/datasets/loans")

    assert response.status_code == 404
    assert response.json()["code"] == "dataset_not_found"


def test_project_routes_need_a_configured_project(tmp_path: Path) -> None:
    client = _client(tmp_path, configured=False)

    response = client.get("/project/datasets")

    assert response.status_code == 503
    assert response.json()["code"] == "project_not_configured"
