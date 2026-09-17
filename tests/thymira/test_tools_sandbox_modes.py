"""Public subprocess-tool mode declarations and local fail-closed behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import (
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    load_policy_stack,
)
from thymira.schemas import Decision, ExecutionConstraints, SandboxEnforcement, SandboxMode, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import Tool, ToolContext, ToolManager
from thymira.tools.builtins import builtins_registry, configured_builtins_registry
from thymira.tools.builtins.run_python import RunPython, RunPythonArguments
from thymira.tools.models import ToolInvocation
from thymira.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_SUBPROCESS_TOOLS = frozenset(
    {
        "run_notebook",
        "run_python",
        "run_experiment",
        "inspect_model",
        "audit_model",
        "git_status",
        "git_diff",
        "git_log",
        "git_commit",
        "git_worktree_create",
        "git_worktree_list",
        "git_worktree_remove",
    }
)


def test_registry_preserves_safe_modes_and_declares_each_subprocess_mode() -> None:
    registry = builtins_registry()
    modes = {tool.name: tool.capability.sandbox_mode for tool in registry}

    assert {name for name, mode in modes.items() if mode is not None} == _SUBPROCESS_TOOLS
    assert modes["git_worktree_list"] is SandboxMode.READ_ONLY
    assert all(
        modes[name] is SandboxMode.WORKSPACE_WRITE
        for name in _SUBPROCESS_TOOLS
        - {
            "git_status",
            "git_diff",
            "git_log",
            "git_worktree_list",
        }
    )
    assert all(
        modes[name] is SandboxMode.READ_ONLY
        for name in ("git_status", "git_diff", "git_log", "git_worktree_list")
    )


def test_registry_danger_override_declares_unconfined_host_effects() -> None:
    registry = builtins_registry(subprocess_mode=SandboxMode.DANGER_FULL_ACCESS)

    for tool in registry:
        if tool.name not in _SUBPROCESS_TOOLS:
            continue
        assert tool.capability.sandbox_mode is SandboxMode.DANGER_FULL_ACCESS
        assert "unconfined_code" in tool.capability.risk_tags
        assert {"filesystem", "network"}.issubset(tool.capability.external_effects)


def test_registry_rejects_an_invalid_subprocess_mode() -> None:
    with pytest.raises(TypeError, match="subprocess_mode must be a SandboxMode"):
        builtins_registry(subprocess_mode=cast("Any", "not-a-mode"))


def test_run_python_arguments_refuse_a_model_requested_sandbox_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        RunPythonArguments.model_validate(
            {
                "code": "print('unreachable')",
                "description": "Attempt to override sandbox mode.",
                "mode": SandboxMode.DANGER_FULL_ACCESS,
            }
        )


def test_base_policy_blocks_a_danger_registry_before_run_python_executes(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    manager = ToolManager(builtins_registry(subprocess_mode=SandboxMode.DANGER_FULL_ACCESS))

    execution = manager.execute(
        context,
        "run_python",
        {"code": "raise AssertionError('must not execute')", "description": "Verify policy."},
    )

    assert not execution.result.success
    assert "external effects" in (execution.result.error or "")
    assert not context.workspace.exists()


def test_default_run_python_records_an_unusable_local_sandbox(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        # This test intentionally bypasses the base governance review so it can exercise the
        # sandbox backend's own fail-closed response. The production policy now reviews local
        # side effects before a tool is allowed to reach the backend.
        gate=Gate(
            PolicyEngine(
                Policy(
                    name="sandbox-test",
                    version="1.0",
                    capability_rules=(
                        CapabilityRule(
                            id="ALLOW-SANDBOX-WRITE",
                            decision=Decision.PASS,
                            reason="sandbox backend test",
                            external_effects=(),
                            side_effects=("workspace_write",),
                            execution_constraints=ExecutionConstraints(local_execution_only=True),
                        ),
                    ),
                )
            ),
            log,
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    manager = ToolManager(ToolRegistry((cast("Tool", RunPython()),)))

    execution = manager.execute(
        context,
        "run_python",
        {"code": "print('must not execute')", "description": "Verify local refusal."},
    )

    assert not execution.result.success
    assert execution.result.exit_code == 125
    assert execution.result.sandbox_mode is SandboxMode.WORKSPACE_WRITE
    assert execution.result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert execution.call.sandbox_mode is SandboxMode.WORKSPACE_WRITE


def test_configured_local_safe_mode_fails_closed_without_host_fallback(tmp_path: Path) -> None:
    """The production composition root never silently runs a safe request on the host."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    registry = configured_builtins_registry(
        source={"THYMIRA_SANDBOX_BACKEND": "local", "THYMIRA_SANDBOX_MODE": "read_only"}
    )

    execution = ToolManager(registry).execute(context, "git_status", {"porcelain": True})

    assert not execution.result.success
    assert execution.result.exit_code == 125
    assert execution.result.sandbox_mode is SandboxMode.READ_ONLY


def test_run_python_success_does_not_depend_on_empty_stderr(tmp_path: Path) -> None:
    """A zero exit is a success even when the child printed warnings to stderr (F6.5)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    invocation = ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )
    tool = RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)

    result = tool.execute(
        invocation,
        {
            "code": "import sys; sys.stderr.write('a harmless warning\\n'); print('ok')",
            "description": "Exit zero with non-empty stderr.",
        },
    )

    assert result.exit_code == 0
    assert result.stderr.strip() == "a harmless warning"
    assert result.success
