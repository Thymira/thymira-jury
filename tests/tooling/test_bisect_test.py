from __future__ import annotations

import sys

from tests.tooling.conftest import load_script

SCRIPT = ".agents/skills/python-debug/scripts/bisect_test.py"


def test_build_commands_orders_start_run_reset():
    bisect = load_script(SCRIPT)
    run_cmd = [
        "git",
        "bisect",
        "run",
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-q",
        "tests/test_x.py::test_y",
    ]
    assert bisect.build_commands("v1.0", "HEAD", ["tests/test_x.py::test_y"]) == [
        ["git", "bisect", "start", "HEAD", "v1.0"],
        run_cmd,
        ["git", "bisect", "reset"],
    ]


def test_main_dry_run_prints_commands_and_returns_zero(capsys):
    bisect = load_script(SCRIPT)
    assert bisect.main(["--dry-run", "v1.0", "HEAD", "--", "tests/test_x.py"]) == 0
    out = capsys.readouterr().out
    assert "git bisect start HEAD v1.0" in out
    assert "git bisect run" in out
    assert "git bisect reset" in out
