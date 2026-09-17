"""Validate the fixed format and lifecycle placement of F12 Agent Notes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_HEADINGS = ("Problem", "Decision", "Alternatives considered", "Consequences")
LIFECYCLES = ("proposed", "implemented", "rejected", "archived")
HEADING_RE = re.compile(r"^## (?P<title>.+?)\s*$")
MARKDOWN_PREFIX_RE = re.compile(r"^(?:>\s*|[-+*]\s+|\d+[.)]\s+)")
MARKDOWN_EMPHASIS_RE = re.compile(r"^(?P<delimiter>\*\*|__|\*|_)(?P<content>.+?)(?P=delimiter)$")
ALTERNATIVES_MARKER = "A decision recorded without what it beat invites re-litigation"
ALTERNATIVES_PLACEHOLDERS = frozenset(
    {
        "none",
        "n/a",
        "na",
        "not applicable",
        "no alternatives considered",
        "no alternatives were considered",
    }
)


def validate_notes(notes_root: Path) -> list[str]:
    """Return violations found in lifecycle-scoped Agent Notes."""
    errors: list[str] = []
    if not notes_root.is_dir():
        return [f"missing Agent Notes directory: {notes_root}"]
    for lifecycle in LIFECYCLES:
        directory = notes_root / lifecycle
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            errors.extend(_validate_note(path))
    return errors


def _validate_note(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    headings = [match.group("title") for line in lines if (match := HEADING_RE.match(line))]
    errors: list[str] = []
    if headings != list(REQUIRED_HEADINGS):
        errors.append(
            f"{path}: headings must be exactly "
            + ", ".join(f"## {heading}" for heading in REQUIRED_HEADINGS)
        )
    sections = _section_bodies(lines)
    errors.extend(
        f"{path}: {heading} must contain non-whitespace text"
        for heading in REQUIRED_HEADINGS
        if not any(line.strip() for line in sections.get(heading, ()))
    )

    alternatives = [line.strip() for line in sections.get("Alternatives considered", ())]
    meaningful_alternatives = [
        line for line in alternatives if line and not _is_alternative_placeholder(line)
    ]
    if not meaningful_alternatives:
        errors.append(f"{path}: Alternatives considered must name what the decision beat")
    return errors


def _section_bodies(lines: list[str]) -> dict[str, list[str]]:
    """Return text grouped under each level-two heading."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            current = match.group("title")
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def _is_alternative_placeholder(line: str) -> bool:
    """Return whether a line is only generic alternatives boilerplate."""
    normalized = _normalize_alternative(line)
    marker = _normalize_alternative(ALTERNATIVES_MARKER)
    return normalized == marker or normalized in ALTERNATIVES_PLACEHOLDERS


def _normalize_alternative(line: str) -> str:
    """Normalize Markdown wrappers before comparing alternatives placeholders."""
    normalized = " ".join(line.split())
    while True:
        previous = normalized
        normalized = MARKDOWN_PREFIX_RE.sub("", normalized, count=1)
        normalized = MARKDOWN_EMPHASIS_RE.sub(r"\g<content>", normalized, count=1)
        normalized = normalized.strip()
        if normalized == previous:
            break
    return normalized.casefold().rstrip(".")


def main() -> int:
    """Run the Agent Notes gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = validate_notes(args.root / ".agents" / "notes")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Agent Notes: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
