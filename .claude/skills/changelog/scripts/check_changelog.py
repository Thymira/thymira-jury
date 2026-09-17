"""Fail a change that touches runtime code without recording a user-facing CHANGELOG entry.

The changelog is for users; every branch that changes runtime code (a workspace member under
`packages/`, `runtime/`, `apps/` or `adapters/`) must either add a
line under `## [Unreleased]` in CHANGELOG.md or opt out explicitly with `[skip changelog]` in the
commit message. This script is the mechanical gate behind that rule, for local use and CI.

Usage:
    python check_changelog.py                       # compare HEAD against origin/main
    python check_changelog.py --base HEAD~1         # compare against another ref
    python check_changelog.py --repo /path --json   # machine-readable verdict
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

# Any file below one of these prefixes is user-facing and needs a changelog line.
SRC_PREFIXES = ("packages/", "runtime/", "apps/", "adapters/")
# Commit-message marker that opts a branch out of the changelog requirement.
SKIP_MARKER = "[skip changelog]"


def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo` and return its stdout (raises on non-zero exit)."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def changed_files(repo: Path, base_ref: str) -> list[str]:
    """Return the repo-relative paths changed on HEAD since it diverged from `base_ref`.

    Uses the three-dot form so the comparison is against the merge base (what this branch
    changed), which matches how CI compares a feature branch to `origin/main`.
    """
    out = _git(repo, "diff", "--name-only", f"{base_ref}...HEAD")
    return [line.strip() for line in out.splitlines() if line.strip()]


def needs_entry(changed: list[str]) -> bool:
    """True when any changed path is user-facing runtime code (a workspace member)."""
    return any(path.startswith(SRC_PREFIXES) for path in changed)


def last_commit_message(repo: Path, ref: str = "HEAD") -> str:
    """Return the full message (subject + body) of `ref`."""
    return _git(repo, "log", "-1", "--format=%B", ref)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", type=Path, default=Path(), help="path to the git repository")
    parser.add_argument("--base", default="origin/main", help="ref to compare against")
    parser.add_argument("--changelog", default="CHANGELOG.md", help="changelog path")
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    args = parser.parse_args(argv)

    try:
        message = last_commit_message(args.repo, "HEAD")
        changed = changed_files(args.repo, args.base)
    except subprocess.CalledProcessError as exc:
        # A missing base ref (e.g. origin/main not fetched) must not silently block a merge;
        # report it and pass so the escape hatch is a fetch, not a forced empty commit.
        detail = (exc.stderr or "").strip() or exc
        print(f"check_changelog: cannot compare against {args.base!r} in {args.repo} ({detail}).")
        print("Fetch the base ref (git fetch origin) or pass --base <ref>. Skipping the gate.")
        return 0

    skipped = SKIP_MARKER in message
    required = needs_entry(changed)
    touched = args.changelog in changed
    ok = skipped or not required or touched

    if args.json:
        verdict = {
            "needs_entry": required,
            "changelog_touched": touched,
            "skipped": skipped,
            "ok": ok,
        }
        print(json.dumps(verdict))
    elif ok:
        if skipped and required:
            print(f"check_changelog: OK ({SKIP_MARKER} in the commit message).")
        elif required:
            print(f"check_changelog: OK ({args.changelog} was updated).")
        else:
            print("check_changelog: OK (no user-facing runtime change).")
    else:
        src_hits = [p for p in changed if p.startswith(SRC_PREFIXES)]
        print(
            f"check_changelog: {len(src_hits)} runtime file(s) changed, {args.changelog} was not:"
        )
        for path in src_hits:
            print(f"  - {path}")
        print(f"Add a line under '## [Unreleased]' in {args.changelog}, or put")
        print(f"'{SKIP_MARKER}' in the commit message if the change is not user-visible.")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
