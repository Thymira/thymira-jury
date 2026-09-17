"""Independent process evidence for the local lifecycle ownership slice.

The repository, filesystem locks, staged publication, and subprocess workers are real.
Only the broker notification and model execution boundary are faked, keeping these tests
focused on durable ownership and publication behavior.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.core import GraphFactory

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import AgentCatalog, AgentContext, AgentRunner, AgentSpec
from thymira.agents.llm.routing import Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.settlement import delegation_key as compute_delegation_key
from thymira.core import (
    LifecycleOwnerLoop,
    LifecycleRunWorker,
    OrchestratorBoard,
    OrchestratorBoardError,
    RunController,
    RunService,
    RunTransitionKind,
    SessionService,
    build_lifecycle_worker,
    build_production_lifecycle_worker,
)
from thymira.events import InMemoryEventLog, canonical_json, verify_events
from thymira.schemas import (
    Actor,
    ActorKind,
    AuthorityProof,
    ControlInput,
    ControlInputKind,
    ControlInputState,
    DagBoard,
    DispatchState,
    EventType,
    ExecutionOutcomeKind,
    InboxLane,
    JsonObject,
    ModelRoutePolicy,
    OutboxNotification,
    OwnerReleaseReason,
    PreparedPublication,
    Run,
    RunOwnerHandle,
    Session,
    StopReason,
    SubagentResult,
    Task,
    TerminalAuditBinding,
    TurnEnded,
    TurnEndReason,
    WorkClaim,
    WorkItem,
    WorkNotification,
    WorkResult,
    WorkState,
    new_id,
    utc_now,
)
from thymira.state import (
    BoardConflictError,
    HmacAuthorityVerifier,
    IdempotencyConflictError,
    LifecycleBackendUnavailableError,
    LifecycleError,
    LocalDagRepository,
    LocalLifecycleRepository,
    LocalRunStore,
    LocalSessionRepository,
    OwnerBusyError,
    RunHandle,
    StaleOwnerError,
    sign_authority,
)
from thymira.state.postgres import PgLifecycleRepository

pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_AUTHORITY_SECRET = b"lifecycle-test-secret"


class _FailingLinkRepository(LocalLifecycleRepository):
    """Inject the materialisation-to-link publication fault boundary."""

    def _link_session(self, prepared: PreparedPublication) -> Session:
        """Fail before the inverse Session link is persisted."""
        del prepared
        raise OSError("simulated publication fault")


class _FailingMaterialisationRepository(LocalLifecycleRepository):
    """Fail before Run creation to prove an implicit Session remains a private draft."""

    def _materialise_run(
        self,
        prepared: PreparedPublication,
        owner: RunOwnerHandle,
    ) -> None:
        del prepared, owner
        raise OSError("simulated materialisation fault")


def _new_publication(tmp_path: Path) -> tuple[LocalLifecycleRepository, Session, Run, WorkItem]:
    """Build a fresh repository and a matching session, Run and initial work item."""
    repository = LocalLifecycleRepository(
        tmp_path / "state",
        authority_verifier=HmacAuthorityVerifier(
            _TEST_AUTHORITY_SECRET,
            issuer_process_id="api-test",
            issuer_key_id="test-key",
        ),
    )
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="test")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="lifecycle evidence",
    )
    work = WorkItem(
        run_id=run.id,
        ordinal=0,
        kind="initial",
        idempotency_key=f"initial-{run.id}",
    )
    return repository, session, run, work


def _authority(command: ControlInput) -> AuthorityProof:
    """Create a proof bound to the exact command fields used by the local repository."""
    proof = AuthorityProof(
        proof_id=new_id("proof"),
        actor=Actor(kind=ActorKind.HUMAN, id="human-1", authenticated=True),
        authenticated_principal_id="principal-1",
        authenticated_role="operator",
        permissions=("run:control",),
        session_id=command.session_id,
        run_id=command.run_id,
        issuer_process_id="api-test",
        issuer_key_id="test-key",
        request_id=f"request-{command.input_id}",
        signature="pending-signature",
        input_id=command.input_id,
        payload_sha256=hashlib.sha256(canonical_json(command.payload).encode("utf-8")).hexdigest(),
        binding_sha256=command.binding_sha256(),
        requested_lane=command.requested_lane,
        effective_lane=command.effective_lane,
        kind=command.kind,
        target_turn_id=command.target_turn_id,
        target_work_ids=command.target_work_ids,
        idempotency_key=command.idempotency_key,
    )
    return sign_authority(proof, _TEST_AUTHORITY_SECRET)


def _control(run: Run, session: Session, *, key: str = "control-1") -> ControlInput:
    """Build one authenticated, exact-bound follow-up command."""
    input_id = new_id("input")
    command = ControlInput(
        input_id=input_id,
        run_id=run.id,
        session_id=session.id,
        requested_lane=InboxLane.NEXT_TURN,
        kind=ControlInputKind.FOLLOWUP,
        payload={"prompt": "continue"},
        idempotency_key=key,
        authority=AuthorityProof(
            proof_id=new_id("proof"),
            actor=Actor(kind=ActorKind.HUMAN, id="human-1", authenticated=True),
            authenticated_principal_id="principal-1",
            authenticated_role="operator",
            session_id=session.id,
            run_id=run.id,
            issuer_process_id="api-test",
            issuer_key_id="test-key",
            request_id=f"request-{key}",
            signature="test-signature",
            input_id=input_id,
            payload_sha256="0" * 64,
            binding_sha256="0" * 64,
            requested_lane=InboxLane.NEXT_TURN,
            kind=ControlInputKind.FOLLOWUP,
            idempotency_key=key,
        ),
    )
    return command.model_copy(update={"authority": _authority(command)})


def test_publication_is_private_until_linked_and_leaves_work_outbox(tmp_path: Path) -> None:
    """A prepared publication is hidden, then exposes one linked chain and durable notification."""
    repository, session, run, work = _new_publication(tmp_path)

    prepared = repository.prepare_publication(session, run, work)
    assert not repository.is_visible(run.id)
    assert repository._sessions.get(session.id) is None
    assert not repository._runs._run_dir(run.id).exists()

    handle = repository.commit_publication(prepared)
    try:
        assert repository.is_visible(run.id)
        assert handle.receipt is not None
        assert handle.receipt.work_ids == (work.work_id,)
        linked_session = repository._sessions.get(session.id)
        assert linked_session is not None
        assert linked_session.run_ids == (run.id,)
        persisted_work = repository._files.read_json(repository._files.work_path(work.work_id))
        assert persisted_work is not None
        assert persisted_work["state"] == WorkState.PENDING.value
        notifications = repository.list_outbox()
        assert any(
            notification.get("work_id") == work.work_id and notification.get("run_id") == run.id
            for notification in notifications
        )
    finally:
        handle.close()
    reopened = repository.commit_publication(prepared)
    try:
        assert reopened.receipt == handle.receipt
    finally:
        reopened.close()


def test_publication_fault_window_recovers_without_half_visible_relation(tmp_path: Path) -> None:
    """A failure after Run materialisation remains hidden and is completed by recovery."""
    repository, session, run, work = _new_publication(tmp_path)
    prepared = repository.prepare_publication(session, run, work)
    failing = _FailingLinkRepository(repository.root)
    with pytest.raises(OSError, match="simulated publication fault"):
        failing.commit_publication(prepared)

    assert not repository.is_visible(run.id)
    assert repository._sessions.get(session.id) is None
    reader_script = """
