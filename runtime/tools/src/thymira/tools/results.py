"""Typed values returned by tools and the canonical consumer projections.

The manager-owned :class:`~thymira.tools.models.ToolResult` carries lifecycle and execution
evidence.  These immutable values carry what a tool actually produced.  Keeping the two records
separate means a text adapter cannot accidentally become an authority source while still giving
every tool a schema that a registry and a verifier can inspect.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from thymira.schemas import Framework, ThymiraModel
from thymira.tools.models import ToolResult, ToolResultCode


class ToolValue(ThymiraModel):
    """Common immutable value returned by a successful or failed tool call."""

    text: str = ""


class ToolFailureValue(ToolValue):
    """Canonical value for a call rejected before a producer returned a value."""

    model_config = ConfigDict(allow_inf_nan=False)

    error: str = Field(min_length=1)
    code: ToolResultCode = ToolResultCode.FAILURE
    budget_seconds: float | None = Field(default=None, gt=0)
    aborted: bool = False


class ToolResultValidationError(ValueError):
    """Raised when a tool returns an envelope or typed value outside its contract."""


class ProcessToolValue(ToolValue):
    """Observed output from a process-backed tool."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


class FileReadValue(ToolValue):
    """A bounded file read, retaining the source and the applied bounds."""

    path: str = Field(min_length=1)
    content: str = ""
    offset: int = Field(ge=1)
    lines_returned: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    bytes_returned: int = Field(ge=0)
    truncated: bool = False


class FileWriteValue(ToolValue):
    """A completed write and the artifact it optionally registered."""

    path: str = Field(min_length=1)
    bytes_written: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class FileEditValue(ToolValue):
    """A literal edit and its exact replacement count."""

    path: str = Field(min_length=1)
    replacements: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class FileListingValue(ToolValue):
    """A bounded listing of files in a workspace directory."""

    paths: tuple[str, ...] = ()
    total_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)


class DiscoveryValue(ToolValue):
    """An ordered glob or grep result with complete-list accounting."""

    matches: tuple[str, ...] = ()
    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    omitted_count: int = Field(ge=0)
    complete_list_artifact_id: str | None = Field(default=None, min_length=1)


class DatasetAnalysisValue(ToolValue):
    """Shape facts from a dataset analysis report."""

    dataset: str = Field(min_length=1)
    rows: int = Field(ge=0)
    columns: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class ProfileValue(ToolValue):
    """Source binding and shape facts from a dataset profile report."""

    dataset: str = Field(min_length=1)
    profile_version: int = Field(ge=1)
    source_artifact: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: int = Field(ge=0)
    columns: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class StatisticsValue(ToolValue):
    """A statistical test result with the shared fields used by all test variants."""

    dataset: str = Field(min_length=1)
    test: str = Field(min_length=1)
    value_column: str = Field(min_length=1)
    statistic: float
    p_value: float
    artifact_id: str | None = Field(default=None, min_length=1)


class ExperimentValue(ToolValue):
    """The stable identifiers and metrics produced by a training experiment."""

    experiment_id: str = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)
    model_artifact_id: str = Field(min_length=1)
    tracker_run_id: str = Field(min_length=1)
    artifact_ids: tuple[str, ...] = ()


class ComparisonValue(ToolValue):
    """A tracker comparison with its ordered ranking and winner."""

    metric: str = Field(min_length=1)
    ranking: tuple[str, ...] = ()
    winner: str = Field(min_length=1)
    winner_by_metric: dict[str, str] = Field(default_factory=dict)
    artifact_id: str | None = Field(default=None, min_length=1)


class QueryRowsValue(ToolValue):
    """Bounded SQL rows represented by stable column order and row tuples."""

    model_config = ConfigDict(allow_inf_nan=False)

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str | int | float | bool | None, ...], ...] = ()
    row_count: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _rows_use_json_scalars(cls, data: Any) -> Any:
        """Reject source cells that Pydantic could otherwise coerce into JSON scalars."""
        if not isinstance(data, Mapping):
            return data
        columns = data.get("columns")
        if columns is not None and (
            isinstance(columns, (str, bytes))
            or not isinstance(columns, (tuple, list))
            or any(type(column) is not str for column in columns)
        ):
            raise ValueError("query columns must contain only strings")
        rows = data.get("rows")
        if rows is not None and (
            isinstance(rows, (str, bytes))
            or not isinstance(rows, (tuple, list))
            or any(
                isinstance(row, (str, bytes))
                or not isinstance(row, (tuple, list))
                or any(type(cell) not in (str, int, float, bool, type(None)) for cell in row)
                for row in rows
            )
        ):
            raise ValueError("query rows must contain only JSON scalar values")
        return data

    @model_validator(mode="after")
    def _rows_match_columns(self) -> Self:
        """Reject rows whose width cannot be interpreted against the declared columns."""
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("query rows must have the same width as columns")
        return self


