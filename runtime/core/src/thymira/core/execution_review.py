"""Persisted routing and human-answer checks for execution-start reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.core.governance_binding import policy_decision_binding
from thymira.core.run_state import RunTransitionKind
from thymira.policies import FAIL_SAFE_ESCALATION, auto_approve, auto_reject
from thymira.schemas import (
    Actor,
    ActorKind,
    Decision,
    EventType,
    PolicyDecision,
    approval_decision_id,
    approval_names_decision,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.core.control_plane import RunController
    from thymira.core.graph.protocol import SubgraphDeps
    from thymira.core.graph.state import RuntimeState
    from thymira.policies import Gate, RiskProfile
    from thymira.schemas import Event


EXECUTION_START_RESUME_TARGET = "execution_start"

INTERVIEW_LIMIT_RESUME_TARGET = "interview_limit"
"""The park target of the bounded risk interview's question-limit review.

The interview parks the Run before it ever reaches the Gate, so its approval must re-enter the
interview body rather than the Gate's outgoing edge -- which would demand a policy decision the
Run never produced (`InlineDispatcher._resume_node`).
"""

RISK_UNCERTAINTY_RESUME_TARGET = "risk_uncertainty"
"""The park target of MIRA preflight's bounded risk-uncertainty review.

Mirrors ``INTERVIEW_LIMIT_RESUME_TARGET``: MIRA's own inherent-risk classification can keep
landing under ``RISK_CONFIDENCE_THRESHOLD`` for an otherwise-complete activity profile, and a
plain ``resume`` re-attempting the identical model judgement has no way to change the outcome
(bug-hunt: preflight-information-park-is-unrecoverable). After
``thymira.core.graph.adapters.MAX_RISK_UNCERTAINTY_ATTEMPTS`` uncertain judgements on the same
activity-profile version, the Run parks here instead, and an approval re-enters preflight rather
than the Gate's outgoing edge, for the same reason.
"""

_KNOWN_RESUME_TARGETS = frozenset(
    {EXECUTION_START_RESUME_TARGET, INTERVIEW_LIMIT_RESUME_TARGET, RISK_UNCERTAINTY_RESUME_TARGET}
)
"""Every target a park may persist; anything else is a corrupted or forward-dated log."""


def execution_gate_state(
    state: RuntimeState,
    deps: SubgraphDeps,
    gate: Callable[[RuntimeState, SubgraphDeps], RuntimeState],
) -> RuntimeState:
    """Reuse a persisted start review after checkpoint lag, or evaluate the Gate once."""
    persisted = persisted_execution_start_decision(deps.event_log.events(), state.run_id)
    if persisted is None:
        return gate(state, deps)
    return state.model_copy(
        update={
            "execution_decision": persisted,
            "execution_constraints": persisted.execution_constraints,
        }
    )


def apply_execution_decision(
    controller: RunController,
    run_id: str,
    decision: PolicyDecision,
    events: Sequence[Event],
) -> None:
    """Persist the lifecycle consequence of a non-executable start decision."""
    current = controller.current_state(run_id)
    if current.condition.value in {"terminal", "waiting"}:
        return
    if decision.decision is Decision.BLOCK:
        controller.advance(
            run_id, RunTransitionKind.BLOCK, payload=policy_decision_binding(decision)
        )
    elif decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
        outcome = human_approval_outcome(events, decision)
        if outcome is False:
            controller.advance(
                run_id,
                RunTransitionKind.BLOCK,
                payload=policy_decision_binding(decision),
            )
        elif outcome is None:
            controller.park_for_review(
                run_id,
                decision,
                resume_from=EXECUTION_START_RESUME_TARGET,
            )


def persist_execution_gate_state(
    controller: RunController | None, state: RuntimeState, deps: SubgraphDeps
) -> None:
    """Persist the lifecycle effect of one execution Gate node result."""
    if controller is None or state.execution_decision is None:
        return
    apply_execution_decision(
        controller,
        state.run_id,
        state.execution_decision,
        deps.event_log.events(),
    )


def validate_execution_resolver(gate: Gate, actor: object) -> None:
    """Require one authenticated human and a matching nonautomatic Gate resolver."""
    if not isinstance(actor, Actor) or actor.kind is not ActorKind.HUMAN:
        raise ValueError("execution approval requires a declared human actor")
    if not actor.authenticated:
        raise ValueError("execution approval requires an authenticated human actor")
    if gate.human != actor or gate.approver is None or gate.approver in (auto_approve, auto_reject):
        raise ValueError("execution approval Gate must represent the declared human actor")


def execution_decision_allows_thy(
    controller: RunController | None,
    run_id: str,
    events: Sequence[Event],
    decision: PolicyDecision | None,
) -> bool:
    """Return whether lifecycle state and the preserved decision release THY."""
    if controller is not None and controller.current_state(run_id).condition.value != "active":
        return False
    if decision is None:
        raise RuntimeError("execution Gate did not record an execution decision")
    if decision.decision in (Decision.PASS, Decision.WARNING):
        return True
    if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
        return human_approval_outcome(events, decision) is True
    return False


def execution_state_allows_thy(
    controller: RunController | None, state: RuntimeState, deps: SubgraphDeps
) -> bool:
    """Evaluate execution routing from the graph state and authoritative lifecycle."""
    return execution_decision_allows_thy(
        controller,
        state.run_id,
        deps.event_log.events(),
        state.execution_decision,
    )


def continuation_target(events: Sequence[Event], parked: PolicyDecision | None) -> str:
    """Return the dispatcher target for the exact parked review, including legacy parks."""
    if parked is None:
        return "approval"
    target = review_resume_target(events, parked.id)
    if target is not None:
        return target
    return "execution" if parked.subject_kind == "tool_call" else "approval"


def review_resume_target(events: Sequence[Event], decision_id: str) -> str | None:
    """Return the resume target persisted with the park for an exact decision."""
    for event in reversed(events):
        if event.type is not EventType.RUN_TRANSITIONED:
            continue
        if event.payload.get("command") != "wait_for_approval":
            continue
        decision = event.payload.get("policy_decision")
        if not isinstance(decision, dict) or decision.get("id") != decision_id:
            continue
        target = event.payload.get("resume_from")
        if target is None:
            return None
        if target not in _KNOWN_RESUME_TARGETS:
            raise ValueError(f"unsupported approval resume target: {target!r}")
        return target
    return None


def human_approval_outcome(events: Sequence[Event], decision: PolicyDecision) -> bool | None:
    """Return an explicitly nonautomatic human answer for one exact decision."""
    outcomes = [
        event.payload["approved"]
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and event.run_id == decision.run_id
        and event.subject_id == decision.subject_id
        and event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
        and approval_names_decision(event.payload, decision.id)
        and isinstance(event.payload.get("approved"), bool)
    ]
    if False in outcomes:
        return False
    return True if outcomes else None


def reviewed_risk_profile(
    risk: RiskProfile, decision: PolicyDecision | None, events: Sequence[Event]
) -> RiskProfile:
    """Mark the profile as reviewed when a human approved its uncertainty escalation at start.

    Pure: reads the same evidence :func:`human_approval_outcome` reads, so "human" means an
    authenticated, non-automatic actor answering exactly this Run-level decision. The mark is
    set only for the ``execution.start`` decision that was escalated with the fail-safe phrase;
    an action-rule review, a tool-call review, a rejection, an automatic answer or a decision
    for another Run leave the profile untouched, so the per-call escalation stays in force.
    """
    if (
        decision is None
        or risk.reviewed_decision_id is not None
        or decision.run_id != decision.subject_id
        or decision.subject_kind != "run"
        or decision.decision is not Decision.REQUIRE_HUMAN_REVIEW
        or FAIL_SAFE_ESCALATION not in decision.reason
        or human_approval_outcome(events, decision) is not True
    ):
        return risk
    return risk.model_copy(update={"reviewed_decision_id": decision.id})


def human_rejection_actor(events: Sequence[Event], decision: PolicyDecision) -> Actor | None:
    """Return the actor on the first valid human rejection for one exact decision."""
    return next(
        (
            event.actor
            for event in events
            if event.type is EventType.HUMAN_APPROVAL
            and event.run_id == decision.run_id
            and event.subject_id == decision.subject_id
            and event.actor.kind is ActorKind.HUMAN
            and event.actor.authenticated
            and ("automatic" not in event.payload or event.payload["automatic"] is False)
            and approval_names_decision(event.payload, decision.id)
            and event.payload.get("approved") is False
        ),
        None,
    )


def block_rejected_execution(
    controller: RunController,
    run_id: str,
    events: Sequence[Event],
    decision: PolicyDecision,
    resumed_by: Actor,
) -> None:
    """Block a rejected start while preserving rejector and continuation attribution."""
    rejector = human_rejection_actor(events, decision)
    if rejector is None:
        raise ValueError("execution rejection has no valid human actor")
    controller.advance(
        run_id,
        RunTransitionKind.BLOCK,
        payload={
            "rejected_by": rejector.to_json_dict(),
            "resumed_by": resumed_by.to_json_dict(),
            **policy_decision_binding(decision),
        },
    )


def pending_review_decision(events: Sequence[Event], run_id: str) -> PolicyDecision | None:
    """Return the latest unresolved human-review decision in a verified Run log."""
    resolved = {
        decision_id
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and _closes_review(event)
        and (decision_id := approval_decision_id(event.payload)) is not None
    }
    for event in reversed(events):
        if event.type is not EventType.POLICY_DECISION:
            continue
        try:
            decision = PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"run {run_id}: policy.decision event {event.event_id} is invalid"
            ) from exc
        if decision.decision is Decision.REQUIRE_HUMAN_REVIEW and decision.id not in resolved:
            return decision
    return None


def _closes_review(event: Event) -> bool:
    """Whether one approval event closes a pending review without granting authority.

    System answers are the Gate's automatic terminal response and therefore retire a request, but
    they never satisfy :func:`human_approval_outcome`. A human answer must carry authenticated
    provenance; a declared human identity is evidence of neither approval nor rejection.
    """
    if not isinstance(event.payload.get("approved"), bool):
        return False
    if event.actor.kind is ActorKind.SYSTEM:
        return True
    return (
        event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
    )


def parked_review_decision(events: Sequence[Event], run_id: str) -> PolicyDecision | None:
    """Return the decision named by the Run's latest approval park transition."""
    for event in reversed(events):
        if (
            event.type is not EventType.RUN_TRANSITIONED
            or event.payload.get("command") != "wait_for_approval"
        ):
            continue
        payload = event.payload.get("policy_decision")
        if not isinstance(payload, dict):
            return None
        try:
            return PolicyDecision.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"run {run_id}: transition event {event.event_id} names an invalid decision"
            ) from exc
    return None


def persisted_execution_start_decision(
    events: Sequence[Event], run_id: str
) -> PolicyDecision | None:
    """Return the exact execution-start decision persisted before a checkpoint-lag redelivery."""
    decision = parked_review_decision(events, run_id)
    if (
        decision is None
        or review_resume_target(events, decision.id) != EXECUTION_START_RESUME_TARGET
    ):
        return None
    if decision.run_id != run_id or decision.subject_kind != "run" or decision.subject_id != run_id:
        raise ValueError("persisted execution-start decision does not match the Run")
    return decision


__all__ = [
    "EXECUTION_START_RESUME_TARGET",
    "INTERVIEW_LIMIT_RESUME_TARGET",
    "RISK_UNCERTAINTY_RESUME_TARGET",
    "apply_execution_decision",
    "block_rejected_execution",
    "continuation_target",
    "execution_decision_allows_thy",
    "execution_gate_state",
    "execution_state_allows_thy",
    "human_approval_outcome",
    "human_rejection_actor",
    "parked_review_decision",
    "pending_review_decision",
    "persist_execution_gate_state",
    "persisted_execution_start_decision",
    "review_resume_target",
    "reviewed_risk_profile",
    "validate_execution_resolver",
]
