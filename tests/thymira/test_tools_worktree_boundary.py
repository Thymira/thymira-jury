"""Unit tests for worktree sandbox staging and portable Git argv boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from thymira.schemas import SandboxEnforcement, SandboxMode, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.builtins.worktrees import (
    CreateWorktreeArguments,
    GitWorktreeCreate,
    GitWorktreeList,
    GitWorktreeRemove,
    WorktreeNameArguments,
)
from thymira.tools.models import ToolInvocation
from thymira.tools.sandbox import ContainerSandbox
from thymira.tools.sandbox.base import SandboxRun


@dataclass(frozen=True, slots=True)
class _CapturingSandbox:
    """Record Git argv without invoking a host Git executable."""

    commands: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str], **_kwargs: Any) -> SandboxRun:
        self.commands.append(argv)
        return SandboxRun(
            stdout="",
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )


def _invocation(tmp_path: Path) -> ToolInvocation:
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def test_worktree_create_refuses_read_only_before_staging_or_calling_the_sandbox(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()

    result = GitWorktreeCreate(sandbox=sandbox, mode=SandboxMode.READ_ONLY).execute(
        invocation,
        {
            "name": "candidate",
            "ref": "HEAD",
            "description": "Create the candidate worktree",
        },
    )

    assert not result.success
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not (invocation.workspace / ".thymira").exists()
    assert not sandbox.commands


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            GitWorktreeCreate(),
            {
                "name": "candidate",
                "ref": "HEAD",
                "description": "Create the candidate worktree",
            },
        ),
        (GitWorktreeList(), {}),
        (
            GitWorktreeRemove(),
            {"name": "candidate", "description": "Remove the candidate worktree"},
        ),
    ],
)
@pytest.mark.parametrize("mode", [SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE])
def test_worktree_tools_refuse_confined_modes_before_calling_the_sandbox(
    tmp_path: Path,
    tool: GitWorktreeCreate | GitWorktreeList | GitWorktreeRemove,
    arguments: dict[str, str],
    mode: SandboxMode,
) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()
    configured = type(tool)(sandbox=sandbox, mode=mode)

    result = configured.execute(invocation, arguments)

    assert not result.success
    assert result.exit_code == 125
    assert result.sandbox_mode is None
    assert configured.capability.sandbox_mode is mode
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not (invocation.workspace / ".thymira").exists()
    assert not sandbox.commands


@pytest.mark.parametrize(
    ("tool", "arguments", "target_index"),
    [
        (
            GitWorktreeCreate(mode=SandboxMode.DANGER_FULL_ACCESS),
            {
                "name": "candidate",
                "ref": "HEAD",
                "description": "Create the candidate worktree",
            },
            -2,
        ),
        (
            GitWorktreeRemove(mode=SandboxMode.DANGER_FULL_ACCESS),
            {"name": "candidate", "description": "Remove the candidate worktree"},
            -1,
        ),
    ],
)
def test_worktree_mutations_pass_workspace_relative_posix_targets(
    tmp_path: Path,
    tool: GitWorktreeCreate | GitWorktreeRemove,
    arguments: dict[str, str],
    target_index: int,
) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()
    configured = type(tool)(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)

    result = configured.execute(invocation, arguments)

    assert result.success
    target = sandbox.commands[0][target_index]
    assert target == ".thymira/worktrees/candidate"
    assert not Path(target).is_absolute()


@pytest.mark.parametrize("name", [".", "..", "...", "candidate."])
def test_worktree_names_reject_current_and_parent_directory_markers(name: str) -> None:
    with pytest.raises(ValidationError):
        CreateWorktreeArguments.model_validate({"name": name})
    with pytest.raises(ValidationError):
        WorktreeNameArguments.model_validate({"name": name})


@pytest.mark.parametrize("ref", ["--upload-pack=untrusted", "bad\x00ref"])
def test_worktree_create_arguments_reject_option_like_and_nul_refs(ref: str) -> None:
    with pytest.raises(ValidationError):
        CreateWorktreeArguments.model_validate({"name": "candidate", "ref": ref})


def test_worktree_create_refuses_option_like_refs_before_calling_git(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()

    result = GitWorktreeCreate(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "name": "candidate",
            "ref": "--upload-pack=untrusted",
            "description": "Attempt an unsafe worktree reference",
        },
    )

    assert not result.success
    assert not sandbox.commands


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            GitWorktreeCreate(),
            {"name": "..", "ref": "HEAD", "description": "Try a parent worktree name"},
        ),
        (
            GitWorktreeCreate(),
            {
                "name": "candidate",
                "ref": "bad\x00ref",
                "description": "Try a malformed worktree reference",
            },
        ),
        (
            GitWorktreeRemove(),
            {"name": "bad\x00name", "description": "Try a malformed worktree name"},
        ),
    ],
)
def test_worktree_direct_execute_refuses_malformed_arguments_without_side_effects(
    tmp_path: Path,
    tool: GitWorktreeCreate | GitWorktreeRemove,
    arguments: dict[str, str],
) -> None:
    invocation = _invocation(tmp_path)
    sandbox = _CapturingSandbox()
    configured = type(tool)(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)

    result = configured.execute(invocation, arguments)

    assert not result.success
    assert not (invocation.workspace / ".thymira").exists()
    assert not sandbox.commands


def test_worktree_create_refuses_container_backend_before_staging(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)

    result = GitWorktreeCreate(
        sandbox=ContainerSandbox(),
        mode=SandboxMode.DANGER_FULL_ACCESS,
    ).execute(
        invocation,
        {
            "name": "candidate",
            "ref": "HEAD",
            "description": "Create the candidate worktree",
        },
    )

    assert not result.success
    assert result.exit_code == 125
    assert result.sandbox_mode is None
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not (invocation.workspace / ".thymira").exists()


def test_worktree_backend_failure_does_not_claim_actual_mode_or_expose_exception_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = _invocation(tmp_path)

    def unavailable(*_args: Any, **_kwargs: Any) -> SandboxRun:
        raise OSError("sensitive startup detail")

    monkeypatch.setattr(_CapturingSandbox, "run", unavailable)
    result = GitWorktreeList(
        sandbox=_CapturingSandbox(), mode=SandboxMode.DANGER_FULL_ACCESS
    ).execute(invocation, {})

    assert not result.success
    assert result.sandbox_mode is None
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert "sensitive startup detail" not in (result.error or "")
