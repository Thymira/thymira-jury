"""Modelling-artifact controls A11 (split integrity), A20 (leakage) and A12 (lineage coherence).

These are the FINAL controls from `CTRL-LEAKAGE-SPLIT` and `CTRL-LINEAGE`. Each recomputes a
methodology invariant from the artifact store (the dataset, the split declaration and the artifact
lineage), and is NOT_APPLICABLE when its evidence is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import Actor, ArtifactKind, EventType, new_id

from thymira.state import LocalArtifactStore  # isort: skip

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.mira.checks import AuditReport


def _closed_log(run_id: str) -> InMemoryEventLog:
    """A started-and-closed run so the hash chain verifies and controls are evaluated."""
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    log.append(EventType.RUN_COMPLETED, system, {})
    return log


def _store(tmp_path: Path, run_id: str) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts", run_id)


def _audit(run_id: str, store: LocalArtifactStore) -> AuditReport:
    return audit_run(AuditContext(run_id, _closed_log(run_id).events(), store))


def _control(report: AuditReport, control_id: str):
    return next(c for c in report.controls if c.control_id == control_id)


def _findings(report: AuditReport, control_id: str):
    return [f for f in report.findings if f.control_id == control_id]


# --------------------------------------------------------------- A11: split integrity


def _save_split(store: LocalArtifactStore, name: str, payload: dict) -> None:
    store.save_json(name, payload, produced_by=new_id("tool"), kind=ArtifactKind.OTHER)


def test_a11_fails_overlapping_train_test_indices(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 1, 2],
            "test_indices": [2, 3],  # index 2 is in both partitions
            "n_rows": 4,
            "labels": ["a", "b", "a", "b"],
        },
    )

    report = _audit(run_id, store)

    a11 = _control(report, "A11")
    assert a11.status is ControlStatus.FAILED
    assert "shared between train and test" in a11.detail
    assert "A11" in {f.control_id for f in report.findings}


def test_a11_passes_a_clean_stratified_split(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 1],
            "test_indices": [2, 3],
            "n_rows": 4,
            "labels": ["a", "b", "a", "b"],
        },
    )

    report = _audit(run_id, store)

    assert _control(report, "A11").status is ControlStatus.PASSED
    assert _findings(report, "A11") == []


def test_a11_is_not_applicable_without_split_evidence(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)

    report = _audit(run_id, store)

    assert _control(report, "A11").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A11") == []


# --------------------------------------------------------------- A20: leakage indicators


def _save_dataset(store: LocalArtifactStore, name: str, columns: dict, target: str) -> None:
    store.save_json(
        name,
        {"target_column": target, "columns": columns},
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )


def test_a20_fails_a_dataset_with_a_leaked_feature(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    target = [index % 2 for index in range(40)]
    leaked = [value + 0.001 * (index % 5) for index, value in enumerate(target)]
    clean = [index // 2 for index in range(40)]  # uncorrelated with the alternating target
    _save_dataset(
        store, "datasets/model.json", {"y": target, "leaked": leaked, "clean": clean}, "y"
    )

    report = _audit(run_id, store)

    a20 = _control(report, "A20")
    assert a20.status is ControlStatus.FAILED
    assert "leaked" in a20.detail
    assert "A20" in {f.control_id for f in report.findings}


def test_a20_passes_a_clean_dataset(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    target = [index % 2 for index in range(40)]
    clean = [index // 2 for index in range(40)]
    _save_dataset(store, "datasets/model.json", {"y": target, "clean": clean}, "y")

    report = _audit(run_id, store)

    assert _control(report, "A20").status is ControlStatus.PASSED
    assert _findings(report, "A20") == []


def test_a20_is_not_applicable_without_dataset_evidence(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)

    report = _audit(run_id, store)

    assert _control(report, "A20").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A20") == []


# --------------------------------------------------------------- A12: lineage coherence


def _model(store: LocalArtifactStore, name: str, split_id: str) -> str:
    """Save a candidate model fitted on `split_id` and return its artifact id."""
    artifact = store.save_bytes(
        name, name.encode("utf-8"), produced_by=new_id("tool"), kind=ArtifactKind.MODEL
    )
    store.set_lineage(name, input_artifact_ids=[split_id], execution_key=f"key-{name}")
    return artifact.id


def _select(store: LocalArtifactStore, model_id: str) -> None:
    store.save_json(
        "selection.json",
        {"selected_model_id": model_id},
        produced_by=new_id("tool"),
        kind=ArtifactKind.OTHER,
    )


def _evaluate(store: LocalArtifactStore, model_id: str) -> None:
    store.save_json(
        "metrics/eval.json",
        {"accuracy": 0.8},
        produced_by=new_id("tool"),
        kind=ArtifactKind.METRICS,
    )
    store.set_lineage("metrics/eval.json", input_artifact_ids=[model_id], execution_key="key-eval")


def _split(store: LocalArtifactStore, name: str, **extra: object) -> str:
    payload = {"train_indices": [0, 1], "test_indices": [2, 3], "n_rows": 4, **extra}
    artifact = store.save_json(name, payload, produced_by=new_id("tool"), kind=ArtifactKind.OTHER)
    return artifact.id


def test_a12_lineage_passes_on_a_coherent_chain(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    split_id = _split(store, "splits/main.split.json")
    winner = _model(store, "models/c1.joblib", split_id)
    _model(store, "models/c2.joblib", split_id)
    _select(store, winner)
    _evaluate(store, winner)

    report = _audit(run_id, store)

    assert _control(report, "A12").status is ControlStatus.PASSED
    assert _findings(report, "A12") == []


def test_a12_lineage_fails_when_the_evaluated_model_is_not_the_selected_candidate(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    split_id = _split(store, "splits/main.split.json")
    winner = _model(store, "models/c1.joblib", split_id)
    other = _model(store, "models/c2.joblib", split_id)
    _select(store, winner)
    _evaluate(store, other)  # the evaluation scored the model that was not selected

    report = _audit(run_id, store)

    a12 = _control(report, "A12")
    assert a12.status is ControlStatus.FAILED
    assert "not the selected model" in a12.detail


def test_a12_lineage_fails_when_a_candidate_references_another_split(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    declared_id = _split(store, "splits/main.split.json", declared=True)
    other_split_id = _split(store, "splits/other.split.json")
    winner = _model(store, "models/c1.joblib", declared_id)
    _model(store, "models/c2.joblib", other_split_id)  # fitted on a different split
    _select(store, winner)
    _evaluate(store, winner)

    report = _audit(run_id, store)

    a12 = _control(report, "A12")
    assert a12.status is ControlStatus.FAILED
    assert "not the declared split" in a12.detail


def test_a12_lineage_is_not_applicable_without_modelling_evidence(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)

    report = _audit(run_id, store)

    assert _control(report, "A12").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A12") == []


# --------------------------------------------------------------- A13: model selection re-derived


def _save_selection(store: LocalArtifactStore, name: str, payload: dict) -> None:
    store.save_json(name, payload, produced_by=new_id("tool"), kind=ArtifactKind.OTHER)


def test_a13_selection_fails_when_the_recorded_winner_is_not_metric_optimal(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "roc_auc", "direction": "max"},
            "candidates": [
                {"name": "logreg", "oof_metrics": {"roc_auc": 0.81}},
                {"name": "random_forest", "oof_metrics": {"roc_auc": 0.86}},
            ],
            "selected": "logreg",  # not the roc_auc-maximal candidate
        },
    )

    report = _audit(run_id, store)

    a13 = _control(report, "A13")
    assert a13.status is ControlStatus.FAILED
    assert "max-optimal" in a13.detail
    assert "random_forest" in a13.detail
    assert "A13" in {f.control_id for f in report.findings}


def test_a13_selection_fails_when_the_strategy_is_absent(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "candidates": [
                {"name": "logreg", "oof_metrics": {"roc_auc": 0.81}},
                {"name": "random_forest", "oof_metrics": {"roc_auc": 0.86}},
            ],
            "selected": "random_forest",  # optimal, but the choice cannot be reproduced
        },
    )

    report = _audit(run_id, store)

    a13 = _control(report, "A13")
    assert a13.status is ControlStatus.FAILED
    assert "absent or ambiguous" in a13.detail


def test_a13_selection_fails_when_the_strategy_direction_is_ambiguous(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "roc_auc", "direction": "whichever"},
            "candidates": [
                {"name": "logreg", "oof_metrics": {"roc_auc": 0.81}},
                {"name": "random_forest", "oof_metrics": {"roc_auc": 0.86}},
            ],
            "selected": "random_forest",
        },
    )

    report = _audit(run_id, store)

    assert _control(report, "A13").status is ControlStatus.FAILED


def test_a13_selection_passes_when_the_recomputation_agrees(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "roc_auc", "direction": "max"},
            "candidates": [
                {"name": "logreg", "oof_metrics": {"roc_auc": 0.81}},
                {"name": "random_forest", "oof_metrics": {"roc_auc": 0.86}},
            ],
            "selected": "random_forest",
        },
    )

    report = _audit(run_id, store)

    assert _control(report, "A13").status is ControlStatus.PASSED
    assert _findings(report, "A13") == []


def test_a13_selection_is_not_applicable_without_modelling_evidence(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = _store(tmp_path, run_id)

    report = _audit(run_id, store)

    assert _control(report, "A13").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A13") == []


# --------------------------------------------- A11: the remaining split-integrity invariants


def test_a11_fails_non_integer_and_out_of_range_indices(tmp_path: Path) -> None:
    """Indices MIRA cannot resolve to a row are reported, never silently dropped."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, "1", True],
            "test_indices": [2, 9],
            "n_rows": 4,
            "labels": ["a", "b", "a", "b"],
        },
    )

    a11 = _control(_audit(run_id, store), "A11")

    assert a11.status is ControlStatus.FAILED
    assert "non-integer indices" in a11.detail
    assert "out-of-range indices" in a11.detail


