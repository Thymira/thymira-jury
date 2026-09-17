"""Pure recomputation and comparison of dataset-profile statistics."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    import polars as pl

_TOP_VALUE_LIMIT = 20
_HISTOGRAM_BINS = 10
_MIN_CORRELATION_COLUMNS = 2
_NUMERIC_REL_TOLERANCE = 1e-9
_CORRELATION_ABS_TOLERANCE = 1e-12


class ProfileClaimError(ValueError):
    """A profile claim is malformed or differs from independently computed evidence."""


@dataclass(frozen=True, slots=True)
class _ValueGroup:
    """One independently counted scalar value."""

    value: object
    count: int


def verify_profile_claims(payload: dict[str, Any], frame: pl.DataFrame) -> None:
    """Verify every claimed profile statistic against ``frame``.

    Raises:
        ProfileClaimError: If a claim is malformed, unsupported, or incorrect.
    """
    _verify_shape(payload.get("shape"), frame)
    claimed_columns = payload.get("columns")
    if not isinstance(claimed_columns, dict):
        _fail("columns", "must be an object")
    if set(claimed_columns) != set(frame.columns):
        _fail("columns", "keys do not match the selected source columns")
    for name in frame.columns:
        _verify_column(name, claimed_columns[name], frame.get_column(name))
    _verify_correlation(payload.get("correlation"), frame)


def _verify_shape(claim: object, frame: pl.DataFrame) -> None:
    if not isinstance(claim, dict) or set(claim) != {"rows", "columns"}:
        _fail("shape", "must contain exactly rows and columns")
    _exact_count("shape.rows", claim["rows"], frame.height)
    _exact_count("shape.columns", claim["columns"], frame.width)


def _verify_column(name: str, claim: object, series: pl.Series) -> None:
    path = f"columns/{_pointer_part(name)}"
    expected_keys = {"null_count", "unique_count", "top_values"}
    if series.dtype.is_numeric():
        expected_keys.add("histogram")
    if not isinstance(claim, dict) or set(claim) != expected_keys:
        _fail(path, f"must contain exactly {', '.join(sorted(expected_keys))}")
    values = series.to_list()
    groups = _group_values(values, path)
    _exact_count(f"{path}.null_count", claim["null_count"], series.null_count())
    _exact_count(f"{path}.unique_count", claim["unique_count"], len(groups))
    _verify_top_values(name, claim["top_values"], groups, path)
    if series.dtype.is_numeric():
        _verify_histogram(claim["histogram"], values, path)


def _group_values(values: list[object], path: str) -> list[_ValueGroup]:
    representatives: dict[tuple[str, object], object] = {}
    counts: Counter[tuple[str, object]] = Counter()
    for value in values:
        key = _scalar_key(value, path)
        representatives.setdefault(key, value)
        counts[key] += 1
    return [_ValueGroup(representatives[key], count) for key, count in counts.items()]


def _scalar_key(value: object, path: str) -> tuple[str, object]:
    if value is None:
        result: tuple[str, object] = ("null", "null")
    elif isinstance(value, bool):
        result = ("bool", value)
    elif isinstance(value, int):
        result = ("int", value)
    elif isinstance(value, float):
        if math.isnan(value):
            result = ("float", "nan")
        elif value == 0:
            result = ("float", 0.0)
        else:
            result = ("float", value)
    elif isinstance(value, str):
        result = ("str", value)
    else:
        raise ProfileClaimError(
            f"field {path}: contains unsupported structured value of type {type(value).__name__}"
        )
    return result


def _verify_top_values(
    name: str,
    claim: object,
    groups: list[_ValueGroup],
    path: str,
) -> None:
    top_path = f"{path}.top_values"
    if not isinstance(claim, list):
        _fail(top_path, "must be an array")
    expected_size = min(_TOP_VALUE_LIMIT, len(groups))
    if len(claim) != expected_size:
        _fail(top_path, f"must contain exactly {expected_size} entries")
    by_key = {_scalar_key(group.value, path): group.count for group in groups}
    seen: set[tuple[str, object]] = set()
    claimed_counts: list[int] = []
    for index, entry in enumerate(claim):
        entry_path = f"{top_path}[{index}]"
        if not isinstance(entry, dict) or set(entry) != {name, "count"}:
            _fail(entry_path, "must contain exactly the column value field and count")
        key = _scalar_key(entry[name], entry_path)
        if key in seen:
            _fail(entry_path, "duplicates an earlier value")
        seen.add(key)
        expected_count = by_key.get(key)
        if expected_count is None:
            _fail(entry_path, "names a value absent from the source")
        _exact_count(f"{entry_path}.count", entry["count"], expected_count)
        claimed_counts.append(expected_count)
    if claimed_counts != sorted(claimed_counts, reverse=True):
        _fail(top_path, "counts must be in descending order")
    if expected_size:
        cutoff = sorted((group.count for group in groups), reverse=True)[expected_size - 1]
        required = {_scalar_key(group.value, path) for group in groups if group.count > cutoff}
        if not required.issubset(seen):
            _fail(top_path, "omits a value whose frequency is above the cutoff tie")
        if any(by_key[key] < cutoff for key in seen):
            _fail(top_path, "contains a value whose frequency is below the cutoff tie")


def _verify_histogram(claim: object, values: list[object], path: str) -> None:
    histogram_path = f"{path}.histogram"
    if not isinstance(claim, dict) or set(claim) != {"edges", "counts"}:
        _fail(histogram_path, "must contain exactly edges and counts")
    expected_edges, expected_counts = _histogram(values, path)
    counts = claim["counts"]
    edges = claim["edges"]
    if not isinstance(counts, list) or len(counts) != len(expected_counts):
        _fail(f"{histogram_path}.counts", "has the wrong number of bins")
    for index, expected in enumerate(expected_counts):
        _exact_count(f"{histogram_path}.counts[{index}]", counts[index], expected)
    if not isinstance(edges, list) or len(edges) != len(expected_edges):
        _fail(f"{histogram_path}.edges", "has the wrong number of boundaries")
    for index, expected in enumerate(expected_edges):
        _close_number(f"{histogram_path}.edges[{index}]", edges[index], expected, abs_tol=0.0)


def _histogram(values: list[object], path: str) -> tuple[list[float], list[int]]:
    finite: list[float] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(path, f"contains unsupported numeric value of type {type(value).__name__}")
        number = float(value)
        if math.isfinite(number):
            finite.append(number)
    if not finite:
        return [], []
    lower, upper = min(finite), max(finite)
    if lower == upper:
        return [lower, upper], [len(finite)]
    width = (upper - lower) / _HISTOGRAM_BINS
    if not math.isfinite(width) or width == 0:
        _fail(path, "has a numeric range that cannot be represented by the profile histogram")
    counts = [0] * _HISTOGRAM_BINS
    try:
        for value in finite:
            index = min(int((value - lower) / width), _HISTOGRAM_BINS - 1)
            counts[index] += 1
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise ProfileClaimError(
            f"field {path}: numeric histogram computation failed: {exc}"
        ) from exc
    return (
        [lower + width * index for index in range(_HISTOGRAM_BINS + 1)],
        counts,
    )


def _verify_correlation(claim: object, frame: pl.DataFrame) -> None:
    path = "correlation"
    if not isinstance(claim, dict):
        _fail(path, "must be an object")
    numeric_names = [name for name, dtype in frame.schema.items() if dtype.is_numeric()]
    if len(numeric_names) < _MIN_CORRELATION_COLUMNS:
        if claim:
            _fail(path, "must be empty when fewer than two numeric columns are selected")
        return
    if set(claim) != set(numeric_names):
        _fail(path, "keys do not match the selected numeric columns")
    columns = [frame.get_column(name).to_list() for name in numeric_names]
    for column_index, name in enumerate(numeric_names):
        claimed_column = claim[name]
        if not isinstance(claimed_column, list) or len(claimed_column) != len(numeric_names):
            _fail(f"{path}[{name!r}]", "has the wrong axis length")
        for row_index, other in enumerate(columns):
            expected = _pearson(columns[column_index], other)
            _close_number(
                f"{path}/{_pointer_part(name)}/{row_index}",
                claimed_column[row_index],
                expected,
                abs_tol=_CORRELATION_ABS_TOLERANCE,
            )


def _pearson(left: list[object], right: list[object]) -> float:
    if len(left) < _MIN_CORRELATION_COLUMNS or len(left) != len(right):
        return math.nan
    left_numbers = _finite_series(left)
    right_numbers = _finite_series(right)
    if left_numbers is None or right_numbers is None:
        return math.nan
    left_mean = math.fsum(value / len(left_numbers) for value in left_numbers)
    right_mean = math.fsum(value / len(right_numbers) for value in right_numbers)
    left_deviations = [value - left_mean for value in left_numbers]
    right_deviations = [value - right_mean for value in right_numbers]
    if not all(math.isfinite(value) for value in left_deviations) or not all(
        math.isfinite(value) for value in right_deviations
    ):
        return math.nan
    left_scale = max(abs(value) for value in left_deviations)
    right_scale = max(abs(value) for value in right_deviations)
    if left_scale == 0 or right_scale == 0:
        return math.nan
    left_scaled = [value / left_scale for value in left_deviations]
    right_scaled = [value / right_scale for value in right_deviations]
    left_ss = math.fsum(value * value for value in left_scaled)
    right_ss = math.fsum(value * value for value in right_scaled)
    denominator = math.sqrt(left_ss) * math.sqrt(right_ss)
    return (
        math.fsum(first * second for first, second in zip(left_scaled, right_scaled, strict=True))
        / denominator
    )


def _finite_series(values: list[object]) -> list[float] | None:
    numbers: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return numbers


def _exact_count(path: str, claim: object, expected: int) -> None:
    if isinstance(claim, bool) or not isinstance(claim, int) or claim != expected:
        _fail(path, f"expected {expected}, got {claim!r}")


def _close_number(path: str, claim: object, expected: float, *, abs_tol: float) -> None:
    if isinstance(claim, bool) or not isinstance(claim, (int, float)):
        _fail(path, f"expected numeric value {expected!r}, got {claim!r}")
    claimed = float(claim)
    if math.isnan(expected):
        if not math.isnan(claimed):
            _fail(path, "expected undefined NaN")
        return
    if not math.isfinite(claimed) or not math.isclose(
        claimed,
        expected,
        rel_tol=_NUMERIC_REL_TOLERANCE,
        abs_tol=abs_tol,
    ):
        _fail(path, f"expected {expected!r}, got {claim!r}")


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _fail(path: str, message: str) -> NoReturn:
    raise ProfileClaimError(f"field {path}: {message}")


__all__ = ["ProfileClaimError", "verify_profile_claims"]
