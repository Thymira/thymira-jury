"""Strict IPC records for sandboxed model prediction."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from thymira.tools.model_sidecars import read_bounded_file
from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_PREDICTION_BYTES = 64 * 1024 * 1024
"""Largest accepted serialized prediction envelope."""


class PredictionRequest(BaseModel):
    """Workspace-relative inputs consumed by the model prediction worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_path: str = Field(min_length=1)
    dataset_path: str = Field(min_length=1)
    target_column: str = Field(min_length=1)


class ModelPredictions(BaseModel):
    """Versioned model outputs returned across the sandbox boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    format_version: Literal[1]
    feature_names: tuple[str, ...]
    class_labels: tuple[str, ...]
    predictions: tuple[str, ...]
    positive_probabilities: tuple[Annotated[float, Field(ge=0.0, le=1.0)], ...] | None = None

    @field_validator("format_version", mode="before")
    @classmethod
    def _reject_boolean_version(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004  # Pydantic validators require a validation error
                "format version must be an integer"
            )
        return value


def normal_label(value: str) -> str:
    """Return a spelling-independent representation of a label."""
    try:
        number = float(value)
    except ValueError:
        return value
    return repr(int(number)) if number.is_integer() else repr(number)


def read_model_predictions(
    path: Path, *, columns: Sequence[str], targets: Sequence[str]
) -> ModelPredictions:
    """Read and semantically validate a bounded prediction envelope.

    Raises:
        ToolExecutionError: If the worker output is missing, malformed, oversized, or disagrees
            with the audited dataset.
    """
    source = Path(path)
    metadata = _prediction_stat(source)
    if not stat.S_ISREG(metadata.st_mode):
        raise ToolExecutionError("model prediction envelope is not a regular file")
    if metadata.st_size > MAX_PREDICTION_BYTES:
        raise ToolExecutionError(f"model prediction output exceeds {MAX_PREDICTION_BYTES} bytes")
    try:
        result = ModelPredictions.model_validate_json(
            read_bounded_file(
                source,
                max_bytes=MAX_PREDICTION_BYTES,
                label="model prediction envelope",
            )
        )
    except ToolExecutionError:
        raise
    except (ValidationError, ValueError) as exc:
        raise ToolExecutionError("model prediction envelope is missing or invalid") from exc

    return _validate_predictions(result, columns=columns, targets=targets)


def _prediction_stat(path: Path) -> Any:
    try:
        return path.stat()
    except OSError as exc:
        raise ToolExecutionError("model prediction envelope is missing or invalid") from exc


def _validate_predictions(
    result: ModelPredictions, *, columns: Sequence[str], targets: Sequence[str]
) -> ModelPredictions:
    feature_names = result.feature_names
    if len(set(feature_names)) != len(feature_names):
        raise ToolExecutionError("model prediction output contains duplicate feature names")
    unknown = sorted(set(feature_names) - set(columns))
    if unknown:
        raise ToolExecutionError(
            f"model prediction output contains unknown features: {', '.join(unknown)}"
        )

    normalized_classes = [normal_label(label) for label in result.class_labels]
    if len(set(normalized_classes)) != len(normalized_classes):
        raise ToolExecutionError("model prediction output contains duplicate class labels")
    if not normalized_classes:
        raise ToolExecutionError("model prediction output contains no class labels")
    if result.positive_probabilities is not None and len(normalized_classes) != 2:  # noqa: PLR2004
        raise ToolExecutionError("positive probabilities require exactly two class labels")

    unknown_predictions = sorted(
        prediction
        for prediction in set(result.predictions)
        if normal_label(prediction) not in normalized_classes
    )
    if unknown_predictions:
        raise ToolExecutionError(
            "model predictions fall outside the declared classes: " + ", ".join(unknown_predictions)
        )
    if len(result.predictions) != len(targets):
        raise ToolExecutionError("model prediction row count does not match the audited dataset")
    if result.positive_probabilities is not None and len(result.positive_probabilities) != len(
        targets
    ):
        raise ToolExecutionError("model probability row count does not match the audited dataset")

    unknown_targets = sorted(
        target for target in set(targets) if normal_label(target) not in normalized_classes
    )
    if unknown_targets:
        raise ToolExecutionError(
            "audited target labels fall outside the model classes: " + ", ".join(unknown_targets)
        )
    target_spellings = {normal_label(target): target for target in targets}
    aligned_classes = tuple(
        target_spellings.get(normal_label(label), label) for label in result.class_labels
    )
    by_normal = {normal_label(label): label for label in aligned_classes}
    aligned_predictions = tuple(by_normal[normal_label(value)] for value in result.predictions)
    return result.model_copy(
        update={"class_labels": aligned_classes, "predictions": aligned_predictions}
    )


__all__ = [
    "MAX_PREDICTION_BYTES",
    "ModelPredictions",
    "PredictionRequest",
    "normal_label",
    "read_model_predictions",
]
