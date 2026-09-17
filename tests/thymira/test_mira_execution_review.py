"""A3 independently verifies current execution-review evidence.

Real: Gate, PolicyEngine, human approval service, ToolManager, RunController and local stores.
The registered tool is an effect-free fake. Corrupted evidence is rebuilt into a valid hash
chain so these tests exercise authorization and lifecycle meaning rather than hash failures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest

from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from thymira.core import RunController, RunEventLog
from thymira.core.run_state import RunTransitionKind
from thymira.events import InMemoryEventLog, verify_events
from thymira.mira.checks import AuditContext, ControlStatus, controls
from thymira.policies import (
    CapabilityRule,
    Gate,
    LocalApprovalService,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
)
from thymira.schemas import Actor, Decision, EventType, ExecutionConstraints, Run, new_id
from thymira.state import LocalRunStore
from thymira.tools import ToolContext, ToolManager, ToolRegistry, tool_intent_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from thymira.schemas import Event


_HUMAN = Actor(kind="human", id="reviewer", authenticated=True)
_RISK = RiskProfile(risk_level="medium", activity_category="analysis", confidence=1)
_PROBE = ToolCapability(id="echo", risk_tags=("probe",), external_effects=())
_TRAINING = ToolCapability(id="training", risk_tags=("model_training",))
_EFFECT = {"value": "exact-effect"}
_POLICY = Policy(
    name="mira-execution-review",
    version="1.0",
    default_decision=Decision.PASS,
    capability_default_decision=Decision.PASS,
    capability_rules=(
        CapabilityRule(
            id="TRAIN",
            risk_tags=("model_training",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Training requires review before every call.",
            execution_constraints=ExecutionConstraints(requires_human_review=True),
        ),
        CapabilityRule(
            id="PROBE",
            risk_tags=("probe",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Review this exact probe.",
        ),
    ),
)


@dataclass(frozen=True)
class _ReviewRuntime:
    context: ToolContext
    manager: ToolManager
    controller: RunController


@pytest.fixture
def review_runtime(tmp_path: Path) -> _ReviewRuntime:
    """Build real review evidence with the shared effect-free tool fixture."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Verify exact execution reviews.",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    log = RunEventLog(store, run.id)
    context = review_gated_context(tmp_path, run_id=run.id, log=log, engine=PolicyEngine(_POLICY))
    tool = FakeTool("echo", _PROBE, arguments_model=EchoArguments)
    return _ReviewRuntime(
        replace(context, risk_profile=_RISK),
        ToolManager(ToolRegistry((tool,))),
        RunController(store),
    )


def _approve(context: ToolContext, decision_id: str) -> None:
    LocalApprovalService(context.event_log).resolve(
        context.run_id, decision_id, approved=True, by=_HUMAN
    )


def _assert_a3(events: Sequence[Event], expected: ControlStatus) -> None:
    assert verify_events(events).valid
    result = controls.a3_authorization_before_tool(AuditContext(events[0].run_id, events))
    assert result[0] is expected, result


def _rechain(context: ToolContext, change: Callable[[Event, dict[str, Any]], None]) -> list[Event]:
    """Keep a valid chain while replacing only the evidence a semantic check must reject."""
    rebuilt = InMemoryEventLog(context.run_id)
    for event in context.event_log.events():
        payload = dict(event.payload)
        change(event, payload)
        rebuilt.append(
            event.type,
            event.actor,
            payload,
            subject_id=event.subject_id,
            producer=event.producer,
            producer_version=event.producer_version,
        )
    return rebuilt.events()


@pytest.mark.parametrize("drift", ["inherited_review", "policy_hash"])
def test_a3_rejects_stale_approval_for_an_exact_effect(
    review_runtime: _ReviewRuntime, drift: str
) -> None:
    context, manager = review_runtime.context, review_runtime.manager
    old = manager.execute(context, "echo", _EFFECT)
    assert old.pending_approval is not None
    _approve(context, old.pending_approval.id)

    if drift == "inherited_review":
        start = context.gate.authorize_execution(_RISK, (_PROBE, _TRAINING))
        _approve(context, start.id)
        context = replace(
            context,
            execution_decision=start,
            execution_constraints=start.execution_constraints,
        )
        awaiting = manager.execute(context, "echo", _EFFECT)
        assert awaiting.pending_approval is not None
        assert not any(e.type is EventType.TOOL_STARTED for e in context.event_log.events())
        claimed = old.pending_approval.id
    else:
        replacement = _POLICY.model_copy(update={"name": "replacement-policy"})
        context = replace(context, gate=Gate(PolicyEngine(replacement), context.event_log))
        awaiting = manager.execute(context, "echo", _EFFECT)
        assert awaiting.pending_approval is not None
        assert awaiting.pending_approval.policy_sha256 != old.pending_approval.policy_sha256
        _approve(context, awaiting.pending_approval.id)
        assert manager.execute(context, "echo", _EFFECT).result.success
        assert manager.execute(context, "echo", _EFFECT).pending_approval is not None
        claimed = awaiting.pending_approval.id

    forged_call = new_id("tool")
    context.event_log.append(
        EventType.TOOL_STARTED,
        Actor(kind="tool", id="echo"),
        {
            "tool_call_id": forged_call,
            "tool": "echo",
            "arguments": _EFFECT,
            "tool_intent_sha256": tool_intent_sha256("echo", _EFFECT),
            "decision_id": claimed,
        },
        subject_id=forged_call,
    )

    _assert_a3(context.event_log.events(), ControlStatus.FAILED)