def test_a11_fails_an_empty_partition_and_duplicate_indices(tmp_path: Path) -> None:
    """A split with an empty side or a repeated row is not a split."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 1, 1],
            "test_indices": [],
            "n_rows": 2,
            "labels": ["a", "b"],
        },
    )

    a11 = _control(_audit(run_id, store), "A11")

    assert a11.status is ControlStatus.FAILED
    assert "a partition is empty" in a11.detail
    assert "duplicate indices" in a11.detail


def test_a11_fails_rows_covered_by_no_partition(tmp_path: Path) -> None:
    """Every declared row belongs to exactly one partition."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0],
            "test_indices": [1],
            "n_rows": 4,
            "labels": ["a", "b", "a", "b"],
        },
    )

    a11 = _control(_audit(run_id, store), "A11")

    assert a11.status is ControlStatus.FAILED
    assert "covered by no partition" in a11.detail


def test_a11_fails_a_class_allocated_to_only_one_partition(tmp_path: Path) -> None:
    """Both partitions must carry every class, or the test set cannot measure the model."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 1],
            "test_indices": [2, 3],
            "n_rows": 4,
            "labels": ["a", "a", "b", "b"],
        },
    )

    a11 = _control(_audit(run_id, store), "A11")

    assert a11.status is ControlStatus.FAILED
    assert "absent from train" in a11.detail
    assert "absent from test" in a11.detail


def test_a11_resolves_labels_and_row_count_from_the_referenced_dataset(tmp_path: Path) -> None:
    """A split that names its dataset and target is checked against that dataset labels."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_json(
        "data/train.json",
        {"columns": {"x": [1, 2, 3, 4], "y": ["a", "a", "b", "b"]}},
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 1],
            "test_indices": [2, 3],
            "dataset": "data/train.json",
            "target_column": "y",
        },
    )

    a11 = _control(_audit(run_id, store), "A11")

    # The row count falls back to the resolved label count, so allocation is still checked.
    assert a11.status is ControlStatus.FAILED
    assert "absent from train" in a11.detail


