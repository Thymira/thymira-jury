"""Real child output limits through local development and Docker sandbox backends.

Processes and pipes are real; no LLM, credentials or remote service is involved. Docker tests
skip only when the daemon or built image is absent. All workspace writes stay under tmp_path.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import development_policy, human_approved_context
from thymira.events import verify_events
from thymira.policies import PolicyEngine, RiskProfile
from thymira.schemas import SandboxEnforcement, SandboxMode, ToolCallStatus, new_id
from thymira.tools import Tool, ToolManager, ToolRegistry
from thymira.tools.builtins import RunPython
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox, Sandbox
from thymira.tools.sandbox import container as container_module

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _backend(backend: str, *, limit: int | None = None) -> tuple[Sandbox, SandboxMode]:
    if backend == "container":
        require_sandbox_image()
        sandbox = (
            ContainerSandbox() if limit is None else ContainerSandbox(output_limit_bytes=limit)
        )
        return sandbox, SandboxMode.WORKSPACE_WRITE
    local = (
        LocalSubprocessSandbox()
        if limit is None
        else LocalSubprocessSandbox(output_limit_bytes=limit)
    )
    return local, SandboxMode.DANGER_FULL_ACCESS


@pytest.mark.parametrize("backend", ["local", "container"])
def test_sandbox_refuses_child_output_above_its_capture_budget(
    tmp_path: Path, backend: str
) -> None:
    sandbox, mode = _backend(backend)

    run = sandbox.run(
        ["python", "-c", "import sys; sys.stdout.write('x' * (9 * 1024 * 1024))"],
        workspace=tmp_path,
        mode=mode,
        timeout_s=30,
    )

    exit_code = run.exit_code
    assert exit_code != 0, "child output exceeding the capture budget was accepted"
    assert "output" in run.stderr.lower()
    assert len(run.stdout.encode()) <= 8 * 1024 * 1024


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize(
    "code",
    [
        "import os; os.write(1, b'x' * 4096)",
        "import os; os.write(2, b'x' * 4096)",
        "import os; os.write(1, b'x' * 768); os.write(2, b'y' * 768)",
    ],
    ids=["stdout", "stderr", "combined"],
)
def test_sandbox_enforces_one_budget_across_both_streams(
    tmp_path: Path, backend: str, code: str
) -> None:
    sandbox, mode = _backend(backend, limit=1024)

    run = sandbox.run(["python", "-c", code], workspace=tmp_path, mode=mode, timeout_s=30)

    assert run.exit_code == 125
    assert "output" in run.stderr.lower()
    # Runtime-generated failure diagnostics are separate from the captured raw-byte budget.
    assert len(run.stdout.encode()) + len(run.stderr.encode()) <= 1024 + 256


@pytest.mark.parametrize("backend", ["local", "container"])
def test_sandbox_accepts_exact_byte_budget_and_preserves_unicode(
    tmp_path: Path, backend: str
) -> None:
    sandbox, mode = _backend(backend, limit=1024)
    code = "import os; os.write(1, bytes([195, 169]) * 256); os.write(2, b'e' * 512)"

    run = sandbox.run(["python", "-c", code], workspace=tmp_path, mode=mode, timeout_s=30)

    assert run.exit_code == 0, run.stderr
    assert run.stdout == "é" * 256
    assert run.stderr == "e" * 512
    assert run.enforcement is SandboxEnforcement.PARTIAL


@pytest.mark.parametrize("backend", ["local", "container"])
def test_sandbox_preserves_ordinary_failure_output_and_newlines(
    tmp_path: Path, backend: str
) -> None:
    sandbox, mode = _backend(backend, limit=1024)
    code = (
        "import os; os.write(1, b'hello\\r\\n'); os.write(2, b'failure\\r\\n'); raise SystemExit(7)"
    )

    run = sandbox.run(["python", "-c", code], workspace=tmp_path, mode=mode, timeout_s=30)

    assert run.exit_code == 7
    assert run.stdout == "hello\n"
    assert run.stderr == "failure\n"
    assert run.enforcement is SandboxEnforcement.PARTIAL


@pytest.mark.parametrize("backend", ["local", "container"])
def test_output_failure_remains_verifiable_through_tool_manager(
    tmp_path: Path, backend: str
) -> None:
    sandbox, mode = _backend(backend, limit=32768)
    engine = PolicyEngine(development_policy()) if backend == "local" else None
    context = replace(
        human_approved_context(tmp_path, run_id=new_id("run"), engine=engine),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    manager = ToolManager(ToolRegistry((cast("Tool", RunPython(sandbox=sandbox, mode=mode)),)))

    execution = manager.execute(
        context,
        "run_python",
        {"code": "print('x' * 131072)", "description": "Exercise the output capture budget"},
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.sandbox_mode is mode
    expected = SandboxEnforcement.UNUSABLE if backend == "container" else SandboxEnforcement.PARTIAL
    assert execution.call.sandbox_enforcement is expected
    assert verify_events(context.event_log.events()).valid
    assert context.artifact_store.verify() == []


def test_container_output_failure_removes_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = uuid4()
    monkeypatch.setattr(container_module, "uuid4", lambda: identity)
    sandbox, mode = _backend("container", limit=1024)

    run = sandbox.run(
        ["python", "-c", "import os; os.write(1, b'x' * 4096)"],
        workspace=tmp_path,
        mode=mode,
        timeout_s=30,
    )
    remaining = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"name=^/thymira-sandbox-{identity.hex}$",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )

    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert run.exit_code == 125
    assert remaining.stdout == ""


def test_local_inherited_output_pipes_cannot_extend_the_execution_deadline(tmp_path: Path) -> None:
    stop = tmp_path / "stop-child"
    child = (
        "import sys,time; from pathlib import Path; "
        "stop=Path(sys.argv[1]); deadline=time.monotonic()+30; "
        "exec('while not stop.exists() and time.monotonic()<deadline: time.sleep(0.05)')"
    )
    parent = (
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
    )
    started = time.monotonic()
    try:
        run = LocalSubprocessSandbox().run(
            [sys.executable, "-c", parent, child, str(stop)],
            workspace=tmp_path,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            timeout_s=1,
        )
    finally:
        stop.touch()

    assert run.exit_code == 124
    assert run.timed_out is True
    assert time.monotonic() - started < 10
    assert "timeout" in run.stderr


@pytest.mark.parametrize("backend", ["local", "container"])
def test_child_closing_output_can_still_finish_within_its_deadline(
    tmp_path: Path, backend: str
) -> None:
    sandbox, mode = _backend(backend)
    code = (
        "import os,time; from pathlib import Path; "
        "os.close(1); os.close(2); time.sleep(2); Path('finished').touch()"
    )

    run = sandbox.run(["python", "-c", code], workspace=tmp_path, mode=mode, timeout_s=10)

    assert run.exit_code == 0, run.stderr
    assert (tmp_path / "finished").exists()


def test_container_executes_the_linux_capture_implementation(tmp_path: Path) -> None:
    sandbox, mode = _backend("container")
    code = """
import os
import sys
from pathlib import Path
from thymira.tools.sandbox.process_capture import OutputLimitExceeded, run_bounded_process
assert os.name == 'posix'
try:
    run_bounded_process(
        [sys.executable, '-c', "import os; os.write(1, b'x' * (9 * 1024 * 1024))"],
        cwd=Path.cwd(), timeout_s=10, output_limit_bytes=4096,
    )
except OutputLimitExceeded as error:
    assert len(error.stdout.encode()) + len(error.stderr.encode()) == 4096
    print('Linux capture budget enforced')
else:
    raise AssertionError('Linux capture accepted oversized output')
"""

    run = sandbox.run(["python", "-c", code], workspace=tmp_path, mode=mode, timeout_s=30)

    assert run.exit_code == 0, run.stderr
    assert run.stdout == "Linux capture budget enforced\n"
