"""Recomputation of audit control A13: model selection re-derived independently.

A13 reads the model-selection evidence a run leaves in the artifact store -- the candidate models,
their recorded out-of-fold metrics and the declared selection strategy -- and re-derives the choice
from that evidence rather than trusting the winner THY reported (the inherited meaning at
``docs/legacy/trazabilidad.md:113``). The recomputation disagreeing with the recorded winner is a
finding, and so is a strategy not recorded precisely enough to reproduce the choice at all: an
unverifiable selection is a finding in its own right, so the control keeps working when new
strategies are added -- it never needs to know them in advance, only that the recorded one
reproduces. It is ``NOT_APPLICABLE`` when no selection evidence is present.

Evidence shape (a self-describing selection artifact, any active JSON carrying a ``candidates`` list
and a ``selected`` field):

    {
      "selection_strategy": {"metric": "roc_auc", "direction": "max"},
      "candidates": [
        {"name": "logreg", "oof_metrics": {"roc_auc": 0.81}},
        {"name": "random_forest", "oof_metrics": {"roc_auc": 0.86}}
      ],
      "selected": "random_forest"
    }
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.mira.checks.models import ControlStatus

if TYPE_CHECKING:
    from thymira.schemas import Artifact
    from thymira.state import ArtifactStore

_TOLERANCE = 1e-9
"""A candidate whose metric is this close to the optimum counts as achieving it (ties included)."""

_MAXIMISE = frozenset({"max", "maximize", "maximise", "higher", "higher_is_better", "greater"})
"""Strategy directions that mean the metric-optimal candidate has the greatest value."""

_MINIMISE = frozenset({"min", "minimize", "minimise", "lower", "lower_is_better", "less"})
"""Strategy directions that mean the metric-optimal candidate has the smallest value."""

_STRATEGY_KEYS = ("selection_strategy", "strategy")
"""Where the declared selection strategy may live on a selection artifact."""

_SELECTED_KEYS = ("selected", "selected_model", "selected_model_id")
"""Where the recorded winner may be named on a selection artifact."""

_CANDIDATE_ID_KEYS = ("name", "model", "id", "model_id")
"""Where a candidate's own identifier may live, tried in order."""

_METRIC_KEYS = ("oof_metrics", "metrics", "cv_metrics", "out_of_fold_metrics")
"""Where a candidate's recorded out-of-fold metrics may live, tried in order."""


def check_model_selection(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Re-derive the model choice from the recorded OOF metrics and the declared strategy."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    selections = _selection_artifacts(store)
    if not selections:
        return ControlStatus.NOT_APPLICABLE, "no model-selection evidence"
    problems: list[str] = []
    for artifact, payload in selections:
        problems.extend(_one_selection_problems(artifact.name, payload))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return ControlStatus.PASSED, f"{len(selections)} selection(s) reproduce the recorded winner"


def _one_selection_problems(name: str, payload: dict[str, Any]) -> list[str]:
    """Recompute one selection artifact's winner and compare it to the recorded choice."""
    prefix = f"selection {name!r}"
    metric, direction = _parse_strategy(payload)
    if metric is None or direction is None:
        return [f"{prefix}: selection strategy is absent or ambiguous"]
    values, missing = _candidate_values(payload, metric)
    if missing or not values:
        listed = sorted(missing) if missing else "none"
        return [f"{prefix}: candidate(s) {listed} record no {metric!r} out-of-fold metric"]
    selected = _selected_id(payload)
    if selected is None or selected not in values:
        return [f"{prefix}: recorded winner {selected!r} is not among the recorded candidates"]
    optimum = max(values.values()) if direction == "max" else min(values.values())
    if abs(values[selected] - optimum) > _TOLERANCE:
        winners = sorted(cid for cid, value in values.items() if abs(value - optimum) <= _TOLERANCE)
        message = (
            f"{prefix}: recorded winner {selected!r} ({metric}={values[selected]:g}) is not the "
            f"{direction}-optimal candidate(s) {winners} ({metric}={optimum:g})"
        )
        return [message]
    return []


def _parse_strategy(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``(metric, direction)`` from the declared strategy, or ``(None, None)`` if vague."""
    strategy = next(
        (payload[key] for key in _STRATEGY_KEYS if isinstance(payload.get(key), dict)), None
    )
    if strategy is None:
        return None, None
    metric = strategy.get("metric")
    if not isinstance(metric, str) or not metric:
        return None, None
    raw_direction = strategy.get("direction", strategy.get("goal", strategy.get("mode")))
    if not isinstance(raw_direction, str):
        return None, None
    normalised = raw_direction.strip().casefold()
    if normalised in _MAXIMISE:
        return metric, "max"
    if normalised in _MINIMISE:
        return metric, "min"
    return None, None


def _candidate_values(payload: dict[str, Any], metric: str) -> tuple[dict[str, float], set[str]]:
    """Map each candidate id to its recorded ``metric`` value; collect ids that record none."""
    values: dict[str, float] = {}
    missing: set[str] = set()
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return values, missing
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        cid = _candidate_id(candidate, index)
        value = _metric_value(candidate, metric)
        if value is None:
            missing.add(cid)
        else:
            values[cid] = value
    return values, missing


def _candidate_id(candidate: dict[str, Any], index: int) -> str:
    """Resolve a candidate's identifier, falling back to its position."""
    for key in _CANDIDATE_ID_KEYS:
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return f"candidate[{index}]"


def _metric_value(candidate: dict[str, Any], metric: str) -> float | None:
    """Read one candidate's recorded value for ``metric`` from its metrics map."""
    for key in _METRIC_KEYS:
        metrics = candidate.get(key)
        if isinstance(metrics, dict) and metric in metrics:
            value = metrics[metric]
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _selected_id(payload: dict[str, Any]) -> str | None:
    """Read the recorded winner's identifier from the selection artifact."""
    for key in _SELECTED_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _selection_artifacts(store: ArtifactStore) -> list[tuple[Artifact, dict[str, Any]]]:
    """Every active JSON artifact that names candidates and a recorded winner."""
    found: list[tuple[Artifact, dict[str, Any]]] = []
    for artifact in store.list_active():
        if not artifact.name.endswith(".json"):
            continue
        payload = _load_json(store, artifact.name)
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            continue
        if any(isinstance(payload.get(key), str) and payload[key] for key in _SELECTED_KEYS):
            found.append((artifact, payload))
    return found


def _load_json(store: ArtifactStore, name: str) -> Any:
    """Load an artifact as JSON, returning ``None`` when it is missing or not JSON."""
    try:
        value = store.load_json(name)
    except (OSError, ValueError):
        return None
    else:
        return value


__all__ = ["check_model_selection"]
