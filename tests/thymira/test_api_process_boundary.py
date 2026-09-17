"""`thymira-api` as a real OS process, reached over a real socket (bug-hunt H12).

Every other API test drives `create_app` in-process through `fastapi.testclient.TestClient`,
which calls the ASGI app directly and never opens a socket. That is the right tool for testing
routing and dependency wiring, but it cannot catch anything that only exists once a process
boundary is real: the console script's own argument parsing, the server actually binding a port
and staying up, a client reaching it as an external caller would. `scripts/real_e2e_smoke.py` is
the only thing in this repository that starts a real subprocess and speaks real HTTP to it, and it
is deliberately outside `just test`/`just check` because it also spends real API budget on a real
model. This test crosses the same process boundary without that cost: `/healthz` and listing Runs
never call a model, so there is nothing here for `THYMIRA_MODEL` to gate. The protected listing
requests still carry the process credential required by the API boundary.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_TIMEOUT_S = 30.0
_NONEXISTENT_RUN_ID = "run_" + "0" * 32
_PROCESS_API_TOKEN = "process-boundary-token-0123456789abcdef"  # noqa: S105 - test credential.


def _free_port() -> int:
    """Ask the OS for a currently-unused TCP port, to avoid colliding with a real dev server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_healthy(port: int, deadline: float, log_path: Path) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # local, fixed URL
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
        time.sleep(0.2)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise AssertionError(
        f"thymira-api never answered {url} in time (last error: {last_error})\n"
        f"--- server output ---\n{log}"
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Terminate the server, tolerating the transient Windows shutdown lock."""
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


_MINIMAL_CONFIG = "project:\n  name: process-boundary-test\n  domain: general\n"


@pytest.fixture
def running_server(tmp_path: Path) -> Iterator[int]:
    """Start a real `thymira-api` subprocess against a minimal workspace; yield its port."""
    port = _free_port()
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(_MINIMAL_CONFIG, encoding="utf-8")
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log_file:
        environment = os.environ.copy()
        environment["THYMIRA_API_TOKEN"] = _PROCESS_API_TOKEN
        process = subprocess.Popen(  # fixed argv, no shell, sys.executable is this venv's own
            [
                sys.executable,
                "-m",
                "thymira.api.server",
                "--workspace",
                str(workspace),
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=_REPO_ROOT,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_healthy(port, time.monotonic() + _STARTUP_TIMEOUT_S, log_path)
            yield port
        finally:
            _stop(process)


def _get(port: int, path: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {_PROCESS_API_TOKEN}"},
    )  # local, fixed URL
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310  # local, fixed URL
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_the_real_process_answers_healthz_over_a_real_socket(running_server: int) -> None:
    status, body = _get(running_server, "/healthz")

    assert status == 200
    assert body  # a real body came back over the wire, not just a status line


def test_the_real_process_lists_runs_for_an_empty_workspace(running_server: int) -> None:
    status, body = _get(running_server, "/runs")

    assert status == 200
    assert b'"items":[]' in body.replace(b" ", b"")


def test_the_real_process_404s_a_run_that_does_not_exist(running_server: int) -> None:
    status, _ = _get(running_server, f"/runs/{_NONEXISTENT_RUN_ID}")

    assert status == 404


def test_the_real_cli_consumes_the_real_api_process(running_server: int) -> None:
    """The shipped CLI and API entrypoints cross a real socket without sharing Run state."""
    environment = dict(os.environ)
    environment["THYMIRA_API_URL"] = f"http://127.0.0.1:{running_server}"
    environment["THYMIRA_API_TOKEN"] = _PROCESS_API_TOKEN
    result = subprocess.run(
        [sys.executable, "-m", "thymira.cli", "runs"],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "No runs found." in result.stdout
