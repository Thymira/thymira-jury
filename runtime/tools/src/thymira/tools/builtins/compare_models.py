"""Model comparison over tracker runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, Field

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import ComparisonValue


class CompareModelsArguments(BaseModel):
    """Arguments for a side-by-side tracker comparison."""

    # ``min_length`` on the tuple bounds how many ids arrive; the element constraint bounds
    # what an id may be. Without it ``("", "")`` is two well-formed ids that name no run.
    run_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=2)
    description: Description = DESCRIPTION_FIELD
    metric: str = Field(default="accuracy", min_length=1)


@dataclass(frozen=True, slots=True)
class CompareModels:
    """Compare two or more tracker runs and rank them by a primary metric."""

    name: str = "compare_models"
    description: str = "Compare experiment runs and report the winner by metric."
    arguments_model: type[BaseModel] = CompareModelsArguments
    result_model: type[BaseModel] = ComparisonValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="compare_models",
            # model_comparison is what CR-001 matches for comparisons over sensitive data.
            risk_tags=("model_comparison",),
            data_access=("experiment",),
            side_effects=("artifact_write",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Write a deterministic comparison report and return the winner summary.

        Higher-is-better is assumed for every metric (accuracy, AUC): a lower-is-better metric
        such as loss would need an explicit direction, which the MVP does not yet model. Ties
        keep the earliest run in ``run_ids`` order, so the summary is byte-identical on a re-run.
        """
        tracker = MlflowTracker(invocation.workspace)
        primary = arguments["metric"]
        rows: list[dict[str, Any]] = []
        for run_id in arguments["run_ids"]:
            matches = tracker.query_runs(run_id=run_id)
            if not matches:
                raise ValueError(f"unknown tracker run: {run_id}")
            run = matches[0]
            rows.append(
                {
                    "run_id": run.run_id,
                    "experiment_name": run.experiment_name,
                    "metric": run.metrics.get(primary),
                    "metrics": dict(run.metrics),
                    "params": dict(run.params),
                }
            )
        ranking = [row["run_id"] for row in sorted(rows, key=_primary_metric_key, reverse=True)]
        payload = {
            "metric": primary,
            "runs": rows,
            "ranking": ranking,
            "winner": ranking[0],
            "winner_by_metric": _winners_by_metric(rows),
        }
        artifact = invocation.artifact_store.save_json(
            f"comparisons/{primary}.json",
            payload,
            produced_by=invocation.agent_id,
            kind=ArtifactKind.REPORT,
            media_type="application/json",
        )
        text = json.dumps(payload, sort_keys=True)
        return ToolResult(
            success=True,
            stdout=text,
            value=ComparisonValue(
                text=text,
                metric=primary,
                ranking=tuple(ranking),
                winner=ranking[0],
                winner_by_metric=payload["winner_by_metric"],
                artifact_id=artifact.id,
            ),
            artifact_ids=(artifact.id,),
        )


def _primary_metric_key(row: dict[str, Any]) -> float:
    """Sort key that pushes runs missing the primary metric to the bottom."""
    value = row["metric"]
    return float(value) if value is not None else float("-inf")


def _winners_by_metric(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Return, for every metric any run recorded, the run that scored highest on it."""
    winners: dict[str, str] = {}
    metric_names = sorted({name for row in rows for name in row["metrics"]})
    for name in metric_names:
        best_run: str | None = None
        best_value: float | None = None
        for row in rows:
            value = row["metrics"].get(name)
            if value is None:
                continue
            if best_value is None or value > best_value:
                best_value = value
                best_run = row["run_id"]
        if best_run is not None:
            winners[name] = best_run
    return winners


__all__ = ["CompareModels", "CompareModelsArguments"]
