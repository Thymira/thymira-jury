"""Unit tests for the bounded MIRA-to-Run control-plane path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest

from thymira.core import (
    AuthorizationScopeError,
    ExpiredAuthorizationError,
    MiraControlPlane,
    ReusedAuthorizationError,
    RunController,
    RunTransitionCommand,
    RunTransitionKind,
    apply_transition,
    initial_run_state,
)
from thymira.events import hash_authorization_context
from thymira.policies import (
    ApprovalRequest,
    Approver,
    PolicyEngine,
    auto_approve,
    auto_reject,
    load_default_policy,
)
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    Approval,
    AuthorizationContext,
    AuthorizationDecision,
    EventType,
    PolicyDecision,
    Run,
    new_id,
)
from thymira.state import LocalRunStore

if TYPE_CHECKING:
    from pathlib import Path


NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _run() -> Run:
    """Build one valid Run for the control-plane tests."""
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Review the MIRA finding.",
    )


def _planning_store(tmp_path: Path) -> tuple[LocalRunStore, Run]:
    """Create a Run with its ordinary initial start transition already recorded."""
    store = LocalRunStore(tmp_path / "runs")
    run = _run()
    store.create(run, actor=Actor.system())
    initial = initial_run_state(run.id)
    started = apply_transition(
        initial,
        RunTransitionCommand(RunTransitionKind.START, initial.version),
    )
    store.append(
        run.id,
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {**started.event.payload(), "state": started.state.state.model_dump(mode="json")},
        expected_version=store.version(run.id),
    )
    return store, run


def test_run_controller_advance_persists_ordinary_progress(tmp_path: Path) -> None:
    """Forward lifecycle progress is recorded without a policy or approval object."""
    store = LocalRunStore(tmp_path / "runs")
    run = _run()
    store.create(run, actor=Actor.system())
    controller = RunController(store, now=lambda: NOW)

    event, state = controller.advance(run.id, RunTransitionKind.START)

    assert state.stage.value == "planning"
    assert event.type is EventType.RUN_TRANSITIONED
    assert event.authorization_context_sha256 is None
    assert controller.current_state(run.id).version == 1


def test_run_controller_rejects_control_action_in_advance(tmp_path: Path) -> None:
    """MIRA control actions cannot bypass the policy-gated transition path."""
    store = LocalRunStore(tmp_path / "runs")
    run = _run()
    store.create(run, actor=Actor.system())
    controller = RunController(store)

    with pytest.raises(ValueError, match="ordinary progress"):
        controller.advance(run.id, RunTransitionKind.PAUSE)


def _intent(
    run: Run,
    action_kind: ActionKind,
    *,
    subject_kind: Literal["run", "findings"] = "run",
) -> ActionIntent:
    """Build a MIRA proposal with no embedded authorization value."""
    return ActionIntent(
        id=new_id("intent"),
        run_id=run.id,
        requester=Actor(kind=ActorKind.AGENT, id="mira", authenticated=True),
        action_kind=action_kind,
        subject_kind=subject_kind,
        subject_id=run.id if subject_kind == "run" else new_id("finding"),
        purpose=f"Request {action_kind.value}.",
        idempotency_key=new_id("intent"),
    )


def _plane(
    store: LocalRunStore,
    *,
    approver: Approver | None = None,
    human: Actor | None = None,
) -> MiraControlPlane:
    """Build the single linear integration path using the reviewed base policy."""
    return MiraControlPlane(
        store,
        PolicyEngine(load_default_policy("base")),
        approver=approver,
        human=human,
        now=lambda: NOW,
    )


def test_mira_action_intent_has_no_direct_state_authority(tmp_path: Path) -> None:
    """A bare MIRA proposal cannot call the persistent transition writer."""
    store, run = _planning_store(tmp_path)
    controller = RunController(store, now=lambda: NOW)
    intent = _intent(run, ActionKind.PAUSE_RUN)
    engine = PolicyEngine(load_default_policy("base"))
    decision = engine.decide_action(
        run_id=run.id,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_type=intent.action_kind.value,
    )
    context = AuthorizationContext(
        id=new_id("authorization"),
        run_id=run.id,
        intent_id=intent.id,
        policy_decision_id=decision.id,
        policy_sha256=decision.policy_sha256,
        decision=AuthorizationDecision.ALLOW,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_kind=intent.action_kind,
        constraints={"expected_state_version": 1},
        expires_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(AuthorizationScopeError, match="matching Gate record"):
        controller.transition(intent=intent, decision=decision, context=context, approval=None)

    assert controller.current_state(run.id).condition.value == "active"


def test_pause_intent_passes_policy_and_records_an_allowed_transition(tmp_path: Path) -> None:
    """A PASS policy decision maps to ALLOW and persists the exact authorization hash."""
    store, run = _planning_store(tmp_path)

    result = _plane(store).handle(_intent(run, ActionKind.PAUSE_RUN))

    assert result.decision.decision.value == "PASS"
    assert result.context.decision is AuthorizationDecision.ALLOW
    assert result.approval is None
    assert result.event is not None
    assert result.event.type is EventType.RUN_TRANSITIONED
    assert result.event.authorization_context_sha256 is not None
    assert result.state is not None
    assert result.state.condition.value == "paused"


def test_request_information_moves_the_run_to_a_waiting_information_state(tmp_path: Path) -> None:
    """The local information request is a permitted, policy-recorded transition."""
    store, run = _planning_store(tmp_path)

    result = _plane(store).handle(_intent(run, ActionKind.REQUEST_INFORMATION))

    assert result.event is not None
    assert result.state is not None
    assert result.state.condition.value == "waiting"
    assert result.state.wait_reason is not None
    assert result.state.wait_reason.value == "information"


def test_review_findings_waits_for_a_human_when_no_response_is_available(tmp_path: Path) -> None:
    """A required review records its pending request but does not transition the Run."""
    store, run = _planning_store(tmp_path)

    result = _plane(store).handle(_intent(run, ActionKind.REVIEW_FINDINGS, subject_kind="findings"))

    assert result.context.requires_approval
    assert result.approval is None
    assert result.event is None
    assert [event.type for event in store.events(run.id)][-2:] == [
        EventType.POLICY_DECISION,
        EventType.HUMAN_APPROVAL_REQUESTED,
    ]


def test_required_approval_cannot_be_removed_from_a_review_context(tmp_path: Path) -> None:
    """Changing the approval flag cannot turn a pending review into an allowed transition."""
    store, run = _planning_store(tmp_path)
    intent = _intent(run, ActionKind.REVIEW_FINDINGS, subject_kind="findings")
    result = _plane(store).handle(intent)
    relaxed = result.context.model_copy(update={"requires_approval": False})

    with pytest.raises(AuthorizationScopeError, match="invalid approval requirement"):
        _plane(store).controller.transition(
            intent=intent,
            decision=result.decision,
            context=relaxed,
            approval=None,
        )


def test_resume_intent_can_only_resume_a_paused_run(tmp_path: Path) -> None:
    """The bounded resume action uses the same authorization path as an initial pause."""
    store, run = _planning_store(tmp_path)
    plane = _plane(store)
    paused = plane.handle(_intent(run, ActionKind.PAUSE_RUN))
    resumed = plane.handle(_intent(run, ActionKind.RESUME_RUN))

    assert paused.state is not None
    assert paused.state.condition.value == "paused"
    assert resumed.state is not None
    assert resumed.state.condition.value == "active"


@pytest.mark.parametrize(
    ("approver", "expected_transition"),
    [(auto_approve, True), (auto_reject, False)],
)
def test_request_approval_records_human_response_and_respects_it(
    tmp_path: Path,
    approver: Approver,
    expected_transition: bool,
) -> None:
    """An approval enables only its exact context; rejection leaves the Run unchanged."""
    store, run = _planning_store(tmp_path)

    result = _plane(store, approver=approver).handle(_intent(run, ActionKind.REQUEST_APPROVAL))

    assert result.approval is not None
    assert result.approval.approved is expected_transition
    assert (result.event is not None) is expected_transition
    assert [event.type for event in store.events(run.id)][-1] is (
        EventType.RUN_TRANSITIONED if expected_transition else EventType.HUMAN_APPROVAL
    )


def test_controller_rejects_an_authorization_context_with_the_wrong_scope(tmp_path: Path) -> None:
    """A policy decision cannot be reused for another ActionIntent subject."""
    store, run = _planning_store(tmp_path)
    result = _plane(store).handle(_intent(run, ActionKind.PAUSE_RUN))
    assert result.event is not None
    assert result.state is not None
    assert result.context is not None
    mismatched = result.context.model_copy(update={"subject_id": "another-subject"})

    with pytest.raises(AuthorizationScopeError, match="does not match the ActionIntent"):
        _plane(store).controller.transition(
            intent=_intent(run, ActionKind.RESUME_RUN),
            decision=result.decision,
            context=mismatched,
            approval=None,
        )


def test_controller_rejects_expired_and_reused_authorization_contexts(tmp_path: Path) -> None:
    """A transition context is short-lived and can occur only once in the event history."""
    store, run = _planning_store(tmp_path)
    intent = _intent(run, ActionKind.PAUSE_RUN)
    result = _plane(store).handle(intent)
    assert result.event is not None
    expired = result.context.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    controller = _plane(store).controller

    with pytest.raises(ExpiredAuthorizationError, match="expired"):
        controller.transition(
            intent=_intent(run, ActionKind.RESUME_RUN),
            decision=result.decision,
            context=expired,
            approval=None,
        )
    stale = result.context.model_copy(update={"constraints": {"expected_state_version": 0}})
    with pytest.raises(AuthorizationScopeError, match="stale Run state"):
        controller.transition(
            intent=intent,
            decision=result.decision,
            context=stale,
            approval=None,
        )
    with pytest.raises(ReusedAuthorizationError, match="already been used"):
        controller.transition(
            intent=intent,
            decision=result.decision,
            context=result.context,
            approval=None,
        )


def test_run_projection_rebuilds_the_authoritative_transition_state(tmp_path: Path) -> None:
    """A missing run.json is rebuilt from the transition event and retains the paused state."""
    store, run = _planning_store(tmp_path)
    result = _plane(store).handle(_intent(run, ActionKind.PAUSE_RUN))
    assert result.event is not None
    assert result.state is not None
    projection = tmp_path / "runs" / run.id / "run.json"
    projection.unlink()

    store.version(run.id)

    rebuilt = json.loads(projection.read_text(encoding="utf-8"))
    assert rebuilt["run_state"] == result.state.state.model_dump(mode="json")


HUMAN_REVIEWER = Actor(
    kind=ActorKind.HUMAN,
    id="ana.lopez@bank.es",
    role="model_risk_officer",
    authenticated=True,
)


def _approve(_request: ApprovalRequest) -> bool:
    """Approve as a named human: the Gate credits ``human`` only for a custom approver."""
    return True


def _recorded_review(
    store: LocalRunStore,
    run: Run,
    *,
    approved_by: Actor,
    rationale: str,
    event_actor: Actor | None = None,
) -> tuple[ActionIntent, PolicyDecision, AuthorizationContext, Approval]:
    """Record the Gate evidence for one human-reviewed intent and return its exact scope."""
    intent = _intent(run, ActionKind.REVIEW_FINDINGS, subject_kind="findings")
    engine = PolicyEngine(load_default_policy("base"))
    decision = engine.decide_action(
        run_id=run.id,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_type=intent.action_kind.value,
        payload=intent.payload,
    )
    store.append(
        run.id,
        EventType.POLICY_DECISION,
        Actor.system(),
        decision.to_json_dict(),
        expected_version=store.version(run.id),
        subject_id=decision.subject_id,
        causation_id=intent.id,
    )
    context = AuthorizationContext(
        id=new_id("authorization"),
        run_id=run.id,
        intent_id=intent.id,
        policy_decision_id=decision.id,
        policy_sha256=decision.policy_sha256,
        decision=AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
        subject_kind=intent.subject_kind,
        subject_id=intent.subject_id,
        action_kind=intent.action_kind,
        constraints={
            "expected_state_version": RunController(store).current_state(run.id).version,
            "transition": RunTransitionKind.WAIT_FOR_APPROVAL.value,
        },
        requires_approval=True,
        expires_at=NOW + timedelta(minutes=5),
    )
    context_hash = hash_authorization_context(context)
    approval = Approval(
        id=new_id("approval"),
        run_id=run.id,
        policy_decision_id=decision.id,
        authorization_context_sha256=context_hash,
        approved=True,
        approved_by=approved_by,
        rationale=rationale,
        approved_at=NOW,
    )
    store.append(
        run.id,
        EventType.HUMAN_APPROVAL,
        event_actor or approved_by,
        approval.to_json_dict(),
        expected_version=store.version(run.id),
        subject_id=context.subject_id,
        authorization_context_sha256=context_hash,
    )
    return intent, decision, context, approval


def test_control_plane_authorizes_an_approval_from_a_human_with_an_exact_id(
    tmp_path: Path,
) -> None:
    """A human approver's exact id stays on the private canonical log and authorizes the context."""
    store, run = _planning_store(tmp_path)
    plane = _plane(store, approver=_approve, human=HUMAN_REVIEWER)

    result = plane.handle(_intent(run, ActionKind.REVIEW_FINDINGS, subject_kind="findings"))

    assert result.approval is not None
    assert result.approval.approved_by.id == "ana.lopez@bank.es"
    assert result.event is not None
    assert result.state is not None
    assert result.state.condition.value == "waiting"
    assert result.state.wait_reason is not None
    assert result.state.wait_reason.value == "approval"
    approvals = [event for event in store.events(run.id) if event.type is EventType.HUMAN_APPROVAL]
    assert approvals[-1].payload["approved_by"]["id"] == "ana.lopez@bank.es"


