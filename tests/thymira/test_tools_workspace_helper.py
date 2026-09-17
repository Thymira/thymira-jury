"""Fast tests for the fixed helper's independent walk and copy protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.tools.sandbox import workspace_helper

if TYPE_CHECKING:
    from pathlib import Path


def test_stage_and_export_preserve_helper_digest_and_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    export = tmp_path / "export"
    source.mkdir()
    workspace.mkdir()
    (source / "input.txt").write_text("input", encoding="utf-8")
    monkeypatch.setattr(
        workspace_helper,
        "_probe",
        lambda _path, _requested: {
            "capacity_bytes": 128,
            "free_bytes": 128,
            "inode_capacity": 256,
            "inode_free": 256,
            "filesystem": "tmpfs",
        },
    )

    staged = workspace_helper.stage(source, workspace, requested=128, max_entries=100)
    (workspace / "output.txt").write_text("output", encoding="utf-8")
    exported = workspace_helper.export(workspace, export, requested=128, max_entries=100)

    assert staged["source"]["tree_sha256"] == staged["final"]["tree_sha256"]
    assert exported["final"]["tree_sha256"] == exported["exported"]["tree_sha256"]
    assert (export / "output.txt").read_text(encoding="utf-8") == "output"


def test_helper_rejects_capacity_drift_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    export = tmp_path / "export"
    workspace.mkdir()
    (workspace / "output.txt").write_text("output", encoding="utf-8")
    calls = iter(
        [
            {
                "capacity_bytes": 128,
                "free_bytes": 128,
                "inode_capacity": 256,
                "inode_free": 256,
                "filesystem": "tmpfs",
            },
            {
                "capacity_bytes": 256,
                "free_bytes": 256,
                "inode_capacity": 256,
                "inode_free": 256,
                "filesystem": "tmpfs",
            },
        ]
    )
    monkeypatch.setattr(workspace_helper, "_probe", lambda _path, _requested: next(calls))

    with pytest.raises(workspace_helper.HelperError, match="capacity"):
        workspace_helper.export(workspace, export, requested=128, max_entries=100)


def test_stage_charges_runtime_inputs_and_export_removes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    inputs = tmp_path / "inputs"
    workspace = tmp_path / "workspace"
    export = tmp_path / "export"
    source.mkdir()
    inputs.mkdir()
    workspace.mkdir()
    (source / "existing.txt").write_text("source", encoding="utf-8")
    (inputs / "runtime.py").write_text("print('runtime')", encoding="utf-8")
    monkeypatch.setattr(
        workspace_helper,
        "_probe",
        lambda _path, _requested: {
            "capacity_bytes": 128,
            "free_bytes": 128,
            "inode_capacity": 256,
            "inode_free": 256,
            "filesystem": "tmpfs",
        },
    )

    staged = workspace_helper.stage(
        source, workspace, inputs=inputs, requested=128, max_entries=100
    )

    assert staged["inputs"]["logical_bytes"] == len("print('runtime')")
    assert staged["final"]["logical_bytes"] == (
        staged["source"]["logical_bytes"] + staged["inputs"]["logical_bytes"]
    )
    (workspace / "result.txt").write_text("result", encoding="utf-8")
    exported = workspace_helper.export(
        workspace, export, inputs=inputs, requested=128, max_entries=100
    )
    assert not (export / "runtime.py").exists()
    assert (export / "result.txt").read_text(encoding="utf-8") == "result"
    assert exported["inputs"]["tree_sha256"] == staged["inputs"]["tree_sha256"]


def test_copy_rejects_growth_after_snapshot_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    source_file = source / "input.bin"
    source_file.write_bytes(b"x")
    snapshot = workspace_helper._snapshot(source, max_bytes=4 * 1024 * 1024, max_entries=100)
    original_open = workspace_helper._open_relative

    def grow_after_open(root: Path, relative: str) -> int:
        descriptor = original_open(root, relative)
        source_file.write_bytes(b"x" * (2 * 1024 * 1024))
        return descriptor

    monkeypatch.setattr(workspace_helper, "_open_relative", grow_after_open)
    with pytest.raises(workspace_helper.HelperError, match="changed before bounded copy"):
        workspace_helper._copy(source, destination, snapshot)
    assert not (destination / "input.bin").exists()