import sys
from pathlib import Path
from thymira.schemas import OwnerReleaseReason
from thymira.state import LocalLifecycleRepository

repo = LocalLifecycleRepository(Path(sys.argv[1]))
try:
    repo.get_run(sys.argv[2])
except FileNotFoundError:
    Path(sys.argv[3]).write_text("hidden", encoding="utf-8")
else:
    Path(sys.argv[3]).write_text("visible", encoding="utf-8")
"""
    reader_marker = tmp_path / "reader.txt"
    subprocess.run(
        [sys.executable, "-c", reader_script, str(repository.root), run.id, str(reader_marker)],
        cwd=_REPO_ROOT,
        check=True,
    )
    assert reader_marker.read_text(encoding="utf-8") == "hidden"
    receipts = repository.recover_publications()
    assert [receipt.run_id for receipt in receipts] == [run.id]
    assert repository.is_visible(run.id)
    assert verify_events(repository.events(run.id)).valid


def test_publication_idempotency_is_durable_and_request_bound(tmp_path: Path) -> None:
    """A restarted repository returns the same committed publication for an exact request."""
    repository, session, run, work = _new_publication(tmp_path)
    key = "create-request-1"
    digest = "a" * 64
    prepared = repository.prepare_publication(
        session,
        run,
        work,
        idempotency_key=key,
        request_binding_sha256=digest,
    )
    first = repository.commit_publication(prepared)
    assert first.receipt is not None
    receipt = first.receipt

    concurrent_retry = repository.prepare_publication(
        session,
        Run(
            id=new_id("run"),
            project_id=run.project_id,
            session_id=session.id,
            prompt=run.prompt,
        ),
        WorkItem(
            run_id=new_id("run"),
            ordinal=0,
            kind="initial",
            idempotency_key="retry-work",
        ),
        idempotency_key=key,
        request_binding_sha256=digest,
    )
    concurrent = repository.commit_publication(concurrent_retry)
    try:
        assert not concurrent.is_live_owner
        assert concurrent.receipt == receipt
        with pytest.raises(StaleOwnerError, match="receipt-only"):
            concurrent.append(EventType.AGENT_MESSAGE, Actor.system(), {})
    finally:
        concurrent.close()
    first.close()

    restarted = LocalLifecycleRepository(
        tmp_path / "state",
        authority_verifier=HmacAuthorityVerifier(
            _TEST_AUTHORITY_SECRET,
            issuer_process_id="api-test",
            issuer_key_id="test-key",
        ),
    )
    retry = restarted.prepare_publication(
        session,
        Run(
            id=new_id("run"),
            project_id=run.project_id,
            session_id=session.id,
            prompt=run.prompt,
        ),
        WorkItem(
            run_id=new_id("run"),
            ordinal=0,
            kind="initial",
            idempotency_key="different-work",
        ),
        idempotency_key=key,
        request_binding_sha256=digest,
    )
    assert retry.publication_id == prepared.publication_id
    second = restarted.commit_publication(retry)
    try:
        assert second.receipt == receipt
    finally:
        second.close()

    with pytest.raises(IdempotencyConflictError):
        restarted.prepare_publication(
            session,
            run,
            work,
            idempotency_key=key,
            request_binding_sha256="b" * 64,
        )


def test_run_service_publishes_implicit_session_atomically(tmp_path: Path) -> None:
    """A new implicit Session stays private through a fault and retries with one receipt."""
    root = tmp_path / "runtime"
    project_id = new_id("project")
    key = "implicit-session-request"
    actor = Actor.system()

    failing_runs = LocalRunStore(root / "runs")
    failing_sessions = LocalSessionRepository(root / "repositories")
    failing_lifecycle = _FailingMaterialisationRepository(
        root,
        run_store=failing_runs,
        session_repository=failing_sessions,
    )
    failing_service = RunService(
        failing_runs,
        SessionService(failing_sessions),
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
        lifecycle_repository=failing_lifecycle,
    )
    with pytest.raises(OSError, match="materialisation fault"):
        failing_service.create_run(
            None,
            "implicit session",
            actor=actor,
            workspace=tmp_path,
            project_id=project_id,
            session_client="test",
            idempotency_key=key,
            dispatch=False,
        )
    assert failing_sessions.list(project_id=project_id).items == ()
    assert not any((root / "runs").glob("*/run.json"))

    restarted_runs = LocalRunStore(root / "runs")
    restarted_sessions = LocalSessionRepository(root / "repositories")
    restarted_lifecycle = LocalLifecycleRepository(
        root,
        run_store=restarted_runs,
        session_repository=restarted_sessions,
    )
    restarted_service = RunService(
        restarted_runs,
        SessionService(restarted_sessions),
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
        lifecycle_repository=restarted_lifecycle,
    )
    run, receipt = restarted_service.create_run_published(
        None,
        "implicit session",
        actor=actor,
        workspace=tmp_path,
        project_id=project_id,
        session_client="test",
        idempotency_key=key,
        dispatch=False,
    )
    assert run.id == receipt.run_id
    session = restarted_sessions.get(receipt.session_id)
    assert session is not None
    assert session.run_ids == (run.id,)

    retry, retry_receipt = restarted_service.create_run_published(
        None,
        "implicit session",
        actor=actor,
        workspace=tmp_path,
        project_id=project_id,
        session_client="test",
        idempotency_key=key,
        dispatch=False,
    )
    assert retry == run
    assert retry_receipt == receipt


def test_malformed_private_manifest_hides_materialised_run(tmp_path: Path) -> None:
    """A reader fails closed when a private publication record cannot be validated."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    handle.close()
    malformed = repository._files.publications / "publication_bad.json"
    malformed.write_text(
        json.dumps({"status": "prepared", "publication": {"run": {"id": run.id}}}) + "\n",
        encoding="utf-8",
    )
    assert not repository.is_visible(run.id)
    with pytest.raises(FileNotFoundError):
        repository.get_run(run.id)


