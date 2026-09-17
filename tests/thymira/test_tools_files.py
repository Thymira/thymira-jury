"""Workspace file tools: paged reads, literal edits, glob and grep (harness basics 1)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation
from thymira.tools.builtins.files import (
    EditFile,
    EditFileArguments,
    ReadFile,
    WriteFile,
    WriteFileArguments,
)
from thymira.tools.builtins.output_bounds import MAX_LINE_CHARS, MAX_OUTPUT_BYTES, MAX_READ_BYTES
from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.builtins.search import Glob, Grep
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


def test_effectful_description_is_normalized_and_rejects_whitespace_only() -> None:
    """The shared description contract records concise text and rejects blank rationale."""
    arguments = WriteFileArguments(
        path="output.txt", content="value", description="\t Write   the file \n"
    )
    assert arguments.description == "Write the file"
    with pytest.raises(ValidationError, match="description"):
        WriteFileArguments(path="output.txt", content="value", description=" \t\n")

    edit = EditFileArguments(
        path="output.txt", old_string="value", new_string="new", description="  Edit it  "
    )
    assert edit.description == "Edit it"


def test_read_file_returns_a_window_and_says_which_lines_it_showed(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "big.txt").write_text(
        "\n".join(f"row {i}" for i in range(1, 11)) + "\n", encoding="utf-8"
    )

    result = ReadFile().execute(invocation, {"path": "big.txt", "offset": 3, "limit": 2})

    assert result.stdout == "row 3\nrow 4\n[showing lines 3-4 of 10]"


def test_write_file_publishes_registered_output_through_batch_store(tmp_path: Path) -> None:
    """A file tool's registered output reaches the transactional attachment path."""
    invocation = _invocation(tmp_path)

    result = WriteFile().execute(
        invocation,
        {
            "path": "report.txt",
            "content": "durable report",
            "kind": ArtifactKind.REPORT,
            "description": "Write the durable report",
        },
    )

    assert result.artifact_ids
    artifact = invocation.artifact_store.get("report.txt")
    assert artifact is not None
    assert artifact.uri.startswith(".batches/")
    assert invocation.artifact_store.load_text("report.txt") == "durable report"
    assert invocation.artifact_store.verify() == []


def test_read_file_returns_the_whole_file_without_a_marker_when_it_fits(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "small.txt").write_text("a\nb\n", encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "small.txt"})

    assert result.stdout == "a\nb\n"


def test_read_file_says_so_when_the_offset_is_past_the_end(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "short.txt").write_text("a\nb\n", encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "short.txt", "offset": 10, "limit": 5})

    assert result.stdout == "[no lines at offset 10; the file has 2 lines]"


def test_read_file_truncates_a_single_long_line_and_says_so(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "longline.txt").write_text("x" * 5000 + "\n", encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "longline.txt"})

    assert f"1 line truncated at {MAX_LINE_CHARS} characters" in result.stdout
    assert "x" * MAX_LINE_CHARS in result.stdout
    assert "x" * (MAX_LINE_CHARS + 1) not in result.stdout
    assert "showing lines 1-1 of 1" in result.stdout


def test_read_file_bounds_output_by_bytes_and_says_so(tmp_path: Path):
    invocation = _invocation(tmp_path)
    line = "x" * 100
    content = "\n".join(line for _ in range(5000)) + "\n"
    (invocation.workspace / "huge.txt").write_text(content, encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "huge.txt", "limit": 20_000})

    assert f"output bounded at {MAX_READ_BYTES} bytes" in result.stdout
    assert result.stdout.count("\n") < 5000
    assert len(result.stdout.encode("utf-8")) <= MAX_READ_BYTES


def test_read_file_never_corrupts_multi_byte_characters_when_truncating(tmp_path: Path):
    invocation = _invocation(tmp_path)
    # U+1F600 GRINNING FACE is a 4-byte UTF-8 sequence; well over the per-line character bound.
    emoji_line = "\U0001f600" * 2500
    (invocation.workspace / "emoji.txt").write_text(emoji_line + "\n", encoding="utf-8")

    result = ReadFile().execute(invocation, {"path": "emoji.txt"})

    assert "�" not in result.stdout
    result.stdout.encode("utf-8").decode("utf-8")
    assert f"1 line truncated at {MAX_LINE_CHARS} characters" in result.stdout
    assert len(result.stdout.encode("utf-8")) <= MAX_READ_BYTES


def test_edit_file_replaces_a_unique_literal_and_reports_the_count(tmp_path: Path):
    invocation = _invocation(tmp_path)
    target = invocation.workspace / "script.py"
    target.write_text("x = 1\ny = x + 1\n", encoding="utf-8")

    result = EditFile().execute(
        invocation,
        {
            "path": "script.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
            "description": "Bump x",
        },
    )

    assert result.success
    assert target.read_text(encoding="utf-8") == "x = 2\ny = x + 1\n"
    assert "1 replacement" in result.stdout


