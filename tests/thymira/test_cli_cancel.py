"""Acceptance tests for the ``thymira cancel`` command.

The abort a vanished or changed-its-mind caller performs has to be available to the MVP's only
real client, with the CLI's usual run-id validation and error rendering.
"""

from __future__ import annotations

import json
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
INPUT_ID = "input_4f28c60d5d5a4fe5b14c5e68931f1234"


def _run_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "BLOCKED",
        "prompt": "Analyze iris.csv",
        "created_at": "2026-08-22T10:14:05Z",
        "started_at": "2026-08-22T10:14:06Z",
        "agent_ids": ["agent_1"],
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


def _receipt() -> dict[str, object]:
    """Build a strict durable control-input acknowledgement."""
    return {
        "input_id": INPUT_ID,
        "run_id": RUN_ID,
        "accepted": True,
        "duplicate": False,
        "durable_at": "2026-09-08T10:00:00Z",
    }


def _turn_ended_response(request: httpx.Request) -> httpx.Response:
    """Return the canonical turn end that settles the test control input."""
    content = (
        b"id: 0\n"
        b"event: turn.ended\n"
        b'data: {"seq":0,"type":"turn.ended","actor":{"id":"system"},'
        b'"payload":{"turn_id":"turn_4f28c60d5d5a4fe5b14c5e68931f1234",'
        b'"run_id":"run_4f28c60d5d5a4fe5b14c5e68931f1234",'
        b'"claimed_input_ids":["input_4f28c60d5d5a4fe5b14c5e68931f1234"],'
        b'"work_ids":[],"step_count":1,"end_reason":"completed","cause":null}}\n\n'
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=content,
        request=request,
    )


def test_cancel_command_reports_the_cancelled_run() -> None:
    """Cancel calls the canonical endpoint and renders the returned Run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(), request=request)
        assert request.method == "POST"
        assert request.url == f"http://api.test/runs/{RUN_ID}/cancel"
        assert json.loads(request.content) == {"actor": "val", "reason": "the caller went away"}
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(
        ["cancel", RUN_ID, "--actor", "val", "--reason", "the caller went away"], handler
    )

    assert result.exit_code == 0
    assert "Run cancelled" in result.output
    assert "  Status      BLOCKED" in result.output


def test_cancel_command_sends_no_body_when_nothing_was_declared() -> None:
    """Neither an actor nor a reason is required to abort a run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(), request=request)
        assert request.content in (b"", b"null")
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["cancel", RUN_ID], handler)

    assert result.exit_code == 0


def test_cancel_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["cancel", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_cancel_reports_a_missing_run() -> None:
    """An API 404 becomes the same concise not-found message as status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found"}, request=request)

    result = _invoke_with_api(["cancel", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output


def test_cancel_reports_a_refused_abort() -> None:
    """An API 409 leaves the operator with the runtime's own reason and no run cancelled."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"title": "cancel_failed", "detail": "run integrity error"},
            request=request,
        )

    result = _invoke_with_api(["cancel", RUN_ID], handler)

    assert result.exit_code == 1
    assert "No run was cancelled." in result.output
