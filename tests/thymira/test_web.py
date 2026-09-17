"""Unit tests for the web console: static assets, security headers and the API pass-through.

The console application is real and driven in process through Starlette's ``TestClient``. The
Thymira API behind it is an ``httpx.MockTransport`` that records exactly what the console
forwarded, so nothing here opens a socket.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from thymira.web import app as console_app
from thymira.web import create_app, is_forwardable_path, validate_api_url
from thymira.web.server import allowed_hosts_for, main

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

CONSOLE_ORIGIN = "http://127.0.0.1:8080"
API_URL = "http://127.0.0.1:8000"


class _RecordingApi:
    """A fake Thymira API that records every forwarded request and answers each one."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (
            lambda _request: httpx.Response(200, json={"items": [], "next_cursor": None})
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _console(
    api: Callable[[httpx.Request], httpx.Response], *, base_url: str = CONSOLE_ORIGIN
) -> TestClient:
    return TestClient(create_app(API_URL, transport=httpx.MockTransport(api)), base_url=base_url)


def test_index_is_served_with_a_same_origin_content_security_policy() -> None:
    with _console(_RecordingApi()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert response.headers["x-frame-options"] == "DENY"


def test_console_module_script_is_served_as_javascript() -> None:
    with _console(_RecordingApi()) as client:
        response = client.get("/static/js/main.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


def test_run_header_only_offers_resume_for_a_resumable_event_state() -> None:
    """The console must not race the active dispatcher with a second resume request."""
    with _console(_RecordingApi()) as client:
        response = client.get("/static/js/views/run.js")

    assert response.status_code == 200
    source = response.text
    assert "const resumable = isResumableState(state);" in source
    assert re.search(r'resumable\s+\?\s+button\("Resume"', source)
    assert 'active ? button("Resume"' not in source


def test_pass_through_forwards_the_callers_authorization_header_unchanged() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        response = client.get(
            "/api/runs", params={"limit": "5"}, headers={"Authorization": "Bearer caller-token"}
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    forwarded = api.requests[0]
    assert forwarded.url == httpx.URL(f"{API_URL}/runs?limit=5")
    assert forwarded.headers["authorization"] == "Bearer caller-token"


def test_pass_through_never_adds_an_authorization_header() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        client.get("/api/runs")

    assert "authorization" not in api.requests[0].headers


def test_pass_through_drops_cookies_and_identity_headers() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        client.get(
            "/api/runs",
            headers={
                "Cookie": "session=1",
                "X-Forwarded-For": "10.0.0.1",
                "X-Thymira-Actor": "someone-else",
            },
        )

    forwarded = api.requests[0].headers
    assert "cookie" not in forwarded
    assert "x-forwarded-for" not in forwarded
    assert "x-thymira-actor" not in forwarded


def test_pass_through_forwards_a_json_body_to_the_same_api_route() -> None:
    api = _RecordingApi(lambda _request: httpx.Response(201, json={"id": "run_1"}))

    with _console(api) as client:
        response = client.post("/api/runs", json={"prompt": "Profile it"})

    assert response.status_code == 201
    forwarded = api.requests[0]
    assert forwarded.method == "POST"
    assert forwarded.url.path == "/runs"
    assert json.loads(forwarded.content) == {"prompt": "Profile it"}


def test_pass_through_streams_server_sent_events_with_the_redaction_guard_header() -> None:
    frame = b'id: 0\nevent: run.started\ndata: {"seq":0}\n\n'
    api = _RecordingApi(
        lambda _request: httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-thymira-redacted-stream": "event-projection-v1",
            },
            content=frame,
        )
    )

    with _console(api) as client:
        response = client.get("/api/runs/run_1/events", params={"follow": "true"})

    assert response.content == frame
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-thymira-redacted-stream"] == "event-projection-v1"


@pytest.mark.parametrize(
    "path",
    ["runs", "runs/run_1/artifacts/artifact_2", "healthz", "tools/run_python", "settings/ui"],
)
def test_is_forwardable_path_accepts_api_routes(path: str) -> None:
    assert is_forwardable_path(path)


@pytest.mark.parametrize(
    "path",
    ["", "runs/../healthz", "runs/./x", "runs//x", "internal/state", "runs/a b", "runs/%2e%2e"],
)
def test_is_forwardable_path_refuses_anything_but_plain_api_segments(path: str) -> None:
    assert not is_forwardable_path(path)


def test_pass_through_refuses_a_path_outside_the_api_routes() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        response = client.get("/api/internal/state")

    assert response.status_code == 404
    assert response.json()["code"] == "not_an_api_route"
    assert api.requests == []


def test_a_request_addressed_to_a_foreign_host_name_is_refused() -> None:
    api = _RecordingApi()

    with _console(api, base_url="http://rebound.example") as client:
        response = client.get("/api/runs", headers={"Authorization": "Bearer caller-token"})

    assert response.status_code == 400
    assert api.requests == []


def test_a_cross_origin_mutation_is_refused_before_it_is_forwarded() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        response = client.post(
            "/api/runs/run_1/approve", json={}, headers={"Origin": "http://attacker.example"}
        )

    assert response.status_code == 403
    assert response.json()["code"] == "cross_origin_refused"
    assert api.requests == []


def test_a_same_origin_mutation_is_forwarded() -> None:
    api = _RecordingApi()

    with _console(api) as client:
        client.post("/api/runs/run_1/approve", json={}, headers={"Origin": CONSOLE_ORIGIN})

    assert api.requests[0].url.path == "/runs/run_1/approve"


def test_a_request_body_above_the_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_app, "MAX_REQUEST_BODY_BYTES", 8)
    api = _RecordingApi()

    with _console(api) as client:
        response = client.post("/api/runs", json={"prompt": "longer than eight bytes"})

    assert response.status_code == 413
    assert api.requests == []


