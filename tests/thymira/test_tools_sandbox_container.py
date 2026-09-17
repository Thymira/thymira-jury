"""Real Docker boundary tests; no provider, keys or external network service is required.

Only missing Docker/image prerequisites skip. A backend execution failure after those checks
fails the test. These observed boundaries do not constitute a FULL confinement claim.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests.thymira.docker_support import require_sandbox_image
from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox import ContainerSandbox

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration
_TIMEOUT_S = 30.0


@pytest.fixture(scope="module")
def sandbox() -> ContainerSandbox:
    """Require the backend prerequisites without accepting a failed execution probe."""
    require_sandbox_image()
    return ContainerSandbox(image="thymira:dev")


def test_container_runs_nonroot_with_partial_enforcement(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = sandbox.run(
        ["python", "-c", "import os; print(os.geteuid())"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code == 0, run.stderr
    assert int(run.stdout.strip()) != 0
    assert run.mode is SandboxMode.WORKSPACE_WRITE
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_workspace_write_does_not_make_container_root_writable(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = sandbox.run(
        [
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "Path('inside.txt').write_text('inside'); "
                "Path('/etc/thymira-boundary-test').write_text('outside')"
            ),
        ],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "inside"
    assert run.exit_code != 0
    assert "PermissionError" in run.stderr or "Read-only file system" in run.stderr
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_read_only_workspace_preserves_existing_bytes(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    run = sandbox.run(
        ["python", "-c", "from pathlib import Path; Path('existing.txt').write_text('changed')"],
        workspace=tmp_path,
        mode=SandboxMode.READ_ONLY,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code != 0
    assert target.read_text(encoding="utf-8") == "original"
    assert run.mode is SandboxMode.READ_ONLY
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_network_socket_is_blocked(sandbox: ContainerSandbox, tmp_path: Path) -> None:
    run = sandbox.run(
        ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code != 0
    assert "unreachable" in run.stderr.lower()
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_memory_limit_kills_overallocating_process(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = ContainerSandbox(image=sandbox.image, memory="64m").run(
        ["python", "-c", "b = bytearray(400 * 1024 * 1024); print('ALLOCATED', len(b))"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code == 137, run.stderr
    assert "ALLOCATED" not in run.stdout
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_child_failure_is_distinct_from_backend_refusal(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = sandbox.run(
        ["python", "-c", "raise SystemExit(125)"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code == 125
    assert run.enforcement is SandboxEnforcement.PARTIAL


def test_missing_image_refuses_without_host_fallback(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = ContainerSandbox(image="thymira-container-test-missing:never-pull").run(
        ["python", "-c", "from pathlib import Path; Path('escaped').touch()"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code == 125
    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert not (tmp_path / "escaped").exists()


def test_unreachable_daemon_refuses_without_changing_the_live_service(
    sandbox: ContainerSandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Override only this client's endpoint; do not stop the shared Docker daemon.
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:0")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    run = sandbox.run(
        ["python", "-c", "from pathlib import Path; Path('escaped').touch()"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5.0,
    )

    assert run.exit_code == 125
    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert not (tmp_path / "escaped").exists()


def test_missing_container_command_is_not_a_started_child(
    sandbox: ContainerSandbox, tmp_path: Path
) -> None:
    run = sandbox.run(
        ["thymira-test-command-that-does-not-exist"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=_TIMEOUT_S,
    )

    assert run.exit_code == 125
    assert run.enforcement is SandboxEnforcement.UNUSABLE


def test_timeout_removes_its_container(sandbox: ContainerSandbox, tmp_path: Path) -> None:
    marker = "thymira_timeout_" + tmp_path.name
    run = sandbox.run(
        ["python", "-c", "import time; time.sleep(60)", marker],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=2.0,
    )

    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert run.exit_code == 125
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "name=thymira-sandbox-",
            "--format",
            "{{json .Command}}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    commands = [json.loads(line) for line in result.stdout.splitlines()]
    assert all(marker not in command for command in commands)
