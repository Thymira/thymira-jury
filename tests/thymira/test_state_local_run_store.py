"""Unit tests for local JSON/JSONL Run persistence."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.api import build_default_deps
from thymira.events import GENESIS_HASH, canonical_json
from thymira.schemas import (
    ActivityProfile,
    Actor,
    EventType,
    Evidence,
    Run,
    RunStatus,
    new_id,
)
from thymira.state import LocalArtifactStore, LocalRunStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _run() -> Run:
    """Build a valid Run for persistence tests."""
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Inspect the credit-risk dataset.",
    )


def _store(tmp_path: Path) -> LocalRunStore:
    """Return an isolated Run store."""
    return LocalRunStore(tmp_path / "runs")


def _profile(run_id: str) -> ActivityProfile:
    """Build typed regulatory evidence for append testing."""
    return ActivityProfile(
        id=new_id("profile"),
        activity_id=new_id("activity"),
        version=1,
        run_id=run_id,
        purpose="Prioritise credit-risk cases for human review.",
        affected_population="Applicants for consumer credit.",
        decision_effect="Changes review order but not credit approval.",
        autonomy="Produces a recommendation only.",
        human_oversight="An analyst can override every recommendation.",
        jurisdiction="ES",
        data_categories=("income",),
        sensitive_attributes=(),
        potential_consequences=("A delayed review of an urgent application.",),
        evidence_refs=(Evidence(kind="event", ref="seq:0", sha256="a" * 64),),
    )


def test_create_and_get_run_creates_required_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()

    created = store.create(run, actor=Actor.system())

    run_dir = tmp_path / "runs" / run.id
    assert created == run
    assert store.get(run.id) == run
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "mira-context").is_dir()
    assert (run_dir / "exports").is_dir()


def test_local_run_storage_exposes_verified_restrictive_permission_evidence(
    tmp_path: Path,
) -> None:
    """The local evidence root is secured by an OS ACL or exact private mode."""
    store = _store(tmp_path)

    evidence = store.permission_evidence

    assert evidence.path == tmp_path / "runs"
    if evidence.platform == "nt":
        assert evidence.detail.startswith("D:")
        assert ";;;WD)" not in evidence.detail
        assert ";;;BU)" not in evidence.detail
    else:
        assert evidence.detail == "mode=700"


def test_default_composition_secures_the_raw_artifact_store_before_writes(tmp_path: Path) -> None:
    """The sibling artifact root used by API composition carries the same private boundary."""
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)
    run_id = new_id("run")
    store = deps.artifact_store_factory(run_id)

    assert isinstance(store, LocalArtifactStore)
    artifact = store.save_text(
        "prompt.json",
        "model-visible contact alice@example.com",
        produced_by=new_id("agent"),
    )

    assert store.permission_evidence.path == tmp_path / "artifacts" / run_id
    if store.permission_evidence.platform == "nt":
        assert store.permission_evidence.detail.startswith("D:")
        assert ";;;WD)" not in store.permission_evidence.detail
        assert ";;;BU)" not in store.permission_evidence.detail
    else:
        assert store.permission_evidence.detail == "mode=700"
        assert artifact.uri == "prompt.json"
        assert (tmp_path / "artifacts" / run_id / artifact.uri).stat().st_mode & 0o777 == 0o600


def test_exports_are_redacted_and_cannot_serialize_unknown_values(tmp_path: Path) -> None:
    """A presentation export is safe while an unclassifiable value fails closed."""
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())

    target = store.write_export(
        run.id,
        "view.json",
        {"contact": "alice@example.com", "metric": 0.8123456789012345},
    )

    assert target.is_file()
    exported = store.read_export(run.id, "view.json")
    assert exported == {"contact": "[REDACTED:EMAIL]", "metric": 0.8123456789012345}
    with pytest.raises(ValueError, match="could not be safely redacted"):
        store.write_export(run.id, "unsafe.json", {"value": object()})
    assert store.read_export(run.id, "unsafe.json") is None


def test_append_events_uses_expected_version_and_updates_hash_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())

    appended = store.append(
        run.id,
        EventType.AGENT_STARTED,
        Actor.system(),
        {"name": "thy"},
        expected_version=1,
    )

    events = store.events(run.id)
    assert [event.seq for event in events] == [0, 1]
    assert events[0].prev_hash == GENESIS_HASH
    assert appended.prev_hash == events[0].hash
    assert appended.hash == events[-1].hash
    assert store.version(run.id) == 2


def test_append_rejects_a_stale_expected_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())

    with pytest.raises(ValueError, match="expected version 0, current version is 1"):
        store.append(
            run.id,
            EventType.AGENT_STARTED,
            Actor.system(),
            expected_version=0,
        )


def test_missing_or_stale_projection_is_rebuilt_from_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())
    store.append(run.id, EventType.AGENT_STARTED, Actor.system(), expected_version=1)
    projection = tmp_path / "runs" / run.id / "run.json"
    projection.unlink()

    assert store.get(run.id) == run
    rebuilt = json.loads(projection.read_text(encoding="utf-8"))
    assert rebuilt["run_id"] == run.id
    assert rebuilt["version"] == 2
    assert rebuilt["last_event_hash"] == store.events(run.id)[-1].hash


def test_mismatched_projection_is_rebuilt_from_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())
    projection = tmp_path / "runs" / run.id / "run.json"
    projection.write_text('{"run_id":"wrong","version":0}', encoding="utf-8", newline="\n")

    assert store.get(run.id) == run
    assert json.loads(projection.read_text(encoding="utf-8"))["run_id"] == run.id


def test_corrupt_projection_is_rebuilt_from_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())
    projection = tmp_path / "runs" / run.id / "run.json"
    projection.write_text("{not-json", encoding="utf-8", newline="\n")

    assert store.version(run.id) == 1
    assert json.loads(projection.read_text(encoding="utf-8"))["run_id"] == run.id


@pytest.mark.parametrize("content", ["{", '{"bad":'])
def test_corrupt_or_truncated_jsonl_is_reported(tmp_path: Path, content: str) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())
    events = tmp_path / "runs" / run.id / "events.jsonl"
    events.write_text(content, encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=r"events\.jsonl is invalid"):
        store.get(run.id)


def test_runs_are_separate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run()
    second = _run()
    store.create(first, actor=Actor.system())
    store.create(second, actor=Actor.system())
    store.append(first.id, EventType.AGENT_STARTED, Actor.system(), expected_version=1)

    assert len(store.events(first.id)) == 2
    assert len(store.events(second.id)) == 1
    assert store.get(second.id) == second


def test_regulatory_event_is_persisted_as_a_typed_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())
    profile = _profile(run.id)

    store.append(
        run.id,
        EventType.ACTIVITY_PROFILE_RECORDED,
        Actor.system(),
        {"activity_profile": profile.model_dump(mode="json")},
        expected_version=1,
    )

    event = store.events(run.id)[-1]
    assert event.type is EventType.ACTIVITY_PROFILE_RECORDED
    assert (
        ActivityProfile.model_validate_json(canonical_json(event.payload["activity_profile"]))
        == profile
    )


def test_concurrent_appends_never_corrupt_the_event_log(tmp_path: Path) -> None:
    """Real threads racing store.version()+append(), as RunEventLog.append() does.

    Each thread mirrors the production pattern (read the current version, then append with it
    as ``expected_version``): some must lose the race and get a clean ``ValueError``, but the
    log on disk must never end up with a duplicated or skipped ``seq`` -- reopening it must
    always succeed and its sequence must stay gapless.
    """
    store = _store(tmp_path)
    run = _run()
    store.create(run, actor=Actor.system())

    thread_count = 16
    barrier = threading.Barrier(thread_count)
    stale_version_errors: list[ValueError] = []
    lock = threading.Lock()

    def _append_like_run_event_log(i: int) -> None:
        barrier.wait()
        version = store.version(run.id)
        try:
            store.append(
                run.id,
                EventType.AGENT_STARTED,
                Actor.system(),
                {"i": i},
                expected_version=version,
            )
        except ValueError as exc:
            with lock:
                stale_version_errors.append(exc)

    threads = [
        threading.Thread(target=_append_like_run_event_log, args=(i,)) for i in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = store.events(run.id)
    assert [event.seq for event in events] == list(range(len(events)))
    assert len(events) == 1 + (thread_count - len(stale_version_errors))


def _run_at(created_at: datetime) -> Run:
    """Build a Run with an explicit creation time so listing order is deterministic."""
    return Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Inspect the credit-risk dataset.",
        created_at=created_at,
    )


def _hydrated(current: dict[str, RunStatus]) -> Callable[[Run], Run]:
    """Return a pure hydrate callback that overrides each Run's status by id."""

    def hydrate(run: Run) -> Run:
        return run.model_copy(update={"status": current.get(run.id, run.status)})

    return hydrate