def test_controller_matches_a_gate_approval_whose_rationale_is_exact(tmp_path: Path) -> None:
    """An exact approval rationale stays verifiable on the private canonical log."""
    store, run = _planning_store(tmp_path)
    intent, decision, context, approval = _recorded_review(
        store,
        run,
        approved_by=Actor(kind=ActorKind.HUMAN, id="reviewer-7", authenticated=True),
        rationale="Confirmed with the model owner on +34 611 222 333.",
    )
    controller = RunController(store, now=lambda: NOW)

    event, state = controller.transition(
        intent=intent,
        decision=decision,
        context=context,
        approval=approval,
    )

    assert event.type is EventType.RUN_TRANSITIONED
    assert state.condition.value == "waiting"
    recorded = [e for e in store.events(run.id) if e.type is EventType.HUMAN_APPROVAL][-1]
    assert recorded.payload["rationale"] == "Confirmed with the model owner on +34 611 222 333."
    assert approval.rationale is not None
    assert "+34 611 222 333" in approval.rationale


def test_controller_rejects_an_approval_whose_event_actor_does_not_match(
    tmp_path: Path,
) -> None:
    """A payload naming an authenticated human cannot override the event envelope's actor."""
    store, run = _planning_store(tmp_path)
    approved_by = Actor(kind=ActorKind.HUMAN, id="reviewer-7", authenticated=True)
    intent, decision, context, approval = _recorded_review(
        store,
        run,
        approved_by=approved_by,
        event_actor=Actor.system(),
        rationale="Confirmed.",
    )

    with pytest.raises(AuthorizationScopeError, match="matching Gate record"):
        RunController(store, now=lambda: NOW).transition(
            intent=intent,
            decision=decision,
            context=context,
            approval=approval,
        )


