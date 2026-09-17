"""Adversarial unit coverage for independent A18 dataset-profile verification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import polars as pl
import pytest

from thymira.mira.checks.models import ControlStatus
from thymira.mira.checks.report import check_report_fidelity
from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.builtins.data_analysis import ProfileDataset
from thymira.tools.datasets import register_dataset
from thymira.tools.models import ToolInvocation

if TYPE_CHECKING:
    from pathlib import Path


_PROFILE_DESCRIPTION = "Profile the registered sample dataset"
"""Rationale shared by every direct `ProfileDataset` call in this module's adversarial suite."""


def _profile(
    tmp_path: Path,
    frame: pl.DataFrame,
    *,
    suffix: str = ".csv",
    arguments: dict[str, Any] | None = None,
) -> tuple[LocalArtifactStore, dict[str, Any]]:
    run_id = new_id("run")
    agent_id = new_id("agent")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    source = tmp_path / f"sample{suffix}"
    if suffix == ".csv":
        frame.write_csv(source)
    else:
        frame.write_parquet(source)
    register_dataset(store, source, "sample", produced_by=agent_id)
    invocation = ToolInvocation(run_id, agent_id, tmp_path, store)
    ProfileDataset().execute(
        invocation,
        {"dataset": "sample", "description": _PROFILE_DESCRIPTION, **(arguments or {})},
    )
    payload = store.load_json("profile/sample.json")
    assert isinstance(payload, dict)
    return store, payload


def _replace_profile(store: LocalArtifactStore, payload: dict[str, Any]) -> None:
    store.save_json(
        "profile/sample.json",
        payload,
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )


def _replace_schema(
    store: LocalArtifactStore, payload: dict[str, Any], schema: dict[str, Any]
) -> None:
    artifact = store.save_json(
        "datasets/sample.schema.json",
        schema,
        produced_by=new_id("agent"),
        kind=ArtifactKind.OTHER,
    )
    payload["source"]["schema_sha256"] = artifact.sha256
    _replace_profile(store, payload)


def _outcome(store: LocalArtifactStore) -> tuple[ControlStatus, str]:
    return check_report_fidelity(store, ())


@pytest.mark.parametrize(
    ("family", "expected_path"),
    [
        ("shape", "shape.rows"),
        ("null", "null_count"),
        ("unique", "unique_count"),
        ("top", "top_values"),
        ("histogram", "histogram.counts"),
        ("correlation", "correlation/left/1"),
    ],
)
def test_profile_verifier_rejects_each_false_claim_family(
    tmp_path: Path, family: str, expected_path: str
) -> None:
    frame = pl.DataFrame({"left": [1.0, 2.0, None, 4.0], "right": [2.0, 4.0, 6.0, 8.0]})
    store, payload = _profile(tmp_path, frame)
    if family == "shape":
        payload["shape"]["rows"] = 5
    elif family == "null":
        payload["columns"]["left"]["null_count"] = 0
    elif family == "unique":
        payload["columns"]["left"]["unique_count"] = 2
    elif family == "top":
        payload["columns"]["left"]["top_values"][0]["count"] = 99
    elif family == "histogram":
        payload["columns"]["left"]["histogram"]["counts"][0] += 1
    else:
        payload["correlation"]["left"][1] = 1.0
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert expected_path in detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_version", True),
        ("profile_version", 2),
        ("source.max_rows", False),
        ("source.max_rows", 0),
        ("source.max_rows", 1_000_001),
        ("source.columns", ["left", "left"]),
        ("source.columns", ["missing"]),
    ],
)
def test_profile_verifier_rejects_malformed_version_and_scope(
    tmp_path: Path, field: str, value: object
) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"left": [1], "right": [2]}))
    if field == "profile_version":
        payload[field] = value
    else:
        payload["source"][field.removeprefix("source.")] = value
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert field in detail


@pytest.mark.parametrize("section", ["profile", "source", "schema"])
def test_profile_verifier_requires_exact_declaration_and_schema_keys(
    tmp_path: Path, section: str
) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    if section == "profile":
        payload["unexpected"] = "claim"
        _replace_profile(store, payload)
    elif section == "source":
        payload["source"]["unexpected"] = "claim"
        _replace_profile(store, payload)
    else:
        schema = store.load_json("datasets/sample.schema.json")
        schema["unexpected"] = "claim"
        schema_artifact = store.save_json(
            "datasets/sample.schema.json",
            schema,
            produced_by=new_id("agent"),
            kind=ArtifactKind.OTHER,
        )
        payload["source"]["schema_sha256"] = schema_artifact.sha256
        _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "exact keys required" in detail


