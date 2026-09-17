"""Modelling-protocol controls A14 (test-once), A21 (fold-local CV), A22 (baseline) and A23 (repro).

These are the FINAL controls from ``CTRL-CV-BASELINE-REPRO``. Each recomputes a methodology
invariant from the modelling evidence -- a self-describing modelling-protocol artifact in the store
(A14/A21/A22) or the ``model.trained`` events (A23) -- and is NOT_APPLICABLE when its evidence is
absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import Actor, ArtifactKind, EventType, Severity, new_id

from thymira.state import LocalArtifactStore  # isort: skip

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.mira.checks import AuditReport


def _closed_log() -> InMemoryEventLog:
    """A started-and-closed run so the hash chain verifies and controls are evaluated."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    log.append(EventType.RUN_COMPLETED, system, {})
    return log


def _trained_log(payloads: list[dict[str, Any]]) -> InMemoryEventLog:
    """A closed run that records one ``model.trained`` event per supplied payload."""
    log = InMemoryEventLog(new_id("run"))
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    for payload in payloads:
        log.append(EventType.MODEL_TRAINED, system, payload, subject_id=new_id("artifact"))
    log.append(EventType.RUN_COMPLETED, system, {})
    return log


def _store(tmp_path: Path, run_id: str) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts", run_id)


def _save(store: LocalArtifactStore, name: str, payload: dict[str, Any]) -> None:
    store.save_json(name, payload, produced_by=new_id("tool"), kind=ArtifactKind.OTHER)


def _audit(log: InMemoryEventLog, store: LocalArtifactStore | None = None) -> AuditReport:
    return audit_run(AuditContext(log.run_id, log.events(), store))


def _control(report: AuditReport, control_id: str):
    return next(c for c in report.controls if c.control_id == control_id)


def _findings(report: AuditReport, control_id: str):
    return [f for f in report.findings if f.control_id == control_id]


# --------------------------------------------------------------- A14: test partition read once


def test_a14_fails_when_the_test_set_is_touched_before_final_evaluation(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {
            "test_protocol": {
                "threshold_source": "out_of_fold",
                "accesses": [{"stage": "cross_validation"}, {"stage": "final_evaluation"}],
            }
        },
    )

    report = _audit(log, store)

    a14 = _control(report, "A14")
    assert a14.status is ControlStatus.FAILED
    assert "before final evaluation" in a14.detail
    assert "A14" in {f.control_id for f in report.findings}


def test_a14_passes_a_single_final_read_with_the_oof_threshold(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {"test_protocol": {"threshold_source": "out_of_fold", "accesses": [{"stage": "final"}]}},
    )

    report = _audit(log, store)

    assert _control(report, "A14").status is ControlStatus.PASSED
    assert _findings(report, "A14") == []


# --------------------------------------------------------------- A21: fold-local cross-validation


def test_a21_fails_when_a_fold_preprocessing_is_fitted_outside_its_fold(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {
            "cross_validation": {
                "folds_id": "cv5-seed-42",
                "folds": [
                    {"fold": 0, "preprocessing_fit": "in_fold"},
                    {"fold": 1, "preprocessing_fit": "full_data"},  # fitted outside the fold
                ],
            }
        },
    )

    report = _audit(log, store)

    a21 = _control(report, "A21")
    assert a21.status is ControlStatus.FAILED
    assert "outside its fold" in a21.detail
    assert "A21" in {f.control_id for f in report.findings}


def test_a21_passes_fold_local_preprocessing_shared_by_candidates(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {
            "cross_validation": {
                "folds_id": "cv5-seed-42",
                "folds": [
                    {"fold": 0, "preprocessing_fit": "in_fold"},
                    {"fold": 1, "preprocessing_fit": "train_fold"},
                ],
            },
            "candidates": [{"name": "logreg", "folds_id": "cv5-seed-42"}],
        },
    )

    report = _audit(log, store)

    assert _control(report, "A21").status is ControlStatus.PASSED
    assert _findings(report, "A21") == []


# --------------------------------------------------------------- A22: baseline on identical folds


def test_a22_fails_when_no_baseline_shares_the_folds(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {
            "cross_validation": {"folds_id": "cv5-seed-42"},
            "candidates": [
                {"name": "logreg", "folds_id": "cv5-seed-42", "baseline": False},
                {"name": "random_forest", "folds_id": "cv5-seed-42", "baseline": False},
            ],
        },
    )

    report = _audit(log, store)

    a22 = _control(report, "A22")
    assert a22.status is ControlStatus.FAILED
    assert "no trivial baseline" in a22.detail
    assert _findings(report, "A22")[0].severity is Severity.LOW  # never blocks on its own


def test_a22_passes_when_a_baseline_shares_the_folds(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)
    _save(
        store,
        "modelling_protocol.json",
        {
            "cross_validation": {"folds_id": "cv5-seed-42"},
            "candidates": [
                {"name": "logreg", "folds_id": "cv5-seed-42", "baseline": False},
                {"name": "dummy_majority", "folds_id": "cv5-seed-42", "baseline": True},
            ],
        },
    )

    report = _audit(log, store)

    assert _control(report, "A22").status is ControlStatus.PASSED
    assert _findings(report, "A22") == []


