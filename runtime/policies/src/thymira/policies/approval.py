"""Async human-approval flow: the PendingApproval fold and the ApprovalService (HITL-01).

A ``REQUIRE_HUMAN_REVIEW`` decision is never authorized in process. In the Gate's deferred mode a
checkpoint records ``human.approval_requested`` and returns, leaving the decision pending; a human
resolves it later, out of band, through an :class:`ApprovalService`. The pending set is a pure fold
of the log — ``human.approval_requested`` minus valid ``human.approval`` events — so resume and
fork recover it for free (ADR-0005 idea 8). Only an authenticated human answer, or the explicit
system automation answer, retires a request; a declared human identity remains pending. Approval
evidence never enters a model transcript (ADR-0005 idea 7: approval events stay ``LOG_ONLY``). The
human's answer is recorded as evidence and returns the resolved
:class:`~thymira.schemas.PolicyDecision`; it is never itself an authorization, which remains the
deterministic Policy Engine's ``thymira.policies.allows_execution``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from thymira.policies.approval_scope import lifecycle_closure
from thymira.schemas import (
    ActorKind,
    Decision,
    EventSurface,
    EventType,
    Id,
    PolicyDecision,
    ThymiraModel,
    approval_decision_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.events import EventLog
    from thymira.schemas import Actor, Event


_TOOL_CALL_KEYS: tuple[str, ...] = ("tool", "arguments", "tool_intent_sha256")


class UnknownApprovalError(LookupError):
    """No pending approval matches the given run and decision id."""


class PendingApproval(ThymiraModel):
    """One unresolved human-review request, projected from the log.

    A pure fold value: it exists exactly while a ``human.approval_requested`` for ``decision_id``
    has no matching valid ``human.approval``. It carries no authority — it is a view an operator or
    the API reads to know what still needs a human. ``cost_so_far`` is whatever the checkpoint
    recorded when it asked (``None`` if it recorded nothing).
    """

    run_id: Id
    decision_id: Id
    subject_id: str | None = None
    rule_id: str = ""
    reason: str = ""
    summary: str = ""
    cost_so_far: dict[str, Any] | None = None
    requested_at: datetime
    tool_call: dict[str, Any] | None = None
    """The tool call the human is asked to approve -- ``tool``, its redacted ``arguments`` and
    the ``tool_intent_sha256`` the answer is bound to -- or ``None`` for a run-level review."""


def pending_approvals(events: Sequence[Event]) -> tuple[PendingApproval, ...]:
    """Fold the log into its unresolved human-review requests.

    The pending set is ``human.approval_requested`` minus ``human.approval``, matched by the
    decision id each side names (ADR-0005 idea 8). Both sides read
    :func:`~thymira.schemas.approval_decision_id`, because that id is recorded under two names --
    the frozen ``Approval.policy_decision_id`` on the control-plane path and the hand-built
    ``decision_id`` everywhere else -- and a fold that knew only one name reported approvals a
    human really gave as still pending. A decision id yields at most one entry: a second request
    for the same decision is the same pending approval, not a new one, and only a valid answer from
    an authenticated human or the system automation actor removes it. A payload that names no
    decision is not an identity: it neither becomes a pending entry nor resolves one.

    A request whose Run lifecycle closed it leaves the set as well
    (:func:`~thymira.policies.approval_scope.lifecycle_closure`): once Core has recorded a
    terminal command, or the end of the pass that raised the review, there is no longer a state
    a human can answer *for*, and :meth:`LocalApprovalService.resolve` inherits the refusal
    through its pending-set membership check -- the stale governance resolve. Only
    ``Actor.system()`` events produced by ``thymira.core`` whose payload agrees with itself close
    anything, so a transition a tool, an agent or a human wrote closes nothing, and neither does
    one whose serialised state, flattened copy, command or version disagree: ``producer`` is a
    free argument on ``EventLog.append``, so without that a forged payload would be an
    availability attack on every open review.

    Expiry is deliberately *not* applied here. A parked Run whose deadline had passed would be
    left with a review nobody may answer and no way forward; expiry is an authorization-boundary
    property, enforced where a credit is spent.

    Args:
        events: The run's events, in order.

    Returns:
        The unresolved requests, in the order they were first requested.
    """
    resolved = {
        decision_id
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and _closes_pending_approval(event)
        and (decision_id := approval_decision_id(event.payload)) is not None
    }
    pending: list[PendingApproval] = []
    seen: set[str] = set()
    for event in events:
        if event.type is not EventType.HUMAN_APPROVAL_REQUESTED:
            continue
        decision_id = approval_decision_id(event.payload)
        if decision_id is None or decision_id in resolved or decision_id in seen:
            continue
        seen.add(decision_id)
        if lifecycle_closure(events, after_seq=event.seq) is not None:
            continue
        pending.append(
            PendingApproval(
                run_id=event.run_id,
                decision_id=decision_id,
                subject_id=event.subject_id,
                rule_id=str(event.payload.get("rule_id", "")),
                reason=str(event.payload.get("reason", "")),
                summary=str(event.payload.get("summary", "")),
                cost_so_far=_as_cost(event.payload.get("cost_so_far")),
                requested_at=event.ts,
                tool_call=_tool_call(event.payload),
            )
        )
    return tuple(pending)


def needs_human(decision: PolicyDecision) -> bool:
    """Whether a deferred Gate left this decision for an out-of-band human resolution.

    ``True`` exactly for ``REQUIRE_HUMAN_REVIEW``: the decision has no in-process authority, and an
    :class:`ApprovalService` must resolve it before a caller may proceed.

    Args:
        decision: The deterministic decision a checkpoint recorded.

    Returns:
        ``True`` if the decision still needs a human answer.
    """
    return decision.decision is Decision.REQUIRE_HUMAN_REVIEW


class ApprovalService(Protocol):
    """Resolve deferred human-review decisions out of band.

    The MVP resolves in process over the local event log (:class:`LocalApprovalService`); FINAL is a
    Postgres-backed service driving LangGraph ``interrupt()``/resume. Both satisfy this protocol
    unchanged, so the API and CLI approve path never change (ADR-0005 idea 7; roadmap day 10).
    """

    def pending(self, run_id: str) -> tuple[PendingApproval, ...]:
        """Return every unresolved human-review request for ``run_id``, in request order."""
        ...

    def resolve(
        self,
        run_id: str,
        decision_id: str,
        *,
        approved: bool,
        by: Actor,
        rationale: str | None = None,
    ) -> PolicyDecision:
        """Record a human's answer to one pending decision and return that decision.

        Appends exactly one ``LOG_ONLY`` ``human.approval`` event for ``decision_id`` and leaves the
        rest of the chain untouched. The answer is evidence, never authorization: authorization
        stays ``thymira.policies.allows_execution`` over the returned decision and this approval.

        Args:
            run_id: The run the decision belongs to.
            decision_id: The pending decision to resolve.
            approved: The human's answer.
            by: The authenticated human recording the answer.
            rationale: Optional free-text reason, recorded on the approval event.

        Returns:
            The resolved :class:`~thymira.schemas.PolicyDecision`.

        Raises:
            UnknownApprovalError: The run has no pending approval for ``decision_id``.
        """
        ...


class LocalApprovalService:
    """An :class:`ApprovalService` backed by one run's local event log.

    The MVP keeps Run history in a single append-only event log per run (ADR-0010: no database),
    so this service serves exactly that run and refuses any other ``run_id``.
    """

    def __init__(self, log: EventLog) -> None:
        """Bind the service to one run's authoritative event log.

        Args:
            log: The run's hash-chained event log; its ``run_id`` is the only run served.
        """
        self._log = log

    @property
    def run_id(self) -> str:
        """The run this service resolves approvals for."""
        return self._log.run_id

    def pending(self, run_id: str) -> tuple[PendingApproval, ...]:
        """Return every unresolved human-review request for ``run_id`` (this service's run)."""
        self._require_run(run_id)
        return pending_approvals(self._log.events())

    def resolve(
        self,
        run_id: str,
        decision_id: str,
        *,
        approved: bool,
        by: Actor,
        rationale: str | None = None,
    ) -> PolicyDecision:
        """Record the human answer to one pending decision and return that decision.

        Args:
            run_id: The run the decision belongs to; must be this service's run.
            decision_id: The pending decision to resolve.
            approved: The human's answer.
            by: The human (or delegated actor) recording the answer.
            rationale: Optional free-text reason, recorded on the approval event.

        Returns:
            The resolved :class:`~thymira.schemas.PolicyDecision`.

        Raises:
            UnknownApprovalError: The run has no pending approval for ``decision_id``.
        """
        self._require_run(run_id)
        if by.kind is not ActorKind.HUMAN or not by.authenticated:
            raise ValueError("approval requires an authenticated human actor")
        if not isinstance(approved, bool):
            raise TypeError("approval answer must be a bool")
        events = self._log.events()
        if decision_id not in {approval.decision_id for approval in pending_approvals(events)}:
            raise UnknownApprovalError(
                f"run {run_id!r} has no pending approval for decision {decision_id!r}"
            )
        decision = decision_from_events(events, decision_id)
        payload: dict[str, Any] = {"decision_id": decision_id, "approved": bool(approved)}
        if rationale is not None:
            payload["rationale"] = rationale
        self._log.append(
            EventType.HUMAN_APPROVAL,
            by,
            payload,
            subject_id=decision.subject_id,
            surface=EventSurface.LOG_ONLY,
        )
        return decision

    def _require_run(self, run_id: str) -> None:
        if run_id != self._log.run_id:
            raise ValueError(
                f"run {run_id!r} is not served by this LocalApprovalService "
                f"(serves {self._log.run_id!r})"
            )


def _closes_pending_approval(event: Event) -> bool:
    """Whether an approval event closes its pending request in the public fold.

    Automatic answers are written by the system actor and close the request without granting
    human authority. A human answer closes it only when the event carries authenticated human
    provenance; a declared but unauthenticated human must remain pending. Other actor kinds are
    not approval sources.
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


def decision_from_events(events: Sequence[Event], decision_id: str) -> PolicyDecision:
    """Rebuild the recorded :class:`PolicyDecision` for ``decision_id`` from its own event.

    Args:
        events: The run's events, in order.
        decision_id: The decision to rebuild.

    Returns:
        The :class:`~thymira.schemas.PolicyDecision` the ``policy.decision`` event recorded.

    Raises:
        UnknownApprovalError: No such event exists, or its payload no longer validates.
    """
    for event in events:
        if event.type is not EventType.POLICY_DECISION or event.payload.get("id") != decision_id:
            continue
        try:
            return PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError) as exc:
            raise UnknownApprovalError(
                f"policy.decision event for {decision_id!r} is invalid: {exc}"
            ) from exc
    raise UnknownApprovalError(f"no policy.decision event for decision {decision_id!r}")


def _tool_call(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The tool-call facts a Tool Manager request carries, or ``None`` for a run-level one."""
    if not isinstance(payload.get("tool"), str):
        return None
    return {key: payload[key] for key in _TOOL_CALL_KEYS if key in payload}


def _as_cost(value: object) -> dict[str, Any] | None:
    """Return a recorded ``cost_so_far`` mapping, or ``None`` when none was recorded."""
    return value if isinstance(value, dict) else None
