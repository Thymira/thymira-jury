"""Content-addressed dataset registration and bounded loading."""

from __future__ import annotations

import csv
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
from pydantic import BaseModel, Field

from thymira.schemas import Artifact, ArtifactKind, new_id

if TYPE_CHECKING:
    from thymira.state import ArtifactStore

_RAGGED_LINES_SHOWN = 10

MAX_DATASET_BYTES = 256 * 1024 * 1024
"""The largest declared dataset `register_dataset` will read.

Registration runs at Inspect, before the Tool Manager and before any policy decision -- so a
project that declares a multi-gigabyte file must not be allowed to load it into memory and take
the runtime down. A file over this ceiling is refused outright, before it is even opened for
schema capture.
"""


def schema_artifact_name(name: str) -> str:
    """Return the artifact name a registered dataset's captured schema is saved under.

    The one place the ``datasets/<name>.schema.json`` name is *built*; `thymira.agents.
    runtime_context` parses the same convention when it lists a store (it cannot import this
    module at load time, THY-33).
    """
    return f"datasets/{name}.schema.json"


class DatasetSchema(BaseModel):
    """Stable facts captured when a dataset enters a run."""

    name: str = Field(min_length=1)
    artifact_name: str = Field(min_length=1)
    columns: tuple[str, ...]
    dtypes: tuple[str, ...]
    row_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str | None = Field(default=None, min_length=1)


