"""MIRA independently verifies sandbox-bound tool approval evidence."""

from __future__ import annotations

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import Gate, PolicyEngine, RiskProfile, ToolCapability, load_policy_stack
from thymira.schemas import Actor, ActorKind, EventType, SandboxMode, ToolCallStatus, new_id
from thymira.tools import tool_intent_sha256


def _control(log: InMemoryEventLog, control_id: str):
    report = audit_run(AuditContext(log.run_id, log.events()))
    return next(control for control in report.controls if control.control_id == control_id)


def _passing_execution(
    requested: SandboxMode | None,
    reported: object,
    *,
    ticket_mode: SandboxMode | None = None,
    completed_status: ToolCallStatus | None = None,
    repeated_request: object | None = None,
) -> InMemoryEventLog:
    log = InMemoryEventLog(new_id("run"))
    subject = new_id("tool")
    arguments = {"value": "x"}
    ticket = tool_intent_sha256("echo", arguments, sandbox_mode=ticket_mode or requested)
    decision = Gate(PolicyEngine(load_policy_stack()), log).check_capability(
        subject_id=subject,
        capability=ToolCapability(id="echo", sandbox_mode=requested),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0),
    )
    log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": "echo",
            "arguments": arguments,
            "decision_id": decision.id,
            "tool_intent_sha256": ticket,
            "sandbox_mode": requested,
        },
        subject_id=subject,
    )
    completed_payload = {"sandbox_mode": reported}
    if completed_status is not None:
        completed_payload["status"] = completed_status
    if repeated_request is not None:
        completed_payload["requested_sandbox_mode"] = repeated_request
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        completed_payload,
        subject_id=subject,
    )
    return log


def _approved_start(
    mode: SandboxMode,
    *,
    request_mode: object | None = None,
    start_mode: object | None = None,
):
    log = InMemoryEventLog(new_id("run"))
    subject = new_id("tool")
    arguments = {"code": "x"}
    ticket = tool_intent_sha256("run_python", arguments, sandbox_mode=mode)
    decision = Gate(PolicyEngine(load_policy_stack()), log, approver=None).check_capability(
        subject_id=subject,
        capability=ToolCapability(
            id="run_python", risk_tags=("code_execution",), sandbox_mode=mode
        ),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
        details={
            "tool": "run_python",
            "arguments": arguments,
            "tool_intent_sha256": ticket,
            "sandbox_mode": mode if request_mode is None else request_mode,
        },
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision.id, "approved": True, "automatic": False},
    )
    start = log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {
            "tool": "run_python",
            "arguments": arguments,
            "decision_id": decision.id,
            "tool_intent_sha256": ticket,
            "sandbox_mode": mode if start_mode is None else start_mode,
        },
        subject_id=new_id("tool"),
    )
    return log, start


def test_a3_rejects_reported_mode_different_from_requested_mode() -> None:
    log, start = _approved_start(SandboxMode.WORKSPACE_WRITE)
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"sandbox_mode": SandboxMode.DANGER_FULL_ACCESS},
        subject_id=start.subject_id,
    )

    assert _control(log, "A3").status is ControlStatus.FAILED


def test_a3_accepts_same_requested_and_reported_mode() -> None:
    log, start = _approved_start(SandboxMode.WORKSPACE_WRITE)
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"sandbox_mode": SandboxMode.WORKSPACE_WRITE},
        subject_id=start.subject_id,
    )

    assert _control(log, "A3").status is ControlStatus.PASSED


@pytest.mark.parametrize("reported", [None, "forged"])
def test_a3_rejects_missing_or_invalid_report_for_declared_pass_call(reported: object) -> None:
    log = _passing_execution(SandboxMode.WORKSPACE_WRITE, reported)

    assert _control(log, "A3").status is ControlStatus.FAILED


def test_a3_accepts_failed_call_without_actual_mode_when_request_is_repeated() -> None:
    log = _passing_execution(
        SandboxMode.WORKSPACE_WRITE,
        None,
        completed_status=ToolCallStatus.FAILED,
        repeated_request=SandboxMode.WORKSPACE_WRITE,
    )

    assert _control(log, "A3").status is ControlStatus.PASSED


def test_a3_rejects_failed_call_with_bogus_repeated_requested_mode() -> None:
    log = _passing_execution(
        SandboxMode.WORKSPACE_WRITE,
        None,
        completed_status=ToolCallStatus.FAILED,
        repeated_request="forged",
    )

    assert _control(log, "A3").status is ControlStatus.FAILED


def test_a3_preserves_legacy_call_without_declared_mode() -> None:
    log = _passing_execution(None, SandboxMode.WORKSPACE_WRITE)

    assert _control(log, "A3").status is ControlStatus.PASSED


def test_a3_rejects_mode_bound_digest_when_start_omits_mode() -> None:
    log = _passing_execution(
        None,
        SandboxMode.WORKSPACE_WRITE,
        ticket_mode=SandboxMode.WORKSPACE_WRITE,
    )

    assert _control(log, "A3").status is ControlStatus.FAILED


@pytest.mark.parametrize("location", ["request", "start"])
def test_a3_rejects_invalid_persisted_sandbox_mode(location: str) -> None:
    log, start = (
        _approved_start(SandboxMode.WORKSPACE_WRITE, request_mode="forged")
        if location == "request"
        else _approved_start(SandboxMode.WORKSPACE_WRITE, start_mode="forged")
    )
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"sandbox_mode": SandboxMode.WORKSPACE_WRITE},
        subject_id=start.subject_id,
    )

    assert _control(log, "A3").status is ControlStatus.FAILED


def test_a6_rejects_denial_bound_to_forged_request_mode() -> None:
    log = InMemoryEventLog(new_id("run"))
    arguments = {"code": "x"}
    mode = SandboxMode.WORKSPACE_WRITE
    ticket = tool_intent_sha256("run_python", arguments, sandbox_mode=mode)
    decision = Gate(PolicyEngine(load_policy_stack()), log, approver=None).check_capability(
        subject_id=new_id("tool"),
        capability=ToolCapability(
            id="run_python", risk_tags=("code_execution",), sandbox_mode=mode
        ),
        risk=RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1),
        details={
            "tool": "run_python",
            "arguments": arguments,
            "tool_intent_sha256": ticket,
            "sandbox_mode": "forged",
        },
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision.id, "approved": False, "automatic": False},
    )
    log.append(
        EventType.TOOL_DENIED,
        Actor.system(),
        {
            "decision_id": decision.id,
            "reason": "tool call rejected by a human",
            "tool": "run_python",
            "arguments": arguments,
            "tool_intent_sha256": ticket,
            "sandbox_mode": mode,
        },
        subject_id=new_id("tool"),
    )

    assert _control(log, "A6").status is ControlStatus.FAILED
