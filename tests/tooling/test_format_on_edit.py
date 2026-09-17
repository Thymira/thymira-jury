from __future__ import annotations

import json

from tests.tooling.conftest import load_script


def test_extracts_python_file_inside_root(tmp_path):
    hook = load_script("scripts/hooks/format_on_edit.py")
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("x=1\n", encoding="utf-8")
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}, "cwd": str(tmp_path)}
    assert hook.target_file(payload, tmp_path) == target


def test_ignores_non_python_and_outside_files(tmp_path):
    hook = load_script("scripts/hooks/format_on_edit.py")
    assert hook.target_file({"tool_input": {"file_path": str(tmp_path / "a.md")}}, tmp_path) is None
    assert hook.target_file({"tool_input": {"file_path": "C:/elsewhere/a.py"}}, tmp_path) is None
    assert hook.target_file({"tool_input": {}}, tmp_path) is None


def test_main_formats_file_and_exits_zero(tmp_path, monkeypatch):
    hook = load_script("scripts/hooks/format_on_edit.py")
    target = tmp_path / "scripts" / "y.py"
    target.parent.mkdir(parents=True)
    target.write_text("import os,sys\nx=( 1 )\n", encoding="utf-8")
    monkeypatch.setenv("FORMAT_ON_EDIT_ROOT", str(tmp_path))
    stdin = json.dumps({"tool_input": {"file_path": str(target)}})
    assert hook.main(stdin) == 0
    assert target.read_text(encoding="utf-8") == "x = 1\n"
