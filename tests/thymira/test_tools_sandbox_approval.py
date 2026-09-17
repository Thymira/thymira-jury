"""Sandbox mode is part of the Tool Manager's exact approval identity."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import Gate, PolicyEngine, RiskProfile, ToolCapability, load_policy_stack
from thymira.schemas import Actor, ActorKind, EventType, SandboxEnforcement, SandboxMode, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
)
from thymira.tools.builtins import GitStatus

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.tools.sandbox import SandboxRun


@dataclass(frozen=True, slots=True)
class _ModeTool:
    capability: ToolCapability
    reported_mode: SandboxMode | None = None
    success: bool = True
    name: str = "run_python"
    description: str = "Execute test code."
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(
            success=self.success,
            value=LegacyToolValue(text=""),
            sandbox_mode=self.reported_mode or self.capability.sandbox_mode,
            sandbox_enforcement=SandboxEnforcement.PARTIAL,
        )


def _context(tmp_path: Path) -> ToolContext:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path,
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=None),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=0.1
        ),
    )


@dataclass(frozen=True, slots=True)
class _RaisingSandbox:
    def run(self, *_args: Any, **_kwargs: Any) -> SandboxRun:
        raise OSError("git executable unavailable")


class _RequiredArguments(BaseModel):
    value: str = Field(min_length=1)


def _manager(mode: SandboxMode) -> ToolManager:
    capability = ToolCapability(id="run_python", risk_tags=("code_execution",), sandbox_mode=mode)
    return ToolManager(ToolRegistry((cast("Tool", _ModeTool(capability)),)))


def _mismatched_manager(requested: SandboxMode, reported: SandboxMode) -> ToolManager:
    capability = ToolCapability(id="echo", sandbox_mode=requested)
    tool = _ModeTool(capability, reported_mode=reported, name="echo")
    return ToolManager(ToolRegistry((cast("Tool", tool),)))


def _approve(context: ToolContext, decision_id: str) -> None:
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": True, "automatic": False},
    )


def test_workspace_approval_cannot_buy_danger_mode_after_registry_restart(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = _manager(SandboxMode.WORKSPACE_WRITE).execute(context, "run_python", {"code": "x"})
    assert first.pending_approval is not None
    _approve(context, first.pending_approval.id)

    changed = _manager(SandboxMode.DANGER_FULL_ACCESS).execute(context, "run_python", {"code": "x"})

    assert changed.pending_approval is not None
    assert changed.pending_approval.id != first.pending_approval.id


def test_same_mode_approval_executes_once_and_records_requested_mode(tmp_path: Path) -> None:
    context = _context(tmp_path)
    manager = _manager(SandboxMode.DANGER_FULL_ACCESS)
    first = manager.execute(context, "run_python", {"code": "x"})
    assert first.pending_approval is not None
    _approve(context, first.pending_approval.id)

    executed = manager.execute(context, "run_python", {"code": "x"})

    assert executed.result.success
    assert executed.call.sandbox_mode is SandboxMode.DANGER_FULL_ACCESS
    started = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_STARTED
    )
    assert started.payload["sandbox_mode"] == SandboxMode.DANGER_FULL_ACCESS.value
    repeated = manager.execute(context, "run_python", {"code": "x"})
    assert repeated.pending_approval is not None


def test_completed_call_keeps_requested_mode_when_backend_reports_different_mode(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    context = replace(
        context,
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = _mismatched_manager(
        SandboxMode.WORKSPACE_WRITE, SandboxMode.DANGER_FULL_ACCESS
    ).execute(context, "echo")

    assert execution.call.sandbox_mode is SandboxMode.WORKSPACE_WRITE
    completed = context.event_log.events()[-1]
    assert completed.payload["sandbox_mode"] == SandboxMode.DANGER_FULL_ACCESS.value
    assert completed.payload["requested_sandbox_mode"] == SandboxMode.WORKSPACE_WRITE.value


def test_failed_preflight_records_requested_mode_without_claiming_actual_mode(
    tmp_path: Path,
) -> None:
    context = replace(
        _context(tmp_path),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    tool = GitStatus(sandbox=_RaisingSandbox())

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, "git_status")

    assert execution.result.sandbox_mode is None
    assert execution.call.sandbox_mode is SandboxMode.READ_ONLY
    completed = context.event_log.events()[-1]
    assert completed.payload["sandbox_mode"] is None
    assert completed.payload["requested_sandbox_mode"] == SandboxMode.READ_ONLY.value
    report = audit_run(AuditContext(context.run_id, context.event_log.events()))
    a3 = next(control for control in report.controls if control.control_id == "A3")
    assert a3.status is ControlStatus.PASSED


def test_failed_tool_with_an_invalid_actual_mode_fails_a3(tmp_path: Path) -> None:
    context = replace(
        _context(tmp_path),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    capability = ToolCapability(id="echo", sandbox_mode=SandboxMode.READ_ONLY)
    tool = _ModeTool(
        capability,
        reported_mode=cast("SandboxMode", "forged"),  # Deliberately malformed boundary output.
        success=False,
        name="echo",
    )

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, "echo")

    assert not execution.result.success
    completed = context.event_log.events()[-1]
    assert completed.payload["sandbox_mode"] is None
    report = audit_run(AuditContext(context.run_id, context.event_log.events()))
    a3 = next(control for control in report.controls if control.control_id == "A3")
    assert a3.status is ControlStatus.FAILED
    assert completed.payload["sandbox_mode_invalid"] is True


def test_argument_validation_failure_records_requested_but_no_actual_mode(tmp_path: Path) -> None:
    context = _context(tmp_path)
    capability = ToolCapability(id="echo", sandbox_mode=SandboxMode.READ_ONLY)
    tool = _ModeTool(capability, name="echo", arguments_model=_RequiredArguments)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, "echo")

    assert execution.call.sandbox_mode is SandboxMode.READ_ONLY
    completed = context.event_log.events()[-1]
    assert completed.payload["sandbox_mode"] is None
    assert completed.payload["requested_sandbox_mode"] == SandboxMode.READ_ONLY.value
