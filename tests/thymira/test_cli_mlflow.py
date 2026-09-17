"""Acceptance tests for the CLI ``mlflow`` command.

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


def _mlflow_payload(**updates: object) -> dict[str, object]:
    """Build a valid API MLflow tracker-run entry."""
    payload: dict[str, object] = {
        "tracker_run_id": "run_" + "a" * 32,
        "experiment_name": "baseline-logreg",
        "status": "FINISHED",
        "params": {"seed": "7", "dataset": "german_credit"},
        "metrics": {"accuracy": 0.87, "auc": 0.91},
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


def test_mlflow_lists_tracker_runs_with_their_metrics() -> None:
    """The mlflow table prints each tracker run with its params and metric values."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == f"http://api.test/runs/{RUN_ID}/mlflow"
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [_mlflow_payload()]},
            request=request,
        )

    result = _invoke_with_api(["mlflow", "--run", RUN_ID], handler)

    assert result.exit_code == 0
    assert "baseline-logreg" in result.output
    assert "FINISHED" in result.output
    assert "run_" + "a" * 32 in result.output
    assert "accuracy=0.87" in result.output
    assert "auc=0.91" in result.output
    assert "seed=7" in result.output


def test_mlflow_reports_no_tracker_runs_found() -> None:
    """An empty MLflow run list is a successful result with a useful message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": RUN_ID, "items": []}, request=request)

    result = _invoke_with_api(["mlflow", "--run", RUN_ID], handler)

    assert result.exit_code == 0
    assert "No MLflow runs found." in result.output


def test_mlflow_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["mlflow", "--run", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_mlflow_reports_a_missing_run() -> None:
    """A missing Run uses the same not-found message as other Run commands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"code": "run_not_found", "message": "Run not found.", "details": {}},
            request=request,
        )

    result = _invoke_with_api(["mlflow", "--run", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output
