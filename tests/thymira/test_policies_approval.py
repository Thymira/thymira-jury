"""HITL-01 async human-approval flow: the PendingApproval fold, deferred Gate, ApprovalService.

Done-when (docs/roadmap/product-final.md, ``HITL-01``): in deferred mode a review decision leaves
exactly one pending approval and ``needs_human`` True; ``LocalApprovalService.resolve`` appends a
valid, chain-verifying ``human.approval`` and returns approved is True; resolving an unknown
``decision_id`` raises; approval events are ``log_only`` (never ``model_visible``).
"""

from __future__ import annotations

from typing import Any

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import (
    Gate,
    GateMode,
    LocalApprovalService,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    UnknownApprovalError,
    auto_approve,
    decision_from_events,
    load_policy_stack,
    needs_human,
    pending_approvals,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    Decision,
    EventSurface,
    EventType,
    PolicyDecision,
    RunCondition,
    RunOutcome,
    RunStage,
    RunState,
    new_id,
)

RUN = new_id("run")
_APPROVAL_EVENTS = (EventType.HUMAN_APPROVAL_REQUESTED, EventType.HUMAN_APPROVAL)


@pytest.fixture
def engine() -> PolicyEngine:
    """The credit-risk stack: ``final_decision`` maps to ``REQUIRE_HUMAN_REVIEW``."""
    return PolicyEngine(load_policy_stack("credit_risk"))


@pytest.fixture
def human() -> Actor:
    return Actor(kind=ActorKind.HUMAN, id="alice", role="risk-officer", authenticated=True)


def _defer(
    engine: PolicyEngine, log: InMemoryEventLog, *, cost: dict | None = None
) -> PolicyDecision:
    """Run a review action through a deferred Gate and return the unresolved decision."""
    gate = Gate(engine, log, mode=GateMode.DEFERRED)
    return gate.check_action(
        subject_kind="run",
        subject_id="run",
        action_type="final_decision",
        summary="close the run",
        cost_so_far=cost,
    )


def _decision(kind: Decision) -> PolicyDecision:
    """A minimal, well-formed PolicyDecision for pure fold and helper tests."""
    return PolicyDecision(
        id=new_id("decision"),
        run_id=RUN,
        subject_kind="run",
        subject_id="run",
        decision=kind,
        rule_id="default",
        reason="reason",
        policy_name="base@1.0",
        policy_sha256="0" * 64,
    )


def test_deferred_review_leaves_exactly_one_pending_and_needs_human(
    engine: PolicyEngine,
) -> None:
    """Done-when: a deferred review leaves exactly one pending approval; needs_human is True."""
    log = InMemoryEventLog(RUN)

    decision = _defer(engine, log, cost={"total_cost_usd": 0.5})

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert needs_human(decision) is True
    pending = pending_approvals(log.events())
    assert len(pending) == 1
    only = pending[0]
    assert only.decision_id == decision.id
    assert only.run_id == RUN
    assert only.summary == "close the run"
    assert only.cost_so_far == {"total_cost_usd": 0.5}


def test_deferred_gate_requests_but_records_no_answer(engine: PolicyEngine) -> None:
    """Deferred mode records the request and returns; it never appends a human.approval."""
    log = InMemoryEventLog(RUN)

    _defer(engine, log)

    assert [event.type for event in log.events()] == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
    ]
    assert log.verify().valid


def test_deferred_gate_forbids_an_in_process_approver(engine: PolicyEngine) -> None:
    """A deferred Gate resolves out of band, so an injected approver is a configuration error."""
    log = InMemoryEventLog(RUN)

    with pytest.raises(ValueError, match="no in-process approver"):
        Gate(engine, log, approver=auto_approve, mode=GateMode.DEFERRED)


