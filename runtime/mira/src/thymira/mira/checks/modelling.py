"""Recomputation of the modelling-artifact controls A11, A20 and A12.

These controls read the modelling evidence a run leaves in the artifact store -- the dataset, the
train/test split declaration and the artifact lineage of the candidate models -- and recompute the
methodology invariants from it rather than trusting any success flag THY recorded (Appendix B of
``docs/adr/0002-legacy-disposition.md``; the inherited meanings at
``docs/legacy/trazabilidad.md:111-112``). Each check is ``NOT_APPLICABLE`` when its evidence is
absent, so a run that produced no modelling artifacts is never faulted for lacking them.

- ``check_split_integrity`` (A11): no train/test overlap, no duplicate or out-of-range indices,
  non-empty partitions, complete coverage and every class allocated to both partitions.
- ``check_leakage_indicators`` (A20): an exact target copy, a feature correlated with the target at
  ``|r| >= 0.95``, an identifier-like column correlated with the target, or a winsorize/impute/
  encode transform applied to the target or a sensitive attribute.
- ``check_lineage_coherence`` (A12): every candidate was fitted on the declared split, the selected
  model is one of the candidates, and the evaluated model is the selected one -- each link resolved
  through ``input_artifact_ids`` in the manifest, not through what THY reported.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.mira.checks.models import ControlStatus
from thymira.schemas import ArtifactKind

if TYPE_CHECKING:
    from thymira.schemas import Artifact
    from thymira.state import ArtifactStore

_CORR_THRESHOLD = 0.95
"""A feature this correlated with the target is a leakage indicator (Appendix B)."""

_IDENTIFIER_CORR_THRESHOLD = 0.5
"""An identifier-like column even moderately correlated with the target is suspicious."""

_FORBIDDEN_OPS = frozenset({"winsorize", "impute", "encode"})
"""Transforms that must never touch the target or a sensitive attribute."""

_MAX_LISTED = 5
"""How many offending indices to quote in a finding detail before truncating."""

_BINARY_CLASSES = 2
"""A target with exactly this many distinct values can be encoded to 0/1 for correlation."""

_MIN_CORRELATION_POINTS = 2
"""Pearson correlation is undefined below this many paired points."""

_ParsedDataset = tuple[dict[str, list[str]], str | None, list[str], list[dict[str, Any]]]
"""``(columns, declared_target, sensitive_columns, transforms)`` parsed from a dataset artifact."""


@dataclass(frozen=True)
class _DatasetEvidence:
    """A dataset artifact reduced to aligned string columns with an identified target."""

    name: str
    target_column: str
    columns: dict[str, list[str]]
    sensitive: frozenset[str]
    transforms: tuple[dict[str, Any], ...]


# ----------------------------------------------------------------------------- A11: split integrity


def check_split_integrity(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute train/test split integrity from every split declaration in the store."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    splits = _split_declarations(store)
    if not splits:
        return ControlStatus.NOT_APPLICABLE, "no train/test split evidence"
    problems: list[str] = []
    for artifact, payload in splits:
        problems.extend(_one_split_problems(store, artifact, payload))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return ControlStatus.PASSED, f"{len(splits)} split declaration(s) verified"


def _one_split_problems(
    store: ArtifactStore, artifact: Artifact, payload: dict[str, Any]
) -> list[str]:
    """Recompute every split-integrity family for one declaration."""
    prefix = f"split {artifact.name!r}"
    train, train_bad = _index_list(payload.get("train_indices"))
    test, test_bad = _index_list(payload.get("test_indices"))
    labels = _split_labels(store, payload)
    n_rows = _split_n_rows(payload, labels)
    problems = _index_validity_problems(prefix, train, test, train_bad, test_bad, n_rows)
    problems.extend(_partition_problems(prefix, train, test))
    problems.extend(_coverage_problems(prefix, train, test, n_rows))
    if labels is not None and n_rows is not None and len(labels) == n_rows:
        problems.extend(_allocation_problems(prefix, train, test, labels, n_rows))
    return problems


def _index_validity_problems(
    prefix: str,
    train: list[int],
    test: list[int],
    train_bad: list[Any],
    test_bad: list[Any],
    n_rows: int | None,
) -> list[str]:
    """Report non-integer and out-of-range indices."""
    problems: list[str] = []
    if train_bad or test_bad:
        problems.append(f"{prefix}: non-integer indices {(train_bad + test_bad)[:_MAX_LISTED]}")
    out_of_range = [
        index for index in (*train, *test) if index < 0 or (n_rows is not None and index >= n_rows)
    ]
    if out_of_range:
        problems.append(f"{prefix}: out-of-range indices {sorted(set(out_of_range))[:_MAX_LISTED]}")
    return problems


def _partition_problems(prefix: str, train: list[int], test: list[int]) -> list[str]:
    """Report empty partitions, duplicate indices and train/test overlap."""
    problems: list[str] = []
    if not train or not test:
        problems.append(f"{prefix}: a partition is empty")
    duplicates = _duplicates(train) + _duplicates(test)
    if duplicates:
        problems.append(f"{prefix}: duplicate indices {sorted(set(duplicates))[:_MAX_LISTED]}")
    overlap = set(train) & set(test)
    if overlap:
        problems.append(f"{prefix}: {len(overlap)} index(es) shared between train and test")
    return problems


def _coverage_problems(
    prefix: str, train: list[int], test: list[int], n_rows: int | None
) -> list[str]:
    """Report rows the split covers with no partition."""
    if n_rows is None:
        return []
    missing = set(range(n_rows)) - (set(train) | set(test))
    if missing:
        return [f"{prefix}: {len(missing)} row(s) covered by no partition"]
    return []


def _allocation_problems(
    prefix: str, train: list[int], test: list[int], labels: list[str], n_rows: int
) -> list[str]:
    """Report classes absent from either partition (per-class allocation)."""
    train_classes = {labels[index] for index in train if 0 <= index < n_rows}
    test_classes = {labels[index] for index in test if 0 <= index < n_rows}
    all_classes = set(labels)
    problems: list[str] = []
    missing_train = all_classes - train_classes
    if missing_train:
        problems.append(f"{prefix}: class(es) {sorted(missing_train)} absent from train")
    missing_test = all_classes - test_classes
    if missing_test:
        problems.append(f"{prefix}: class(es) {sorted(missing_test)} absent from test")
    return problems


def _index_list(value: Any) -> tuple[list[int], list[Any]]:
    """Split a declared index list into valid integers and non-integer entries."""
    if not isinstance(value, list):
        return [], []
    good: list[int] = []
    bad: list[Any] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            bad.append(item)
        else:
            good.append(item)
    return good, bad


def _duplicates(indices: list[int]) -> list[int]:
    """Return the indices that appear more than once."""
    seen: set[int] = set()
    repeated: list[int] = []
    for index in indices:
        if index in seen:
            repeated.append(index)
        seen.add(index)
    return repeated


def _split_n_rows(payload: dict[str, Any], labels: list[str] | None) -> int | None:
    """Resolve the row count from the declaration, falling back to the label count."""
    raw = payload.get("n_rows")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if labels is not None:
        return len(labels)
    return None


def _split_labels(store: ArtifactStore, payload: dict[str, Any]) -> list[str] | None:
    """Resolve per-row class labels from the declaration or its referenced dataset."""
    inline = payload.get("labels")
    if isinstance(inline, list):
        return [_scalar(value) for value in inline]
    dataset = payload.get("dataset")
    target = payload.get("target_column")
    if isinstance(dataset, str) and isinstance(target, str):
        artifact = store.get(dataset)
        if artifact is not None:
            parsed = _parse_dataset(store, artifact)
            if parsed is not None and target in parsed[0]:
                return parsed[0][target]
    return None


# ------------------------------------------------------------------------- A20: leakage indicators


def check_leakage_indicators(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute leakage indicators from the dataset evidence."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    evidence = _dataset_evidence(store)
    if evidence is None:
        return ControlStatus.NOT_APPLICABLE, "no dataset evidence with an identified target"
    problems = _leakage_problems(evidence)
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    feature_count = len(evidence.columns) - 1
    return ControlStatus.PASSED, f"no leakage indicators across {feature_count} feature(s)"


def _leakage_problems(evidence: _DatasetEvidence) -> list[str]:
    """Collect every leakage indicator over one dataset's features and declared transforms."""
    target_values = evidence.columns[evidence.target_column]
    encoded_target = _encode_target(target_values)
    problems: list[str] = []
    for name, values in evidence.columns.items():
        if name == evidence.target_column:
            continue
        problems.extend(_feature_leakage(name, values, target_values, encoded_target))
    problems.extend(_transform_leakage(evidence))
    return problems