class TrackerRunValue(ThymiraModel):
    """One locally tracked run returned by query_mlflow."""

    tracker_run_id: str = Field(min_length=1)
    experiment_name: str
    status: str
    params: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    artifacts: tuple[str, ...] = ()


class TrackerRunsValue(ToolValue):
    """Typed local tracker query output."""

    runs: tuple[TrackerRunValue, ...] = ()


class ModelInspectionValue(ToolValue):
    """Stable model inspection facts surfaced to an agent."""

    class_name: str = Field(min_length=1)
    pipeline_steps: tuple[tuple[str, str], ...] = ()
    artifact_id: str | None = Field(default=None, min_length=1)


class ModelAuditValue(ToolValue):
    """Stable model audit facts, with the full report retained in its artifact."""

    dataset: str = Field(min_length=1)
    reference_dataset: str | None = None
    subgroup_count: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class MlflowMutationValue(ToolValue):
    """Result of a tracker mutation, optionally naming the affected run."""

    tracker_run_id: str | None = Field(default=None, min_length=1)


class RegulationSearchMatch(ThymiraModel):
    """One citable regulation match and its deterministic ranking provenance."""

    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    framework: Framework
    location: str = Field(min_length=1)
    fragment: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(ge=0.0)
    backend: str = Field(min_length=1)


class RegulationSearchValue(ToolValue):
    """Deterministic regulation search results with citation and ranking provenance."""

    result_count: int = Field(ge=0)
    matches: tuple[RegulationSearchMatch, ...] = ()


class LegacyToolValue(ToolValue):
    """Compatibility value for test-only tools predating the typed result contract."""


def value_schema(model: type[BaseModel]) -> dict[str, object]:
    """Return the JSON schema advertised for a tool's typed output value."""
    return model.model_json_schema()


def result_schema(tool: Any) -> dict[str, object]:
    """Return the declared output schema for one tool, or an empty schema for legacy fakes."""
    model = result_model_for(tool)
    return value_schema(model) if model is not None else {}


def value_text(value: BaseModel) -> str:
    """Return the canonical model-facing text projection from a validated typed value."""
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        raise TypeError("typed tool value must expose string text")
    return text


def result_model_for(tool: Any) -> type[BaseModel] | None:
    """Return a tool's declared output model, if it has one."""
    model = getattr(tool, "result_model", None)
    return model if isinstance(model, type) and issubclass(model, BaseModel) else None