# ------------------------------------------------- A20: the remaining leakage indicators


def test_a20_fails_a_feature_that_is_an_exact_copy_of_the_target(tmp_path: Path) -> None:
    """A duplicated target column is leakage regardless of its correlation."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_json(
        "data/train.json",
        {
            "target_column": "y",
            "columns": {"y": ["a", "b", "a", "b"], "copy": ["a", "b", "a", "b"]},
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )

    a20 = _control(_audit(run_id, store), "A20")

    assert a20.status is ControlStatus.FAILED
    assert "exact copy of the target" in a20.detail


def test_a20_fails_an_identifier_like_column_correlated_with_the_target(tmp_path: Path) -> None:
    """An id column that tracks the target orders the rows by outcome."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_json(
        "data/train.json",
        {
            "target_column": "y",
            "columns": {
                "y": [0, 0, 0, 1, 1, 1],
                "customer_id": [1, 2, 3, 7, 8, 20],
            },
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )

    a20 = _control(_audit(run_id, store), "A20")

    assert a20.status is ControlStatus.FAILED
    assert "identifier-like feature" in a20.detail


def test_a20_fails_a_forbidden_transform_on_a_protected_column(tmp_path: Path) -> None:
    """Winsorising, imputing or encoding the target or a sensitive attribute is leakage."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_json(
        "data/train.json",
        {
            "target_column": "y",
            "sensitive": ["age"],
            "columns": {"y": ["a", "b", "a", "b"], "age": [30, 41, 35, 52]},
            "transforms": [
                {"column": "y", "op": "impute"},
                {"column": "age", "op": "winsorize"},
                {"column": "age", "op": "scale"},
                {"column": 7, "op": "impute"},
            ],
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )

    a20 = _control(_audit(run_id, store), "A20")

    assert a20.status is ControlStatus.FAILED
    assert "forbidden" in a20.detail
    assert "protected column" in a20.detail
    assert "scale" not in a20.detail


def test_a20_reads_a_csv_dataset_whose_target_the_split_declares(tmp_path: Path) -> None:
    """A CSV dataset has no target of its own; the split declaration names it."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_bytes(
        "data/train.csv",
        b"y,leak\n0,1\n0,2\n1,9\n1,10\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )
    _save_split(
        store,
        "splits/main.split.json",
        {
            "train_indices": [0, 2],
            "test_indices": [1, 3],
            "n_rows": 4,
            "labels": ["0", "0", "1", "1"],
            "dataset": "data/train.csv",
            "target_column": "y",
        },
    )

    a20 = _control(_audit(run_id, store), "A20")

    assert a20.status is ControlStatus.FAILED
    assert "correlated with the target" in a20.detail


