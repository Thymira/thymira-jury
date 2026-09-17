from __future__ import annotations

from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path


def make_skill(root: Path, name: str, body: str = "# Demo\n") -> Path:
    skill = root / name
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Demo skill.\n---\n{body}", encoding="utf-8"
    )
    (skill / "scripts" / "tool.py").write_text("print('hi')\n", encoding="utf-8")
    return skill


def test_sync_copies_skills_and_reports_in_sync(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    make_skill(source, "beta")
    assert sync_skills.sync(source, target, check=False) == []
    content = (target / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---\nname: alpha")
    assert (target / "beta" / "scripts" / "tool.py").exists()
    assert sync_skills.sync(source, target, check=True) == []


def test_check_reports_drift_and_stale_targets(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    sync_skills.sync(source, target, check=False)
    (target / "alpha" / "SKILL.md").write_text("tampered", encoding="utf-8")
    make_skill(target, "stale")
    drift = sync_skills.sync(source, target, check=True)
    assert any("alpha/SKILL.md" in item for item in drift)
    assert any("stale" in item for item in drift)
    # a real sync repairs both
    assert sync_skills.sync(source, target, check=False) == []
    assert not (target / "stale").exists()
    assert sync_skills.sync(source, target, check=True) == []


def test_pycache_is_not_copied(tmp_path):
    sync_skills = load_script("scripts/sync_skills.py")
    source, target = tmp_path / "src", tmp_path / "dst"
    make_skill(source, "alpha")
    (source / "alpha" / "scripts" / "__pycache__").mkdir()
    (source / "alpha" / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"")
    sync_skills.sync(source, target, check=False)
    assert not (target / "alpha" / "scripts" / "__pycache__").exists()
