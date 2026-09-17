"""Policies as data: the rules the engine evaluates and the facts they match against.

Nothing here decides anything by itself. Rules are declarative, loaded from YAML/JSON, and
hashed so every decision can name the exact policy snapshot that produced it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from thymira.schemas import (
    AuditFinding,
    Decision,
    ExecutionConstraints,
    Framework,
    SandboxMode,
    Severity,
    ThymiraModel,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

WILDCARD = "*"


def _matches_any(expected: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """True when ``expected`` is a wildcard over a non-empty set or intersects ``actual``."""
    return (WILDCARD in expected and bool(actual)) or bool(set(expected) & set(actual))


class RiskProfile(ThymiraModel):
    """The risk classification a Risk agent produced for the run (facts, not permissions).

    ``unknown`` values and any missing information are treated as uncertainty by the engine.

    ``reviewed_decision_id`` is the id of the ``execution.start`` decision an authenticated,
    non-automatic human approved for this exact classification. It is a projection Core sets from
    the event log (``thymira.core.execution_review.reviewed_risk_profile``), never written into
    ``risk.classified``; while set, the engine no longer escalates permissive tool decisions for
    the classification's uncertainty, because a human already answered it once.
    """

    risk_level: str = "unknown"
    activity_category: str = "unknown"
    risk_factors: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_review: bool = False
    reviewed_decision_id: str | None = None


class ToolCapability(ThymiraModel):
    """Technical facts a tool declares about itself. Deliberately carries no authorization.

    ``side_effects`` is an open vocabulary the base policy names rule by rule. Two names are in
    use: ``artifact_write`` -- the tool persists only its own bounded report, metrics or tracker
    record inside the Run workspace and runs no code the model wrote (``GOV-009`` passes it
    locally) -- and ``workspace_write`` -- arbitrary files or code (``GOV-008`` reviews it). A
    name no rule covers falls to the capability default, a human review.
    """

    id: str = Field(min_length=1)
    risk_tags: tuple[str, ...] = ()
    data_access: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    external_effects: tuple[str, ...] = ()
    reversibility: str = "reversible"
    sandbox_mode: SandboxMode | None = None


class ActionRule(ThymiraModel):
    """Matches an action (by type, risk level and payload) and names a decision."""

    id: str = Field(min_length=1)
    description: str = ""
    action_types: tuple[str, ...] = (WILDCARD,)
    risk_levels: tuple[str, ...] = (WILDCARD,)
    payload_equals: dict[str, Any] = Field(default_factory=dict)
    payload_contains: dict[str, Any] = Field(default_factory=dict)
    decision: Decision
    reason: str = Field(min_length=1)

    def matches(self, action_type: str, payload: dict[str, Any], risk_level: str) -> bool:
        """Whether this rule applies to the given action."""
        if WILDCARD not in self.action_types and action_type not in self.action_types:
            return False
        if WILDCARD not in self.risk_levels and risk_level not in self.risk_levels:
            return False
        for key, value in self.payload_equals.items():
            if payload.get(key) != value:
                return False
        for key, value in self.payload_contains.items():
            collection = payload.get(key, ())
            if (
                not isinstance(collection, list | tuple | set | frozenset)
                or value not in collection
            ):
                return False
        return True


class CapabilityRule(ThymiraModel):
    """Matches a tool capability against the run's risk profile.

    Every listed group must match (``_matches_any`` within a group). For both effect fields,
    ``()`` requires *no* declared effects, ``("*",)`` matches any declared effect, and ``None``
    ignores the field.

    ``execution_constraints`` names only conditions the Core, THY, or Tool Manager can check.
    The Policy Engine derives them from every applicable rule, records them on the decision, and
    the Tool Manager applies them again at each call.  Rules can only narrow execution.
    """

    id: str = Field(min_length=1)
    description: str = ""
    decision: Decision
    reason: str = Field(min_length=1)
    risk_levels: tuple[str, ...] = ()
    activity_categories: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    data_access: tuple[str, ...] = ()
    external_effects: tuple[str, ...] | None = None
    side_effects: tuple[str, ...] | None = None
    reversibility: tuple[str, ...] = ()
    execution_constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)

    def matches(self, risk: RiskProfile, capability: ToolCapability) -> bool:
        """Whether this rule applies to the capability under the given risk profile."""
        checks = (
            (self.risk_levels, (risk.risk_level,)),
            (self.activity_categories, (risk.activity_category,)),
            (self.risk_factors, risk.risk_factors),
            (self.risk_tags, capability.risk_tags),
            (self.data_access, capability.data_access),
            (self.reversibility, (capability.reversibility,)),
        )
        for expected, actual in checks:
            if expected and not _matches_any(expected, actual):
                return False
        if self.external_effects is not None:
            if not self.external_effects:
                if capability.external_effects:
                    return False
            elif not _matches_any(self.external_effects, capability.external_effects):
                return False
        if self.side_effects is not None:
            if not self.side_effects:
                return not capability.side_effects
            return _matches_any(self.side_effects, capability.side_effects)
        return True


class FindingRule(ThymiraModel):
    """Matches an audit finding by severity, confidence, framework, control or evidence."""

    id: str = Field(min_length=1)
    description: str = ""
    decision: Decision
    reason: str = Field(min_length=1)
    min_severity: Severity | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    frameworks: tuple[Framework, ...] = ()
    control_ids: tuple[str, ...] = ()
    evidence: Literal["missing", "present"] | None = None

    def matches(self, finding: AuditFinding) -> bool:
        """Whether this rule applies to the finding."""
        if self.min_severity and SEVERITY_RANK[finding.severity] < SEVERITY_RANK[self.min_severity]:
            return False
        if self.min_confidence is not None and finding.confidence < self.min_confidence:
            return False
        if self.frameworks and finding.framework not in self.frameworks:
            return False
        if self.control_ids and finding.control_id not in self.control_ids:
            return False
        if self.evidence == "missing" and finding.evidence:
            return False
        return not (self.evidence == "present" and not finding.evidence)


class BudgetRule(ThymiraModel):
    """A spending or usage ceiling and what to decide when a run crosses it.

    Declared ahead of the engine that evaluates it: see :class:`Policy` for why a field cannot be
    added after the contract freezes. The MVP records budgets through the usage ledger and
    enforces only the hard ceiling; turning a threshold into a decision is FINAL work (`POL-01`)
    that adds no field.
    """

    id: str = Field(min_length=1)
    description: str = ""
    decision: Decision
    reason: str = Field(min_length=1)
    max_usd: float | None = Field(default=None, ge=0.0)
    max_tokens: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    scope: str = Field(default="run", description="'run' or an agent role the ceiling applies to")

    def crossed(self, usage: Mapping[str, float | int | None]) -> bool:
        """Whether a usage snapshot crosses any ceiling this rule declares.

        A ceiling the snapshot cannot measure counts as crossed. Cost tracking degrades to
        ``None`` the moment one model response arrives unpriced -- which is every new, self-hosted
        or proxied model -- and a ceiling that stops being enforced exactly when its input becomes
        unknown is a guard reporting success because it could not perform its check. This mirrors
        ``thymira.core``'s ``UsageLedger.exceeds`` and ``thymira.agents``'s ``RunUsage``, which
        escalate on the same condition; the three are separate implementations because the layer
        order forbids sharing one, so they must be changed together.

        A *missing* key still is not compared: a snapshot that never carried ``tool_calls`` is a
        different thing from one whose count is unknown, and only the second is a degraded
        measurement.

        Args:
            usage: The runtime usage-ledger snapshot, keyed ``cost_usd`` / ``tokens`` /
                ``tool_calls``.

        Returns:
            ``True`` when the snapshot is strictly above ``max_usd``, ``max_tokens`` or
            ``max_tool_calls``, or when a declared ceiling's value is present but unmeasured;
            a rule that declares no ceiling is never crossed.
        """
        unmeasured = object()
        ceilings: tuple[tuple[float | int | None, object], ...] = (
            (self.max_usd, usage.get("cost_usd", unmeasured)),
            (self.max_tokens, usage.get("tokens", unmeasured)),
            (self.max_tool_calls, usage.get("tool_calls", unmeasured)),
        )
        return any(
            limit is not None and actual is not unmeasured and (actual is None or actual > limit)  # ty: ignore[unsupported-operator]
            for limit, actual in ceilings
        )


_MODEL_TIER_RANK: dict[str, int] = {"FAST": 0, "STANDARD": 1, "FRONTIER": 2}


class ModelRule(ThymiraModel):
    """Which models a role may use, and what to decide when it asks for another one.

    Models are configuration, never names in the code (ADR-0003), so a rule names them as data.
    Declared ahead of its engine for the same contract-freeze reason as :class:`BudgetRule`; the
    decision logic is FINAL work (`POL-02`).
    """

    id: str = Field(min_length=1)
    description: str = ""
    decision: Decision
    reason: str = Field(min_length=1)
    roles: tuple[str, ...] = (WILDCARD,)
    allowed_models: tuple[str, ...] = ()
    denied_models: tuple[str, ...] = ()
    max_tier: str | None = Field(default=None, description="highest tier the role may request")

    def applies_to(self, role: str) -> bool:
        """Whether this rule governs the given role's model choice."""
        return WILDCARD in self.roles or role in self.roles

    def permits(self, model: str, tier: str | None = None) -> bool:
        """Whether this rule allows ``model`` (at ``tier``) for a role it governs.

        A model is permitted when it is not denied, is on the allow-list (or the allow-list is
        empty, meaning 'anything not denied'), and does not exceed ``max_tier``. Call only after
        :meth:`applies_to`; it does not re-check the role.

        Args:
            model: The resolved model id the router selected.
            tier: The applied tier (``FAST`` / ``STANDARD`` / ``FRONTIER``), when known.

        Returns:
            ``True`` when the rule allows the model; ``False`` for a denied, off-allow-list or
            over-tier model, which the engine then surfaces as this rule's decision.
        """
        if model in self.denied_models:
            return False
        if self.allowed_models and model not in self.allowed_models:
            return False
        if self.max_tier is not None and tier is not None:
            ceiling = _MODEL_TIER_RANK.get(self.max_tier.upper())
            requested = _MODEL_TIER_RANK.get(tier.upper())
            if ceiling is not None and requested is not None and requested > ceiling:
                return False
        return True


