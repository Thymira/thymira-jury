r"""Preview a package's first-segment import graph and flag layer violations.

This is the teaching companion to ``just check-imports`` (import-linter). It builds the
same "who imports whom" picture the layered contract enforces, so you can see a broken
edge before running the real contract checker. import-linter stays the source of truth;
this script never edits configuration and takes the package root as an argument, so it
works for any ``thymira`` workspace member (point it at the package directory).

Usage:
    python import_graph.py runtime/mira/src/thymira/mira
    python import_graph.py runtime/policies/src/thymira/policies --fail-on-violation \
        --layers "gate;engine;loader;models"
    python import_graph.py runtime/thy/src/thymira/thy --json
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

# Directories that never hold source we should attribute to a layer.
SKIP_DIRS = {"__pycache__", ".venv"}


def _node_for_file(py_file: Path, package_root: Path) -> str | None:
    """Return the module's first path segment under the package (its layer), or None."""
    parts = py_file.relative_to(package_root).parts
    first = parts[0].removesuffix(".py")
    if first == "__init__":  # the package's own __init__, not a sub-module
        return None
    return first


def _package_parts(py_file: Path, package_root: Path) -> list[str]:
    """Dotted parts that a level-1 relative import inside this file resolves against."""
    rel = py_file.relative_to(package_root).parts
    # Drop the file component: a module drops its own name; __init__.py is its package.
    return [package_root.name, *rel[:-1]]


def _strip_namespace(parts: list[str], package_name: str) -> list[str]:
    """Drop a namespace prefix so ``ns.pkg.sub`` and ``pkg.sub`` both start at ``pkg``."""
    if package_name in parts:
        return parts[parts.index(package_name) :]
    return parts


def _targets_from_node(node: ast.AST, package_name: str, package_parts: list[str]) -> list[str]:
    """Return the in-package layer nodes a single import statement points at."""
    if isinstance(node, ast.Import):
        targets: list[str] = []
        for alias in node.names:
            parts = _strip_namespace(alias.name.split("."), package_name)
            if len(parts) >= 2 and parts[0] == package_name:
                targets.append(parts[1])
        return targets
    if isinstance(node, ast.ImportFrom):
        if node.level == 0:
            # Absolute imports are fully qualified in a namespace package
            # (`from thymira.policies.models import X`): drop anything before the member.
            base = _strip_namespace(node.module.split(".") if node.module else [], package_name)
        else:
            # Relative import: climb `level - 1` packages up from the containing package.
            anchor = package_parts[: len(package_parts) - (node.level - 1)]
            base = [*anchor, *(node.module.split(".") if node.module else [])]
        if not base or base[0] != package_name:
            return []
        if len(base) >= 2:  # `from pkg.sub[...] import x` -> the sub-module is the layer
            return [base[1]]
        return [alias.name for alias in node.names]  # `from pkg import x` -> x is a sub-module
    return []


def build_graph(package_root: Path) -> set[tuple[str, str]]:
    """Return the set of (importer_layer, imported_layer) edges inside a package."""
    package_root = Path(package_root)
    package_name = package_root.name
    edges: set[tuple[str, str]] = set()
    for py_file in sorted(package_root.rglob("*.py")):
        if SKIP_DIRS.intersection(py_file.parts):
            continue
        source = _node_for_file(py_file, package_root)
        if source is None:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        package_parts = _package_parts(py_file, package_root)
        edges.update(
            (source, target)
            for node in ast.walk(tree)
            for target in _targets_from_node(node, package_name, package_parts)
            if target != source
        )
    return edges


def violations(edges: set[tuple[str, str]], layers: list[list[str]]) -> list[tuple[str, str]]:
    """Return edges that break the layering: an upward import or one between siblings."""
    position = {module: index for index, layer in enumerate(layers) for module in layer}
    bad = [
        (source, target)
        for source, target in edges
        if source in position
        and target in position
        # target at the same layer (independent sibling) or higher (smaller index) is illegal.
        and position[target] <= position[source]
        and source != target
    ]
    return sorted(bad)


def parse_layers(spec: str | None) -> list[list[str]]:
    """Parse ``"cli;decisions|policies;utils"`` into layers of independent siblings."""
    if not spec:
        return []
    layers = []
    for chunk in spec.split(";"):  # ";" separates layers top -> bottom
        siblings = [name.strip() for name in chunk.split("|") if name.strip()]
        if siblings:
            layers.append(siblings)
    return layers


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Print a package's first-segment import graph and flag layer violations.",
    )
    parser.add_argument(
        "package", type=Path, help="package directory, e.g. runtime/mira/src/thymira/mira"
    )
    parser.add_argument(
        "--layers", help='layers top->bottom; ";" between layers, "|" between siblings'
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument(
        "--fail-on-violation", action="store_true", help="exit 1 when a violation exists"
    )
    args = parser.parse_args(argv)

    if not args.package.is_dir():
        print(f"error: package root not found: {args.package}")
        print("Pass the package directory, e.g. runtime/mira/src/thymira/mira")
        return 2

    edges = build_graph(args.package)
    layers = parse_layers(args.layers)
    broken = violations(edges, layers)

    if args.json:
        payload = {
            "package": str(args.package),
            "edges": [list(edge) for edge in sorted(edges)],
            "violations": [list(edge) for edge in broken],
        }
        print(json.dumps(payload, indent=2))
    else:
        broken_set = set(broken)
        print(f"package: {args.package}  edges: {len(edges)}  violations: {len(broken)}")
        for edge in sorted(edges):
            mark = "  <- BREAKS LAYERING" if edge in broken_set else ""
            print(f"  {edge[0]} -> {edge[1]}{mark}")
        if not layers:
            print("no --layers given: edges only, direction not checked")

    return 1 if (broken and args.fail_on_violation) else 0


if __name__ == "__main__":
    raise SystemExit(main())