def _feature_leakage(
    name: str,
    values: list[str],
    target_values: list[str],
    encoded_target: list[float] | None,
) -> list[str]:
    """Report an exact target copy or a target-correlated feature."""
    if values == target_values:
        return [f"feature {name!r} is an exact copy of the target"]
    numeric = _to_floats(values)
    if numeric is None or encoded_target is None:
        return []
    correlation = _pearson(numeric, encoded_target)
    if correlation is None:
        return []
    magnitude = abs(correlation)
    if magnitude >= _CORR_THRESHOLD:
        return [f"feature {name!r} is {magnitude:.2f}-correlated with the target"]
    if magnitude >= _IDENTIFIER_CORR_THRESHOLD and _is_identifier(name, values):
        return [f"identifier-like feature {name!r} is {magnitude:.2f}-correlated with the target"]
    return []


def _transform_leakage(evidence: _DatasetEvidence) -> list[str]:
    """Report a forbidden transform applied to the target or a sensitive attribute."""
    problems: list[str] = []
    for transform in evidence.transforms:
        column = transform.get("column")
        operation = transform.get("op")
        if not isinstance(column, str) or not isinstance(operation, str):
            continue
        protected = column == evidence.target_column or column in evidence.sensitive
        if operation in _FORBIDDEN_OPS and protected:
            problems.append(f"forbidden {operation!r} applied to protected column {column!r}")
    return problems


