"""Unit tests for the F12 Agent Notes mechanical gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tooling.conftest import load_script


def test_agent_notes_gate_accepts_the_repository_notes(tmp_path: Path) -> None:
    """The checked-in lifecycle notes satisfy the fixed format."""
    script = load_script("scripts/validate_agent_notes.py")
    assert script.validate_notes(Path(__file__).parents[2] / ".agents" / "notes") == []


@pytest.mark.parametrize(
    "boilerplate",
    [
        "- None.",
        "> A decision recorded without what it beat invites re-litigation.",
        "1. **None.**",
        "> *A decision recorded without what it beat invites re-litigation.*",
    ],
)
def test_agent_notes_gate_rejects_formatted_boilerplate_alternatives(
    tmp_path: Path,
    boilerplate: str,
) -> None:
    """Formatted placeholders cannot satisfy the mandatory alternatives rationale."""
    script = load_script("scripts/validate_agent_notes.py")
    note = tmp_path / "implemented" / "decision.md"
    note.parent.mkdir()
    note.write_text(
        "## Problem\nproblem\n## Decision\ndecision\n## Alternatives considered\n"
        f"{boilerplate}\n"
        "## Consequences\nconsequences\n",
        encoding="utf-8",
    )
    errors = script.validate_notes(tmp_path)
    assert any("must name what the decision beat" in error for error in errors)


def test_agent_notes_gate_rejects_empty_sections_with_boilerplate_marker(
    tmp_path: Path,
) -> None:
    """A boilerplate alternatives sentence cannot satisfy empty note sections."""
    script = load_script("scripts/validate_agent_notes.py")
    note = tmp_path / "implemented" / "empty.md"
    note.parent.mkdir()
    note.write_text(
        "## Problem\n\n## Decision\n\n## Alternatives considered\n"
        "A decision recorded without what it beat invites re-litigation.\n"
        "## Consequences\n",
        encoding="utf-8",
    )

    errors = script.validate_notes(tmp_path)

    assert any("Problem must contain" in error for error in errors)
    assert any("Decision must contain" in error for error in errors)
    assert any(
        "Alternatives considered must name what the decision beat" in error for error in errors
    )
    assert any("Consequences must contain" in error for error in errors)


def test_agent_notes_gate_accepts_named_alternatives(tmp_path: Path) -> None:
    """A complete note with a named rejected alternative passes the parser."""
    script = load_script("scripts/validate_agent_notes.py")
    note = tmp_path / "implemented" / "valid.md"
    note.parent.mkdir()
    note.write_text(
        "## Problem\nThe old path was ambiguous.\n"
        "## Decision\nUse one explicit path.\n"
        "## Alternatives considered\n"
        "1. **Reuse the caller's path**: rejected because it permits collisions.\n"
        "## Consequences\nThe path is scoped to the run.\n",
        encoding="utf-8",
    )

    assert script.validate_notes(tmp_path) == []
