"""CLI integration tests.

Real boundary: the Typer application, package metadata and ``ApiClient`` HTTP serialization.
Faked boundary: the not-yet-implemented Thymira API, through ``httpx.MockTransport``.
"""

from __future__ import annotations

from importlib.metadata import version as package_version
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import BANNER, app
from thymira.cli.client import ApiClient, ApiTimeoutError

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
        "artifact_ids": [],
    }
    payload.update(updates)
    return payload


def _create_run_payload(**updates: object) -> dict[str, object]:
    """Build the committed Run plus publication receipt response envelope."""
    run = _run_payload(**updates)
    return {
        "run": run,
        "receipt": {
            "publication_id": "intent_5f28c60d5d5a4fe5b14c5e68931f1234",
            "run_id": RUN_ID,
            "session_id": "session_5f28c60d5d5a4fe5b14c5e68931f1234",
            "work_ids": [],
            "committed_at": "2026-08-22T10:14:05Z",
        },
    }


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    client = ApiClient(
        "http://api.test",
        token=TEST_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_banner_is_safe_for_windows_and_basic_terminals() -> None:
    """Keep the welcome screen printable without requiring UTF-8 console mode."""
    assert BANNER.isascii()


def test_cli_without_arguments_displays_branded_help() -> None:
    """The bare command should be a successful, discoverable entry point."""
    result = runner.invoke(app)

    assert result.exit_code == 0
    assert "Agentic Data Science Runtime" in result.output
    assert "Get started:" in result.output
    assert 'thymira run "Describe the Data Science work to execute."' in result.output
    assert "Usage:" in result.output
    assert "[OPTIONS] COMMAND [ARGS]..." in result.output
    assert "run" in result.output
    assert "status" in result.output


def test_cli_help_displays_available_options() -> None:
    """The root help should advertise global options and MVP commands."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in result.output
    assert "--help" in result.output
    assert "Create a new run." in result.output
    assert "Show the current state of a run." in result.output


def test_cli_version_matches_installed_package() -> None:
    """The reported version should come from package metadata."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"thymira {package_version('thymira-cli')}\n"


def test_run_posts_prompt_and_renders_created_run() -> None:
    """The run command should serialize the prompt and render the API response."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "http://api.test/runs"
        assert request.content == b'{"prompt":"Analyze iris.csv"}'
        return httpx.Response(201, json=_create_run_payload(), request=request)

    result = _invoke_with_api(["run", "Analyze iris.csv"], handler)

    assert result.exit_code == 0
    assert "Run created" in result.output
    assert f"  ID          {RUN_ID}" in result.output
    assert "  Status      CREATED" in result.output
    assert f"Next: thymira status {RUN_ID}" in result.output


def test_headless_run_prints_only_the_durable_response_to_stdout() -> None:
    """Headless mode keeps progress on stderr and settles from a streamed terminal event."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=_create_run_payload(), request=request)
        if request.url.params.get("follow") is None:
            assert request.url.params["after_seq"] == "-1"
            return httpx.Response(
                200,
                json={"run_id": RUN_ID, "items": [], "next_after_seq": None, "has_more": False},
                request=request,
            )
        return httpx.Response(
            200,
            content=(
                b"id: 0\n"
                b"event: run.completed\n"
                b'data: {"seq":0,"type":"run.completed","actor":{"id":"system"},'
                b'"payload":{"response":"final answer"}}\n\n'
                b"id: 1\n"
                b"event: turn.ended\n"
                b'data: {"seq":1,"type":"turn.ended","actor":{"id":"system"},'
                b'"payload":{"turn_id":"turn_12345678","run_id":"run_4f28c60d5d5a4fe5b14c5e68931f1234",'
                b'"claimed_input_ids":[],"work_ids":[],"step_count":1,'
                b'"end_reason":"completed","cause":null}}\n\n'
            ),
            headers={
                "content-type": "text/event-stream",
                "x-thymira-redacted-stream": "event-projection-v1",
            },
            request=request,
        )

    result = _invoke_with_api(["run", "Analyze iris.csv", "--headless"], handler)

    assert result.exit_code == 0
    assert result.stdout == "final answer\n"
    assert "Run created" not in result.stdout
    assert "submitted" in result.stderr
    assert "event 1: turn.ended" in result.stderr
    assert len(requests) == 3


def test_run_reports_unavailable_api_without_inventing_a_run() -> None:
    """A connection failure should say explicitly that no run was created."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _invoke_with_api(["run", "Analyze iris.csv"], handler)

    assert result.exit_code == 1
    assert "Cannot connect to the Thymira API at http://api.test." in result.output
    assert "No run was created." in result.output
    assert RUN_ID not in result.output


def test_api_timeout_is_not_reported_as_connection_refusal() -> None:
    """A response timeout reports an unknown outcome instead of claiming no Run exists."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow response", request=request)

    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiTimeoutError, match="outcome is unknown"):
            client.create_run("Analyze iris.csv")
    finally:
        client.close()


