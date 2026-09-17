"""Independent source verification for versioned dataset-profile reports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn

import polars as pl

from thymira.mira.checks.profile_statistics import ProfileClaimError, verify_profile_claims
from thymira.schemas import ArtifactKind

if TYPE_CHECKING:
    from thymira.schemas import Artifact
    from thymira.state import ArtifactStore

_PROFILE_KEYS = {"profile_version", "source", "dataset", "shape", "columns", "correlation"}
_SOURCE_KEYS = {"artifact", "sha256", "schema_sha256", "max_rows", "columns"}
_SCHEMA_KEYS = {
    "name",
    "artifact_name",
    "columns",
    "dtypes",
    "row_count",
    "byte_size",
    "sha256",
    "source_path",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PROFILE_BYTES = 16 * 1024 * 1024
_MAX_SCHEMA_BYTES = 1024 * 1024
_MAX_DATASET_BYTES = 256 * 1024 * 1024
_MAX_ROWS = 1_000_000


class ProfileEvidenceError(ValueError):
    """A profile declaration or one of its registered sources cannot be verified."""


@dataclass(frozen=True, slots=True)
class ProfileVerification:
    """Result of classifying and, when applicable, verifying one report."""

    candidate: bool
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _SourceDeclaration:
    """Strictly parsed scope and content binding recorded by the producer."""

    artifact: str
    sha256: str
    schema_sha256: str
    max_rows: int
    columns: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _DatasetSchema:
    """Strict subset of the independently interpreted registered schema contract."""

    name: str
    artifact_name: str
    columns: tuple[str, ...]
    dtypes: tuple[str, ...]
    row_count: int
    byte_size: int
    sha256: str
    source_path: str | None


def verify_profile_report(store: ArtifactStore, report: Artifact) -> ProfileVerification:
    """Classify and independently verify one REPORT artifact."""
    reserved = report.name.startswith("profile/")
    try:
        raw = _verified_bytes(
            store, report.name, _MAX_PROFILE_BYTES, "profile report", report.run_id
        )
        payload = _load_json_object(raw, "profile report")
    except ProfileEvidenceError as exc:
        if not reserved:
            return ProfileVerification(candidate=False, passed=False)
        return _failed(report.name, (), exc)
    candidate = reserved or "profile_version" in payload
    if not candidate:
        return ProfileVerification(candidate=False, passed=False)

    schema_name: str | None = None
    dataset_name: str | None = None
    try:
        _require_exact_keys(payload, _PROFILE_KEYS, "profile")
        version = payload["profile_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            _evidence_fail("profile_version", "must be the integer 1")
        if version != 1:
            _evidence_fail("profile_version", f"unsupported version {version!r}")
        dataset = _required_text(payload["dataset"], "dataset")
        expected_report_name = f"profile/{dataset}.json"
        if report.name != expected_report_name:
            _evidence_fail("dataset", f"does not match report name {report.name!r}")
        source = _parse_source(payload["source"])
        schema_name = f"datasets/{dataset}.schema.json"
        dataset_name = source.artifact
        schema_artifact = _active_artifact(
            store, schema_name, "source.schema_sha256", report.run_id
        )
        if schema_artifact.kind is not ArtifactKind.OTHER:
            _evidence_fail("source.schema_sha256", "does not name an OTHER schema artifact")
        if schema_artifact.sha256 != source.schema_sha256:
            _evidence_fail("source.schema_sha256", "does not match the schema manifest digest")
        schema_raw = _verified_bytes(
            store, schema_name, _MAX_SCHEMA_BYTES, "dataset schema", report.run_id
        )
        schema = _parse_schema(_load_json_object(schema_raw, "dataset schema"))
        dataset_artifact = _active_artifact(store, dataset_name, "source.artifact", report.run_id)
        if dataset_artifact.kind is not ArtifactKind.DATASET:
            _evidence_fail("source.artifact", "does not name a DATASET artifact")
        dataset_raw = _verified_bytes(
            store, dataset_name, _MAX_DATASET_BYTES, "dataset", report.run_id
        )
        _verify_source_links(dataset, source, schema, dataset_artifact, dataset_raw)
        frame = _load_profile_frame(dataset_name, dataset_raw, source.max_rows, schema)
        selected = _selected_columns(source.columns, schema.columns)
        frame = frame.select(list(selected))
        verify_profile_claims(payload, frame)
    except (ProfileEvidenceError, ProfileClaimError) as exc:
        names = tuple(name for name in (schema_name, dataset_name) if name is not None)
        return _failed(report.name, names, exc)

    detail = (
        f"profile report {report.name!r} matches registered schema {schema_name!r} "
        f"and dataset {dataset_name!r}"
    )
    return ProfileVerification(
        candidate=True,
        passed=True,
        detail=detail,
    )


def _parse_source(value: object) -> _SourceDeclaration:
    if not isinstance(value, dict):
        _evidence_fail("source", "must be an object")
    _require_exact_keys(value, _SOURCE_KEYS, "source")
    columns_value = value["columns"]
    columns: tuple[str, ...] | None
    if columns_value is None:
        columns = None
    elif isinstance(columns_value, list) and all(
        isinstance(item, str) and item for item in columns_value
    ):
        columns = tuple(columns_value)
        if len(set(columns)) != len(columns):
            _evidence_fail("source.columns", "must not contain duplicates")
    else:
        _evidence_fail("source.columns", "must be null or an array of non-empty strings")
    max_rows = value["max_rows"]
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        _evidence_fail("source.max_rows", "must be an integer")
    if not 1 <= max_rows <= _MAX_ROWS:
        _evidence_fail("source.max_rows", f"must be between 1 and {_MAX_ROWS}")
    return _SourceDeclaration(
        artifact=_required_text(value["artifact"], "source.artifact"),
        sha256=_required_sha256(value["sha256"], "source.sha256"),
        schema_sha256=_required_sha256(value["schema_sha256"], "source.schema_sha256"),
        max_rows=max_rows,
        columns=columns,
    )


def _parse_schema(value: dict[str, Any]) -> _DatasetSchema:
    _require_exact_keys(value, _SCHEMA_KEYS, "schema")
    columns = _string_tuple(value["columns"], "schema.columns")
    dtypes = _string_tuple(value["dtypes"], "schema.dtypes")
    if len(columns) != len(dtypes):
        _evidence_fail("schema.dtypes", "length does not match schema.columns")
    if len(set(columns)) != len(columns):
        _evidence_fail("schema.columns", "must not contain duplicates")
    return _DatasetSchema(
        name=_required_text(value["name"], "schema.name"),
        artifact_name=_required_text(value["artifact_name"], "schema.artifact_name"),
        columns=columns,
        dtypes=dtypes,
        row_count=_nonnegative_int(value["row_count"], "schema.row_count"),
        byte_size=_nonnegative_int(value["byte_size"], "schema.byte_size"),
        sha256=_required_sha256(value["sha256"], "schema.sha256"),
        source_path=_optional_text(value["source_path"], "schema.source_path"),
    )


def _verify_source_links(
    dataset: str,
    source: _SourceDeclaration,
    schema: _DatasetSchema,
    dataset_artifact: Artifact,
    dataset_raw: bytes,
) -> None:
    if schema.name != dataset:
        _evidence_fail("schema.name", "does not match profile dataset")
    if schema.artifact_name != source.artifact:
        _evidence_fail("schema.artifact_name", "does not match source.artifact")
    if source.sha256 != dataset_artifact.sha256:
        _evidence_fail("source.sha256", "does not match the dataset manifest digest")
    if schema.sha256 != dataset_artifact.sha256:
        _evidence_fail("schema.sha256", "does not match the dataset manifest digest")
    if schema.byte_size != dataset_artifact.size_bytes:
        _evidence_fail("schema.byte_size", "does not match the dataset manifest size")
    if schema.byte_size != len(dataset_raw):
        _evidence_fail("schema.byte_size", "does not match the dataset bytes")


def _load_profile_frame(
    artifact_name: str,
    raw: bytes,
    max_rows: int,
    schema: _DatasetSchema,
) -> pl.DataFrame:
    suffix = PurePosixPath(artifact_name).suffix.casefold()
    try:
        lazy = _scan_dataset(suffix, raw)
        actual_schema = lazy.collect_schema()
        row_count = lazy.select(pl.len()).collect(engine="streaming").item()
        frame = _scan_dataset(suffix, raw).head(max_rows).collect(engine="streaming")
    except (OSError, TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise ProfileEvidenceError(
            f"field source.artifact: cannot decode registered data: {exc}"
        ) from exc
    if tuple(actual_schema) != schema.columns:
        _evidence_fail("schema.columns", "does not match the decoded dataset")
    if tuple(str(dtype) for dtype in actual_schema.values()) != schema.dtypes:
        _evidence_fail("schema.dtypes", "does not match the decoded dataset")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count != schema.row_count
    ):
        _evidence_fail("schema.row_count", f"expected decoded row count {row_count!r}")
    return frame


def _scan_dataset(suffix: str, raw: bytes) -> pl.LazyFrame:
    if suffix == ".csv":
        return pl.scan_csv(BytesIO(raw))
    if suffix == ".parquet":
        return pl.scan_parquet(BytesIO(raw))
    raise ProfileEvidenceError("field source.artifact: must be a CSV or Parquet artifact")


def _selected_columns(
    selected: tuple[str, ...] | None, available: tuple[str, ...]
) -> tuple[str, ...]:
    if selected is None:
        return available
    unknown = [name for name in selected if name not in available]
    if unknown:
        _evidence_fail("source.columns", f"contains unknown column {unknown[0]!r}")
    return selected


def _verified_bytes(
    store: ArtifactStore,
    name: str,
    limit: int,
    role: str,
    run_id: str,
) -> bytes:
    artifact = _active_artifact(store, name, role, run_id)
    if artifact.size_bytes > limit:
        raise ProfileEvidenceError(
            f"field {role}: artifact {name!r} exceeds the {limit}-byte verification limit"
        )
    try:
        raw = store.load_bytes(name)
    except (OSError, ValueError) as exc:
        raise ProfileEvidenceError(f"field {role}: cannot read artifact {name!r}: {exc}") from exc
    if len(raw) > limit:
        raise ProfileEvidenceError(
            f"field {role}: artifact {name!r} exceeds the {limit}-byte verification limit"
        )
    if len(raw) != artifact.size_bytes:
        raise ProfileEvidenceError(
            f"field {role}: artifact {name!r} fails manifest size verification"
        )
    if hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ProfileEvidenceError(f"field {role}: artifact {name!r} fails sha256 verification")
    return raw


def _active_artifact(store: ArtifactStore, name: str, path: str, run_id: str) -> Artifact:
    artifact = store.get(name)
    if artifact is None or not artifact.valid or not store.exists(name):
        raise ProfileEvidenceError(f"field {path}: active artifact {name!r} is absent")
    if artifact.run_id != run_id:
        raise ProfileEvidenceError(f"field {path}: artifact {name!r} belongs to another run")
    return artifact


def _load_json_object(raw: bytes, role: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProfileEvidenceError(f"field {role}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProfileEvidenceError(f"field {role}: is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileEvidenceError(f"field {role}: must be a JSON object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ProfileEvidenceError(
            f"field {path}: exact keys required; missing={missing!r}, extra={extra!r}"
        )


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        _evidence_fail(path, "must be a non-empty string")
    return value


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _required_sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _evidence_fail(path, "must be a lowercase sha256 digest")
    return value


def _string_tuple(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _evidence_fail(path, "must be an array of strings")
    return tuple(value)


def _nonnegative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _evidence_fail(path, "must be a non-negative integer")
    return value


def _evidence_fail(path: str, message: str) -> NoReturn:
    raise ProfileEvidenceError(f"field {path}: {message}")


def _failed(
    report_name: str,
    source_names: tuple[str, ...],
    error: ValueError,
) -> ProfileVerification:
    named = "".join(f" using artifact {name!r}" for name in source_names)
    return ProfileVerification(
        candidate=True,
        passed=False,
        detail=f"profile report {report_name!r}{named}: {error}",
    )


__all__ = ["ProfileVerification", "verify_profile_report"]
