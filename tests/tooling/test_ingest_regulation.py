from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

_CHUNK_KEYS = {"source_id", "version", "framework", "location", "text", "sha256"}


def test_main_writes_jsonl_to_requested_out(tmp_path: Path) -> None:
    script = load_script("scripts/ingest_regulation.py")
    out = tmp_path / "regulation.jsonl"

    exit_code = script.main(["--out", str(out)])

    assert exit_code == 0
    assert out.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    for line in lines:
        assert set(json.loads(line)) == _CHUNK_KEYS


def test_main_uses_the_repo_default_mapping(tmp_path: Path) -> None:
    script = load_script("scripts/ingest_regulation.py")

    assert script.DEFAULT_MAPPING.name == "requirements_controls.json"
    assert script.DEFAULT_MAPPING.parent.name == "governance"
    assert script.build_parser().parse_args(["--out", str(tmp_path / "x.jsonl")]).mapping == (
        script.DEFAULT_MAPPING
    )
