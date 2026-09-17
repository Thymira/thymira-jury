"""Tests for the machine checked package and relationship inventory."""

from __future__ import annotations

import json
from pathlib import Path

from tests.tooling.conftest import load_script


def test_package_inventory_matches_workspace_and_checker_sources() -> None:
    """The checked in inventory resolves every current distribution and A30 checker."""
    script = load_script("scripts/check_package_invariants.py")
    root = Path(__file__).resolve().parents[2]
    assert script.validate_manifest(root, root / "docs/governance/package-invariants.json") == []


def test_package_inventory_rejects_a_fictional_checker(tmp_path: Path) -> None:
    """An implemented relationship cannot point to an import that is not in the workspace."""
    script = load_script("scripts/check_package_invariants.py")
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "docs/governance/package-invariants.json").read_text(encoding="utf-8")
    )
    a30 = next(entry for entry in manifest["packages"] if entry["distribution"] == "thymira-tools")
    a30["checker_import"] = "thymira.mira.checks.missing"
    path = tmp_path / "package-invariants.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = script.validate_manifest(root, path)
    assert any("checker path" in error for error in errors)


def test_package_inventory_rejects_an_unknown_owner(tmp_path: Path) -> None:
    """Package ownership uses the P1..P5 enum rather than arbitrary prose."""
    script = load_script("scripts/check_package_invariants.py")
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "docs/governance/package-invariants.json").read_text(encoding="utf-8")
    )
    tools = next(
        entry for entry in manifest["packages"] if entry["distribution"] == "thymira-tools"
    )
    tools["owner"] = "not-an-owner"
    path = tmp_path / "package-invariants.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = script.validate_manifest(root, path)
    assert any("owner" in error for error in errors)


def test_package_inventory_rejects_empty_scoped_absences(tmp_path: Path) -> None:
    """The inventory must retain the explicit source placeholder set."""
    script = load_script("scripts/check_package_invariants.py")
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "docs/governance/package-invariants.json").read_text(encoding="utf-8")
    )
    manifest["scoped_absences"] = []
    path = tmp_path / "package-invariants.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = script.validate_manifest(root, path)
    assert any("scoped_absences" in error for error in errors)
