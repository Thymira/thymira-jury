"""Integration: the RA-STATE-05 conformance suite against the PostgreSQL backends (RA-STATE-04).

The backend-agnostic assertions that RA-STATE-05 shipped for the local backends are run here,
unchanged, over the ``Pg*`` repositories against a dockerized ``postgres:16-alpine``. Proving the
identical suite passes for both backends is what demonstrates the persistence seam is truly
substitutable. A dedicated test also shows an event chain still verifies after being reloaded
from the database, and that a fresh appender continues the same chain.

Real: a live Docker daemon running PostgreSQL, the Alembic migrations building the schema, and
the ``Pg*`` repositories against it. Nothing is faked; the module skips only when Docker is
absent, and never reports a false pass.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, ProgrammingError

from tests.thymira.docker_support import require_docker_daemon
from tests.thymira.repo_conformance import (
    assert_event_and_checkpoint_contract,
    assert_record_contract,
    assert_run_and_session_contract,
    assert_unit_of_work_contract,
)
from thymira.events import canonical_json, verify_events
from thymira.schemas import (
    Actor,
    ActorKind,
    AuthorityProof,
    ControlInput,
    ControlInputKind,
    ControlInputState,
    DispatchState,
    EventType,
    ExecutionOutcomeKind,
    InboxLane,
    OwnerReleaseReason,
    Run,
    RunOwnerHandle,
    Session,
    StopReason,
    SubagentResult,
    WorkClaim,
    WorkItem,
    WorkResult,
    new_id,
    utc_now,
)
from thymira.state import HmacAuthorityVerifier, sign_authority
from thymira.state.postgres import (
    PgCheckpointRepository,
    PgEventStore,
    PgLifecycleRepository,
    PgRecordRepository,
    PgRunRepository,
    PgSessionRepository,
    PgUnitOfWork,
    PostgresSettings,
    create_engine,
    run_migrations,
    tables,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_IMAGE = "postgres:16-alpine"
_READY_TIMEOUT_S = 90.0


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command, raising with its output on failure."""
    return subprocess.run(("docker", *args), capture_output=True, text=True, check=True)


def _published_port(container: str) -> int:
    """Return the ephemeral host port docker mapped to the container's 5432."""
    mapping = _docker("port", container, "5432/tcp").stdout.strip().splitlines()[0]
    return int(mapping.rsplit(":", 1)[1])


