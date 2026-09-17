"""Dataset analysis and profiling tools."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import polars as pl


from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.datasets import DatasetSchema, load_dataset, schema_artifact_name
from thymira.tools.models import ToolInvocation, ToolResult
from thymira.tools.results import DatasetAnalysisValue, ProfileValue, ToolValue

_MIN_CORRELATION_COLUMNS = 2


class DatasetArguments(BaseModel):
    """Shared dataset tool arguments."""

    dataset: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    columns: tuple[str, ...] | None = None
    max_rows: int = Field(default=100_000, gt=0, le=1_000_000)


def _frame(invocation: ToolInvocation, arguments: dict[str, Any]) -> pl.DataFrame:
    frame = load_dataset(
        invocation.artifact_store,
        arguments["dataset"],
        max_rows=arguments.get("max_rows", 100_000),
    )
    columns = arguments.get("columns")
    if columns is None:
        return frame
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"unknown dataset columns: {', '.join(missing)}")
    return frame.select(list(columns))


def _numeric(series: pl.Series) -> bool:
    return series.dtype.is_numeric()


def _save_report(
    invocation: ToolInvocation,
    name: str,
    payload: dict[str, Any],
    kind: ArtifactKind,
    value: ToolValue,
) -> ToolResult:
    artifact = invocation.artifact_store.save_json(
        name,
        payload,
        produced_by=invocation.agent_id,
        kind=kind,
        media_type="application/json",
    )
    text = json.dumps(payload, sort_keys=True)
    typed = value.model_copy(update={"text": text, "artifact_id": artifact.id})
    return ToolResult(success=True, stdout=text, value=typed, artifact_ids=(artifact.id,))


def _profile_source(invocation: ToolInvocation, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return verified registered-source metadata for a dataset profile."""
    dataset = arguments["dataset"]
    schema_name = schema_artifact_name(dataset)
    schema_artifact = invocation.artifact_store.get(schema_name)
    if schema_artifact is None or not invocation.artifact_store.exists(schema_name):
        msg = f"active dataset schema artifact is required: {schema_name}"
        raise ValueError(msg)
    schema_bytes = invocation.artifact_store.load_bytes(schema_name)
    if sha256(schema_bytes).hexdigest() != schema_artifact.sha256:
        msg = f"registered dataset schema artifact has changed: {schema_name}"
        raise ValueError(msg)
    schema = DatasetSchema.model_validate_json(schema_bytes)
    if schema.name != dataset:
        msg = f"dataset schema name does not match requested dataset: {dataset}"
        raise ValueError(msg)

    source_artifact = invocation.artifact_store.get(schema.artifact_name)
    if source_artifact is None or not invocation.artifact_store.exists(schema.artifact_name):
        msg = f"active dataset artifact is required: {schema.artifact_name}"
        raise ValueError(msg)
    if source_artifact.kind is not ArtifactKind.DATASET:
        msg = f"registered source is not a dataset artifact: {schema.artifact_name}"
        raise ValueError(msg)
    source_sha256 = sha256(invocation.artifact_store.load_bytes(schema.artifact_name)).hexdigest()
    if source_sha256 != source_artifact.sha256 or source_sha256 != schema.sha256:
        msg = f"registered dataset artifact has changed: {schema.artifact_name}"
        raise ValueError(msg)

    selected_columns = arguments["columns"]
    return {
        "artifact": schema.artifact_name,
        "sha256": source_artifact.sha256,
        "schema_sha256": schema_artifact.sha256,
        "max_rows": arguments["max_rows"],
        "columns": None if selected_columns is None else list(selected_columns),
    }


