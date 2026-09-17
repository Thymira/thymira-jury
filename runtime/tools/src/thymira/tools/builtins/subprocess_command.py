"""Portable subprocess commands and staging refusals for workspace-backed tools."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.models import ToolResult, ToolResultCode

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.tools.sandbox.base import SandboxRun, StagedInput


PYTHON_BOOTSTRAP_FLAGS: tuple[str, ...] = ("-I", "-u", "-B")
"""How every Python child this runtime starts is bootstrapped, whatever the ambient environment.

``-I`` is isolated mode: no user site directory, no ``PYTHON*`` variables honoured, and the
script's own directory is not prepended to ``sys.path``. It makes the interpreter's behaviour a
property of this argv rather than of whatever the host or the image happened to export. ``-u``
makes both streams unbuffered, so output that has been produced has actually been written when a
deadline or a limit ends the child. ``-B`` writes no ``__pycache__`` into the workspace, which
would otherwise leave runtime droppings inside evidence the audit reads.
"""


_MAX_PATH_DEPTH = 64
"""How far below the workspace a staged path may sit before it is refused unvalidated."""


def _is_link(path: Path) -> bool:
    """Return whether one component is link-shaped, in either of the two Windows shapes."""
    return path.is_symlink() or path.is_junction()


def assert_no_link_components(workspace: Path, path: Path) -> None:
    """Refuse a path any of whose components below the workspace is a link.

    Resolution alone already keeps a staged write inside the workspace -- a component that
    redirects outside makes ``relative_to`` fail. This is the narrower, explicit statement of the
    property the criterion names (F6.8): a symlink, junction or reparse point planted inside the
    workspace is refused *as a link*, before anything is written through it, rather than being
    caught incidentally by where it happened to point. The refusal names the component, so a
    caller can produce a fail-closed result instead of an unexpected error.
    """
    root = workspace.resolve()
    absolute = workspace.absolute()
    current = path.absolute()
    for _ in range(_MAX_PATH_DEPTH):
        if current in {root, absolute} or current == current.parent:
            return
        if _is_link(current):
            raise ValueError(f"subprocess path must not traverse a link: {current.name}")
        current = current.parent
    raise ValueError("subprocess path is too deeply nested to validate")


def python_workspace_command(workspace: Path, script: Path) -> list[str]:
    """Build a Python command whose script is a workspace-relative POSIX path."""
    root = workspace.resolve()
    assert_no_link_components(workspace, script)
    try:
        relative = script.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("subprocess script must be contained in the workspace") from exc
    return ["python", *PYTHON_BOOTSTRAP_FLAGS, relative.as_posix()]


def python_module_command(module: str, *arguments: str) -> list[str]:
    """Build a ``python -m`` command bootstrapped exactly like a staged script.

    Used by the one tool that runs an installed worker module instead of staging a file, so both
    shapes of Python child get the same interpreter.
    """
    return ["python", *PYTHON_BOOTSTRAP_FLAGS, "-m", module, *arguments]


def workspace_relative_path(workspace: Path, path: Path) -> str:
    """Return a contained path relative to ``workspace`` in portable POSIX form."""
    root = workspace.resolve()
    assert_no_link_components(workspace, path)
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("subprocess path must be contained in the workspace") from exc


def link_shaped_refusal(mode: SandboxMode, workspace: Path, *paths: Path) -> ToolResult | None:
    """Refuse, fail-closed, staging paths whose components include a link.

    Shaped like :func:`read_only_staging_refusal` so an owned call site produces a recorded
    refusal (exit 125, ``UNUSABLE``) rather than the unexpected-error shape a raised
    :class:`ValueError` would reach the manager as.
    """
    for path in paths:
        try:
            assert_no_link_components(workspace, path)
        except ValueError as exc:
            return ToolResult(
                success=False,
                exit_code=125,
                error=str(exc),
                sandbox_mode=mode,
                sandbox_enforcement=SandboxEnforcement.UNUSABLE,
            )
    return None


def read_only_staging_refusal(mode: SandboxMode) -> ToolResult | None:
    """Refuse file staging a read-only sandbox cannot make available to a subprocess."""
    if mode is SandboxMode.READ_ONLY:
        return ToolResult(
            success=False,
            exit_code=125,
            error="read-only sandbox cannot stage subprocess files",
            sandbox_mode=mode,
            sandbox_enforcement=SandboxEnforcement.UNUSABLE,
        )
    return None


def with_sandbox_evidence(
    result: ToolResult, run: SandboxRun, *, timeout_s: float | None = None
) -> ToolResult:
    """Copy one sandbox execution's confinement facts onto a result.

    The single seam every code-executing tool routes its ``ToolResult`` through: mode,
    enforcement, the resolved specification, the cleanup result, the termination evidence and
    the sandbox-owned timeout fact travel together so a tool cannot report one without the
    others (F6.5/F6.8).
    """
    timed_out = run.timed_out
    success = result.success and run.tool_succeeded
    error = result.error
    if result.success and not run.tool_succeeded and error is None:
        error = "sandbox execution or workspace writeback was not confirmed"
    return replace(
        result,
        success=success,
        error=error,
        code=ToolResultCode.TOOL_TIMEOUT if timed_out else result.code,
        timeout_s=timeout_s if timed_out else result.timeout_s,
        aborted=result.aborted or timed_out,
        sandbox_mode=run.mode,
        sandbox_enforcement=run.enforcement,
        sandbox_spec=run.spec,
        sandbox_cleanup_confirmed=run.cleanup_confirmed,
        sandbox_termination=run.termination,
        quota_evidence=run.quota_evidence,
    )


def ensure_workspace_root(workspace: Path) -> None:
    """Create the workspace root a backend is about to bind, snapshot or run the child in.

    Staging the *contents* is the backend's job -- `StagedInput` bytes are charged and written by
    the sandbox, never pre-written here, so a quota refusal leaves an empty host workspace. The
    root directory itself is not an input: it carries no runtime bytes and no entries, every
    backend refuses an absent workspace (`sandbox workspace does not exist`), and the quota
    helper snapshots it as the source tree. Creating it is therefore the caller's responsibility
    and the one host effect that survived the move to backend staging.
    """
    workspace.mkdir(parents=True, exist_ok=True)


def persist_for_uninstrumented_backend(workspace: Path, inputs: tuple[StagedInput, ...]) -> None:
    """Keep compatibility with instrumentation-only sandbox doubles lacking a resolved spec.

    Every production backend returns a resolved specification and stages through its own run
    implementation.  A small number of legacy test doubles only capture argv; preserving their
    old observable files keeps those tests useful without allowing a real quota backend onto this
    path.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    for item in inputs:
        target = workspace.joinpath(*item.relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)


__all__ = [
    "PYTHON_BOOTSTRAP_FLAGS",
    "assert_no_link_components",
    "ensure_workspace_root",
    "link_shaped_refusal",
    "persist_for_uninstrumented_backend",
    "python_module_command",
    "python_workspace_command",
    "read_only_staging_refusal",
    "with_sandbox_evidence",
    "workspace_relative_path",
]