def _wait_until_ready(dsn: str, *, timeout_s: float) -> None:
    """Block until the database accepts a connection, or fail after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    last_error: OperationalError | None = None
    while time.monotonic() < deadline:
        engine = sa.create_engine(dsn)
        try:
            with engine.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
        except OperationalError as error:
            last_error = error
            time.sleep(0.5)
        else:
            return
        finally:
            engine.dispose()
    msg = f"PostgreSQL at {dsn} did not become ready within {timeout_s}s: {last_error}"
    raise RuntimeError(msg)


@dataclass(frozen=True, slots=True)
class PgRepositoryFactory:
    """Build fresh PostgreSQL repository handles sharing one engine."""

    engine: Engine

    def run_repository(self) -> PgRunRepository:
        """Return a PostgreSQL run repository."""
        return PgRunRepository(self.engine)

    def session_repository(self) -> PgSessionRepository:
        """Return a PostgreSQL session repository."""
        return PgSessionRepository(self.engine)

    def record_repository(self) -> PgRecordRepository:
        """Return a PostgreSQL child-record repository."""
        return PgRecordRepository(self.engine)

    def event_store(self) -> PgEventStore:
        """Return a PostgreSQL event store."""
        return PgEventStore(self.engine)

    def checkpoint_repository(self) -> PgCheckpointRepository:
        """Return a PostgreSQL checkpoint repository."""
        return PgCheckpointRepository(self.engine)

    def unit_of_work(self) -> PgUnitOfWork:
        """Return a PostgreSQL transaction boundary."""
        return PgUnitOfWork(self.engine)


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """Start a dockerized ``postgres:16-alpine`` and yield its DSN, tearing it down after."""
    require_docker_daemon()
    container = f"thymira-pg-{uuid.uuid4().hex[:12]}"
    try:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=thymira",
            "-e",
            "POSTGRES_PASSWORD=thymira",
            "-e",
            "POSTGRES_DB=thymira",
            "-p",
            "127.0.0.1::5432",
            _IMAGE,
        )
        port = _published_port(container)
        dsn = f"postgresql+psycopg://thymira:thymira@127.0.0.1:{port}/thymira"
        _wait_until_ready(dsn, timeout_s=_READY_TIMEOUT_S)
        yield dsn
    finally:
        subprocess.run(
            ("docker", "rm", "-f", container), capture_output=True, text=True, check=False
        )


@pytest.fixture(scope="session")
def pg_engine(postgres_dsn: str) -> Iterator[Engine]:
    """Migrate the database and yield a pooled engine over it."""
    run_migrations(postgres_dsn)
    engine = create_engine(PostgresSettings(dsn=postgres_dsn))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def unmigrated_postgres_dsn() -> Iterator[str]:
    """Start a dedicated, un-migrated dockerized ``postgres:16-alpine`` and yield its DSN.

    A separate, function-scoped container is required (rather than the session-scoped
    ``postgres_dsn``) because this fixture seeds the *old*, pre-widening ``checkpoints`` shape
    before any migration runs -- exactly what an already-deployed database looks like.
    """
    require_docker_daemon()
    container = f"thymira-pg-{uuid.uuid4().hex[:12]}"
    try:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=thymira",
            "-e",
            "POSTGRES_PASSWORD=thymira",
            "-e",
            "POSTGRES_DB=thymira",
            "-p",
            "127.0.0.1::5432",
            _IMAGE,
        )
        port = _published_port(container)
        dsn = f"postgresql+psycopg://thymira:thymira@127.0.0.1:{port}/thymira"
        _wait_until_ready(dsn, timeout_s=_READY_TIMEOUT_S)
        yield dsn
    finally:
        subprocess.run(
            ("docker", "rm", "-f", container), capture_output=True, text=True, check=False
        )


def test_upgrade_head_does_not_widen_an_already_migrated_checkpoints_table(
    unmigrated_postgres_dsn: str,
) -> None:
    """``alembic upgrade head`` is a no-op against a database that already ran revision 0001.

    Retained platform limitation, not fixed here: ``0001_initial.upgrade()`` is
    ``metadata.create_all(op.get_bind())``, whose default ``checkfirst=True`` skips a table that
    already exists. A database that ran 0001 before this branch's ``checkpoints`` widening lands
    on -- the only kind of database that exists after any prior deployment -- is therefore never
    widened by re-running migrations, and every ``PgCheckpointRepository.put`` afterwards raises
    ``UndefinedColumn``. Seeds the pre-widening shape by hand (``thread_id`` sole primary key, no
    ``checkpoint_ns`` column) and stamps ``alembic_version`` at ``0001``, exactly as a database
    that migrated before this change would look, then runs this branch's migrations against it.
    """
    seed_engine = sa.create_engine(unmigrated_postgres_dsn)
    with seed_engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE checkpoints ("
                "  thread_id TEXT PRIMARY KEY,"
                "  checkpoint BYTEA NOT NULL,"
                "  updated_at TIMESTAMPTZ NOT NULL"
                ")"
            )
        )
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(sa.text("INSERT INTO alembic_version VALUES ('0001')"))
    seed_engine.dispose()

    run_migrations(unmigrated_postgres_dsn)

    inspector = sa.inspect(sa.create_engine(unmigrated_postgres_dsn))
    assert inspector.get_pk_constraint("checkpoints")["constrained_columns"] == ["thread_id"]
    assert "checkpoint_ns" not in {c["name"] for c in inspector.get_columns("checkpoints")}

    repository = PgCheckpointRepository(
        create_engine(PostgresSettings(dsn=unmigrated_postgres_dsn))
    )
    with pytest.raises(ProgrammingError, match="checkpoint_ns"):
        repository.put("t1", "", b"blob")


@pytest.fixture
def pg_factory(pg_engine: Engine) -> PgRepositoryFactory:
    """Return a repository factory over the migrated PostgreSQL database."""
    return PgRepositoryFactory(pg_engine)


def test_run_and_session_repository_contract(pg_factory: PgRepositoryFactory) -> None:
    """The PostgreSQL backend satisfies run and session persistence contracts."""
    assert_run_and_session_contract(pg_factory)


def test_record_repository_contract(pg_factory: PgRepositoryFactory) -> None:
    """The PostgreSQL backend satisfies the child-record persistence contract."""
    assert_record_contract(pg_factory)


def test_event_and_checkpoint_repository_contract(pg_factory: PgRepositoryFactory) -> None:
    """The PostgreSQL backend preserves event chains and checkpoint blobs."""
    assert_event_and_checkpoint_contract(pg_factory)


def test_unit_of_work_contract(pg_factory: PgRepositoryFactory) -> None:
    """The PostgreSQL backend satisfies atomic commit and rollback semantics."""
    assert_unit_of_work_contract(pg_factory)


def test_migration_creates_baseline_tables(pg_engine: Engine) -> None:
    """The Alembic migration materialises every baseline S19 table."""
    present = set(sa.inspect(pg_engine).get_table_names())
    expected = {
        "sessions",
        "runs",
        "agents",
        "tasks",
        "tool_calls",
        "experiments",
        "audit_findings",
        "policy_decisions",
        "events",
        "checkpoints",
    }
    assert expected <= present


def test_checkpoints_table_has_a_composite_namespace_primary_key(pg_engine: Engine) -> None:
    """The ``checkpoints`` primary key is the composite ``(thread_id, checkpoint_ns)`` identity.

    Independent schema oracle: a schema reader that performed none of the writes confirms the
    composite primary key the unchanged 0001 baseline migration materialised.
    """
    constrained = sa.inspect(pg_engine).get_pk_constraint("checkpoints")["constrained_columns"]
    assert constrained == ["thread_id", "checkpoint_ns"]


def test_migration_creates_lifecycle_tables(pg_engine: Engine) -> None:
    """The lifecycle migration materialises every durable owner and work-board table."""
    present = set(sa.inspect(pg_engine).get_table_names())
    expected = {
        "lifecycle_controls",
        "lifecycle_outbox",
        "lifecycle_owner_epochs",
        "lifecycle_outcomes",
        "lifecycle_publications",
        "lifecycle_terminal_bindings",
        "lifecycle_turns",
        "lifecycle_work",
    }
    assert expected <= present


def _expire_postgres_work(
    pg_engine: Engine,
    repository: PgLifecycleRepository,
    claim: WorkClaim,
) -> WorkClaim:
    """Move a test claim into the past while preserving its authoritative document projection."""
    item = repository.get_work(claim.work_id)
    assert item is not None
    expired = item.model_copy(update={"claim_expires_at": utc_now() - timedelta(seconds=1)})
    with pg_engine.begin() as conn:
        conn.execute(
            tables.lifecycle_work.update()
            .where(tables.lifecycle_work.c.id == claim.work_id)
            .values(
                claim_expires_at=expired.claim_expires_at,
                document=canonical_json(expired.to_json_dict()),
            )
        )
    return claim.model_copy(update={"expires_at": expired.claim_expires_at})


def _settle_delegated_result(
    repository: PgLifecycleRepository,
    run: Run,
    item: WorkItem,
    owner: RunOwnerHandle,
) -> None:
    """Prove PostgreSQL compares child source facts with the authoritative WorkItem."""
    claim = repository.claim_work(item.work_id, "worker-child")
    assert claim is not None
    repository.mark_dispatched(claim, owner)
    assert item.task_id is not None
    assert item.objective is not None
    assert item.agent_id is not None
    assert item.agent is not None
    assert item.parent_agent is not None
    assert item.delegation_key is not None
    child = SubagentResult(
        run_id=run.id,
        task_id=item.task_id,
        agent_id=item.agent_id,
        agent=item.agent,
        parent_agent=item.parent_agent,
        objective=item.objective,
        delegation_depth=item.delegation_depth,
        delegation_key=item.delegation_key,
        stop_reason=StopReason.COMPLETED,
        result_json="{}",
        result_schema="builtins:dict",
        diagnostics_limit=0,
    )
    bound_result = WorkResult(
        kind=ExecutionOutcomeKind.SUCCEEDED,
        dispatch_state=DispatchState.EFFECT_CONFIRMED,
        source_work_id=item.work_id,
        source_run_id=run.id,
        source_task_id=item.task_id,
        source_attempt=claim.attempt,
        source_claim_token=claim.claim_token,
        source_ordinal=item.ordinal,
        source_kind=item.kind,
        source_objective=item.objective,
        source_delegation_depth=item.delegation_depth,
        source_agent_id=item.agent_id,
        source_agent=item.agent,
        source_parent_agent=item.parent_agent,
        source_delegation_key=item.delegation_key,
        subagent_result=child,
    )
    foreign_child = child.model_copy(update={"task_id": new_id("task")})
    foreign_result = bound_result.model_copy(
        update={"source_task_id": foreign_child.task_id, "subagent_result": foreign_child}
    )
    with pytest.raises(RuntimeError, match="authoritative work invocation"):
        repository.settle_work(claim, foreign_result, owner)
    settled = repository.settle_work(claim, bound_result, owner)
    assert settled.settled_result == bound_result
    assert repository.get_work_result(item.work_id) == bound_result


def _child_work(run_id: str) -> WorkItem:
    """Build one fully bound child item for the live owner settlement test."""
    item = WorkItem(
        run_id=run_id,
        ordinal=2,
        kind="dispatch-child",
        idempotency_key=f"child-work-{run_id}",
        task_id=new_id("task"),
        objective="profile child",
        agent_id=new_id("agent"),
        agent="data",
        parent_agent="thy",
        delegation_key=hashlib.sha256(b"child-invocation").hexdigest(),
    )
    assert item.task_id is not None
    assert item.objective is not None
    assert item.agent_id is not None
    assert item.agent is not None
    assert item.parent_agent is not None
    assert item.delegation_key is not None
    return item


def test_postgres_lifecycle_publication_claim_and_fenced_settlement(pg_engine: Engine) -> None:
    """Publication, advisory ownership, work claims and embedded results share durable state."""
    repository = PgLifecycleRepository(pg_engine)
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="postgres-test")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="postgres lifecycle",
    )
    work = WorkItem(run_id=run.id, ordinal=0, kind="initial", idempotency_key=f"work-{run.id}")
    unknown_work = WorkItem(
        run_id=run.id,
        ordinal=1,
        kind="unknown-recovery",
        idempotency_key=f"unknown-work-{run.id}",
    )
    child_work = _child_work(run.id)
    handle = repository.commit_publication(
        repository.prepare_publication(session, run, (work, unknown_work, child_work))
    )
    try:
        assert repository.is_visible(run.id)
        linked_session = PgSessionRepository(pg_engine).get(session.id)
        assert linked_session is not None
        assert linked_session.run_ids == (run.id,)
        assert any(
            notification.get("work_id") == work.work_id for notification in repository.list_outbox()
        )
        claim = repository.claim_work(work.work_id, "worker-a")
        assert claim is not None
        assert repository.claim_work(work.work_id, "worker-b") is None
        assert (
            repository.mark_dispatched(claim, handle.owner).dispatch_state
            is DispatchState.DISPATCHED
        )
        result = WorkResult(
            kind=ExecutionOutcomeKind.SUCCEEDED,
            payload={"answer": "ok"},
            dispatch_state=DispatchState.EFFECT_CONFIRMED,
        )
        settled = repository.settle_work(claim, result, handle.owner)
        assert settled.settled_result == result
        assert repository.get_work_result(work.work_id) == result
        assert verify_events(repository.events(run.id)).valid
        assert repository.acquire_owner(run.id, "worker-b") is None

        _settle_delegated_result(repository, run, child_work, handle.owner)

        expired = repository.claim_work(unknown_work.work_id, "worker-a")
        assert expired is not None
        expired = _expire_postgres_work(pg_engine, repository, expired)
        requeued = repository.recover_expired_work(expired, handle.owner)
        assert requeued.state.value == "pending"

        dispatched = repository.claim_work(unknown_work.work_id, "worker-a")
        assert dispatched is not None
        repository.mark_dispatched(dispatched, handle.owner)
        dispatched = _expire_postgres_work(pg_engine, repository, dispatched)
        unknown = repository.recover_expired_work(dispatched, handle.owner)
        assert unknown.settled_result is not None
        assert unknown.settled_result.kind is ExecutionOutcomeKind.UNKNOWN
        assert repository.get_work_result(unknown_work.work_id) == unknown.settled_result
        first_epoch = handle.epoch
    finally:
        handle.close(OwnerReleaseReason.COMPLETED)
    recovered = repository.acquire_owner(run.id, "worker-c")
    assert recovered is not None
    try:
        assert recovered.epoch == first_epoch + 1
    finally:
        repository.release_owner(recovered, OwnerReleaseReason.RECOVERED)


def test_postgres_lifecycle_control_proof_is_exact_and_idempotent(pg_engine: Engine) -> None:
    """The PostgreSQL inbox verifies proof scope and deduplicates one accepted command."""
    secret = b"pg-lifecycle-test-secret"
    repository = PgLifecycleRepository(
        pg_engine,
        authority_verifier=HmacAuthorityVerifier(
            secret,
            issuer_process_id="pg-api-test",
            issuer_key_id="pg-test-key",
        ),
    )
    project_id = new_id("project")
    session = Session(id=new_id("session"), project_id=project_id, client="postgres-test")
    run = Run(
        id=new_id("run"),
        project_id=project_id,
        session_id=session.id,
        prompt="postgres controls",
    )
    input_id = new_id("input")
    key = f"control-{run.id}"
    payload = {"prompt": "continue"}
    proof = AuthorityProof(
        proof_id=new_id("proof"),
        actor=Actor(kind=ActorKind.HUMAN, id="human-1", authenticated=True),
        authenticated_principal_id="principal-1",
        authenticated_role="operator",
        permissions=("run:control",),
        session_id=session.id,
        run_id=run.id,
        issuer_process_id="pg-api-test",
        issuer_key_id="pg-test-key",
        request_id=f"request-{input_id}",
        signature="pending-signature",
        input_id=input_id,
        payload_sha256=hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        binding_sha256="0" * 64,
        requested_lane=InboxLane.NEXT_TURN,
        kind=ControlInputKind.FOLLOWUP,
        idempotency_key=key,
    )
    command = ControlInput(
        input_id=input_id,
        run_id=run.id,
        session_id=session.id,
        requested_lane=InboxLane.NEXT_TURN,
        kind=ControlInputKind.FOLLOWUP,
        payload=payload,
        authority=proof,
        idempotency_key=key,
    )
    command = command.model_copy(
        update={
            "authority": sign_authority(
                proof.model_copy(update={"binding_sha256": command.binding_sha256()}),
                secret,
            )
        }
    )
    first = repository.enqueue_control(command)
    duplicate = repository.enqueue_control(command)
    assert first.input_id == duplicate.input_id
    assert duplicate.duplicate
    assert repository.controls(run.id)[0].authority.signature != secret.decode(errors="ignore")
    handle = repository.commit_publication(repository.prepare_publication(session, run))
    try:
        applied = repository.mark_control_applied(repository.controls(run.id)[0], handle.owner)
        assert applied.state is ControlInputState.APPLIED
    finally:
        handle.close(OwnerReleaseReason.COMPLETED)


def test_event_append_preserves_chain_after_reload(pg_engine: Engine) -> None:
    """A hash chain verifies after reload, and a fresh appender continues it."""
    run_id = new_id("run")
    log = PgEventStore(pg_engine).open(run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {"source": "first"})
    log.append(EventType.RUN_COMPLETED, Actor.system(), {"source": "second"})

    reloaded = PgEventStore(pg_engine).read(run_id)
    assert [event.type for event in reloaded] == [EventType.RUN_STARTED, EventType.RUN_COMPLETED]
    assert verify_events(reloaded).valid

    PgEventStore(pg_engine).open(run_id).append(EventType.RUN_FAILED, Actor.system())
    final = PgEventStore(pg_engine).read(run_id)
    assert [event.seq for event in final] == [0, 1, 2]
    assert final[1].prev_hash == final[0].hash
    assert final[2].prev_hash == final[1].hash
    assert verify_events(final).valid


def test_settings_from_env_assembles_dsn_and_pool() -> None:
    """``from_env`` builds a psycopg DSN and reads pool sizing from the parts."""
    env = {
        "THYMIRA_POSTGRES_HOST": "db",
        "THYMIRA_POSTGRES_PORT": "6000",
        "THYMIRA_POSTGRES_USER": "u",
        "THYMIRA_POSTGRES_PASSWORD": "p",
        "THYMIRA_POSTGRES_DB": "mydb",
        "THYMIRA_POSTGRES_POOL_SIZE": "7",
        "THYMIRA_POSTGRES_MAX_OVERFLOW": "3",
    }
    settings = PostgresSettings.from_env(env)
    assert settings.dsn == "postgresql+psycopg://u:p@db:6000/mydb"
    assert settings.pool_size == 7
    assert settings.max_overflow == 3


def test_settings_from_env_prefers_explicit_dsn() -> None:
    """A whole DSN in the environment is used verbatim."""
    env = {"THYMIRA_POSTGRES_DSN": "postgresql+psycopg://user@host:5432/db"}
    assert PostgresSettings.from_env(env).dsn == "postgresql+psycopg://user@host:5432/db"
