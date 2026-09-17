"""Integration tests for the CLI human approval commands.

Real: Typer command dispatch and the HTTP client. Faked: the remote API transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient, PendingApprovalView
from thymira.cli.render import render_pending_approval

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"
DECISION_ID = "policy_" + "3" * 32
INPUT_ID = "input_4f28c60d5d5a4fe5b14c5e68931f1234"


def _run_payload(**updates: object) -> dict[str, object]:
    """Build a valid API Run response for the command boundary."""
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "AUDITING",
        "prompt": "Review the model",
        "created_at": "2026-08-22T10:14:05Z",
        "started_at": "2026-08-22T10:14:06Z",
        "agent_ids": ["agent_1"],
        "tool_call_ids": [],
        "experiment_ids": [],
        "artifact_ids": [],
    }
    payload.update(updates)
    return payload


def _event_envelope(seq: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    """Build a valid API Event envelope wrapping one event payload."""
    return {
        "event_id": f"event_{seq:032x}",
        "run_id": RUN_ID,
        "seq": seq,
        "type": event_type,
        "schema_version": "0.3",
        "ts": "2026-08-22T10:14:05Z",
        "actor": {"kind": "system", "id": "system", "role": None, "authenticated": True},
        "surface": "log_only",
        "producer": "test",
        "producer_version": "test-v1",
        "payload": payload,
        "prev_hash": "0" * 64,
        "hash": "1" * 64,
    }


def _pending_payload(**overrides: object) -> dict[str, object]:
    """Build a valid human.approval_requested event payload."""
    payload: dict[str, object] = {
        "decision_id": DECISION_ID,
        "rule_id": "budget_over_threshold",
        "reason": "Estimated cost exceeds the configured budget.",
        "summary": "Approve continuing past the budget threshold.",
        "cost_so_far": {"usd": 12.5},
    }
    payload.update(overrides)
    return payload


def _tool_pending_payload(**overrides: object) -> dict[str, object]:
    """Build a valid human.approval_requested payload for a tool-call review."""
    payload: dict[str, object] = {
        "decision_id": DECISION_ID,
        "subject_kind": "tool_call",
        "rule_id": "tool_requires_review",
        "reason": "The tool call requires human review.",
        "summary": "Approve running the tool.",
        "tool": "echo",
        "arguments": {"value": "x"},
        "tool_intent_sha256": "a" * 64,
    }
    payload.update(overrides)
    return payload


def _wait_for_approval_payload(decision_id: str) -> dict[str, object]:
    """Build a valid run.transitioned payload parking the Run on one pending decision."""
    return {
        "command": "wait_for_approval",
        "from_version": 1,
        "to_version": 2,
        "previous_stage": "auditing",
        "previous_condition": "running",
        "previous_wait_reason": None,
        "policy_decision": {"id": decision_id},
    }


def _events_page(items: list[dict[str, object]]) -> dict[str, object]:
    """Build a complete (single-page) API Event history response."""
    return {"run_id": RUN_ID, "items": items, "next_after_seq": None, "has_more": False}


def _pending_events_handler(request: httpx.Request) -> httpx.Response:
    """Answer only the pending-approval events fetch with one open request."""
    assert request.method == "GET"
    return httpx.Response(
        200,
        json=_events_page([_event_envelope(0, "human.approval_requested", _pending_payload())]),
        request=request,
    )


def _invoke_with_api(
    args: list[str],
    handler: Callable[[httpx.Request], httpx.Response],
) -> Result:
    """Run a CLI command against a fake HTTP API transport."""
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


@pytest.mark.parametrize(
    ("command", "expected_status", "expected_title"),
    [("approve", "COMPLETED", "Run approved"), ("reject", "BLOCKED", "Run rejected")],
)
def test_human_decision_posts_payload_and_renders_status(
    command: str,
    expected_status: str,
    expected_title: str,
) -> None:
    """Approve and reject post actor/note and render only the compact result."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return _pending_events_handler(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status=expected_status), request=request)
        assert request.method == "POST"
        assert request.url == f"http://api.test/runs/{RUN_ID}/{command}"
        assert request.read() == b'{"actor":"mario","note":"Reviewed the evidence."}'
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(
        [command, RUN_ID, "--actor", "mario", "--note", "Reviewed the evidence."],
        handler,
    )

    assert result.exit_code == 0
    assert "Pending decision" not in result.output
    assert expected_title in result.output
    assert f"  Status      {expected_status}" in result.output


