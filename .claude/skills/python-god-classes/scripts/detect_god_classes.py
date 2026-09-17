"""Detect god classes and overgrown modules with a static AST pass.

The point is to replace "this file feels huge" with a number. It parses every ``*.py``
file, counts methods, attributes, lines and a cohesion ratio per class, and flags the
ones that cross documented thresholds. Cohesion is deliberately weighted over raw size:
a 40-field dataclass is not a god class, so ``dataclass``/``NamedTuple``/``TypedDict``/
``Enum`` types are measured but only flagged when they are genuinely long.

Cohesion = mean over methods of (attributes that method touches / all ``self`` attributes
of the class). A class whose methods each touch a different slice of the state is low
cohesion: that is the seam an Extract Class follows. It is 1.0 (perfect) when a class has
fewer than two methods or touches no attributes, so tiny classes are never flagged on it.

Usage:
    python detect_god_classes.py packages runtime apps adapters   # human table
    python detect_god_classes.py runtime --json                   # machine output
    python detect_god_classes.py runtime --fail-over              # exit 1 if anything is flagged
    python detect_god_classes.py runtime --top 5                  # only the worst five
Thresholds are documented defaults to tune per code base, not laws:
    --max-methods --max-class-lines --max-module-lines --max-module-defs --min-cohesion
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Directories that never contain first-party source worth measuring.
SKIP_DIRS = {".venv", "__pycache__", ".git", "build", "dist", ".pytest-tmp", ".ruff_cache"}
# Decorators / bases that mark a class as a data holder: measured, but only flagged when long.
DATA_DECORATORS = {"dataclass"}
DATA_BASES = {"NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "IntFlag", "Flag"}


@dataclass(frozen=True)
class Thresholds:
    """Tunable limits. Defaults are the documented starting point for this repository."""

    max_methods: int = 15
    max_class_lines: int = 300
    max_module_lines: int = 600
    max_module_defs: int = 25
    min_cohesion: float = 0.3


@dataclass
class ClassMetrics:
    """Measurements of one class (methods, lines, cohesion)."""

    file: str
    name: str
    line: int
    lines: int
    methods: int
    public_methods: int
    attributes: int
    cohesion: float
    flags: list[str]


@dataclass
class ModuleMetrics:
    """Measurements of one module (lines, classes, functions)."""

    file: str
    lines: int
    top_level_defs: int
    flags: list[str]


@dataclass
class Report:
    """All findings of one scan."""

    classes: list[ClassMetrics]
    modules: list[ModuleMetrics]

    def to_dict(self) -> dict:
        """JSON-serialisable view of the report."""
        return {
            "classes": [asdict(c) for c in self.classes],
            "modules": [asdict(m) for m in self.modules],
        }


def _callable_name(node: ast.expr) -> str:
    """Final attribute/name of a decorator or base (``a.b.dataclass`` -> ``dataclass``)."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _is_data_class(node: ast.ClassDef) -> bool:
    if any(_callable_name(dec) in DATA_DECORATORS for dec in node.decorator_list):
        return True
    return any(_callable_name(base) in DATA_BASES for base in node.bases)


