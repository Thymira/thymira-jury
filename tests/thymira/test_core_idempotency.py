"""Tests for core execution idempotency and artifact lineage invalidation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.core import (
    DuplicateExecutionError,
    IdempotencyJournal,
    cascade_invalidation,
    compute_execution_key,
)
from thymira.schemas import new_id
from thymira.state import LocalArtifactStore

if TYPE_CHECKING:
    from pathlib import Path


def _execution_key(
    *,
    tool: str = "profile_dataset",
    input_fingerprints: tuple[str, ...] = ("sha256:input",),
    parameters: dict[str, float | str] | None = None,
    phase_revision: str = "profile-v1",
    decision_fingerprint: str = "sha256:decision",
) -> str:
    """Return a representative execution key with optional changed components."""
    return compute_execution_key(
        tool=tool,
        input_fingerprints=input_fingerprints,
        parameters=parameters or {"target": "risk", "threshold": 0.5},
        phase_revision=phase_revision,
        decision_fingerprint=decision_fingerprint,
    )


def test_execution_key_is_stable_for_equal_inputs() -> None:
    """Equal execution inputs produce the same digest regardless of parameter order."""
    first = _execution_key()
    second = _execution_key(parameters={"threshold": 0.5, "target": "risk"})

    assert first == second


@pytest.mark.parametrize("changed_component", ["tool", "input", "parameters", "phase", "decision"])
def test_execution_key_changes_when_an_input_changes(changed_component: str) -> None:
    """Changing any execution-key component produces a different digest."""
    if changed_component == "tool":
        changed = _execution_key(tool="analyze_dataset")
    elif changed_component == "input":
        changed = _execution_key(input_fingerprints=("sha256:other-input",))
    elif changed_component == "parameters":
        changed = _execution_key(parameters={"target": "income", "threshold": 0.5})
    elif changed_component == "phase":
        changed = _execution_key(phase_revision="profile-v2")
    else:
        changed = _execution_key(decision_fingerprint="sha256:other-decision")

    assert changed != _execution_key()


def test_idempotency_journal_rejects_a_duplicate_execution() -> None:
    """A completed execution cannot be registered twice."""
    journal = IdempotencyJournal()
    journal.record("execution-key", [new_id("artifact")])

    assert journal.seen("execution-key") is True
    with pytest.raises(DuplicateExecutionError, match="already recorded"):
        journal.record("execution-key", [])


def test_cascade_invalidation_marks_all_transitive_descendants(tmp_path: Path) -> None:
    """Changing one input invalidates its stored descendants and preserves the reason."""
    store = LocalArtifactStore(tmp_path / "artifacts", run_id=new_id("run"))
    producer = new_id("agent")
    store.save_text("dataset.csv", "v1", produced_by=producer)
    features = store.save_json("features.json", {"rows": 1}, produced_by=producer)
    metrics = store.save_json("metrics.json", {"auc": 0.9}, produced_by=producer)
    store.save_text("report.md", "report", produced_by=producer)
    dataset = store.get("dataset.csv")
    assert dataset is not None
    store.set_lineage("features.json", input_artifact_ids=[dataset.id], execution_key="key-1")
    store.set_lineage("metrics.json", input_artifact_ids=[features.id], execution_key="key-2")
    store.set_lineage("report.md", input_artifact_ids=[metrics.id], execution_key="key-3")

    invalidated = cascade_invalidation(
        store,
        changed_names=["dataset.csv"],
        downstream_graph={
            "dataset.csv": ("features.json",),
            "features.json": ("metrics.json",),
            "metrics.json": ("report.md",),
        },
        reason="dataset changed",
    )

    assert invalidated == ["features.json", "metrics.json", "report.md"]
    assert store.exists("dataset.csv") is True
    for name in invalidated:
        artifact = store.get(name)
        assert artifact is not None
        assert artifact.valid is False
        assert artifact.invalidated_reason == "dataset changed"
