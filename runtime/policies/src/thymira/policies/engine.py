"""The Policy Engine: deterministic decisions from declarative rules.

The LLM never calls this with a decision in mind; it supplies facts (an action, a tool
capability, audit findings) and the engine answers ``PASS | WARNING | REQUIRE_HUMAN_REVIEW |
BLOCK``, naming the rule and the policy snapshot hash so the decision can be replayed later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    ActorKind,
    Approval,
    AuditFinding,
    Decision,
    ExecutionAction,
    ExecutionConstraints,
    PolicyDecision,
    new_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.policies.models import Policy, RiskProfile, ToolCapability

SubjectKind = Literal["run", "task", "tool_call", "findings"]

FAIL_SAFE_ESCALATION = "Fail-safe escalation"
"""The phrase every uncertainty escalation carries in its reason, per call and at start."""


class _DecidingRule(Protocol):
    """What every ordered rule has in common: what it is, why, and what it decides."""

    @property
    def id(self) -> str: ...

    @property
    def reason(self) -> str: ...

    @property
    def decision(self) -> Decision: ...


def _strictest[RuleT: _DecidingRule](matching: Sequence[RuleT]) -> tuple[RuleT | None, str]:
    """Return the highest-precedence match and a note naming the matches it outranks.

    Rule order must never decide on its own. A permissive rule listed ahead of a stricter one
    would shadow it and silently de-escalate the decision, which is the single thing a fail-safe
    engine may not do -- and because ``Policy.merged_with`` *prepends* an overlay, the shadowing
    rule is exactly the one a project can add. Ties fall back to policy order (``max`` keeps the
    first maximum), so an overlay still decides ahead of the base rule it tightens. What the
    winner outranks is named rather than dropped, so a replayed decision (control A16) shows what
    the engine considered and discarded.

    Args:
        matching: Every rule that covers the subject, in policy order.

    Returns:
        The deciding rule (``None`` when nothing matched) and a sentence to append to its reason,
        empty when it outranked nothing.
    """
    rule = max(matching, key=lambda candidate: candidate.decision.precedence, default=None)
    if rule is None:
        return None, ""
    shadowed = ", ".join(
        f"{other.id} ({other.decision.value})" for other in matching if other is not rule
    )
    return rule, f" Also matched: {shadowed}." if shadowed else ""


def policy_sha256(policy: Policy) -> str:
    """Content hash of a policy; identical rules always give the identical hash."""
    return sha256_text(canonical_json(policy.to_json_dict()))


def uncertainty_reasons(risk: RiskProfile, minimum_confidence: float) -> list[str]:
    """Name every way a risk classification is too thin to leave a permissive outcome standing.

    A transformation over the classification and one policy threshold, so it is a function: the
    engine reads nothing else to decide it, and a caller can hold the same list against the same
    profile without building an engine. An empty list means the classification is solid enough
    for the rule's own verdict to stand.
    """
    reasons: list[str] = []
    if risk.risk_level == "unknown":
        reasons.append("unknown risk level")
    if risk.activity_category == "unknown":
        reasons.append("unknown activity category")
    if risk.missing_information:
        reasons.append("incomplete risk information")
    if risk.confidence < minimum_confidence:
        reasons.append("classification confidence below the policy threshold")
    if risk.needs_human_review:
        reasons.append("classification flagged for human review")
    return reasons


def effective_uncertainty(risk: RiskProfile, minimum_confidence: float) -> list[str]:
    """The uncertainty a decision must still escalate for: none once a human reviewed it.

    :func:`uncertainty_reasons` stays pure and lists everything; this is what the engine escalates
    on. A profile carrying ``reviewed_decision_id`` was answered once, at ``execution.start``, by
    an authenticated human who saw the classification, so the same reasons are not asked again on
    every call. Nothing else is skipped: a rule's own review, a run-wide review constraint and an
    allow-list conflict still decide exactly as before.
    """
    if risk.reviewed_decision_id is not None:
        return []
    return uncertainty_reasons(risk, minimum_confidence)


_ALLOW_LIST_CONFLICT = (
    " Execution constraints conflict: the tool's allow-list shares no tool with the run-wide "
    "allow-list."
)


def allow_lists_conflict(derived: ExecutionConstraints, inherited: ExecutionConstraints) -> bool:
    """Whether two allow-lists narrow the surface to nothing, which a merge cannot express.

    :meth:`~thymira.schemas.ExecutionConstraints.merged_with` intersects two non-empty allow-lists,
    and an empty intersection is indistinguishable from an allow-list that narrows nothing at all:
    the merge of two disjoint allow-lists reads back as "every tool is allowed", the exact opposite
    of what the two rules said. The conflict is therefore detected here, before the merge, so the
    decision can answer it instead of recording a surface neither rule authorised.
    """
    if not derived.allowed_tools or not inherited.allowed_tools:
        return False
    return not set(derived.allowed_tools) & set(inherited.allowed_tools)


def escalate_capability_decision(
    decision: Decision,
    reason: str,
    *,
    uncertainty: Sequence[str],
    constraints: ExecutionConstraints,
    allow_list_conflict: bool = False,
) -> tuple[Decision, str]:
    """Raise a permissive capability outcome, on any of three grounds; never lower one.

    ``allow_list_conflict`` is the strictest and decides first: no tool satisfies both allow-lists
    at once, so no call under them can be authorised at all and the outcome is ``BLOCK``. A rule
    that already decided ``BLOCK`` keeps its own reason -- it refused for a reason of its own, and
    restating the conflict would bury it.

    Otherwise only ``PASS`` and ``WARNING`` are ever raised. A ``BLOCK`` is returned untouched -- a
    prohibition never becomes a question for a human -- and a rule that already decided
    ``REQUIRE_HUMAN_REVIEW`` keeps its own reason rather than collecting a second sentence saying
    the same thing. An uncertain classification is the first of the two review grounds and states
    what is uncertain; ``constraints`` demanding a human before any call are the second, and it is
    appended only when the decision was still permissive, so the two never stack on one outcome.

    Returns:
        The decision to record and the reason that explains it.
    """
    if allow_list_conflict:
        if decision is Decision.BLOCK:
            return decision, reason
        return Decision.BLOCK, f"{reason}{_ALLOW_LIST_CONFLICT}"
    if decision in (Decision.PASS, Decision.WARNING) and uncertainty:
        decision = Decision.REQUIRE_HUMAN_REVIEW
        reason = f"{reason} {FAIL_SAFE_ESCALATION}: {', '.join(uncertainty)}."
    if constraints.requires_human_review and decision in (Decision.PASS, Decision.WARNING):
        decision = Decision.REQUIRE_HUMAN_REVIEW
        reason = f"{reason} Execution constraints require human review."
    return decision, reason


@dataclass(frozen=True, slots=True)
class CapabilityEvaluation:
    """What the policy says about one capability, before anything is decided or recorded.

    Everything :meth:`PolicyEngine.decide_capability` puts on the record except the identity of
    the decision itself: the same rule resolution, the same escalations, and the exact constraints
    the decision would persist. It exists so a caller holding an already-answered decision can ask
    what the engine would decide *now* -- the only honest test of whether that answer still
    applies -- without minting a decision nobody asked for, and without re-deriving the rules
    itself and reaching a different answer than the engine.
    """

    decision: Decision
    rule_id: str
    reason: str
    execution_constraints: ExecutionConstraints


def derive_execution_constraints(
    policy: Policy, risk: RiskProfile, capabilities: Sequence[ToolCapability]
) -> ExecutionConstraints:
    """Combine only the typed constraints of rules applicable to available capabilities."""
    constraints = ExecutionConstraints()
    for capability in capabilities:
        for rule in policy.capability_rules:
            if rule.matches(risk, capability):
                constraints = constraints.merged_with(rule.execution_constraints)
    return constraints


def evaluate_capability(
    policy: Policy,
    capability: ToolCapability,
    risk: RiskProfile,
    inherited_constraints: ExecutionConstraints | None = None,
) -> CapabilityEvaluation:
    """Resolve one capability against a policy: the rule, the escalations and the constraints.

    Pure and recording nothing, so it is safe to ask repeatedly -- to decide a call, and again to
    test whether an answer given to an earlier decision still describes what would be decided
    today. The strictest matching rule wins (:func:`_strictest`), the outcome is escalated by
    :func:`escalate_capability_decision`, and the constraints returned are the merge of the
    capability's own rules with the Run's inherited ones: exactly what the decision persists.
    """
    matching = [rule for rule in policy.capability_rules if rule.matches(risk, capability)]
    rule, shadowed = _strictest(matching)
    if rule is None:
        decision, rule_id, reason = (
            policy.capability_default_decision,
            "capability_default",
            policy.capability_default_reason,
        )
    else:
        decision, rule_id, reason = rule.decision, rule.id, f"{rule.reason}{shadowed}"
    derived = derive_execution_constraints(policy, risk, (capability,))
    inherited = inherited_constraints or ExecutionConstraints()
    constraints = derived.merged_with(inherited)
    decision, reason = escalate_capability_decision(
        decision,
        reason,
        uncertainty=effective_uncertainty(risk, policy.minimum_risk_confidence),
        constraints=constraints,
        allow_list_conflict=allow_lists_conflict(derived, inherited),
    )
    return CapabilityEvaluation(decision, rule_id, reason, constraints)


class PolicyEngine:
    """Evaluates one policy. Stateless apart from the policy it was built with."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    @property
    def policy_sha256(self) -> str:
        """Content hash of the policy **as it stands now**, recomputed for every decision.

        A cached hash would pin what the engine was built with, not what it evaluated. Rule
        models are frozen, but the mappings inside them (``payload_equals``,
        ``payload_contains``) are ordinary dicts that can still be mutated in place, so a caller
        holding the shared policy could flip a rule from BLOCK to PASS while every recorded
        decision kept quoting the original digest — and restoring it afterwards would leave no
        trace at all. Recomputing keeps `PolicyDecision.policy_sha256` an honest record of what
        actually decided, which is the whole point of pinning it (control A16).
        """
        return policy_sha256(self.policy)

    # ------------------------------------------------------------------ actions
    def decide_action(
        self,
        *,
        run_id: str,
        subject_kind: SubjectKind,
        subject_id: str,
        action_type: str,
        payload: dict[str, Any] | None = None,
        risk_level: str = "unknown",
    ) -> PolicyDecision:
        """The strictest matching action rule wins; no match falls back to the policy default.

        Resolution is :func:`_strictest`, the same as :meth:`decide_capability`. Taking the first
        match instead let a project overlay adding ``deploy_model -> PASS`` outrank the hard
        ``BLOCK`` the shipped EU AI Act rule carries, and the shadowed prohibition never reached
        the record.
        """
        payload = payload or {}
        matching = [
            rule
            for rule in self.policy.action_rules
            if rule.matches(action_type, payload, risk_level)
        ]
        rule, shadowed = _strictest(matching)
        if rule is None:
            return self._decision(
                run_id,
                subject_kind,
                subject_id,
                self.policy.default_decision,
                "default",
                self.policy.default_reason,
            )
        return self._decision(
            run_id, subject_kind, subject_id, rule.decision, rule.id, f"{rule.reason}{shadowed}"
        )

    # ------------------------------------------------------------- capabilities
    def decide_capability(
        self,
        *,
        run_id: str,
        subject_id: str,
        capability: ToolCapability,
        risk: RiskProfile,
        inherited_constraints: ExecutionConstraints | None = None,
    ) -> PolicyDecision:
        """The highest-precedence matching rule wins; no match falls back to the capability default.

        Overlapping rules resolve exactly as they do in :meth:`decide_findings`: by
        ``Decision.precedence`` (``BLOCK`` > ``REQUIRE_HUMAN_REVIEW`` > ``WARNING`` > ``PASS``),
        ties breaking by policy order so an overlay still decides ahead of the base. Order alone
        must never resolve them — a permissive rule listed first would otherwise shadow a stricter
        one appended later, silently de-escalating the decision. Every match the winner outranks is
        named in the reason, so the record shows what the engine considered and discarded.

        A permissive outcome is escalated by :func:`escalate_capability_decision` whenever the risk
        profile is uncertain (unknown level or activity, missing information, low confidence, or a
        review flag) or the constraints in force require a human. The engine never lowers a
        decision.

        ``inherited_constraints`` are the Run's own authorised constraints, from the recorded
        ``execution.start`` decision. They are merged into this call's derived constraints and
        persisted on it, so a requirement one capability's rule raised for the whole Run reaches
        every call under it -- a read tool whose own rules pass included. ``None`` constrains
        nothing beyond the capability, which is what every pre-Run-wide caller gets. Two allow-lists
        that share no tool cannot be merged into anything truthful, so that conflict is answered
        with a ``BLOCK`` (:func:`allow_lists_conflict`) before the merge is consulted at all; the
        merge is still recorded, where it authorises nothing because the decision refuses.
        """
        evaluated = evaluate_capability(self.policy, capability, risk, inherited_constraints)
        return self._decision(
            run_id,
            "tool_call",
            subject_id,
            evaluated.decision,
            evaluated.rule_id,
            evaluated.reason,
            execution_constraints=evaluated.execution_constraints,
        )

    def evaluate_capability(
        self,
        *,
        capability: ToolCapability,
        risk: RiskProfile,
        inherited_constraints: ExecutionConstraints | None = None,
    ) -> CapabilityEvaluation:
        """What this engine would decide for one capability right now, recording nothing.

        The non-recording half of :meth:`decide_capability`, for a caller holding an answer to an
        earlier decision that needs to know whether it still applies. Asking here rather than
        re-deriving the constraints is the point: a caller's own merge cannot see a conflict the
        engine refuses, and two answers to "what binds this call" is one answer too many.
        """
        return evaluate_capability(self.policy, capability, risk, inherited_constraints)

    def derive_execution_constraints(
        self, risk: RiskProfile, capabilities: Sequence[ToolCapability]
    ) -> ExecutionConstraints:
        """Combine only the typed constraints of rules applicable to available capabilities."""
        return derive_execution_constraints(self.policy, risk, capabilities)

    def decide_execution(
        self,
        *,
        run_id: str,
        risk: RiskProfile,
        capabilities: Sequence[ToolCapability],
    ) -> PolicyDecision:
        """Authorize THY's execution surface before its planner or tools run.

        A permissive start under an uncertain classification is escalated here, once, with the
        same ``Fail-safe escalation`` wording the per-call path uses: the human answers the
        classification before any tool runs instead of on every call. Precedence is a prohibited
        start (``BLOCK``) first, then this escalation, then a run-wide review constraint.
        """
        constraints = self.derive_execution_constraints(risk, capabilities)
        base_decision = self.decide_action(
            run_id=run_id,
            subject_kind="run",
            subject_id=run_id,
            action_type="execution.start",
            payload={"tool_count": len(capabilities)},
            risk_level=risk.risk_level,
        )
        permissive = base_decision.decision in (Decision.PASS, Decision.WARNING)
        uncertainty = effective_uncertainty(risk, self.policy.minimum_risk_confidence)
        if ExecutionAction.START in constraints.prohibited_actions:
            outcome = Decision.BLOCK
            reason = "Execution start is prohibited by the authorised execution constraints."
        elif permissive and uncertainty:
            outcome = Decision.REQUIRE_HUMAN_REVIEW
            reason = f"{base_decision.reason} {FAIL_SAFE_ESCALATION}: {', '.join(uncertainty)}."
        elif constraints.requires_human_review and permissive:
            outcome = Decision.REQUIRE_HUMAN_REVIEW
            reason = f"{base_decision.reason} Execution constraints require human review."
        else:
            return base_decision.model_copy(update={"execution_constraints": constraints})
        return base_decision.model_copy(
            update={"decision": outcome, "reason": reason, "execution_constraints": constraints}
        )

    # ----------------------------------------------------------------- findings
    def decide_findings(self, *, run_id: str, findings: Sequence[AuditFinding]) -> PolicyDecision:
        """No findings is PASS; otherwise the highest-precedence matching rule, else the default.

        Ties between rules of equal precedence are broken by policy order (first wins).
        """
        if not findings:
            return self._decision(
                run_id, "findings", "run", Decision.PASS, "no_findings", "No audit findings."
            )
        best: tuple[int, int, str, str, Decision] | None = None
        for position, rule in enumerate(self.policy.finding_rules):
            for finding in findings:
                if rule.matches(finding):
                    candidate = (
                        rule.decision.precedence,
                        -position,
                        rule.id,
                        rule.reason,
                        rule.decision,
                    )
                    if best is None or candidate[:2] > best[:2]:
                        best = candidate
                    break
        finding_ids = tuple(finding.id for finding in findings)
        if best is None:
            return self._decision(
                run_id,
                "findings",
                "run",
                self.policy.findings_default_decision,
                "findings_default",
                f"{len(findings)} finding(s) without a matching rule.",
                finding_ids,
            )
        _, _, rule_id, reason, decision = best
        return self._decision(run_id, "findings", "run", decision, rule_id, reason, finding_ids)

    # ------------------------------------------------------------------- budget
    def declares_budget_scope(self, scope: str) -> bool:
        """Whether any budget rule of the effective policy applies to ``scope``.

        A scope no rule names can only ever decide ``PASS`` (see :meth:`decide_budget`), so a
        caller may skip asking -- and recording -- a decision that carries no information.
        """
        return any(rule.scope == scope for rule in self.policy.budget_rules)

    def decide_budget(
        self, *, run_id: str, usage: Mapping[str, float | int | None], scope: str = "run"
    ) -> PolicyDecision:
        """Decide a usage snapshot against the policy's budget ceilings; under every ceiling PASS.

        Evaluates each :class:`~thymira.policies.models.BudgetRule` whose ``scope`` matches
        ``scope`` against ``usage`` (the runtime usage ledger's snapshot: ``cost_usd``, ``tokens``
        and ``tool_calls``). A rule is *crossed* when the snapshot exceeds any ceiling it declares;
        among the crossed rules the highest-precedence decision wins (``BLOCK`` >
        ``REQUIRE_HUMAN_REVIEW`` > ``WARNING``), ties breaking by policy order (overlay first). A
        soft ceiling maps to a ``WARNING`` rule and a hard ceiling to a stricter rule the same
        snapshot also crosses, so crossing the hard limit raises the outcome. No rule crossed is
        ``PASS``: a run under every ceiling proceeds.

        Args:
            run_id: The run the snapshot belongs to.
            usage: The usage-ledger snapshot to hold against the ceilings.
            scope: Which budget rules apply — ``'run'`` for the run total, or an agent role.

        Returns:
            The recorded-shape :class:`~thymira.schemas.PolicyDecision`, quoting the crossed rule
            and the recomputed ``policy_sha256``.
        """
        best: tuple[int, int, str, str, Decision] | None = None
        for position, rule in enumerate(self.policy.budget_rules):
            if rule.scope != scope or not rule.crossed(usage):
                continue
            candidate = (rule.decision.precedence, -position, rule.id, rule.reason, rule.decision)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            return self._decision(
                run_id,
                "run",
                run_id,
                Decision.PASS,
                "budget_within_limits",
                "Usage is within every budget ceiling.",
            )
        _, _, rule_id, reason, decision = best
        return self._decision(run_id, "run", run_id, decision, rule_id, reason)

    def decide_audit_freshness(self, *, run_id: str, fresh: bool, reason: str) -> PolicyDecision:
        """Authorize reuse only when the evidence snapshot is still current.

        Freshness is evidence supplied by MIRA; this deterministic decision is the Core seam that
        turns it into an authorization outcome. A stale report is blocked and is never silently
        treated as a request to recalculate the audit.
        """
        return self._decision(
            run_id,
            "run",
            run_id,
            Decision.PASS if fresh else Decision.BLOCK,
            "audit_freshness",
            reason,
        )

    def decide_rework_escalation(
        self, *, run_id: str, finding_ids: tuple[str, ...], reason: str
    ) -> PolicyDecision:
        """Require a human when bounded rework cannot demonstrate progress.

        This is a policy decision, not a THY instruction. Core calls it only after the ordinary
        findings decision has been evaluated and records the result through the same Gate.
        """
        return self._decision(
            run_id,
            "findings",
            "run",
            Decision.REQUIRE_HUMAN_REVIEW,
            "rework_escalation",
            reason,
            finding_ids,
        )

    # ------------------------------------------------------------------- models
    def decide_model(
        self, *, run_id: str, subject_id: str, role: str, model: str, tier: str | None = None
    ) -> PolicyDecision:
        """Decide whether a selected model is on the policy's allow-list; a substitution warns.

        The router (``thymira.agents.llm.routing``) chooses a model by code and records it as a
        ``model.selected`` event; this reads that choice — the role, the resolved model id and the
        applied tier — and holds it against the policy's model rules. Every rule covering the role
        is evaluated and the strictest outcome decides, exactly as in :meth:`decide_action`: a
        model the rule permits is ``PASS``; a denied, off-allow-list or over-tier model takes that
        rule's decision (a substitution or blocked model is a ``WARNING`` per ADR-0004), never a
        silent pass. Stopping at the first covering rule let a permissive rule listed ahead of a
        deny-list hand a denied model a ``PASS``, so order is not allowed to decide; ties still
        fall back to policy order, which keeps a prepended overlay ahead of the base. A role no
        rule covers is unconstrained — the model choice authorizes nothing by itself (the action,
        capability and tool gates decide effects) — so it passes.

        Args:
            run_id: The run the selection belongs to.
            subject_id: The task or agent the model was selected for.
            role: The routing role that requested the model (``thy`` / ``mira`` / ``agent``).
            model: The resolved model id the router selected.
            tier: The applied tier, when known, checked against a rule's ``max_tier``.

        Returns:
            The recorded-shape :class:`~thymira.schemas.PolicyDecision`, naming the deciding rule
            and the recomputed ``policy_sha256``.
        """
        candidates: list[tuple[Decision, str, str]] = []
        for rule in self.policy.model_rules:
            if not rule.applies_to(role):
                continue
            if rule.permits(model, tier):
                candidates.append(
                    (
                        Decision.PASS,
                        rule.id,
                        f"Model {model!r} is permitted for role {role!r} by rule {rule.id}.",
                    )
                )
            else:
                candidates.append((rule.decision, rule.id, rule.reason))
        if not candidates:
            return self._decision(
                run_id,
                "task",
                subject_id,
                Decision.PASS,
                "model_default",
                f"No model policy constrains role {role!r}; the model choice authorizes nothing.",
            )
        winner = max(range(len(candidates)), key=lambda index: candidates[index][0].precedence)
        decision, rule_id, reason = candidates[winner]
        shadowed = ", ".join(
            f"{other_id} ({other.value})"
            for index, (other, other_id, _) in enumerate(candidates)
            if index != winner
        )
        if shadowed:
            reason = f"{reason} Also matched: {shadowed}."
        return self._decision(run_id, "task", subject_id, decision, rule_id, reason)

    # ------------------------------------------------------------------ helpers
    def _decision(  # noqa: PLR0917  # shared record builder carries every persisted decision field
        self,
        run_id: str,
        subject_kind: SubjectKind,
        subject_id: str,
        decision: Decision,
        rule_id: str,
        reason: str,
        finding_ids: tuple[str, ...] = (),
        execution_constraints: ExecutionConstraints | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            id=new_id("decision"),
            run_id=run_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            decision=decision,
            rule_id=rule_id,
            reason=reason,
            policy_name=self.policy.label,
            policy_sha256=self.policy_sha256,
            finding_ids=finding_ids,
            execution_constraints=execution_constraints or ExecutionConstraints(),
        )


def allows_execution(decision: PolicyDecision, approval: Approval | None = None) -> bool:
    """Whether the subject may proceed under a separate matching approval when required."""
    if decision.decision in (Decision.PASS, Decision.WARNING):
        return True
    if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
        return (
            approval is not None
            and approval.policy_decision_id == decision.id
            and approval.approved
            and approval.approved_by.kind is ActorKind.HUMAN
            and approval.approved_by.authenticated
        )
    return False
