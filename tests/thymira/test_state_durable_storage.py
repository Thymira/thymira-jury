"""Independent durability oracles for local Run, metadata, settings and attachment state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import thymira.state.local_store as artifact_module
from thymira.schemas import Actor, EventType, Run, SecretReference, Session, new_id
from thymira.state import (
    LocalArtifactStore,
    LocalRunStore,
    LocalSessionRepository,
    LocalSettingsStore,
    LocalWorkspaceRegistry,
    SettingsConflictError,
    SettingsCorruptError,
    workspace_id,
    workspace_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _run_store(tmp_path: Path) -> tuple[LocalRunStore, Run]:
    """Create one event-backed Run with a child event for projection assertions."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="replay this Run",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    store.append(
        run.id,
        EventType.AGENT_STARTED,
        Actor.system(),
        {"task_id": new_id("task")},
        expected_version=1,
        subject_id=new_id("agent"),
    )
    return store, run


@pytest.mark.parametrize("replacement", [None, '{"partial"', '{"run_id": "wrong"}'])
def test_run_open_replays_authoritative_log_when_projection_is_missing_or_unusable(
    tmp_path: Path, replacement: str | None
) -> None:
    """A missing, partial or stale projection never prevents opening the event-backed Run."""
    store, run = _run_store(tmp_path)
    projection_path = tmp_path / "runs" / run.id / "run.json"
    if replacement is None:
        projection_path.unlink()
    else:
        projection_path.write_text(replacement, encoding="utf-8", newline="\n")

    reopened = LocalRunStore(tmp_path / "runs")
    projected = reopened.get(run.id)

    assert projected.id == run.id
    assert projected.agent_ids
    assert json.loads(projection_path.read_text(encoding="utf-8"))["version"] == 2
    assert store.events(run.id) == reopened.events(run.id)


def test_session_listing_reads_metadata_records_without_scanning_run_logs(tmp_path: Path) -> None:
    """Session pages come from session metadata even when an unrelated log is unusable."""
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="cli")
    repository = LocalSessionRepository(tmp_path)
    repository.save(session)
    run_dir = tmp_path / "runs" / new_id("run")
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("not a session record", encoding="utf-8")

    reopened = LocalSessionRepository(tmp_path)
    page = reopened.list(project_id=project_id)

    assert page.items == (session,)


def test_workspace_identity_is_canonical_and_registry_delete_preserves_user_data(
    tmp_path: Path,
) -> None:
    """Equivalent paths share metadata identity and deleting it never removes workspace files."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    sentinel = workspace / "user-data.csv"
    sentinel.write_text("owned by the user", encoding="utf-8")
    equivalent = workspace / "."
    registry = LocalWorkspaceRegistry(tmp_path / "registry")
    project_id = new_id("project")

    record = registry.register(workspace, project_id)

    assert workspace_identity(workspace) == workspace_identity(equivalent)
    assert record.workspace_id == workspace_id(equivalent)
    assert registry.get(equivalent) == record
    assert registry.list() == (record,)
    assert registry.delete(record.workspace_id) is True
    assert registry.get(workspace) is None
    assert sentinel.read_text(encoding="utf-8") == "owned by the user"


def test_settings_are_one_cas_layer_and_keep_secrets_as_source_references(tmp_path: Path) -> None:
    """Reopen and stale writers observe revisions while operational secrets stay external."""
    root = tmp_path / "settings"
    first = LocalSettingsStore(root)
    reference = SecretReference(source="env", name="THYMIRA_API_KEY")

    initial = first.put(
        "runtime",
        {"mode": "safe"},
        secret_refs={"api_key": reference},
    )
    second = LocalSettingsStore(root)
    assert second.get("runtime") == initial
    updated = second.put("runtime", {"mode": "strict"}, expected_revision=1)

    with pytest.raises(SettingsConflictError) as conflict:
        first.put("runtime", {"mode": "unsafe"}, expected_revision=1)
    assert conflict.value.current == updated
    with pytest.raises(ValueError, match="secret-shaped"):
        first.put("credentials", {"api_key": "raw-secret"})
    with pytest.raises(ValueError, match="cannot also have values"):
        first.put(
            "credentials",
            {"api_key": "raw-secret"},
            secret_refs={"api_key": reference},
        )
    with pytest.raises(ValueError, match="secret-shaped"):
        first.put("nested", {"provider": {"api_key": "raw-secret"}})
    with pytest.raises(ValueError, match="secret-shaped"):
        first.put("token", {"token": "raw-secret"})


def test_settings_corruption_is_explicitly_reported(tmp_path: Path) -> None:
    """A malformed settings document is never silently treated as an empty layer."""
    root = tmp_path / "settings"
    store = LocalSettingsStore(root)
    store.put("runtime", {"mode": "safe"})
    (root / "settings.json").write_text("{", encoding="utf-8", newline="\n")

    with pytest.raises(SettingsCorruptError):
        LocalSettingsStore(root).get("runtime")


def _artifact_store(tmp_path: Path) -> LocalArtifactStore:
    """Build a local artifact store for one isolated test Run."""
    return LocalArtifactStore(tmp_path / "artifacts", run_id=new_id("run"))


def _assert_published_manifest_files(root: Path) -> None:
    """Check the published manifest and bytes directly, independently of store methods."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest.values():
        path = root / entry["uri"]
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["size_bytes"]


