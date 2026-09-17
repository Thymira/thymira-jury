"""Backend-agnostic conformance assertions for the state repository protocols."""

from __future__ import annotations

from typing import NoReturn, Protocol

import pytest

from thymira.events import verify_events
from thymira.schemas import (
    Actor,
    Agent,
    EventType,
    Experiment,
    Layer,
    Run,
    Session,
    Task,
    ToolCall,
    new_id,
)
from thymira.state import (
    CheckpointRepository,
    EventStore,
    RecordRepository,
    RunRepository,
    SessionRepository,
    UnitOfWork,
)


class RepositoryFactory(Protocol):
    """Build fresh repository handles over one backend root."""

    def run_repository(self) -> RunRepository:
        """Return a run repository."""
        ...

    def session_repository(self) -> SessionRepository:
        """Return a session repository."""
        ...

    def record_repository(self) -> RecordRepository:
        """Return a child-record repository."""
        ...

    def event_store(self) -> EventStore:
        """Return an event store."""
        ...

    def checkpoint_repository(self) -> CheckpointRepository:
        """Return a checkpoint repository."""
        ...

    def unit_of_work(self) -> UnitOfWork:
        """Return a transaction boundary over the backend."""
        ...


def _run(project_id: str, session_id: str) -> Run:
    """Build a valid run for conformance assertions."""
    return Run(id=new_id("run"), project_id=project_id, session_id=session_id, prompt="profile")


def _raise_rollback_requested() -> NoReturn:
    """Raise the controlled failure used to verify transaction rollback."""
    raise RuntimeError("rollback requested")


def assert_run_and_session_contract(factory: RepositoryFactory) -> None:
    """Assert round-trip, filtering and cursor pagination for runs and sessions."""
    project_id = new_id("project")
    first_session = Session(id=new_id("session"), project_id=project_id, client="cli")
    second_session = Session(id=new_id("session"), project_id=project_id, client="cli")
    first = _run(project_id, first_session.id)
    second = _run(project_id, second_session.id)
    runs = factory.run_repository()
    sessions = factory.session_repository()

    runs.save(first)
    runs.save(second)
    sessions.save(first_session)
    sessions.save(second_session)

    ordered = tuple(sorted((first, second), key=lambda run: (run.created_at, run.id)))
    first_page = runs.list(project_id=project_id, limit=1)
    assert first_page.items == (ordered[0],)
    assert first_page.next_cursor is not None
    assert runs.list(project_id=project_id, limit=1, cursor=first_page.next_cursor).items == (
        ordered[1],
    )
    ordered_sessions = tuple(
        sorted(
            (first_session, second_session), key=lambda session: (session.created_at, session.id)
        )
    )
    session_page = sessions.list(project_id=project_id, limit=1)
    assert session_page.items == (ordered_sessions[0],)
    assert session_page.next_cursor is not None
    assert sessions.list(
        project_id=project_id,
        limit=1,
        cursor=session_page.next_cursor,
    ).items == (ordered_sessions[1],)
    assert runs.get(first.id) == first
    assert sessions.get(first_session.id) == first_session
    assert runs.list(project_id=new_id("project")).items == ()
    assert isinstance(runs, RunRepository)
    assert isinstance(sessions, SessionRepository)


def assert_record_contract(factory: RepositoryFactory) -> None:
    """Assert child-record round-trips and insertion order for every record kind."""
    run_id = new_id("run")
    agent_id = new_id("agent")
    agent = Agent(id=agent_id, run_id=run_id, name="thy", layer=Layer.EXECUTION)
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=agent_id,
        objective="profile the dataset",
    )
    tool_call = ToolCall(
        id=new_id("tool"),
        run_id=run_id,
        agent_id=agent_id,
        tool_name="inspect_dataset",
    )
    experiment = Experiment(id=new_id("experiment"), run_id=run_id, name="baseline")
    follow_up_experiment = Experiment(id=new_id("experiment"), run_id=run_id, name="follow-up")
    records = factory.record_repository()

    for record in (agent, task, tool_call, experiment, follow_up_experiment):
        records.save(record)

    assert records.get(run_id, agent.id, "agent") == agent
    assert records.get(run_id, task.id, "task") == task
    assert records.get(run_id, tool_call.id, "tool_call") == tool_call
    assert records.get(run_id, experiment.id, "experiment") == experiment
    assert records.list_for_run(run_id, "agent") == (agent,)
    assert records.list_for_run(run_id, "task") == (task,)
    assert records.list_for_run(run_id, "tool_call") == (tool_call,)
    assert records.list_for_run(run_id, "experiment") == (experiment, follow_up_experiment)
    assert isinstance(records, RecordRepository)


