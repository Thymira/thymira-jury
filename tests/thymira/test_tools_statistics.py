"""Regression tests for the ``run_statistics`` tool's scientific-claim guarantees.

Each test here pins a way the tool could otherwise put a false scientific claim into a
hash-chained event and a sha256'd report artifact: a two-sample test reported over three
groups, a null category silently mangling a contingency table, a correlation over too few
pairs, or a NaN statistic serialized as invalid JSON.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest

from thymira.schemas import new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation, register_dataset
from thymira.tools.builtins import RunStatistics
from thymira.tools.builtins.run_statistics import _run_test

if TYPE_CHECKING:
    from pathlib import Path


def _reject_non_finite(token: str) -> float:
    """Refuse the ``NaN`` / ``Infinity`` tokens a strict JSON reader must not accept."""
    raise ValueError(f"non-finite JSON token is not valid evidence: {token}")


def _strict_json_roundtrip(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialize as the tool does and parse it back with a strict (spec-compliant) reader."""
    text = json.dumps(payload, sort_keys=True)
    return json.loads(text, parse_constant=_reject_non_finite)


# --- #56-2: welch_t is a two-sample test; anova is the many-group test -------------------


def test_welch_t_rejects_three_groups_instead_of_silently_comparing_only_two() -> None:
    frame = pl.DataFrame(
        {
            "value": [1.0, 2.0, 10.0, 12.0, 100.0, 102.0],
            "grp": ["A", "A", "B", "B", "C", "C"],
        }
    )

    with pytest.raises(ValueError, match="welch_t requires exactly two groups"):
        _run_test(frame, {"value_column": "value", "group_column": "grp"}, "welch_t")


def test_welch_t_reports_the_two_group_result_when_exactly_two_groups_are_present() -> None:
    frame = pl.DataFrame({"value": [1.0, 2.0, 10.0, 12.0], "grp": ["A", "A", "B", "B"]})

    result = _run_test(frame, {"value_column": "value", "group_column": "grp"}, "welch_t")

    assert result["groups"] == ["A", "B"]
    assert math.isfinite(result["statistic"])
    assert math.isfinite(result["p_value"])


def test_anova_accepts_three_or_more_groups() -> None:
    frame = pl.DataFrame(
        {
            "value": [1.0, 2.0, 10.0, 12.0, 100.0, 102.0],
            "grp": ["A", "A", "B", "B", "C", "C"],
        }
    )

    result = _run_test(frame, {"value_column": "value", "group_column": "grp"}, "anova")

    assert result["groups"] == ["A", "B", "C"]
    assert math.isfinite(result["statistic"])


def test_anova_rejects_fewer_than_two_groups() -> None:
    frame = pl.DataFrame({"value": [1.0, 2.0, 3.0], "grp": ["A", "A", "A"]})

    with pytest.raises(ValueError, match="anova requires at least two groups"):
        _run_test(frame, {"value_column": "value", "group_column": "grp"}, "anova")


# --- #56-4a: a null category is not a category; drop the observation ----------------------


def test_chi_square_drops_null_categories_from_the_contingency_table() -> None:
    # One observation has a null column category (index 4) and one has a null row category
    # (index 5); both must be dropped so the reported levels carry no null.
    frame = pl.DataFrame(
        {
            "value": ["x", "x", "y", "y", None, "x", "y"],
            "grp": ["A", "A", "B", "B", "A", None, "B"],
        }
    )

    result = _run_test(frame, {"value_column": "value", "group_column": "grp"}, "chi_square")

    assert result["row_levels"] == ["A", "B"]
    assert result["column_levels"] == ["x", "y"]
    assert None not in result["row_levels"]
    assert "null" not in result["column_levels"]
    assert math.isfinite(result["statistic"])
    assert math.isfinite(result["p_value"])


def test_chi_square_rejects_a_table_with_no_non_null_observations() -> None:
    frame = pl.DataFrame(
        {"value": [None, None], "grp": [None, None]},
        schema={"value": pl.String, "grp": pl.String},
    )

    with pytest.raises(ValueError, match="chi_square requires at least one non-null observation"):
        _run_test(frame, {"value_column": "value", "group_column": "grp"}, "chi_square")


# --- #56-4b: a correlation needs at least two valid pairs, like its siblings --------------


@pytest.mark.parametrize(
    ("value", "second"),
    [
        pytest.param([None, None, None], [1.0, 2.0, 3.0], id="zero-valid-pairs"),
        pytest.param([1.0, None, None], [2.0, None, 3.0], id="one-valid-pair"),
    ],
)
def test_correlation_rejects_fewer_than_two_valid_pairs(
    value: list[float | None],
    second: list[float | None],
) -> None:
    frame = pl.DataFrame({"a": value, "b": second})

    with pytest.raises(ValueError, match="correlation requires at least two valid value pairs"):
        _run_test(frame, {"value_column": "a", "second_value_column": "b"}, "correlation")


def test_correlation_computes_over_the_pairs_without_nulls() -> None:
    frame = pl.DataFrame({"a": [1.0, None, 3.0, 4.0], "b": [2.0, 4.0, 6.0, 8.0]})

    result = _run_test(frame, {"value_column": "a", "second_value_column": "b"}, "correlation")

    assert result["statistic"] == pytest.approx(1.0)
    assert math.isfinite(result["p_value"])


# --- #39-4: an undefined (non-finite) statistic is a failed test, not NaN evidence -------


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.parametrize(
    ("test_name", "frame"),
    [
        pytest.param(
            "welch_t",
            pl.DataFrame({"value": [5.0, 5.0, 5.0, 5.0], "grp": ["A", "A", "B", "B"]}),
            id="welch_t-constant-groups",
        ),
        pytest.param(
            "normality",
            pl.DataFrame({"value": [3.0, 3.0, 3.0, 3.0]}),
            id="normality-constant-column",
        ),
    ],
)
def test_a_non_finite_statistic_is_reported_as_a_failed_test(
    test_name: str,
    frame: pl.DataFrame,
) -> None:
    with pytest.raises(ValueError, match=r"produced an undefined \(non-finite\) statistic"):
        _run_test(frame, {"value_column": "value", "group_column": "grp"}, test_name)


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_a_non_finite_result_never_reaches_the_report_artifact(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    source = tmp_path / "constant.csv"
    source.write_text("value\n3\n3\n3\n3\n", encoding="utf-8", newline="\n")
    register_dataset(store, source, "constant", produced_by=new_id("agent"))
    invocation = ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        artifact_store=store,
    )

    with pytest.raises(ValueError, match=r"produced an undefined \(non-finite\) statistic"):
        RunStatistics().execute(
            invocation,
            {
                "dataset": "constant",
                "value_column": "value",
                "test": "normality",
                "max_rows": 100_000,
            },
        )

    statistics_artifacts = [
        artifact for artifact in store.list_active() if artifact.name.startswith("statistics/")
    ]
    assert statistics_artifacts == []


def test_a_valid_report_serializes_to_strict_json_without_nan() -> None:
    frame = pl.DataFrame({"value": [1.0, 2.0, 10.0, 12.0], "grp": ["A", "A", "B", "B"]})

    payload = _run_test(frame, {"value_column": "value", "group_column": "grp"}, "welch_t")

    # A strict reader rejects the ``NaN`` token; a valid report must round-trip cleanly.
    restored = _strict_json_roundtrip(payload)
    assert restored["statistic"] == payload["statistic"]