def test_attachment_batch_publishes_one_manifest_and_preserves_old_readers(tmp_path: Path) -> None:
    """A batch makes every staged member visible together through one manifest snapshot."""
    store = _artifact_store(tmp_path)
    store.save_bytes("a.txt", b"old", produced_by=new_id("agent"))
    run_id = store.manifest()["a.txt"].run_id
    old_reader = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    old_manifest = (tmp_path / "artifacts" / "manifest.json").read_text(encoding="utf-8")

    committed = store.save_batch(
        {"a.txt": b"new", "b.txt": b"second"},
        produced_by=new_id("tool"),
    )

    assert [artifact.name for artifact in committed] == ["a.txt", "b.txt"]
    assert store.load_bytes("a.txt") == b"new"
    assert store.load_bytes("b.txt") == b"second"
    assert old_reader.load_bytes("a.txt") == b"old"
    assert old_reader.get("b.txt") is None
    assert (tmp_path / "artifacts" / "manifest.json").read_text(encoding="utf-8") != old_manifest
    assert all(artifact.uri.startswith(".batches/") for artifact in committed)
    assert store.verify() == []
    _assert_published_manifest_files(tmp_path / "artifacts")
    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    assert reopened.load_bytes("a.txt") == b"new"
    assert reopened.load_bytes("b.txt") == b"second"
    assert reopened.verify() == []


def test_single_attachment_publication_preserves_old_readers(tmp_path: Path) -> None:
    """The single-item convenience API publishes a new immutable object URI."""
    store = _artifact_store(tmp_path)
    first = store.save_bytes("same.txt", b"old", produced_by=new_id("tool"))
    reader = LocalArtifactStore(tmp_path / "artifacts", run_id=first.run_id)

    second = store.save_bytes("same.txt", b"new", produced_by=new_id("tool"))

    assert first.uri.startswith(".batches/")
    assert second.uri.startswith(".batches/")
    assert reader.load_bytes("same.txt") == b"old"
    assert reader.load_bytes_bounded("same.txt", 3) == b"old"
    assert store.load_bytes("same.txt") == b"new"
    assert store.load_bytes_bounded("same.txt", 3) == b"new"
    assert store.verify() == []


def test_invalid_attachment_batch_leaves_manifest_unchanged(tmp_path: Path) -> None:
    """Whole-batch validation rejects one bad member before any staging begins."""
    store = _artifact_store(tmp_path)
    store.save_bytes("a.txt", b"old", produced_by=new_id("agent"))
    manifest_path = tmp_path / "artifacts" / "manifest.json"
    before = manifest_path.read_text(encoding="utf-8")
    batches = tmp_path / "artifacts" / ".batches"
    before_batches = set(batches.iterdir())

    with pytest.raises(ValueError, match="escape"):
        store.save_batch(
            {"a.txt": b"new", "../escape.txt": b"must refuse"},
            produced_by=new_id("tool"),
        )

    assert manifest_path.read_text(encoding="utf-8") == before
    assert store.load_bytes("a.txt") == b"old"
    assert set(batches.iterdir()) == before_batches


