from __future__ import annotations

from pathlib import Path

from tests.tooling.conftest import load_script


def test_executable_is_found_in_the_environment():
    wrapper = load_script("scripts/lint_imports.py")
    path = Path(wrapper.lint_imports_executable())
    assert path.exists()
    assert path.stem == "lint-imports"


def test_environment_forces_utf8(monkeypatch):
    wrapper = load_script("scripts/lint_imports.py")
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = wrapper.utf8_environment()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_main_forwards_arguments_and_exit_code(monkeypatch):
    wrapper = load_script("scripts/lint_imports.py")
    calls: list[tuple[list[str], dict[str, str]]] = []

    class _Completed:
        returncode = 3

    def fake_run(command, env, check):
        calls.append((command, env))
        assert check is False
        return _Completed()

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    assert wrapper.main(["--verbose"]) == 3
    command, env = calls[0]
    assert command[1:] == ["--verbose"]
    assert env["PYTHONUTF8"] == "1"


def test_help_runs_end_to_end():
    wrapper = load_script("scripts/lint_imports.py")
    assert wrapper.main(["--help"]) == 0
