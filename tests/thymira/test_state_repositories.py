"""Tests for local state repositories and their persistence seams."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from thymira.events import verify_events
from thymira.schemas import Actor, EventType, Experiment, Run, Session, new_id
from thymira.state import (
    LocalCheckpointRepository,
    LocalEventStore,
    LocalRecordRepository,
    LocalRunRepository,
    LocalSessionRepository,
    LocalUnitOfWork,
    RunRepository,
    SessionRepository,
)

if TYPE_CHECKING:
    from pathlib import Path


def _run(project_id: str) -> Run:
    """Build a valid run for repository tests."""
    return Run(
        id=new_id("run"), project_id=project_id, session_id=new_id("session"), prompt="profile"
    )


def test_local_repositories_round_trip_and_paginate(tmp_path: Path) -> None:
    """Local JSON repositories round-trip records and expose opaque cursors."""
    project_id = new_id("project")
    runs = LocalRunRepository(tmp_path)
    sessions = LocalSessionRepository(tmp_path)
    first = _run(project_id)
    second = _run(project_id)
    runs.save(first)
    runs.save(second)
    session = Session(id=new_id("session"), project_id=project_id, client="cli")
    sessions.save(session)

    page = runs.list(project_id=project_id, limit=1)

    assert runs.get(first.id) == first
    assert page.items == (min((first, second), key=lambda run: (run.created_at, run.id)),)
    assert page.next_cursor is not None
    assert runs.list(project_id=project_id, limit=1, cursor=page.next_cursor).items
    assert sessions.get(session.id) == session
    assert isinstance(runs, RunRepository)
    assert isinstance(sessions, SessionRepository)


def test_repository_rejects_a_malformed_cursor(tmp_path: Path) -> None:
    """Malformed cursors raise the repository's stable validation error."""
    runs = LocalRunRepository(tmp_path)

    with pytest.raises(ValueError, match="invalid repository cursor"):
        runs.list(limit=1, cursor="not-a-valid-cursor")


