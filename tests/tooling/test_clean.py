from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path


def test_remove_tree_deletes_read_only_entries(tmp_path: Path) -> None:
    clean = load_script("scripts/clean.py")
    tree = tmp_path / ".pytest-tmp" / "repo" / ".git" / "objects"
    tree.mkdir(parents=True)
    blob = tree / "0f614940"
    blob.write_bytes(b"x")
    blob.chmod(stat.S_IREAD)  # what git leaves behind in fixture repositories

    clean.remove_tree(tmp_path / ".pytest-tmp")

    assert not (tmp_path / ".pytest-tmp").exists()