def test_a20_passes_a_multiclass_target_it_cannot_encode(tmp_path: Path) -> None:
    """Without a numeric encoding there is no correlation to compute, and no indicator."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    store.save_json(
        "data/train.json",
        {
            "target_column": "y",
            "columns": {"y": ["a", "b", "c", "a"], "x": [1, 2, 3, 4]},
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.DATASET,
    )

    a20 = _control(_audit(run_id, store), "A20")

    assert a20.status is ControlStatus.PASSED


# ----------------------------------------------- A12: the remaining lineage invariants


def test_a12_lineage_fails_a_selected_model_that_is_not_a_candidate(tmp_path: Path) -> None:
    """A selection can only name a model the Run actually fitted."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    split_id = _split(store, "splits/main.split.json")
    winner = _model(store, "models/c1.joblib", split_id)
    _select(store, "artifact_" + "0" * 32)
    _evaluate(store, winner)

    a12 = _control(_audit(run_id, store), "A12")

    assert a12.status is ControlStatus.FAILED
    assert "is not among the fitted candidates" in a12.detail


def test_a12_lineage_fails_a_candidate_with_no_declared_split_in_its_lineage(
    tmp_path: Path,
) -> None:
    """A candidate whose lineage omits the declared split cannot be tied to it."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _split(store, "splits/main.split.json")
    store.save_bytes(
        "models/c1.joblib",
        b"c1",
        produced_by=new_id("tool"),
        kind=ArtifactKind.MODEL,
    )

    a12 = _control(_audit(run_id, store), "A12")

    assert a12.status is ControlStatus.FAILED
    assert "was not fitted on the declared split" in a12.detail


# ------------------------------ A13: the remaining selection-recomputation branches


def test_a13_selection_fails_when_a_candidate_records_no_metric(tmp_path: Path) -> None:
    """A candidate without the strategy metric makes the choice unreproducible."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "strategy": {"metric": "roc_auc", "goal": "higher_is_better"},
            "candidates": [
                {"name": "logreg", "metrics": {"roc_auc": 0.81}},
                {"name": "random_forest", "metrics": {"accuracy": 0.9}},
                {"model": "svm", "metrics": {"roc_auc": True}},
                "not-a-candidate",
                {"metrics": {"accuracy": 0.5}},
            ],
            "selected_model": "logreg",
        },
    )

    a13 = _control(_audit(run_id, store), "A13")

    assert a13.status is ControlStatus.FAILED
    # Ids come from the candidate itself, or from its position when it names none.
    assert "random_forest" in a13.detail
    assert "svm" in a13.detail
    assert "candidate[4]" in a13.detail


