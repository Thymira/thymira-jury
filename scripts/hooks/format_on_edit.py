"""Claude Code PostToolUse hook: format the Python file that was just edited.

Reads the hook payload from stdin, and if `tool_input.file_path` is a `.py` file inside the
repository (or FORMAT_ON_EDIT_ROOT), runs `ruff check --fix` and `ruff format` on it. Always
exits 0 so a formatting problem never blocks the agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def target_file(payload: dict, root: Path) -> Path | None:
    """The Python file the hook payload refers to, or None when it is not a .py file."""
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw or not str(raw).endswith(".py"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def ruff_command() -> list[str]:
    """The ruff invocation that formats and fixes one file."""
    local = Path(sys.executable).with_name("ruff.exe" if os.name == "nt" else "ruff")
    if local.exists():
        return [str(local)]
    found = shutil.which("ruff")
    return [found] if found else ["uv", "run", "ruff"]


def main(stdin_text: str | None = None) -> int:
    """CLI entry point."""
    root = Path(os.environ.get("FORMAT_ON_EDIT_ROOT", REPO_ROOT))
    try:
        payload = json.loads(stdin_text if stdin_text is not None else sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    path = target_file(payload, root)
    if path is None:
        return 0
    ruff = ruff_command()
    for args in (["check", "--fix", "--quiet", str(path)], ["format", "--quiet", str(path)]):
        subprocess.run([*ruff, *args], cwd=root, check=False, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
