"""Discovery evidence: glob, grep and list_files persist their complete ordered list (F3.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from tests.thymira.fixtures_tools import development_policy
from thymira.events import InMemoryEventLog, sha256_text
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve
from thymira.schemas import new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolContext, ToolInvocation, ToolManager, ToolRegistry
from thymira.tools.builtins.discovery import DISCOVERY_SCHEMA, persist_discovery
from thymira.tools.builtins.files import ListFiles
from thymira.tools.builtins.search import GLOB_CAP, Glob, Grep

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from thymira.state import ArtifactStore
    from thymira.tools import Tool


def _invocation(tmp_path: Path) -> tuple[ToolInvocation, Path, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts_root = tmp_path / "artifacts"
    run_id = new_id("run")
    invocation = ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(artifacts_root, run_id),
    )
    return invocation, artifacts_root, run_id


def _discovery_artifact_name(result_artifact_ids: tuple[str, ...], store: ArtifactStore) -> str:
    (artifact_id,) = result_artifact_ids
    for name, artifact in store.manifest().items():
        if artifact.id == artifact_id:
            return name
    raise AssertionError(f"no manifest entry for artifact id {artifact_id!r}")


def test_a_capped_glob_persists_every_match_not_only_the_shown_ones(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    for i in range(GLOB_CAP + 30):
        (invocation.workspace / f"f{i:04d}.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    assert len(result.artifact_ids) == 1
    name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(name)
    assert payload["schema"] == DISCOVERY_SCHEMA
    assert payload["tool"] == "glob"
    assert payload["order"] == "path"
    assert payload["total"] == GLOB_CAP + 30
    assert len(payload["matches"]) == GLOB_CAP + 30
    assert payload["matches"] == sorted(payload["matches"])
    assert payload["rendered"]["count"] == GLOB_CAP
    assert payload["rendered"]["truncated"] is True
    assert payload["rendered"]["locations"] == payload["matches"][:GLOB_CAP]


def test_an_uncapped_glob_persists_a_list_matching_the_rendering(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt"):
        (invocation.workspace / name).write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    artifact_name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(artifact_name)
    assert payload["matches"] == ["a.txt", "b.txt", "c.txt"]
    assert payload["rendered"]["truncated"] is False
    assert payload["rendered"]["count"] == 3
    assert payload["rendered"]["locations"] == payload["matches"]


def test_glob_artifact_ids_and_footer_both_name_the_discovery_artifact(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    assert len(result.artifact_ids) == 1
    artifact_id = result.artifact_ids[0]
    assert artifact_id in result.stdout


def test_glob_result_sha256_matches_the_persisted_rendered_digest(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")
    (invocation.workspace / "b.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    artifact_name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(artifact_name)
    assert result.result_sha256 == payload["rendered"]["sha256"]
    assert result.result_sha256 == sha256_text("a.txt\nb.txt")


def test_a_capped_grep_persists_every_match_and_the_skipped_files(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("hit\nhit\n", encoding="utf-8")
    (invocation.workspace / "b.txt").write_text("hit\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": "hit"})

    artifact_name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(artifact_name)
    assert payload["tool"] == "grep"
    assert payload["order"] == "path_then_line"
    assert payload["matches"] == [
        {"path": "a.txt", "line": 1},
        {"path": "a.txt", "line": 2},
        {"path": "b.txt", "line": 1},
    ]
    assert payload["skipped"] == []
    assert payload["rendered"]["locations"] == payload["matches"]


def test_grep_persists_files_it_had_to_skip(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "bad.txt").write_bytes(b"\xff\xfe not valid utf-8 \x00\x01")
    (invocation.workspace / "good.txt").write_text("hit\n", encoding="utf-8")

    result = Grep().execute(invocation, {"pattern": "hit"})

    artifact_name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(artifact_name)
    assert payload["skipped"] == ["bad.txt"]
    assert "1 file skipped" in result.stdout


def test_list_files_persists_the_complete_list_referenced_in_artifact_ids(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")
    (invocation.workspace / "b.txt").write_text("", encoding="utf-8")

    result = ListFiles().execute(invocation, {"path": "."})

    assert len(result.artifact_ids) == 1
    artifact_name = _discovery_artifact_name(result.artifact_ids, invocation.artifact_store)
    payload = invocation.artifact_store.load_json(artifact_name)
    assert payload["tool"] == "list_files"
    assert payload["matches"] == ["a.txt", "b.txt"]


def test_the_persisted_list_survives_a_separately_constructed_store(tmp_path: Path):
    invocation, artifacts_root, run_id = _invocation(tmp_path)
    for i in range(10):
        (invocation.workspace / f"f{i}.txt").write_text("", encoding="utf-8")

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    # A second, independently constructed store over the same root: this is the leg a
    # producer's own store handle cannot supply -- a later, unrelated reader opening the same
    # directory from scratch, verifying the manifest's own integrity check, and reading the
    # artifact back through it rather than through the object that wrote it.
    reopened = LocalArtifactStore(artifacts_root, run_id)
    assert reopened.verify() == []
    artifact_name = _discovery_artifact_name(result.artifact_ids, reopened)
    payload = reopened.load_json(artifact_name)
    assert sorted(payload["matches"]) == payload["matches"]
    assert set(payload["matches"]) == {f"f{i}.txt" for i in range(10)}


def test_a_failed_store_reports_an_honest_failed_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")

    def fail_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk is full")

    monkeypatch.setattr(invocation.artifact_store, "save_json", fail_save)

    result = Glob().execute(invocation, {"pattern": "*.txt"})

    assert not result.success
    assert result.error is not None
    assert result.error.startswith("FS_DISCOVERY_PERSISTENCE")
    assert result.artifact_ids == ()
    assert "discovery list not persisted" in result.stdout
    assert "a.txt" in result.stdout


def test_a_failed_store_makes_grep_report_failure_with_its_bounded_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("hit\n", encoding="utf-8")

    def fail_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk is full")

    monkeypatch.setattr(invocation.artifact_store, "save_json", fail_save)

    result = Grep().execute(invocation, {"pattern": "hit"})

    assert not result.success
    assert result.error is not None
    assert result.error.startswith("FS_DISCOVERY_PERSISTENCE")
    assert result.artifact_ids == ()
    assert "a.txt:1: hit" in result.stdout
    assert "discovery list not persisted" in result.stdout


def test_a_failed_store_makes_list_files_report_failure_with_its_bounded_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation, _, _ = _invocation(tmp_path)
    (invocation.workspace / "a.txt").write_text("", encoding="utf-8")

    def fail_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk is full")

    monkeypatch.setattr(invocation.artifact_store, "save_json", fail_save)

    result = ListFiles().execute(invocation, {"path": "."})

    assert not result.success
    assert result.error is not None
    assert result.error.startswith("FS_DISCOVERY_PERSISTENCE")
    assert result.artifact_ids == ()
    assert '"files": ["a.txt"]' in result.stdout
    assert "discovery_persistence_error" in result.stdout


def test_manager_records_discovery_persistence_failure_as_failed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = new_id("run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("", encoding="utf-8")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        event_log=log,
        gate=Gate(
            PolicyEngine(development_policy()),
            log,
            approver=auto_approve,
        ),
        artifact_store=store,
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )

    def fail_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk is full")

    monkeypatch.setattr(store, "save_json", fail_save)
    execution = ToolManager(ToolRegistry((cast("Tool", Glob()),))).execute(
        context, "glob", {"pattern": "*.txt"}
    )

    assert not execution.result.success
    assert execution.result.stdout.startswith("a.txt\n")
    assert execution.result.error is not None
    assert execution.result.error.startswith("FS_DISCOVERY_PERSISTENCE")
    completed = log.events()[-1]
    assert completed.payload["status"] == "FAILED"
    assert completed.payload["error"].startswith("FS_DISCOVERY_PERSISTENCE")


def test_persist_discovery_is_a_fresh_name_every_call_never_a_collision(tmp_path: Path):
    invocation, _, _ = _invocation(tmp_path)

    first = persist_discovery(
        invocation,
        tool="glob",
        query={},
        order="path",
        matches=["a.txt"],
        rendered_text="a.txt",
        rendered_locations=["a.txt"],
    )
    second = persist_discovery(
        invocation,
        tool="glob",
        query={},
        order="path",
        matches=["a.txt"],
        rendered_text="a.txt",
        rendered_locations=["a.txt"],
    )

    assert first.artifact_id is not None
    assert second.artifact_id is not None
    assert first.artifact_id != second.artifact_id
    assert len(invocation.artifact_store.list_active()) == 2