def test_local_service_resolve_appends_verifying_approval(
    engine: PolicyEngine, human: Actor
) -> None:
    """Done-when: resolve appends a valid, chain-verifying human.approval and approved is True."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    service = LocalApprovalService(log)

    resolved = service.resolve(RUN, decision.id, approved=True, by=human, rationale="signed off")

    assert isinstance(resolved, PolicyDecision)
    assert resolved.id == decision.id
    answer = log.events()[-1]
    assert answer.type is EventType.HUMAN_APPROVAL
    assert answer.payload["approved"] is True
    assert answer.payload["rationale"] == "signed off"
    assert answer.actor == human
    assert log.verify().valid
    assert service.pending(RUN) == ()


def test_local_service_rejects_a_declared_human_without_authentication(
    engine: PolicyEngine,
) -> None:
    """A self-declared human cannot append approval evidence to the public service."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    service = LocalApprovalService(log)

    with pytest.raises(ValueError, match="authenticated human"):
        service.resolve(
            RUN,
            decision.id,
            approved=True,
            by=Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False),
        )

    assert service.pending(RUN)[0].decision_id == decision.id
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in log.events())


def test_resolve_rejection_records_a_negative_answer_and_clears_pending(
    engine: PolicyEngine, human: Actor
) -> None:
    """A rejection is recorded as approved False and removes the pending approval just the same."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    service = LocalApprovalService(log)

    service.resolve(RUN, decision.id, approved=False, by=human)

    assert log.events()[-1].payload["approved"] is False
    assert pending_approvals(log.events()) == ()
    assert log.verify().valid


def test_resolve_unknown_decision_raises(engine: PolicyEngine, human: Actor) -> None:
    """Done-when: resolving an unknown decision_id raises."""
    log = InMemoryEventLog(RUN)
    _defer(engine, log)
    service = LocalApprovalService(log)

    with pytest.raises(UnknownApprovalError):
        service.resolve(RUN, new_id("decision"), approved=True, by=human)


def test_resolving_the_same_decision_twice_raises(engine: PolicyEngine, human: Actor) -> None:
    """Once resolved a decision is no longer pending, so a second answer is refused."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    service = LocalApprovalService(log)

    service.resolve(RUN, decision.id, approved=True, by=human)

    with pytest.raises(UnknownApprovalError):
        service.resolve(RUN, decision.id, approved=True, by=human)


def test_resolve_refuses_a_foreign_run(engine: PolicyEngine, human: Actor) -> None:
    """A local service serves exactly its own run and refuses any other run_id."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    service = LocalApprovalService(log)

    with pytest.raises(ValueError, match="not served by this"):
        service.resolve(new_id("run"), decision.id, approved=True, by=human)


def test_approval_events_are_log_only(engine: PolicyEngine, human: Actor) -> None:
    """Done-when: approval events are log_only (never model_visible) (ADR-0005 idea 7)."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    LocalApprovalService(log).resolve(RUN, decision.id, approved=True, by=human)

    approval_events = [event for event in log.events() if event.type in _APPROVAL_EVENTS]
    assert len(approval_events) == 2
    assert all(event.surface is EventSurface.LOG_ONLY for event in approval_events)
    assert not any(event.surface is EventSurface.MODEL_VISIBLE for event in approval_events)


def test_pending_fold_matches_by_decision_id_and_deduplicates() -> None:
    """The fold is requested-minus-resolved by decision_id; a repeat request is one pending."""
    log = InMemoryEventLog(RUN)
    first = new_id("decision")
    second = new_id("decision")
    for decision_id in (first, first, second):
        log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            {"decision_id": decision_id, "rule_id": "R", "reason": "r", "summary": "s"},
            subject_id="run",
        )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {"decision_id": second, "approved": True},
        subject_id="run",
    )

    pending = pending_approvals(log.events())

    assert [item.decision_id for item in pending] == [first]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (Decision.PASS, False),
        (Decision.WARNING, False),
        (Decision.BLOCK, False),
        (Decision.REQUIRE_HUMAN_REVIEW, True),
    ],
)
def test_needs_human_is_true_only_for_review(kind: Decision, expected: bool) -> None:
    """needs_human isolates the one disposition an ApprovalService must resolve."""
    assert needs_human(_decision(kind)) is expected


def _control_plane_answer(
    log: InMemoryEventLog, decision_id: str, *, approved: bool = True
) -> None:
    """Append the approval shape `Gate.request_approval` writes: `Approval.to_json_dict()`.

    The control-plane path appends the frozen Contract 0.3 record, whose decision field is
    `policy_decision_id`; `Gate.check_action` and `LocalApprovalService.resolve` write the ad-hoc
    `decision_id` instead. Both shapes are already on the append-only chain, so every reader of
    the pair must accept either name.
    """
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        {
            "id": new_id("approval"),
            "run_id": log.run_id,
            "policy_decision_id": decision_id,
            "authorization_context_sha256": "0" * 64,
            "approved": approved,
        },
        subject_id="run",
        surface=EventSurface.LOG_ONLY,
    )


