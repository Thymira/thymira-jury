"""Independent A18 verification of dataset profile reports through the real tool boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import polars as pl
import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools.builtins.data_analysis import ProfileDataset
from thymira.tools.datasets import register_dataset
from thymira.tools.models import ToolInvocation

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.mira.checks import ControlResult


def _profile_store(
    tmp_path: Path, *, data: str = "x,y,label\n1,4,a\n2,7,b\n4,8,a\n8,12,b\n"
) -> LocalArtifactStore:
    run_id = new_id("run")
    agent_id = new_id("agent")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    source = tmp_path / "sample.csv"
    source.write_text(data, encoding="utf-8", newline="\n")
    register_dataset(store, source, "sample", produced_by=agent_id)
    invocation = ToolInvocation(run_id, agent_id, tmp_path, store)
    ProfileDataset().execute(
        invocation, {"dataset": "sample", "description": "Profile the registered sample dataset"}
    )
    return store


def _a18(store: LocalArtifactStore) -> ControlResult:
    run_id = store.list_active()[0].run_id
    log = InMemoryEventLog(run_id)
    report = audit_run(AuditContext(run_id, log.events(), store))
    return next(control for control in report.controls if control.control_id == "A18")


@pytest.mark.parametrize("with_metrics", [False, True])
def test_a18_recomputes_profile_facts_from_the_registered_dataset(
    tmp_path: Path, *, with_metrics: bool
) -> None:
    store = _profile_store(tmp_path)
    if with_metrics:
        store.save_json(
            "metrics/baseline.json",
            {"accuracy": 0.83},
            produced_by=new_id("agent"),
            kind=ArtifactKind.METRICS,
        )

    a18 = _a18(store)

    assert a18.status is ControlStatus.PASSED, a18.detail
    assert store.verify() == []


def test_a18_records_the_verified_profile_and_source_digests(tmp_path: Path) -> None:
    store = _profile_store(tmp_path)

    a18 = _a18(store)

    assert {item.ref: item.sha256 for item in a18.evidence} == {
        name: artifact.sha256
        for name, artifact in store.manifest().items()
        if name in {"profile/sample.json", "datasets/sample.csv", "datasets/sample.schema.json"}
    }


def test_a18_does_not_use_verified_profile_numbers_to_support_unrelated_report_claims(
    tmp_path: Path,
) -> None:
    store = _profile_store(tmp_path)
    store.save_json(
        "metrics/baseline.json",
        {"accuracy": 0.83},
        produced_by=new_id("agent"),
        kind=ArtifactKind.METRICS,
    )
    store.save_text(
        "report.md",
        "An experiment achieved accuracy 1.0.",
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )

    a18 = _a18(store)

    assert a18.status is ControlStatus.FAILED
    assert "1.0" in a18.detail


def test_a18_verification_leaves_the_artifact_store_unchanged(tmp_path: Path) -> None:
    store = _profile_store(tmp_path)
    before = store.manifest()

    _a18(store)

    assert store.manifest() == before
    assert store.verify() == []


@pytest.mark.parametrize("suffix", ["csv", "parquet"])
def test_a18_recomputes_capped_columns_in_source_order(tmp_path: Path, suffix: str) -> None:
    run_id = new_id("run")
    agent_id = new_id("agent")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    frame = pl.DataFrame(
        {
            "z": [index % 17 for index in range(20_000)],
            "x": [index * 2 for index in range(20_000)],
            "b": [index % 3 for index in range(20_000)],
        }
    )
    source = tmp_path / f"ordered.{suffix}"
    if suffix == "csv":
        frame.write_csv(source)
    else:
        frame.write_parquet(source)
    register_dataset(store, source, "ordered", produced_by=agent_id)
    ProfileDataset().execute(
        ToolInvocation(run_id, agent_id, tmp_path, store),
        {
            "dataset": "ordered",
            "max_rows": 4,
            "columns": ["b", "z", "x"],
            "description": "Profile the registered sample dataset",
        },
    )

    a18 = _a18(store)

    assert a18.status is ControlStatus.PASSED, a18.detail
    assert store.load_json("profile/ordered.json")["shape"] == {"rows": 4, "columns": 3}


@pytest.mark.parametrize("mutation", ["duplicate", "omit_high_frequency"])
def test_a18_rejects_misleading_top_frequency_membership(tmp_path: Path, mutation: str) -> None:
    values = ["required"] * 3 + [f"tie-{index}" for index in range(21) for _ in range(2)]
    store = _profile_store(tmp_path, data="label\n" + "\n".join([*values, "rare"]) + "\n")
    payload = store.load_json("profile/sample.json")
    top_values = payload["columns"]["label"]["top_values"]
    if mutation == "duplicate":
        top_values[-1] = dict(top_values[1])
    else:
        top_values[0] = {"label": "rare", "count": 1}
        top_values.sort(key=lambda entry: entry["count"], reverse=True)
    store.save_json(
        "profile/sample.json", payload, produced_by=new_id("agent"), kind=ArtifactKind.REPORT
    )

    a18 = _a18(store)

    assert a18.status is ControlStatus.FAILED
    assert "top_values" in a18.detail