def test_claim_and_settlement_are_exclusive_and_turn_end_is_chainable(tmp_path: Path) -> None:
    """Duplicate notifications produce one claim/effect and a valid canonical event chain."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    try:
        claim = repository.claim_work(work.work_id, "worker-a")
        assert claim is not None
        assert repository.claim_work(work.work_id, "worker-b") is None
        repository.mark_dispatched(claim, handle.owner)
        settled = repository.settle_work(
            claim,
            WorkResult(
                kind=ExecutionOutcomeKind.SUCCEEDED,
                dispatch_state=DispatchState.EFFECT_CONFIRMED,
            ),
            handle.owner,
        )
        assert settled.state is WorkState.SETTLED
        ended = TurnEnded(
            turn_id=new_id("turn"),
            run_id=run.id,
            work_ids=(work.work_id,),
            step_count=1,
            end_reason=TurnEndReason.COMPLETED,
        )
        event = handle.append(
            EventType.TURN_ENDED,
            Actor.system(),
            ended.to_json_dict(),
            expected_version=repository.version(run.id),
        )
        assert event.type is EventType.TURN_ENDED
        assert sum(item.state is WorkState.SETTLED for item in [settled]) == 1
        assert verify_events(repository.events(run.id)).valid
    finally:
        handle.close()


def test_forged_or_released_owner_cannot_write(tmp_path: Path) -> None:
    """Matching public owner fields do not confer authority after forgery or release."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    forged = type(handle.owner).model_validate(handle.owner.model_dump(mode="json"))
    with pytest.raises(StaleOwnerError, match="live process lock"):
        repository.append_owned(forged, EventType.AGENT_MESSAGE, Actor.system(), {})

    handle.close()
    with pytest.raises(StaleOwnerError):
        repository.append_owned(handle.owner, EventType.AGENT_MESSAGE, Actor.system(), {})


def test_owner_lock_is_per_run_and_epoch_survives_process_death(tmp_path: Path) -> None:
    """Independent processes race one Run lock, then a fresh owner receives the next epoch."""
    repository, session, run, work = _new_publication(tmp_path)
    prepared = repository.prepare_publication(session, run, work)
    handle = repository.commit_publication(prepared)
    first_epoch = handle.epoch
    handle.close()

    script = """
import sys
import time
from pathlib import Path
from thymira.state import LocalLifecycleRepository

root, run_id, marker, hold = sys.argv[1:]
repo = LocalLifecycleRepository(Path(root))
owner = repo.acquire_owner(run_id, "subprocess")
if owner is None:
    Path(marker).write_text("busy", encoding="utf-8")
    raise SystemExit(0)
Path(marker).write_text(str(owner.epoch), encoding="utf-8")
if hold == "yes":
    while True:
        time.sleep(0.1)
else:
    repo.release_owner(owner, OwnerReleaseReason.COMPLETED)
"""
    root = str(repository.root)
    first_marker = tmp_path / "owner-one.txt"
    second_marker = tmp_path / "owner-two.txt"
    first = subprocess.Popen(
        [sys.executable, "-c", script, root, run.id, str(first_marker), "yes"],
        cwd=_REPO_ROOT,
    )
    try:
        deadline = time.monotonic() + 10
        while not first_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert first_marker.read_text(encoding="utf-8") == str(first_epoch + 1)
        second = subprocess.run(
            [sys.executable, "-c", script, root, run.id, str(second_marker), "no"],
            cwd=_REPO_ROOT,
            check=True,
        )
        assert second.returncode == 0
        assert second_marker.read_text(encoding="utf-8") == "busy"
    finally:
        if first.poll() is None:
            first.terminate()
        first.wait(timeout=10)

    recovered = repository.recover_owner(run.id, "recovery")
    assert recovered is not None
    try:
        assert recovered.epoch == first_epoch + 2
    finally:
        repository.release_owner(recovered, OwnerReleaseReason.RECOVERED)


