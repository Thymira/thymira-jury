"""The Gate: the single checkpoint that records every decision and every human answer.

It wraps the engine with the event log: ``policy.decision`` for every verdict, and for
``REQUIRE_HUMAN_REVIEW`` the pair ``human.approval_requested`` → ``human.approval``. The approver
is injectable (console, web, API, or the test helpers below), so the gate never changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from thymira.events import hash_authorization_context
from thymira.observability import score_run
from thymira.policies.engine import FAIL_SAFE_ESCALATION
from thymira.schemas import (
    ActionIntent,
    Actor,
    ActorKind,
    Approval,
    AuditFinding,
    AuthorizationContext,
    Decision,
    EventType,
    ExecutionConstraints,
    PolicyDecision,
    ThymiraModel,
    approval_names_decision,
    new_id,
)

if TYPE_CHECKING:
    from thymira.events import EventLog
    from thymira.policies.engine import PolicyEngine, SubjectKind
    from thymira.policies.models import RiskProfile, ToolCapability


class ApprovalRequest(ThymiraModel):
    """What a human sees when the gate asks for permission."""

    run_id: str
    decision: PolicyDecision
    summary: str = ""
    cost_so_far: dict[str, Any] | None = None
    request_payload: dict[str, Any] | None = None


Approver = Callable[[ApprovalRequest], bool]


def auto_approve(_request: ApprovalRequest) -> bool:
    """Approve everything. Tests and CI only — never wire it into a real run."""
    return True


def auto_reject(_request: ApprovalRequest) -> bool:
    """Reject everything. Tests only."""
    return False


_AUTOMATION = Actor(
    kind=ActorKind.SYSTEM, id="automation", role="test_automation", authenticated=True
)

_EXECUTION_CONSTRAINT_REVIEW_DISCLOSURE = (
    " Approval starts THY, and under this execution constraint every tool call still needs its "
    "own separate human approval before it runs."
)


def _execution_approval_summary(
    decision: PolicyDecision, summary: str, risk: RiskProfile | None = None
) -> str:
    """Describe what a human accepts at a start review: the classification, then constraints.

    When the start was escalated for the classification's uncertainty, the summary carries the
    classification itself (level, category, factors, missing information, confidence, review
    flag) so the human decides on content, not on a blank "accept"; the reasons for the
    uncertainty are already in the decision's own reason. The existing CLI and console surfaces
    print the summary as it is, so no interface changes.
    """
    if (
        risk is not None
        and decision.decision is Decision.REQUIRE_HUMAN_REVIEW
        and FAIL_SAFE_ESCALATION in decision.reason
    ):
        summary = (
            f"{summary}: risk {risk.risk_level} ({risk.activity_category}); "
            f"factors: {', '.join(risk.risk_factors) or 'none'}; "
            f"missing: {', '.join(risk.missing_information) or 'none'}; "
            f"confidence {risk.confidence:.2f}; needs_human_review={risk.needs_human_review}"
        )
    if decision.execution_constraints.requires_human_review:
        return f"{summary}{_EXECUTION_CONSTRAINT_REVIEW_DISCLOSURE}"
    return summary


def _requested_payload(
    decision: PolicyDecision,
    summary: str,
    cost_so_far: dict[str, Any] | None,
    details: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``human.approval_requested`` payload one recorded decision asks a human about.

    The caller's ``details`` -- the Tool Manager's tool, arguments and ``tool_intent_sha256`` --
    go in first so the Gate's own keys always win: a caller can describe the call a human is
    shown, never restate which decision is being answered. ``cost_so_far`` is omitted rather than
    recorded as ``null`` when there is none.
    """
    return {
        **dict(details or {}),
        "decision_id": decision.id,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "summary": summary,
        **({"cost_so_far": cost_so_far} if cost_so_far is not None else {}),
    }


def _answering_actor(approver: Approver | None, human: Actor | None) -> tuple[bool, Actor]:
    """Name who answered an approval request, and whether that answer is automatic.

    An automatic answer is evidence, never authority, so the actor has to say so on the log: the
    test doubles and a Gate with no configured human identity both record the automation actor,
    and every fold that reads answers back (the tools module's ``human_answer``, MIRA's
    ``is_human_answer``) can refuse them on the actor alone, before believing any payload marker.
    """
    if approver in (auto_approve, auto_reject) or human is None:
        return True, _AUTOMATION
    if human.kind is not ActorKind.HUMAN or not human.authenticated:
        raise ValueError("approval requires an authenticated human actor")
    return False, human