@pytest.mark.parametrize(
    ("command", "expected_title"),
    [("approve", "Tool call approved"), ("reject", "Tool call rejected")],
)
@pytest.mark.parametrize("status", ["COMPLETED", "WAITING_FOR_APPROVAL"])
def test_tool_decision_reports_the_call_answer_and_resulting_run_status(
    command: str, expected_title: str, status: str
) -> None:
    """Answering one tool call does not approve or reject the whole Run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=_events_page(
                    [_event_envelope(0, "human.approval_requested", _tool_pending_payload())]
                ),
                request=request,
            )
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status=status), request=request)
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api([command, RUN_ID, "--actor", "mario"], handler)

    assert result.exit_code == 0
    assert expected_title in result.output
    assert f"  Status      {status}" in result.output
    if status == "WAITING_FOR_APPROVAL":
        assert "Human review required to continue" in result.output
        assert f"thymira review {RUN_ID}" in result.output


def test_approve_prints_the_resolved_decision() -> None:
    """The compact approval receipt includes the resolved Decision when available."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return _pending_events_handler(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_run_payload(status="COMPLETED", final_decision="PASS"),
                request=request,
            )
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["approve", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Pending decision" not in result.output
    assert "Decision    PASS" in result.output


def test_reject_reason_posts_the_reason_as_the_decision_note() -> None:
    """--reason maps onto the API's note field and is shown before it is posted."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return _pending_events_handler(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="BLOCKED"), request=request)
        assert request.read() == b'{"note":"Missing subgroup metrics."}'
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(
        ["reject", RUN_ID, "--reason", "Missing subgroup metrics."],
        handler,
    )

    assert result.exit_code == 0
    assert "Run rejected" in result.output


def test_approval_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["approve", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_approval_reports_a_run_without_pending_review() -> None:
    """A pending decision that resolves elsewhere first becomes a concise CLI error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _pending_events_handler(request)
        return httpx.Response(
            409,
            json={"code": "approval_not_pending", "message": "No approval pending."},
            request=request,
        )

    result = _invoke_with_api(["approve", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' is not awaiting a human approval." in result.output
    assert "No decision was recorded." in result.output


def test_approval_reports_nothing_pending_without_posting_a_decision() -> None:
    """An empty event history is reported cleanly and never reaches the decision route."""
    posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.method == "POST":
            posted = True
        return httpx.Response(200, json=_events_page([]), request=request)

    result = _invoke_with_api(["approve", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' has no pending human-approval decision." in result.output
    assert posted is False


def test_approval_reports_a_missing_run() -> None:
    """A missing Run uses the same not-found message as other Run commands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found"}, request=request)

    result = _invoke_with_api(["reject", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output


def test_approval_rejects_an_empty_actor() -> None:
    """An empty actor is rejected locally rather than sent to the API."""
    result = runner.invoke(app, ["reject", RUN_ID, "--actor", " "])

    assert result.exit_code == 2
    assert "--actor cannot be empty" in result.output


def test_approve_sends_no_actor_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --actor the CLI asserts nobody: the credential already names who is acting.

    The CLI used to default ``--actor`` to ``THYMIRA_ACTOR`` or the OS user, because an approval
    naming nobody was recorded as ``Actor.system()`` (bug-hunt C6). Authentication supplies the
    identity now, and an OS username that differs from the token's principal would be refused
    ``actor_mismatch`` on every machine, so guessing an identity is strictly worse than sending
    none.
    """
    monkeypatch.setenv("THYMIRA_ACTOR", "reviewer@bank.example")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return _pending_events_handler(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="COMPLETED"), request=request)
        assert request.read() == b"{}"
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["approve", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Run approved" in result.output


def test_render_pending_approval_shows_the_tool_call() -> None:
    """render_pending_approval prints Tool and Arguments lines for a tool-call review."""
    pending = PendingApprovalView(
        decision_id=DECISION_ID,
        rule_id="tool_requires_review",
        reason="The tool call requires human review.",
        summary="Approve running the tool.",
        cost_so_far=None,
        tool_call={"tool": "echo", "arguments": {"value": "x"}},
    )

    rendered = render_pending_approval(pending)

    assert "Tool" in rendered
    assert "echo" in rendered
    assert "Arguments" in rendered
    assert "value=x" in rendered


def test_render_pending_approval_omits_tool_fields_for_a_run_level_review() -> None:
    """A run-level review (no tool call) never prints Tool or Arguments lines."""
    pending = PendingApprovalView(
        decision_id=DECISION_ID,
        rule_id="budget_over_threshold",
        reason="Estimated cost exceeds the configured budget.",
        summary="Approve continuing past the budget threshold.",
        cost_so_far=None,
        tool_call=None,
    )

    rendered = render_pending_approval(pending)

    assert "Tool" not in rendered
    assert "Arguments" not in rendered


def test_approve_keeps_the_tool_call_result_compact() -> None:
    """A tool-call approval receipt names the subject without repeating its full arguments."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=_events_page(
                    [_event_envelope(0, "human.approval_requested", _tool_pending_payload())]
                ),
                request=request,
            )
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="COMPLETED"), request=request)
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["approve", RUN_ID, "--actor", "reviewer"], handler)

    assert result.exit_code == 0
    assert "Tool call approved" in result.output
    assert "value=x" not in result.output


def test_approve_shows_the_decision_the_run_is_parked_on_not_a_later_request() -> None:
    """A fan-out step defers a second call before the parked review is answered (R2/R6).

    The human is shown the decision the Run actually parked on, not whichever was requested last.
    """
    second_decision_id = "policy_" + "4" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=_events_page(
                    [
                        _event_envelope(0, "human.approval_requested", _pending_payload()),
                        _event_envelope(
                            1, "run.transitioned", _wait_for_approval_payload(DECISION_ID)
                        ),
                        _event_envelope(
                            2,
                            "human.approval_requested",
                            _pending_payload(
                                decision_id=second_decision_id,
                                reason="A second, unrelated call also needs review.",
                            ),
                        ),
                    ]
                ),
                request=request,
            )
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="COMPLETED"), request=request)
        assert request.url == f"http://api.test/runs/{RUN_ID}/approve"
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["approve", RUN_ID, "--actor", "reviewer"], handler)

    assert result.exit_code == 0
    assert "Run approved" in result.output
    assert "A second, unrelated call also needs review." not in result.output


def test_review_shows_the_pending_decision_without_posting() -> None:
    """The read-only review command explains the decision before approval is chosen."""
    posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.method == "POST":
            posted = True
        if request.url.path == f"/runs/{RUN_ID}":
            return httpx.Response(
                200, json=_run_payload(status="WAITING_FOR_APPROVAL"), request=request
            )
        assert request.url.path.endswith("/events")
        return httpx.Response(
            200,
            json=_events_page([_event_envelope(0, "human.approval_requested", _pending_payload())]),
            request=request,
        )

    result = _invoke_with_api(["review", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Human review required" in result.output
    assert "What this run is doing" in result.output
    assert "Review the model" in result.output
    assert "Why approval is needed" in result.output
    assert "thymira approve" in result.output
    assert posted is False


def test_approve_never_asks_the_operating_system_who_is_running_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OS username is not an identity the API would accept, so the CLI never sends it."""
    monkeypatch.delenv("THYMIRA_ACTOR", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "marioo14")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("follow") == "true":
            return _turn_ended_response(request)
        if request.method == "GET" and request.url.path.endswith("/events"):
            return _pending_events_handler(request)
        if request.method == "GET":
            return httpx.Response(200, json=_run_payload(status="COMPLETED"), request=request)
        assert b"marioo14" not in request.read()
        return httpx.Response(202, json=_receipt(), request=request)

    result = _invoke_with_api(["approve", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Run approved" in result.output
