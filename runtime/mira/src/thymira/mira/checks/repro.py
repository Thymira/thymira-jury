"""Recomputation of the modelling-protocol controls A14, A21, A22 and A23.

These are the FINAL controls from ``CTRL-CV-BASELINE-REPRO``. Each recomputes a methodology
invariant from the modelling evidence a run leaves behind -- a self-describing modelling-protocol
artifact in the store and the ``model.trained`` events -- rather than trusting any success flag THY
recorded (Appendix B of ``docs/adr/0002-legacy-disposition.md``; the inherited meaning of A14 at
``docs/legacy/trazabilidad.md:114``). Each check is ``NOT_APPLICABLE`` when its evidence is absent,
so a run that produced no modelling protocol is never faulted for lacking one.

- ``check_test_once`` (A14): the test partition is read once, for the final evaluation, using the
  out-of-fold-selected threshold. Any access at a non-final stage is early test access.
- ``check_fold_local_cv`` (A21): cross-validation preprocessing is fitted inside its own fold and
  the same folds are shared by every candidate -- no cross-fold leakage.
- ``check_baseline`` (A22): a trivial baseline was scored on the identical folds the candidates use.
- ``check_reproducibility_metadata`` (A23): every ``model.trained`` event carries its
  reproducibility metadata (seed, library versions, single-thread execution, estimator arguments).

Evidence shape (a self-describing modelling-protocol artifact, any active JSON carrying a
``cross_validation`` dict and/or a ``test_protocol`` dict)::

    {
        "cross_validation": {
            "folds_id": "cv5-seed-42",
            "folds": [{"fold": 0, "preprocessing_fit": "in_fold"}, ...],
        },
        "candidates": [
            {"name": "logreg", "folds_id": "cv5-seed-42", "baseline": false},
            {"name": "dummy_majority", "folds_id": "cv5-seed-42", "baseline": true},
        ],
        "test_protocol": {
            "threshold_source": "out_of_fold",
            "accesses": [{"stage": "final_evaluation"}],
        },
    }
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.mira.checks.models import ControlStatus
from thymira.schemas import EventType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event
    from thymira.state import ArtifactStore

_FINAL_STAGES = frozenset(
    {"final_evaluation", "final", "holdout", "holdout_evaluation", "test_evaluation"}
)
"""Stage labels that count as the single, final read of the test partition."""

_OOF_SOURCES = frozenset({"oof", "out_of_fold", "out-of-fold", "cross_validation", "cv"})
"""Threshold sources that count as out-of-fold-selected."""

_IN_FOLD = frozenset({"in_fold", "in-fold", "fold", "fold_local", "train_fold", "per_fold"})
"""Preprocessing fit scopes that are fold-local (no cross-fold leakage)."""

_BASELINE_NAMES = ("baseline", "dummy", "majority", "constant", "most_frequent")
"""Substrings marking a candidate as a trivial baseline when it carries no explicit flag."""

_REPRO_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("seed", ("seed", "random_state", "random_seed")),
    ("library versions", ("library_versions", "versions", "dependencies")),
    ("single-thread execution", ("single_thread", "n_jobs", "threads")),
    (
        "explicit estimator arguments",
        ("estimator_args", "params", "hyperparameters", "estimator_params"),
    ),
)
"""Each reproducibility fact A23 requires, with the payload keys that may carry it."""

_MAX_LISTED = 5
"""How many offending items to quote in a finding detail before truncating."""


# --------------------------------------------------------------------- A14: test read once, at end


def check_test_once(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute the test partition is read once, at final evaluation, with the OOF threshold."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    protocols = _artifacts_with(store, "test_protocol")
    if not protocols:
        return ControlStatus.NOT_APPLICABLE, "no test-protocol evidence"
    problems: list[str] = []
    for name, payload in protocols:
        problems.extend(_test_protocol_problems(name, payload["test_protocol"]))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return (
        ControlStatus.PASSED,
        f"{len(protocols)} test protocol(s): read once, at final evaluation",
    )


def _test_protocol_problems(name: str, protocol: dict[str, Any]) -> list[str]:
    """Report early test access, repeated final reads, or a non-out-of-fold threshold."""
    prefix = f"test protocol {name!r}"
    problems: list[str] = []
    accesses = protocol.get("accesses")
    stages = (
        [access.get("stage") for access in accesses if isinstance(access, dict)]
        if isinstance(accesses, list)
        else []
    )
    early = [stage for stage in stages if _stage_key(stage) not in _FINAL_STAGES]
    if early:
        problems.append(
            f"{prefix}: test partition accessed at {early[:_MAX_LISTED]} before final evaluation"
        )
    final_reads = [stage for stage in stages if _stage_key(stage) in _FINAL_STAGES]
    if len(final_reads) > 1:
        problems.append(
            f"{prefix}: test partition read {len(final_reads)} times; it must be read once"
        )
    source = protocol.get("threshold_source")
    if isinstance(source, str) and _stage_key(source) not in _OOF_SOURCES:
        problems.append(
            f"{prefix}: final evaluation used threshold source {source!r}, not out-of-fold"
        )
    return problems


def _stage_key(stage: Any) -> str:
    """Normalise a stage or source label for comparison."""
    return stage.strip().casefold() if isinstance(stage, str) else ""


# ----------------------------------------------------------------------- A21: fold-local CV


def check_fold_local_cv(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute that CV preprocessing is fold-local and the folds are shared by every candidate."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    evidence = _artifacts_with(store, "cross_validation")
    if not evidence:
        return ControlStatus.NOT_APPLICABLE, "no cross-validation evidence"
    problems: list[str] = []
    for name, payload in evidence:
        problems.extend(_fold_local_problems(name, payload))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return ControlStatus.PASSED, f"{len(evidence)} CV protocol(s): fold-local, shared folds"


def _fold_local_problems(name: str, payload: dict[str, Any]) -> list[str]:
    """Report fold preprocessing fitted outside its fold, or a candidate using different folds."""
    prefix = f"CV protocol {name!r}"
    cv = payload["cross_validation"]
    problems: list[str] = []
    folds = cv.get("folds") if isinstance(cv, dict) else None
    for index, fold in enumerate(folds if isinstance(folds, list) else []):
        if not isinstance(fold, dict):
            continue
        scope = fold.get("preprocessing_fit", fold.get("preprocessing"))
        label = fold.get("fold", index)
        if isinstance(scope, str) and _stage_key(scope) not in _IN_FOLD:
            problems.append(
                f"{prefix}: fold {label} preprocessing fitted outside its fold ({scope!r})"
            )
    folds_id = cv.get("folds_id") if isinstance(cv, dict) else None
    candidates = payload.get("candidates")
    if isinstance(folds_id, str) and isinstance(candidates, list):
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            cfid = candidate.get("folds_id")
            if isinstance(cfid, str) and cfid != folds_id:
                who = _candidate_label(candidate, index)
                problems.append(
                    f"{prefix}: candidate {who!r} used folds {cfid!r}, not the shared {folds_id!r}"
                )
    return problems


# ------------------------------------------------------------------- A22: baseline on the folds


def check_baseline(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute that a trivial baseline was scored on the identical folds the candidates use."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    evidence = [
        (name, payload)
        for name, payload in _artifacts_with(store, "cross_validation")
        if isinstance(payload.get("candidates"), list)
    ]
    if not evidence:
        return ControlStatus.NOT_APPLICABLE, "no cross-validated candidate evidence"
    problems: list[str] = []
    for name, payload in evidence:
        problems.extend(_baseline_problems(name, payload))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return ControlStatus.PASSED, f"{len(evidence)} protocol(s): a baseline shares the folds"


def _baseline_problems(name: str, payload: dict[str, Any]) -> list[str]:
    """Report a missing baseline or a baseline scored on folds other than the candidates'."""
    prefix = f"CV protocol {name!r}"
    cv = payload["cross_validation"]
    shared = cv.get("folds_id") if isinstance(cv, dict) else None
    candidates = [c for c in payload["candidates"] if isinstance(c, dict)]
    baselines = [c for c in candidates if _is_baseline(c)]
    if not baselines:
        return [f"{prefix}: no trivial baseline was scored on the folds"]
    if isinstance(shared, str):
        on_shared = [b for b in baselines if b.get("folds_id") in (None, shared)]
        if not on_shared:
            used = sorted({str(b.get("folds_id")) for b in baselines})
            return [f"{prefix}: the baseline used folds {used}, not the candidates' {shared!r}"]
    return []