@pytest.mark.parametrize(
    "damage",
    [None, "missing_hash", "malformed_hash", "missing_constraints", "malformed_constraints"],
)
def test_a3_requires_readable_policy_and_constraints_for_modern_tickets(
    review_runtime: _ReviewRuntime, damage: str | None
) -> None:
    context, manager = review_runtime.context, review_runtime.manager
    pending = manager.execute(context, "echo", _EFFECT)
    assert pending.pending_approval is not None
    _approve(context, pending.pending_approval.id)
    assert manager.execute(context, "echo", _EFFECT).result.success

    def corrupt(event: Event, payload: dict[str, Any]) -> None:
        if damage is None or event.type is not EventType.POLICY_DECISION:
            return
        key = "policy_sha256" if "hash" in damage else "execution_constraints"
        if damage.startswith("missing"):
            payload.pop(key)
        else:
            payload[key] = (
                "not-a-digest" if "hash" in damage else {"requires_human_review": "invalid"}
            )

    expected = ControlStatus.PASSED if damage is None else ControlStatus.FAILED
    _assert_a3(_rechain(context, corrupt), expected)


def _enter_audit(review_runtime: _ReviewRuntime) -> Event:
    """Enter the real audit lifecycle after approving the recorded execution review."""
    context, controller = review_runtime.context, review_runtime.controller
    controller.advance(context.run_id, RunTransitionKind.START)
    start = context.gate.authorize_execution(_RISK, (_TRAINING,))
    controller.park_for_review(context.run_id, start, resume_from="execution_start")
    _approve(context, start.id)
    controller.advance(
        context.run_id, RunTransitionKind.RESUME, payload={"resume_from": "execution_start"}
    )
    controller.advance(context.run_id, RunTransitionKind.BEGIN_EXECUTION)
    transition, _ = controller.advance(context.run_id, RunTransitionKind.BEGIN_AUDIT)
    return transition


def _read_without_inherited_review(context: ToolContext) -> None:
    """Record a tool PASS under the independent audit context, without a per-call approval."""
    read_capability = ToolCapability(id="audit_read", external_effects=())
    manager = ToolManager(ToolRegistry((FakeTool("audit_read", read_capability),)))
    assert manager.execute(context, "audit_read").result.success


@pytest.mark.parametrize(
    ("key", "value"), [(None, None), ("wait_reason", "approval"), ("outcome", "completed")]
)
def test_a3_requires_coherent_flat_state_before_releasing_inherited_review(
    review_runtime: _ReviewRuntime, key: str | None, value: str | None
) -> None:
    _enter_audit(review_runtime)
    context = review_runtime.context
    _read_without_inherited_review(context)

    def corrupt(event: Event, payload: dict[str, Any]) -> None:
        if (
            key is not None
            and event.type is EventType.RUN_TRANSITIONED
            and payload.get("command") == "begin_audit"
        ):
            assert payload["state"][key] is None
            payload[key] = value

    expected = ControlStatus.PASSED if key is None else ControlStatus.FAILED
    _assert_a3(_rechain(context, corrupt), expected)


@pytest.mark.parametrize(
    ("exit_command", "forged_reentry"),
    [
        (RunTransitionKind.BEGIN_REPORTING, False),
        (RunTransitionKind.BEGIN_REPORTING, True),
        (RunTransitionKind.REOPEN, False),
        (RunTransitionKind.BLOCK, False),
        (RunTransitionKind.FAIL, False),
        (RunTransitionKind.CANCEL, False),
    ],
)
def test_a3_rearms_inherited_review_after_leaving_audit(
    review_runtime: _ReviewRuntime, exit_command: RunTransitionKind, *, forged_reentry: bool
) -> None:
    audit_transition = _enter_audit(review_runtime)
    context = review_runtime.context
    review_runtime.controller.advance(context.run_id, exit_command)
    if forged_reentry:
        context.event_log.append(
            EventType.RUN_TRANSITIONED,
            Actor(kind="tool", id="audit_read"),
            dict(audit_transition.payload),
            subject_id=context.run_id,
            producer="thymira.core",
        )
    _read_without_inherited_review(context)

    _assert_a3(context.event_log.events(), ControlStatus.FAILED)


def test_a3_preserves_audit_scope_across_approval_wait_and_resume(
    review_runtime: _ReviewRuntime,
) -> None:
    _enter_audit(review_runtime)
    context, controller = review_runtime.context, review_runtime.controller
    review = context.gate.check_capability(subject_id=new_id("tool"), capability=_PROBE, risk=_RISK)
    controller.park_for_review(context.run_id, review)
    _approve(context, review.id)
    controller.advance(context.run_id, RunTransitionKind.RESUME)
    _read_without_inherited_review(context)

    _assert_a3(context.event_log.events(), ControlStatus.PASSED)


def test_a3_ignores_terminal_boundary_whose_outcome_disagrees_with_command(
    review_runtime: _ReviewRuntime,
) -> None:
    _enter_audit(review_runtime)
    context = review_runtime.context
    review_runtime.controller.advance(context.run_id, RunTransitionKind.BLOCK)
    _read_without_inherited_review(context)

    def corrupt(event: Event, payload: dict[str, Any]) -> None:
        if event.type is EventType.RUN_TRANSITIONED and payload.get("command") == "block":
            payload["command"] = "fail"

    _assert_a3(_rechain(context, corrupt), ControlStatus.PASSED)
