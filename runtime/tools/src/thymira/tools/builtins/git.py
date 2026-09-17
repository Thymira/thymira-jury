"""Git tools executed through the sandbox boundary."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from thymira.policies import ToolCapability
from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.subprocess_command import with_sandbox_evidence
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import ProcessToolValue
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox, Sandbox

if TYPE_CHECKING:
    from pathlib import Path

_CONFINED_REPOSITORY_ERROR = "confined Git requires a standalone repository under the workspace"
_CONTAINER_GIT_PREFIX = ["git", "-c", "safe.directory=/workspace"]
_CONTAINER_GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


@dataclass(frozen=True, slots=True)
class _GitTool:
    """Shared implementation for Git subprocess calls."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.READ_ONLY
    result_model: type[BaseModel] = ProcessToolValue

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SandboxMode):
            raise TypeError("Git sandbox mode must be a SandboxMode")

    @property
    def capability(self) -> ToolCapability:
        """Return the concrete tool's capability declaration."""
        raise NotImplementedError

    def _run(self, invocation: ToolInvocation, argv: list[str]) -> ToolResult:
        """Run Git in the workspace and translate failures into a result."""
        mode = requested_sandbox_mode(self.capability)
        if mode is not SandboxMode.DANGER_FULL_ACCESS and not _standalone_repository(
            invocation.workspace
        ):
            return ToolResult(
                success=False,
                exit_code=125,
                error=_CONFINED_REPOSITORY_ERROR,
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
        env: dict[str, str] | None = None
        if isinstance(self.sandbox, ContainerSandbox):
            argv = [*_CONTAINER_GIT_PREFIX, *argv[1:]]
            env = dict(_CONTAINER_GIT_ENV)
            if mode is SandboxMode.READ_ONLY:
                env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            run = self.sandbox.run(
                argv,
                workspace=invocation.workspace,
                mode=mode,
                timeout_s=30,
                env=env,
            )
        except (OSError, ValueError):
            return ToolResult(
                success=False,
                exit_code=125,
                error="Git sandbox unavailable",
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
        return with_sandbox_evidence(
            ToolResult(
                success=run.exit_code == 0,
                value=ProcessToolValue(
                    text=run.stdout,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                ),
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                error=None if run.exit_code == 0 else (run.stderr or "git command failed"),
            ),
            run,
            timeout_s=30,
        )


class GitStatusArguments(BaseModel):
    """Select the status output format.

    ``porcelain`` (the default) asks for Git's stable, machine-readable ``--porcelain=v1`` format,
    which is what an agent parsing the result wants; set it false for Git's human-readable long
    format.
    """

    model_config = ConfigDict(extra="forbid")

    porcelain: bool = True


def _status_capability() -> ToolCapability:
    return ToolCapability(
        id="git_status",
        risk_tags=("code_execution",),
        data_access=("repository",),
        external_effects=(),
    )


def _diff_capability() -> ToolCapability:
    return ToolCapability(
        id="git_diff",
        risk_tags=("code_execution",),
        data_access=("repository",),
        external_effects=(),
    )


def _log_capability() -> ToolCapability:
    return ToolCapability(
        id="git_log",
        risk_tags=("code_execution",),
        data_access=("repository",),
        external_effects=(),
    )


def _commit_capability() -> ToolCapability:
    return ToolCapability(
        id="git_commit",
        risk_tags=("code_execution",),
        data_access=("repository",),
        side_effects=("workspace_write",),
        external_effects=(),
    )


@dataclass(frozen=True, slots=True)
class GitStatus(_GitTool):
    """Show the repository status."""

    name: str = "git_status"
    description: str = "Show the repository status."
    arguments_model: type[BaseModel] = GitStatusArguments
    _capability: ToolCapability = field(default_factory=_status_capability)

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Execute git status, honouring the ``porcelain`` argument.

        The published ``porcelain`` field selects the format: true (the default) returns the
        stable ``--porcelain=v1`` output an agent can parse, false returns Git's long format.
        """
        if arguments.get("porcelain", True):
            return self._run(invocation, ["git", "status", "--porcelain=v1"])
        return self._run(invocation, ["git", "status"])


@dataclass(frozen=True, slots=True)
class GitDiff(_GitTool):
    """Show the repository diff."""

    name: str = "git_diff"
    description: str = "Show the repository diff."
    arguments_model: type[BaseModel] | None = None
    _capability: ToolCapability = field(default_factory=_diff_capability)

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Execute git diff."""
        return self._run(_invocation, ["git", "diff", "--no-ext-diff"])


@dataclass(frozen=True, slots=True)
class GitLog(_GitTool):
    """Show recent repository commits."""

    name: str = "git_log"
    description: str = "Show recent repository commits."
    arguments_model: type[BaseModel] | None = None
    _capability: ToolCapability = field(default_factory=_log_capability)

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Execute git log."""
        return self._run(_invocation, ["git", "log", "--oneline", "-20"])


class GitCommitArguments(BaseModel):
    """Commit message and literal file or directory paths; empty paths select the workspace."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")
    description: Description = DESCRIPTION_FIELD
    paths: tuple[Annotated[str, Field(pattern=r"^[^\x00]*$")], ...] = ()


def _standalone_repository(workspace: Path) -> bool:
    """Return whether ``workspace/.git`` is a contained, non-link directory."""
    root = workspace.resolve()
    git_directory = root / ".git"
    try:
        entry = git_directory.lstat()
        resolved = git_directory.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(entry, "st_file_attributes", 0)
    return (
        stat.S_ISDIR(entry.st_mode)
        and not git_directory.is_symlink()
        and not (reparse_point and attributes & reparse_point)
    )


@dataclass(frozen=True, slots=True)
class GitCommit(_GitTool):
    """Create a local Git commit for selected paths."""

    name: str = "git_commit"
    description: str = "Create a local Git commit for selected paths."
    arguments_model: type[BaseModel] = GitCommitArguments
    _capability: ToolCapability = field(default_factory=_commit_capability)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Stage selected paths and create a commit."""
        add = ["git", "--literal-pathspecs", "add", "--", *arguments.get("paths", ())]
        if not arguments.get("paths"):
            add = ["git", "add", "-A"]
        staged = self._run(invocation, add)
        if not staged.success:
            return staged
        commit = ["git", "--literal-pathspecs", "commit", "-m", arguments["message"]]
        if paths := arguments.get("paths"):
            commit.extend(["--only", "--", *paths])
        return self._run(invocation, commit)


__all__ = ["GitCommit", "GitDiff", "GitLog", "GitStatus"]