@dataclass(frozen=True, slots=True)
class AnalyzeDataset:
    """Produce bounded descriptive metrics for a registered dataset."""

    name: str = "analyze_dataset"
    description: str = "Summarize shape, missingness, cardinality and numeric statistics."
    arguments_model: type[BaseModel] = DatasetArguments
    result_model: type[BaseModel] = DatasetAnalysisValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="analyze_dataset",
            data_access=("dataset",),
            side_effects=("artifact_write",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Analyze the selected registered dataset."""
        frame = _frame(invocation, arguments)
        columns: dict[str, dict[str, Any]] = {}
        for name in frame.columns:
            series = frame.get_column(name)
            values: dict[str, Any] = {
                "dtype": str(series.dtype),
                "null_count": series.null_count(),
                "unique_count": series.n_unique(),
            }
            if _numeric(series):
                values.update(
                    {
                        "min": _finite(series.min()),
                        "max": _finite(series.max()),
                        "mean": _finite(series.mean()),
                        "std": _finite(series.std()),
                    }
                )
            columns[name] = values
        payload = {
            "dataset": arguments["dataset"],
            "shape": {"rows": frame.height, "columns": frame.width},
            "columns": columns,
        }
        return _save_report(
            invocation,
            f"analysis/{arguments['dataset']}.json",
            payload,
            ArtifactKind.METRICS,
            DatasetAnalysisValue(
                dataset=arguments["dataset"],
                rows=frame.height,
                columns=frame.width,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProfileDataset:
    """Produce missingness, distributions and numeric correlations."""

    name: str = "profile_dataset"
    description: str = (
        "Create a deterministic JSON profile (missingness, distributions, numeric correlations) "
        "of a registered dataset and save it as a report artifact. Use it before modelling; it "
        "is the only correct way to look at a dataset, because the profile is bounded and "
        "reproducible."
    )
    arguments_model: type[BaseModel] = DatasetArguments
    result_model: type[BaseModel] = ProfileValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="profile_dataset",
            data_access=("dataset",),
            side_effects=("artifact_write",),
            external_effects=(),
        )
    )

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Profile the selected registered dataset."""
        effective_arguments = DatasetArguments.model_validate(arguments).model_dump()
        source = _profile_source(invocation, effective_arguments)
        frame = _frame(invocation, effective_arguments)
        columns: dict[str, Any] = {}
        numeric_names: list[str] = []
        for name in frame.columns:
            series = frame.get_column(name)
            if _numeric(series):
                numeric_names.append(name)
            columns[name] = {
                "null_count": series.null_count(),
                "unique_count": series.n_unique(),
                "top_values": series.value_counts(sort=True).head(20).to_dicts(),
            }
            if _numeric(series):
                columns[name]["histogram"] = _histogram(series)
        correlations: dict[str, Any] = {}
        if len(numeric_names) >= _MIN_CORRELATION_COLUMNS:
            correlations = frame.select(numeric_names).corr().to_dict(as_series=False)
        payload = {
            "profile_version": 1,
            "source": source,
            "dataset": effective_arguments["dataset"],
            "shape": {"rows": frame.height, "columns": frame.width},
            "columns": columns,
            "correlation": correlations,
        }
        return _save_report(
            invocation,
            f"profile/{effective_arguments['dataset']}.json",
            payload,
            ArtifactKind.REPORT,
            ProfileValue(
                dataset=effective_arguments["dataset"],
                profile_version=payload["profile_version"],
                source_artifact=source["artifact"],
                source_sha256=source["sha256"],
                schema_sha256=source["schema_sha256"],
                rows=frame.height,
                columns=frame.width,
            ),
        )


def _finite(value: Any) -> float | int | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _histogram(series: pl.Series, bins: int = 10) -> dict[str, Any]:
    """Build a deterministic bounded histogram without a plotting dependency.

    ``drop_nulls`` removes polars nulls but not float ``NaN``, and a ``NaN`` reaching the ``int``
    bin conversion below raises ``ValueError: cannot convert float NaN to integer``. Every other
    numeric path in this module routes through :func:`_finite`; this one does too, so ``NaN`` and
    the infinities are dropped rather than crashing the profile.
    """
    values = [
        finite for value in series.drop_nulls().to_list() if (finite := _finite(value)) is not None
    ]
    if not values:
        return {"edges": [], "counts": []}
    lower, upper = min(values), max(values)
    if lower == upper:
        return {"edges": [lower, upper], "counts": [len(values)]}
    width = (upper - lower) / bins
    counts = [0] * bins
    for value in values:
        index = min(int((value - lower) / width), bins - 1)
        counts[index] += 1
    edges = [lower + width * index for index in range(bins + 1)]
    return {"edges": edges, "counts": counts}


__all__ = ["AnalyzeDataset", "DatasetArguments", "ProfileDataset"]