class Policy(ThymiraModel):
    """A named, versioned, hashable set of rules (a base policy plus optional overlays).

    **Every field this model will ever need must exist before the contract freezes.**
    ``policy_sha256`` hashes ``to_json_dict()``, which is ``model_dump(mode="json")`` with no
    ``exclude_defaults``: adding any field later — even one defaulting to empty — adds a key to
    the serialised dict and therefore changes the hash of *every* policy already in use,
    invalidating every recorded ``PolicyDecision.policy_sha256`` and breaking the replay that
    control A16 exists to perform. ``budget_rules`` and ``model_rules`` are therefore declared
    now (`product-final.md` correction C-10), ahead of the engine that will evaluate them.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    action_rules: tuple[ActionRule, ...] = ()
    capability_rules: tuple[CapabilityRule, ...] = ()
    finding_rules: tuple[FindingRule, ...] = ()
    budget_rules: tuple[BudgetRule, ...] = ()
    model_rules: tuple[ModelRule, ...] = ()
    default_decision: Decision = Decision.REQUIRE_HUMAN_REVIEW
    default_reason: str = "Action not covered by the policy: when in doubt, ask a human."
    capability_default_decision: Decision = Decision.REQUIRE_HUMAN_REVIEW
    capability_default_reason: str = "Capability not covered by the policy: requires approval."
    # Not fail-open, despite reading like it next to the two REQUIRE_HUMAN_REVIEW defaults above.
    # An action or a capability is a *request to act*, so an uncovered one escalates. A finding is
    # an observation already made, and the base policy `load_policy_stack` always loads first
    # carries the full ladder: CRITICAL -> BLOCK (GOV-101), HIGH -> REQUIRE_HUMAN_REVIEW
    # unconditionally (GOV-102/103 and the GOV-103b catch-all that keeps a self-assessed
    # confidence from softening one), MEDIUM -> WARNING (GOV-104). This default is only reached by a
    # LOW finding no rule matched, and sending every low-severity audit observation to a human
    # would stop every run on informational noise.
    findings_default_decision: Decision = Decision.WARNING
    minimum_risk_confidence: float = Field(default=0.75, ge=0.0, le=1.0)

    @property
    def label(self) -> str:
        """``name@version`` as recorded on every decision."""
        return f"{self.name}@{self.version}"

    def merged_with(self, overlay: Policy) -> Policy:
        """Return a policy where the overlay's rules are evaluated before this policy's rules.

        Scalar defaults (default decisions, minimum confidence) come from the base policy.
        """
        return self.model_copy(
            update={
                "name": f"{self.name}+{overlay.name}",
                "version": f"{self.version}+{overlay.version}",
                "description": overlay.description or self.description,
                "action_rules": (*overlay.action_rules, *self.action_rules),
                "capability_rules": (*overlay.capability_rules, *self.capability_rules),
                "finding_rules": (*overlay.finding_rules, *self.finding_rules),
                "budget_rules": (*overlay.budget_rules, *self.budget_rules),
                "model_rules": (*overlay.model_rules, *self.model_rules),
            }
        )