def test_run_timeout_directs_the_user_to_check_the_run_list() -> None:
    """A timed-out create request avoids claiming that the Run was never created."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow response", request=request)

    result = _invoke_with_api(["run", "Analyze iris.csv"], handler)

    assert result.exit_code == 1
    assert "request outcome is unknown" in result.output
    assert "Check the run list before retrying." in result.output
    assert "No run was created." not in result.output


def test_run_requires_a_prompt() -> None:
    """Typer should reject run before HTTP when its required prompt is absent."""
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 2
    assert "Missing argument" in result.output
    assert "prompt" in result.output


def test_status_gets_and_renders_current_run_state() -> None:
    """The status command should request the canonical id and render useful fields.

    Status also folds the Run's events into a THY activity view; here the run has no buffered
    events, so the activity section is absent and only the run fields are shown. The deployment
    has no Langfuse configured either, so the API reports no trace and no link is printed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == f"/runs/{RUN_ID}/risk-interview":
            return httpx.Response(
                200,
                json={
                    "run": _run_payload(status="RUNNING"),
                    "profile": {"id": "profile_4f28c60d5d5a4fe5b14c5e68931f1234"},
                    "pending_question": None,
                    "requires_human_review": False,
                },
                request=request,
            )
        if request.url.path == f"/runs/{RUN_ID}/events":
            return httpx.Response(
                200,
                json={"run_id": RUN_ID, "items": [], "next_after_seq": None, "has_more": False},
                request=request,
            )
        if request.url.path == f"/runs/{RUN_ID}/trace":
            return httpx.Response(200, json={"run_id": RUN_ID, "trace_url": None}, request=request)
        assert request.url == f"http://api.test/runs/{RUN_ID}"
        return httpx.Response(
            200,
            json=_run_payload(
                status="RUNNING",
                started_at="2026-08-22T10:14:06Z",
                agent_ids=["agent_a", "agent_b"],
                artifact_ids=["artifact_a"],
            ),
            request=request,
        )

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert f"  ID          {RUN_ID}" in result.output
    assert "  Status      RUNNING" in result.output
    assert "  Created     2026-08-22T10:14:05Z" in result.output
    assert "  Started     2026-08-22T10:14:06Z" in result.output
    assert "  Agents      2" in result.output
    assert "  Artifacts   1" in result.output
    assert "THY activity" not in result.output
    assert "Trace:" not in result.output


