"""Rework node: a `policy.decision` reopens an earlier phase, bounded by a budget (THY-24).

ADR-0004 dec.5: MIRA never talks to THY; it reaches THY only as a `policy.decision`. When that
decision asks for rework (`ThyState.rework`, a `ReworkSignal`), ThyGraph re-enters Execute to
re-run the affected tasks -- but only if `can_reopen` allows it, and only within a budget.

`can_reopen` here mirrors `thymira.core.phases.can_reopen` exactly (same signature, same rules:
backward-only, reopenable phases only, within budget, justified) but is a THY-local reimplementation
because `thymira.thy` sits below `thymira.core` in the enforced layer order and may not import it.
`REOPENABLE_PHASES`/`PHASE_ORDER` likewise mirror `thymira.core.phases`; `tests/thymira/
test_thy_rework.py` asserts the mirror against the real `thymira.core.phases` so the two never
drift. This copy governs *only* ThyGraph's in-graph rework loop -- `RunController` (`thymira.core`)
stays the sole writer of persistent Run transitions (ADR-0008); reopening a phase in ThyGraph is a
control-flow loop, not a `run.transitioned`, so this node emits no event and writes no Run state.

The loop is budgeted, never infinite: each allowed reopen spends one unit of `reopen_count`; once
`reopen_count` reaches `max_reopens`, `can_reopen` refuses with a budget message, the node records
that refusal on `state.rework_refusal`, clears `state.rework`, and the graph proceeds to Summarize
instead of looping. A refusal is not a failure (`state.error` stays clear): the run still finishes
and reports what it has, plus why the reopen was denied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.thy.models import ThyPhase

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.thy.models import ThyState

PHASE_ORDER: tuple[str, ...] = (
    "understanding",
    "preparation",
    "modeling",
    "evaluation",
    "reporting",
)
"""Analytical-phase names in order, mirroring `thymira.core.phases.PHASE_ORDER` by value."""

REOPENABLE_PHASES: frozenset[str] = frozenset({"preparation", "modeling"})
"""The only analytical phases a rework may reopen, mirroring `thymira.core.phases`."""


def can_reopen(
    current: str,
    target: str,
    *,
    reopen_count: int,
    max_reopens: int,
    justification: str,
) -> tuple[bool, str]:
    """Whether a reopen from `current` to `target` is allowed, and why not when it is refused.

    A THY-local mirror of `thymira.core.phases.can_reopen`: backward-only, limited to
    `REOPENABLE_PHASES`, within `max_reopens`, and justified. `current`/`target` are analytical
    phase names (values of `thymira.core.phases.Phase`).

    Returns:
        ``(True, "ok")`` when the reopen is allowed; ``(False, reason)`` otherwise, the reason
        naming the rule that refused it (unjustified, non-reopenable, forward, or over budget).
    """
    if not justification.strip():
        return False, "a reopen needs a justification"
    if target not in REOPENABLE_PHASES:
        return False, f"phase {target} cannot be reopened"
    if not _precedes(target, current):
        return False, f"{target} does not precede {current}; reopening only moves backwards"
    if reopen_count >= max_reopens:
        return False, f"reopen budget exhausted ({reopen_count}/{max_reopens})"
    return True, "ok"


def _precedes(candidate: str, current: str) -> bool:
    """Whether analytical phase `candidate` comes strictly before `current` in `PHASE_ORDER`.

    An unknown phase name is treated as not preceding anything: a reopen naming a phase outside the
    analytical vocabulary is refused rather than silently allowed.
    """
    if candidate not in PHASE_ORDER or current not in PHASE_ORDER:
        return False
    return PHASE_ORDER.index(candidate) < PHASE_ORDER.index(current)


def rework_node() -> Callable[..., dict[str, Any]]:
    """Build the Rework node: consult `state.rework`, reopen within budget or refuse.

    Reached only when `state.rework` is set (`_route_after_execute`, `thymira.thy.graph`). On an
    allowed reopen it spends one `reopen_count`, clears the previous execution outcomes and resets
    `phase` to EXECUTE so the graph re-runs the plan, keeping `state.rework` so a still-unhappy
    signal is re-evaluated (and eventually stopped by the budget). On a refusal it records the
    reason on `state.rework_refusal`, clears `state.rework`, and lets the graph move to Summarize.
    """

    def _rework(state: ThyState) -> dict[str, Any]:
        signal = state.rework
        if signal is None:  # defensive: the router only sends us here when a signal is present.
            return state.model_dump()
        allowed, reason = can_reopen(
            signal.from_phase,
            signal.target_phase,
            reopen_count=state.reopen_count,
            max_reopens=state.max_reopens,
            justification=signal.justification,
        )
        if not allowed:
            return state.model_copy(update={"rework": None, "rework_refusal": reason}).model_dump()
        updates: dict[str, Any] = {
            "reopen_count": state.reopen_count + 1,
            "completed": (),
            "agent_messages": (),
            "phase": ThyPhase.EXECUTE,
        }
        if state.single_rework:
            # Core already authorized one reopen. Consume the signal after entering the
            # re-entry so the subsequent audit, rather than a THY-local loop, decides whether
            # another correction is needed.
            updates.update(
                {
                    "rework": None,
                    "single_rework": False,
                }
            )
        return state.model_copy(update=updates).model_dump()

    return _rework


__all__ = ["PHASE_ORDER", "REOPENABLE_PHASES", "can_reopen", "rework_node"]
