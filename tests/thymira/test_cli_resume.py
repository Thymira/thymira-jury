"""Acceptance tests for the ``thymira resume`` command."""

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
INPUT_ID = "input_4f28c60d5d5a4fe5b14c5e68931f1234"


def _run_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "PAUSED",
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


def _interview_payload(**updates: object) -> dict[str, object]:
    """Build the minimum response exposed by the risk-interview API routes."""
    payload: dict[str, object] = {
        "run": _run_payload(status="WAITING_FOR_APPROVAL"),
        "profile": {"id": "profile_4f28c60d5d5a4fe5b14c5e68931f1234"},
        "pending_question": {
            "field": "jurisdiction",
            "question": "Which jurisdiction governs this activity?",
            "profile_id": "profile_4f28c60d5d5a4fe5b14c5e68931f1234",
            "profile_version": 2,
            "question_number": 2,
        },
        "requires_human_review": False,
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


def test_resume_posts_to_the_api_and_renders_new_status() -> None:
    """Resume calls the canonical endpoint and renders the returned Run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="RUNNING"), request=request)
        assert request.method == "POST"
        assert request.url == f"http://api.test/runs/{RUN_ID}/resume"
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["resume", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Run resumed" in result.output
    assert "  Status      RUNNING" in result.output


def test_resume_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["resume", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_resume_reports_a_missing_run() -> None:
    """An API 404 becomes the same concise not-found message as status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found"}, request=request)

    result = _invoke_with_api(["resume", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output


def test_status_renders_the_pending_risk_interview_question() -> None:
    """Status exposes the one answer the Run needs before it can start THY work."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/runs/{RUN_ID}/risk-interview":
            return httpx.Response(200, json=_interview_payload(), request=request)
        if request.url.path == f"/runs/{RUN_ID}/events":
            return httpx.Response(
                200,
                json={"run_id": RUN_ID, "items": [], "next_after_seq": None, "has_more": False},
                request=request,
            )
        return httpx.Response(200, json=_run_payload(), request=request)

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Risk information required" in result.output
    assert "Which jurisdiction governs this activity?" in result.output
    assert f"thymira answer {RUN_ID}" in result.output


def test_answer_accepts_the_interview_response_the_current_api_returns() -> None:
    """The live API answers the interview itself; the CLI must not demand a durable receipt.

    Reproduced by the real-model smoke on main: `POST /runs/{id}/risk-interview` returns the
    `RiskInterviewResponse` (Run + profile + next question, 202), and `thymira answer` failed with
    "The enqueue receipt has invalid acceptance fields" although the answer had been recorded.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(200, json=_run_payload(status="RUNNING"), request=request)
        if request.method == "GET":
            return httpx.Response(200, json=_interview_payload(), request=request)
        assert request.method == "POST"
        assert request.url == f"http://api.test/runs/{RUN_ID}/risk-interview"
        return httpx.Response(202, json=_interview_payload(), request=request)

    result = _invoke_with_api(["answer", RUN_ID, "ES"], handler)

    assert result.exit_code == 0, result.output
    assert "invalid acceptance fields" not in result.output
    assert "Question" in result.output


def test_answer_posts_to_the_risk_interview_api_and_renders_the_next_question() -> None:
    """The CLI sends one answer and immediately shows the next bounded prompt."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(200, json=_run_payload(status="RUNNING"), request=request)
        if request.method == "GET":
            return httpx.Response(200, json=_interview_payload(), request=request)
        assert request.method == "POST"
        assert request.url == f"http://api.test/runs/{RUN_ID}/risk-interview"
        assert request.content == b'{"answer":"ES"}'
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["answer", RUN_ID, "ES"], handler)

    assert result.exit_code == 0
    assert "Run updated" in result.output
    assert "Risk information required" in result.output
    assert "Question" in result.output
    assert "  2" in result.output
