"""Policy approval disclosure for execution constraints that require tool reviews.

Approving the execution-start review starts THY; it authorises no tool call. Under
``requires_human_review`` every call inherits the requirement into its own capability decision
and needs its own ticketed human answer, so the summary the human is shown says exactly that
instead of promising that tools stay blocked.
"""

import pytest

from thymira.events import InMemoryEventLog
from thymira.policies import (
    ActionRule,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    pending_approvals,
)
from thymira.schemas import Decision, ExecutionConstraints, new_id

_RISK = RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0)
_CAPABILITY = ToolCapability(id="profile")
_DISCLOSURE = (
    " Approval starts THY, and under this execution constraint every tool call still needs its "
    "own separate human approval before it runs."
)


def _gate(action_decision: Decision, *, constrained: bool) -> Gate:
    """Build an execution Gate with one action decision and optional tool-review constraint."""
    constraints = ExecutionConstraints(requires_human_review=True) if constrained else None
    policy = Policy(
        name="execution-disclosure",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=action_decision,
                reason="Execution start requires review.",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="TOOL-REVIEW",
                decision=Decision.PASS,
                reason="Tool constraint applies.",
                execution_constraints=constraints or ExecutionConstraints(),
            ),
        ),
    )
    return Gate(PolicyEngine(policy), InMemoryEventLog(new_id("run")))


@pytest.mark.parametrize(
    ("action_decision", "expected_reason"),
    [
        (
            Decision.PASS,
            "Execution start requires review. Execution constraints require human review.",
        ),
        (Decision.REQUIRE_HUMAN_REVIEW, "Execution start requires review."),
    ],
)
def test_authorize_execution_discloses_constraint_review_for_each_review_origin(
    action_decision: Decision, expected_reason: str
) -> None:
    """A constrained review explains that each tool call still needs its own human answer."""
    gate = _gate(action_decision, constrained=True)

    decision = gate.authorize_execution(_RISK, (_CAPABILITY,))
    pending = pending_approvals(gate.log.events())

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.reason == expected_reason
    assert decision.execution_constraints == ExecutionConstraints(requires_human_review=True)
    assert len(pending) == 1
    assert pending[0].decision_id == decision.id
    assert pending[0].summary == f"execution.start{_DISCLOSURE}"


def test_authorize_execution_keeps_unconstrained_review_summary_and_decision() -> None:
    """A direct start review does not gain the constraint-specific disclosure."""
    gate = _gate(Decision.REQUIRE_HUMAN_REVIEW, constrained=False)

    decision = gate.authorize_execution(_RISK, (_CAPABILITY,))
    pending = pending_approvals(gate.log.events())

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.reason == "Execution start requires review."
    assert decision.execution_constraints == ExecutionConstraints()
    assert len(pending) == 1
    assert pending[0].summary == "execution.start"


_UNCERTAIN = _RISK.model_copy(update={"confidence": 0.3, "needs_human_review": True})
_CLASSIFICATION_SUMMARY = (
    "execution.start: risk limited (analysis); factors: none; missing: none; confidence 0.30; "
    "needs_human_review=True"
)


@pytest.mark.parametrize("constrained", [False, True])
def test_uncertainty_start_review_shows_the_classification_in_its_summary(
    *, constrained: bool
) -> None:
    """The human answering the start review sees what they accept, on the existing surfaces."""
    gate = _gate(Decision.PASS, constrained=constrained)

    decision = gate.authorize_execution(_UNCERTAIN, (_CAPABILITY,))
    pending = pending_approvals(gate.log.events())

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert "Fail-safe escalation" in decision.reason
    assert "classification confidence below the policy threshold" in decision.reason
    assert pending[0].summary == (
        f"{_CLASSIFICATION_SUMMARY}{_DISCLOSURE}" if constrained else _CLASSIFICATION_SUMMARY
    )