def test_edit_file_refuses_an_ambiguous_old_string_and_says_how_to_fix_it(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "dup.txt").write_text("a\na\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match=r"appears 2 times.*replace_all"):
        EditFile().execute(
            invocation,
            {"path": "dup.txt", "old_string": "a", "new_string": "b", "description": "Rename"},
        )


def test_edit_file_refuses_a_missing_old_string(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "f.txt").write_text("hello\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="old_string was not found"):
        EditFile().execute(
            invocation,
            {"path": "f.txt", "old_string": "bye", "new_string": "x", "description": "Edit"},
        )


def test_edit_file_replace_all_replaces_every_occurrence(tmp_path: Path):
    invocation = _invocation(tmp_path)
    target = invocation.workspace / "dup.txt"
    target.write_text("a a a\n", encoding="utf-8")

    result = EditFile().execute(
        invocation,
        {
            "path": "dup.txt",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
            "description": "Rename all",
        },
    )

    assert target.read_text(encoding="utf-8") == "b b b\n"
    assert "3 replacements" in result.stdout


def _split_footer(stdout: str) -> tuple[list[str], str]:
    """Split a glob/grep rendering into its content lines and its trailing bracket footer."""
    if stdout.startswith("["):
        return [], stdout
    body, _, footer = stdout.rpartition("\n[")
    return body.splitlines(), "[" + footer


def test_glob_returns_files_only_matching_basenames_at_any_depth(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "src").mkdir()
    (invocation.workspace / "src" / "a.py").write_text("", encoding="utf-8")
    (invocation.workspace / "b.py").write_text("", encoding="utf-8")
    (invocation.workspace / "notes.md").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.py"})

    lines, footer = _split_footer(result.stdout)
    assert lines == ["b.py", "src/a.py"]  # path order: "b.py" < "src/a.py"
    assert footer.startswith("[glob: 2 of 2 paths shown, ordered by path")
    assert "; complete list in artifact artifact_" in footer


def test_glob_caps_the_result_and_says_how_many_were_omitted(tmp_path: Path):
    invocation = _invocation(tmp_path)
    for i in range(105):
        (invocation.workspace / f"f{i:03d}.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    lines, footer = _split_footer(result.stdout)
    assert len(lines) == 100
    assert lines == sorted(lines)
    assert footer.startswith("[glob: 100 of 105 paths shown, ordered by path")
    assert "; complete list in artifact artifact_" in footer


def test_glob_single_star_does_not_cross_directories(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "src" / "sub").mkdir(parents=True)
    (invocation.workspace / "src" / "b.py").write_text("", encoding="utf-8")
    (invocation.workspace / "src" / "sub" / "a.py").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "src/*.py"})

    lines, _ = _split_footer(result.stdout)
    assert lines == ["src/b.py"]


def test_glob_double_star_matches_at_any_depth_under_a_prefix(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "src" / "sub").mkdir(parents=True)
    (invocation.workspace / "src" / "b.py").write_text("", encoding="utf-8")
    (invocation.workspace / "src" / "sub" / "a.py").write_text("", encoding="utf-8")
    (invocation.workspace / "top.py").write_text("", encoding="utf-8")

    prefixed = Glob().execute(invocation, {"pattern": "src/**/*.py"})
    unprefixed = Glob().execute(invocation, {"pattern": "**/*.py"})

    prefixed_lines, _ = _split_footer(prefixed.stdout)
    unprefixed_lines, _ = _split_footer(unprefixed.stdout)
    assert prefixed_lines == ["src/b.py", "src/sub/a.py"]
    assert unprefixed_lines == ["src/b.py", "src/sub/a.py", "top.py"]


def test_glob_question_mark_matches_one_character_only(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a1.txt").write_text("", encoding="utf-8")
    (invocation.workspace / "a10.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "a?.txt"})

    lines, _ = _split_footer(result.stdout)
    assert lines == ["a1.txt"]


def test_glob_orders_by_path_whatever_order_the_directory_was_built_in(tmp_path: Path):
    invocation = _invocation(tmp_path)
    # Adversarial creation order (z, a, m, nested-a) with inverted mtimes: the newest file
    # ("z/c.txt", created first below) is given the OLDEST mtime and the last-created file
    # ("a/a.txt") is given the NEWEST mtime -- an mtime-based order would show it last-first,
    # the opposite of path order, so this proves the sort no longer depends on mtime at all.
    plan = [
        ("z/c.txt", 1_700_000_400),
        ("a/b.txt", 1_700_000_300),
        ("m.txt", 1_700_000_200),
        ("a/a.txt", 1_700_000_100),
    ]
    for relative, mtime in plan:
        path = invocation.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    result = Glob().execute(invocation, {"pattern": "**/*"})

    lines, footer = _split_footer(result.stdout)
    assert lines == ["a/a.txt", "a/b.txt", "m.txt", "z/c.txt"]
    assert footer.startswith("[glob: 4 of 4 paths shown, ordered by path")
    assert "; complete list in artifact artifact_" in footer


def test_grep_groups_matches_by_file_with_line_numbers(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "a.py").write_text("import os\nx = 1\n", encoding="utf-8")
    (invocation.workspace / "b.py").write_text("import sys\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": r"^import \w+", "include": "*.py"})

    lines, footer = _split_footer(result.stdout)
    assert lines == ["a.py:1: import os", "b.py:1: import sys"]
    assert footer.startswith("[grep: 2 of 2 matches shown in 2 files, ordered by path then line")
    assert "; complete list in artifact artifact_" in footer


def test_grep_orders_by_path_then_line_number(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "z").mkdir()
    (invocation.workspace / "z" / "c.txt").write_text("hit\nhit\n", encoding="utf-8")
    (invocation.workspace / "a").mkdir()
    (invocation.workspace / "a" / "b.txt").write_text("hit\n", encoding="utf-8")
    (invocation.workspace / "m.txt").write_text("hit\nnope\nhit\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": "hit"})

    lines, _ = _split_footer(result.stdout)
    assert lines == [
        "a/b.txt:1: hit",
        "m.txt:1: hit",
        "m.txt:3: hit",
        "z/c.txt:1: hit",
        "z/c.txt:2: hit",
    ]


def test_grep_bounds_a_very_long_matching_line_and_says_so(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / "long.txt").write_text("x" * 5000 + "\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": "x+"})

    lines, footer = _split_footer(result.stdout)
    assert len(lines) == 1
    assert "1 line truncated at 2000 characters" in footer


