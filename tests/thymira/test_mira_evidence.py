"""Unit tests for MIRA's Run-scoped evidence projection and reader."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thymira.events import InMemoryEventLog
from thymira.mira.evidence import (
    ArtifactStoreEvidenceReader,
    EvidenceContentError,
    EvidenceIntegrityError,
    EvidenceLimitError,
    EvidenceReadError,
    EvidenceReferenceError,
    build_evidence_observations,
)
from thymira.schemas import Actor, ArtifactKind, EventType, Evidence, new_id
from thymira.state import LocalArtifactStore

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _store_and_artifact(tmp_path, *, run_id: str, name: str = "reports/validation.txt"):
    """Create one local store with one text artifact."""
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    artifact = store.save_text(
        name,
        "Validation report for alice@example.com.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
    )
    return store, artifact


def test_build_evidence_observations_projects_events_artifacts_and_tool_results(tmp_path) -> None:
    """The post-THY projection contains verifiable event, manifest, and tool evidence."""
    run_id = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=run_id)
    log = InMemoryEventLog(run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {})
    log.append(
        EventType.ARTIFACT_CREATED,
        Actor.system(),
        {"artifact_id": artifact.id, "name": artifact.name, "sha256": artifact.sha256},
        subject_id=artifact.id,
    )
    log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {
            "tool_call_id": new_id("tool"),
            "tool": "profile_dataset",
            "status": "COMPLETED",
            "result_sha256": "a" * 64,
        },
    )

    observations = build_evidence_observations(run_id, log.events(), store)

    assert [observation.evidence.kind for observation in observations] == [
        "event",
        "event",
        "event",
        "artifact",
        "tool_call",
    ]
    artifact_observation = observations[3]
    assert artifact_observation.evidence.ref == artifact.name
    assert artifact_observation.evidence.sha256 == artifact.sha256
    assert artifact_observation.evidence.note == (
        f"id={artifact.id}; kind=report; size_bytes={artifact.size_bytes}; "
        f"produced_by={artifact.produced_by}"
    )
    assert artifact_observation.integrity_verified is True


def test_evidence_reader_allows_a_hash_pinned_artifact_of_the_current_run(tmp_path) -> None:
    """A registered current-Run artifact is readable only as a redacted excerpt."""
    run_id = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=run_id)
    evidence = Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256)

    excerpt = ArtifactStoreEvidenceReader(store, run_id).read_excerpt(evidence, max_chars=200)

    assert excerpt.actual_sha256 == artifact.sha256
    assert "alice@example.com" not in excerpt.text
    assert "[REDACTED:EMAIL]" in excerpt.text


def test_evidence_reader_rejects_foreign_and_traversal_references(tmp_path) -> None:
    """The reader cannot cross Run or filesystem boundaries."""
    foreign_run = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=foreign_run)
    reader = ArtifactStoreEvidenceReader(store, new_id("run"))

    with pytest.raises(EvidenceReferenceError, match="another Run"):
        reader.read_excerpt(
            Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256), max_chars=100
        )
    with pytest.raises(EvidenceReferenceError, match="safe store path"):
        reader.read_excerpt(
            Evidence(kind="artifact", ref="../secret.txt", sha256="a" * 64), max_chars=100
        )


def test_evidence_reader_enforces_byte_character_and_excerpt_count_limits(tmp_path) -> None:
    """The reader bounds content before exposure and refuses a fourth read."""
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    artifact = store.save_text(
        "large.txt",
        "x" * 20,
        produced_by=new_id("tool"),
    )
    evidence = Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256)
    reader = ArtifactStoreEvidenceReader(store, run_id, max_bytes=10, max_excerpts=1)

    with pytest.raises(EvidenceLimitError, match="bytes"):
        reader.read_excerpt(evidence, max_chars=100)

    small = store.save_text("small.txt", "0123456789", produced_by=new_id("tool"))
    small_evidence = Evidence(kind="artifact", ref=small.name, sha256=small.sha256)
    bounded_reader = ArtifactStoreEvidenceReader(store, run_id, max_bytes=20, max_excerpts=1)
    first = bounded_reader.read_excerpt(small_evidence, max_chars=4)
    assert first.text == "0123"
    assert first.truncated is True
    with pytest.raises(EvidenceLimitError, match="excerpt limit"):
        bounded_reader.read_excerpt(small_evidence, max_chars=4)


def test_evidence_reader_rejects_hash_mismatch_and_binary_content(tmp_path) -> None:
    """The reader never returns bytes whose identity or text representation is unverified."""
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    binary = store.save_bytes("model.bin", b"\x00\xff", produced_by=new_id("tool"))
    reader = ArtifactStoreEvidenceReader(store, run_id)

    with pytest.raises(EvidenceIntegrityError, match="does not match"):
        reader.read_excerpt(
            Evidence(kind="artifact", ref=binary.name, sha256="b" * 64), max_chars=100
        )
    with pytest.raises(EvidenceContentError, match="not UTF-8"):
        reader.read_excerpt(
            Evidence(kind="artifact", ref=binary.name, sha256=binary.sha256), max_chars=100
        )


def test_evidence_reader_rejects_non_positive_limits(tmp_path) -> None:
    """A reader with no bound is not a bounded reader."""
    run_id = new_id("run")
    store, _artifact = _store_and_artifact(tmp_path, run_id=run_id)

    with pytest.raises(ValueError, match="limits must be positive"):
        ArtifactStoreEvidenceReader(store, run_id, max_bytes=0)
    with pytest.raises(ValueError, match="limits must be positive"):
        ArtifactStoreEvidenceReader(store, run_id, max_excerpts=0)


def test_evidence_reader_rejects_an_artifact_owned_by_another_run(tmp_path) -> None:
    """A reader is Run-scoped: another Run's artifact is never readable through it."""
    owner = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=owner)
    reader = ArtifactStoreEvidenceReader(store, new_id("run"))
    evidence = Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256)

    with pytest.raises(EvidenceReferenceError, match="belongs to another Run"):
        reader.read_excerpt(evidence, max_chars=100)


