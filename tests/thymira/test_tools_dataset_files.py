"""Unit tests for reading a dataset file the way registration will, before any Run registers it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from thymira.tools import datasets as datasets_module
from thymira.tools.datasets import dataset_columns, describe_dataset_file

if TYPE_CHECKING:
    from pathlib import Path


def _csv(tmp_path: Path, text: str, name: str = "loans.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_describe_dataset_file_reports_the_rows_and_columns_of_a_csv(tmp_path: Path) -> None:
    summary = describe_dataset_file(_csv(tmp_path, "amount,term\n500,12\n900,24\n"))

    assert (summary.rows, summary.columns) == (2, ("amount", "term"))


def test_describe_dataset_file_refuses_a_ragged_csv(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="do not match"):
        describe_dataset_file(_csv(tmp_path, "amount,term\n500\n"))


def test_describe_dataset_file_refuses_a_format_registration_does_not_read(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="CSV or Parquet"):
        describe_dataset_file(_csv(tmp_path, "amount\n1\n", name="loans.xlsx"))


def test_describe_dataset_file_refuses_a_file_over_the_registration_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(datasets_module, "MAX_DATASET_BYTES", 4)

    with pytest.raises(ValueError, match="registration ceiling"):
        describe_dataset_file(_csv(tmp_path, "amount,term\n500,12\n"))


def test_dataset_columns_reads_a_csv_header(tmp_path: Path) -> None:
    assert dataset_columns(_csv(tmp_path, "amount,term,defaulted\n500,12,0\n")) == (
        "amount",
        "term",
        "defaulted",
    )


def test_dataset_columns_reads_a_parquet_schema(tmp_path: Path) -> None:
    path = tmp_path / "loans.parquet"
    pl.DataFrame({"amount": [500, 900], "defaulted": [0, 1]}).write_parquet(path)

    assert dataset_columns(path) == ("amount", "defaulted")