def assert_event_and_checkpoint_contract(factory: RepositoryFactory) -> None:
    """Assert event-chain persistence and opaque checkpoint round-trips."""
    run_id = new_id("run")
    events = factory.event_store()
    log = events.open(run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {"source": "conformance"})
    log.append(EventType.RUN_COMPLETED, Actor.system(), {"source": "conformance"})
    reopened = events.open(run_id)

    assert reopened.events() == events.read(run_id)
    assert verify_events(reopened.events()).valid
    assert isinstance(events, EventStore)

    checkpoints = factory.checkpoint_repository()
    checkpoints.put("thread-1", "", b"checkpoint-v1")
    assert checkpoints.get("thread-1", "") == b"checkpoint-v1"
    assert checkpoints.list("thread-1", "") == ("",)
    assert checkpoints.get("missing-thread", "") is None
    assert checkpoints.list("missing-thread", "") == ()
    assert isinstance(checkpoints, CheckpointRepository)

    _assert_checkpoint_namespaces_are_isolated(factory)


_NAMESPACE = "alpha|beta:1"


def _assert_checkpoint_namespaces_are_isolated(factory: RepositoryFactory) -> None:
    """Assert the composite ``(thread_id, checkpoint_ns)`` identity, in both write orders.

    Two threads exercise both write orders: ``thread-1`` puts the root namespace first and a
    LangGraph-shaped nested namespace second; ``thread-2`` puts the nested namespace first and
    the root second. The namespace literal carries LangGraph's own separators (``"|"``, ``":"``)
    so any local-backend encoding is exercised against the real characters it must survive.
    """
    checkpoints = factory.checkpoint_repository()

    checkpoints.put("thread-1", "", b"thread-1-root")
    checkpoints.put("thread-1", _NAMESPACE, b"thread-1-nested")
    checkpoints.put("thread-2", _NAMESPACE, b"thread-2-nested")
    checkpoints.put("thread-2", "", b"thread-2-root")

    reader = factory.checkpoint_repository()
    assert reader.get("thread-2", "") == b"thread-2-root"
    assert reader.get("thread-2", _NAMESPACE) == b"thread-2-nested"
    assert reader.get("thread-1", _NAMESPACE) == b"thread-1-nested"
    assert reader.get("thread-1", "") == b"thread-1-root"
    assert reader.list("thread-1", "") == ("",)
    assert reader.list("thread-1", _NAMESPACE) == (_NAMESPACE,)
    assert reader.list("thread-2", _NAMESPACE) == (_NAMESPACE,)
    assert reader.list("thread-2", "") == ("",)
    assert reader.get("thread-1", "missing-namespace") is None
    assert reader.list("thread-1", "missing-namespace") == ()


def assert_unit_of_work_contract(factory: RepositoryFactory) -> None:
    """Assert atomic commit and rollback for runs, records, sessions and events."""
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="cli")
    run = _run(project_id, session.id)
    record = Experiment(id=new_id("experiment"), run_id=run.id, name="baseline")
    event_store = factory.event_store()
    log = event_store.open(run.id)

    with factory.unit_of_work() as transaction:
        transaction.save_run(run)
        transaction.save_session(session)
        transaction.save_record(record)
        transaction.append_event(log, EventType.RUN_STARTED, Actor.system())

    assert factory.run_repository().get(run.id) == run
    assert factory.session_repository().get(session.id) == session
    assert factory.record_repository().get(run.id, record.id, "experiment") == record
    assert verify_events(event_store.read(run.id)).valid

    failed_run = _run(project_id, session.id)
    failed_log = event_store.open(failed_run.id)

    def fail_transaction() -> NoReturn:
        """Stage writes and fail before the unit of work can commit."""
        with factory.unit_of_work() as transaction:
            transaction.save_run(failed_run)
            transaction.append_event(failed_log, EventType.RUN_STARTED, Actor.system())
            _raise_rollback_requested()

    with pytest.raises(RuntimeError, match="rollback requested"):
        fail_transaction()

    assert factory.run_repository().get(failed_run.id) is None
    assert event_store.read(failed_run.id) == []
