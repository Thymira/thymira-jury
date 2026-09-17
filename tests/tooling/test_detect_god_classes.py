from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

BIG = "class Big:\n" + "".join(
    f"    def m{i}(self):\n        self.a{i % 3} = {i}\n        return self.a{i % 3}\n"
    for i in range(20)
)
SMALL = (
    "\n".join(
        [
            "class Small:",
            "    def __init__(self):",
            "        self.x = 1",
            "    def get(self):",
            "        return self.x",
        ]
    )
    + "\n"
)
DATACLASS = "from dataclasses import dataclass\n@dataclass\nclass Point:\n    x: int\n    y: int\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _thresholds(detect):
    return detect.Thresholds(
        max_methods=15,
        max_class_lines=300,
        max_module_lines=600,
        max_module_defs=25,
        min_cohesion=0.3,
    )


def test_flags_big_class_only(tmp_path):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "mod.py", BIG + "\n" + SMALL + "\n" + DATACLASS)
    report = detect.analyse_paths([tmp_path], _thresholds(detect))
    flagged = [c.name for c in report.classes if c.flags]
    assert flagged == ["Big"]
    big = next(c for c in report.classes if c.name == "Big")
    assert big.methods == 20
    assert 0.0 <= big.cohesion <= 1.0


def test_flags_long_module(tmp_path):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "long.py", "".join(f"def f{i}():\n    return {i}\n\n" for i in range(30)))
    report = detect.analyse_paths([tmp_path], _thresholds(detect))
    assert any("top-level definitions" in flag for m in report.modules for flag in m.flags)


def test_cli_json_and_exit_code(tmp_path, capsys):
    detect = load_script(".agents/skills/python-god-classes/scripts/detect_god_classes.py")
    write(tmp_path, "mod.py", BIG)
    assert detect.main([str(tmp_path), "--json", "--fail-over"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["classes"][0]["name"] == "Big"
    assert detect.main([str(tmp_path), "--json", "--max-methods", "50"]) == 0
