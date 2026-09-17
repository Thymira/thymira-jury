"""Process-level durability evidence for the local artifact and settings stores."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from thymira.schemas import new_id
from thymira.state import LocalArtifactStore

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_for_markers(paths: tuple[Path, ...]) -> None:
    """Wait until every child process has reached its controlled start barrier."""
    deadline = time.monotonic() + 10
    while not all(path.exists() for path in paths):
        if time.monotonic() >= deadline:
            raise AssertionError("child process did not reach the start barrier")
        time.sleep(0.01)


def test_concurrent_batch_producers_keep_both_manifest_members(tmp_path: Path) -> None:
    """Two processes opened before publication must not lose either successful batch."""
    root = tmp_path / "artifacts"
    run_id = new_id("run")
    script = """
import sys
from pathlib import Path
from thymira.schemas import new_id
from thymira.state import LocalArtifactStore

root, run_id, ready, result, name, method = sys.argv[1:]
store = LocalArtifactStore(Path(root), run_id=run_id)
Path(ready).write_text("ready", encoding="utf-8")
sys.stdin.readline()
if method == "single":
    store.save_bytes(name, name.encode("ascii"), produced_by=new_id("tool"))
else:
    store.save_batch({name: name.encode("ascii")}, produced_by=new_id("tool"))
Path(result).write_text("ok", encoding="utf-8")
"""
    children: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    result_paths: list[Path] = []
    try:
        for name in ("one.txt", "two.txt"):
            ready = tmp_path / f"{name}.ready"
            result = tmp_path / f"{name}.result"
            ready_paths.append(ready)
            result_paths.append(result)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(root),
                    run_id,
                    str(ready),
                    str(result),
                    name,
                    "batch",
                ],
                cwd=_REPO_ROOT,
                stdin=subprocess.PIPE,
                text=True,
            )
            children.append(process)
        _wait_for_markers(tuple(ready_paths))
        for process in children:
            stdin = process.stdin
            assert stdin is not None
            stdin.write("go\n")
            stdin.close()
        for process in children:
            assert process.wait(timeout=10) == 0
        assert all(path.read_text(encoding="utf-8") == "ok" for path in result_paths)
    finally:
        for process in children:
            if process.poll() is None:
                stdin = process.stdin
                if stdin is not None:
                    stdin.write("stop\n")
                    stdin.close()
                process.kill()
                process.wait()

    reopened = LocalArtifactStore(root, run_id=run_id)
    assert set(reopened.manifest()) == {"one.txt", "two.txt"}
    assert reopened.load_bytes("one.txt") == b"one.txt"
    assert reopened.load_bytes("two.txt") == b"two.txt"
    assert reopened.verify() == []


def test_concurrent_single_and_batch_producers_keep_both_manifest_members(
    tmp_path: Path,
) -> None:
    """The single item API and explicit batches share the same cross process transaction."""
    root = tmp_path / "artifacts"
    run_id = new_id("run")
    script = """
import sys
from pathlib import Path
from thymira.schemas import new_id
from thymira.state import LocalArtifactStore

root, run_id, ready, result, name, method = sys.argv[1:]
store = LocalArtifactStore(Path(root), run_id=run_id)
Path(ready).write_text("ready", encoding="utf-8")
sys.stdin.readline()
if method == "single":
    store.save_bytes(name, name.encode("ascii"), produced_by=new_id("tool"))
else:
    store.save_batch({name: name.encode("ascii")}, produced_by=new_id("tool"))
Path(result).write_text("ok", encoding="utf-8")
"""
    children: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    result_paths: list[Path] = []
    try:
        for name, method in (("single.txt", "single"), ("batch.txt", "batch")):
            ready = tmp_path / f"{name}.ready"
            result = tmp_path / f"{name}.result"
            ready_paths.append(ready)
            result_paths.append(result)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(root),
                    run_id,
                    str(ready),
                    str(result),
                    name,
                    method,
                ],
                cwd=_REPO_ROOT,
                stdin=subprocess.PIPE,
                text=True,
            )
            children.append(process)
        _wait_for_markers(tuple(ready_paths))
        for process in children:
            stdin = process.stdin
            assert stdin is not None
            stdin.write("go\n")
            stdin.close()
        for process in children:
            assert process.wait(timeout=10) == 0
        assert all(path.read_text(encoding="utf-8") == "ok" for path in result_paths)
    finally:
        for process in children:
            if process.poll() is None:
                stdin = process.stdin
                if stdin is not None:
                    stdin.write("stop\n")
                    stdin.close()
                process.kill()
                process.wait()

    reopened = LocalArtifactStore(root, run_id=run_id)
    assert set(reopened.manifest()) == {"single.txt", "batch.txt"}
    assert reopened.load_bytes("single.txt") == b"single.txt"
    assert reopened.load_bytes("batch.txt") == b"batch.txt"
    assert reopened.verify() == []