def validate_tool_result(  # noqa: PLR0912  # this boundary validates every producer-controlled field
    tool: Any, result: Any
) -> ToolResult:
    """Validate one producer result and make its typed value the projection source.

    Registration and dispatch require ``result_model`` for every tool.  A successful call with no
    value is an explicit contract failure; a failed call receives the manager-owned typed failure
    value so all exits still have one canonical shape, while keeping the ``stdout``, ``stderr``
    and ``exit_code`` the runtime observed for it.
    """
    if not isinstance(result, ToolResult):
        raise ToolResultValidationError("tool returned a value that is not ToolResult")
    if not isinstance(result.success, bool):
        raise ToolResultValidationError("tool result success must be a boolean")
    if result.error is not None and not isinstance(result.error, str):
        raise ToolResultValidationError("tool result error must be a string")
    if result.code is not None and not isinstance(result.code, ToolResultCode):
        raise ToolResultValidationError("tool result code must be a ToolResultCode")
    if result.timeout_s is not None and (
        type(result.timeout_s) not in (int, float)
        or not math.isfinite(result.timeout_s)
        or result.timeout_s <= 0
    ):
        raise ToolResultValidationError("tool result timeout budget must be a positive number")
    if not isinstance(result.aborted, bool):
        raise ToolResultValidationError("tool result aborted must be a boolean")
    model = result_model_for(tool)
    if model is None:
        raise ToolResultValidationError(
            f"tool '{getattr(tool, 'name', '<unknown>')}' has no typed result model"
        )
    if not result.success:
        error = result.error or "tool call failed"
        code = result.code or ToolResultCode.FAILURE
        if code is ToolResultCode.SUCCESS:
            code = ToolResultCode.FAILURE
        try:
            value = ToolFailureValue(
                text=error,
                error=error,
                code=code,
                budget_seconds=result.timeout_s if code is ToolResultCode.TOOL_TIMEOUT else None,
                aborted=result.aborted or code is ToolResultCode.TOOL_TIMEOUT,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ToolResultValidationError("tool result failure metadata is invalid") from exc
    elif result.value is None:
        raise ToolResultValidationError(
            f"tool '{getattr(tool, 'name', '<unknown>')}' returned no typed result value"
        )
    else:
        try:
            value = model.model_validate(result.value)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ToolResultValidationError(
                f"tool '{getattr(tool, 'name', '<unknown>')}' returned an invalid typed result"
            ) from exc
    try:
        text = value_text(value)
    except (TypeError, ValueError) as exc:
        raise ToolResultValidationError(
            f"tool '{getattr(tool, 'name', '<unknown>')}' typed result has no text projection"
        ) from exc
    code = result.code or (ToolResultCode.SUCCESS if result.success else ToolResultCode.FAILURE)
    if result.success:
        if code is not ToolResultCode.SUCCESS:
            raise ToolResultValidationError("successful tool result has a non-success result code")
    elif code is ToolResultCode.SUCCESS:
        code = ToolResultCode.FAILURE
    if not result.success:
        # The manager-owned failure value replaces what the producer claimed, never what the
        # runtime observed: ``stdout``, ``stderr`` and ``exit_code`` are the process facts the
        # sandbox recorded for this call, so they survive validation for every declared result
        # model.  The error text stays in ``error`` and in the typed failure value rather than
        # overwriting a child's own output.
        return replace(result, code=code, value=value)
    # The typed value is authoritative for projections.  Keep the envelope's process facts in
    # sync where a process family carries them, so legacy scalar consumers cannot diverge.
    if isinstance(value, ProcessToolValue):
        return replace(
            result,
            code=code,
            value=value,
            stdout=text,
            stderr=value.stderr,
            exit_code=value.exit_code,
        )
    # A producer may still populate the legacy stderr field while returning a typed value.  That
    # field is outside the canonical envelope for every non-process value, so retaining it would
    # let a model or adapter observe data that durable evidence cannot reconstruct.
    return replace(result, code=code, value=value, stdout=text, stderr="")


def canonical_value(value: BaseModel) -> dict[str, object]:
    """Return a JSON-compatible canonical value payload for durable evidence and adapters."""
    payload = value.model_dump(mode="json")
    if not isinstance(payload, dict):
        raise TypeError("typed tool value must serialise to an object")
    return payload


def canonical_result(value: BaseModel, *, success: bool) -> dict[str, object]:
    """Return the discriminated result envelope persisted for one tool completion.

    The discriminator is part of the durable payload rather than inferred from a sibling event
    status.  This lets an independent reader select the producer's registered success model or
    the manager-owned failure model from the value itself.
    """
    if not isinstance(success, bool):
        raise TypeError("result success discriminator must be a boolean")
    if success:
        if isinstance(value, ToolFailureValue):
            raise ToolResultValidationError("successful result cannot use the failure value model")
        return {"kind": "success", "value": canonical_value(value)}
    if not isinstance(value, ToolFailureValue):
        raise ToolResultValidationError("failed result must use the failure value model")
    return {"kind": "failure", "value": canonical_value(value)}


def reconstruct_value(tool: Any, payload: Mapping[str, object]) -> BaseModel:
    """Rebuild a typed value from durable evidence and the registry declaration.

    This verifier path intentionally does not read ``ToolResult.stdout`` or call a producer.  A
    consumer can therefore check that an event's canonical value satisfies the registered schema
    and derive its own projection from exactly the value that was recorded.
    """
    model = result_model_for(tool)
    if model is None:
        raise ToolResultValidationError("tool has no registered result schema")
    if set(payload) != {"kind", "value"}:
        raise ToolResultValidationError("durable tool result has an invalid discriminated envelope")
    kind = payload.get("kind")
    value_payload = payload.get("value")
    if (
        not isinstance(kind, str)
        or kind not in {"success", "failure"}
        or not isinstance(value_payload, Mapping)
    ):
        raise ToolResultValidationError("durable tool result has an invalid discriminated envelope")
    selected_model: type[BaseModel] = ToolFailureValue if kind == "failure" else model
    try:
        return selected_model.model_validate(value_payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ToolResultValidationError(
            "durable tool value does not satisfy its result schema"
        ) from exc


__all__ = [
    "ComparisonValue",
    "DatasetAnalysisValue",
    "DiscoveryValue",
    "ExperimentValue",
    "FileEditValue",
    "FileListingValue",
    "FileReadValue",
    "FileWriteValue",
    "LegacyToolValue",
    "MlflowMutationValue",
    "ModelAuditValue",
    "ModelInspectionValue",
    "ProcessToolValue",
    "ProfileValue",
    "QueryRowsValue",
    "RegulationSearchMatch",
    "RegulationSearchValue",
    "StatisticsValue",
    "ToolFailureValue",
    "ToolResultValidationError",
    "ToolValue",
    "TrackerRunValue",
    "TrackerRunsValue",
    "canonical_result",
    "canonical_value",
    "reconstruct_value",
    "result_model_for",
    "result_schema",
    "validate_tool_result",
    "value_schema",
    "value_text",
]
