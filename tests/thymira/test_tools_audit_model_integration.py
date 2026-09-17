"""Integration regressions for model-risk auditing through the real sandbox boundary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import joblib
import numpy as np
import polars as pl
import pytest
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score

from tests.thymira.fixtures_tools import tool_invocation
from thymira.schemas import ArtifactKind, SandboxMode
from thymira.tools import ToolInvocation, register_dataset
from thymira.tools.builtins.audit_model import AuditModel

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _register_frame(invocation: ToolInvocation, frame: pl.DataFrame, name: str) -> None:
    invocation.workspace.mkdir(parents=True, exist_ok=True)
    source = invocation.workspace / f"{name}.csv"
    frame.write_csv(source)
    register_dataset(invocation.artifact_store, source, name, produced_by=invocation.agent_id)


def _store_model(invocation: ToolInvocation, model: object, name: str) -> str:
    invocation.workspace.mkdir(parents=True, exist_ok=True)
    path = invocation.workspace / "model.joblib"
    joblib.dump(model, path)
    invocation.artifact_store.save_bytes(
        name,
        path.read_bytes(),
        produced_by=invocation.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    return name


def test_audit_orders_features_by_the_model_when_the_frame_columns_are_reordered(
    tmp_path: Path,
) -> None:
    # The label depends asymmetrically on x0 and x1, so feeding the columns in the wrong order
    # flips most predictions. The frame stores them in a different order than the model was
    # trained on; the model records the training order in ``feature_names_in_``.
    rng = np.random.default_rng(0)
    size = 300
    x0 = rng.normal(size=size)
    x1 = rng.normal(size=size)
    sex = rng.integers(0, 2, size=size)
    target = (x0 - x1 > 0).astype(int)
    x_train = np.column_stack([x0, x1, sex])
    model = LogisticRegression(solver="liblinear", random_state=0).fit(x_train, target)
    model.feature_names_in_ = np.array(["x0", "x1", "sex"])
    true_accuracy = float(accuracy_score(target, model.predict(x_train)))

    invocation = tool_invocation(tmp_path)
    # Columns deliberately in a different order than the model's training order.
    frame = pl.DataFrame({"sex": sex, "x1": x1, "x0": x0, "target": target})
    _register_frame(invocation, frame, "reordered")
    artifact = _store_model(invocation, model, "models/reordered.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "reordered",
            "target_column": "target",
            "protected_column": "sex",
        },
    )
    payload = json.loads(result.stdout)

    # A separated problem: true accuracy is high; feeding features in frame order scores ~chance.
    assert true_accuracy > 0.9
    assert payload["accuracy"] == pytest.approx(true_accuracy)


def test_audit_labels_predictions_from_model_classes_not_the_sorted_string_frame(
    tmp_path: Path,
) -> None:
    # Integer class labels 1, 2 and 10: the model's ``classes_`` sort numerically ([1, 2, 10]),
    # while ``sorted(set(str(target)))`` sorts lexicographically (["1", "10", "2"]). The old tool
    # used the string order to relabel integer predictions, silently reporting the wrong class.
    feature = list(range(7))
    protected = [0, 0, 0, 1, 1, 1, 0]
    target = [1, 1, 1, 1, 1, 2, 10]
    x_train = np.column_stack([feature, protected])
    model = DummyClassifier(strategy="most_frequent").fit(x_train, target)
    # DummyClassifier predicts the constant class 1 for every row.
    predicted_labels = [str(value) for value in model.predict(x_train)]
    true_accuracy = float(accuracy_score([str(value) for value in target], predicted_labels))

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"feature": feature, "protected": protected, "target": target})
    _register_frame(invocation, frame, "multiclass")
    artifact = _store_model(invocation, model, "models/multiclass.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "multiclass",
            "target_column": "target",
            "protected_column": "protected",
        },
    )
    payload = json.loads(result.stdout)

    # The constant prediction is class 1; five of seven rows are class 1, so accuracy is 5/7.
    assert true_accuracy == pytest.approx(5 / 7)
    assert payload["accuracy"] == pytest.approx(true_accuracy)


def test_audit_does_not_raise_when_the_audited_cohort_has_a_single_class(
    tmp_path: Path,
) -> None:
    # The model predicts both classes, but the audited cohort's target column is constant. The old
    # tool built ``labels`` from the cohort (one element) and indexed it by the prediction code,
    # raising ``IndexError`` on the first row the model predicted as the other class.
    model = LogisticRegression(solver="liblinear", random_state=0).fit(
        [[0, 0], [1, 0], [0, 1], [1, 1]], [0, 1, 0, 1]
    )

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"feature": [1, 1, 0], "protected": [0, 1, 0], "target": [0, 0, 0]})
    _register_frame(invocation, frame, "cohort")
    artifact = _store_model(invocation, model, "models/cohort.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "cohort",
            "target_column": "target",
            "protected_column": "protected",
        },
    )
    payload = json.loads(result.stdout)

    # The model predicts class 1 for the two feature==1 rows, so accuracy against the all-zero
    # cohort is 1/3. A single-class cohort has no ROC AUC, so it is not reported.
    assert payload["accuracy"] == pytest.approx(1 / 3)
    assert "auc" not in payload


def test_audit_raises_a_named_error_when_the_frame_is_missing_a_model_feature(
    tmp_path: Path,
) -> None:
    model = LogisticRegression(solver="liblinear", random_state=0).fit(
        [[0, 0], [1, 1], [0, 1], [1, 0]], [0, 1, 1, 0]
    )
    model.feature_names_in_ = np.array(["x0", "x1"])

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"x0": [0, 1, 0], "wrong": [1, 0, 1], "target": [0, 1, 0]})
    _register_frame(invocation, frame, "mismatch")
    artifact = _store_model(invocation, model, "models/mismatch.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "mismatch",
            "target_column": "target",
            "protected_column": "x0",
        },
    )

    assert result.success is False
    assert "missing model features: x1" in (result.error or "")


def test_audit_scores_auc_against_the_models_positive_class_for_string_labels(
    tmp_path: Path,
) -> None:
    # String class labels: the model's ``predict`` returns "no"/"yes" directly, and its positive
    # ``predict_proba`` column is ``classes_[1]`` == "yes". The old tool did ``int(value)`` on the
    # string prediction and crashed before ever reaching the AUC.
    rng = np.random.default_rng(1)
    size = 120
    score = rng.normal(size=size)
    protected = rng.integers(0, 2, size=size)
    target = np.where(score + rng.normal(scale=0.3, size=size) > 0, "yes", "no")
    x_train = np.column_stack([score, protected])
    model = LogisticRegression(solver="liblinear", random_state=0).fit(x_train, target)
    positive_probability = model.predict_proba(x_train)[:, 1]
    expected_auc = float(
        roc_auc_score([1 if value == "yes" else 0 for value in target], positive_probability)
    )

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"score": score, "protected": protected, "target": target})
    _register_frame(invocation, frame, "labelled")
    artifact = _store_model(invocation, model, "models/labelled.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "labelled",
            "target_column": "target",
            "protected_column": "protected",
        },
    )
    payload = json.loads(result.stdout)

    assert model.classes_.tolist() == ["no", "yes"]
    assert payload["auc"] == pytest.approx(expected_auc)


def test_audit_refuses_a_model_whose_class_space_names_none_of_the_audited_labels(
    tmp_path: Path,
) -> None:
    """A model fitted on a discarded label encoding is refused, not scored against the codes.

    `run_experiment` used to fit on the output of a ``LabelEncoder`` and then drop the encoder, so
    the saved model's ``classes_`` were positional codes that no longer named anything. Decoding
    predictions against them compares ``"1"`` with ``"yes"``, which never matches: the tool would
    have reported an accuracy of zero and a diagonal-free confusion matrix as though those were
    facts about the model. It fits on the labels themselves now, but caller-supplied training code
    can still encode, so the disagreement is named rather than scored.
    """
    encoded = LogisticRegression(solver="liblinear", random_state=0).fit(
        [[0, 0], [1, 1], [0, 1], [1, 0]], [0, 1, 1, 0]
    )

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"x0": [0, 1, 0, 1], "x1": [0, 1, 1, 0], "target": ["no", "yes"] * 2})
    _register_frame(invocation, frame, "encoded")
    artifact = _store_model(invocation, encoded, "models/encoded.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "encoded",
            "target_column": "target",
            "protected_column": "x0",
        },
    )

    assert result.success is False
    assert "cannot predict labels the dataset carries" in (result.error or "")


def test_audit_matches_a_label_the_dataset_and_the_model_spell_differently(
    tmp_path: Path,
) -> None:
    """A numeric label written ``1`` by one reader and ``1.0`` by another is one label.

    The training script reads the dataset with ``csv.DictReader`` and the audit reads it through
    polars, so the same numeric target reaches the two sides in two spellings. Comparing the raw
    strings called a model trained on this very data a mismatch and refused to audit it -- a
    stricter check that fires on nothing real is just a way of not doing the job.
    """
    model = LogisticRegression(solver="liblinear", random_state=0).fit(
        [[0, 0], [1, 1], [0, 1], [1, 0]], ["0.0", "1.0", "1.0", "0.0"]
    )

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"x0": [0, 1, 0, 1], "x1": [0, 1, 1, 0], "target": [0, 1, 1, 0]})
    _register_frame(invocation, frame, "spelling")
    artifact = _store_model(invocation, model, "models/spelling.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "spelling",
            "target_column": "target",
            "protected_column": "x0",
        },
    )

    payload = json.loads(result.stdout)
    assert result.success is True
    # Scored, not refused -- and labelled the way the dataset labels itself.
    assert payload["accuracy"] == 1.0


def test_audit_refuses_a_cohort_carrying_a_label_the_model_cannot_predict(
    tmp_path: Path,
) -> None:
    """A partial overlap is still a mismatch: the extra label depresses every metric silently.

    The first version of this guard refused only a *disjoint* class space, so an encoded model
    whose codes happened to coincide with some of the dataset's labels slipped through and was
    scored against the codes -- reproducing exactly the silent mis-score the guard exists to stop.
    """
    model = LogisticRegression(solver="liblinear", random_state=0).fit(
        [[0, 0], [1, 1], [0, 1], [1, 0]], [0, 1, 1, 0]
    )

    invocation = tool_invocation(tmp_path)
    frame = pl.DataFrame({"x0": [0, 1, 0, 1], "x1": [0, 1, 1, 0], "target": [0, 1, 2, 1]})
    _register_frame(invocation, frame, "extra")
    artifact = _store_model(invocation, model, "models/extra.joblib")

    result = AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        invocation,
        {
            "model_artifact": artifact,
            "dataset": "extra",
            "target_column": "target",
            "protected_column": "x0",
        },
    )

    assert result.success is False
    assert "cannot predict labels the dataset carries: 2" in (result.error or "")
