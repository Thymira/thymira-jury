"""Validate the package and relationship inventory without importing runtime producers."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "governance" / "package-invariants.json"
_PACKAGE_ROOTS = ("packages", "runtime", "apps", "adapters")
_STATUSES = {"implemented", "pending", "scoped-absence"}
_OWNER_RE = re.compile(r"^P[1-5](?:/P[1-5])*$")
_EXPECTED_SCOPED_ABSENCE_PATHS = frozenset(
    {
        "adapters/vscode",
        "adapters/opencode",
        "adapters/claude-code",
        "adapters/codex",
        "adapters/generic",
        "packages/sdk-*",
        "services/*",
        "infrastructure/*",
    }
)


def _workspace_packages(root: Path) -> dict[str, tuple[str, Path]]:
    """Read each workspace distribution and its declared import module from TOML."""
    packages: dict[str, tuple[str, Path]] = {}
    for directory in _PACKAGE_ROOTS:
        for pyproject in sorted((root / directory).glob("*/pyproject.toml")):
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project")
            backend = data.get("tool", {}).get("uv", {}).get("build-backend")
            if not isinstance(project, dict) or not isinstance(backend, dict):
                continue
            distribution = project.get("name")
            build = data.get("tool", {}).get("uv", {}).get("build-backend", {})
            import_name = build.get("module-name") if isinstance(build, dict) else None
            if isinstance(distribution, str) and isinstance(import_name, str):
                packages[distribution] = (import_name, pyproject)
    return packages


def _module_path(pyproject: Path, import_name: str) -> Path:
    """Resolve an import module to its source file without importing it."""
    package_name = import_name.removeprefix("thymira.").replace(".", "/")
    return pyproject.parent / "src" / "thymira" / f"{package_name}.py"


def _callable_exists(path: Path, name: str) -> bool:
    """Return whether a source module defines a top-level function or class of ``name``."""
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == name
        for node in tree.body
    )


def _entry_errors(entry: dict[str, Any], packages: dict[str, tuple[str, Path]]) -> list[str]:
    """Validate one manifest package row against its real pyproject metadata."""
    errors: list[str] = []
    distribution = entry.get("distribution")
    import_name = entry.get("import")
    status = entry.get("status")
    if not isinstance(distribution, str) or distribution not in packages:
        return [f"unknown distribution: {distribution!r}"]
    actual_import, _ = packages[distribution]
    if import_name != actual_import:
        errors.append(
            f"{distribution}: import {import_name!r} does not match pyproject {actual_import!r}"
        )
    if status not in _STATUSES:
        errors.append(f"{distribution}: invalid status {status!r}")
    errors.extend(
        f"{distribution}: {field} must be a non-empty string"
        for field in ("owner", "registry", "checker", "invariant")
        if not isinstance(entry.get(field), str) or not entry[field].strip()
    )
    owner = entry.get("owner")
    if isinstance(owner, str) and (
        _OWNER_RE.fullmatch(owner) is None
        or len(set(owner.split("/"))) != len(owner.split("/"))
        or owner.split("/") != sorted(owner.split("/"))
    ):
        errors.append(f"{distribution}: owner must be an ordered P1..P5 enum value")
    if status == "scoped-absence" and not isinstance(entry.get("reason"), str):
        errors.append(f"{distribution}: scoped absence must name a reason")
    if status == "pending" and not isinstance(entry.get("reason"), str):
        errors.append(f"{distribution}: pending claim must name a reason")
    return errors


def _relationship_errors(entry: dict[str, Any], packages: dict[str, tuple[str, Path]]) -> list[str]:
    """Validate a relationship checker location when its implementation is claimed."""
    if entry.get("status") != "implemented" or "relationship_id" not in entry:
        return []
    checker = entry.get("checker")
    checker_import = entry.get("checker_import")
    checker_name = entry.get("checker_name")
    independent = entry.get("independent_checker")
    if (
        not isinstance(checker, str)
        or not isinstance(checker_import, str)
        or not isinstance(checker_name, str)
        or not isinstance(independent, str)
    ):
        return [
            f"{entry.get('distribution')}: implemented relationship checker metadata is incomplete"
        ]
    if checker != f"{checker_import}.{checker_name}":
        return [f"{entry.get('distribution')}: checker path does not match its module and name"]

    if "." not in independent:
        return [f"{entry.get('distribution')}: independent checker path is malformed"]
    independent_import, independent_name = independent.rsplit(".", 1)
    locations = [(checker_import, checker_name), (independent_import, independent_name)]
    errors: list[str] = []
    for module_import, callable_name in locations:
        checker_distribution = next(
            (
                distribution
                for distribution, (import_name, _) in packages.items()
                if module_import == import_name or module_import.startswith(import_name + ".")
            ),
            None,
        )
        if checker_distribution is None:
            errors.append(
                f"{entry.get('distribution')}: checker import {module_import!r} is not a "
                "workspace package"
            )
            continue
        _, pyproject = packages[checker_distribution]
        module = _module_path(pyproject, module_import)
        if not _callable_exists(module, callable_name):
            errors.append(
                f"{entry.get('distribution')}: checker {module_import}.{callable_name} "
                f"is not defined in {module}"
            )
    return errors


def validate_manifest(root: Path = REPO_ROOT, manifest: Path = DEFAULT_MANIFEST) -> list[str]:
    """Return package inventory defects; an empty list means the inventory is exact."""
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read package inventory: {exc}"]
    if not isinstance(payload, dict) or payload.get("schema") != "thymira.package-invariants/1":
        return ["package inventory has an unknown schema"]
    entries = payload.get("packages")
    if not isinstance(entries, list):
        return ["package inventory packages must be a list"]
    packages = _workspace_packages(root)
    errors: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("package inventory entry must be an object")
            continue
        distribution = entry.get("distribution")
        if distribution in seen:
            errors.append(f"duplicate distribution: {distribution}")
        if isinstance(distribution, str):
            seen.add(distribution)
        errors.extend(_entry_errors(entry, packages))
        errors.extend(_relationship_errors(entry, packages))
    errors.extend(
        f"workspace distribution missing from inventory: {distribution}"
        for distribution in sorted(set(packages) - seen)
    )
    errors.extend(
        f"inventory distribution is not a workspace package: {distribution}"
        for distribution in sorted(seen - set(packages))
    )
    absences = payload.get("scoped_absences")
    if (
        not isinstance(absences, list)
        or not absences
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not item.get("path", "").strip()
            or not isinstance(item.get("reason"), str)
            or not item.get("reason", "").strip()
            for item in absences
        )
    ):
        errors.append("scoped_absences must name every absent path and reason")
    elif {
        path.strip() for item in absences for path in item["path"].split(",") if path.strip()
    } != _EXPECTED_SCOPED_ABSENCE_PATHS:
        errors.append("scoped_absences must match the repository's expected placeholder members")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Validate the package inventory from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    errors = validate_manifest(REPO_ROOT, args.manifest)
    for error in errors:
        print(error)
    if errors:
        return 1
    print("package/invariant inventory OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