def test_glob_and_grep_bound_the_rendered_output_by_bytes(tmp_path: Path):
    """The byte budget can stop rendering before GLOB_CAP/GREP_CAP would.

    Grep's matched *content* is not limited by a filesystem path-length ceiling the way a
    filename would be, so this drives the byte budget directly: 250 matching lines (at
    GREP_CAP, so the count cap alone would not explain a shorter rendering) each long enough
    that their combined bytes exceed MAX_OUTPUT_BYTES well before 250 lines are rendered.
    Glob's own byte-bounding is the same `bound_lines` call, already proven directly against
    the byte budget in test_tools_output_bounds.py; a realistic all-under-MAX_PATH glob case
    never reaches the byte budget before GLOB_CAP does, which is why this node exercises grep.
    """
    invocation = _invocation(tmp_path)
    line = "x" * 250  # one match line, well within MAX_LINE_CHARS so it is never char-truncated
    content = "\n".join(line for _ in range(300)) + "\n"
    (invocation.workspace / "wide.txt").write_text(content, encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": "x+"})

    lines, footer = _split_footer(result.stdout)
    assert 0 < len(lines) < 250
    assert "of 300 matches shown" in footer
    assert str(len(lines)) in footer
    assert len(result.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_grep_footer_and_multibyte_rendering_stay_within_the_byte_bound(tmp_path: Path):
    invocation = _invocation(tmp_path)
    emoji_line = "\U0001f600" * MAX_LINE_CHARS
    (invocation.workspace / "emoji.txt").write_text(
        "\n".join(emoji_line for _ in range(80)) + "\n", encoding="utf-8"
    )

    result = Grep().execute(invocation, {"pattern": "\U0001f600+"})

    assert len(result.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES
    result.stdout.encode("utf-8").decode("utf-8")


def test_glob_and_grep_skip_the_thymira_scratch_directory(tmp_path: Path):
    invocation = _invocation(tmp_path)
    (invocation.workspace / ".thymira").mkdir()
    (invocation.workspace / ".thymira" / "scratch.py").write_text("hit\n", encoding="utf-8")
    (invocation.workspace / "visible.py").write_text("hit\n", encoding="utf-8")

    glob_lines, _ = _split_footer(Glob().execute(invocation, {"pattern": "*.py"}).stdout)
    grep_lines, _ = _split_footer(Grep().execute(invocation, {"pattern": "hit"}).stdout)

    assert glob_lines == ["visible.py"]
    assert grep_lines == ["visible.py:1: hit"]


def test_grep_rejects_an_invalid_regular_expression(tmp_path: Path):
    invocation = _invocation(tmp_path)

    with pytest.raises(ToolExecutionError, match="invalid regular expression"):
        Grep().execute(invocation, {"pattern": "("})


def test_builtins_registry_holds_the_eight_core_tools():
    names = {tool.name for tool in builtins_registry()}

    assert {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "run_python",
        "profile_dataset",
        "run_experiment",
    } <= names