def test_duplicate_work_notifications_claim_once_across_processes(tmp_path: Path) -> None:
    """Two independent workers racing one notification produce one durable claim."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    handle.close()
    script = """
import sys
import time
from pathlib import Path
from thymira.state import LocalLifecycleRepository

root, work_id, ready, start, result = sys.argv[1:]
Path(ready).touch()
deadline = time.monotonic() + 10
while not Path(start).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
claim = LocalLifecycleRepository(Path(root)).claim_work(work_id, Path(result).stem)
Path(result).write_text("claimed" if claim is not None else "none", encoding="utf-8")
"""
    start = tmp_path / "claim-start"
    ready_paths = [tmp_path / "claim-ready-a", tmp_path / "claim-ready-b"]
    result_paths = [tmp_path / "claim-result-a", tmp_path / "claim-result-b"]
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(repository.root),
                work.work_id,
                str(ready),
                str(start),
                str(result),
            ],
            cwd=_REPO_ROOT,
        )
        for ready, result in zip(ready_paths, result_paths, strict=True)
    ]
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(path.exists() for path in ready_paths)
        start.touch()
        for worker in workers:
            worker.wait(timeout=10)
            assert worker.returncode == 0
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)
    assert sorted(path.read_text(encoding="utf-8") for path in result_paths) == ["claimed", "none"]
    assert repository.claim_work(work.work_id, "after-race") is None


def test_expired_claim_recovery_never_retries_dispatched_effect(tmp_path: Path) -> None:
    """Undispatched claims requeue, while dispatched expiry settles as unknown exactly once."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    try:
        first = repository.claim_work(work.work_id, "worker-a")
        assert first is not None
        work_path = repository._files.work_path(work.work_id)
        raw = repository._files.read_json(work_path)
        assert raw is not None
        repository._files.write_json(
            work_path,
            {**raw, "claim_expires_at": (utc_now() - timedelta(seconds=1)).isoformat()},
        )
        requeued = repository.recover_expired_work(first, handle.owner)
        assert requeued.state is WorkState.PENDING

        second = repository.claim_work(work.work_id, "worker-b")
        assert second is not None
        repository.mark_dispatched(second, handle.owner)
        raw = repository._files.read_json(work_path)
        assert raw is not None
        repository._files.write_json(
            work_path,
            {**raw, "claim_expires_at": (utc_now() - timedelta(seconds=1)).isoformat()},
        )
        unknown = repository.recover_expired_work(second, handle.owner)
        assert unknown.state is WorkState.SETTLED
        assert unknown.dispatch_state is DispatchState.EFFECT_UNKNOWN
        assert repository.claim_work(work.work_id, "worker-c") is None
        with pytest.raises(StaleOwnerError):
            repository.settle_work(
                second,
                WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED),
                handle.owner,
            )
    finally:
        handle.close()


def test_control_input_binding_idempotency_and_tamper_refusal(tmp_path: Path) -> None:
    """Durable controls return the same receipt and refuse payload or proof replay."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    handle.close()

    command = _control(run, session)
    receipt = repository.enqueue_control(command)
    duplicate = repository.enqueue_control(command)
    assert receipt.input_id == duplicate.input_id
    assert duplicate.duplicate

    changed = command.model_copy(update={"payload": {"prompt": "tampered"}})
    with pytest.raises(PermissionError, match="binding"):
        repository.enqueue_control(changed)

    spoofed_issuer = command.model_copy(
        update={"authority": command.authority.model_copy(update={"issuer_process_id": "spoofed"})}
    )
    with pytest.raises(PermissionError, match="signature is not trusted"):
        repository.enqueue_control(spoofed_issuer)

    spoofed_signature = command.model_copy(
        update={"authority": command.authority.model_copy(update={"signature": "0" * 64})}
    )
    with pytest.raises(PermissionError, match="signature is not trusted"):
        repository.enqueue_control(spoofed_signature)

    replayed = command.model_copy(
        update={"input_id": new_id("input"), "idempotency_key": "control-replay"}
    )
    with pytest.raises(PermissionError, match="control input"):
        repository.enqueue_control(replayed)

    reused_command = _control(run, session, key="proof-replay")
    reused_authority = _authority(reused_command).model_copy(
        update={"proof_id": command.authority.proof_id}
    )
    reused_proof = reused_command.model_copy(
        update={"authority": sign_authority(reused_authority, _TEST_AUTHORITY_SECRET)}
    )
    with pytest.raises(IdempotencyConflictError, match="proof"):
        repository.enqueue_control(reused_proof)

    assert len(repository.controls(run.id)) == 1
    conflicted = _control(run, session, key=command.idempotency_key).model_copy(
        update={"payload": {"prompt": "different"}}
    )
    conflicted = conflicted.model_copy(update={"authority": _authority(conflicted)})
    with pytest.raises(IdempotencyConflictError):
        repository.enqueue_control(conflicted)


def test_postgres_lifecycle_constructs_without_mixing_local_state() -> None:
    """Construction selects PostgreSQL while unauthenticated controls still fail closed."""
    repository = PgLifecycleRepository("postgresql+psycopg://unused")
    assert repository.engine.dialect.name == "postgresql"
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="test")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="construction",
    )
    with pytest.raises(LifecycleBackendUnavailableError, match="authority verification"):
        repository.enqueue_control(_control(run, session))


def test_publication_rejects_existing_owner(tmp_path: Path) -> None:
    """A publication cannot steal a live Run owner from a worker."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    prepared_again = repository.prepare_publication(session, run, work)
    try:
        with pytest.raises(OwnerBusyError):
            repository.commit_publication(prepared_again)
    finally:
        handle.close()