def _cursor(payload: dict[str, object]) -> str:
    """Build an opaque cursor string from a raw payload, as the repository encodes them."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_a_cursor_with_a_naive_timestamp_is_rejected_not_compared(tmp_path: Path) -> None:
    """A timezone-less cursor is refused up front, not left to blow up during comparison.

    The stored ordering key is timezone-aware (``schemas.utc_now``). A cursor whose ``created_at``
    carries no offset used to decode cleanly and then raise `TypeError` deep in pagination when
    compared against an aware timestamp; it is now rejected as an unusable cursor -- the fail-safe
    reading, since silently assuming UTC is how two stores disagree about the same page
    (finding #26-4).
    """
    runs = LocalRunRepository(tmp_path)
    runs.save(_run(new_id("project")))
    naive = _cursor({"created_at": "2026-08-30T12:00:00", "id": new_id("run")})

    with pytest.raises(ValueError, match="invalid repository cursor"):
        runs.list(limit=1, cursor=naive)


def test_every_unusable_cursor_shape_raises_one_named_error(tmp_path: Path) -> None:
    """Every cursor the store cannot use is one named failure, not three incidental types.

    A non-string id used to raise `TypeError`, an undecodable string `ValueError`, and a naive
    timestamp `TypeError` from a later comparison -- three types for a single condition. They now
    all raise the one named ``InvalidCursorError`` (a `ValueError`) with the stable message
    (finding #26-4).
    """
    runs = LocalRunRepository(tmp_path)
    runs.save(_run(new_id("project")))
    aware = "2026-08-30T12:00:00+00:00"
    unusable = [
        "not-a-valid-cursor",
        _cursor({"created_at": aware}),  # missing id
        _cursor({"created_at": aware, "id": 123}),  # non-string id
        _cursor({"created_at": "2026-08-30T12:00:00", "id": new_id("run")}),  # naive timestamp
    ]

    raised: list[type[BaseException]] = []
    for cursor in unusable:
        with pytest.raises(ValueError, match="invalid repository cursor") as excinfo:
            runs.list(limit=1, cursor=cursor)
        raised.append(excinfo.type)

    assert len({error.__name__ for error in raised}) == 1
    assert raised[0].__name__ == "InvalidCursorError"


def test_local_event_store_reopens_a_verifiable_chain(tmp_path: Path) -> None:
    """Events survive reopening the local store without breaking their hash chain."""
    run_id = new_id("run")
    store = LocalEventStore(tmp_path)
    log = store.open(run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {"safe": "value"})

    reopened = store.open(run_id)

    assert len(reopened.events()) == 1
    assert verify_events(reopened.events()).valid


def test_local_record_and_checkpoint_repositories_round_trip(tmp_path: Path) -> None:
    """Child records and opaque graph checkpoints are persisted locally."""
    run_id = new_id("run")
    record = Experiment(id=new_id("experiment"), run_id=run_id, name="baseline")
    records = LocalRecordRepository(tmp_path)
    checkpoints = LocalCheckpointRepository(tmp_path)
    records.save(record)
    checkpoints.put("thread-1", "", b"checkpoint")

    assert records.get(run_id, record.id, "experiment") == record
    assert records.list_for_run(run_id, "experiment") == (record,)
    assert checkpoints.get("thread-1", "") == b"checkpoint"
    assert checkpoints.list("thread-1", "") == ("",)


def test_local_checkpoint_repository_isolates_namespaces_on_disk(tmp_path: Path) -> None:
    """Two namespaces sharing a thread land in two files, named by the namespace's own digest.

    The namespace literal carries LangGraph's own subgraph separators (``"|"`` and ``":"``, both
    reserved in Windows filenames) to prove the on-disk encoding survives the real characters a
    nested subgraph namespace uses, not just a plain-name stand-in.
    """
    thread_id = "thread-1"
    root_ns = ""
    nested_ns = "alpha|beta:1"
    checkpoints = LocalCheckpointRepository(tmp_path)

    checkpoints.put(thread_id, root_ns, b"root-blob")
    checkpoints.put(thread_id, nested_ns, b"nested-blob")

    thread_dir = tmp_path / "checkpoints" / thread_id
    root_path = thread_dir / f"{hashlib.sha256(root_ns.encode('utf-8')).hexdigest()[:32]}.bin"
    nested_path = thread_dir / f"{hashlib.sha256(nested_ns.encode('utf-8')).hexdigest()[:32]}.bin"

    assert root_path.read_bytes() == b"root-blob"
    assert nested_path.read_bytes() == b"nested-blob"
    assert checkpoints.get(thread_id, root_ns) == b"root-blob"
    assert checkpoints.get(thread_id, nested_ns) == b"nested-blob"
    assert list(thread_dir.glob("*.tmp")) == []
    assert {path.name for path in thread_dir.iterdir()} == {root_path.name, nested_path.name}


def test_local_record_repository_preserves_insertion_order_after_reopen(tmp_path: Path) -> None:
    """Records are listed in save order, not identifier order, after reopening the store."""
    run_id = "run_" + "f" * 32
    records = LocalRecordRepository(tmp_path)
    first = Experiment(id="experiment_" + "c" * 32, run_id=run_id, name="first")
    second = Experiment(id="experiment_" + "a" * 32, run_id=run_id, name="second")
    third = Experiment(id="experiment_" + "b" * 32, run_id=run_id, name="third")

    records.save(first)
    records.save(second)
    records.save(third)
    records.save(second)

    reopened = LocalRecordRepository(tmp_path)

    assert reopened.list_for_run(run_id, "experiment") == (first, second, third)


def test_local_unit_of_work_discards_staged_writes_on_error(tmp_path: Path) -> None:
    """A failed write scope leaves no run or event behind."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    run = _run(new_id("project"))
    log = events.open(run.id)

    def abort() -> None:
        """Raise inside a write scope."""
        with LocalUnitOfWork(runs, records) as transaction:
            transaction.save_run(run)
            transaction.append_event(log, EventType.RUN_STARTED, Actor.system())
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        abort()

    assert runs.get(run.id) is None
    assert log.events() == []
