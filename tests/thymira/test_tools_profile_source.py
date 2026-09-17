"""Source declarations emitted by the bounded dataset profiler."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.builtins.data_analysis import ProfileDataset
from thymira.tools.datasets import register_dataset, schema_artifact_name
from thymira.tools.models import ToolInvocation

if TYPE_CHECKING:
    from pathlib import Path


def _invocation(tmp_path: Path) -> ToolInvocation:
    """Build an isolated tool invocation backed by a local artifact store."""
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def _register_dataset(invocation: ToolInvocation, tmp_path: Path) -> None:
    """Register a compact dataset with a deliberate non-alphabetical column order."""
    source = tmp_path / "sample.csv"
    source.write_text(
        "first,second,label\n1,4,a\n2,7,b\n4,8,a\n",
        encoding="utf-8",
        newline="\n",
    )
    register_dataset(
        invocation.artifact_store,
        source,
        "sample",
        produced_by=invocation.agent_id,
    )


def test_profile_dataset_declares_registered_dataset_and_schema_hashes(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)

    result = ProfileDataset().execute(
        invocation,
        {"dataset": "sample", "description": "Profile the sample dataset"},
    )
    payload = json.loads(result.stdout)
    dataset = invocation.artifact_store.get("datasets/sample.csv")
    schema = invocation.artifact_store.get(schema_artifact_name("sample"))

    assert dataset is not None
    assert schema is not None
    assert dataset.uri.startswith(".batches/")
    assert schema.uri.startswith(".batches/")
    assert payload["profile_version"] == 1
    assert payload["source"] == {
        "artifact": "datasets/sample.csv",
        "sha256": dataset.sha256,
        "schema_sha256": schema.sha256,
        "max_rows": 100_000,
        "columns": None,
    }
    report = invocation.artifact_store.get("profile/sample.json")
    assert report is not None
    assert report.kind is ArtifactKind.REPORT


def test_profile_dataset_declares_effective_selected_row_and_column_scope(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)

    result = ProfileDataset().execute(
        invocation,
        {
            "dataset": "sample",
            "columns": ["second", "first"],
            "max_rows": 2,
            "description": "Profile selected sample columns",
        },
    )
    payload = json.loads(result.stdout)

    assert payload["source"]["max_rows"] == 2
    assert payload["source"]["columns"] == ["second", "first"]
    assert payload["shape"] == {"rows": 2, "columns": 2}
    assert set(payload["columns"]) == {"second", "first"}


def test_profile_dataset_refuses_an_unregistered_dataset_source(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)

    with pytest.raises(ValueError, match="active dataset schema artifact is required"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "missing", "description": "Profile the missing dataset"},
        )


@pytest.mark.parametrize("artifact_name", ["datasets/sample.schema.json", "datasets/sample.csv"])
def test_profile_dataset_refuses_an_inactive_registered_source(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    invocation.artifact_store.invalidate((artifact_name,), "test invalidation")

    with pytest.raises(ValueError, match=r"active dataset (schema )?artifact is required"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the invalid sample source"},
        )


def test_profile_dataset_refuses_schema_bytes_that_differ_from_its_manifest(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    schema_name = schema_artifact_name("sample")
    artifact = invocation.artifact_store.get(schema_name)
    assert artifact is not None
    (tmp_path / "artifacts" / artifact.uri).write_bytes(b"{}")

    with pytest.raises(ValueError, match="schema artifact has changed"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the sample dataset"},
        )


def test_profile_dataset_refuses_dataset_bytes_that_differ_from_its_manifest(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    artifact = invocation.artifact_store.get("datasets/sample.csv")
    assert artifact is not None
    (tmp_path / "artifacts" / artifact.uri).write_bytes(b"changed dataset bytes")

    with pytest.raises(ValueError, match="dataset artifact has changed"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the changed sample dataset"},
        )


def test_profile_dataset_refuses_a_schema_for_a_different_dataset_name(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    schema_name = schema_artifact_name("sample")
    schema = invocation.artifact_store.load_json(schema_name)
    schema["name"] = "other"
    invocation.artifact_store.save_json(
        schema_name,
        schema,
        produced_by=invocation.agent_id,
        kind=ArtifactKind.OTHER,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="schema name does not match"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the renamed sample dataset"},
        )


def test_profile_dataset_refuses_a_schema_whose_digest_does_not_link_the_source(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    schema_name = schema_artifact_name("sample")
    schema = invocation.artifact_store.load_json(schema_name)
    schema["sha256"] = "0" * 64
    invocation.artifact_store.save_json(
        schema_name,
        schema,
        produced_by=invocation.agent_id,
        kind=ArtifactKind.OTHER,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="dataset artifact has changed"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the inconsistent sample dataset"},
        )


def test_profile_dataset_refuses_a_registered_source_that_is_not_a_dataset(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_dataset(invocation, tmp_path)
    source = invocation.artifact_store.load_bytes("datasets/sample.csv")
    invocation.artifact_store.save_bytes(
        "datasets/sample.csv",
        source,
        produced_by=invocation.agent_id,
        kind=ArtifactKind.OTHER,
        media_type="text/csv",
    )

    with pytest.raises(ValueError, match="source is not a dataset artifact"):
        ProfileDataset().execute(
            invocation,
            {"dataset": "sample", "description": "Profile the non dataset source"},
        )