def test_controller_rejects_an_approval_from_a_declared_human(
    tmp_path: Path,
) -> None:
    """A self-declared human cannot authorize a control-plane transition."""
    store, run = _planning_store(tmp_path)
    declared = Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False)
    intent, decision, context, approval = _recorded_review(
        store,
        run,
        approved_by=declared,
        rationale="Claimed.",
    )

    with pytest.raises(AuthorizationScopeError, match="authenticated human"):
        RunController(store, now=lambda: NOW).transition(
            intent=intent,
            decision=decision,
            context=context,
            approval=approval,
        )


def test_controller_rejects_an_approval_that_has_no_matching_gate_record(tmp_path: Path) -> None:
    """An approval body that was never recorded is refused even with a valid context hash."""
    store, run = _planning_store(tmp_path)
    intent, decision, context, approval = _recorded_review(
        store,
        run,
        approved_by=HUMAN_REVIEWER,
        rationale="Reviewed by the model risk committee.",
    )
    controller = RunController(store, now=lambda: NOW)
    forgeries = [
        approval.model_copy(update={"rationale": "Approved for immediate deployment."}),
        approval.model_copy(update={"id": new_id("approval")}),
    ]

    for forged in forgeries:
        with pytest.raises(AuthorizationScopeError, match="matching Gate record"):
            controller.transition(
                intent=intent,
                decision=decision,
                context=context,
                approval=forged,
            )
    assert controller.current_state(run.id).condition.value == "active"
