"""The read-before-edit freshness policy and literal-edit match refusals (F3.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.schemas import (
    FS_AMBIGUOUS_MATCH,
    FS_NO_MATCH,
    FS_READ_REQUIRED,
    FS_STALE_VERSION,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation
from thymira.tools.builtins.files import EditFile, ReadFile, WriteFile
from thymira.tools.builtins.freshness import FreshnessPolicy, ReadLedger
from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from pathlib import Path


def _invocation(tmp_path: Path) -> ToolInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def _fresh_tools() -> tuple[ReadFile, WriteFile, EditFile]:
    """Three tools sharing one ledger with freshness enforcement turned on."""
    ledger = ReadLedger()
    policy = FreshnessPolicy(require_read_before_edit=True)
    return (
        ReadFile(ledger=ledger),
        WriteFile(ledger=ledger, freshness=policy),
        EditFile(ledger=ledger, freshness=policy),
    )


def test_editing_a_file_never_read_is_refused_with_read_required(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    _read, _write, edit = _fresh_tools()

    with pytest.raises(ToolExecutionError, match=FS_READ_REQUIRED) as info:
        edit.execute(
            invocation,
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
        )
    assert "read it with read_file first" in str(info.value)


def test_reading_then_editing_succeeds(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()

    read.execute(invocation, {"path": "a.py"})
    result = edit.execute(
        invocation,
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
    )

    assert result.success
    assert (invocation.workspace / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_reading_then_a_change_behind_the_tools_back_then_editing_is_refused_stale(
    tmp_path: Path,
):
    invocation = _invocation(tmp_path)
    target = invocation.workspace / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()

    read.execute(invocation, {"path": "a.py"})
    target.write_text("x = 999\n", encoding="utf-8")  # changed behind the ledger's back

    with pytest.raises(ToolExecutionError, match=FS_STALE_VERSION) as info:
        edit.execute(
            invocation,
            {
                "path": "a.py",
                "old_string": "x = 999",
                "new_string": "x = 2",
                "description": "Bump",
            },
        )
    assert "read it again" in str(info.value)


def test_write_file_to_a_new_path_needs_no_prior_read(tmp_path: Path):
    invocation = _invocation(tmp_path)
    _read, write, _edit = _fresh_tools()

    result = write.execute(
        invocation, {"path": "new.txt", "content": "hello", "description": "Write"}
    )

    assert result.success
    assert (invocation.workspace / "new.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_over_an_existing_unread_file_is_refused(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("old", encoding="utf-8")
    _read, write, _edit = _fresh_tools()

    with pytest.raises(ToolExecutionError, match=FS_READ_REQUIRED):
        write.execute(invocation, {"path": "a.txt", "content": "new", "description": "Overwrite"})


def test_write_then_write_to_the_same_path_succeeds_without_a_re_read(tmp_path: Path):
    """The tool's own write refreshes the ledger, so a second write needs no fresh read_file.

    This is the exact shape a real THY coding-recovery loop drives (write, run, fix, write
    again) and the highest-risk part of turning freshness on by default in the registry.
    """
    invocation = _invocation(tmp_path)
    _read, write, _edit = _fresh_tools()

    first = write.execute(
        invocation, {"path": "script.py", "content": "1 / 0\n", "description": "Write broken"}
    )
    second = write.execute(
        invocation,
        {"path": "script.py", "content": "print('ok')\n", "description": "Write fixed"},
    )

    assert first.success
    assert second.success
    assert (invocation.workspace / "script.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_edit_then_edit_to_the_same_path_succeeds_without_a_re_read(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()

    read.execute(invocation, {"path": "a.py"})
    edit.execute(
        invocation,
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
    )
    second = edit.execute(
        invocation,
        {"path": "a.py", "old_string": "x = 2", "new_string": "x = 3", "description": "Bump again"},
    )

    assert second.success
    assert (invocation.workspace / "a.py").read_text(encoding="utf-8") == "x = 3\n"


def test_a_zero_match_edit_is_refused_with_no_match(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()
    read.execute(invocation, {"path": "a.py"})

    with pytest.raises(ToolExecutionError, match=FS_NO_MATCH):
        edit.execute(
            invocation,
            {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3", "description": "Edit"},
        )


def test_a_two_match_edit_without_replace_all_is_refused_ambiguous(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()
    read.execute(invocation, {"path": "a.py"})

    with pytest.raises(ToolExecutionError, match=FS_AMBIGUOUS_MATCH) as info:
        edit.execute(
            invocation,
            {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Edit"},
        )
    assert "appears 2 times" in str(info.value)
    assert "replace_all" in str(info.value)


def test_replace_all_on_the_same_two_match_input_succeeds(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    read, _write, edit = _fresh_tools()
    read.execute(invocation, {"path": "a.py"})

    result = edit.execute(
        invocation,
        {
            "path": "a.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
            "description": "Edit",
        },
    )

    assert result.success
    assert (invocation.workspace / "a.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_freshness_policy_can_be_configured_off_to_restore_pre_slice_behaviour(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    ledger = ReadLedger()
    off = FreshnessPolicy(require_read_before_edit=False)
    edit = EditFile(ledger=ledger, freshness=off)

    result = edit.execute(
        invocation,
        {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": "Bump"},
    )

    assert result.success


def test_write_file_and_edit_file_refuse_a_target_inside_the_scratch_directory(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / ".thymira").mkdir()
    _read, write, edit = _fresh_tools()

    with pytest.raises(ToolExecutionError, match=r"\.thymira"):
        write.execute(
            invocation,
            {"path": ".thymira/x.py", "content": "x = 1", "description": "Write"},
        )
    with pytest.raises(ToolExecutionError, match=r"\.thymira"):
        edit.execute(
            invocation,
            {
                "path": ".thymira/x.py",
                "old_string": "x",
                "new_string": "y",
                "description": "Edit",
            },
        )