# --------------------------------------------------------------- A23: reproducibility metadata


def test_a23_fails_a_model_trained_event_missing_a_seed(tmp_path: Path) -> None:
    log = _trained_log(
        [
            {
                "library_versions": {"scikit-learn": "1.5.0"},
                "n_jobs": 1,
                "estimator_args": {"C": 1.0},
            }  # no seed
        ]
    )

    report = _audit(log)

    a23 = _control(report, "A23")
    assert a23.status is ControlStatus.FAILED
    assert "seed" in a23.detail
    assert "A23" in {f.control_id for f in report.findings}


def test_a23_passes_when_every_model_trained_event_carries_metadata(tmp_path: Path) -> None:
    log = _trained_log(
        [
            {
                "seed": 42,
                "library_versions": {"scikit-learn": "1.5.0", "numpy": "2.0.0"},
                "n_jobs": 1,
                "estimator_args": {"C": 1.0, "penalty": "l2"},
            }
        ]
    )

    report = _audit(log)

    assert _control(report, "A23").status is ControlStatus.PASSED
    assert _findings(report, "A23") == []


# --------------------------------------------------------------- all NA without modelling evidence


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("seed", True, "seed"),
        ("seed", -1, "seed"),
        ("library_versions", "unknown", "library versions"),
        ("library_versions", {"numpy": ""}, "library versions"),
        ("library_versions", {"numpy": 2}, "library versions"),
        ("n_jobs", 8, "single-thread execution"),
        ("n_jobs", True, "single-thread execution"),
        ("n_jobs", "1", "single-thread execution"),
        ("estimator_args", "defaults", "explicit estimator arguments"),
        ("estimator_args", [], "explicit estimator arguments"),
    ],
)
def test_a23_rejects_invalid_reproducibility_values(field, value, label) -> None:
    payload = {
        "seed": 0,
        "library_versions": {"numpy": "2.0.0"},
        "n_jobs": 1,
        "estimator_args": {"random_state": 0},
    }
    payload[field] = value

    control = _control(_audit(_trained_log([payload])), "A23")

    assert control.status is ControlStatus.FAILED
    assert label in control.detail


@pytest.mark.parametrize(
    "threads",
    [
        {"deterministic": True},
        {"single_thread": True, "n_jobs": 8},
        {"single_thread": 1},
        {"threads": 1, "observed_threadpools": []},
        {"threads": 1, "observed_threadpools": [{"num_threads": 8}]},
        {"threads": 1, "observed_threadpools": [{"num_threads": True}]},
        {"threads": 1, "observed_threadpools": "unknown"},
        {"threads": 1, "observed_threadpools": [None]},
    ],
)
def test_a23_requires_an_uncontradicted_single_thread_declaration(threads) -> None:
    payload = {
        "seed": 0,
        "library_versions": {"numpy": "2.0.0"},
        "estimator_args": {"random_state": 0},
        **threads,
    }

    control = _control(_audit(_trained_log([payload])), "A23")

    assert control.status is ControlStatus.FAILED
    assert "single-thread execution" in control.detail


@pytest.mark.parametrize(
    "thread_evidence",
    [
        {"single_thread": True},
        {"threads": 1, "observed_threadpools": [{"num_threads": 1}]},
    ],
)
def test_a23_accepts_valid_aliases_with_zero_seed_and_null_estimator_defaults(
    thread_evidence,
) -> None:
    payload = {
        "random_seed": 0,
        "versions": {"numpy": "2.0.0"},
        "hyperparameters": {"class_weight": None, "random_state": 0},
        **thread_evidence,
    }

    control = _control(_audit(_trained_log([payload])), "A23")

    assert control.status is ControlStatus.PASSED


def test_a23_rejects_one_incomplete_model_even_when_another_has_metadata() -> None:
    valid = {
        "seed": 7,
        "library_versions": {"numpy": "2.0.0"},
        "n_jobs": 1,
        "estimator_args": {"random_state": 7},
    }

    control = _control(_audit(_trained_log([valid, {}])), "A23")

    assert control.status is ControlStatus.FAILED
    assert "missing or invalid" in control.detail


def test_a23_rejects_conflicting_seed_aliases() -> None:
    payload = {
        "seed": 7,
        "random_seed": 99,
        "library_versions": {"numpy": "2.0.0"},
        "n_jobs": 1,
        "estimator_args": {"random_state": 7},
    }

    control = _control(_audit(_trained_log([payload])), "A23")

    assert control.status is ControlStatus.FAILED
    assert "seed" in control.detail


def test_all_repro_controls_are_not_applicable_without_modelling_evidence(tmp_path: Path) -> None:
    log = _closed_log()
    store = _store(tmp_path, log.run_id)

    report = _audit(log, store)

    for control_id in ("A14", "A21", "A22", "A23"):
        assert _control(report, control_id).status is ControlStatus.NOT_APPLICABLE
        assert _findings(report, control_id) == []
