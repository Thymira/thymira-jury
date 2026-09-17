"""Fast regression tests for the data-analysis helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import polars as pl
import pytest

from tests.thymira.fixtures_tools import tool_invocation
from thymira.tools import register_dataset
from thymira.tools.builtins.data_analysis import ProfileDataset, _histogram

if TYPE_CHECKING:
    from pathlib import Path


def test_histogram_drops_nan_values_instead_of_raising(tmp_path: Path) -> None:
    # ``drop_nulls`` does not drop NaN in polars, so the NaN reached ``int(... )`` and raised
    # ``ValueError: cannot convert float NaN to integer``. It must be dropped like any non-finite.
    series = pl.Series("value", [1.0, float("nan"), 3.0, 2.0, float("inf")])

    histogram = _histogram(series)

    assert histogram["counts"]
    assert sum(histogram["counts"]) == 3
    assert histogram["edges"][0] == pytest.approx(1.0)
    assert histogram["edges"][-1] == pytest.approx(3.0)


def test_profile_dataset_profiles_a_numeric_column_that_contains_nan(tmp_path: Path) -> None:
    invocation = tool_invocation(tmp_path)
    invocation.workspace.mkdir(parents=True, exist_ok=True)
    # Parquet round-trips a float NaN faithfully, unlike a CSV null.
    frame = pl.DataFrame({"value": [1.0, float("nan"), 3.0, 2.0], "label": ["a", "b", "a", "b"]})
    source = invocation.workspace / "with_nan.parquet"
    frame.write_parquet(source)
    register_dataset(invocation.artifact_store, source, "with_nan", produced_by=invocation.agent_id)

    result = ProfileDataset().execute(
        invocation, {"dataset": "with_nan", "description": "Profile the registered sample dataset"}
    )
    payload = json.loads(result.stdout)

    histogram = payload["columns"]["value"]["histogram"]
    assert sum(histogram["counts"]) == 3
