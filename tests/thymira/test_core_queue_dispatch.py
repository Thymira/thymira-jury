"""Focused tests for the durable lifecycle queue boundary.

The broker transports only the strict ``{work_id, run_id}`` projection.  A local lifecycle
repository supplies the durable WorkItem and outbox records; the worker claims that work and
executes it through the same fenced owner loop used by the production worker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from thymira.core import (
    ExecutionDispatchError,
    QueueDispatcher,
    QueueTransportError,
    RunService,
    SessionService,
    build_lifecycle_worker,
    notification_from_bytes,
    notification_to_bytes,
)
from thymira.events import canonical_json, verify_events
from thymira.schemas import (
    Actor,
    DispatchState,
    EventType,
    ExecutionOutcomeKind,
    FailureCause,
    OutboxNotification,
    OwnerReleaseReason,
    Run,
    Session,
    TerminalAuditBinding,
    TurnEnded,
    TurnEndReason,
    WorkItem,
    WorkNotification,
    WorkResult,
    WorkState,
    new_id,
    utc_now,
)
from thymira.state import LocalLifecycleRepository, LocalRunStore, LocalSessionRepository

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from thymira.state import RunHandle


class _MemoryQueue:
    """An in-process strict notification publisher and draining consumer."""

    def __init__(self) -> None:
        self.pending: list[WorkNotification] = []
        self.dead_letters: list[WorkNotification] = []

    def publish(self, notification: WorkNotification) -> None:
        """Queue one identifier-only notification."""
        self.pending.append(notification)

    def consume(self, handler: Callable[[WorkNotification], None]) -> None:
        """Deliver pending messages, dead-lettering handler failures."""
        while self.pending:
            notification = self.pending.pop(0)
            try:
                handler(notification)
            except Exception:  # noqa: BLE001  # mirrors broker poison-message handling
                self.dead_letters.append(notification)


class _RecordingPublisher:
    """Publisher spy used to verify the durable outbox projection."""

    def __init__(self) -> None:
        self.notifications: list[WorkNotification] = []

    def publish(self, notification: WorkNotification) -> None:
        """Record one strict notification."""
        self.notifications.append(notification)


class _FailingPublisher:
    """Publisher representing an unavailable broker."""

    def publish(self, notification: WorkNotification) -> None:
        """Raise without claiming the outbox row was delivered."""
        del notification
        raise QueueTransportError("broker unavailable")


class _RecordingExecutor:
    """Owner-bound executor that records one canonical event per settled work item."""

    def __init__(self, store: LocalRunStore) -> None:
        self._store = store
        self.calls: list[str] = []

    def execute(self, work: WorkItem, owner: RunHandle) -> WorkResult:
        """Append through the live Run handle and return a typed result."""
        self.calls.append(work.work_id)
        owner.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"work_id": work.work_id},
            expected_version=self._store.version(work.run_id),
        )
        return WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED)


def _published_work(
    tmp_path: Path,
) -> tuple[LocalLifecycleRepository, LocalRunStore, Run, WorkItem]:
    """Create one committed Run with a pending durable work item and outbox notification."""
    run_store = LocalRunStore(tmp_path / "runs")
    sessions = LocalSessionRepository(tmp_path / "repositories")
    repository = LocalLifecycleRepository(
        tmp_path,
        run_store=run_store,
        session_repository=sessions,
    )
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="queue-test")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="queue boundary",
    )
    work = WorkItem(
        run_id=run.id,
        ordinal=0,
        kind="run.execute",
        idempotency_key=f"queue:{run.id}",
    )
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    handle.close()
    return repository, run_store, run, work


def test_notification_roundtrip_rejects_non_contract_shapes() -> None:
    """The broker codec accepts exactly the shared two-field notification contract."""
    notification = WorkNotification(work_id=new_id("work"), run_id=new_id("run"))
    assert notification_from_bytes(notification_to_bytes(notification)) == notification

    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        notification_from_bytes(b"not json")
    with pytest.raises(ValueError, match="invalid closed shape"):
        notification_from_bytes(
            canonical_json(
                {
                    "work_id": notification.work_id,
                    "run_id": notification.run_id,
                    "authority": "forbidden",
                }
            ).encode("utf-8")
        )


def test_queue_dispatcher_projects_pending_durable_outbox(tmp_path: Path) -> None:
    """Submission publishes the outbox row and marks it only after broker success."""
    repository, _store, run, work = _published_work(tmp_path)
    publisher = _RecordingPublisher()
    dispatcher = QueueDispatcher(publisher, repository)

    dispatcher.submit(run.id)

    expected = WorkNotification(work_id=work.work_id, run_id=run.id)
    assert publisher.notifications == [expected]
    records = [OutboxNotification.model_validate(raw) for raw in repository.list_outbox()]
    assert len(records) == 1
    assert records[0].published
    with pytest.raises(ExecutionDispatchError, match="no pending"):
        dispatcher.resume(run.id)


def test_queue_dispatcher_keeps_outbox_pending_when_broker_fails(tmp_path: Path) -> None:
    """A publish failure leaves the durable notification available for a later dispatcher."""
    repository, _store, run, _work = _published_work(tmp_path)
    dispatcher = QueueDispatcher(_FailingPublisher(), repository)

    with pytest.raises(ExecutionDispatchError, match="could not publish"):
        dispatcher.submit(run.id)

    records = [OutboxNotification.model_validate(raw) for raw in repository.list_outbox()]
    assert len(records) == 1
    assert not records[0].published


def test_lifecycle_worker_claims_and_settles_one_notification_once(tmp_path: Path) -> None:
    """A redelivered notification cannot execute a settled WorkItem a second time."""
    repository, store, run, work = _published_work(tmp_path)
    queue = _MemoryQueue()
    dispatcher = QueueDispatcher(queue, repository)
    dispatcher.submit(run.id)
    notification = queue.pending[0]
    executor = _RecordingExecutor(store)
    worker = build_lifecycle_worker(queue, repository, "queue-worker", executor.execute)

    worker.run()
    event_count = len(store.events(run.id))
    queue.publish(notification)
    worker.run()

    assert executor.calls == [work.work_id]
    assert len(store.events(run.id)) == event_count
    persisted = repository.get_work(work.work_id)
    assert persisted is not None
    assert persisted.settled_result == WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED)
    assert queue.dead_letters == []


def test_settled_success_is_repaired_without_reexecuting_after_turn_crash(tmp_path: Path) -> None:
    """A crash after result settlement cannot strand a successful turn without its closure."""
    repository, store, run, work = _published_work(tmp_path)
    claim = repository.claim_work(work.work_id, "crashed-worker")
    if claim is None:
        raise AssertionError("test setup could not claim work")
    owner = repository.acquire_owner(run.id, claim.claim_token)
    if owner is None:
        raise AssertionError("test setup could not acquire owner")
    repository.mark_dispatched(claim, owner)
    repository.settle_work(
        claim,
        WorkResult(
            kind=ExecutionOutcomeKind.SUCCEEDED,
            dispatch_state=DispatchState.EFFECT_CONFIRMED,
        ),
        owner,
    )
    repository.release_owner(owner, OwnerReleaseReason.FAILED)

    loop = build_lifecycle_worker(
        _MemoryQueue(),
        repository,
        "recovery-worker",
        lambda _work, _owner: pytest.fail("settled work must not execute again"),
    )
    result = loop.handle(WorkNotification(work_id=work.work_id, run_id=run.id))

    assert result == WorkResult(
        kind=ExecutionOutcomeKind.SUCCEEDED,
        dispatch_state=DispatchState.EFFECT_CONFIRMED,
    )
    assert sum(event.type is EventType.TURN_ENDED for event in store.events(run.id)) == 1
    assert verify_events(store.events(run.id)).valid


@pytest.mark.parametrize("outcome", [ExecutionOutcomeKind.FAILED, ExecutionOutcomeKind.CANCELLED])
def test_non_success_turn_runs_one_terminal_audit_and_replays_idempotently(
    tmp_path: Path,
    outcome: ExecutionOutcomeKind,
) -> None:
    """A failed or cancelled owner turn invokes its audit seam once, including redelivery."""
    repository, store, run, work = _published_work(tmp_path)
    queue = _MemoryQueue()
    queue.publish(WorkNotification(work_id=work.work_id, run_id=run.id))
    audit_calls: list[str] = []

    def audit(
        owner: RunHandle,
        item: WorkItem,
        result: WorkResult,
        binding: TerminalAuditBinding,
    ) -> None:
        audit_calls.append(item.work_id)
        assert result.kind is outcome
        owner.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "work_id": item.work_id,
                "status": "failed-turn-audited",
                "terminal_audit_binding": binding.to_json_dict(),
            },
            expected_version=repository.version(run.id),
        )

    def execute(_work: WorkItem, _owner: RunHandle) -> WorkResult:
        return WorkResult(
            kind=outcome,
            cause=FailureCause(
                code="test_failure",
                phase="test",
                exception_type="TestFailure",
                message="test failed",
            ),
        )

    worker = build_lifecycle_worker(
        queue,
        repository,
        "failed-worker",
        execute,
        terminal_auditor=audit,
    )
    worker.run()
    queue.publish(WorkNotification(work_id=work.work_id, run_id=run.id))
    worker.run()

    assert audit_calls == [work.work_id]
    events = store.events(run.id)
    assert sum(event.type is EventType.TURN_ENDED for event in events) == 1
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in events) == 1
    assert verify_events(events).valid


def test_unrelated_audit_cannot_close_another_settled_exit(tmp_path: Path) -> None:
    """Recovery reopens a terminal audit when the only completion is unrelated."""
    repository, store, run, work = _published_work(tmp_path)
    claim = repository.claim_work(work.work_id, "crashed-worker")
    if claim is None:
        raise AssertionError("test setup could not claim work")
    owner = repository.acquire_owner(run.id, claim.claim_token)
    if owner is None:
        raise AssertionError("test setup could not acquire owner")
    result = WorkResult(
        kind=ExecutionOutcomeKind.FAILED,
        cause=FailureCause(
            code="test_failure",
            phase="test",
            exception_type="TestFailure",
            message="test failed",
        ),
    )
    repository.mark_dispatched(claim, owner)
    repository.settle_work(claim, result, owner)
    with repository.run_handle(owner) as handle:
        handle.append(
            EventType.TURN_ENDED,
            Actor.system(),
            TurnEnded(
                turn_id=new_id("turn"),
                run_id=run.id,
                work_ids=(work.work_id,),
                step_count=0,
                end_reason=TurnEndReason.FAILED,
                cause=result.cause,
            ).to_json_dict(),
            expected_version=repository.version(run.id),
        )
        # This completion follows the work's turn but belongs to no terminal scope.
        handle.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {"status": "unrelated-audit"},
            expected_version=repository.version(run.id),
        )
        handle.close(OwnerReleaseReason.FAILED)

    audit_calls: list[str] = []

    def audit(
        owner: RunHandle,
        item: WorkItem,
        settled: WorkResult,
        binding: TerminalAuditBinding,
    ) -> None:
        audit_calls.append(item.work_id)
        assert settled == result
        owner.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {"terminal_audit_binding": binding.to_json_dict()},
            expected_version=repository.version(run.id),
        )

    worker = build_lifecycle_worker(
        _MemoryQueue(),
        repository,
        "recovery-worker",
        lambda _work, _owner: pytest.fail("settled work must not execute again"),
        terminal_auditor=audit,
    )
    replayed = worker.handle(WorkNotification(work_id=work.work_id, run_id=run.id))

    assert replayed == result
    assert audit_calls == [work.work_id]
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in store.events(run.id)) == 2
    assert verify_events(store.events(run.id)).valid


def test_lifecycle_worker_recovers_expired_dispatched_work_as_unknown(
    tmp_path: Path,
) -> None:
    """A redelivery after a worker loss records UNKNOWN instead of repeating an external effect."""
    repository, store, run, work = _published_work(tmp_path)
    claim = repository.claim_work(work.work_id, "crashed-worker")
    if claim is None:
        raise AssertionError("test setup could not claim work")
    owner = repository.acquire_owner(run.id, claim.claim_token)
    if owner is None:
        raise AssertionError("test setup could not acquire owner")
    repository.mark_dispatched(claim, owner)
    repository.release_owner(owner, OwnerReleaseReason.FAILED)
    raw = repository._files.read_json(repository._files.work_path(work.work_id))
    if raw is None:
        raise AssertionError("test setup lost work item")
    raw["claim_expires_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
    repository._files.write_json(
        repository._files.work_path(work.work_id),
        raw,
    )

    queue = _MemoryQueue()
    queue.publish(WorkNotification(work_id=work.work_id, run_id=run.id))
    audited: list[str] = []

    def audit(
        owner: RunHandle,
        item: WorkItem,
        result: WorkResult,
        binding: TerminalAuditBinding,
    ) -> None:
        audited.append(item.work_id)
        assert result.kind is ExecutionOutcomeKind.UNKNOWN
        owner.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "work_id": item.work_id,
                "status": "recovered",
                "terminal_audit_binding": binding.to_json_dict(),
            },
            expected_version=repository.version(run.id),
        )

    worker = build_lifecycle_worker(
        queue,
        repository,
        "recovery-worker",
        lambda _work, _owner: pytest.fail("expired dispatched work must not execute again"),
        terminal_auditor=audit,
    )
    worker.run()

    result = repository.get_work_result(work.work_id)
    assert result is not None
    assert result.kind is ExecutionOutcomeKind.UNKNOWN
    assert result.dispatch_state is DispatchState.EFFECT_UNKNOWN
    persisted = repository.get_work(work.work_id)
    assert persisted is not None
    assert persisted.state is WorkState.SETTLED
    assert any(event.type is EventType.TURN_ENDED for event in store.events(run.id))
    assert audited == [work.work_id]
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in store.events(run.id)) == 1
    assert verify_events(store.events(run.id)).valid


def test_run_service_uses_durable_queue_publication(tmp_path: Path) -> None:
    """Run creation commits a durable work item before QueueDispatcher publishes it."""
    store = LocalRunStore(tmp_path / "runs")
    sessions = LocalSessionRepository(tmp_path / "repositories")
    session_service = SessionService(sessions)
    repository = LocalLifecycleRepository(
        tmp_path,
        run_store=store,
        session_repository=sessions,
    )
    queue = _MemoryQueue()
    service = RunService(
        store,
        session_service,
        dispatcher=QueueDispatcher(queue, repository),
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
        lifecycle_repository=repository,
    )
    session = session_service.create_session(project_id=new_id("project"), client="queue-test")

    created = service.create_run(
        session.id,
        "queue me",
        actor=Actor.system(),
        workspace=tmp_path,
        dispatch=True,
    )

    assert created.id
    assert len(queue.pending) == 1
    assert queue.pending[0].run_id == created.id
    outbox = [OutboxNotification.model_validate(raw) for raw in repository.list_outbox()]
    assert len(outbox) == 1
    assert queue.pending[0].work_id == outbox[0].work_id