def test_owner_loop_applies_goal_control_and_replays_settled_result(tmp_path: Path) -> None:
    """The one owner loop applies a goal command and leaves one replayable work result."""
    repository, session, run, work = _new_publication(tmp_path)
    publication = repository.prepare_publication(session, run, work)
    publication_handle = repository.commit_publication(publication)
    publication_handle.close()

    plan = _control(run, session, key="plan-control").model_copy(
        update={"kind": ControlInputKind.PLAN}
    )
    plan = plan.model_copy(update={"authority": _authority(plan)})
    repository.enqueue_control(plan)

    class _BoardConsumer:
        def apply_goal_control(
            self,
            command: ControlInput,
            live_owner: RunHandle,
        ) -> JsonObject:
            assert command.kind is ControlInputKind.PLAN
            live_owner.append(
                EventType.AGENT_MESSAGE,
                Actor.system(),
                {"control_input_id": command.input_id},
                expected_version=repository.version(run.id),
            )
            return {"accepted": True}

    owner_loop = LifecycleOwnerLoop(
        repository,
        "inline-owner",
        goal_board_consumer=_BoardConsumer(),
    )
    result = owner_loop.process(
        work.work_id,
        run.id,
        lambda _work, _owner: WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED),
    )

    assert result is not None
    assert result.kind is ExecutionOutcomeKind.SUCCEEDED
    replayed = repository.get_work_result(work.work_id)
    assert replayed == result
    persisted_work = repository.get_work(work.work_id)
    assert persisted_work is not None
    assert persisted_work.state is WorkState.SETTLED
    assert repository.controls(run.id)[0].state is ControlInputState.APPLIED
    assert verify_events(repository.events(run.id)).valid


def test_owner_loop_host_pause_records_initiator_and_aborted_turn(tmp_path: Path) -> None:
    """A host pause closes an unstarted turn with its authenticated initiator and pause reason."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    setup_owner = repository.acquire_owner(run.id, "setup-worker")
    if setup_owner is None:
        raise AssertionError("test setup could not acquire owner")
    with repository.run_handle(setup_owner) as setup_handle:
        setup_controller = RunController(repository._runs, writer=setup_handle.append_transition)
        setup_controller.advance(run.id, RunTransitionKind.START)
        setup_controller.advance(run.id, RunTransitionKind.BEGIN_EXECUTION)

    pause = _control(run, session, key="host-pause").model_copy(
        update={"kind": ControlInputKind.HOST_PAUSE}
    )
    pause = pause.model_copy(update={"authority": _authority(pause)})
    repository.enqueue_control(pause)

    from thymira.core.worker import _OwnerControlConsumer  # composition test

    audited: list[str] = []

    def audit(
        owner: RunHandle,
        item: WorkItem,
        result: WorkResult,
        binding: TerminalAuditBinding,
    ) -> None:
        audited.append(item.work_id)
        assert result.kind is ExecutionOutcomeKind.CANCELLED
        owner.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "work_id": item.work_id,
                "status": "host-paused",
                "terminal_audit_binding": binding.to_json_dict(),
            },
            expected_version=repository.version(run.id),
        )

    owner_loop = LifecycleOwnerLoop(
        repository,
        "pause-worker",
        control_consumer=_OwnerControlConsumer(repository._runs, repository),
        terminal_auditor=audit,
    )
    executed = False

    def execute(_work: WorkItem, _owner: RunHandle) -> WorkResult:
        nonlocal executed
        executed = True
        pytest.fail("a host pause must prevent graph dispatch")

    result = owner_loop.process(work.work_id, run.id, execute)

    assert result is not None
    assert result.kind is ExecutionOutcomeKind.CANCELLED
    assert result.cause is not None
    assert result.cause.code == "host_paused_before_dispatch"
    assert result.payload["aborted_before_dispatch"] is True
    assert result.payload["dispatched"] is False
    initiator = result.payload["initiator"]
    assert isinstance(initiator, dict)
    assert initiator["id"] == "human-1"
    assert not executed
    assert audited == [work.work_id]

    ended = [event for event in repository.events(run.id) if event.type is EventType.TURN_ENDED]
    assert len(ended) == 1
    assert ended[0].payload["end_reason"] == TurnEndReason.PAUSED.value
    transitions = [
        event for event in repository.events(run.id) if event.type is EventType.RUN_TRANSITIONED
    ]
    assert transitions[-1].payload["initiator"]["id"] == "human-1"
    assert repository.controls(run.id)[0].state is ControlInputState.APPLIED
    assert sum(event.type is EventType.AUDIT_COMPLETED for event in repository.events(run.id)) == 1
    assert verify_events(repository.events(run.id)).valid


def test_owner_loop_leaves_next_step_control_for_the_live_model_step(tmp_path: Path) -> None:
    """A run work item consumes next-turn input while leaving next-step input pending."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()

    followup = _control(run, session, key="lane-followup")
    steer = _control(run, session, key="lane-steer").model_copy(
        update={
            "kind": ControlInputKind.STEER,
            "requested_lane": InboxLane.NEXT_STEP,
        }
    )
    steer = steer.model_copy(update={"authority": _authority(steer)})
    repository.enqueue_control(followup)
    repository.enqueue_control(steer)
    applied: list[str] = []

    class _Consumer:
        def apply_control(
            self,
            command: ControlInput,
            live_owner: RunHandle,
        ) -> JsonObject:
            applied.append(command.input_id)
            return {"input_id": command.input_id}

    owner_loop = LifecycleOwnerLoop(repository, "lane-worker", control_consumer=_Consumer())
    result = owner_loop.process(
        work.work_id,
        run.id,
        lambda _work, _owner: WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED),
    )

    assert result is not None
    assert applied == [followup.input_id]
    controls = {command.input_id: command for command in repository.controls(run.id)}
    assert controls[followup.input_id].state is ControlInputState.APPLIED
    assert controls[steer.input_id].state is ControlInputState.PENDING


