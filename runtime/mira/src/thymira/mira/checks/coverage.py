"""Recomputation of the requirements-coverage control A26.

For each governance framework a project declares, this control reads the requirements->controls
mapping (``docs/governance/requirements_controls.json``, the KB-02 schema) and checks that every
mapped requirement's deterministic control actually produced evidence in the run. A control
"produced evidence" when, re-evaluated against the same run, it is not ``NOT_APPLICABLE``: a
``NOT_APPLICABLE`` result means the evidence the control audits is simply absent, which is exactly a
coverage gap. The finding names the uncovered requirement and the control that has no evidence.

This is the deterministic counterpart to the AUD-COMPLIANCE audit agent: both read the same stable
mapping, the agent narrating and the control asserting. It never re-runs a control to judge
correctness -- a control that FAILED still produced evidence and is covered here; a failing control
raises its own finding. Control ids the mapping cites that are not deterministic controls (for
example a reviewed-pack control evaluated in preflight) are not assessable here and are skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from thymira.mira.checks.models import ControlStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from thymira.mira.checks.controls import AuditContext
    from thymira.mira.kb import RequirementsControls

_MAX_LISTED = 8
"""How many coverage gaps to quote in a finding detail before truncating."""

_NO_EVIDENCE = frozenset({ControlStatus.NOT_APPLICABLE, ControlStatus.NOT_EVALUATED})
"""The statuses that mean a control produced no evidence, and so left a requirement uncovered."""


def check_requirements_coverage(
    ctx: AuditContext,
    control_status: Mapping[str, ControlStatus],
    *,
    mapping: RequirementsControls | None = None,
) -> tuple[ControlStatus, str]:
    """Fail when a declared framework's requirement has a mapped control with no run evidence.

    ``control_status`` is what the other controls already concluded in this same audit, keyed by
    control id. Reading their recorded outcome rather than invoking each check again keeps A26
    consistent with the report it is part of -- it can no longer disagree with the ``ControlResult``
    published beside it -- and stops one audit from evaluating every deterministic control twice.
    A control absent from the mapping is one this audit did not run, so it is not assessable here.
    """
    if not ctx.frameworks:
        return ControlStatus.NOT_APPLICABLE, "no governance frameworks declared"
    resolved = mapping if mapping is not None else load_default_requirements_controls()
    if resolved is None:
        return ControlStatus.NOT_APPLICABLE, "no requirements-controls mapping available"
    declared = set(ctx.frameworks)
    gaps: list[str] = []
    assessed = 0
    for requirement in resolved.requirements:
        if requirement.framework not in declared:
            continue
        for control_id in requirement.control_ids:
            status = control_status.get(control_id)
            if status is None:
                continue
            assessed += 1
            if status in _NO_EVIDENCE:
                gaps.append(
                    f"{requirement.requirement_id} has no evidence from control {control_id}"
                )
    if assessed == 0:
        return (
            ControlStatus.NOT_APPLICABLE,
            "no deterministic control covers the declared frameworks",
        )
    if gaps:
        return ControlStatus.FAILED, "; ".join(gaps[:_MAX_LISTED])
    return ControlStatus.PASSED, f"{assessed} requirement-control evidence link(s) present"


def load_default_requirements_controls() -> RequirementsControls | None:
    """Load the repo requirements->controls mapping, or ``None`` when it cannot be found."""
    path = _default_mapping_path()
    if path is None:
        return None
    from thymira.mira.kb.ingest import load_requirements  # noqa: PLC0415  # lazy KB import

    try:
        return load_requirements(path)
    except (OSError, ValueError, TypeError):
        return None


def _default_mapping_path() -> Path | None:
    """Find ``docs/governance/requirements_controls.json`` by walking up from this module."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "governance" / "requirements_controls.json"
        if candidate.is_file():
            return candidate
    return None


__all__ = ["check_requirements_coverage", "load_default_requirements_controls"]