def test_profile_verifier_fails_a_legacy_profile_without_source_evidence(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_json(
        "profile/legacy.json",
        {"dataset": "legacy", "shape": {"rows": 0, "columns": 0}},
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "profile/legacy.json" in detail
    assert "exact keys required" in detail


def test_profile_with_unverifiable_ordinary_report_is_not_labelled_passed(tmp_path: Path) -> None:
    store, _ = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    store.save_text(
        "analysis.md",
        "Accuracy was 99.9%.",
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.NOT_APPLICABLE
    assert "ordinary reports" in detail
    assert "profile/sample.json" in detail


def test_profile_verifier_reports_deep_json_as_a_finding(tmp_path: Path) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    nested = '{"x":' + "[" * 20_000 + "0" + "]" * 20_000 + "}"
    store.save_text(
        "profile/bad.json",
        nested,
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "valid UTF-8 JSON" in detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"profile_version":1,"profile_version":1}', "duplicate JSON key"),
        ("[]", "must be a JSON object"),
    ],
)
def test_profile_verifier_rejects_ambiguous_or_non_object_json(
    tmp_path: Path, text: str, expected: str
) -> None:
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_text(
        "profile/bad.json",
        text,
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert expected in detail


@pytest.mark.parametrize("artifact_name", ["datasets/sample.csv", "datasets/sample.schema.json"])
@pytest.mark.parametrize("unavailable", ["missing", "inactive"])
def test_profile_verifier_requires_active_registered_source_artifacts(
    tmp_path: Path, artifact_name: str, unavailable: str
) -> None:
    store, _ = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    if unavailable == "missing":
        artifact = store.get(artifact_name)
        assert artifact is not None
        (tmp_path / "artifacts" / artifact.uri).unlink()
    else:
        store.invalidate((artifact_name,), "test invalidation")

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "active artifact" in detail
    assert artifact_name in detail


@pytest.mark.parametrize("digest_field", ["sha256", "schema_sha256"])
def test_profile_verifier_rejects_declared_digest_mismatch(
    tmp_path: Path, digest_field: str
) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    payload["source"][digest_field] = "0" * 64
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert f"source.{digest_field}" in detail


@pytest.mark.parametrize(
    ("schema_field", "wrong_value"),
    [
        ("columns", ["renamed"]),
        ("dtypes", ["Float64"]),
        ("row_count", 3),
        ("byte_size", 999),
        ("sha256", "0" * 64),
    ],
)
def test_profile_verifier_recomputes_captured_schema_facts_from_dataset_bytes(
    tmp_path: Path, schema_field: str, wrong_value: object
) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    schema = store.load_json("datasets/sample.schema.json")
    schema[schema_field] = wrong_value
    _replace_schema(store, payload, schema)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert f"schema.{schema_field}" in detail


@pytest.mark.parametrize("wrong_value", ["", 17])
def test_profile_verifier_rejects_malformed_schema_source_path(
    tmp_path: Path, wrong_value: object
) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    schema = store.load_json("datasets/sample.schema.json")
    schema["source_path"] = wrong_value
    _replace_schema(store, payload, schema)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "schema.source_path" in detail


def test_profile_verifier_rejects_changed_dataset_bytes(tmp_path: Path) -> None:
    store, _ = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    artifact = store.get("datasets/sample.csv")
    assert artifact is not None
    (tmp_path / "artifacts" / artifact.uri).write_text(
        "value\n1\n3\n", encoding="utf-8", newline="\n"
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "sha256 verification" in detail
    assert "datasets/sample.csv" in detail


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_profile_verifier_honours_selected_columns_and_row_cap(tmp_path: Path, suffix: str) -> None:
    store, _ = _profile(
        tmp_path,
        pl.DataFrame({"first": [1, 2, 3], "second": [4, 5, 6], "label": ["a", "b", "c"]}),
        suffix=suffix,
        arguments={"columns": ["second", "first"], "max_rows": 2},
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.PASSED, detail


def test_profile_verifier_accepts_any_valid_top_twenty_cutoff_tie(tmp_path: Path) -> None:
    values = ["required"] * 3
    values.extend(value for index in range(21) for value in [f"tie-{index}"] * 2)
    store, payload = _profile(tmp_path, pl.DataFrame({"label": values}))
    claimed = payload["columns"]["label"]["top_values"]
    claimed_values = {entry["label"] for entry in claimed}
    omitted = next(f"tie-{index}" for index in range(21) if f"tie-{index}" not in claimed_values)
    replacement = next(entry for entry in reversed(claimed) if entry["label"] != "required")
    replacement["label"] = omitted
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.PASSED, detail


def test_profile_verifier_handles_empty_constant_and_nonfinite_numeric_values(
    tmp_path: Path,
) -> None:
    frame = pl.DataFrame(
        {
            "constant": pl.Series([3.0, 3.0, None, float("nan")]),
            "nonfinite": pl.Series([float("inf"), float("-inf"), None, float("nan")]),
        }
    )
    store, _ = _profile(tmp_path, frame, suffix=".parquet")

    status, detail = _outcome(store)

    assert status is ControlStatus.PASSED, detail


def test_profile_verifier_handles_an_empty_numeric_parquet_dataset(tmp_path: Path) -> None:
    frame = pl.DataFrame(schema={"left": pl.Float64, "right": pl.Int64, "label": pl.String})
    store, _ = _profile(tmp_path, frame, suffix=".parquet")

    status, detail = _outcome(store)

    assert status is ControlStatus.PASSED, detail


@pytest.mark.parametrize("scale", [1e-100, 1e80])
def test_profile_verifier_computes_scale_safe_pearson_correlation(
    tmp_path: Path, scale: float
) -> None:
    store, _ = _profile(
        tmp_path,
        pl.DataFrame({"x": [scale, 2 * scale, 3 * scale], "y": [2 * scale, 4 * scale, 6 * scale]}),
        suffix=".parquet",
    )

    status, detail = _outcome(store)

    assert status is ControlStatus.PASSED, detail


def test_profile_verifier_uses_scale_sensitive_histogram_tolerance(tmp_path: Path) -> None:
    store, payload = _profile(
        tmp_path,
        pl.DataFrame({"x": [1e-100, 2e-100, 3e-100]}),
        suffix=".parquet",
    )
    payload["columns"]["x"]["histogram"]["edges"] = [0.0] * 11
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "histogram.edges" in detail


def test_profile_verifier_rejects_boolean_counts(tmp_path: Path) -> None:
    store, payload = _profile(tmp_path, pl.DataFrame({"value": [1, 2]}))
    payload["shape"]["rows"] = True
    _replace_profile(store, payload)

    status, detail = _outcome(store)

    assert status is ControlStatus.FAILED
    assert "shape.rows" in detail
