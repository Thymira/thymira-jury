"""Copy canonical skills from .agents/skills into .claude/skills.

Codex, Cursor and Gemini CLI read `.agents/skills/` natively; Claude Code only reads
`.claude/skills/`. Symlinks are not an option on Windows, so the directory is copied and
the copy is committed. Run `--check` (pre-commit, CI) to fail when the copy drifted.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / ".agents" / "skills"
TARGET = REPO_ROOT / ".claude" / "skills"
IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
README = (
    "# Generated directory\n\n"
    "These skills are copied from `.agents/skills/` by `scripts/sync_skills.py`.\n"
    "Edit the canonical copy and run `just sync-skills`; do not edit files here.\n"
)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_DIRS or name.endswith(".pyc")}


def _skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def _compare_tree(source: Path, target: Path, prefix: str) -> list[str]:
    """Return differences between two skill directories as 'prefix/relative: reason'."""
    drift: list[str] = []
    comparison = filecmp.dircmp(source, target, ignore=list(IGNORED_DIRS))
    drift.extend(
        f"{prefix}/{name}: missing in target"
        for name in comparison.left_only
        if not name.endswith(".pyc")
    )
    drift.extend(
        f"{prefix}/{name}: unexpected in target"
        for name in comparison.right_only
        if not name.endswith(".pyc")
    )
    drift.extend(f"{prefix}/{name}: content differs" for name in comparison.diff_files)
    for sub in comparison.common_dirs:
        drift.extend(_compare_tree(source / sub, target / sub, f"{prefix}/{sub}"))
    return drift


def sync(source: Path, target: Path, check: bool) -> list[str]:
    """Mirror `source` into `target`. With check=True, only report drift."""
    drift: list[str] = []
    source_names = {p.name for p in _skill_dirs(source)}
    target.mkdir(parents=True, exist_ok=True)
    for stale in sorted(p for p in target.iterdir() if p.is_dir() and p.name not in source_names):
        if check:
            drift.append(f"{stale.name}: stale, not present in {source}")
        else:
            shutil.rmtree(stale)
    for skill in _skill_dirs(source):
        destination = target / skill.name
        if check:
            if not destination.is_dir():
                drift.append(f"{skill.name}: missing in target")
            else:
                drift.extend(_compare_tree(skill, destination, skill.name))
            continue
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill, destination, ignore=_ignore)
    if not check:
        (target / "README.md").write_text(README, encoding="utf-8", newline="\n")
    return drift


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--target", type=Path, default=TARGET)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)
    drift = sync(args.source, args.target, check=args.check)
    if args.check:
        if drift:
            print("Skill copies are out of date:")
            print("\n".join(f"  - {item}" for item in drift))
            print("Run: just sync-skills  (or: python scripts/sync_skills.py)")
            return 1
        print("Skill copies are in sync")
        return 0
    print(f"Synced {len(_skill_dirs(args.source))} skill(s) into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