def test_pending_fold_clears_an_approval_recorded_by_the_control_plane(
    engine: PolicyEngine,
) -> None:
    """A decision a human really approved through the control plane is no longer pending."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)

    _control_plane_answer(log, decision.id)

    assert pending_approvals(log.events()) == ()


@pytest.mark.parametrize("approved", [True, False])
def test_pending_fold_ignores_a_valid_chain_answer_from_an_unauthenticated_human(
    engine: PolicyEngine, approved: bool
) -> None:
    """A valid chain does not turn a declared human identity into approval provenance."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False),
        {"decision_id": decision.id, "approved": approved, "automatic": False},
        subject_id=decision.subject_id,
    )

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]
    assert log.verify().valid


def test_pending_fold_keeps_an_automatic_human_answer_pending(engine: PolicyEngine) -> None:
    """A human event marked automatic cannot hide the request from the public projection."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision.id, "approved": True, "automatic": True},
        subject_id=decision.subject_id,
    )

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]
    assert log.verify().valid


def test_local_service_refuses_to_re_answer_a_control_plane_approval(
    engine: PolicyEngine, human: Actor
) -> None:
    """A second answer would put two contradictory approvals for one decision on the chain."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    _control_plane_answer(log, decision.id, approved=False)
    service = LocalApprovalService(log)

    with pytest.raises(UnknownApprovalError):
        service.resolve(RUN, decision.id, approved=True, by=human)

    assert [e.type for e in log.events()].count(EventType.HUMAN_APPROVAL) == 1


def test_pending_fold_keeps_a_request_no_approval_names(engine: PolicyEngine) -> None:
    """An approval naming no decision answers nothing: `None` never becomes an identity."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)

    log.append(EventType.HUMAN_APPROVAL, Actor.system(), {"approved": True}, subject_id="run")

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]


def test_pending_fold_reads_a_request_recorded_under_the_contract_key() -> None:
    """The tolerant accessor is the field's property, so it applies to both sides of the pair."""
    log = InMemoryEventLog(RUN)
    decision_id = new_id("decision")
    log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"policy_decision_id": decision_id, "rule_id": "R", "reason": "r", "summary": "s"},
        subject_id="run",
    )

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision_id]


def test_pending_fold_carries_the_tool_call_a_human_is_asked_about(engine: PolicyEngine) -> None:
    """A Tool Manager request names the exact call; a run-level review names none."""
    log = InMemoryEventLog(RUN)
    gate = Gate(engine, log, mode=GateMode.DEFERRED)
    risk = RiskProfile(risk_level="limited", activity_category="analysis", confidence=0.1)
    gate.check_capability(
        subject_id="tool_1",
        capability=ToolCapability(id="echo", external_effects=()),
        risk=risk,
        summary="echo",
        details={"tool": "echo", "arguments": {"value": "x"}, "tool_intent_sha256": "a" * 64},
    )
    _defer(engine, log)

    tool_call, run_level = pending_approvals(log.events())

    assert tool_call.tool_call == {
        "tool": "echo",
        "arguments": {"value": "x"},
        "tool_intent_sha256": "a" * 64,
    }
    assert run_level.tool_call is None


def test_decision_from_events_rebuilds_the_recorded_decision(engine: PolicyEngine) -> None:
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)

    assert decision_from_events(log.events(), decision.id) == decision
    with pytest.raises(UnknownApprovalError):
        decision_from_events(log.events(), new_id("decision"))


_ACTIVE_STAGES = {
    "begin_audit": RunStage.AUDITING,
    "begin_reporting": RunStage.REPORTING,
    "reopen": RunStage.EXECUTING,
}
_TERMINAL_STATES = {
    "complete": (RunStage.REPORTING, RunOutcome.COMPLETED),
    "block": (RunStage.EXECUTING, RunOutcome.BLOCKED),
    "fail": (RunStage.EXECUTING, RunOutcome.FAILED),
    "cancel": (RunStage.EXECUTING, RunOutcome.CANCELLED),
}