def test_list_runs_status_filter_uses_the_frozen_creation_status_without_hydration(
    tmp_path: Path,
) -> None:
    """The store records the creation status and, given no callback, can only filter that."""
    store = _store(tmp_path)
    runs = [_run() for _ in range(3)]
    for run in runs:
        store.create(run, actor=Actor.system())

    # Every persisted body carries the creation status, so a current-status query is empty...
    assert store.list_runs(status=RunStatus.COMPLETED).items == ()
    # ...and the creation-status query returns all of them.
    assert {run.id for run in store.list_runs(status=RunStatus.CREATED).items} == {
        run.id for run in runs
    }


def test_list_runs_applies_hydrate_before_the_status_filter(tmp_path: Path) -> None:
    """A hydrate callback filters on the caller's current status, not the frozen creation one."""
    store = _store(tmp_path)
    created = _run_at(datetime(2026, 1, 1, tzinfo=UTC))
    completed = _run_at(datetime(2026, 1, 2, tzinfo=UTC))
    store.create(created, actor=Actor.system())
    store.create(completed, actor=Actor.system())
    hydrate = _hydrated({completed.id: RunStatus.COMPLETED})

    done = store.list_runs(status=RunStatus.COMPLETED, hydrate=hydrate)
    assert [run.id for run in done.items] == [completed.id]
    assert done.items[0].status is RunStatus.COMPLETED

    # The creation-status query no longer returns a body that says COMPLETED.
    still_created = store.list_runs(status=RunStatus.CREATED, hydrate=hydrate)
    assert [run.id for run in still_created.items] == [created.id]


def test_list_runs_pages_across_a_boundary_after_hydration(tmp_path: Path) -> None:
    """Hydration precedes paging, so a status filter stays consistent across the cursor boundary."""
    store = _store(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    runs = [_run_at(base + timedelta(seconds=index)) for index in range(5)]
    for run in runs:
        store.create(run, actor=Actor.system())
    # Interleave completed and created Runs: a filter applied after paging would mis-size a page.
    completed_ids = {runs[0].id, runs[2].id, runs[4].id}
    hydrate = _hydrated(dict.fromkeys(completed_ids, RunStatus.COMPLETED))

    first = store.list_runs(status=RunStatus.COMPLETED, hydrate=hydrate, limit=2)
    assert [run.id for run in first.items] == [runs[0].id, runs[2].id]
    assert first.next_cursor is not None

    second = store.list_runs(
        status=RunStatus.COMPLETED, hydrate=hydrate, limit=2, cursor=first.next_cursor
    )
    assert [run.id for run in second.items] == [runs[4].id]
    assert second.next_cursor is None

    both = (*first.items, *second.items)
    assert {run.id for run in both} == completed_ids
    assert all(run.status is RunStatus.COMPLETED for run in both)