def test_evidence_reader_rejects_an_invalidated_artifact(tmp_path) -> None:
    """A superseded artifact is no longer evidence for the current audit."""
    run_id = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=run_id)
    store.save_text(
        artifact.name,
        "Superseding revision.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
    )
    reader = ArtifactStoreEvidenceReader(store, run_id)
    evidence = Evidence(kind="artifact", ref=artifact.id, sha256=artifact.sha256)

    with pytest.raises(EvidenceReferenceError, match="invalidated"):
        reader.read_excerpt(evidence, max_chars=100)


def test_evidence_reader_rejects_an_artifact_larger_than_its_byte_bound(tmp_path) -> None:
    """The manifest size is checked before any content is loaded."""
    run_id = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=run_id)
    reader = ArtifactStoreEvidenceReader(store, run_id, max_bytes=4)
    evidence = Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256)

    with pytest.raises(EvidenceLimitError, match="maximum is 4"):
        reader.read_excerpt(evidence, max_chars=100)


def test_evidence_reader_reports_an_artifact_whose_bytes_are_gone(tmp_path) -> None:
    """A manifest entry whose file cannot be read is a read failure, not empty evidence."""
    run_id = new_id("run")
    store, artifact = _store_and_artifact(tmp_path, run_id=run_id)
    (tmp_path / "artifacts" / artifact.uri).unlink()
    reader = ArtifactStoreEvidenceReader(store, run_id)
    evidence = Evidence(kind="artifact", ref=artifact.name, sha256=artifact.sha256)

    with pytest.raises(EvidenceReadError, match="could not read artifact"):
        reader.read_excerpt(evidence, max_chars=100)


def test_evidence_reader_reports_an_unregistered_reference(tmp_path) -> None:
    """A name the manifest does not hold is not evidence."""
    run_id = new_id("run")
    store, _artifact = _store_and_artifact(tmp_path, run_id=run_id)
    reader = ArtifactStoreEvidenceReader(store, run_id)
    evidence = Evidence(kind="artifact", ref="reports/absent.txt", sha256="a" * 64)

    with pytest.raises(EvidenceReferenceError, match="is not registered"):
        reader.read_excerpt(evidence, max_chars=100)
