"""Drive ``git bisect run`` to find the commit that first broke a failing test.

Given a known-good ref, a known-bad ref (default ``HEAD``) and the failing test node(s),
this automates the whole bisect: it starts the bisect, hands ``git`` a ``pytest`` command
to score each revision, and *always* runs ``git bisect reset`` at the end -- even when a
step fails -- so the working tree is never left mid-bisect.

Usage:
    python bisect_test.py <good> <bad> -- <pytest args>
    python bisect_test.py v0.1.0 HEAD -- tests/test_x.py::test_y
    python bisect_test.py v0.1.0 HEAD --dry-run -- tests/test_x.py   # print, run nothing

Reach for this only when the suspect range is more than ~5 commits; for a short range,
read the diff instead.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def build_commands(good: str, bad: str, pytest_args: list[str]) -> list[list[str]]:
    """Return the three git commands: start (bad, good), run (pytest scorer), reset."""
    return [
        ["git", "bisect", "start", bad, good],
        ["git", "bisect", "run", sys.executable, "-m", "pytest", "-x", "-q", *pytest_args],
        ["git", "bisect", "reset"],
    ]


def run_commands(commands: list[list[str]]) -> int:
    """Run every command except the last, then always run the last (``git bisect reset``)."""
    *steps, reset = commands
    try:
        for command in steps:
            subprocess.run(command, check=False)
    finally:
        # Reset even on Ctrl-C or a raised error: a half-finished bisect poisons the tree.
        subprocess.run(reset, check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    raw = list(sys.argv[1:] if argv is None else argv)
    pytest_args: list[str] = []
    if "--" in raw:
        cut = raw.index("--")
        raw, pytest_args = raw[:cut], raw[cut + 1 :]
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("good", help="last ref known to PASS the test")
    parser.add_argument("bad", nargs="?", default="HEAD", help="a ref known to FAIL (default HEAD)")
    parser.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    args = parser.parse_args(raw)
    if not pytest_args:
        parser.error(
            "pass the failing test after `--`, e.g. "
            "`bisect_test.py v0.1.0 HEAD -- tests/test_x.py::test_y`"
        )
    commands = build_commands(args.good, args.bad, pytest_args)
    if args.dry_run:
        for command in commands:
            print(" ".join(command))
        return 0
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