def test_control_claim_transfers_lane_and_replays_idempotently(tmp_path: Path) -> None:
    """One live owner atomically claims a control and can safely replay that claim."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    command = _control(run, session, key="claim-control")
    repository.enqueue_control(command)
    owner = repository.acquire_owner(run.id, "claim-owner")
    assert owner is not None
    try:
        claimed = repository.claim_control(
            command,
            owner,
            effective_lane=InboxLane.NEXT_TURN,
        )
        assert claimed is not None
        assert claimed.state is ControlInputState.CLAIMED
        assert claimed.effective_lane is InboxLane.NEXT_TURN
        assert claimed.claim_owner == owner.owner_id
        assert claimed.claim_token is not None
        assert claimed.claim_attempt == 1
        assert (
            repository.claim_control(
                command,
                owner,
                effective_lane=InboxLane.NEXT_TURN,
            )
            == claimed
        )
        repository.mark_control_applied(claimed, owner)
        assert repository.enqueue_control(command).duplicate
    finally:
        repository.release_owner(owner, OwnerReleaseReason.COMPLETED)


def test_control_claim_rolls_steer_to_next_turn_when_no_model_is_live(tmp_path: Path) -> None:
    """A next-step steer arriving outside a live turn persists its next-turn transfer."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    command = _control(run, session, key="rollover-control").model_copy(
        update={
            "kind": ControlInputKind.STEER,
            "requested_lane": InboxLane.NEXT_STEP,
        }
    )
    command = command.model_copy(update={"authority": _authority(command)})
    repository.enqueue_control(command)
    owner = repository.acquire_owner(run.id, "rollover-owner")
    assert owner is not None
    try:
        claimed = repository.claim_control(
            command,
            owner,
            effective_lane=InboxLane.NEXT_TURN,
        )
        assert claimed is not None
        assert claimed.effective_lane is InboxLane.NEXT_TURN
    finally:
        repository.release_owner(owner, OwnerReleaseReason.COMPLETED)


def test_expired_control_claim_requeues_for_the_next_owner(tmp_path: Path) -> None:
    """A crashed owner leaves a claim that a later owner can reclaim exactly once."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    command = _control(run, session, key="expired-claim")
    repository.enqueue_control(command)
    first_owner = repository.acquire_owner(run.id, "expired-owner-a")
    assert first_owner is not None
    claimed = repository.claim_control(
        command,
        first_owner,
        effective_lane=InboxLane.NEXT_TURN,
    )
    assert claimed is not None
    path = tmp_path / "state" / ".lifecycle" / "controls" / f"{command.input_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["claim_expires_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(raw), encoding="utf-8")
    repository.release_owner(first_owner, OwnerReleaseReason.FAILED)

    second_owner = repository.acquire_owner(run.id, "expired-owner-b")
    assert second_owner is not None
    try:
        reclaimed = repository.claim_control(
            command,
            second_owner,
            effective_lane=InboxLane.NEXT_TURN,
        )
        assert reclaimed is not None
        assert reclaimed.claim_owner == second_owner.owner_id
        assert reclaimed.claim_attempt == 2
    finally:
        repository.release_owner(second_owner, OwnerReleaseReason.COMPLETED)


def test_owner_loop_rejects_the_first_claim_as_a_zero_step_turn(tmp_path: Path) -> None:
    """A rejected first control claim settles one zero-step turn without model evidence."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    command = _control(run, session, key="reject-first")
    repository.enqueue_control(command)

    class _RejectingConsumer:
        def apply_control(
            self,
            command: ControlInput,
            live_owner: RunHandle,
        ) -> JsonObject:
            del command, live_owner
            raise ValueError("policy refused this control")

    owner_loop = LifecycleOwnerLoop(
        repository,
        "rejecting-worker",
        control_consumer=_RejectingConsumer(),
    )
    result = owner_loop.process(
        work.work_id,
        run.id,
        lambda _work, _owner: WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED),
    )

    assert result is not None
    assert result.kind is ExecutionOutcomeKind.FAILED
    assert result.dispatch_state is DispatchState.NEVER_DISPATCHED
    assert repository.controls(run.id)[0].state is ControlInputState.REJECTED
    settled = repository.get_work(work.work_id)
    assert settled is not None
    assert settled.state is WorkState.SETTLED
    ended = [event for event in repository.events(run.id) if event.type is EventType.TURN_ENDED]
    assert len(ended) == 1
    assert ended[0].payload["end_reason"] == TurnEndReason.REJECTED.value
    assert ended[0].payload["step_count"] == 0
    assert ended[0].payload["claimed_input_ids"] == [command.input_id]
    assert not any(event.type is EventType.MODEL_SELECTED for event in repository.events(run.id))
    assert verify_events(repository.events(run.id)).valid