def _is_baseline(candidate: dict[str, Any]) -> bool:
    """Whether a candidate is a trivial baseline, by explicit flag or by name."""
    if candidate.get("baseline") is True:
        return True
    name = candidate.get("name", candidate.get("model"))
    lowered = name.casefold() if isinstance(name, str) else ""
    return any(marker in lowered for marker in _BASELINE_NAMES)


def _candidate_label(candidate: dict[str, Any], index: int) -> str:
    """Resolve a candidate's display label, falling back to its position."""
    for key in ("name", "model", "id", "model_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return f"candidate[{index}]"


# ------------------------------------------------------------- A23: reproducibility metadata


def check_reproducibility_metadata(events: Sequence[Event]) -> tuple[ControlStatus, str]:
    """Recompute that every ``model.trained`` event carries its reproducibility metadata."""
    trained = [event for event in events if event.type is EventType.MODEL_TRAINED]
    if not trained:
        return ControlStatus.NOT_APPLICABLE, "no model.trained events"
    problems: list[str] = []
    for event in trained:
        missing = _missing_repro_fields(event.payload)
        if missing:
            problems.append(
                f"model.trained at seq {event.seq} missing or invalid: {', '.join(missing)}"
            )
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return (
        ControlStatus.PASSED,
        f"{len(trained)} model.trained event(s) carry reproducibility metadata",
    )


def _missing_repro_fields(payload: dict[str, Any]) -> list[str]:
    """List the reproducibility facts one ``model.trained`` payload fails to record."""
    return [label for label, keys in _REPRO_FIELDS if not _has_repro_fact(payload, label, keys)]


def _has_repro_fact(payload: dict[str, Any], label: str, keys: tuple[str, ...]) -> bool:
    """Require well-formed facts under every supplied alias, rather than mere presence."""
    values = [payload[key] for key in keys if key in payload]
    if not values:
        return False
    if label == "seed":
        return all(type(value) is int and value >= 0 for value in values) and len(set(values)) == 1
    if label == "single-thread execution":
        return _single_thread(payload, keys)
    mappings = all(
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(key, str) and bool(key.strip()) for key in value)
        for value in values
    )
    if not mappings:
        return False
    if label == "library versions":
        return all(
            isinstance(version, str) and bool(version.strip())
            for value in values
            for version in value.values()
        )
    return True


