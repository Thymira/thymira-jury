"""Remove caches and build artefacts (cross-platform replacement for `rm -rf`).

Also removes read-only files (git objects left by test fixtures under `.pytest-tmp`), which
`shutil.rmtree` refuses on Windows without help.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = [
    ".pytest_cache",
    ".pytest-tmp",
    ".ruff_cache",
    ".import_linter_cache",
    ".hypothesis",
    "build",
    "dist",
    "htmlcov",
]
FILES = [".coverage", "coverage.xml", "coverage.json"]


def _make_writable_and_retry(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """`shutil.rmtree` error hook: clear the read-only bit and retry the failed operation."""
    Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only entries."""
    shutil.rmtree(path, onexc=_make_writable_and_retry)


def main() -> int:
    """CLI entry point."""
    removed = 0
    for name in DIRECTORIES:
        path = REPO_ROOT / name
        if path.is_dir():
            remove_tree(path)
            removed += 1
    for name in FILES:
        path = REPO_ROOT / name
        if path.is_file():
            path.unlink()
            removed += 1
    for pycache in REPO_ROOT.rglob("__pycache__"):
        if ".venv" in pycache.parts or not pycache.is_dir():
            continue
        remove_tree(pycache)
        removed += 1
    print(f"Removed {removed} cache entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