def test_status_points_to_review_for_a_waiting_run() -> None:
    """A paused Run gets a simple pointer to the read-only review command."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(
                200, json=_run_payload(status="WAITING_FOR_APPROVAL"), request=request
            )
        if request.url.path.endswith("/risk-interview"):
            return httpx.Response(
                200,
                json={
                    "run": _run_payload(status="WAITING_FOR_APPROVAL"),
                    "profile": None,
                    "pending_question": None,
                    "requires_human_review": False,
                },
                request=request,
            )
        if request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"trace_url": None}, request=request)
        assert request.url.path.endswith("/events")
        event = {
            "event_id": "event_" + "1" * 32,
            "run_id": RUN_ID,
            "seq": 0,
            "type": "human.approval_requested",
            "schema_version": "0.3",
            "ts": "2026-08-22T10:14:05Z",
            "actor": {"kind": "system", "id": "system", "authenticated": True},
            "surface": "log_only",
            "producer": "test",
            "producer_version": "test-v1",
            "payload": {
                "decision_id": "policy_" + "2" * 32,
                "rule_id": "default",
                "reason": "A human must decide.",
                "summary": "Continue the run.",
            },
            "prev_hash": "0" * 64,
            "hash": "1" * 64,
        }
        return httpx.Response(
            200,
            json={"run_id": RUN_ID, "items": [event], "next_after_seq": None, "has_more": False},
            request=request,
        )

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Human review required" in result.output
    assert f"thymira review {RUN_ID}" in result.output


def test_status_links_to_the_trace_when_the_runtime_is_tracing() -> None:
    """A Run and its trace share an id derivation, but only the runtime can name the project.

    So the link comes from the API rather than being built here, and the CLI prints it when the
    deployment has one — which is what makes a Run navigable from the terminal to Langfuse.
    """
    url = "https://cloud.langfuse.com/project/p1/traces/abc"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/runs/{RUN_ID}/events":
            return httpx.Response(
                200,
                json={"run_id": RUN_ID, "items": [], "next_after_seq": None, "has_more": False},
                request=request,
            )
        if request.url.path == f"/runs/{RUN_ID}/trace":
            return httpx.Response(200, json={"run_id": RUN_ID, "trace_url": url}, request=request)
        return httpx.Response(200, json=_run_payload(status="RUNNING"), request=request)

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert f"Trace: {url}" in result.output


def test_status_survives_an_api_that_has_no_trace_route() -> None:
    """The status is the command's contract: an older or partial API must not break it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/events", "/trace")):
            return httpx.Response(404, json={"code": "not_found", "message": "no"}, request=request)
        return httpx.Response(200, json=_run_payload(status="RUNNING"), request=request)

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Trace:" not in result.output


def test_status_reports_a_missing_run() -> None:
    """An API 404 should become a concise run-not-found message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found"}, request=request)

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output


def test_status_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers should fail before any API call."""
    result = runner.invoke(app, ["status", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_run_reports_an_invalid_api_response() -> None:
    """A malformed success response should not be presented as a created run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": "CREATED"}, request=request)

    result = _invoke_with_api(["run", "Analyze iris.csv"], handler)

    assert result.exit_code == 1
    assert "The Thymira API returned an invalid Run response." in result.output
    assert "No run was created." in result.output


def test_run_accepts_the_run_record_an_api_without_publication_receipts_returns() -> None:
    """A creation answered with the bare Run record is a created Run, not a protocol error.

    The durable publication envelope is the target contract; an API that has not adopted it yet
    still answers with the canonical Run record, and the CLI must read that Run rather than refuse
    the whole response for a receipt the server never claimed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=_run_payload(), request=request)

    result = _invoke_with_api(["run", "Analyze iris.csv"], handler)

    assert result.exit_code == 0, result.output
    assert "Run created" in result.output
    assert RUN_ID in result.output
    assert "Publication" not in result.output


def test_resume_still_refuses_a_receipt_with_invalid_acceptance_fields() -> None:
    """A body that claims to be an enqueue receipt is still validated strictly.

    Accepting the Run record must not become a way to smuggle a malformed receipt past the
    durable transport: only a complete Run record is read as one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "input_id": "input_5f28c60d5d5a4fe5b14c5e68931f1234",
                "run_id": RUN_ID,
                "accepted": "yes",
                "duplicate": False,
                "durable_at": "2026-08-22T10:14:05Z",
            },
            request=request,
        )

    result = _invoke_with_api(["resume", RUN_ID], handler)

    assert result.exit_code == 1
    assert "The enqueue receipt has invalid acceptance fields." in result.output
    assert "No run was resumed." in result.output