def test_attachment_batch_staging_failure_keeps_previous_snapshot_reopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in a staged member leaves no partial publication or changed old attachment."""
    store = _artifact_store(tmp_path)
    store.save_bytes("a.txt", b"old", produced_by=new_id("agent"))
    original: Callable[..., None] = store._write_staged_bytes
    calls = 0

    def fail_on_second(target: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staging failure")
        original(target, data)

    monkeypatch.setattr(store, "_write_staged_bytes", fail_on_second)
    batches = tmp_path / "artifacts" / ".batches"
    before_batches = set(batches.iterdir())
    with pytest.raises(OSError, match="injected"):
        store.save_batch(
            {"a.txt": b"new", "b.txt": b"second"},
            produced_by=new_id("tool"),
        )

    run_id = store.manifest()["a.txt"].run_id
    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    _assert_published_manifest_files(tmp_path / "artifacts")
    assert reopened.load_bytes("a.txt") == b"old"
    assert reopened.get("b.txt") is None
    assert set(batches.iterdir()) == before_batches


def test_attachment_batch_post_replace_fault_returns_committed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact durable manifest makes a post-replace signal a successful commit."""
    store = _artifact_store(tmp_path)
    store.save_bytes("a.txt", b"old", produced_by=new_id("agent"))
    real_replace = artifact_module.replace_with_retry
    calls = 0

    def replace_then_fault(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        real_replace(source, target)
        if calls == 3:  # two immutable objects, then the one manifest publication
            raise OSError("injected post-publication failure")

    monkeypatch.setattr(artifact_module, "replace_with_retry", replace_then_fault)
    committed = store.save_batch(
        {"a.txt": b"new", "b.txt": b"second"},
        produced_by=new_id("tool"),
    )

    run_id = store.manifest()["a.txt"].run_id
    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    assert [artifact.name for artifact in committed] == ["a.txt", "b.txt"]
    assert store.load_bytes("a.txt") == b"new"
    assert store.load_bytes("b.txt") == b"second"
    _assert_published_manifest_files(tmp_path / "artifacts")
    for artifact in committed:
        reopened_artifact = reopened.get(artifact.name)
        assert reopened_artifact is not None
        assert reopened_artifact.id == artifact.id
    assert reopened.load_bytes("a.txt") == b"new"
    assert reopened.load_bytes("b.txt") == b"second"
    assert reopened.verify() == []


def test_attachment_batch_manifest_replace_failure_keeps_old_snapshot_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed manifest replace leaves staged objects invisible and the old snapshot intact."""
    store = _artifact_store(tmp_path)
    store.save_bytes("a.txt", b"old", produced_by=new_id("agent"))
    run_id = store.manifest()["a.txt"].run_id
    real_replace = artifact_module.replace_with_retry
    calls = 0

    def fail_manifest(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:  # two immutable objects, then the one manifest publication
            raise OSError("injected manifest failure")
        real_replace(source, target)

    monkeypatch.setattr(artifact_module, "replace_with_retry", fail_manifest)
    with pytest.raises(OSError, match="manifest"):
        store.save_batch(
            {"a.txt": b"new", "b.txt": b"second"},
            produced_by=new_id("tool"),
        )

    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    _assert_published_manifest_files(tmp_path / "artifacts")
    assert reopened.load_bytes("a.txt") == b"old"
    assert reopened.get("b.txt") is None


def test_first_attachment_post_replace_fault_returns_committed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first exact manifest commit is returned despite a post-replace signal."""
    run_id = new_id("run")
    store = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    manifest_path = tmp_path / "artifacts" / "manifest.json"
    assert not manifest_path.exists()
    real_replace = artifact_module.replace_with_retry

    def replace_then_fault(source: Path, target: Path) -> None:
        real_replace(source, target)
        if target == manifest_path:
            raise OSError("first publication fault after replace")

    monkeypatch.setattr(artifact_module, "replace_with_retry", replace_then_fault)
    committed = store.save_bytes("first.txt", b"first", produced_by=new_id("tool"))

    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=run_id)
    artifact = reopened.get("first.txt")
    assert committed.name == "first.txt"
    assert store.load_bytes("first.txt") == b"first"
    assert artifact is not None
    assert artifact.id == committed.id
    assert artifact.uri.startswith(".batches/")
    assert reopened.load_bytes("first.txt") == b"first"
    assert reopened.verify() == []


def test_attachment_batch_fsyncs_and_reads_back_staged_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging and manifest publication flush files before the store reports success."""
    store = _artifact_store(tmp_path)
    real_fsync = artifact_module.os.fsync
    calls: list[int] = []

    def record_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(artifact_module.os, "fsync", record_fsync)
    data = b"content-addressed bytes"
    artifact = store.save_batch({"a.txt": data}, produced_by=new_id("tool"))[0]

    assert hashlib.sha256(store.load_bytes("a.txt")).hexdigest() == artifact.sha256
    assert len(calls) >= 2  # staged object and published manifest


def test_manifest_publication_does_not_fsync_a_read_only_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real Windows publication fsyncs writable bytes and reopens successfully."""
    store = _artifact_store(tmp_path)
    manifest_path = tmp_path / "artifacts" / "manifest.json"
    real_open = Path.open
    read_only_manifest_opens: list[Path] = []

    def record_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode")
        if path == manifest_path and mode == "rb":
            read_only_manifest_opens.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_open)
    artifact = store.save_bytes("manifest-proof.txt", b"durable", produced_by=new_id("tool"))

    reopened = LocalArtifactStore(tmp_path / "artifacts", run_id=artifact.run_id)
    assert reopened.load_bytes("manifest-proof.txt") == b"durable"
    assert reopened.verify() == []
    assert read_only_manifest_opens == []
