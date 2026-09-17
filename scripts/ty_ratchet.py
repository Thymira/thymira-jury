"""Fail CI only when the number of ty diagnostics grows.

ty is pre-1.0 and the legacy code base has a known number of diagnostics. This script
turns that number into a ratchet: the count may go down, never up.

Usage:
    python scripts/ty_ratchet.py                   # compare against .ty-baseline.json
    python scripts/ty_ratchet.py --strict          # also fail if the baseline is stale (CI)
    python scripts/ty_ratchet.py --update-baseline # rewrite the baseline after a clean-up
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / ".ty-baseline.json"
DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>error|warning|info)\[(?P<rule>[a-z0-9-]+)\]"
)
SUMMARY = re.compile(r"^Found (?P<n>\d+) diagnostics?")


def find_ty() -> str:
    """Locate the ty executable next to the running interpreter, then on PATH."""
    candidates = [Path(sys.executable).with_name("ty"), Path(sys.executable).with_name("ty.exe")]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("ty")
    if found is None:
        msg = "ty was not found. Install the typecheck group: uv sync --group typecheck"
        raise SystemExit(msg)
    return found


def run_ty() -> tuple[int, str]:
    """Run `ty check` and return its exit code and combined output.

    The exit code is *not* irrelevant: ty exits non-zero both when it finds diagnostics (which
    must still be counted) and when it fails to run at all — a crash, a missing dependency, a
    usage error. The caller tells the two apart by whether the output parsed into any diagnostics;
    a ratchet that discarded the code would report green precisely when ty never ran.
    """
    completed = subprocess.run(
        [find_ty(), "check", "--output-format", "concise"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def parse_concise(text: str) -> dict[str, int]:
    """Count diagnostics per rule from ty's concise output."""
    counts: Counter[str] = Counter()
    summary_total: int | None = None
    for line in text.splitlines():
        match = DIAGNOSTIC.match(line.strip())
        if match:
            counts[match.group("rule")] += 1
            continue
        summary = SUMMARY.match(line.strip())
        if summary:
            summary_total = int(summary.group("n"))
    if not counts and summary_total:
        return {"<unparsed>": summary_total}
    return dict(sorted(counts.items()))


def compare(current: dict[str, int], baseline: dict, strict: bool) -> tuple[bool, str]:
    """Return (ok, human-readable message)."""
    current_total = sum(current.values())
    baseline_total = int(baseline.get("total", 0))
    baseline_rules: dict[str, int] = baseline.get("by_rule", {})
    lines = [f"ty diagnostics: {current_total} (baseline {baseline_total})"]
    for rule in sorted(set(current) | set(baseline_rules)):
        before, after = baseline_rules.get(rule, 0), current.get(rule, 0)
        if before != after:
            lines.append(f"  {rule}: {before} -> {after}")
    if current_total > baseline_total:
        lines.append("FAIL: diagnostics increased. Fix the new ones or, if they are pre-existing")
        lines.append(
            "      findings surfaced by a ty upgrade, run: "
            "python scripts/ty_ratchet.py --update-baseline"
        )
        return False, "\n".join(lines)
    if strict and current_total < baseline_total:
        lines.append(
            "FAIL (strict): the baseline is stale. Lower it with: "
            "python scripts/ty_ratchet.py --update-baseline"
        )
        return False, "\n".join(lines)
    if current_total < baseline_total:
        lines.append(
            "Diagnostics went down — consider: python scripts/ty_ratchet.py --update-baseline"
        )
    lines.append("OK")
    return True, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail when the baseline is stale")
    args = parser.parse_args(argv)

    returncode, output = run_ty()
    current = parse_concise(output)
    # Distinguish "ran and found problems" from "did not run at all". ty exits non-zero in both
    # cases, but a real diagnostics run always parses into at least the summary line, whereas a
    # crash or a missing dependency yields nothing to parse. Nothing-parsed on a non-zero exit is
    # a ty that could not check the code, and the fail-safe posture is to fail, not to write or
    # trust a baseline of zero. A clean run exits zero, so it never reaches this branch.
    if returncode != 0 and not current:
        print(
            f"FAIL: ty exited {returncode} but produced no parseable diagnostics — it did not run."
            "\nCheck the typecheck group is installed (uv sync --group typecheck). ty output:\n"
            f"{output.rstrip()}"
        )
        return 1
    if args.update_baseline:
        payload = {"total": sum(current.values()), "by_rule": current}
        args.baseline.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"Baseline written to {args.baseline}: {payload['total']} diagnostics")
        return 0
    if not args.baseline.exists():
        print(f"No baseline at {args.baseline}. Create it with --update-baseline")
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    ok, message = compare(current, baseline, strict=args.strict)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
