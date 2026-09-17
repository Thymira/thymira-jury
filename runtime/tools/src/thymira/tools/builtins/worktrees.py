"""Run-scoped Git worktree management tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from thymira.policies import ToolCapability
from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.files import _contained
from thymira.tools.builtins.subprocess_command import with_sandbox_evidence
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import ProcessToolValue
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox, Sandbox

_CONFINED_MODE_ERROR = "worktree operations require danger_full_access"
_CONTAINER_UNSUPPORTED_ERROR = "portable Git worktrees are unavailable in container sandbox"
_INVALID_ARGUMENTS_ERROR = "invalid worktree arguments"


class CreateWorktreeArguments(BaseModel):
    """Arguments for creating a worktree."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    description: Description = DESCRIPTION_FIELD
    ref: str = "HEAD"

    @field_validator("name")
    @classmethod
    def _name_is_not_a_directory_marker(cls, value: str) -> str:
        if value.endswith("."):
            raise ValueError("name must not end with a dot")
        return value

    @field_validator("ref")
    @classmethod
    def _ref_is_not_an_option_or_nul(cls, value: str) -> str:
        if value.startswith("-"):
            raise ValueError("ref must not start with a hyphen")
        if "\x00" in value:
            raise ValueError("ref must not contain a NUL byte")
        return value


class WorktreeNameArguments(BaseModel):
    """Arguments identifying a worktree."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    description: Description = DESCRIPTION_FIELD

    @field_validator("name")
    @classmethod
    def _name_is_not_a_directory_marker(cls, value: str) -> str:
        if value.endswith("."):
            raise ValueError("name must not end with a dot")
        return value


@dataclass(frozen=True, slots=True)
class _WorktreeTool:
    """Shared worktree command helper."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    result_model: type[BaseModel] = ProcessToolValue

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SandboxMode):
            raise TypeError("worktree sandbox mode must be a SandboxMode")

    @property
    def capability(self) -> ToolCapability:
        """Return the concrete tool's capability declaration."""
        raise NotImplementedError

    def _preflight(self) -> ToolResult | None:
        """Refuse modes and backends that cannot provide portable worktree behavior."""
        if self.mode is not SandboxMode.DANGER_FULL_ACCESS:
            return ToolResult(
                success=False,
                exit_code=125,
                error=_CONFINED_MODE_ERROR,
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
        if isinstance(self.sandbox, ContainerSandbox):
            return ToolResult(
                success=False,
                exit_code=125,
                error=_CONTAINER_UNSUPPORTED_ERROR,
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
        return None

    def _validated_arguments(
        self,
        arguments: dict[str, Any],
        model: type[CreateWorktreeArguments] | type[WorktreeNameArguments],
    ) -> dict[str, str] | ToolResult:
        """Validate direct calls before they can stage a path or launch Git."""
        try:
            return model.model_validate(arguments).model_dump()
        except ValidationError:
            return ToolResult(success=False, error=_INVALID_ARGUMENTS_ERROR)

    def _run(self, invocation: ToolInvocation, argv: list[str]) -> ToolResult:
        try:
            result = self.sandbox.run(
                argv,
                workspace=invocation.workspace,
                mode=requested_sandbox_mode(self.capability),
                timeout_s=30,
            )
        except (OSError, ValueError):
            return ToolResult(
                success=False,
                error="sandbox unavailable",
                exit_code=125,
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
        return with_sandbox_evidence(
            ToolResult(
                success=result.exit_code == 0,
                value=ProcessToolValue(
                    text=result.stdout,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                ),
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                error=(
                    None if result.exit_code == 0 else (result.stderr or "worktree command failed")
                ),
            ),
            result,
            timeout_s=30,
        )


def _create_capability() -> ToolCapability:
    return ToolCapability(
        id="git_worktree_create",
        risk_tags=("code_execution",),
        data_access=("repository",),
        side_effects=("workspace_write",),
        external_effects=(),
    )


def _list_capability() -> ToolCapability:
    return ToolCapability(
        id="git_worktree_list",
        risk_tags=("code_execution",),
        data_access=("repository",),
        external_effects=(),
    )


def _remove_capability() -> ToolCapability:
    return ToolCapability(
        id="git_worktree_remove",
        risk_tags=("code_execution",),
        data_access=("repository",),
        side_effects=("workspace_write",),
        external_effects=(),
    )


@dataclass(frozen=True, slots=True)
class GitWorktreeCreate(_WorktreeTool):
    """Create an isolated run-scoped worktree."""

    name: str = "git_worktree_create"
    description: str = "Create a worktree under the run workspace."
    arguments_model: type[BaseModel] = CreateWorktreeArguments
    _capability: ToolCapability = field(default_factory=_create_capability)

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Create the named worktree."""
        if refused := self._preflight():
            return refused
        parsed = self._validated_arguments(arguments, CreateWorktreeArguments)
        if isinstance(parsed, ToolResult):
            return parsed
        target = f".thymira/worktrees/{parsed['name']}"
        _contained(Path(invocation.workspace), target)
        return self._run(
            invocation,
            # Detached worktrees allow two runs to use the same ref concurrently. A normal
            # branch checkout would fail as soon as another worktree already owns that branch.
            ["git", "worktree", "add", "--detach", target, parsed["ref"]],
        )


@dataclass(frozen=True, slots=True)
class GitWorktreeList(_WorktreeTool):
    """List repository worktrees."""

    name: str = "git_worktree_list"
    description: str = "List repository worktrees."
    arguments_model: type[BaseModel] | None = None
    _capability: ToolCapability = field(default_factory=_list_capability)
    mode: SandboxMode = SandboxMode.READ_ONLY

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return porcelain worktree records."""
        if refused := self._preflight():
            return refused
        return self._run(invocation, ["git", "worktree", "list", "--porcelain"])


@dataclass(frozen=True, slots=True)
class GitWorktreeRemove(_WorktreeTool):
    """Remove an isolated run-scoped worktree."""

    name: str = "git_worktree_remove"
    description: str = "Remove a run-scoped worktree."
    arguments_model: type[BaseModel] = WorktreeNameArguments
    _capability: ToolCapability = field(default_factory=_remove_capability)

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Remove the named contained worktree."""
        if refused := self._preflight():
            return refused
        parsed = self._validated_arguments(arguments, WorktreeNameArguments)
        if isinstance(parsed, ToolResult):
            return parsed
        target = f".thymira/worktrees/{parsed['name']}"
        _contained(Path(invocation.workspace), target)
        return self._run(invocation, ["git", "worktree", "remove", "--force", target])


__all__ = ["GitWorktreeCreate", "GitWorktreeList", "GitWorktreeRemove"]