def _receiver(method: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Name bound to the instance (``self``/``cls``); ``None`` for staticmethods or no args."""
    if any(_callable_name(dec) == "staticmethod" for dec in method.decorator_list):
        return None
    args = method.args.posonlyargs + method.args.args
    return args[0].arg if args else None


def _touched_attributes(method: ast.AST, receiver: str | None) -> set[str]:
    if receiver is None:
        return set()
    return {
        sub.attr
        for sub in ast.walk(method)
        if isinstance(sub, ast.Attribute)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == receiver
    }


def _class_metrics(node: ast.ClassDef, file: str, thresholds: Thresholds) -> ClassMetrics:
    methods = [n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    per_method = [_touched_attributes(m, _receiver(m)) for m in methods]
    attributes: set[str] = set().union(*per_method) if per_method else set()

    if len(methods) < 2 or not attributes:
        cohesion = 1.0
    else:
        cohesion = round(sum(len(t) / len(attributes) for t in per_method) / len(methods), 4)

    end = node.end_lineno or node.lineno
    lines = end - node.lineno + 1
    public = sum(1 for m in methods if not m.name.startswith("_"))

    flags: list[str] = []
    if lines > thresholds.max_class_lines:
        flags.append(f"{lines} lines > {thresholds.max_class_lines}")
    # Data holders (dataclass/NamedTuple/TypedDict/Enum) are exempt from method/cohesion flags:
    # many fields and thin accessors are their job, not a smell.
    if not _is_data_class(node):
        if len(methods) > thresholds.max_methods:
            flags.append(f"{len(methods)} methods > {thresholds.max_methods}")
        if cohesion < thresholds.min_cohesion:
            flags.append(f"cohesion {cohesion:.2f} < {thresholds.min_cohesion:.2f}")

    return ClassMetrics(
        file=file,
        name=node.name,
        line=node.lineno,
        lines=lines,
        methods=len(methods),
        public_methods=public,
        attributes=len(attributes),
        cohesion=cohesion,
        flags=flags,
    )


def analyse_file(path: Path, thresholds: Thresholds) -> tuple[list[ClassMetrics], ModuleMetrics]:
    """Return (class metrics, module metrics) for one file. Unparseable files yield no classes."""
    source = path.read_text(encoding="utf-8")
    file = path.as_posix()
    n_lines = len(source.splitlines())
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Report the file, but do not guess at classes we could not parse.
        return [], ModuleMetrics(file=file, lines=n_lines, top_level_defs=0, flags=[])

    classes = [
        _class_metrics(n, file, thresholds) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    ]
    top_level_defs = sum(
        isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) for n in tree.body
    )
    flags: list[str] = []
    if n_lines > thresholds.max_module_lines:
        flags.append(f"{n_lines} lines > {thresholds.max_module_lines}")
    if top_level_defs > thresholds.max_module_defs:
        flags.append(f"{top_level_defs} top-level definitions > {thresholds.max_module_defs}")
    module = ModuleMetrics(file=file, lines=n_lines, top_level_defs=top_level_defs, flags=flags)
    return classes, module


def _iter_py_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for path in sorted(root.rglob("*.py")):
        # Only inspect directory names *below* the scan root, so a root that itself sits
        # under a skipped name (e.g. a pytest tmp dir) is still scanned.
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def analyse_paths(paths: Sequence[Path], thresholds: Thresholds) -> Report:
    """Walk every path (file or directory) and return a Report sorted worst-first."""
    classes: list[ClassMetrics] = []
    modules: list[ModuleMetrics] = []
    for raw in paths:
        for path in _iter_py_files(Path(raw)):
            file_classes, module = analyse_file(path, thresholds)
            classes.extend(file_classes)
            modules.append(module)
    # Worst first: most flags, then most lines, then name for a stable order.
    classes.sort(key=lambda c: (-len(c.flags), -c.lines, c.name))
    modules.sort(key=lambda m: (-len(m.flags), -m.lines, m.file))
    return Report(classes=classes, modules=modules)


def render_table(report: Report, top: int | None) -> str:
    """Render the findings as an aligned text table."""
    classes = [c for c in report.classes if c.flags]
    modules = [m for m in report.modules if m.flags]
    if top is not None:
        classes = classes[:top]
        modules = modules[:top]
    out = [
        f"God-class report: {len(classes)} class(es) and {len(modules)} module(s) over threshold"
    ]
    if classes:
        out.append("")
        out.append("Classes:")
        out.extend(
            f"  {c.file}:{c.line} {c.name}  "
            f"[{'; '.join(c.flags)}]  "
            f"methods={c.methods} cohesion={c.cohesion:.2f} lines={c.lines}"
            for c in classes
        )
    if modules:
        out.append("")
        out.append("Modules:")
        out.extend(f"  {m.file}  [{'; '.join(m.flags)}]" for m in modules)
    if not classes and not modules:
        out.append("No god classes or overgrown modules found.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Detect god classes and overgrown modules.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to scan")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    parser.add_argument("--top", type=int, default=None, help="show only the N worst findings")
    parser.add_argument(
        "--fail-over", action="store_true", help="exit 1 when anything is flagged (for CI)"
    )
    parser.add_argument("--max-methods", type=int, default=Thresholds.max_methods)
    parser.add_argument("--max-class-lines", type=int, default=Thresholds.max_class_lines)
    parser.add_argument("--max-module-lines", type=int, default=Thresholds.max_module_lines)
    parser.add_argument("--max-module-defs", type=int, default=Thresholds.max_module_defs)
    parser.add_argument("--min-cohesion", type=float, default=Thresholds.min_cohesion)
    args = parser.parse_args(argv)

    thresholds = Thresholds(
        max_methods=args.max_methods,
        max_class_lines=args.max_class_lines,
        max_module_lines=args.max_module_lines,
        max_module_defs=args.max_module_defs,
        min_cohesion=args.min_cohesion,
    )
    report = analyse_paths(args.paths, thresholds)

    if args.as_json:
        classes = report.classes[: args.top] if args.top is not None else report.classes
        modules = report.modules[: args.top] if args.top is not None else report.modules
        print(json.dumps(Report(classes=classes, modules=modules).to_dict(), indent=2))
    else:
        print(render_table(report, args.top))

    flagged = any(c.flags for c in report.classes) or any(m.flags for m in report.modules)
    return 1 if args.fail_over and flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
