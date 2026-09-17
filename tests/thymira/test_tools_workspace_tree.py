"""Fast host-side tests for bounded workspace walks and journaled publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thymira.tools.sandbox import workspace_tree
from thymira.tools.sandbox.workspace_tree import (
    WorkspaceTreeError,
    publish_tree,
    recover_publication,
    snapshot_tree,
)


def test_snapshot_digest_is_stable_and_charges_logical_bytes(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "data.txt").write_text("hello", encoding="utf-8")

    first = snapshot_tree(tmp_path, max_bytes=100)
    second = snapshot_tree(tmp_path, max_bytes=100)

    assert first.tree_sha256 == second.tree_sha256
    assert first.logical_bytes == 5
    assert first.entry_count == 2


def test_snapshot_refuses_links_and_logical_quota_overrun(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("the host does not permit symlink creation")

    with pytest.raises(WorkspaceTreeError, match="link"):
        snapshot_tree(tmp_path, max_bytes=100)

    link.unlink()
    root_target = tmp_path / "root-target"
    root_target.mkdir()
    root_link = tmp_path / "root-link"
    try:
        root_link.symlink_to(root_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("the host does not permit directory symlink creation")
    with pytest.raises(WorkspaceTreeError, match="portable directory"):
        snapshot_tree(root_link, max_bytes=100)
    root_link.unlink()
    with pytest.raises(WorkspaceTreeError, match="logical byte"):
        snapshot_tree(tmp_path, max_bytes=3)


def test_publish_tree_commits_new_tree_and_removes_journal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    source = snapshot_tree(workspace, max_bytes=100)
    incoming = tmp_path / ".workspace.incoming"
    incoming.mkdir()
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    new = snapshot_tree(incoming, max_bytes=100)
    journal = tmp_path / ".workspace.journal.json"

    assert (
        publish_tree(
            workspace,
            incoming,
            expected_old_digest=source.tree_sha256,
            expected_new_digest=new.tree_sha256,
            journal_path=journal,
            execution_id="abc123",
            max_bytes=100,
        )
        == "committed"
    )
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new"
    assert not journal.exists()


def test_publish_tree_retries_a_transient_committed_journal_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient Windows lock cannot strand an otherwise committed workspace swap."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    source = snapshot_tree(workspace, max_bytes=100)
    incoming = tmp_path / ".workspace.incoming"
    incoming.mkdir()
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    new = snapshot_tree(incoming, max_bytes=100)
    journal = tmp_path / ".workspace.journal.json"
    real_replace = Path.replace
    denied = False

    def deny_committed_journal_once(source_path: Path, target_path: Path) -> Path:
        nonlocal denied
        target = Path(target_path)
        if (
            not denied
            and target == journal
            and '"state":"committed"' in source_path.read_text(encoding="utf-8")
        ):
            denied = True
            raise PermissionError(5, "simulated transient journal lock", str(journal))
        return real_replace(source_path, target)

    monkeypatch.setattr(Path, "replace", deny_committed_journal_once)

    result = publish_tree(
        workspace,
        incoming,
        expected_old_digest=source.tree_sha256,
        expected_new_digest=new.tree_sha256,
        journal_path=journal,
        execution_id="abc123",
        max_bytes=100,
    )

    assert result == "committed"
    assert denied
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / ".workspace.backup-abc123").exists()
    assert not journal.exists()


def test_publish_tree_discards_an_identical_incoming_tree_without_a_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-op writeback does not swap the live workspace or leave recovery state."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "same.txt").write_text("same", encoding="utf-8")
    incoming = tmp_path / ".workspace.incoming"
    incoming.mkdir()
    (incoming / "same.txt").write_text("same", encoding="utf-8")
    digest = snapshot_tree(workspace, max_bytes=100).tree_sha256
    journal = tmp_path / ".workspace.journal.json"

    def unexpected_journal(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an identical tree must not enter the journaled swap")

    monkeypatch.setattr(workspace_tree, "_journal_write", unexpected_journal)

    assert (
        publish_tree(
            workspace,
            incoming,
            expected_old_digest=digest,
            expected_new_digest=digest,
            journal_path=journal,
            execution_id="abc123",
            max_bytes=100,
        )
        == "committed"
    )
    assert (workspace / "same.txt").read_text(encoding="utf-8") == "same"
    assert not incoming.exists()
    assert not journal.exists()


def test_publish_tree_refuses_the_live_workspace_as_its_own_incoming_tree(
    tmp_path: Path,
) -> None:
    """The identical-tree fast path must never delete the live tree passed in both roles."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    digest = snapshot_tree(workspace, max_bytes=100).tree_sha256

    with pytest.raises(WorkspaceTreeError, match="differ from the live workspace"):
        publish_tree(
            workspace,
            workspace,
            expected_old_digest=digest,
            expected_new_digest=digest,
            journal_path=tmp_path / ".workspace.journal.json",
            execution_id="abc123",
            max_bytes=100,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_publish_tree_is_compare_and_swap_against_external_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    source = snapshot_tree(workspace, max_bytes=100)
    (workspace / "external.txt").write_text("race", encoding="utf-8")
    incoming = tmp_path / ".workspace.incoming"
    incoming.mkdir()
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    new = snapshot_tree(incoming, max_bytes=100)

    with pytest.raises(WorkspaceTreeError, match="changed"):
        publish_tree(
            workspace,
            incoming,
            expected_old_digest=source.tree_sha256,
            expected_new_digest=new.tree_sha256,
            journal_path=tmp_path / ".workspace.journal.json",
            execution_id="abc123",
            max_bytes=100,
        )
    assert (workspace / "external.txt").exists()


def test_recover_publication_completes_a_live_renamed_crash_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    backup = tmp_path / ".workspace.backup-abc"
    incoming = tmp_path / ".workspace.incoming-abc"
    backup.mkdir()
    incoming.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    old = snapshot_tree(backup, max_bytes=100)
    new = snapshot_tree(incoming, max_bytes=100)
    journal = tmp_path / ".workspace.quota-journal.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "live_renamed",
                "live": str(workspace),
                "incoming": str(incoming),
                "backup": str(backup),
                "old_digest": old.tree_sha256,
                "new_digest": new.tree_sha256,
            }
        ),
        encoding="utf-8",
    )

    assert recover_publication(journal, workspace=workspace, max_bytes=100) == "recovered"
    assert (workspace / "new.txt").exists()
    assert not backup.exists()
    assert not journal.exists()