def test_an_unreachable_api_is_reported_as_a_bad_gateway() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _console(refuse) as client:
        response = client.get("/api/runs")

    assert response.status_code == 502
    assert response.json()["code"] == "api_unreachable"


def test_validate_api_url_strips_a_trailing_slash() -> None:
    assert validate_api_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"


@pytest.mark.parametrize(
    "url",
    ["ftp://127.0.0.1:8000", "http://127.0.0.1:8000/?token=x", "https://127.0.0.1:8000/#part"],
)
def test_validate_api_url_refuses_an_unusable_base_url(url: str) -> None:
    with pytest.raises(ValueError, match="API URL"):
        validate_api_url(url)


def test_allowed_hosts_for_a_wildcard_bind_answers_loopback_names_only() -> None:
    assert allowed_hosts_for("0.0.0.0") == ("127.0.0.1", "localhost")  # noqa: S104  # input


def test_allowed_hosts_for_a_concrete_bind_adds_that_address() -> None:
    assert allowed_hosts_for("192.168.1.20") == ("127.0.0.1", "localhost", "192.168.1.20")


def test_server_refuses_an_api_url_that_is_not_http(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--api-url", "ftp://127.0.0.1:8000"])

    assert exit_info.value.code == 2
    assert "invalid --api-url" in capsys.readouterr().err


def test_is_forwardable_path_accepts_the_project_input_routes() -> None:
    assert is_forwardable_path("project/datasets/german_credit")


def test_a_dataset_upload_may_pass_the_ordinary_body_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_app, "MAX_REQUEST_BODY_BYTES", 8)
    monkeypatch.setattr(console_app, "MAX_UPLOAD_BODY_BYTES", 64)
    api = _RecordingApi()
    data = b"amount,term\n500,12\n900,24\n"

    with _console(api) as client:
        response = client.put(
            "/api/project/datasets/loans",
            params={"filename": "loans.csv"},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 200
    assert api.requests[0].content == data


def test_a_dataset_upload_above_its_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_app, "MAX_UPLOAD_BODY_BYTES", 8)
    api = _RecordingApi()

    with _console(api) as client:
        response = client.put(
            "/api/project/datasets/loans", params={"filename": "loans.csv"}, content=b"x" * 32
        )

    assert response.status_code == 413
    assert api.requests == []


def test_a_streamed_body_without_a_declared_length_is_stopped_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console_app, "MAX_REQUEST_BODY_BYTES", 8)
    api = _RecordingApi()

    def chunks() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"y" * 6

    with _console(api) as client:
        response = client.post("/api/runs", content=chunks())

    assert response.status_code == 413
    assert api.requests == []


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_the_pass_through_forwards_patch_and_delete(method: str) -> None:
    api = _RecordingApi()

    with _console(api) as client:
        response = client.request(
            method, "/api/project/datasets/loans", headers={"Origin": CONSOLE_ORIGIN}
        )

    assert response.status_code == 200
    assert api.requests[0].method == method
