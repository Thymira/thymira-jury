"""Analytical phases: a control on what an orchestrator may consider, not a fixed recipe.

Ported from the thesis prototype. Phases gate capabilities; moving backwards is never a normal
transition — it is an explicit, budgeted, justified reopen that the Policy Engine approves.
"""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """Ordered phases of a data-science run."""

    UNDERSTANDING = "understanding"
    PREPARATION = "preparation"
    MODELING = "modeling"
    EVALUATION = "evaluation"
    REPORTING = "reporting"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.UNDERSTANDING,
    Phase.PREPARATION,
    Phase.MODELING,
    Phase.EVALUATION,
    Phase.REPORTING,
)

REOPENABLE_PHASES: frozenset[Phase] = frozenset({Phase.PREPARATION, Phase.MODELING})


def phase_precedes(candidate: Phase, current: Phase) -> bool:
    """Whether ``candidate`` comes before ``current``."""
    return PHASE_ORDER.index(candidate) < PHASE_ORDER.index(current)


def next_phase(current: Phase, *, requires_modeling: bool) -> Phase | None:
    """The minimal forward path: descriptive objectives skip straight to reporting."""
    if current is Phase.UNDERSTANDING:
        return Phase.PREPARATION if requires_modeling else Phase.REPORTING
    if current is Phase.REPORTING:
        return None
    return PHASE_ORDER[PHASE_ORDER.index(current) + 1]


def can_reopen(
    current: Phase,
    target: Phase,
    *,
    reopen_count: int,
    max_reopens: int,
    justification: str,
) -> tuple[bool, str]:
    """Backward-only, limited to reopenable phases, within budget, and justified."""
    if not justification.strip():
        return False, "a reopen needs a justification"
    if target not in REOPENABLE_PHASES:
        return False, f"phase {target} cannot be reopened"
    if not phase_precedes(target, current):
        return False, f"{target} does not precede {current}; reopening only moves backwards"
    if reopen_count >= max_reopens:
        return False, f"reopen budget exhausted ({reopen_count}/{max_reopens})"
    return True, "ok"
