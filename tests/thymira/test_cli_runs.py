"""Acceptance tests for run listing and the richer CLI status view."""

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


def _run_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "CREATED",
        "prompt": "Analyze iris.csv",
        "created_at": "2026-08-22T10:14:05Z",
        "started_at": None,
        "agent_ids": [],
        "tool_call_ids": [],
        "experiment_ids": [],
        "artifact_ids": [],
    }
    payload.update(updates)
    return payload


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_runs_lists_api_items() -> None:
    """The runs command requests the API page and renders its visible fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == "http://api.test/runs"
        return httpx.Response(
            200,
            json={
                "items": [
                    _run_payload(status="RUNNING"),
                    _run_payload(
                        id="run_a893c60d5d5a4fe5b14c5e68931f1234",
                        status="COMPLETED",
                        prompt="Compare models",
                    ),
                ],
                "next_cursor": None,
            },
            request=request,
        )

    result = _invoke_with_api(["runs"], handler)

    assert result.exit_code == 0
    assert "Runs" in result.output
    assert RUN_ID in result.output
    assert "RUNNING" in result.output
    assert "Compare models" in result.output


def test_runs_reports_an_empty_page() -> None:
    """An empty API page is a successful result with a useful message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "next_cursor": None}, request=request)

    result = _invoke_with_api(["runs"], handler)

    assert result.exit_code == 0
    assert "No runs found." in result.output


def test_status_renders_enriched_run_fields() -> None:
    """Status displays phase and execution counters when the API supplies them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_payload(
                status="COMPLETED",
                phase="REPORTING",
                tool_call_ids=["tool_1", "tool_2"],
                experiment_ids=["experiment_1"],
                artifact_ids=["artifact_1", "artifact_2"],
                final_decision="PASS",
            ),
            request=request,
        )

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert "  Tools       2" in result.output
    assert "  Experiments 1" in result.output
    assert "  Artifacts   2" in result.output
    assert "  Phase       REPORTING" in result.output
    assert "  Decision    PASS" in result.output
