"""Run import-linter with a UTF-8 console so it works under Windows pipes.

import-linter renders progress with Rich (an emoji spinner). When stdout is a pipe on
Windows — pre-commit, CI logs, editor terminals — the console encoding is cp1252 and the
run crashes with ``UnicodeEncodeError`` before any contract is checked. This wrapper forces
UTF-8 and forwards every argument to ``lint-imports``.

Usage:
    python scripts/lint_imports.py            # same as `lint-imports`
    python scripts/lint_imports.py --verbose  # any lint-imports flag is forwarded
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def lint_imports_executable() -> str:
    """Locate `lint-imports` next to the running interpreter, then on PATH."""
    for name in ("lint-imports", "lint-imports.exe"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.exists():
            return str(candidate)
    found = shutil.which("lint-imports")
    if found is None:
        msg = "lint-imports was not found. Install the lint group: uv sync --group lint"
        raise SystemExit(msg)
    return found


def utf8_environment() -> dict[str, str]:
    """Return the current environment with Python forced into UTF-8 mode."""
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = sys.argv[1:] if argv is None else argv
    completed = subprocess.run(
        [lint_imports_executable(), *args], env=utf8_environment(), check=False
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
