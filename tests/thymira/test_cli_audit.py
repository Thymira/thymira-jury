"""Acceptance tests for the CLI ``audit`` command.

Real: Typer command dispatch and the HTTP client. Faked: the remote API transport.
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
from thymira.mira.checks import AuditReport

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"


def _control_payload(**updates: object) -> dict[str, object]:
    """Build a valid API ControlResult entry."""
    payload: dict[str, object] = {
        "control_id": "A1",
        "title": "Event chain integrity",
        "status": "PASSED",
        "severity": "LOW",
        "detail": "Hash chain verified for 12 events.",
        "evidence": [],
    }
    payload.update(updates)
    return payload


def _finding_payload(**updates: object) -> dict[str, object]:
    """Build a valid API AuditFinding entry."""
    payload: dict[str, object] = {
        "id": "finding_" + "1" * 31 + "a",
        "run_id": RUN_ID,
        "agent_id": None,
        "control_id": "A9",
        "framework": "METHODOLOGY",
        "title": "Leakage risk not documented",
        "finding": "No evidence of a leakage check before training.",
        "severity": "MEDIUM",
        "confidence": 0.7,
        "evidence": [],
        "recommendation": None,
        "created_at": "2026-08-22T10:14:05Z",
    }
    payload.update(updates)
    return payload


def _report_payload(**updates: object) -> dict[str, object]:
    """Build a valid API AuditReport response body."""
    payload: dict[str, object] = {
        "run_id": RUN_ID,
        "control_set": "thymira-mira-checks@0.1",
        "status": "passed",
        "controls": [_control_payload()],
        "findings": [],
        "terminal_hash": None,
        "manifest_sha256": None,
        "summary": {},
    }
    payload.update(updates)
    return payload


def _decision_payload(**updates: object) -> dict[str, object]:
    """Build a valid API PolicyDecision entry."""
    payload: dict[str, object] = {
        "id": "policy_" + "2" * 31 + "b",
        "run_id": RUN_ID,
        "subject_kind": "findings",
        "subject_id": "run",
        "decision": "PASS",
        "rule_id": "default",
        "reason": "No findings above the policy threshold.",
        "policy_name": "credit-risk@1.0",
        "policy_sha256": "a" * 64,
        "finding_ids": [],
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


def test_audit_prints_controls_and_a_pass_decision() -> None:
    """A PASS audit requests the audit route and prints each control row and the decision."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == f"http://api.test/runs/{RUN_ID}/audit"
        return httpx.Response(
            200,
            json={"report": _report_payload(), "decision": _decision_payload()},
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID], handler)

    assert result.exit_code == 0
    assert "A1" in result.output
    assert "PASSED" in result.output
    assert "LOW" in result.output
    assert "Hash chain verified for 12 events." in result.output
    assert "Decision: PASS" in result.output


def test_audit_prints_findings_when_present() -> None:
    """Findings render alongside the controls when the report carries any."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "report": _report_payload(
                    status="passed_with_warnings", findings=[_finding_payload()]
                ),
                "decision": _decision_payload(decision="WARNING"),
            },
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Findings" in result.output
    assert "Leakage risk not documented" in result.output
    assert "Decision: WARNING" in result.output


def test_audit_prints_the_approval_hint_for_require_human_review() -> None:
    """A REQUIRE_HUMAN_REVIEW decision prints the approve/reject next step."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "report": _report_payload(status="failed"),
                "decision": _decision_payload(
                    decision="REQUIRE_HUMAN_REVIEW",
                    reason="A finding needs a human review.",
                ),
            },
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Decision: REQUIRE_HUMAN_REVIEW" in result.output
    assert f"thymira approve {RUN_ID}" in result.output


def test_audit_renders_no_decision_recorded_when_absent() -> None:
    """A report with no policy decision yet still renders successfully."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"report": _report_payload(), "decision": None},
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID], handler)

    assert result.exit_code == 0
    assert "Decision: none recorded" in result.output


def test_audit_json_emits_a_document_that_parses_back_to_the_audit_report_shape() -> None:
    """--json emits the raw AuditReport, which must validate against the real contract."""
    report = _report_payload(findings=[_finding_payload()])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"report": report, "decision": _decision_payload()},
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID, "--json"], handler)

    assert result.exit_code == 0
    parsed = AuditReport.model_validate(json.loads(result.output))
    assert parsed.run_id == RUN_ID
    assert parsed.controls[0].control_id == "A1"
    assert parsed.findings[0].control_id == "A9"


def test_audit_bundle_prints_the_verified_bundle_json() -> None:
    """--bundle asks the API for the complete assurance export and keeps CLI stateless."""
    bundle = {
        "run_id": RUN_ID,
        "bundle_sha256": "b" * 64,
        "report": _report_payload(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == f"http://api.test/runs/{RUN_ID}/assurance"
        return httpx.Response(200, json=bundle, request=request)

    result = _invoke_with_api(["audit", RUN_ID, "--bundle"], handler)

    assert result.exit_code == 0
    assert json.loads(result.output) == bundle


def test_audit_rejects_a_noncanonical_run_id() -> None:
    """Malformed identifiers fail before the API is called."""
    result = runner.invoke(app, ["audit", "123"])

    assert result.exit_code == 2
    assert "RUN_ID must look like 'run_'" in result.output


def test_audit_reports_a_missing_run() -> None:
    """A missing Run uses the same not-found message as other Run commands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"code": "run_not_found", "message": "Run not found.", "details": {}},
            request=request,
        )

    result = _invoke_with_api(["audit", RUN_ID], handler)

    assert result.exit_code == 1
    assert f"Run '{RUN_ID}' was not found." in result.output