def test_a13_selection_fails_when_the_recorded_winner_is_not_a_candidate(tmp_path: Path) -> None:
    """A winner absent from the candidate list cannot be re-derived from the evidence."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "roc_auc", "direction": "max"},
            "candidates": [{"name": "logreg", "oof_metrics": {"roc_auc": 0.81}}],
            "selected": "random_forest",
        },
    )

    a13 = _control(_audit(run_id, store), "A13")

    assert a13.status is ControlStatus.FAILED
    assert "is not among the recorded candidates" in a13.detail


def test_a13_selection_reproduces_a_minimised_metric(tmp_path: Path) -> None:
    """For a loss metric the optimum is the smallest value, not the largest."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "log_loss", "mode": "lower_is_better"},
            "candidates": [
                {"id": "logreg", "cv_metrics": {"log_loss": 0.31}},
                {"id": "random_forest", "cv_metrics": {"log_loss": 0.44}},
            ],
            "selected_model_id": "logreg",
        },
    )

    assert _control(_audit(run_id, store), "A13").status is ControlStatus.PASSED


def test_a13_selection_fails_a_minimised_metric_whose_winner_is_not_optimal(
    tmp_path: Path,
) -> None:
    """Direction is applied, not assumed: the larger loss is not the winner."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"metric": "log_loss", "direction": "min"},
            "candidates": [
                {"id": "logreg", "out_of_fold_metrics": {"log_loss": 0.31}},
                {"id": "random_forest", "out_of_fold_metrics": {"log_loss": 0.44}},
            ],
            "selected": "random_forest",
        },
    )

    a13 = _control(_audit(run_id, store), "A13")

    assert a13.status is ControlStatus.FAILED
    assert "min-optimal" in a13.detail


def test_a13_selection_fails_when_the_strategy_names_no_metric(tmp_path: Path) -> None:
    """A strategy without a metric names nothing to recompute."""
    run_id = new_id("run")
    store = _store(tmp_path, run_id)
    _save_selection(
        store,
        "selection.json",
        {
            "selection_strategy": {"direction": "max"},
            "candidates": [{"name": "logreg", "oof_metrics": {"roc_auc": 0.81}}],
            "selected": "logreg",
        },
    )

    a13 = _control(_audit(run_id, store), "A13")

    assert a13.status is ControlStatus.FAILED
    assert "absent or ambiguous" in a13.detail
