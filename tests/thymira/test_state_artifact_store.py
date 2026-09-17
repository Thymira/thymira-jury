"""LocalArtifactStore: content-addressed saves, history, lineage, invalidation and verify."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from thymira.schemas import Artifact, ArtifactKind, new_id
from thymira.state import ArtifactStore, ArtifactWrite, LocalArtifactStore

if TYPE_CHECKING:
    from pathlib import Path


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts", run_id=new_id("run"))


def test_save_bytes_returns_artifact_with_hash_size_kind_and_producer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    data = b"\x00\x01payload\xff"

    artifact = store.save_bytes(
        "model.bin",
        data,
        produced_by=producer,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )

    assert isinstance(artifact, Artifact)
    assert artifact.name == "model.bin"
    assert artifact.kind is ArtifactKind.MODEL
    assert artifact.produced_by == producer
    assert artifact.media_type == "application/octet-stream"
    assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert artifact.size_bytes == len(data)
    assert artifact.valid is True
    assert store.load_bytes("model.bin") == data


def test_heterogeneous_batch_records_metadata_and_exact_same_batch_lineage(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = b"# Auditable source\n"
    rendered = b"%PDF-1.7\nrendered bytes"

    artifacts = store.save_artifact_batch(
        (
            ArtifactWrite(
                name="reports/final.source.md",
                data=source,
                kind=ArtifactKind.REPORT,
                media_type="text/markdown",
            ),
            ArtifactWrite(
                name="reports/final.pdf",
                data=rendered,
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
                input_artifact_names=("reports/final.source.md",),
                execution_key="render:source-sha256",
            ),
        ),
        produced_by=new_id("tool"),
    )

    source_artifact, pdf_artifact = artifacts
    assert source_artifact.media_type == "text/markdown"
    assert pdf_artifact.media_type == "application/pdf"
    assert pdf_artifact.input_artifact_ids == (source_artifact.id,)
    assert pdf_artifact.execution_key == "render:source-sha256"
    assert store.load_bytes(source_artifact.name) == source
    assert store.load_bytes(pdf_artifact.name) == rendered
    assert store.verify() == []


def test_save_text_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    text = "report line\nsecond line\n"

    artifact = store.save_text("report.md", text, produced_by=new_id("agent"))

    assert artifact.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert artifact.size_bytes == len(text.encode("utf-8"))
    assert store.load_text("report.md") == text


def test_save_json_uses_canonical_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}

    artifact = store.save_json("metrics.json", data, produced_by=new_id("tool"))

    assert artifact.sha256 == hashlib.sha256(_canonical(data)).hexdigest()
    assert artifact.kind is ArtifactKind.OTHER
    assert store.load_json("metrics.json") == data
    # Canonical: keys are sorted with no whitespace, independent of input order.
    assert store.load_bytes("metrics.json") == _canonical(data)


def test_manifest_persisted_and_reloaded_by_a_second_instance(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    run_id = new_id("run")
    first = LocalArtifactStore(root, run_id=run_id)
    saved = first.save_json("metrics.json", {"auc": 0.9}, produced_by=new_id("agent"))

    assert (root / "manifest.json").exists()

    second = LocalArtifactStore(root, run_id=run_id)
    reloaded = second.get("metrics.json")
    assert reloaded is not None
    assert reloaded == saved
    assert "metrics.json" in second.manifest()
    assert second.load_json("metrics.json") == {"auc": 0.9}


def test_preserve_history_archives_old_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    store.save_text("metrics.json", "old", produced_by=producer)
    old_sha = hashlib.sha256(b"old").hexdigest()

    store.save_text("metrics.json", "new", produced_by=producer)

    # The active name points at the new revision.
    assert store.exists("metrics.json") is True
    assert store.load_text("metrics.json") == "new"

    # The old revision survives on disk under .history with a valid=False manifest entry.
    history_dir = tmp_path / "artifacts" / ".history"
    assert history_dir.exists()
    archived_files = [p for p in history_dir.rglob("*") if p.is_file()]
    assert len(archived_files) == 1
    assert archived_files[0].read_text(encoding="utf-8") == "old"

    archived = [a for a in store.manifest().values() if not a.valid]
    assert len(archived) == 1
    entry = archived[0]
    assert entry.sha256 == old_sha
    assert entry.invalidated_reason == "superseded by a new revision of metrics.json"
    assert entry.invalidated_at is not None
    # verify() stays clean: the archived file matches its recorded hash.
    assert store.verify() == []


@pytest.mark.parametrize(
    "name",
    ["../escaped.json", "   ", "NUL", "metrics.json.", "C:\\secrets\\x.json", "a/../../b.json"],
    ids=["escaping", "blank", "device", "trailing-dot", "absolute", "traversal"],
)
def test_a_query_answers_no_for_a_name_the_store_would_refuse_to_write(
    tmp_path: Path, name: str
) -> None:
    store = _store(tmp_path)
    store.save_text("metrics.json", "v1", produced_by=new_id("agent"))

    # A query has a correct answer for an impossible name: nothing is stored under it. Raising
    # instead means any caller holding untrusted input — MIRA reading an `artifact.created`
    # payload, above all — crashes where it should have reported. The write paths still refuse
    # loudly, which is where a caller asking to escape the root is a bug worth surfacing.
    assert store.exists(name) is False
    assert store.get(name) is None


def test_the_archived_key_of_a_dotted_name_stays_readable(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    run_id = new_id("run")
    producer = new_id("agent")
    store = LocalArtifactStore(root, run_id=run_id)
    store.save_text("x..json", "old", produced_by=producer)

    store.save_text("x..json", "new", produced_by=producer)

    # `_next_archive_name` builds the .history key from the raw stem, so a stem ending in a dot
    # mints a key `_store_key` refuses. It is written to manifest.json all the same, and from
    # then on *every* open of the store raises — not that one artifact, the whole run's evidence.
    reopened = LocalArtifactStore(root, run_id=run_id)
    assert reopened.load_text("x..json") == "new"
    assert reopened.verify() == []


def test_preserve_history_false_overwrites_without_archiving(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    store.save_text("metrics.json", "old", produced_by=producer)
    store.save_text("metrics.json", "new", produced_by=producer, preserve_history=False)

    assert store.load_text("metrics.json") == "new"
    assert not (tmp_path / "artifacts" / ".history").exists()
    assert all(a.valid for a in store.manifest().values())


def test_set_lineage_stores_inputs_and_execution_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_json("model_selection.json", {"model": "rf"}, produced_by=new_id("agent"))
    inputs = [new_id("artifact"), new_id("artifact")]

    updated = store.set_lineage(
        "model_selection.json",
        input_artifact_ids=inputs,
        execution_key="exec-123",
    )

    assert updated.input_artifact_ids == tuple(inputs)
    assert updated.execution_key == "exec-123"
    # Persisted across a reload.
    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=updated.run_id)
    entry = reopened.get("model_selection.json")
    assert entry is not None
    assert entry.input_artifact_ids == tuple(inputs)
    assert entry.execution_key == "exec-123"


def test_set_lineage_on_unknown_name_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        store.set_lineage("missing.json", input_artifact_ids=[], execution_key="k")


def test_invalidate_returns_changed_names_and_flips_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    store.save_json("a.json", {"v": 1}, produced_by=producer)
    store.save_json("b.json", {"v": 2}, produced_by=producer)

    changed = store.invalidate(["b.json", "a.json"], "input changed")

    assert changed == ["a.json", "b.json"]  # sorted
    assert store.exists("a.json") is False
    assert store.exists("b.json") is False
    entry = store.get("a.json")
    assert entry is not None
    assert entry.valid is False
    assert entry.invalidated_reason == "input changed"
    assert entry.invalidated_at is not None
    # A second invalidation is a no-op: nothing still valid to change.
    assert store.invalidate(["a.json", "b.json"], "again") == []


def test_list_active_excludes_invalid_and_archived(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    store.save_text("keep.txt", "v1", produced_by=producer)
    store.save_text("keep.txt", "v2", produced_by=producer)  # archives v1
    store.save_text("drop.txt", "x", produced_by=producer)
    store.invalidate(["drop.txt"], "obsolete")

    active_names = {a.name for a in store.list_active()}
    assert active_names == {"keep.txt"}


def test_verify_is_empty_on_a_clean_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_json("a.json", {"v": 1}, produced_by=new_id("agent"))
    store.save_text("b.txt", "hello", produced_by=new_id("agent"))

    assert store.verify() == []


def test_verify_reports_a_modified_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.save_text("a.txt", "original", produced_by=new_id("agent"))

    (tmp_path / "artifacts" / artifact.uri).write_text("tampered", encoding="utf-8", newline="\n")

    problems = store.verify()
    assert len(problems) == 1
    assert "a.txt" in problems[0]
    assert "modified" in problems[0]


def test_verify_reports_a_missing_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.save_text("a.txt", "original", produced_by=new_id("agent"))

    (tmp_path / "artifacts" / artifact.uri).unlink()

    problems = store.verify()
    assert len(problems) == 1
    assert "a.txt" in problems[0]
    assert "missing" in problems[0]


def test_uri_is_posix_even_for_nested_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.save_json(
        "reports/quarter/metrics.json", {"v": 1}, produced_by=new_id("agent")
    )

    assert artifact.name == "reports/quarter/metrics.json"
    assert artifact.uri.startswith(".batches/")
    assert "\\" not in artifact.uri
    assert store.load_json("reports/quarter/metrics.json") == {"v": 1}


def test_local_store_satisfies_the_protocol(tmp_path: Path) -> None:
    store: ArtifactStore = _store(tmp_path)
    assert isinstance(store, ArtifactStore)


# ---------------------------------------------------------------------------
# containment: a name may never escape the store root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "nested/../../escape.txt",
        "/absolute.txt",
        "C:/windows.txt",
        r"C:\windows.txt",
        r"\\server\share\unc.txt",
        "",
        "   ",
    ],
)
def test_a_name_that_escapes_the_store_root_is_refused(tmp_path: Path, name: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="artifact name"):
        store.save_text(name, "PWNED", produced_by=new_id("agent"))


def test_traversal_never_writes_outside_the_root_and_verify_stays_honest(tmp_path: Path) -> None:
    outside = tmp_path / "SECRET.txt"
    outside.write_text("original", encoding="utf-8")
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="artifact name"):
        store.save_text("../SECRET.txt", "PWNED", produced_by=new_id("agent"))

    # The file outside the store is untouched and nothing was registered.
    assert outside.read_text(encoding="utf-8") == "original"
    assert store.manifest() == {}
    assert store.verify() == []


def test_equivalent_names_resolve_to_one_canonical_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    producer = new_id("agent")
    store.save_text("a.json", "V1", produced_by=producer, kind=ArtifactKind.METRICS)
    store.save_text("./a.json", "V2", produced_by=producer, kind=ArtifactKind.METRICS)

    # One logical artifact, not two colliding manifest keys pointing at one file.
    active = [name for name, artifact in store.manifest().items() if artifact.valid]
    assert active == ["a.json"]
    assert store.load_text("a.json") == "V2"
    # The superseded revision was archived rather than silently overwritten.
    assert any(name.startswith(".history/") for name in store.manifest())
    assert store.verify() == []


def test_a_manifest_pointing_outside_the_root_is_refused_on_reload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_text("report.md", "ok", produced_by=new_id("agent"))
    manifest_path = tmp_path / "artifacts" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = raw.pop("report.md")
    entry["uri"] = "../SECRET.txt"
    raw["../SECRET.txt"] = entry
    manifest_path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=r"artifact name|manifest entry"):
        LocalArtifactStore(tmp_path / "artifacts", run_id=new_id("run"))


@pytest.mark.parametrize(
    "name",
    ["NUL", "nul", "NUL.txt", "con", "AUX.json", "COM1", "lpt9.csv", "reports/nul.md"],
)
def test_a_windows_device_name_is_refused(tmp_path: Path, name: str) -> None:
    """Windows discards a write to a device, and the store would record the empty digest for it.

    The file would read back as ``b""``, so ``sha256_file`` and the manifest agree on the hash of
    nothing: the loss is total and ``verify`` cannot see it. Refused everywhere, so the same run
    produces the same evidence on every platform.
    """
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="reserved device name"):
        store.save_bytes(name, b"IMPORTANT", produced_by=new_id("agent"))


@pytest.mark.parametrize("name", ["a.json.", "a.json ", "reports./x.md", "x/ /y.json"])
def test_a_name_ending_in_a_dot_or_space_is_refused(tmp_path: Path, name: str) -> None:
    """Windows strips them, so two distinct keys would open one file."""
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="dot or a space"):
        store.save_text(name, "v", produced_by=new_id("agent"))


def test_two_names_differing_only_in_case_are_refused(tmp_path: Path) -> None:
    """One file must never carry two manifest keys, whatever the filesystem folds together."""
    store = _store(tmp_path)
    agent = new_id("agent")
    store.save_text("metrics.json", "V1", produced_by=agent)

    with pytest.raises(ValueError, match="case-insensitive"):
        store.save_text("Metrics.json", "V2", produced_by=agent)

    # The first revision is untouched and the store stays internally consistent: without the
    # guard, NTFS would overwrite it without archiving and verify() would then report BOTH keys
    # as modified — an integrity alarm about a collision the store caused itself.
    assert store.load_text("metrics.json") == "V1"
    assert list(store.manifest()) == ["metrics.json"]
    assert store.verify() == []


def test_a_manifest_with_two_case_variant_keys_is_refused_on_reload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_text("metrics.json", "V1", produced_by=new_id("agent"))
    manifest_path = tmp_path / "artifacts" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = dict(raw["metrics.json"])
    entry["uri"] = "Metrics.json"
    raw["Metrics.json"] = entry
    manifest_path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="case-insensitive"):
        LocalArtifactStore(tmp_path / "artifacts", run_id=new_id("run"))


def test_invalidate_and_set_lineage_accept_the_spelling_that_saved_the_artifact(
    tmp_path: Path,
) -> None:
    """Governance actions must not no-op on a spelling ``save`` and ``get`` both accept."""
    store = _store(tmp_path)
    agent = new_id("agent")
    store.save_text("./report.md", "hello", produced_by=agent)
    assert store.get("./report.md") is not None

    updated = store.set_lineage("./report.md", input_artifact_ids=(), execution_key="k")
    assert updated.execution_key == "k"

    assert store.invalidate(["./report.md"], reason="superseded") == ["report.md"]
    invalidated = store.get("report.md")
    assert invalidated is not None
    assert invalidated.valid is False


def test_invalidate_refuses_a_bare_string(tmp_path: Path) -> None:
    """``str`` is an ``Iterable[str]``: this invalidated nothing and reported success."""
    store = _store(tmp_path)
    store.save_text("report.md", "hello", produced_by=new_id("agent"))

    with pytest.raises(TypeError, match="not one string"):
        store.invalidate("report.md", reason="superseded")

    artifact = store.get("report.md")
    assert artifact is not None
    assert artifact.valid is True


# ---------------------------------------------------------------------------
# a write may never overwrite the store's own bookkeeping (manifest / history)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "manifest.json.tmp",
        ".manifest.lock",
        "MANIFEST.JSON",
        "Manifest.Json.Tmp",
        ".MANIFEST.LOCK",
    ],
    ids=[
        "manifest",
        "manifest-tmp",
        "manifest-lock",
        "manifest-upper",
        "manifest-tmp-mixedcase",
        "manifest-lock-mixedcase",
    ],
)
def test_writing_over_the_store_manifest_is_refused(tmp_path: Path, name: str) -> None:
    """The manifest and its scratch file are the store's own bookkeeping, not artifact slots.

    Saving under ``manifest.json`` overwrites the record ``a5_artifact_integrity`` verifies
    against; the ``manifest.json.tmp`` variant is outright data loss. They are reserved
    case-folded and refused on every platform, the way a Windows device name is, so the audit
    trail surfaces the bug rather than normalising it away.
    """
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="reserved by the store"):
        store.save_bytes(name, b"payload", produced_by=new_id("agent"))


@pytest.mark.parametrize(
    "name",
    [".history", ".history/leak.json", ".history/nested/leak.bin", ".History/leak.json"],
    ids=["dir", "under-dir", "deep", "mixedcase"],
)
def test_writing_into_the_store_history_is_refused(tmp_path: Path, name: str) -> None:
    """A name landing in ``.history`` would clobber an archived revision or block archiving.

    A file named ``.history`` stops the archive directory from ever being created; a name under
    ``.history`` can overwrite a superseded revision the store still verifies. The archive is
    reserved case-folded on the write paths; only the store's own archiving reaches it.
    """
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="history"):
        store.save_bytes(name, b"payload", produced_by=new_id("agent"))


def test_a_manifest_named_artifact_under_a_subdirectory_is_still_allowed(tmp_path: Path) -> None:
    """Only the store's own top-level manifest and history are reserved.

    ``reports/manifest.json`` and ``reports/.history/x`` are ordinary artifacts in their own
    subtree; refusing them would be an over-correction that denies legitimate names.
    """
    store = _store(tmp_path)

    nested_manifest = store.save_json(
        "reports/manifest.json", {"v": 1}, produced_by=new_id("agent")
    )
    nested_history = store.save_text("reports/.history/x.txt", "ok", produced_by=new_id("agent"))

    assert nested_manifest.name == "reports/manifest.json"
    assert nested_manifest.uri.startswith(".batches/")
    assert store.load_json("reports/manifest.json") == {"v": 1}
    assert nested_history.name == "reports/.history/x.txt"
    assert nested_history.uri.startswith(".batches/")
    assert store.load_text("reports/.history/x.txt") == "ok"
    # The store's real manifest is a different file and stays intact.
    assert store.verify() == []


def test_a_query_answers_no_for_a_reserved_store_name(tmp_path: Path) -> None:
    """Queries stay total: asking about a reserved name answers ``no`` rather than raising."""
    store = _store(tmp_path)
    store.save_text("metrics.json", "v1", produced_by=new_id("agent"))

    for name in (
        "manifest.json",
        "manifest.json.tmp",
        ".manifest.lock",
        ".history",
        ".history/x.json",
    ):
        assert store.exists(name) is False
        assert store.get(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json::$DATA",
        "manifest.json:hidden",
        "reports/summary.json::$DATA",
    ],
)
def test_a_name_carrying_an_alternate_data_stream_is_refused(tmp_path: Path, name: str) -> None:
    """A colon in a name is a second route to a file that already has a key.

    On NTFS, ``manifest.json::$DATA`` and ``manifest.json`` are the same bytes reached by two
    names, so a colon defeats every name-based rule after it -- including the refusal of the
    store's own reserved names, which is how this was found. It is refused outright rather than
    stripped: no artifact in this system needs a colon, and normalising one away would hide the
    fact that a caller asked for something it should not have.
    """
    store = LocalArtifactStore(tmp_path, new_id("run"))

    with pytest.raises(ValueError, match="must not contain a colon"):
        store.save_text(name, "payload", produced_by=new_id("tool"))
