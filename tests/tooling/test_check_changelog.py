from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = ".agents/skills/changelog/scripts/check_changelog.py"


def git(repo: Path, *args: str) -> None:
    """Run a git command in `repo` with a fixed identity and no signing."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    """Write `files` (path -> content) under `repo`, stage everything and commit."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, text=True)
    # A base commit so `HEAD~1` always resolves.
    commit(repo, {"README.md": "start\n"}, "chore: init")
    return repo


def test_changed_files_lists_changes_since_base(tmp_path):
    check = load_script(SCRIPT)
    repo = init_repo(tmp_path)
    commit(repo, {"runtime/core/src/thymira/core/x.py": "x = 1\n"}, "feat: add x")
    changed = check.changed_files(repo, "HEAD~1")
    assert "runtime/core/src/thymira/core/x.py" in changed
    assert "README.md" not in changed


def test_needs_entry_true_for_src_false_for_others():
    check = load_script(SCRIPT)
    assert check.needs_entry(["runtime/core/src/thymira/core/cli.py"]) is True
    assert check.needs_entry(["docs/x.md", "tests/test_x.py", "scripts/y.py"]) is False
    assert check.needs_entry([]) is False


def test_main_fails_when_src_changed_without_changelog(tmp_path):
    check = load_script(SCRIPT)
    repo = init_repo(tmp_path)
    commit(repo, {"runtime/core/src/thymira/core/x.py": "x = 1\n"}, "feat: add x")
    assert check.main(["--repo", str(repo), "--base", "HEAD~1"]) == 1


def test_main_passes_when_changelog_also_changed(tmp_path):
    check = load_script(SCRIPT)
    repo = init_repo(tmp_path)
    commit(
        repo,
        {
            "runtime/core/src/thymira/core/x.py": "x = 1\n",
            "CHANGELOG.md": "## [Unreleased]\n### Added\n- x\n",
        },
        "feat: add x",
    )
    assert check.main(["--repo", str(repo), "--base", "HEAD~1"]) == 0


def test_main_passes_with_skip_token(tmp_path):
    check = load_script(SCRIPT)
    repo = init_repo(tmp_path)
    commit(repo, {"runtime/core/src/thymira/core/x.py": "x = 1\n"}, "feat: add x [skip changelog]")
    assert check.main(["--repo", str(repo), "--base", "HEAD~1"]) == 0


def test_main_passes_when_only_non_src_changed(tmp_path):
    check = load_script(SCRIPT)
    repo = init_repo(tmp_path)
    commit(repo, {"docs/guide.md": "hello\n"}, "docs: guide")
    assert check.main(["--repo", str(repo), "--base", "HEAD~1"]) == 0