def register_dataset(
    store: ArtifactStore,
    path: Path,
    name: str,
    *,
    produced_by: str | None = None,
    source_path: str | None = None,
) -> tuple[Artifact, DatasetSchema]:
    """Register a CSV or Parquet file and persist its captured schema as JSON.

    Refuses a file over `MAX_DATASET_BYTES` before reading it: registration runs at Inspect,
    before any policy decision, so a declared file that size alone makes dangerous must not
    reach `_read_frame`.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.casefold()
    if suffix not in {".csv", ".parquet"}:
        raise ValueError("datasets must be CSV or Parquet files")
    size = source.stat().st_size
    if size > MAX_DATASET_BYTES:
        msg = (
            f"{source.name}: {size} bytes exceeds the {MAX_DATASET_BYTES}-byte registration ceiling"
        )
        raise ValueError(msg)
    frame = _read_frame(source)
    data = source.read_bytes()
    artifact_name = f"datasets/{name}{suffix}"
    owner = produced_by or new_id("tool")
    artifact = store.save_bytes(
        artifact_name,
        data,
        produced_by=owner,
        kind=ArtifactKind.DATASET,
        media_type="text/csv" if suffix == ".csv" else "application/vnd.apache.parquet",
    )
    schema = DatasetSchema(
        name=name,
        artifact_name=artifact.name,
        columns=tuple(frame.columns),
        dtypes=tuple(str(dtype) for dtype in frame.dtypes),
        row_count=frame.height,
        byte_size=len(data),
        sha256=sha256(data).hexdigest(),
        source_path=source_path.replace("\\", "/") if source_path is not None else None,
    )
    store.save_json(
        schema_artifact_name(name),
        schema.model_dump(mode="json"),
        produced_by=owner,
        kind=ArtifactKind.OTHER,
        media_type="application/json",
    )
    return artifact, schema


def load_dataset(store: ArtifactStore, name: str, *, max_rows: int) -> pl.DataFrame:
    """Load a registered dataset and enforce a hard row cap."""
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    schema = DatasetSchema.model_validate(store.load_json(schema_artifact_name(name)))
    raw = store.load_bytes(schema.artifact_name)
    suffix = Path(schema.artifact_name).suffix.casefold()
    if suffix == ".csv":
        frame = pl.read_csv(BytesIO(raw))
    elif suffix == ".parquet":
        frame = pl.read_parquet(BytesIO(raw))
    else:
        raise ValueError(f"unsupported registered dataset: {schema.artifact_name}")
    return frame.head(max_rows)


def _read_frame(path: Path) -> pl.DataFrame:
    """Read a supported dataset only for schema capture.

    A ragged CSV (a row whose field count differs from the header's) is refused, naming the
    1-based line numbers of the offending rows so the owner can fix the file rather than guess
    which value is missing. Raggedness is checked up front rather than left to polars: polars
    only raises for a row *longer* than the header (``ComputeError``) and silently pads a
    *shorter* row with nulls, so relying on that exception alone would miss short rows entirely.
    The file is read twice on purpose -- once by ``csv.reader`` for the width check, once by
    polars -- which is negligible at the demo-dataset size ceiling (well under 1 MB). Any other
    polars read failure (for example ``NoDataError`` on an empty file) is still turned into a
    ``ValueError`` so callers only ever have to catch one exception type.
    """
    if path.suffix.casefold() != ".csv":
        try:
            return pl.read_parquet(path)
        except pl.exceptions.PolarsError as exc:
            msg = f"{path.name}: cannot be read as Parquet: {exc}"
            raise ValueError(msg) from exc
    width, ragged = _ragged_lines(path)
    if ragged:
        shown = ", ".join(str(number) for number in ragged[:_RAGGED_LINES_SHOWN])
        more = (
            f" and {len(ragged) - _RAGGED_LINES_SHOWN} more"
            if len(ragged) > _RAGGED_LINES_SHOWN
            else ""
        )
        msg = (
            f"{path.name}: {len(ragged)} row(s) do not match the header's {width} fields "
            f"(lines {shown}{more}); fix the file before registering it"
        )
        raise ValueError(msg)
    try:
        return pl.read_csv(path)
    except pl.exceptions.PolarsError as exc:
        msg = f"{path.name}: cannot be read as CSV: {exc}"
        raise ValueError(msg) from exc


def _ragged_lines(path: Path) -> tuple[int, list[int]]:
    """Return the header width and the 1-based physical line numbers of the ragged records.

    Each entry is ``reader.line_num`` -- the physical line at which `csv.reader` finished
    parsing the offending record -- rather than a plain row counter, so a quoted field that
    itself spans multiple physical lines does not shift the line numbers reported for every
    record that follows it.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return 0, []
        width = len(header)
        ragged = [reader.line_num for row in reader if row and len(row) != width]
    return width, ragged


class DatasetFileSummary(BaseModel):
    """The shape of a dataset file as registration would capture it."""

    rows: int = Field(ge=0)
    columns: tuple[str, ...]


def _check_dataset_file(source: Path) -> None:
    """Apply registration's up-front refusals -- presence, format and size -- to one file."""
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.casefold() not in {".csv", ".parquet"}:
        raise ValueError("datasets must be CSV or Parquet files")
    size = source.stat().st_size
    if size > MAX_DATASET_BYTES:
        msg = (
            f"{source.name}: {size} bytes exceeds the {MAX_DATASET_BYTES}-byte registration ceiling"
        )
        raise ValueError(msg)


def describe_dataset_file(path: Path) -> DatasetFileSummary:
    """Read a CSV or Parquet file exactly as :func:`register_dataset` would, without registering it.

    A file this refuses -- the wrong format, over the size ceiling, ragged or unparsable -- is one
    Inspect would refuse when a Run registers it, so a caller can reject it before any Run starts.
    """
    source = Path(path)
    _check_dataset_file(source)
    frame = _read_frame(source)
    return DatasetFileSummary(rows=frame.height, columns=tuple(frame.columns))


def dataset_columns(path: Path) -> tuple[str, ...]:
    """Return a dataset file's column names, reading only its first row or its Parquet schema."""
    source = Path(path)
    _check_dataset_file(source)
    try:
        if source.suffix.casefold() == ".csv":
            return tuple(pl.read_csv(source, n_rows=1).columns)
        return tuple(pl.read_parquet_schema(source).keys())
    except pl.exceptions.PolarsError as exc:
        msg = f"{source.name}: cannot read the dataset's columns: {exc}"
        raise ValueError(msg) from exc


__all__ = [
    "MAX_DATASET_BYTES",
    "DatasetFileSummary",
    "DatasetSchema",
    "dataset_columns",
    "describe_dataset_file",
    "load_dataset",
    "register_dataset",
    "schema_artifact_name",
]
