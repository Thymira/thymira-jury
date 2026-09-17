"""Validate agent skills against the Agent Skills specification (agentskills.io).

Checks every `<dir>/SKILL.md`: frontmatter delimiters on the very first line, the
`name`/`description` constraints, the portable key allowlist, body length, referenced
files and forward-slash paths. Exit code 1 when any skill is invalid.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRS = [REPO_ROOT / ".agents" / "skills"]
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
BLOCK_SCALAR_RE = re.compile(r"^[>|][+-]?$")
RESERVED = ("anthropic", "claude")
ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
MAX_BODY_LINES = 500
# The path must END on a word character or a hyphen, so a reference that closes a sentence --
# "see references/guide.md." -- does not capture the full stop as part of the filename. Windows
# hides that bug rather than surfacing it: the Win32 API strips trailing dots from a path, so
# `Path("references/guide.md.").exists()` is True there and False on Linux, and a local
# `just validate-skills` passed while CI failed.
REFERENCE_RE = re.compile(r"(?<![\w/])((?:scripts|references|assets)/[\w./-]*[\w-])")
XML_TAG_RE = re.compile(r"<[^>]+>")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the portable YAML subset used by SKILL.md files.

    Supports `key: value`, block scalars (`description: >-`, `key: |`), block sequences
    (`allowed-tools:` followed by `- item` lines) and nested mappings (`metadata:` followed
    by indented `key: value` lines) — the shapes skills are actually written in, including
    third-party ones. Returns (frontmatter, body). Raises ValueError when the delimiters are
    missing or a line is malformed.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        msg = "SKILL.md must start with '---' on the first line"
        raise ValueError(msg)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        msg = "closing '---' for the frontmatter not found"
        raise ValueError(msg) from exc
    return _parse_mapping(lines[1:end]), "\n".join(lines[end + 1 :])


def _parse_mapping(lines: list[str]) -> dict:
    """Read the top-level `key: …` entries of one block, each with its indented body."""
    data: dict = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):
            msg = f"unexpected indented line in frontmatter: {raw!r}"
            raise ValueError(msg)
        key, sep, value = raw.partition(":")
        if not sep:
            msg = f"malformed frontmatter line: {raw!r}"
            raise ValueError(msg)
        block, index = _take_block(lines, index)
        data[key.strip()] = _parse_value(value.strip(), block)
    return data


def _take_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """Return the indented (and interleaved blank) lines belonging to the entry at `start`."""
    index = start
    while index < len(lines) and (not lines[index].strip() or lines[index].startswith((" ", "\t"))):
        index += 1
    while index > start and not lines[index - 1].strip():
        index -= 1
    return lines[start:index], index


def _parse_value(value: str, block: list[str]) -> str | list | dict:
    """Combine one entry's inline value and indented block into a scalar, list or mapping."""
    if BLOCK_SCALAR_RE.match(value):
        return _fold(_dedent(block), literal=value.startswith("|"))
    if value:
        if block:
            msg = f"unexpected indented line in frontmatter: {block[0]!r}"
            raise ValueError(msg)
        return _unquote(value)
    return _parse_block(block)


def _parse_block(block: list[str]) -> list | dict:
    """Read an indented block as a sequence (`- item`) or, recursively, a nested mapping."""
    body = _dedent(block)
    items = [line for line in body if line.strip()]
    if items and items[0].startswith("- "):
        return [_unquote(line[2:]) for line in items if line.startswith("- ")]
    return _parse_mapping(body)


def _fold(body: list[str], *, literal: bool) -> str:
    """Join a block scalar: `|` keeps the line breaks, `>` folds them into single spaces."""
    if literal:
        return "\n".join(body).strip("\n")
    return " ".join(line.strip() for line in body if line.strip())


def _dedent(block: list[str]) -> list[str]:
    """Strip the block's common leading indentation, keeping blank lines blank."""
    filled = [line for line in block if line.strip()]
    if not filled:
        return []
    indent = min(len(line) - len(line.lstrip()) for line in filled)
    return [line[indent:] if line.strip() else "" for line in block]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def validate_skill(skill_dir: Path) -> list[str]:  # noqa: PLR0912  # one branch per frontmatter rule
    """Return a list of human-readable errors for one skill directory (empty = valid)."""
    errors: list[str] = []
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return [f"{skill_dir.name}: SKILL.md is missing"]
    text = skill_file.read_text(encoding="utf-8")
    try:
        meta, body = parse_frontmatter(text)
    except ValueError as exc:
        return [f"{skill_dir.name}: {exc}"]

    name = str(meta.get("name", ""))
    if not name:
        errors.append(f"{skill_dir.name}: 'name' is required")
    else:
        if not NAME_RE.match(name):
            errors.append(
                f"{skill_dir.name}: name {name!r} must match {NAME_RE.pattern}"
                " (lowercase, hyphens, no '--')"
            )
        if len(name) > 64:
            errors.append(f"{skill_dir.name}: name longer than 64 characters")
        if name != skill_dir.name:
            errors.append(f"{skill_dir.name}: {name!r} must equal the directory name")
        if any(word in name for word in RESERVED):
            reserved = ", ".join(RESERVED)
            errors.append(f"{skill_dir.name}: name contains a reserved word ({reserved})")

    description = str(meta.get("description", "")).strip()
    if not description:
        errors.append(f"{skill_dir.name}: 'description' is required and must be non-empty")
    elif len(description) > 1024:
        errors.append(f"{skill_dir.name}: description longer than 1024 characters")
    if XML_TAG_RE.search(description) or XML_TAG_RE.search(name):
        errors.append(f"{skill_dir.name}: name/description must not contain XML tags")

    unknown = sorted(set(meta) - ALLOWED_KEYS)
    if unknown:
        errors.append(f"{skill_dir.name}: non-portable frontmatter key(s): {', '.join(unknown)}")
    compatibility = meta.get("compatibility")
    if compatibility and len(str(compatibility)) > 500:
        errors.append(f"{skill_dir.name}: compatibility longer than 500 characters")

    body_lines = body.count("\n") + 1
    if body_lines > MAX_BODY_LINES:
        errors.append(
            f"{skill_dir.name}: body has {body_lines} lines; keep SKILL.md under {MAX_BODY_LINES}"
        )

    if re.search(r"(scripts|references|assets)\\", body):
        errors.append(f"{skill_dir.name}: use forward slashes in paths, not backslashes")
    errors.extend(
        f"{skill_dir.name}: referenced file does not exist: {reference}"
        for reference in sorted(set(REFERENCE_RE.findall(body)))
        if not (skill_dir / reference).exists()
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, default=DEFAULT_DIRS, help="skill roots to validate"
    )
    args = parser.parse_args(argv)
    valid = invalid = 0
    for root in args.paths:
        skill_dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        if not skill_dirs:
            print(f"{root}: no skill directories found")
            invalid += 1
        for skill_dir in skill_dirs:
            errors = validate_skill(skill_dir)
            if errors:
                invalid += 1
                print(f"FAIL {skill_dir}")
                print("\n".join(f"  - {e}" for e in errors))
            else:
                valid += 1
    print(f"{valid} skill(s) valid, {invalid} invalid")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
