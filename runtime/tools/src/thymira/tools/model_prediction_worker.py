"""Sandbox worker that deserializes a model and returns predictions as strict JSON."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import joblib
import polars as pl
from pydantic import ValidationError

from thymira.tools.model_predictions import (
    MAX_PREDICTION_BYTES,
    ModelPredictions,
    PredictionRequest,
    normal_label,
)
from thymira.tools.model_sidecars import read_bounded_json
from thymira.tools.models import ToolExecutionError

_REQUEST_BYTES = 1024 * 1024


def create_model_predictions(request: PredictionRequest, *, workspace: Path) -> ModelPredictions:
    """Deserialize and execute one model using inputs contained in ``workspace``."""
    root = Path(workspace).resolve()
    model_path = _contained_relative_path(root, request.model_path)
    dataset_path = _contained_relative_path(root, request.dataset_path)
    frame = pl.read_parquet(dataset_path)
    if request.target_column not in frame.columns:
        raise ValueError(f"audit dataset is missing target column: {request.target_column}")

    model = joblib.load(model_path)
    features = _feature_order(model, frame, request.target_column)
    values = frame.select(features).to_numpy().tolist()
    targets = [str(value) for value in frame.get_column(request.target_column).to_list()]
    class_labels = _class_labels(model, targets)
    predictions = _decode_predictions(model.predict(values), class_labels)
    probabilities = _positive_probabilities(model, values, len(class_labels))
    return ModelPredictions(
        format_version=1,
        feature_names=tuple(features),
        class_labels=tuple(class_labels),
        predictions=tuple(predictions),
        positive_probabilities=probabilities,
    )


def main(argv: list[str] | None = None) -> int:
    """Read one request and write one response beneath the current workspace."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:  # noqa: PLR2004  # fixed CLI arity
        sys.stderr.write("usage: model_prediction_worker REQUEST RESPONSE\n")
        return 2
    workspace = Path.cwd().resolve()
    try:
        request_path = _contained_relative_path(workspace, arguments[0])
        response_path = _contained_relative_path(workspace, arguments[1])
        request = _read_request(request_path)
        result = create_model_predictions(request, workspace=workspace)
        response_path.write_bytes(_encode_response(result))
    except (OSError, ValueError, ToolExecutionError, ValidationError) as exc:
        sys.stderr.write(f"model prediction worker failed: {exc}\n")
        return 1
    return 0


def _read_request(path: Path) -> PredictionRequest:
    return read_bounded_json(
        path,
        PredictionRequest,
        max_bytes=_REQUEST_BYTES,
        label="model prediction request",
    )


def _encode_response(result: ModelPredictions) -> bytes:
    encoded = result.model_dump_json().encode("utf-8") + b"\n"
    if len(encoded) > MAX_PREDICTION_BYTES:
        raise ValueError("model prediction output is too large")
    return encoded


def _contained_relative_path(workspace: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError("worker paths must be relative paths contained in the workspace")
    resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("worker paths must be relative paths contained in the workspace") from exc
    return resolved


def _feature_order(model: Any, frame: pl.DataFrame, target_column: str) -> list[str]:
    names = getattr(model, "feature_names_in_", None)
    if names is None:
        return [name for name in frame.columns if name != target_column]
    expected = [str(name) for name in names]
    missing = [name for name in expected if name not in frame.columns]
    if missing:
        raise ValueError(f"dataset is missing model features: {', '.join(missing)}")
    return expected


def _class_labels(model: Any, targets: list[str]) -> list[str]:
    classes = getattr(model, "classes_", None)
    if classes is None:
        return sorted(set(targets))
    labels = [str(value) for value in classes]
    observed = set(targets)
    if not observed:
        return labels
    frame_spelling = {normal_label(value): value for value in observed}
    known = {normal_label(label) for label in labels}
    unpredictable = sorted(value for value in observed if normal_label(value) not in known)
    if unpredictable:
        raise ValueError(
            "the model cannot predict labels the dataset carries: "
            f"{', '.join(unpredictable)} (the model's classes are {', '.join(labels)}); "
            "the model was most likely trained on an encoding of the target column that was "
            "not saved with it"
        )
    return [frame_spelling.get(normal_label(label), label) for label in labels]


def _decode_predictions(values: Any, labels: list[str]) -> list[str]:
    by_normal = {normal_label(label): label for label in labels}
    decoded: list[str] = []
    for value in values:
        text = str(value)
        if text in labels:
            decoded.append(text)
            continue
        aligned = by_normal.get(normal_label(text))
        if aligned is None:
            raise ValueError(
                f"the model predicted {text!r}, which is not one of the classes it declares"
            )
        decoded.append(aligned)
    return decoded


def _positive_probabilities(
    model: Any, values: list[list[Any]], class_count: int
) -> tuple[float, ...] | None:
    if not hasattr(model, "predict_proba") or class_count != 2:  # noqa: PLR2004
        return None
    probabilities = model.predict_proba(values)
    if len(probabilities) != len(values):
        raise ValueError("model probability row count does not match its predictions")
    try:
        if any(len(row) != 2 for row in probabilities):  # noqa: PLR2004
            raise ValueError("model probabilities must have exactly two columns")
        return tuple(float(row[1]) for row in probabilities)
    except (IndexError, TypeError) as exc:
        raise ValueError("model probabilities must have exactly two columns") from exc


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_model_predictions", "main"]