def _single_thread(payload: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """Reject parallel counts, contradictory aliases and invalid observed thread pools."""
    for key in keys:
        if key in payload:
            value = payload[key]
            valid = value is True if key == "single_thread" else type(value) is int and value == 1
            if not valid:
                return False
    pools = payload.get("observed_threadpools")
    if "observed_threadpools" not in payload:
        return True
    return (
        isinstance(pools, list)
        and bool(pools)
        and all(
            isinstance(pool, dict)
            and type(pool.get("num_threads")) is int
            and pool["num_threads"] == 1
            for pool in pools
        )
    )


# --------------------------------------------------------------------------- shared evidence access


def _artifacts_with(store: ArtifactStore, key: str) -> list[tuple[str, dict[str, Any]]]:
    """Every active JSON artifact whose top-level object carries ``key``."""
    found: list[tuple[str, dict[str, Any]]] = []
    for artifact in store.list_active():
        if not artifact.name.endswith(".json"):
            continue
        payload = _load_json(store, artifact.name)
        if isinstance(payload, dict) and key in payload:
            found.append((artifact.name, payload))
    return found


def _load_json(store: ArtifactStore, name: str) -> Any:
    """Load an artifact as JSON, returning ``None`` when it is missing or not JSON."""
    try:
        value = store.load_json(name)
    except (OSError, ValueError):
        return None
    else:
        return value


__all__ = [
    "check_baseline",
    "check_fold_local_cv",
    "check_reproducibility_metadata",
    "check_test_once",
]
