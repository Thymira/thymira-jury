"""Acceptance tests for the durable ``thymira plan`` command.

Real: Typer command dispatch and the HTTP client. Faked: the remote API transport. The sequence
proves that a POST receipt is observed through ``turn.ended`` before the read-only plan GET.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient, ApiDomainError

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"
INPUT_ID = "input_4f28c60d5d5a4fe5b14c5e68931f1234"
TURN_ID = "turn_4f28c60d5d5a4fe5b14c5e68931f1234"
PLAN_ID = "plan_4f28c60d5d5a4fe5b14c5e68931f1234"
ARTIFACT_ID = "artifact_4f28c60d5d5a4fe5b14c5e68931f1234"
ARTIFACT_DIGEST = hashlib.sha256(b"# Plan\n\nProfile data").hexdigest()


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    """Run a CLI command against a fake HTTP API transport."""
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def _receipt() -> dict[str, object]:
    """Build the strict durable acknowledgement for one plan request."""
    return {
        "input_id": INPUT_ID,
        "run_id": RUN_ID,
        "accepted": True,
        "duplicate": False,
        "durable_at": "2026-09-08T10:00:00Z",
    }


def _run_payload(**updates: object) -> dict[str, object]:
    """Build a minimum valid Run snapshot used after receipt settlement."""
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "COMPLETED",
        "prompt": "Plan the analysis",
        "agent_ids": [],
        "artifact_ids": [],
        "tool_call_ids": [],
        "experiment_ids": [],
    }
    payload.update(updates)
    return payload


def _turn_ended_frame() -> bytes:
    """Build the canonical strict terminal event correlated to the plan input."""
    return (
        b"id: 0\n"
        b"event: turn.ended\n"
        b'data: {"seq":0,"type":"turn.ended","actor":{"id":"system"},'
        b'"payload":{"turn_id":"turn_4f28c60d5d5a4fe5b14c5e68931f1234",'
        b'"run_id":"run_4f28c60d5d5a4fe5b14c5e68931f1234",'
        b'"claimed_input_ids":["input_4f28c60d5d5a4fe5b14c5e68931f1234"],'
        b'"work_ids":[],"step_count":1,"end_reason":"completed","cause":null}}\n\n'
    )


def _durable_plan(*, empty: bool = False) -> dict[str, object]:
    """Build the complete persisted plan response, including verified artifact content."""
    return {
        "run_id": RUN_ID,
        "plan_id": PLAN_ID,
        "revision": 2,
        "goal": "Build a reproducible model",
        "goal_status": "active",
        "items": []
        if empty
        else [
            {
                "item_id": "task_4f28c60d5d5a4fe5b14c5e68931f1234",
                "ordinal": 0,
                "text": "Profile data",
                "state": "todo",
            }
        ],
        "completions": [],
        "mode": "execution",
        "artifact": None
        if empty
        else {
            "artifact_id": ARTIFACT_ID,
            "run_id": RUN_ID,
            "revision": 2,
            "sha256": ARTIFACT_DIGEST,
            "storage_key": "plans/plan.md",
            "advisory": True,
            "content": "# Plan\n\nProfile data",
        },
        "review_feedback": [],
        "rounds": [],
        "autonomous_rounds": 0,
        "blocked_cause": None,
        "blocked_count": 0,
        "repeat_state": {"tool_name": None, "arguments_sha256": None, "consecutive_count": 0},
    }


def test_plan_waits_for_receipt_settlement_then_renders_persisted_board() -> None:
    """The plan command never renders the 202 receipt as if it were a completed plan."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url == f"http://api.test/runs/{RUN_ID}/plan"
            return httpx.Response(202, json=_receipt(), request=request)
        if request.url.params.get("follow") == "true":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_turn_ended_frame(),
                request=request,
            )
        if request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(200, json=_run_payload(), request=request)
        assert request.method == "GET"
        assert request.url.path == f"/runs/{RUN_ID}/plan"
        return httpx.Response(200, json=_durable_plan(), request=request)

    result = _invoke_with_api(["plan", RUN_ID], handler)

    assert result.exit_code == 0
    assert f"Plan for {RUN_ID}" in result.output
    assert f"Plan ID: {PLAN_ID}" in result.output
    assert "Profile data" in result.output
    assert "# Plan" in result.output
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]
    assert requests[-1].url.path == f"/runs/{RUN_ID}/plan"


def test_plan_renders_empty_persisted_board() -> None:
    """An empty durable board still renders successfully after its turn ends."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_receipt(), request=request)
        if request.url.params.get("follow") == "true":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_turn_ended_frame(),
                request=request,
            )
        if request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(200, json=_run_payload(), request=request)
        return httpx.Response(200, json=_durable_plan(empty=True), request=request)

    result = _invoke_with_api(["plan", RUN_ID], handler)

    assert result.exit_code == 0
    assert "No todo items were proposed." in result.output


def test_plan_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["plan", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_plan_reports_a_missing_run() -> None:
    """A missing Run uses the same not-found message as other Run commands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"code": "run_not_found", "message": "Run not found.", "details": {}},
            request=request,
        )

    result = _invoke_with_api(["plan", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output


def test_plan_not_found_remains_a_domain_outcome() -> None:
    """A missing persisted plan is distinct from a missing Run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"code": "plan_not_found", "message": "No plan is persisted.", "details": {}},
            request=request,
        )

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiDomainError) as raised:
            client.get_durable_plan(RUN_ID)
    finally:
        client.close()

    assert raised.value.code == "plan_not_found"
