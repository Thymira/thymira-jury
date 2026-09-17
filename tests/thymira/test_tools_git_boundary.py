"""Unit regressions for Git repository and sandbox boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError

from thymira.schemas import SandboxEnforcement, SandboxMode, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.builtins.git import GitCommit, GitCommitArguments, GitDiff, GitLog, GitStatus
from thymira.tools.builtins.worktrees import GitWorktreeList
from thymira.tools.models import ToolInvocation
from thymira.tools.sandbox import ContainerSandbox
from thymira.tools.sandbox.base import SandboxRun

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _CapturingSandbox:
    """Record sandbox inputs while returning a confirmed child result."""

    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def run(self, argv: list[str], **kwargs: Any) -> SandboxRun:
        self.calls.append((argv, kwargs))
        return SandboxRun(
            stdout="ok",
            stderr="",
            exit_code=0,
            mode=kwargs["mode"],
            enforcement=SandboxEnforcement.PARTIAL,
        )


class _CapturingContainerSandbox(ContainerSandbox):
    """Represent the container backend without starting Docker."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(self, argv: list[str], **kwargs: Any) -> SandboxRun:
        self.calls.append((argv, kwargs))
        return SandboxRun(
            stdout="ok",
            stderr="",
            exit_code=0,
            mode=kwargs["mode"],
            enforcement=SandboxEnforcement.PARTIAL,
        )


def _invocation(tmp_path: Path, *, git_directory: bool = True) -> ToolInvocation:
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if git_directory:
        (workspace / ".git").mkdir()
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


@pytest.mark.parametrize("tool", [GitStatus(), GitDiff(), GitLog(), GitCommit()])
def test_every_git_tool_declares_repository_code_execution(
    tool: GitStatus | GitDiff | GitLog | GitCommit,
) -> None:
    assert "code_execution" in tool.capability.risk_tags


@pytest.mark.parametrize("mode", [SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE])
@pytest.mark.parametrize("git_entry", ["missing", "file"])
def test_confined_git_refuses_non_standalone_repository_before_backend(
    tmp_path: Path, mode: SandboxMode, git_entry: str
) -> None:
    invocation = _invocation(tmp_path, git_directory=False)
    if git_entry == "file":
        (invocation.workspace / ".git").write_text("gitdir: ../outside\n", encoding="utf-8")
    sandbox = _CapturingSandbox()

    result = GitStatus(sandbox=sandbox, mode=mode).execute(invocation, {"porcelain": True})

    assert not result.success
    assert result.exit_code == 125
    assert result.sandbox_mode is None
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert result.error == "confined Git requires a standalone repository under the workspace"
    assert not sandbox.calls


def test_confined_git_uses_container_workspace_and_isolated_read_environment(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingContainerSandbox()

    result = GitDiff(sandbox=sandbox, mode=SandboxMode.READ_ONLY).execute(invocation, {})

    assert result.success
    argv, options = sandbox.calls[0]
    assert argv[:3] == ["git", "-c", "safe.directory=/workspace"]
    assert argv[3:] == ["diff", "--no-ext-diff"]
    assert options["env"] == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_explicit_unconfined_git_retains_linked_repository_and_host_argv(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, git_directory=False)
    (invocation.workspace / ".git").write_text("gitdir: ../shared\n", encoding="utf-8")
    sandbox = _CapturingSandbox()

    result = GitStatus(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation, {"porcelain": True}
    )

    assert result.success
    argv, options = sandbox.calls[0]
    assert argv == ["git", "status", "--porcelain=v1"]
    assert options["env"] is None


def test_git_commit_rejects_nul_in_path() -> None:
    with pytest.raises(ValidationError):
        GitCommitArguments.model_validate({"message": "safe", "paths": ["safe\x00unsafe"]})


def test_git_commit_rejects_nul_in_message_before_staging() -> None:
    with pytest.raises(ValidationError):
        GitCommitArguments.model_validate({"message": "invalid\x00message"})


@pytest.mark.parametrize("factory", [GitStatus, GitCommit, GitWorktreeList])
def test_git_tool_rejects_invalid_mode_before_registration(factory: type) -> None:
    with pytest.raises(TypeError, match="SandboxMode"):
        factory(mode=cast("SandboxMode", "read-only"))


def test_git_commit_keeps_user_paths_after_option_terminator(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()

    result = GitCommit(sandbox=sandbox, mode=SandboxMode.WORKSPACE_WRITE).execute(
        invocation, {"message": "record change", "paths": ["--all", "../outside"]}
    )

    assert result.success
    assert sandbox.calls[0][0][-3:] == ["--", "--all", "../outside"]
