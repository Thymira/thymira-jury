"""Tool wrappers for the local experiment tracker."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field, model_validator

from thymira.policies import ToolCapability
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.files import _contained
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import MlflowMutationValue

if TYPE_CHECKING:
    from thymira.tools.models import Tool


def _capability(name: str, *, writes: bool = True) -> ToolCapability:
    return ToolCapability(
        id=name,
        data_access=("experiment",),
        # The tracker persists its own bounded records under the Run workspace; the tool runs
        # no code of the model's, so this is an artifact write, not an arbitrary one.
        side_effects=("artifact_write",) if writes else (),
        external_effects=(),
    )


class StartRunArguments(BaseModel):
    """Arguments for starting a tracker run."""

    description: Description = DESCRIPTION_FIELD
    experiment_name: str = Field(default="default", min_length=1)
    tags: dict[str, str] = Field(default_factory=dict)


class RunIdArguments(BaseModel):
    """Arguments identifying a tracker run."""

    description: Description = DESCRIPTION_FIELD
    run_id: str = Field(min_length=1)


def _one_form_only(key: str | None, single: object, batch: dict[str, Any], *, what: str) -> None:
    """Accept exactly one of ``key``+``value`` or a non-empty batch mapping."""
    if key is None and single is None and not batch:
        raise ValueError(f"log either key and value or a non-empty {what} mapping")
    if (key is not None or single is not None) and batch:
        raise ValueError(f"log either key and value or {what}, not both")
    if (key is None) != (single is None):
        raise ValueError("key and value must be given together")


class LogParamArguments(RunIdArguments):
    """Arguments for logging one parameter, or several at once through ``params``."""

    key: str | None = Field(default=None, min_length=1)
    value: str | int | float | bool | None = None
    params: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Several parameters in one call: {name: value}. Use instead of key/value.",
    )

    @model_validator(mode="after")
    def _exactly_one_form(self) -> LogParamArguments:
        _one_form_only(self.key, self.value, self.params, what="params")
        return self

    def items(self) -> dict[str, str]:
        """Every parameter this call logs as the tracker stores it (a string)."""
        if self.key is not None and self.value is not None:
            return {self.key: str(self.value)}
        return {key: str(value) for key, value in self.params.items()}


class LogMetricArguments(RunIdArguments):
    """Arguments for logging one metric, or several at once through ``metrics``."""

    key: str | None = Field(default=None, min_length=1)
    value: float | None = None
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Several metrics in one call: {name: number}. Use instead of key/value.",
    )

    @model_validator(mode="after")
    def _exactly_one_form(self) -> LogMetricArguments:
        _one_form_only(self.key, self.value, self.metrics, what="metrics")
        return self

    def items(self) -> dict[str, float]:
        """Every metric this call logs, whichever form the caller used."""
        if self.key is not None and self.value is not None:
            return {self.key: self.value}
        return dict(self.metrics)


class LogArtifactArguments(RunIdArguments):
    """Arguments for logging a workspace artifact."""

    path: str = Field(min_length=1)


class EndRunArguments(RunIdArguments):
    """Arguments for ending a tracker run."""

    status: str = Field(default="FINISHED", min_length=1)


@dataclass(frozen=True, slots=True)
class _TrackerTool:
    """Shared tracker construction."""

    result_model: type[BaseModel] = MlflowMutationValue

    def _tracker(self, invocation: ToolInvocation) -> MlflowTracker:
        return MlflowTracker(Path(invocation.workspace))

    def _result(self, run_id: str) -> ToolResult:
        text = json.dumps({"tracker_run_id": run_id})
        return ToolResult(
            success=True,
            stdout=text,
            value=MlflowMutationValue(text=text, tracker_run_id=run_id),
        )


@dataclass(frozen=True, slots=True)
class MlflowStartRun(_TrackerTool):
    """Start a local tracker run."""

    name: str = "mlflow_start_run"
    description: str = "Start a local experiment tracker run."
    arguments_model: type[BaseModel] = StartRunArguments
    capability: ToolCapability = field(default_factory=lambda: _capability("mlflow_start_run"))

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Start the run and return its id."""
        return self._result(
            self._tracker(invocation).start_run(
                arguments["experiment_name"],
                tags=arguments.get("tags"),
            )
        )


@dataclass(frozen=True, slots=True)
class MlflowLogParam(_TrackerTool):
    """Log one tracker parameter."""

    name: str = "mlflow_log_param"
    description: str = (
        "Log parameters to a local tracker run: one with key/value, or several in one call "
        "with params={name: value}. Prefer the batch form; every call costs a turn."
    )
    arguments_model: type[BaseModel] = LogParamArguments
    capability: ToolCapability = field(default_factory=lambda: _capability("mlflow_log_param"))

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Log one or several parameters and return the run id."""
        tracker = self._tracker(invocation)
        for key, value in LogParamArguments.model_validate(arguments).items().items():
            tracker.log_param(arguments["run_id"], key, value)
        return self._result(arguments["run_id"])


@dataclass(frozen=True, slots=True)
class MlflowLogMetric(_TrackerTool):
    """Log one tracker metric."""

    name: str = "mlflow_log_metric"
    description: str = (
        "Log metrics to a local tracker run: one with key/value, or several in one call "
        "with metrics={name: number}. Prefer the batch form; every call costs a turn."
    )
    arguments_model: type[BaseModel] = LogMetricArguments
    capability: ToolCapability = field(default_factory=lambda: _capability("mlflow_log_metric"))

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Log one or several metrics and return the run id."""
        tracker = self._tracker(invocation)
        for key, value in LogMetricArguments.model_validate(arguments).items().items():
            tracker.log_metric(arguments["run_id"], key, value)
        return self._result(arguments["run_id"])


@dataclass(frozen=True, slots=True)
class MlflowLogArtifact(_TrackerTool):
    """Log one workspace artifact."""

    name: str = "mlflow_log_artifact"
    description: str = "Copy a workspace artifact into a local tracker run."
    arguments_model: type[BaseModel] = LogArtifactArguments
    capability: ToolCapability = field(default_factory=lambda: _capability("mlflow_log_artifact"))

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Log an artifact and return the run id."""
        path = _contained(Path(invocation.workspace), arguments["path"])
        self._tracker(invocation).log_artifact(arguments["run_id"], path)
        return self._result(arguments["run_id"])


@dataclass(frozen=True, slots=True)
class MlflowEndRun(_TrackerTool):
    """End one tracker run."""

    name: str = "mlflow_end_run"
    description: str = "Finish a local tracker run."
    arguments_model: type[BaseModel] = EndRunArguments
    capability: ToolCapability = field(default_factory=lambda: _capability("mlflow_end_run"))

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Finish the run and return its id."""
        self._tracker(invocation).end_run(arguments["run_id"], status=arguments["status"])
        return self._result(arguments["run_id"])


def mlflow_tools() -> tuple[Tool, ...]:
    """Return the five tracker tools in stable registration order."""
    return tuple(
        # Each class implements the Tool protocol; the cast keeps the public registry typed.
        cast("Tool", tool)
        for tool in (
            MlflowStartRun(),
            MlflowLogParam(),
            MlflowLogMetric(),
            MlflowLogArtifact(),
            MlflowEndRun(),
        )
    )


__all__ = [
    "MlflowEndRun",
    "MlflowLogArtifact",
    "MlflowLogMetric",
    "MlflowLogParam",
    "MlflowStartRun",
    "mlflow_tools",
]