def _is_identifier(name: str, values: list[str]) -> bool:
    """Whether a column looks like a row identifier (by name or by all-unique values)."""
    lowered = name.casefold()
    if lowered in {"id", "index", "idx", "row_id", "rowid"} or lowered.endswith("_id"):
        return True
    return len(values) > 1 and len(set(values)) == len(values)


def _encode_target(values: list[str]) -> list[float] | None:
    """Numeric encoding of the target: numbers as-is, a binary category as 0/1, else ``None``."""
    numeric = _to_floats(values)
    if numeric is not None:
        return numeric
    uniques = sorted(set(values))
    if len(uniques) == _BINARY_CLASSES:
        mapping = {uniques[0]: 0.0, uniques[1]: 1.0}
        return [mapping[value] for value in values]
    return None


def _to_floats(values: list[str]) -> list[float] | None:
    """Parse a column to floats, or ``None`` when any value is non-numeric."""
    result: list[float] = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            return None
    return result


def _pearson(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation, or ``None`` when it is undefined (too short or constant)."""
    if len(left) != len(right) or len(left) < _MIN_CORRELATION_POINTS:
        return None
    try:
        return statistics.correlation(left, right)
    except statistics.StatisticsError:
        return None


# ------------------------------------------------------------------------- A12: lineage coherence


def check_lineage_coherence(store: ArtifactStore | None) -> tuple[ControlStatus, str]:
    """Recompute the split -> candidates -> selection -> model -> evaluation chain from lineage."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    splits = _split_declarations(store)
    models = [artifact for artifact in store.list_active() if artifact.kind is ArtifactKind.MODEL]
    if not splits and not models:
        return ControlStatus.NOT_APPLICABLE, "no modelling chain evidence"
    declared = _declared_split(splits)
    split_ids = {artifact.id for artifact, _ in splits}
    model_ids = {model.id for model in models}
    problems: list[str] = []
    for model in models:
        problems.extend(_candidate_split_problems(model, declared, split_ids))
    selected_id = _selected_model_id(store)
    if selected_id is not None and model_ids and selected_id not in model_ids:
        problems.append(f"selected model {selected_id} is not among the fitted candidates")
    problems.extend(_evaluation_problems(store, selected_id, model_ids))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    return ControlStatus.PASSED, f"lineage coherent across {len(models)} candidate(s)"


def _declared_split(splits: list[tuple[Artifact, dict[str, Any]]]) -> Artifact | None:
    """The split the candidates must reference: the flagged one, else the only one."""
    flagged = [artifact for artifact, payload in splits if payload.get("declared") is True]
    if flagged:
        return flagged[0]
    if len(splits) == 1:
        return splits[0][0]
    return None


def _candidate_split_problems(
    model: Artifact, declared: Artifact | None, split_ids: set[str]
) -> list[str]:
    """Verify one candidate was fitted on the declared split via its input lineage."""
    if declared is None:
        return []
    other = [
        artifact_id
        for artifact_id in model.input_artifact_ids
        if artifact_id in split_ids and artifact_id != declared.id
    ]
    if other:
        return [f"candidate {model.name!r} references split {other[0]}, not the declared split"]
    if declared.id not in model.input_artifact_ids:
        return [f"candidate {model.name!r} was not fitted on the declared split"]
    return []


def _selected_model_id(store: ArtifactStore) -> str | None:
    """The selected model id declared by a selection artifact, if any."""
    for artifact in store.list_active():
        if not artifact.name.endswith(".json"):
            continue
        payload = _load_json(store, artifact.name)
        if not isinstance(payload, dict):
            continue
        for key in ("selected_model_id", "selected_model"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _evaluation_problems(
    store: ArtifactStore, selected_id: str | None, model_ids: set[str]
) -> list[str]:
    """Verify every evaluation artifact scored the selected model via its input lineage."""
    if selected_id is None:
        return []
    problems: list[str] = []
    for artifact in store.list_active():
        if artifact.kind not in (ArtifactKind.METRICS, ArtifactKind.REPORT):
            continue
        scored = [
            artifact_id
            for artifact_id in artifact.input_artifact_ids
            if artifact_id in model_ids and artifact_id != selected_id
        ]
        if scored:
            problems.append(
                f"evaluation {artifact.name!r} scored model {scored[0]}, not the selected model"
            )
    return problems


# --------------------------------------------------------------------------- shared evidence access


def _split_declarations(store: ArtifactStore) -> list[tuple[Artifact, dict[str, Any]]]:
    """Every active JSON artifact that declares train and test index lists."""
    declarations: list[tuple[Artifact, dict[str, Any]]] = []
    for artifact in store.list_active():
        if not artifact.name.endswith(".json"):
            continue
        payload = _load_json(store, artifact.name)
        if isinstance(payload, dict) and "train_indices" in payload and "test_indices" in payload:
            declarations.append((artifact, payload))
    return declarations


def _dataset_evidence(store: ArtifactStore) -> _DatasetEvidence | None:
    """The first dataset artifact whose target column can be identified and parsed."""
    split_targets = _split_target_map(store)
    for artifact in store.list_active():
        if artifact.kind is not ArtifactKind.DATASET:
            continue
        parsed = _parse_dataset(store, artifact)
        if parsed is None:
            continue
        columns, declared_target, sensitive, transforms = parsed
        target = declared_target or split_targets.get(artifact.name)
        if target is not None and target in columns:
            return _DatasetEvidence(
                name=artifact.name,
                target_column=target,
                columns=columns,
                sensitive=frozenset(sensitive),
                transforms=tuple(transforms),
            )
    return None


def _split_target_map(store: ArtifactStore) -> dict[str, str]:
    """Map each dataset name a split references to the target column that split declares."""
    mapping: dict[str, str] = {}
    for _artifact, payload in _split_declarations(store):
        dataset = payload.get("dataset")
        target = payload.get("target_column")
        if isinstance(dataset, str) and isinstance(target, str):
            mapping.setdefault(dataset, target)
    return mapping


def _parse_dataset(store: ArtifactStore, artifact: Artifact) -> _ParsedDataset | None:
    """Parse a dataset artifact (JSON rows/columns or CSV) into aligned string columns."""
    name = artifact.name
    if name.endswith(".json"):
        return _parse_json_dataset(_load_json(store, name))
    if name.endswith(".csv"):
        return _parse_csv_dataset(_load_text(store, name))
    return None


def _parse_json_dataset(raw: Any) -> _ParsedDataset | None:
    """Parse a JSON dataset: a dict with ``columns``/``rows``, or a bare list of rows."""
    if isinstance(raw, dict):
        columns = _dict_columns(raw)
        if columns is None:
            return None
        declared = raw.get("target_column")
        target = declared if isinstance(declared, str) else None
        sensitive = [value for value in raw.get("sensitive", []) if isinstance(value, str)]
        transforms = [entry for entry in raw.get("transforms", []) if isinstance(entry, dict)]
        return columns, target, sensitive, transforms
    if isinstance(raw, list):
        return _rows_to_columns(raw), None, [], []
    return None


def _parse_csv_dataset(text: str | None) -> _ParsedDataset | None:
    """Parse a CSV dataset into aligned string columns (target resolved from a split)."""
    if text is None:
        return None
    rows = list(csv.DictReader(text.splitlines()))
    return _rows_to_columns(rows), None, [], []


def _dict_columns(raw: dict[str, Any]) -> dict[str, list[str]] | None:
    """Extract aligned string columns from a ``columns`` map or a ``rows`` list."""
    columns = raw.get("columns")
    if isinstance(columns, dict):
        return {
            str(key): [_scalar(value) for value in values]
            for key, values in columns.items()
            if isinstance(values, list)
        }
    rows = raw.get("rows")
    if isinstance(rows, list):
        return _rows_to_columns(rows)
    return None


def _rows_to_columns(rows: list[Any]) -> dict[str, list[str]]:
    """Turn a list of row dicts into aligned string columns keyed by first-seen column name."""
    keys: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                text = str(key)
                if text not in keys:
                    keys.append(text)
    columns: dict[str, list[str]] = {key: [] for key in keys}
    for row in rows:
        mapping = row if isinstance(row, dict) else {}
        for key in keys:
            columns[key].append(_scalar(mapping.get(key)))
    return columns


def _scalar(value: Any) -> str:
    """Normalise one cell to a string, mapping a missing value to the empty string."""
    return "" if value is None else str(value)


def _load_json(store: ArtifactStore, name: str) -> Any:
    """Load an artifact as JSON, returning ``None`` when it is missing or not JSON."""
    try:
        value = store.load_json(name)
    except (OSError, ValueError):
        return None
    else:
        return value


def _load_text(store: ArtifactStore, name: str) -> str | None:
    """Load an artifact as UTF-8 text, returning ``None`` when it cannot be read."""
    try:
        text = store.load_text(name)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    else:
        return text


__all__ = [
    "check_leakage_indicators",
    "check_lineage_coherence",
    "check_split_integrity",
]
