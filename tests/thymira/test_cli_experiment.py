"""Acceptance tests for the CLI ``experiment`` command.

Real: Typer command dispatch and the HTTP client. Faked: the remote API transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"


def _experiment_payload(**updates: object) -> dict[str, object]:
    """Build a valid API Experiment entry."""
    payload: dict[str, object] = {
        "id": "experiment_" + "3" * 31 + "c",
        "run_id": RUN_ID,
        "name": "baseline-logreg",
        "status": "COMPLETED",
        "parameters": {"C": 1.0, "solver": "lbfgs"},
        "metrics": {"accuracy": 0.87, "auc": 0.91},
        "seed": 42,
        "dataset_artifact_id": None,
        "model_artifact_id": "artifact_" + "4" * 31 + "d",
        "artifact_ids": [],
        "tracker": "mlflow",
        "tracker_run_id": "mlflow_run_abc123",
        "started_at": "2026-08-22T10:14:05Z",
        "completed_at": "2026-08-22T10:15:05Z",
        "error": None,
    }
    payload.update(updates)
    return payload


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    """Run a CLI command against a fake HTTP API transport."""
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_experiment_lists_the_run_experiments_with_metrics_and_tracker_run() -> None:
    """The experiment table prints name, status, seed, tracker run and its metric values."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == f"http://api.test/runs/{RUN_ID}/experiments"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [_experiment_payload()]},
            request=request,
        )

    result = _invoke_with_api(["experiment", RUN_ID], handler)

    assert result.exit_code == 0
    assert "baseline-logreg" in result.output
    assert "COMPLETED" in result.output
    assert "42" in result.output
    assert "mlflow_run_abc123" in result.output
    assert "artifact_" + "4" * 31 + "d" in result.output
    assert "accuracy=0.87" in result.output
    assert "C=1.0" in result.output


def test_experiment_reports_no_experiments_found() -> None:
    """An empty experiment list is a successful result with a useful message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": RUN_ID, "items": []}, request=request)

    result = _invoke_with_api(["experiment", RUN_ID], handler)

    assert result.exit_code == 0
    assert "No experiments found." in result.output


def test_experiment_renders_missing_optional_fields_as_a_dash() -> None:
    """A running experiment with no seed, tracker run or metrics yet still renders."""

    def handler(request: httpx.Request) -> httpx.Response:
        experiment = _experiment_payload(
            status="RUNNING",
            seed=None,
            tracker_run_id=None,
            model_artifact_id=None,
            parameters={},
            metrics={},
        )
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [experiment]},
            request=request,
        )

    result = _invoke_with_api(["experiment", RUN_ID], handler)

    assert result.exit_code == 0
    assert "RUNNING" in result.output
    assert "Parameters" not in result.output
    assert "Metrics" not in result.output


def test_experiment_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["experiment", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_experiment_reports_a_missing_run() -> None:
    """A missing Run uses the same not-found message as other Run commands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"code": "run_not_found", "message": "Run not found.", "details": {}},
            request=request,
        )

    result = _invoke_with_api(["experiment", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output
