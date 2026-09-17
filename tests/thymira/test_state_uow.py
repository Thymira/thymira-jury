"""Tests for the local all-or-nothing write boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.events import verify_events
from thymira.schemas import Actor, EventType, Experiment, Run, new_id
from thymira.state import (
    LocalEventStore,
    LocalRecordRepository,
    LocalRunRepository,
    LocalUnitOfWork,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_local_unit_of_work_rolls_back_writes_when_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed commit leaves neither a Run nor its event on disk."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the dataset",
    )
    record = Experiment(id=new_id("experiment"), run_id=run.id, name="baseline")
    log = events.open(run.id)

    def fail_save(_: Experiment) -> Experiment:
        """Raise an error after the Run write has started."""
        msg = "simulated record write failure"
        raise OSError(msg)

    monkeypatch.setattr(records, "save", fail_save)

    def commit() -> None:
        """Stage writes that fail while the transaction is committing."""
        with LocalUnitOfWork(runs, records) as transaction:
            transaction.save_run(run)
            transaction.save_record(record)
            transaction.append_event(log, EventType.RUN_STARTED, Actor.system())

    with pytest.raises(OSError, match="simulated record write failure"):
        commit()

    assert runs.get(run.id) is None
    assert events.read(run.id) == []


def test_local_unit_of_work_rollback_preserves_unrelated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed transaction does not remove files created outside its staged writes."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the dataset",
    )
    record = Experiment(id=new_id("experiment"), run_id=run.id, name="baseline")
    unrelated = tmp_path / "records" / "unrelated.json"

    def fail_save(_: Experiment) -> Experiment:
        """Simulate another writer creating a record before the commit fails."""
        unrelated.write_text('{"owned_by": "another-write"}\n', encoding="utf-8")
        raise OSError("simulated record write failure")

    monkeypatch.setattr(records, "save", fail_save)

    def commit() -> None:
        """Stage writes that fail while the commit is in progress."""
        with LocalUnitOfWork(runs, records) as transaction:
            transaction.save_run(run)
            transaction.save_record(record)
            transaction.append_event(events.open(run.id), EventType.RUN_STARTED, Actor.system())

    with pytest.raises(OSError, match="simulated record write failure"):
        commit()

    assert unrelated.read_text(encoding="utf-8") == '{"owned_by": "another-write"}\n'


def test_local_unit_of_work_rollback_restores_replaced_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed update restores the previous bytes of a replaced record."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Original prompt",
    )
    runs.save(run)
    log = events.open(run.id)
    log.append(EventType.RUN_STARTED, Actor.system())
    replacement = run.model_copy(update={"prompt": "Replacement prompt"})
    record = Experiment(id=new_id("experiment"), run_id=run.id, name="baseline")

    def fail_save(_: Experiment) -> Experiment:
        """Raise after the existing Run has been replaced."""
        raise OSError("simulated record write failure")

    monkeypatch.setattr(records, "save", fail_save)

    def commit() -> None:
        """Stage an update that fails after the Run write."""
        with LocalUnitOfWork(runs, records) as transaction:
            transaction.save_run(replacement)
            transaction.save_record(record)
            transaction.append_event(log, EventType.RUN_COMPLETED, Actor.system())

    with pytest.raises(OSError, match="simulated record write failure"):
        commit()

    assert runs.get(run.id) == run
    assert len(events.read(run.id)) == 1


def test_local_unit_of_work_rollback_restores_record_order_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed record write restores both the record and its insertion-order index."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    run_id = new_id("run")
    existing = Experiment(id=new_id("experiment"), run_id=run_id, name="existing")
    records.save(existing)
    order_path = records.order_path_for(existing)
    original_order = order_path.read_bytes()
    replacement = Experiment(id=new_id("experiment"), run_id=run_id, name="replacement")
    original_save = records.save

    def save_then_fail(record: Experiment) -> Experiment:
        """Persist once, then simulate a later commit failure."""
        original_save(record)
        raise OSError("simulated event write failure")

    monkeypatch.setattr(records, "save", save_then_fail)

    with (
        pytest.raises(OSError, match="simulated event write failure"),
        LocalUnitOfWork(runs, records) as transaction,
    ):
        transaction.save_record(replacement)

    assert records.list_for_run(run_id, "experiment") == (existing,)
    assert order_path.read_bytes() == original_order
    assert records.get(run_id, replacement.id, "experiment") is None


def test_local_unit_of_work_rollback_resyncs_the_event_log_chain_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled-back commit rewinds each event log's in-memory chain cursor, not only its file.

    ``JsonlEventLog.append`` advances the log object's ``seq``/``prev_hash`` as well as writing
    its file. A file-only rollback left the object ahead of the now-empty file, so the next
    append wrote a stale ``seq`` into it and the hash chain could never verify again.
    """
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    good_run = new_id("run")
    failing_run = new_id("run")
    good_log = events.open(good_run)
    failing_log = events.open(failing_run)

    def boom(*_args: object, **_kwargs: object) -> object:
        """Fail the second staged append so the whole commit rolls back."""
        msg = "simulated event write failure"
        raise OSError(msg)

    monkeypatch.setattr(failing_log, "append", boom)

    def commit() -> None:
        """Append to a healthy log, then to one whose append fails mid-commit."""
        with LocalUnitOfWork(runs, records) as transaction:
            transaction.append_event(good_log, EventType.RUN_STARTED, Actor.system())
            transaction.append_event(failing_log, EventType.RUN_STARTED, Actor.system())

    with pytest.raises(OSError, match="simulated event write failure"):
        commit()

    # The healthy log's file was rolled back to empty; its cursor must rewind with it.
    assert events.read(good_run) == []

    good_log.append(EventType.RUN_STARTED, Actor.system())

    on_disk = good_log.events()
    assert [event.seq for event in on_disk] == [0]
    assert verify_events(on_disk).valid


def test_local_unit_of_work_commits_all_records_and_a_verifiable_event(tmp_path: Path) -> None:
    """A clean commit persists every staged record and its final event."""
    runs = LocalRunRepository(tmp_path)
    records = LocalRecordRepository(tmp_path)
    events = LocalEventStore(tmp_path)
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the dataset",
    )
    record = Experiment(id=new_id("experiment"), run_id=run.id, name="baseline")
    log = events.open(run.id)

    with LocalUnitOfWork(runs, records) as transaction:
        transaction.save_run(run)
        transaction.save_record(record)
        transaction.append_event(log, EventType.RUN_STARTED, Actor.system())

    assert runs.get(run.id) == run
    assert records.get(run.id, record.id, "experiment") == record
    assert verify_events(events.read(run.id)).valid
