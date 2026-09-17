"""Read-side tracker query tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from thymira.policies import ToolCapability
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import TrackerRunsValue, TrackerRunValue


class QueryMlflowArguments(BaseModel):
    """Optional tracker query filters.

    A filter is either absent or a real id. An empty string used to be neither: it was falsy, so
    the tracker skipped the filter and returned every run it held, which is the opposite of what a
    caller asking for run ``""`` wants and the reason ``compare_models``' missing-id guard never
    fired. The tracker now compares against ``None``, and an empty id is refused here, before any
    tool runs.
    """

    run_id: str | None = Field(default=None, min_length=1)
    experiment_name: str | None = Field(default=None, min_length=1)


@dataclass(frozen=True, slots=True)
class QueryMlflow:
    """List local tracker runs and their parameters and metrics."""

    name: str = "query_mlflow"
    description: str = "Read local experiment tracker runs."
    arguments_model: type[BaseModel] = QueryMlflowArguments
    result_model: type[BaseModel] = TrackerRunsValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="query_mlflow",
            data_access=("experiment",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return matching tracker runs."""
        tracker = MlflowTracker(invocation.workspace)
        runs = tracker.query_runs(
            run_id=arguments.get("run_id"),
            experiment_name=arguments.get("experiment_name"),
        )
        payload = [
            {
                "run_id": run.run_id,
                "experiment_name": run.experiment_name,
                "status": run.status,
                "params": run.params,
                "metrics": run.metrics,
                "tags": run.tags,
                "artifacts": run.artifacts,
            }
            for run in runs
        ]
        text = json.dumps(payload, sort_keys=True)
        value = TrackerRunsValue(
            text=text,
            runs=tuple(
                TrackerRunValue(
                    tracker_run_id=run.run_id,
                    experiment_name=run.experiment_name,
                    status=run.status,
                    params=run.params,
                    metrics=run.metrics,
                    tags=run.tags,
                    artifacts=run.artifacts,
                )
                for run in runs
            ),
        )
        return ToolResult(success=True, stdout=text, value=value)


__all__ = ["QueryMlflow", "QueryMlflowArguments"]