def test_live_model_step_interceptor_claims_only_next_step_inputs(tmp_path: Path) -> None:
    """The live owner hook claims steering text while preserving later-turn follow-ups."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()
    steer = _control(run, session, key="intercept-steer").model_copy(
        update={
            "kind": ControlInputKind.STEER,
            "requested_lane": InboxLane.NEXT_STEP,
        }
    )
    steer = steer.model_copy(update={"authority": _authority(steer)})
    followup = _control(run, session, key="intercept-followup")
    repository.enqueue_control(steer)
    repository.enqueue_control(followup)

    class _Consumer:
        def apply_control(
            self,
            command: ControlInput,
            live_owner: RunHandle,
        ) -> JsonObject:
            del live_owner
            return {"input_id": command.input_id}

    owner = repository.commit_publication(repository.prepare_publication(session, run, work))
    try:
        owner_loop = LifecycleOwnerLoop(repository, "step-owner", control_consumer=_Consumer())
        prompts = owner_loop.intercept_next_model_step(owner)
    finally:
        owner.close()

    assert prompts == ("continue",)
    controls = {item.input_id: item for item in repository.controls(run.id)}
    assert controls[steer.input_id].state is ControlInputState.APPLIED
    assert controls[steer.input_id].effective_lane is InboxLane.NEXT_STEP
    assert controls[followup.input_id].state is ControlInputState.PENDING


def test_owner_loop_rejects_mismatched_notification_before_claim(tmp_path: Path) -> None:
    """A broker payload cannot redirect a durable work item to another Run."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    handle.close()
    owner_loop = LifecycleOwnerLoop(repository, "notification-worker")

    with pytest.raises(LifecycleError, match="run_id"):
        owner_loop.process(
            work.work_id,
            new_id("run"),
            lambda _work, _owner: WorkResult(kind=ExecutionOutcomeKind.SUCCEEDED),
        )
    persisted_work = repository.get_work(work.work_id)
    assert persisted_work is not None
    assert persisted_work.state is WorkState.PENDING


def test_lifecycle_worker_projects_outbox_and_claims_exact_notification(tmp_path: Path) -> None:
    """The production adapter consumes only the outbox's ``{work_id, run_id}`` projection."""
    repository, session, run, work = _new_publication(tmp_path)
    publication_handle = repository.commit_publication(
        repository.prepare_publication(session, run, work)
    )
    publication_handle.close()

    outbox = OutboxNotification(work_id=work.work_id, run_id=run.id)
    notification = outbox.work_notification()
    assert notification == WorkNotification(work_id=work.work_id, run_id=run.id)
    assert notification.to_json_dict() == {"work_id": work.work_id, "run_id": run.id}

    class _Consumer:
        def __init__(self) -> None:
            self.notifications: list[WorkNotification] = []

        def consume(self, handler: Callable[[WorkNotification], None]) -> None:
            self.notifications.append(notification)
            handler(notification)

    consumer = _Consumer()
    worker = build_lifecycle_worker(
        consumer,
        repository,
        "notification-worker",
        lambda item, _owner: WorkResult(
            kind=ExecutionOutcomeKind.SUCCEEDED,
            payload={"work_kind": item.kind},
        ),
    )
    assert isinstance(worker, LifecycleRunWorker)
    worker.run()

    assert consumer.notifications == [notification]
    result = repository.get_work_result(work.work_id)
    assert result is not None
    assert result.payload == {"work_kind": "initial"}