def _transition_payload(command: str, *, version: int = 3) -> dict[str, Any]:
    """The whole payload ``RunController`` writes for one command, state and flat copy included."""
    if command in _ACTIVE_STAGES:
        state = RunState(
            stage=_ACTIVE_STAGES[command], condition=RunCondition.ACTIVE, version=version
        )
    elif command in _TERMINAL_STATES:
        stage, outcome = _TERMINAL_STATES[command]
        state = RunState(
            stage=stage, condition=RunCondition.TERMINAL, outcome=outcome, version=version
        )
    else:
        # A command that neither ends the Run nor ends a pass: it still writes a coherent
        # transition, and it still has to leave every open review answerable.
        state = RunState(stage=RunStage.EXECUTING, condition=RunCondition.ACTIVE, version=version)
    return {
        "command": command,
        "from_version": version - 1,
        "to_version": version,
        "previous_stage": state.stage.value,
        "stage": state.stage.value,
        "condition": state.condition.value,
        "wait_reason": None,
        "outcome": state.outcome.value if state.outcome is not None else None,
        "state": state.model_dump(mode="json"),
    }


def _core_transition(log: InMemoryEventLog, command: str) -> None:
    """Append the ``run.transitioned`` fact ``RunController`` writes for one command."""
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        _transition_payload(command),
        subject_id=RUN,
        producer="thymira.core",
        producer_version="0.1",
    )


@pytest.mark.parametrize("command", ["cancel", "block", "fail", "complete"])
def test_pending_approvals_drops_a_request_the_run_terminated_on(
    engine: PolicyEngine, human: Actor, command: str
) -> None:
    """A review nobody answered before the Run ended is no longer answerable.

    The stale-resolve hole: ``LocalApprovalService.resolve`` refuses anything outside the pending
    set, so dropping the request here closes the governance path too -- a human.approval can no
    longer be appended for a Run that has left the state which raised the request.
    """
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)
    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]

    _core_transition(log, command)

    assert pending_approvals(log.events()) == ()
    service = LocalApprovalService(log)
    with pytest.raises(UnknownApprovalError):
        service.resolve(RUN, decision.id, approved=True, by=human)
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in log.events())


def test_pending_approvals_keeps_a_request_across_a_park_and_resume(
    engine: PolicyEngine, human: Actor
) -> None:
    """The filter must not strand a parked Run: the exact parked review stays answerable.

    ``wait_for_approval`` and ``resume`` are how a Run waits for the very human this request is
    addressed to. Neither ends the Run nor the pass, so neither closes the scope -- and expiry is
    deliberately not applied in this fold, or a parked Run could be left with a review nobody is
    allowed to answer and no way forward.
    """
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)

    _core_transition(log, "wait_for_approval")
    _core_transition(log, "resume")

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]
    resolved = LocalApprovalService(log).resolve(RUN, decision.id, approved=True, by=human)
    assert resolved.id == decision.id
    assert pending_approvals(log.events()) == ()


def test_pending_approvals_ignores_a_transition_core_did_not_write(
    engine: PolicyEngine, human: Actor
) -> None:
    """Closing a review is Core's fact alone; a forged transition is an availability attack."""
    log = InMemoryEventLog(RUN)
    decision = _defer(engine, log)

    log.append(
        EventType.RUN_TRANSITIONED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        _transition_payload("cancel"),
        subject_id=RUN,
        producer="thymira.core",
        producer_version="0.1",
    )
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        _transition_payload("cancel"),
        subject_id=RUN,
        producer="thymira.thy",
        producer_version="0.1",
    )
    # The envelope is forgeable: a payload that does not agree with itself is refused too.
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {"command": "cancel"},
        subject_id=RUN,
        producer="thymira.core",
        producer_version="0.1",
    )
    log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {**_transition_payload("cancel"), "condition": "active"},
        subject_id=RUN,
        producer="thymira.core",
        producer_version="0.1",
    )

    assert [item.decision_id for item in pending_approvals(log.events())] == [decision.id]
    assert LocalApprovalService(log).resolve(RUN, decision.id, approved=True, by=human).id == (
        decision.id
    )
