"""Core-owned authorization and bounded translation for analytical rework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.core.phases import PHASE_ORDER, Phase, can_reopen
from thymira.schemas import ActorKind, Approval, AuditFinding, Decision, PolicyDecision
from thymira.thy.models import ReworkSignal

if TYPE_CHECKING:
    from collections.abc import Sequence


# These are controls whose remediation is meaningful as a bounded analytical re-entry. The map is
# deliberately explicit: a finding is evidence, not an instruction, and an unknown control can
# never smuggle a phase transition into THY.
REWORK_TARGETS: dict[str, Phase] = {
    "A11": Phase.PREPARATION,
    "A12": Phase.MODELING,
    "A13": Phase.MODELING,
    "A14": Phase.MODELING,
    "A20": Phase.PREPARATION,
    "A21": Phase.MODELING,
    "A22": Phase.MODELING,
    "A23": Phase.MODELING,
    "REWORK-PREPARATION": Phase.PREPARATION,
    "REWORK-MODELING": Phase.MODELING,
}


@dataclass(frozen=True, slots=True)
class ReworkTranslation:
    """Result of translating one already-authorized findings decision."""

    signal: ReworkSignal | None
    reason: str | None = None


def rework_signal_from_decision(
    decision: PolicyDecision,
    findings: Sequence[AuditFinding],
    *,
    current_phase: Phase = Phase.EVALUATION,
    approval: Approval | None = None,
    approved: bool = False,
    reopen_count: int = 0,
    max_reopens: int = 2,
) -> ReworkTranslation:
    """Translate a policy-authorized finding into a THY ``ReworkSignal``.

    Only ``PASS``/``WARNING`` decisions authorize an immediate signal. A
    ``REQUIRE_HUMAN_REVIEW`` decision is eligible only after an exact approved response; a
    ``BLOCK`` is never converted into rework. The target comes solely from the explicit control
    map above, and Core applies the canonical phase/budget rules before returning the signal.
    """
    if decision.decision is Decision.BLOCK:
        return ReworkTranslation(None, "an audit BLOCK is not a rework authorization")
    if decision.decision is Decision.REQUIRE_HUMAN_REVIEW:
        exact_approval = (
            approval is not None
            and approval.policy_decision_id == decision.id
            and approval.approved_by.kind is ActorKind.HUMAN
            and approval.approved_by.authenticated
        )
        if not approved and not (exact_approval and approval.approved):
            return ReworkTranslation(None, "rework requires an approved human decision")
    elif decision.decision not in (Decision.PASS, Decision.WARNING):
        return ReworkTranslation(
            None, f"decision {decision.decision.value} cannot authorize rework"
        )

    by_id = {finding.id: finding for finding in findings}
    selected = tuple(
        by_id[finding_id] for finding_id in decision.finding_ids if finding_id in by_id
    )
    targets = tuple(
        (finding, REWORK_TARGETS[finding.control_id])
        for finding in selected
        if finding.control_id in REWORK_TARGETS
    )
    if not targets:
        return ReworkTranslation(None, "no finding has an explicitly reworkable control")

    target = min((phase for _, phase in targets), key=PHASE_ORDER.index)
    justification = _justification(decision, targets)
    allowed, reason = can_reopen(
        current_phase,
        target,
        reopen_count=reopen_count,
        max_reopens=max_reopens,
        justification=justification,
    )
    if not allowed:
        return ReworkTranslation(None, reason)
    signal = ReworkSignal(
        decision=decision,
        from_phase=current_phase.value,
        target_phase=target.value,
        justification=justification,
    )
    return ReworkTranslation(signal)


def _justification(
    decision: PolicyDecision,
    targets: Sequence[tuple[AuditFinding, Phase]],
) -> str:
    """Build a bounded, auditable justification from policy and finding facts."""
    controls = ", ".join(finding.control_id for finding, _ in targets)
    return f"{decision.reason} Reopen for corrective controls: {controls}."


__all__ = ["REWORK_TARGETS", "ReworkTranslation", "rework_signal_from_decision"]