def test_production_worker_builder_uses_real_owner_repository(tmp_path: Path, monkeypatch) -> None:
    """The shipped worker composition claims a real publication from a broker-like consumer."""
    for key in (
        "THYMIRA_LIFECYCLE_BACKEND",
        "THYMIRA_POSTGRES_DSN",
        "THYMIRA_POSTGRES_HOST",
        "THYMIRA_POSTGRES_PORT",
        "THYMIRA_POSTGRES_USER",
        "THYMIRA_POSTGRES_PASSWORD",
        "THYMIRA_POSTGRES_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "state"
    repository_root = root / "repositories"
    session_repository = LocalSessionRepository(repository_root)
    repository = LocalLifecycleRepository(
        root,
        session_repository=session_repository,
    )
    session = Session(id=new_id("session"), project_id=new_id("project"), client="test")
    session_repository.save(session)
    run = Run(
        id=new_id("run"),
        project_id=session.project_id,
        session_id=session.id,
        prompt="production worker composition",
    )
    work = WorkItem(
        run_id=run.id,
        ordinal=0,
        kind="run.execute",
        idempotency_key=f"run-create:{run.id}",
    )
    publication = repository.prepare_publication(session, run, work)
    committed = repository.commit_publication(publication)
    committed.close()
    notification = WorkNotification(work_id=work.work_id, run_id=run.id)

    class _Consumer:
        def consume(self, handler: Callable[[WorkNotification], None]) -> None:
            handler(notification)

    class _Graph:
        def invoke(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {}

    worker = build_production_lifecycle_worker(
        root,
        _Consumer(),
        worker_id="composition-worker",
        graph_factory=cast("GraphFactory", lambda _run: _Graph()),
    )
    worker.run()

    result = repository.get_work_result(work.work_id)
    assert result is not None
    assert result.kind is ExecutionOutcomeKind.SUCCEEDED
    persisted_work = repository.get_work(work.work_id)
    assert persisted_work is not None
    assert persisted_work.state is WorkState.SETTLED
    assert verify_events(repository.events(run.id)).valid


def test_owned_controller_routes_transition_through_live_handle(tmp_path: Path) -> None:
    """A RunController bound to a live handle refuses the same transition after release."""
    repository, session, run, work = _new_publication(tmp_path)
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    controller = RunController(repository._runs, writer=handle.append_transition)
    try:
        controller.advance(run.id, RunTransitionKind.START)
        assert controller.current_state(run.id).stage.value == "planning"
    finally:
        handle.close()
    with pytest.raises(StaleOwnerError):
        controller.advance(run.id, RunTransitionKind.BEGIN_EXECUTION)


class _LocalBoardLifecycleAdapter:
    """Expose the real local owner records through the board's narrow lifecycle seam."""

    def __init__(self, repository: LocalLifecycleRepository) -> None:
        self._repository = repository

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read the owner-published WorkItem from its durable file."""
        raw = self._repository._files.read_json(  # test the owner record directly
            self._repository._files.work_path(work_id)
        )
        return None if raw is None else WorkItem.model_validate(raw)

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read the canonical result persisted by the real lifecycle owner."""
        item = self.get_work(work_id)
        return item.settled_result if item is not None else None

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Delegate claiming to the real lifecycle owner."""
        return self._repository.claim_work(work_id, claimant_id, limit)

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Delegate dispatch marking to the real lifecycle owner."""
        return self._repository.mark_dispatched(claim, owner)

    def settle_work(self, claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> WorkItem:
        """Delegate settlement to the real lifecycle owner."""
        return self._repository.settle_work(claim, result, owner)


class _FailingLocalDagRepository:
    """Inject a durable DAG projection failure while the owner record remains authoritative."""

    def __init__(self, root: Path) -> None:
        self._repository = LocalDagRepository(root)
        self.fail = False

    def get(self, run_id: str) -> DagBoard | None:
        """Read the latest projection even while writes are unavailable."""
        return self._repository.get(run_id)

    def history(self, run_id: str) -> tuple[DagBoard, ...]:
        """Read append-only topology history for restart evidence."""
        return self._repository.history(run_id)

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Fail projection writes only after the owner has persisted the result."""
        if self.fail:
            raise BoardConflictError("permanent topology conflict")
        return self._repository.save(board, expected_revision=expected_revision)


def test_real_task_runner_owner_and_board_recover_a_distinct_invocation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Task and AgentRunner result survive owner settlement and DAG projection restart."""
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "test-model")
    repository = LocalLifecycleRepository(tmp_path / "lifecycle")
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="integration")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="board integration",
    )
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="Profile the credit-risk dataset.",
    )
    spec = AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        max_turns=1,
        max_depth=1,
        system_prompt_ref="prompts/data.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    catalog = AgentCatalog(
        (spec,),
        system_prompts={spec.name: "You are the data-profiling agent."},
        output_schemas={spec.name: DataProfileOutput},
    )
    agent_result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=InMemoryEventLog(run.id),
            provider=ScriptedProvider([DataProfileOutput(row_count=10, columns=("age",))]),
            route_policy=ModelRoutePolicy.from_routes(
                ("test-model",), authority="code-owned-tests"
            ),
        ),
    )
    assert agent_result.output == DataProfileOutput(row_count=10, columns=("age",))
    assert agent_result.output is not None
    delegation_key = compute_delegation_key(
        run_id=run.id,
        task_id=task.id,
        agent_id=task.agent_id,
        parent_agent="root",
        agent=spec.name,
        objective=task.objective,
        delegation_depth=0,
    )
    work = WorkItem(
        work_id=new_id("work"),
        run_id=run.id,
        ordinal=0,
        kind="analyze",
        task_id=task.id,
        objective=task.objective,
        idempotency_key=f"work-{task.id}",
        delegation_depth=0,
        agent_id=task.agent_id,
        agent=spec.name,
        parent_agent="root",
        delegation_key=delegation_key,
    )
    child = SubagentResult(
        run_id=task.run_id,
        task_id=task.id,
        agent_id=task.agent_id,
        agent=spec.name,
        parent_agent="root",
        objective=task.objective,
        delegation_depth=0,
        delegation_key=delegation_key,
        stop_reason=StopReason.COMPLETED,
        result_json=agent_result.output.model_dump_json(),
        result_schema=spec.output_schema_ref,
        diagnostics_limit=4000,
    )
    handle = repository.commit_publication(repository.prepare_publication(session, run, work))
    try:
        lifecycle = _LocalBoardLifecycleAdapter(repository)
        dag = _FailingLocalDagRepository(tmp_path / "dag")
        board = OrchestratorBoard(
            dag,
            run.id,
            lifecycle=lifecycle,
            owner=handle.owner,
            owner_assertion=repository._owners_registry.assert_live,
        )
        board.add_work_item(work)
        claim = board.claim(work.work_id, claimant_id="worker")
        dag.fail = True
        with pytest.raises(OrchestratorBoardError, match="did not publish"):
            board.settle(claim, result=child)
        canonical = lifecycle.get_work_result(work.work_id)
        assert canonical is not None
        assert canonical.source_work_id == work.work_id
        assert canonical.source_task_id == task.id
        assert canonical.source_kind == work.kind
        assert canonical.source_objective == task.objective

        dag.fail = False
        restarted = OrchestratorBoard(
            dag,
            run.id,
            lifecycle=lifecycle,
            owner=handle.owner,
            owner_assertion=repository._owners_registry.assert_live,
        )
        assert restarted.recover_settlement(work.work_id) == child
        assert restarted.snapshot().results == (child,)
    finally:
        handle.close()


__all__ = []
