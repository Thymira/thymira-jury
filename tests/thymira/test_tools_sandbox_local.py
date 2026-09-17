"""Real local subprocess behavior under explicit sandbox modes, confined to tmp_path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.builtins.subprocess_command import with_sandbox_evidence
from thymira.tools.models import ToolResult, ToolResultCode
from thymira.tools.sandbox import LocalSubprocessSandbox
from thymira.tools.sandbox import local as local_module
from thymira.tools.sandbox.process_capture import OutputLimitExceeded

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", [SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE])
def test_local_sandbox_refuses_unsupported_confinement_before_execution(
    tmp_path: Path, mode: SandboxMode
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('escaped')"

    result = LocalSubprocessSandbox().run(
        [sys.executable, "-c", code, str(outside)],
        workspace=workspace,
        mode=mode,
        timeout_s=10,
    )

    assert not outside.exists(), "a confined request executed code outside the workspace"
    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert result.exit_code == 125
    assert result.mode is mode
    assert result.stderr == "local cannot enforce requested mode"


def test_local_sandbox_runs_explicit_danger_full_access_with_partial_enforcement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('escaped')"

    result = LocalSubprocessSandbox().run(
        [sys.executable, "-c", code, str(outside)],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert outside.read_text() == "escaped"
    assert result.exit_code == 0
    assert result.mode is SandboxMode.DANGER_FULL_ACCESS
    assert result.enforcement is SandboxEnforcement.PARTIAL


def test_child_exit_124_is_not_reported_as_a_timeout(tmp_path: Path) -> None:
    """A child-selected exit code remains an ordinary failure without deadline evidence."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    run = LocalSubprocessSandbox().run(
        [sys.executable, "-c", "raise SystemExit(124)"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=5,
    )
    result = with_sandbox_evidence(
        ToolResult(success=False, exit_code=run.exit_code, error="child exited 124"),
        run,
        timeout_s=5,
    )

    assert run.exit_code == 124
    assert run.timed_out is False
    assert result.code is not ToolResultCode.TOOL_TIMEOUT
    assert result.aborted is False


def test_local_sandbox_uses_the_current_interpreter_when_caller_path_is_scrubbed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = LocalSubprocessSandbox().run(
        ["python", "-c", "import sys; print(sys.executable)"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
        env={"PATH": str(tmp_path / "missing-bin")},
    )

    assert result.exit_code == 0
    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()


def test_local_sandbox_child_can_print_non_ascii_text(tmp_path: Path) -> None:
    """A script that prints outside Latin-1 (e.g. `<=`) must not crash the child on Windows.

    Found live (2026-09-11) driving a real EDA run: a coding agent's script printed `≤` and
    the child's own stdout, with no `PYTHONIOENCODING`/`PYTHONUTF8` in the deliberately minimal
    `clean_env`, fell back to the Windows console codepage and raised
    `UnicodeEncodeError: 'charmap' codec can't encode character '≤'` -- a crash inside the
    child process itself, not a parent-side decode issue (`process_capture._decode` already reads
    raw bytes as UTF-8 with `errors="replace"`).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = LocalSubprocessSandbox().run(
        [sys.executable, "-c", "print('≤ ≥ ± café')"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "≤ ≥ ± café"


def test_local_sandbox_leaves_non_python_commands_without_an_executable_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: dict[str, object] = {}

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(local_module, "run_bounded_process", _run)

    result = LocalSubprocessSandbox().run(
        ["git", "status"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert result.exit_code == 0
    assert captured["argv"] == ["git", "status"]
    assert captured["executable"] is None


@pytest.mark.parametrize("limit", [0, -1, True])
def test_local_sandbox_rejects_an_invalid_output_limit(limit: int) -> None:
    with pytest.raises((TypeError, ValueError), match="output_limit_bytes"):
        LocalSubprocessSandbox(output_limit_bytes=limit)


@pytest.mark.parametrize("value", ["0", "0.0", "0.0000000001", "1" * 129])
def test_local_sandbox_rejects_an_unusable_cpu_limit(value: str) -> None:
    with pytest.raises(ValueError, match="cpus"):
        LocalSubprocessSandbox(cpus=value)


def test_local_sandbox_accepts_nanocpu_precision_boundary() -> None:
    sandbox = LocalSubprocessSandbox(cpus="0.000000001")

    assert sandbox.cpus == "0.000000001"


def test_local_sandbox_refuses_a_workspace_quota_it_cannot_enforce(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = LocalSubprocessSandbox(workspace_quota_bytes=4096).run(
        [sys.executable, "-c", "print('unreachable')"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert "workspace quota" in result.stderr
    assert result.spec is not None
    assert result.spec.workspace_quota_bytes == 4096


def test_local_sandbox_reports_bounded_output_overflow_as_partial_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _overflow(*_args: object, **_kwargs: object) -> None:
        raise OutputLimitExceeded(stdout="bounded output", stderr="", limit_bytes=8)

    monkeypatch.setattr(local_module, "run_bounded_process", _overflow)

    result = LocalSubprocessSandbox(output_limit_bytes=8).run(
        ["python", "-c", "print('unreachable')"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert result.exit_code == 125
    assert result.stdout == "bounded output"
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert "output exceeded 8 bytes" in result.stderr


def test_local_sandbox_preserves_unusable_evidence_when_capture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sensitive_detail = "capture-sensitive-detail-must-not-escape"

    def _failure(*_args: object, **_kwargs: object) -> None:
        raise OSError(sensitive_detail)

    monkeypatch.setattr(local_module, "run_bounded_process", _failure)

    result = LocalSubprocessSandbox().run(
        ["python", "-c", "print('unreachable')"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
    )

    assert result.exit_code == 125
    assert result.mode is SandboxMode.DANGER_FULL_ACCESS
    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert result.stderr == "sandbox process unavailable: OSError"
    assert sensitive_detail not in result.stderr


def test_local_sandbox_rejects_an_invalid_mode_before_creating_a_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('escaped')"

    with pytest.raises(TypeError, match="sandbox mode must be a SandboxMode"):
        LocalSubprocessSandbox().run(
            [sys.executable, "-c", code, str(outside)],
            workspace=workspace,
            mode=cast("Any", "not-a-mode"),
            timeout_s=10,
        )

    assert not outside.exists()
