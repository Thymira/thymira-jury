from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = ".agents/skills/check-imports/scripts/import_graph.py"


def _make_pkg(tmp_path: Path) -> Path:
    """A three-module cycle: a -> b (absolute), b -> c (relative), c -> a (dotted)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from pkg import b\n", encoding="utf-8")
    (pkg / "b.py").write_text("from . import c\n", encoding="utf-8")
    (pkg / "c.py").write_text("import pkg.a\n", encoding="utf-8")
    return pkg


def test_build_graph_collects_first_segment_edges(tmp_path):
    graph = load_script(SCRIPT)
    pkg = _make_pkg(tmp_path)
    assert graph.build_graph(pkg) == {("a", "b"), ("b", "c"), ("c", "a")}


def test_build_graph_understands_fully_qualified_imports_in_a_namespace(tmp_path):
    """`from ns.pkg.b import x` and `import ns.pkg.a` — the form every thymira member uses."""
    graph = load_script(SCRIPT)
    pkg = tmp_path / "ns" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from ns.pkg.b import x\n", encoding="utf-8")
    (pkg / "b.py").write_text("import ns.pkg.a\n", encoding="utf-8")
    assert graph.build_graph(pkg) == {("a", "b"), ("b", "a")}


def test_violations_flags_only_the_upward_edge():
    graph = load_script(SCRIPT)
    edges = {("a", "b"), ("b", "c"), ("c", "a")}
    assert graph.violations(edges, layers=[["a"], ["b"], ["c"]]) == [("c", "a")]


def test_violations_flags_imports_between_independent_siblings():
    graph = load_script(SCRIPT)
    edges = {("a", "b")}
    assert graph.violations(edges, layers=[["a", "b"]]) == [("a", "b")]


def test_main_prints_json_with_edges_and_violations(tmp_path, capsys):
    graph = load_script(SCRIPT)
    pkg = _make_pkg(tmp_path)
    assert graph.main([str(pkg), "--layers", "a;b;c", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {tuple(edge) for edge in payload["edges"]} == {("a", "b"), ("b", "c"), ("c", "a")}
    assert [tuple(edge) for edge in payload["violations"]] == [("c", "a")]


def test_fail_on_violation_controls_the_exit_code(tmp_path):
    graph = load_script(SCRIPT)
    pkg = _make_pkg(tmp_path)
    assert graph.main([str(pkg), "--layers", "a;b;c", "--fail-on-violation"]) == 1
    assert graph.main([str(pkg), "--layers", "a;b;c"]) == 0
