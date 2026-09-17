"""Concrete PostgreSQL lifecycle adapter composed from bounded stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from thymira.schemas import (
    Actor,
    AuditOutcome,
    ControlInput,
    ControlInputState,
    EnqueueReceipt,
    Event,
    EventSurface,
    EventType,
    ExecutionOutcome,
    JsonObject,
    OwnerReleaseReason,
    PreparedPublication,
    PublicationReceipt,
    Run,
    RunOwnerHandle,
    Session,
    TerminalBinding,
    WorkClaim,
    WorkItem,
    WorkResult,
)
from thymira.state.lifecycle import RunHandle
from thymira.state.postgres.config import PostgresSettings, create_engine
from thymira.state.postgres.lifecycle_canonical import PgCanonicalStore
from thymira.state.postgres.lifecycle_inbox import PgControlInbox
from thymira.state.postgres.lifecycle_owner import PgOwnerRegistry
from thymira.state.postgres.lifecycle_publication import PgPublicationStore
from thymira.state.postgres.lifecycle_work import PgWorkBoard

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Engine

    from thymira.state.lifecycle import LocalLifecycleRepository


class PgRunHandle(RunHandle):
    """Live PostgreSQL Run writer bound to one advisory owner connection."""

    def __init__(
        self,
        repository: object,
        owner: RunOwnerHandle,
        receipt: PublicationReceipt | None = None,
    ) -> None:
        super().__init__(cast("LocalLifecycleRepository", repository), owner, receipt)


class _PublicationMethods:
    """Expose publication operations from the bounded publication store."""

    _publication: PgPublicationStore

    def prepare_publication(
        self,
        session_draft: Session | None,
        run: Run,
        initial_work: WorkItem | Sequence[WorkItem] = (),
        *,
        creation_payload: JsonObject | None = None,
        creation_actor: Actor | None = None,
    ) -> PreparedPublication:
        """Stage a private Run, Session and initial work publication."""
        return self._publication.prepare(
            session_draft,
            run,
            initial_work,
            creation_payload=creation_payload,
            creation_actor=creation_actor,
        )

    def commit_publication(self, prepared: PreparedPublication) -> PgRunHandle:
        """Atomically materialise a staged publication and return its owner handle."""
        owner, receipt = self._publication.commit(prepared)
        return PgRunHandle(self, owner, receipt)

    def recover_publications(self) -> tuple[PublicationReceipt, ...]:
        """Complete valid prepared publications after a process restart."""
        return self._publication.recover()

    def is_visible(self, run_id: str) -> bool:
        """Return whether the Run and inverse Session link are committed."""
        return self._publication.is_visible(run_id)

    def get_run(self, run_id: str) -> Run:
        """Read a committed Run projection."""
        return self._publication.get_run(run_id)


class _CanonicalMethods:
    """Expose owner-fenced canonical events and terminal facts."""

    _canonical: PgCanonicalStore

    def events(self, run_id: str) -> list[Event]:
        """Read the verified canonical event chain."""
        return self._canonical.events(run_id)

    def version(self, run_id: str) -> int:
        """Read the event-derived version."""
        return self._canonical.version(run_id)

    def append_owned(
        self,
        owner: RunOwnerHandle,
        type: EventType,  # noqa: A002  # event contract uses `type`
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        subject_id: str | None = None,
        producer: str = "thymira.state.lifecycle",
        producer_version: str = "0.1",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
        allow_transition: bool = False,
    ) -> Event:
        """Append one canonical event through the owner-fenced store."""
        return self._canonical.append_owned(
            owner,
            type,
            actor,
            payload,
            expected_version=expected_version,
            subject_id=subject_id,
            producer=producer,
            producer_version=producer_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            authorization_context_sha256=authorization_context_sha256,
            surface=surface,
            allow_transition=allow_transition,
        )

    def save_execution_outcome(
        self,
        outcome: ExecutionOutcome,
        owner: RunOwnerHandle,
    ) -> ExecutionOutcome:
        """Persist an immutable execution outcome behind the owner fence."""
        return self._canonical.save_execution_outcome(outcome, owner)

    def save_audit_outcome(self, outcome: AuditOutcome, owner: RunOwnerHandle) -> AuditOutcome:
        """Persist an immutable audit outcome behind the owner fence."""
        return self._canonical.save_audit_outcome(outcome, owner)

    def save_terminal_binding(
        self,
        binding: TerminalBinding,
        owner: RunOwnerHandle,
    ) -> TerminalBinding:
        """Persist the one immutable terminal binding behind the owner fence."""
        return self._canonical.save_terminal_binding(binding, owner)


class _OwnerMethods:
    """Expose advisory ownership and live owner handles."""

    _owners: PgOwnerRegistry

    def acquire_owner(self, run_id: str, claimant_id: str) -> RunOwnerHandle | None:
        """Acquire the dedicated PostgreSQL advisory owner connection."""
        return self._owners.acquire(run_id, claimant_id, allow_unmaterialized=False)

    def recover_owner(self, run_id: str, claimant_id: str) -> RunOwnerHandle | None:
        """Reacquire a materialised Run after process recovery."""
        return self.acquire_owner(run_id, claimant_id)

    def release_owner(self, owner: RunOwnerHandle, reason: OwnerReleaseReason) -> None:
        """Durably release the owner and close its held advisory connection."""
        self._owners.release(owner, reason)

    def assert_owner(self, owner: RunOwnerHandle) -> None:
        """Validate a live owner before a composed writer is used."""
        self._owners.assert_live(owner)

    def run_handle(self, owner: RunOwnerHandle) -> PgRunHandle:
        """Bind a live owner to the canonical writer facade."""
        self.assert_owner(owner)
        return PgRunHandle(self, owner)


class _InboxMethods:
    """Expose authenticated controls and the durable outbox."""

    _inbox: PgControlInbox

    def enqueue_control(self, command: ControlInput) -> EnqueueReceipt:
        """Persist an exact authenticated control input."""
        return self._inbox.enqueue(command)

    def controls(self, run_id: str) -> tuple[ControlInput, ...]:
        """Read controls in repository-assigned order."""
        return self._inbox.controls(run_id)

    def mark_control_applied(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        state: ControlInputState = ControlInputState.APPLIED,
    ) -> ControlInput:
        """Record an owner-fenced control acknowledgement."""
        return self._inbox.mark_applied(command, owner, state=state)

    def list_outbox(self) -> tuple[dict[str, object], ...]:
        """Read durable lifecycle notifications."""
        return self._inbox.list_outbox()

    def mark_outbox_published(self, notification_id: str) -> None:
        """Record broker acknowledgement for one notification."""
        self._inbox.mark_outbox_published(notification_id)


class _WorkMethods:
    """Expose bounded work claims, dispatch and settlement."""

    _work: PgWorkBoard

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Atomically claim one pending work item."""
        return self._work.claim_work(work_id, claimant_id, limit)

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read one current work item."""
        return self._work.get_work(work_id)

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read the typed result embedded in a settled work item."""
        return self._work.get_work_result(work_id)

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Mark a current unexpired claim dispatched under the owner fence."""
        return self._work.mark_dispatched(claim, owner)

    def settle_work(
        self,
        claim: WorkClaim,
        result: WorkResult,
        owner: RunOwnerHandle,
    ) -> WorkItem:
        """Atomically settle a claim with its typed result."""
        return self._work.settle_work(claim, result, owner)

    def recover_expired_work(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Recover expiry without retrying an effect whose result is unknown."""
        return self._work.recover_expired_work(claim, owner)


class PgLifecycleRepository(
    _PublicationMethods,
    _CanonicalMethods,
    _OwnerMethods,
    _InboxMethods,
    _WorkMethods,
):
    """Persist the bounded lifecycle boundary in PostgreSQL.

    The adapter requires the packaged lifecycle migration. It never falls back to local files.
    Owner writes use a held connection-level advisory lock plus a durable monotonic epoch checked
    inside every mutation transaction.
    """

    def __init__(
        self,
        engine: Engine | str,
        *,
        authority_verifier: Callable[[ControlInput], bool] | None = None,
    ) -> None:
        self._engine = (
            create_engine(PostgresSettings(dsn=engine)) if isinstance(engine, str) else engine
        )
        self._owners = PgOwnerRegistry(self._engine)
        self._publication = PgPublicationStore(self._engine, self._owners)
        self._inbox = PgControlInbox(self._engine, self._owners, authority_verifier)
        self._work = PgWorkBoard(self._engine, self._owners)
        self._canonical = PgCanonicalStore(self._engine, self._owners)

    @property
    def engine(self) -> Engine:
        """Return the SQLAlchemy engine used by all lifecycle stores."""
        return self._engine


__all__ = ["PgLifecycleRepository", "PgRunHandle"]
