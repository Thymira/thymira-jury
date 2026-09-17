"""Prediction IPC contract and isolated model worker tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import polars as pl
import pytest
from pydantic import ValidationError

from thymira.tools.model_prediction_worker import create_model_predictions, main
from thymira.tools.model_predictions import (
    MAX_PREDICTION_BYTES,
    ModelPredictions,
    PredictionRequest,
    read_model_predictions,
)
from thymira.tools.models import ToolExecutionError


class _FixedModel:
    """Small serializable estimator double for the worker boundary."""

    feature_names_in_ = ("score",)
    classes_ = (0, 1)

    def predict(self, values: list[list[Any]]) -> list[int]:
        return [int(float(row[0]) >= 0.5) for row in values]

    def predict_proba(self, values: list[list[Any]]) -> list[list[float]]:
        return [[1.0 - float(row[0]), float(row[0])] for row in values]


class _FrameMetadataModel:
    """Estimator double that leaves feature and class metadata to the frame."""

    def predict(self, values: list[list[Any]]) -> list[str]:
        return [str(row[0]) for row in values]


class _MissingFeatureModel(_FixedModel):
    feature_names_in_ = ("absent",)


class _UnknownPredictionModel(_FixedModel):
    def predict(self, values: list[list[Any]]) -> list[int]:
        return [2 for _ in values]


class _MalformedProbabilityModel(_FixedModel):
    def predict_proba(self, values: list[list[Any]]) -> list[list[float]]:
        return [[1.0] for _ in values]


class _WideProbabilityModel(_FixedModel):
    def predict_proba(self, values: list[list[Any]]) -> list[list[float]]:
        return [[0.2, 0.8, 0.0] for _ in values]


def _valid_payload() -> dict[str, object]:
    return {
        "format_version": 1,
        "feature_names": ["score"],
        "class_labels": ["0", "1"],
        "predictions": ["0", "1"],
        "positive_probabilities": [0.2, 0.8],
    }


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def test_prediction_contracts_are_frozen_and_forbid_extra_fields() -> None:
    request = PredictionRequest(
        model_path=".thymira/model.joblib",
        dataset_path=".thymira/audit.parquet",
        target_column="target",
    )

    field_name = "model_path"
    with pytest.raises(ValidationError):
        setattr(request, field_name, "other.joblib")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelPredictions.model_validate({**_valid_payload(), "trusted": True})


def test_read_model_predictions_accepts_a_valid_numeric_label_envelope(tmp_path: Path) -> None:
    response = tmp_path / "predictions.json"
    _write_payload(response, _valid_payload())

    predictions = read_model_predictions(
        response,
        columns=("score", "target"),
        targets=("0.0", "1.0"),
    )

    assert predictions.class_labels == ("0.0", "1.0")
    assert predictions.predictions == ("0.0", "1.0")
    assert predictions.positive_probabilities == (0.2, 0.8)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"format_version": 2}, "prediction envelope"),
        ({"feature_names": ["score", "score"]}, "duplicate feature"),
        ({"feature_names": ["missing"]}, "unknown feature"),
        ({"class_labels": ["1", "1.0"]}, "duplicate class"),
        ({"predictions": ["0", "other"]}, "outside the declared classes"),
        ({"predictions": ["0"]}, "row count"),
        ({"positive_probabilities": [0.2]}, "row count"),
        ({"positive_probabilities": [0.2, 1.1]}, "prediction envelope"),
        ({"positive_probabilities": [False, 0.8]}, "prediction envelope"),
        ({"format_version": True}, "prediction envelope"),
        (
            {"class_labels": ["0", "1", "2"], "positive_probabilities": [0.2, 0.8]},
            "exactly two class labels",
        ),
    ],
)
def test_read_model_predictions_rejects_invalid_envelopes(
    tmp_path: Path, update: dict[str, object], message: str
) -> None:
    response = tmp_path / "predictions.json"
    _write_payload(response, {**_valid_payload(), **update})

    with pytest.raises(ToolExecutionError, match=message):
        read_model_predictions(
            response,
            columns=("score", "target"),
            targets=("0", "1"),
        )


def test_read_model_predictions_requires_an_explicit_version(tmp_path: Path) -> None:
    response = tmp_path / "predictions.json"
    payload = _valid_payload()
    del payload["format_version"]
    _write_payload(response, payload)

    with pytest.raises(ToolExecutionError, match="prediction envelope"):
        read_model_predictions(response, columns=("score", "target"), targets=("0", "1"))


def test_read_model_predictions_rejects_target_outside_model_classes(tmp_path: Path) -> None:
    response = tmp_path / "predictions.json"
    _write_payload(response, _valid_payload())

    with pytest.raises(ToolExecutionError, match="target labels"):
        read_model_predictions(
            response,
            columns=("score", "target"),
            targets=("0", "2"),
        )


def test_read_model_predictions_checks_size_before_loading_json(tmp_path: Path) -> None:
    response = tmp_path / "predictions.json"
    with response.open("wb") as handle:
        handle.truncate(MAX_PREDICTION_BYTES + 1)

    with pytest.raises(ToolExecutionError, match="exceeds"):
        read_model_predictions(response, columns=("score",), targets=())


def test_read_model_predictions_rejects_non_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError, match="regular file"):
        read_model_predictions(tmp_path, columns=("score",), targets=())


def test_read_model_predictions_bounds_actual_bytes_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = tmp_path / "predictions.json"
    response.write_bytes(b"x" * 20)
    monkeypatch.setattr("thymira.tools.model_predictions.MAX_PREDICTION_BYTES", 10)
    real_stat = Path.stat

    def stale_stat(path: Path) -> object:
        metadata = real_stat(path)
        values = list(metadata)
        values[6] = 1
        return type(metadata)(values)

    monkeypatch.setattr("thymira.tools.model_predictions._prediction_stat", stale_stat)

    with pytest.raises(ToolExecutionError, match="exceeds"):
        read_model_predictions(response, columns=("score",), targets=())


def test_worker_loads_and_predicts_using_only_contained_paths(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    dataset_path = tmp_path / "audit.parquet"
    joblib.dump(_FixedModel(), model_path)
    pl.DataFrame({"score": [0.2, 0.8], "target": [0.0, 1.0]}).write_parquet(dataset_path)
    request = PredictionRequest(
        model_path="model.joblib", dataset_path="audit.parquet", target_column="target"
    )

    predictions = create_model_predictions(request, workspace=tmp_path)

    assert predictions.feature_names == ("score",)
    assert predictions.class_labels == ("0.0", "1.0")
    assert predictions.predictions == ("0.0", "1.0")
    assert predictions.positive_probabilities == (0.2, 0.8)


def test_worker_uses_frame_metadata_when_estimator_has_none(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    dataset_path = tmp_path / "audit.parquet"
    joblib.dump(_FrameMetadataModel(), model_path)
    pl.DataFrame({"score": ["no", "yes"], "target": ["no", "yes"]}).write_parquet(dataset_path)

    predictions = create_model_predictions(
        PredictionRequest(
            model_path="model.joblib", dataset_path="audit.parquet", target_column="target"
        ),
        workspace=tmp_path,
    )

    assert predictions.feature_names == ("score",)
    assert predictions.class_labels == ("no", "yes")
    assert predictions.positive_probabilities is None


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (_MissingFeatureModel(), "missing model features"),
        (_UnknownPredictionModel(), "not one of the classes"),
        (_MalformedProbabilityModel(), "two columns"),
        (_WideProbabilityModel(), "two columns"),
    ],
)
def test_worker_rejects_incoherent_model_outputs(
    tmp_path: Path, model: object, message: str
) -> None:
    joblib.dump(model, tmp_path / "model.joblib")
    pl.DataFrame({"score": [0.2], "target": [0]}).write_parquet(tmp_path / "audit.parquet")
    request = PredictionRequest(
        model_path="model.joblib", dataset_path="audit.parquet", target_column="target"
    )

    with pytest.raises(ValueError, match=message):
        create_model_predictions(request, workspace=tmp_path)


def test_worker_cli_writes_strict_lf_terminated_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    joblib.dump(_FixedModel(), tmp_path / "model.joblib")
    pl.DataFrame({"score": [0.2], "target": [0]}).write_parquet(tmp_path / "audit.parquet")
    request = PredictionRequest(
        model_path="model.joblib", dataset_path="audit.parquet", target_column="target"
    )
    (tmp_path / "request.json").write_text(
        request.model_dump_json(), encoding="utf-8", newline="\n"
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(["request.json", "response.json"])

    assert exit_code == 0
    raw = (tmp_path / "response.json").read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\r\n")
    assert ModelPredictions.model_validate_json(raw).predictions == ("0",)


def test_worker_cli_refuses_bad_arity_and_malformed_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")

    assert main([]) == 2
    assert main(["bad.json", "response.json"]) == 1
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("model_path", ["../model.joblib", "/model.joblib"])
def test_worker_rejects_paths_outside_the_workspace(tmp_path: Path, model_path: str) -> None:
    request = PredictionRequest(
        model_path=model_path, dataset_path="audit.parquet", target_column="target"
    )

    with pytest.raises(ValueError, match="relative paths contained in the workspace"):
        create_model_predictions(request, workspace=tmp_path)