class GateMode(StrEnum):
    """How the Gate resolves a ``REQUIRE_HUMAN_REVIEW`` decision.

    ``SYNCHRONOUS`` (the default) consults an injected ``approver`` in process and records its
    answer as evidence — never as authority. ``DEFERRED`` never consults an approver: it records
    the ``human.approval_requested`` and returns the unresolved decision, which an
    :class:`thymira.policies.approval.ApprovalService` resolves out of band later (HITL-01).
    """

    SYNCHRONOUS = "synchronous"
    DEFERRED = "deferred"


class Gate:
    """Decides through the engine and records the outcome on the run's event log."""

    def __init__(
        self,
        engine: PolicyEngine,
        log: EventLog,
        *,
        approver: Approver | None = None,
        human: Actor | None = None,
        mode: GateMode = GateMode.SYNCHRONOUS,
    ) -> None:
        if mode is GateMode.DEFERRED and approver is not None:
            raise ValueError(
                "a deferred Gate resolves approvals out of band and takes no in-process approver"
            )
        self.engine = engine
        self.log = log
        self.approver = approver
        self.human = human
        self.mode = mode

    def check_action(
        self,
        *,
        subject_kind: SubjectKind,
        subject_id: str,
        action_type: str,
        payload: dict[str, Any] | None = None,
        risk_level: str = "unknown",
        summary: str = "",
        cost_so_far: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Decide an action, record it, and run the approval flow when required."""
        decision = self.engine.decide_action(
            run_id=self.log.run_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            action_type=action_type,
            payload=payload,
            risk_level=risk_level,
        )
        return self._record(decision, summary or action_type, cost_so_far)

    def authorize_execution(
        self,
        risk: RiskProfile,
        capabilities: Sequence[ToolCapability],
        *,
        summary: str = "execution.start",
    ) -> PolicyDecision:
        """Record the single execution decision that Core must obtain before invoking THY."""
        decision = self.engine.decide_execution(
            run_id=self.log.run_id, risk=risk, capabilities=capabilities
        )
        return self._record(decision, _execution_approval_summary(decision, summary, risk), None)

    def check_intent(self, intent: ActionIntent) -> PolicyDecision:
        """Evaluate and record one bounded intent without granting it an effect.

        The returned decision still needs an authorization context and, when required, a separate
        approval before a control-plane caller may request a transition.
        """
        if intent.run_id != self.log.run_id:
            raise ValueError("intent.run_id must match the Gate event log run_id")
        decision = self.engine.decide_action(
            run_id=intent.run_id,
            subject_kind=intent.subject_kind,
            subject_id=intent.subject_id,
            action_type=intent.action_kind.value,
            payload=intent.payload,
        )
        self.log.append(
            EventType.POLICY_DECISION,
            Actor.system(),
            decision.to_json_dict(),
            subject_id=decision.subject_id,
            causation_id=intent.id,
        )
        return decision

    def request_approval(
        self,
        context: AuthorizationContext,
        decision: PolicyDecision,
        *,
        summary: str,
        cost_so_far: dict[str, Any] | None = None,
        request_payload: Mapping[str, Any] | None = None,
    ) -> Approval | None:
        """Record a scoped approval request and return a response when one is available.

        A non-``bool`` answer from the approver is no answer (Ruling R16): the request stays
        recorded and unanswered -- no ``human.approval`` is written, and this method returns
        ``None`` exactly as it does when there is no approver at all, or none required.
        """
        if context.run_id != self.log.run_id:
            raise ValueError("authorization context must match the Gate event log run_id")
        if context.policy_decision_id != decision.id:
            raise ValueError("authorization context must reference the policy decision")
        if not context.requires_approval:
            return None

        context_hash = hash_authorization_context(context)
        self.log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            {
                "decision_id": decision.id,
                "authorization_context_sha256": context_hash,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "summary": summary,
                **({"cost_so_far": cost_so_far} if cost_so_far is not None else {}),
                **(
                    {"request_payload": dict(request_payload)}
                    if request_payload is not None
                    else {}
                ),
            },
            subject_id=context.subject_id,
            authorization_context_sha256=context_hash,
        )
        if self.approver is None:
            return None

        request = ApprovalRequest(
            run_id=self.log.run_id,
            decision=decision,
            summary=summary,
            cost_so_far=cost_so_far,
            request_payload=dict(request_payload) if request_payload is not None else None,
        )
        answer = self.approver(request)
        if not isinstance(answer, bool):
            # Never coerced to yes, never fabricated as a rejection: a rejection is final for the
            # ticket and must come from a human, not from `bool("no")`.
            return None
        _, actor = _answering_actor(self.approver, self.human)
        approval = Approval(
            id=new_id("approval"),
            run_id=context.run_id,
            policy_decision_id=decision.id,
            authorization_context_sha256=context_hash,
            approved=answer,
            approved_by=actor,
        )
        self.log.append(
            EventType.HUMAN_APPROVAL,
            actor,
            approval.to_json_dict(),
            subject_id=context.subject_id,
            authorization_context_sha256=context_hash,
        )
        return approval

    def resolve_pending_approval(
        self,
        decision: PolicyDecision,
        *,
        summary: str = "human approval",
        cost_so_far: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> bool:
        """Record the human response to an approval request already emitted by this Gate.

        The policy decision and ``human.approval_requested`` event must already exist.  This
        method is the synchronous MVP seam used by the API: the injected ``approver`` supplies
        the submitted human answer, while the Gate remains the only component that writes the
        approval evidence.

        Both lookups go through :func:`~thymira.schemas.approval_names_decision`, because the
        decision id is recorded under two names: :meth:`request_approval` appends
        ``Approval.to_json_dict()`` (``policy_decision_id``) while this method and
        :meth:`_record` write ``decision_id``. Reading only the latter made an approval already
        given through the control plane invisible here, and the Gate answered it a second time --
        two contradictory answers to one decision on an append-only chain.

        A non-``bool`` answer from the approver is no answer (Ruling R16): rather than write a
        fabricated ``human.approval``, this method raises ``ValueError`` naming the malformed
        answer -- the same exception type every other invalid-state check here raises, and the
        one the API route already maps to a 409. Nothing is appended to the log, so the request
        stays pending for a real human to answer.
        """
        if decision.run_id != self.log.run_id:
            raise ValueError("policy decision must target the Gate Run")
        if decision.decision is not Decision.REQUIRE_HUMAN_REVIEW:
            raise ValueError("only a human-review decision can be resolved")
        if self.approver is None:
            raise RuntimeError("resolving an approval requires an injected approver")
        events = self.log.events()
        if not any(
            event.type is EventType.HUMAN_APPROVAL_REQUESTED
            and approval_names_decision(event.payload, decision.id)
            for event in events
        ):
            raise ValueError("policy decision has no pending approval request")
        if any(
            event.type is EventType.HUMAN_APPROVAL
            and approval_names_decision(event.payload, decision.id)
            for event in events
        ):
            raise ValueError("approval request has already been resolved")

        request = ApprovalRequest(
            run_id=self.log.run_id,
            decision=decision,
            summary=summary,
            cost_so_far=cost_so_far,
        )
        answer = self.approver(request)
        if not isinstance(answer, bool):
            msg = f"approver returned a non-bool answer: {answer!r}"
            raise ValueError(msg)  # noqa: TRY004  # ValueError like every sibling raise; the API maps it to 409
        automatic, actor = _answering_actor(self.approver, self.human)
        payload: dict[str, Any] = {
            "decision_id": decision.id,
            "approved": answer,
            "automatic": automatic,
        }
        if note is not None:
            payload["note"] = note
        self.log.append(
            EventType.HUMAN_APPROVAL,
            actor,
            payload,
            subject_id=decision.subject_id,
        )
        return answer

    def check_capability(
        self,
        *,
        subject_id: str,
        capability: ToolCapability,
        risk: RiskProfile,
        summary: str = "",
        cost_so_far: dict[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        inherited_constraints: ExecutionConstraints | None = None,
    ) -> PolicyDecision:
        """Decide whether a tool capability may be used, record it, and handle approval.

        ``details`` are the keys the Tool Manager puts on the ``human.approval_requested``
        payload -- the tool, its validated model-visible arguments and the call's
        ``tool_intent_sha256`` -- so the human answering sees the exact call and the answer is
        bound to it. They never reach the decision itself. ``inherited_constraints`` are the Run's
        authorised constraints, handed to the engine so a run-wide review reaches this very call.
        """
        decision = self.engine.decide_capability(
            run_id=self.log.run_id,
            subject_id=subject_id,
            capability=capability,
            risk=risk,
            inherited_constraints=inherited_constraints,
        )
        return self._record(decision, summary or capability.id, cost_so_far, details=details)

    def review_findings(
        self,
        findings: Sequence[AuditFinding],
        *,
        summary: str = "audit findings",
        cost_so_far: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Turn audit findings into the run-level decision, recorded like any other."""
        decision = self.engine.decide_findings(run_id=self.log.run_id, findings=findings)
        if decision.decision is Decision.BLOCK:
            self.log.append(
                EventType.AUDIT_BLOCK,
                Actor.system(),
                {
                    "decision_id": decision.id,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                },
                subject_id=decision.subject_id,
            )
        return self._record(decision, summary, cost_so_far)

    def escalate_rework(
        self,
        finding_ids: tuple[str, ...],
        *,
        reason: str,
        summary: str = "rework escalation",
        cost_so_far: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Record a policy-mandated human escalation for a refused rework."""
        decision = self.engine.decide_rework_escalation(
            run_id=self.log.run_id,
            finding_ids=finding_ids,
            reason=reason,
        )
        return self._record(decision, summary, cost_so_far)

    def check_budget(
        self,
        usage: Mapping[str, float | int | None],
        *,
        scope: str = "run",
        summary: str = "budget",
        cost_so_far: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Decide a usage snapshot against the budget ceilings and record the outcome.

        A soft ceiling records a ``WARNING`` and returns; crossing a hard ceiling records the
        ``REQUIRE_HUMAN_REVIEW`` and runs the Gate's approval flow (synchronous approver or, in
        deferred mode, a pending ``human.approval_requested`` for an ``ApprovalService`` to
        resolve out of band) — the same recorded path as every other decision.
        """
        decision = self.engine.decide_budget(run_id=self.log.run_id, usage=usage, scope=scope)
        return self._record(decision, summary, cost_so_far)

    def check_audit_freshness(
        self,
        *,
        fresh: bool,
        reason: str,
        summary: str = "audit freshness",
    ) -> PolicyDecision:
        """Record whether an audit snapshot may be reused by a lifecycle operation."""
        decision = self.engine.decide_audit_freshness(
            run_id=self.log.run_id, fresh=fresh, reason=reason
        )
        return self._record(decision, summary, None)

    def check_model(
        self,
        *,
        subject_id: str,
        role: str,
        model: str,
        tier: str | None = None,
        summary: str = "",
        cost_so_far: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Decide whether a selected model is allowed and record it; a substitution is a WARNING.

        A model choice is evidence, never an authorization: the WARNING for a substitution is
        recorded so it is never silent, but it does not gate an effect on its own.
        """
        decision = self.engine.decide_model(
            run_id=self.log.run_id, subject_id=subject_id, role=role, model=model, tier=tier
        )
        return self._record(decision, summary or model, cost_so_far)

    def _record(
        self,
        decision: PolicyDecision,
        summary: str,
        cost_so_far: dict[str, Any] | None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        """Record the decision and, for a review, request approval per the Gate's mode.

        A ``REQUIRE_HUMAN_REVIEW`` always emits ``human.approval_requested`` (carrying
        ``cost_so_far``). In ``DEFERRED`` mode the Gate then returns the unresolved decision — no
        approver is consulted — for an :class:`thymira.policies.approval.ApprovalService` to resolve
        out of band. In ``SYNCHRONOUS`` mode an injected approver's answer is recorded as evidence.
        ``details`` extend the request payload with caller facts; the Gate's own keys take
        precedence.

        A non-``bool`` answer from the approver is no answer (Ruling R16): no ``human.approval``
        is written, so the request stays recorded and unanswered and this decision stays pending
        for a real human to answer later. Never coerced to yes, never fabricated as a rejection.
        """
        self.log.append(
            EventType.POLICY_DECISION,
            Actor.system(),
            decision.to_json_dict(),
            subject_id=decision.subject_id,
        )
        # After the authoritative event, never instead of it: the score is a mirror that makes
        # "how often did we BLOCK this week" a query in Langfuse rather than a script over every
        # events.jsonl on disk. Nothing reads it back, and a backend that is off changes nothing.
        score_run(
            run_id=self.log.run_id,
            name="policy.decision",
            value=decision.decision.value,
            data_type="CATEGORICAL",
            comment=decision.rule_id,
        )
        if decision.decision is not Decision.REQUIRE_HUMAN_REVIEW:
            return decision
        request = ApprovalRequest(
            run_id=self.log.run_id, decision=decision, summary=summary, cost_so_far=cost_so_far
        )
        self.log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            _requested_payload(decision, summary, cost_so_far, details),
            subject_id=decision.subject_id,
        )
        if self.mode is GateMode.DEFERRED or self.approver is None:
            return decision
        answer = self.approver(request)
        if not isinstance(answer, bool):
            # Never coerced to yes, never fabricated as a rejection: a rejection is final for the
            # ticket and must come from a human, not from `bool("no")`. The request stays recorded
            # and unanswered, and this decision stays pending.
            return decision
        automatic, actor = _answering_actor(self.approver, self.human)
        self.log.append(
            EventType.HUMAN_APPROVAL,
            actor,
            {"decision_id": decision.id, "approved": answer, "automatic": automatic},
            subject_id=decision.subject_id,
        )
        return decision
