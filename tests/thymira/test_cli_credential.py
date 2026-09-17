"""Integration tests for the CLI half of F13.3's authentication leg.

Real boundary: the Typer application and ``ApiClient``'s HTTP serialization. Faked boundary: the
API, through ``httpx.MockTransport`` -- these tests pin what the CLI *sends* and what it *prints*,
not what the server decides.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_TOKEN
from thymira.cli.__main__ import app
from thymira.cli.client import API_TOKEN_ENV_VAR, ApiClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from typer.testing import Result

pytestmark = pytest.mark.integration

runner = CliRunner()
RUN_ID = "run_4f28c60d5d5a4fe5b14c5e68931f1234"
_EXPECTED_AUTHORIZATION = f"Bearer {TEST_TOKEN}"


def _run_payload(**updates: object) -> dict[str, object]:
    """Build the minimal Run body the CLI's renderer accepts."""
    payload: dict[str, object] = {
        "id": RUN_ID,
        "status": "COMPLETED",
        "prompt": "Analyze iris.csv",
        "created_at": "2026-08-22T10:14:05Z",
        "started_at": None,
        "agent_ids": [],
        "artifact_ids": [],
    }
    payload.update(updates)
    return payload


def _invoke_with_api(args: list[str], handler: Callable[[httpx.Request], httpx.Response]) -> Result:
    """Run one CLI command against a mock transport, with a credential-carrying client."""
    client = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(handler))
    try:
        return runner.invoke(app, args, obj=client)
    finally:
        client.close()


def test_every_request_carries_the_bearer_token() -> None:
    """A GET, a POST and the SSE follow stream all present the credential.

    The follow path goes through ``httpx.Client.stream`` with its own ``headers=`` argument, which
    merges rather than replaces the client defaults -- worth pinning, because a replace would have
    left exactly one unauthenticated request in the CLI.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("authorization")))
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
                request=request,
            )
        return httpx.Response(202, json=_run_payload(), request=request)

    resumed = _invoke_with_api(["resume", RUN_ID], handler)
    followed = _invoke_with_api(["events", RUN_ID, "--follow"], handler)

    assert resumed.exit_code == 0, resumed.output
    assert followed.exit_code == 0, followed.output
    assert seen, "no request reached the transport; the assertion below would be vacuous"
    assert {method for method, _ in seen} >= {"GET", "POST"}
    for method, authorization in seen:
        assert authorization == _EXPECTED_AUTHORIZATION, method


def test_a_missing_token_is_a_clear_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no credential the CLI names the variable and exits 2, printing no traceback."""
    monkeypatch.delenv(API_TOKEN_ENV_VAR, raising=False)

    result = runner.invoke(app, ["status", RUN_ID])

    assert result.exit_code == 2
    assert API_TOKEN_ENV_VAR in result.output
    assert "Traceback" not in result.output


def test_a_blank_token_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty variable is the shape a shell produces; it is not a credential."""
    monkeypatch.setenv(API_TOKEN_ENV_VAR, "   ")

    result = runner.invoke(app, ["status", RUN_ID])

    assert result.exit_code == 2
    assert API_TOKEN_ENV_VAR in result.output


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "rejected the credential"), (403, "not permitted")],
)
def test_a_refused_request_is_a_clear_error(status: int, expected: str) -> None:
    """A refusal names the credential variable and the kind of refusal, never a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"code": "unauthenticated", "message": "no", "details": {}},
            request=request,
        )

    result = _invoke_with_api(["status", RUN_ID], handler)

    assert result.exit_code == 1
    assert expected in result.output
    assert API_TOKEN_ENV_VAR in result.output
    assert "Traceback" not in result.output


def test_no_cli_output_ever_contains_the_token() -> None:
    """The credential reaches the wire and nothing else -- not stdout, not an error body."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"code": "forbidden", "message": "no"})

    refused = _invoke_with_api(["status", RUN_ID], handler)
    succeeded = _invoke_with_api(
        ["status", RUN_ID],
        lambda request: httpx.Response(200, json=_run_payload(), request=request),
    )

    assert TEST_TOKEN not in refused.output
    assert TEST_TOKEN not in succeeded.output


def test_cli_token_variable_matches_the_api_boundary() -> None:
    """A mechanical gate on the deliberate duplication the CLI's layering rule forces.

    The CLI imports no runtime member, so the variable name is duplicated rather than imported;
    this test -- which may import both -- is what keeps the two halves from drifting apart. Same
    arrangement as ``test_cli_env.py::test_cli_bootstrap_blocklist_matches_the_runtime_boundary``.
    """
    from thymira.api import credential

    assert API_TOKEN_ENV_VAR == credential.API_TOKEN_ENV_VAR


def test_the_client_cannot_be_built_without_a_credential() -> None:
    """``token`` is a required keyword argument, so no caller can build an anonymous client.

    Read off the signature rather than by calling with it missing: ``ty`` rejects that call at the
    gate, so the test could not be written as a ``TypeError`` node without silencing the very
    diagnostic that proves the point.
    """
    parameter = inspect.signature(ApiClient.__init__).parameters["token"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