def test_recover_publication_discards_prepared_incoming_generation(tmp_path: Path) -> None:
    """A crash before the first rename leaves the original generation live."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    incoming = tmp_path / ".workspace.incoming-abc"
    incoming.mkdir()
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    old = snapshot_tree(workspace, max_bytes=100)
    new = snapshot_tree(incoming, max_bytes=100)
    journal = tmp_path / ".workspace.quota-journal.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "prepared",
                "live": str(workspace),
                "incoming": str(incoming),
                "backup": str(tmp_path / ".workspace.backup-abc"),
                "old_digest": old.tree_sha256,
                "new_digest": new.tree_sha256,
            }
        ),
        encoding="utf-8",
    )

    assert recover_publication(journal, workspace=workspace, max_bytes=100) == "recovered"
    assert (workspace / "old.txt").exists()
    assert not incoming.exists()
    assert not journal.exists()


@pytest.mark.parametrize("state", ["published", "committed"])
def test_recover_publication_cleans_a_published_generation(tmp_path: Path, state: str) -> None:
    """A crash after the live swap keeps the validated new tree and removes its backup."""
    workspace = tmp_path / "workspace"
    backup = tmp_path / ".workspace.backup-abc"
    workspace.mkdir()
    backup.mkdir()
    (workspace / "new.txt").write_text("new", encoding="utf-8")
    (backup / "old.txt").write_text("old", encoding="utf-8")
    old = snapshot_tree(backup, max_bytes=100)
    new = snapshot_tree(workspace, max_bytes=100)
    journal = tmp_path / ".workspace.quota-journal.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "state": state,
                "live": str(workspace),
                "incoming": str(tmp_path / ".workspace.incoming-abc"),
                "backup": str(backup),
                "old_digest": old.tree_sha256,
                "new_digest": new.tree_sha256,
            }
        ),
        encoding="utf-8",
    )

    assert recover_publication(journal, workspace=workspace, max_bytes=100) == "recovered"
    assert (workspace / "new.txt").exists()
    assert not backup.exists()
    assert not journal.exists()


def test_recover_publication_preserves_conflicting_generations(tmp_path: Path) -> None:
    """A tampered generation is preserved for inspection and cannot be published silently."""
    workspace = tmp_path / "workspace"
    backup = tmp_path / ".workspace.backup-abc"
    incoming = tmp_path / ".workspace.incoming-abc"
    workspace.mkdir()
    backup.mkdir()
    incoming.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    (backup / "old.txt").write_text("old", encoding="utf-8")
    (incoming / "new.txt").write_text("new", encoding="utf-8")
    old = snapshot_tree(backup, max_bytes=100)
    journal = tmp_path / ".workspace.quota-journal.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "state": "prepared",
                "live": str(workspace),
                "incoming": str(incoming),
                "backup": str(backup),
                "old_digest": old.tree_sha256,
                "new_digest": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceTreeError, match="prepared"):
        recover_publication(journal, workspace=workspace, max_bytes=100)
    assert (workspace / "old.txt").exists()
    assert incoming.exists()
    assert backup.exists()
    assert journal.exists()


def test_workspace_lock_is_a_sibling_path(tmp_path: Path) -> None:
    from thymira.tools.sandbox.workspace_tree import WorkspaceLock

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with WorkspaceLock(workspace) as lock:
        assert lock.path.parent == workspace.parent
        assert lock.path.name.startswith(".workspace.")
        assert not lock.path.is_relative_to(workspace)
