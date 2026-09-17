"""Durable local ownership, publication and work-claim boundaries.

The lifecycle repository is the first write boundary before a Run becomes visible.  It stages a
complete publication privately, links the Session only after Run materialisation, and leaves a
durable work item and outbox record before any broker notification.  Canonical Run writes require
the live process-owned handle returned by :meth:`LocalLifecycleRepository.acquire_owner`.
"""

from __future__ import annotations

import time
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from thymira.schemas import (
    Actor,
    ControlInput,
    ControlInputState,
    EnqueueReceipt,
    Event,
    EventSurface,
    EventType,
    InboxLane,
    JsonObject,
    OutboxNotification,
    OwnerReleaseReason,
    PreparedPublication,
    PublicationReceipt,
    Run,
    RunOwnerHandle,
    Session,
    TurnEnded,
    WorkClaim,
    WorkItem,
    WorkResult,
    WorkState,
)
from thymira.state._lifecycle_files import LifecycleFiles
from thymira.state._lifecycle_inbox import ControlInbox
from thymira.state._lifecycle_lock import FileLock
from thymira.state._lifecycle_owner import OwnerRegistry
from thymira.state._lifecycle_work import WorkBoard
from thymira.state.lifecycle_errors import (
    IdempotencyConflictError,
    LifecycleBackendUnavailable,
    LifecycleBackendUnavailableError,
    LifecycleError,
    OwnerBusyError,
    PublicationError,
    StaleOwnerError,
)
from thymira.state.local_run_store import LocalRunStore
from thymira.state.repositories import LocalSessionRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _committed_receipt(manifest: dict[str, Any]) -> PublicationReceipt:
    """Parse the receipt of an already committed publication."""
    receipt_data = manifest.get("receipt")
    if not isinstance(receipt_data, dict):
        raise PublicationError("committed publication has no receipt")
    return PublicationReceipt.model_validate(receipt_data)


def _validate_turn_ended(
    files: LifecycleFiles,
    run_id: str,
    payload: dict[str, Any] | None,
) -> None:
    """Require a closed, settled turn payload before recording its canonical event."""
    ended = TurnEnded.model_validate(payload)
    if ended.run_id != run_id:
        raise ValueError("turn.ended payload targets another Run")
    for work_id in ended.work_ids:
        raw = files.read_json(files.work_path(work_id))
        if raw is None:
            raise ValueError(f"turn.ended references unknown work item {work_id}")
        item = WorkItem.model_validate(raw)
        if item.run_id != run_id or item.state not in {WorkState.SETTLED, WorkState.CANCELLED}:
            raise ValueError("turn.ended requires every referenced work item to be settled")


@runtime_checkable
class LifecycleRepository(Protocol):
    """Shared contract for publication, durable controls, work and Run ownership."""

    def get_run(self, run_id: str) -> Run:
        """Read one committed Run snapshot."""
        ...

    def prepare_publication(
        self,
        session_draft: Session | None,
        run: Run,
        initial_work: WorkItem | Sequence[WorkItem] = (),
        *,
        creation_payload: dict[str, object] | None = None,
        creation_actor: Actor | None = None,
        idempotency_key: str | None = None,
        request_binding_sha256: str | None = None,
    ) -> PreparedPublication:
        """Stage a complete publication under a private durable name."""
        ...

    def commit_publication(self, prepared: PreparedPublication) -> RunHandle:
        """Materialise and link a staged publication, returning its live Run handle."""
        ...

    def assert_owner(self, owner: RunOwnerHandle) -> None:
        """Validate that a public owner identity still has its private live lock resource."""
        ...

    def enqueue_control(self, command: ControlInput) -> EnqueueReceipt:
        """Persist an authenticated control input before notifying a broker."""
        ...

    def list_outbox(self) -> tuple[dict[str, object], ...]:
        """Read durable broker notifications without consulting the broker."""
        ...

    def mark_outbox_published(self, notification_id: str) -> OutboxNotification:
        """Record successful broker publication for one durable notification."""
        ...

    def controls(self, run_id: str) -> tuple[ControlInput, ...]:
        """Read durable controls in repository order for the single owner loop."""
        ...

    def run_handle(self, owner: RunOwnerHandle) -> RunHandle:
        """Bind a live owner to the canonical writer facade."""
        ...

    def version(self, run_id: str) -> int:
        """Read the authoritative event version for owner-local append CAS."""
        ...

    def mark_control_applied(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        state: ControlInputState = ControlInputState.APPLIED,
    ) -> ControlInput:
        """Mark a control result after its owner-only domain application."""
        ...

    def claim_control(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        effective_lane: InboxLane,
    ) -> ControlInput | None:
        """Atomically claim one pending control for a live owner and effective lane."""
        ...

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Atomically claim one pending work item."""
        ...

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read one durable work item for a claimed notification."""
        ...

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read the canonical typed result persisted by owner settlement."""
        ...

    def acquire_owner(self, run_id: str, claimant_id: str) -> RunOwnerHandle | None:
        """Acquire one process-owned, epoch-fenced Run writer."""
        ...

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Record durable dispatch under the live Run owner."""
        ...

    def settle_work(
        self,
        claim: WorkClaim,
        result: WorkResult,
        owner: RunOwnerHandle,
    ) -> WorkItem:
        """Record one durable work outcome under the live Run owner."""
        ...

    def recover_expired_work(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Recover one expired claim without retrying an unknown external effect."""
        ...


class RunHandle:
    """Live canonical Run writer returned after publication or owner acquisition.

    The public owner identity is serialisable for diagnostics.  The actual lock resource is held
    by the private ``RunOwnerHandle`` and is checked on every mutation, so matching JSON fields
    cannot forge a writer.
    """

    def __init__(
        self,
        repository: LocalLifecycleRepository,
        owner: RunOwnerHandle | None,
        receipt: PublicationReceipt | None = None,
    ) -> None:
        if owner is None and receipt is None:
            raise LifecycleError("receipt-only Run handle requires a publication receipt")
        self._repository = repository
        self._live_owner = owner
        if owner is None:
            receipt = cast("PublicationReceipt", receipt)
            owner = RunOwnerHandle(
                run_id=receipt.run_id,
                owner_id=f"receipt-only:{receipt.publication_id}",
                epoch=1,
            )
        self.owner = owner
        self.receipt = receipt

    @property
    def run_id(self) -> str:
        """Return the Run id protected by this handle."""
        return self.owner.run_id

    @property
    def epoch(self) -> int:
        """Return the fencing epoch protected by this handle."""
        return self.owner.epoch

    @property
    def is_live_owner(self) -> bool:
        """Return whether this handle retains the private process-owned lock resource."""
        return self._live_owner is not None

    def _require_live_owner(self) -> RunOwnerHandle:
        """Return the private owner resource, rejecting receipt-only handles."""
        owner = self._live_owner
        if owner is None:
            raise StaleOwnerError("receipt-only Run handle does not hold a live owner lock")
        self._repository.assert_owner(owner)
        return owner

    def append(
        self,
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
    ) -> Event:
        """Append one canonical event after checking the live lock and epoch."""
        owner = self._require_live_owner()
        return self._repository.append_owned(
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
        )

    def append_transition(
        self,
        type: EventType,  # noqa: A002  # event contract uses `type`
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        subject_id: str | None = None,
        producer: str = "thymira.core",
        producer_version: str = "0.1",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append a Run transition through the controller-only owner gateway.

        Generic owner event writers reject ``RUN_TRANSITIONED`` so a consumer cannot bypass the
        controller's state machine.  ``RunController`` receives this dedicated callback when a
        lifecycle consumer is operating under a live owner handle.
        """
        if type is not EventType.RUN_TRANSITIONED:
            raise ValueError("append_transition accepts only run.transitioned")
        owner = self._require_live_owner()
        return self._repository.append_owned(
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
            allow_transition=True,
        )

    def events(self) -> list[Event]:
        """Read the verified event history for the owned Run."""
        return self._repository.events(self.run_id)

    def close(self, reason: OwnerReleaseReason = OwnerReleaseReason.SHUTDOWN) -> None:
        """Release the process-owned lock with a durable reason."""
        owner = self._live_owner
        if owner is not None:
            self._repository.release_owner(owner, reason)
            self._live_owner = None

    def assert_live(self) -> None:
        """Revalidate the private OS lock and fencing epoch for a delegated writer."""
        self._require_live_owner()

    def owner_handle(self) -> RunOwnerHandle:
        """Return the live owner identity for repository operations requiring its resource."""
        return self._require_live_owner()

    def __enter__(self) -> RunHandle:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(
            OwnerReleaseReason.FAILED if exc_type is not None else OwnerReleaseReason.COMPLETED
        )


class LocalLifecycleRepository:
    """Filesystem lifecycle repository with per-Run ownership and staged publication.

    ``root`` is private runtime state.  The repository uses one lock for each Run, one short
    publication lock for each Session, and one lock per work item; independent Runs therefore do
    not block one another.  Epoch metadata remains after process death and is incremented whenever
    a fresh owner acquires the OS lock.
    """

    def __init__(
        self,
        root: Path,
        *,
        run_store: LocalRunStore | None = None,
        session_repository: LocalSessionRepository | None = None,
        authority_verifier: Callable[[ControlInput], bool] | None = None,
    ) -> None:
        self.root = Path(root)
        self._files = LifecycleFiles(self.root)
        self._runs = run_store or LocalRunStore(self.root / "runs")
        self._sessions = session_repository or LocalSessionRepository(self.root)
        self._owners_registry = OwnerRegistry(
            self._files,
            lambda run_id: (
                self._runs._run_dir(run_id).is_dir()  # noqa: SLF001
                and self.is_visible(run_id)
            ),
        )
        self._inbox = ControlInbox(
            self._files,
            authority_verifier=authority_verifier,
            assert_owner=self._owners_registry.assert_live,
            publish_work=self._publish_control_work,
        )
        self._board = WorkBoard(self._files, self._owners_registry.assert_live)

    def prepare_publication(
        self,
        session_draft: Session | None,
        run: Run,
        initial_work: WorkItem | Sequence[WorkItem] = (),
        *,
        creation_payload: dict[str, object] | None = None,
        creation_actor: Actor | None = None,
        idempotency_key: str | None = None,
        request_binding_sha256: str | None = None,
    ) -> PreparedPublication:
        """Stage a complete Run and its initial work in a private manifest.

        The Session object is only a draft at this point.  It is not saved, and no reader can
        observe a new Session or Run through this repository until :meth:`commit_publication`
        links both sides and records the committed marker.
        """
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if idempotency_key is None and request_binding_sha256 is not None:
            raise ValueError("request_binding_sha256 requires idempotency_key")
        if idempotency_key is not None and request_binding_sha256 is None:
            raise ValueError("request_binding_sha256 is required with idempotency_key")
        if idempotency_key is not None:
            key = idempotency_key
            key_lock = self._acquire_idempotency_lock(key)
            try:
                existing = self._find_idempotent_publication(
                    key,
                    request_binding_sha256,
                )
                if existing is not None:
                    return existing
            finally:
                key_lock.__exit__(None, None, None)
        if session_draft is not None and (
            session_draft.id != run.session_id or session_draft.project_id != run.project_id
        ):
            raise PublicationError("Session draft does not match the Run relationship")
        work = (initial_work,) if isinstance(initial_work, WorkItem) else tuple(initial_work)
        if any(item.run_id != run.id for item in work):
            raise PublicationError("initial work must belong to the staged Run")
        if len({item.work_id for item in work}) != len(work):
            raise PublicationError("initial work ids must be unique")
        prepared = PreparedPublication(
            run=run,
            session=session_draft,
            initial_work=work,
            creation_payload=cast("JsonObject", creation_payload or {}),
            creation_actor=creation_actor or Actor.system(),
            idempotency_key=idempotency_key,
            request_binding_sha256=request_binding_sha256,
        )
        if idempotency_key is None:
            return self._stage_publication(prepared)
        key = idempotency_key
        key_lock = self._acquire_idempotency_lock(key)
        try:
            existing = self._find_idempotent_publication(
                key,
                request_binding_sha256,
            )
            if existing is not None:
                return existing
            return self._stage_publication(prepared)
        finally:
            key_lock.__exit__(None, None, None)

    def _stage_publication(self, prepared: PreparedPublication) -> PreparedPublication:
        """Write one private publication manifest after the idempotency key lock is held."""
        target = self._files.publication_path(prepared.publication_id)
        if target.exists():
            raise FileExistsError(f"publication {prepared.publication_id} already exists")
        self._files.write_json(
            target,
            {
                "status": "prepared",
                "session_linked": False,
                "publication": prepared.to_json_dict(),
            },
        )
        return prepared

    def _find_idempotent_publication(
        self,
        idempotency_key: str,
        request_binding_sha256: str | None,
    ) -> PreparedPublication | None:
        """Find a durable publication with the exact request binding, failing closed on conflict."""
        found: PreparedPublication | None = None
        for path in sorted(self._files.publications.glob("*.json")):
            manifest = self._files.read_json(path)
            if manifest is None:
                continue
            try:
                prepared = self._files.parse_prepared(manifest)
            except (TypeError, ValueError) as exc:
                raise PublicationError(f"publication manifest {path.name} is invalid") from exc
            if prepared.idempotency_key != idempotency_key:
                continue
            if prepared.request_binding_sha256 != request_binding_sha256:
                raise IdempotencyConflictError(
                    "idempotency key is already bound to a different creation request"
                )
            if found is not None and found.publication_id != prepared.publication_id:
                raise PublicationError("idempotency key has multiple durable publications")
            found = prepared
        return found

    def commit_publication(self, prepared: PreparedPublication) -> RunHandle:
        """Materialise a staged Run, link its Session, and leave durable work/outbox records.

        Every step is idempotent under the Session publication lock.  If a process dies between
        steps, :meth:`recover_publications` reopens the private manifest and completes the same
        sequence; a reader never treats a prepared manifest as visible.
        """
        target = self._files.publication_path(prepared.publication_id)
        manifest = self._files.read_json(target)
        if manifest is None:
            raise PublicationError(f"publication {prepared.publication_id} is not staged")
        persisted = self._files.parse_prepared(manifest)
        if persisted != prepared:
            raise PublicationError("publication manifest changed after staging")
        session_id = prepared.run.session_id
        publication_lock = self._acquire_publication_lock(prepared, session_id=session_id)
        if publication_lock is None:
            current_manifest = self._files.read_json(target)
            if (
                current_manifest is not None
                and current_manifest.get("status") == "committed"
                and current_manifest.get("session_linked") is True
            ):
                receipt = _committed_receipt(current_manifest)
                return RunHandle(self, None, receipt)
            raise OwnerBusyError(f"Session {session_id} publication lock is busy")
        try:
            owner = self._owners_registry.acquire(
                prepared.run.id,
                prepared.publication_id,
                allow_unmaterialized=True,
            )
            if owner is None:
                current_manifest = self._files.read_json(target)
                if (
                    current_manifest is not None
                    and current_manifest.get("status") == "committed"
                    and current_manifest.get("session_linked") is True
                ):
                    receipt = _committed_receipt(current_manifest)
                    return RunHandle(self, None, receipt)
                raise OwnerBusyError(f"Run {prepared.run.id} already has a live owner")
            try:
                current_manifest = self._files.read_json(target)
                if (
                    current_manifest is not None
                    and current_manifest.get("status") == "committed"
                    and current_manifest.get("session_linked") is True
                ):
                    receipt = _committed_receipt(current_manifest)
                    return RunHandle(self, owner, receipt)
                self._materialise_run(prepared, owner)
                self._link_session(prepared)
                for item in prepared.initial_work:
                    self._publish_work(item)
                receipt = PublicationReceipt(
                    publication_id=prepared.publication_id,
                    run_id=prepared.run.id,
                    session_id=session_id,
                    work_ids=tuple(item.work_id for item in prepared.initial_work),
                )
                self._files.write_json(
                    target,
                    {
                        "status": "committed",
                        "session_linked": True,
                        "publication": prepared.to_json_dict(),
                        "receipt": receipt.to_json_dict(),
                    },
                )
                return RunHandle(self, owner, receipt)
            except BaseException:  # release the lock on process interruption
                self._owners_registry.release(owner, OwnerReleaseReason.FAILED)
                raise
        finally:
            publication_lock.__exit__(None, None, None)

    def _acquire_publication_lock(
        self,
        prepared: PreparedPublication,
        *,
        session_id: str,
    ) -> FileLock | None:
        """Acquire the brief Session lock, waiting only for an idempotent concurrent commit."""
        attempts = 1 if prepared.idempotency_key is None else 500
        path = self._files.locks / f"session-{session_id}.lock"
        for attempt in range(attempts):
            publication_lock = FileLock(path)
            try:
                publication_lock.__enter__()
            except OwnerBusyError:
                if attempt + 1 == attempts:
                    return None
                time.sleep(0.01)
            else:
                return publication_lock
        return None

    def _acquire_idempotency_lock(self, key: str) -> FileLock:
        """Acquire a bounded per-key lock so concurrent exact retries converge durably."""
        attempts = 1000
        path = self._files.locks / f"publication-key-{_key_digest(key)}.lock"
        for attempt in range(attempts):
            key_lock = FileLock(path)
            try:
                key_lock.__enter__()
            except OwnerBusyError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.01)
            else:
                return key_lock
        raise OwnerBusyError(f"publication idempotency lock is busy: {key}")

    def recover_publications(self) -> tuple[PublicationReceipt, ...]:
        """Finish valid private publications after a process crash.

        Recovery acquires the same per-Session lock and epoch-fenced Run owner as normal commit.
        Malformed private manifests are left for an operator rather than being mistaken for a
        valid publication or silently deleted.
        """
        receipts: list[PublicationReceipt] = []
        for path in sorted(self._files.publications.glob("*.json")):
            manifest = self._files.read_json(path)
            if manifest is None or manifest.get("status") == "committed":
                continue
            try:
                prepared = self._files.parse_prepared(manifest)
                handle = self.commit_publication(prepared)
            except (LifecycleError, OSError, TypeError, ValueError):
                continue
            if handle.receipt is not None:
                receipts.append(handle.receipt)
            handle.close(OwnerReleaseReason.RECOVERED)
        return tuple(receipts)

    def is_visible(self, run_id: str) -> bool:
        """Return whether a Run has a committed marker and an inverse Session link."""
        try:
            publication = self._files.publication_for_run(run_id)
        except PublicationError:
            return False
        if publication is None:
            return True
        if publication.get("status") != "committed" or not publication.get("session_linked"):
            return False
        receipt_data = publication.get("receipt")
        if not isinstance(receipt_data, dict):
            return False
        session_id = receipt_data.get("session_id")
        if not isinstance(session_id, str):
            return False
        session = self._sessions.get(session_id)
        return session is not None and run_id in session.run_ids

    def get_run(self, run_id: str) -> Run:
        """Read a committed Run, hiding staged or half-linked publications."""
        if not self.is_visible(run_id):
            raise FileNotFoundError(f"run {run_id} is not yet visible")
        return self._runs.get(run_id)

    def events(self, run_id: str) -> list[Event]:
        """Read a Run's verified event chain."""
        return self._runs.events(run_id)

    def version(self, run_id: str) -> int:
        """Read the event-derived version of a Run."""
        return self._runs.version(run_id)

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
        """Append one event only through a live process-owned handle."""
        self._owners_registry.assert_live(owner)
        if type is EventType.RUN_TRANSITIONED and not allow_transition:
            raise PermissionError("Run transitions must be appended by RunController")
        if type is EventType.TURN_ENDED:
            _validate_turn_ended(self._files, owner.run_id, payload)
        return self._runs.append(
            owner.run_id,
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
        )

    def enqueue_control(self, command: ControlInput) -> EnqueueReceipt:
        """Persist an authenticated control input before broker delivery."""
        return self._inbox.enqueue(command)

    def _publish_control_work(self, command: ControlInput) -> None:
        """Create one durable owner turn to wake the Run for an accepted control input."""
        ordinals: list[int] = []
        for path in self._files.work.glob("*.json"):
            raw = self._files.read_json(path)
            if raw is None:
                continue
            try:
                item = WorkItem.model_validate(raw)
            except ValueError:
                continue
            if item.run_id == command.run_id:
                if item.payload.get("input_id") == command.input_id:
                    return
                ordinals.append(item.ordinal)
        item = WorkItem(
            run_id=command.run_id,
            ordinal=max(ordinals, default=-1) + 1,
            kind="control.apply",
            idempotency_key=f"control:{command.input_id}",
            payload={"input_id": command.input_id},
        )
        self._publish_work(item)

    def mark_control_applied(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        state: ControlInputState = ControlInputState.APPLIED,
    ) -> ControlInput:
        """Persist an owner-bound control acknowledgement in the durable inbox."""
        self._owners_registry.assert_live(owner, run_id=command.run_id)
        return self._inbox.mark_applied(command, owner, state=state)

    def claim_control(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        effective_lane: InboxLane,
    ) -> ControlInput | None:
        """Claim one pending control through the owner-bound durable inbox."""
        self._owners_registry.assert_live(owner, run_id=command.run_id)
        return self._inbox.claim(command, owner, effective_lane=effective_lane)

    def controls(self, run_id: str) -> tuple[ControlInput, ...]:
        """Read pending and settled controls in repository-assigned order."""
        return self._inbox.controls(run_id)

    def list_outbox(self) -> tuple[dict[str, Any], ...]:
        """Return durable, replayable notifications without broker state."""
        return self._files.outbox_records()

    def mark_outbox_published(self, notification_id: str) -> OutboxNotification:
        """Mark one outbox notification after the broker accepts its exact work projection."""
        return self._files.mark_outbox_published(notification_id)

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Atomically claim one pending work item with a bounded claim expiry."""
        return self._board.claim_work(work_id, claimant_id, limit)

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read one durable work item without granting write authority."""
        return self._board.get(work_id)

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read one owner-settled typed work result for replay and board projections."""
        return self._board.result(work_id)

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Record dispatch after checking both work claim and Run owner fencing."""
        self._owners_registry.assert_live(owner, run_id=claim.run_id)
        return self._board.mark_dispatched(claim, owner)

    def settle_work(self, claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> WorkItem:
        """Durably settle a claimed work item under the live Run owner."""
        self._owners_registry.assert_live(owner, run_id=claim.run_id)
        return self._board.settle_work(claim, result, owner)

    def recover_expired_work(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Recover an expired claim without retrying an unknown external effect."""
        self._owners_registry.assert_live(owner, run_id=claim.run_id)
        return self._board.recover_expired(claim, owner)

    def acquire_owner(self, run_id: str, claimant_id: str) -> RunOwnerHandle | None:
        """Acquire a per-Run OS lock and increment the durable fencing epoch."""
        return self._owners_registry.acquire(run_id, claimant_id, allow_unmaterialized=False)

    def run_handle(self, owner: RunOwnerHandle) -> RunHandle:
        """Bind a live owner identity to its non-serialisable writer resource."""
        self._owners_registry.assert_live(owner)
        return RunHandle(self, owner)

    def assert_owner(self, owner: RunOwnerHandle) -> None:
        """Validate a live owner identity before a composed store writer is used."""
        self._owners_registry.assert_live(owner)

    def release_owner(self, owner: RunOwnerHandle, reason: OwnerReleaseReason) -> None:
        """Persist a release reason, then release the process-owned OS lock."""
        self._owners_registry.assert_live(owner)
        self._owners_registry.release(owner, reason)

    def recover_owner(self, run_id: str, claimant_id: str) -> RunOwnerHandle | None:
        """Acquire a new epoch after process death without stealing a live lock."""
        return self._owners_registry.acquire(run_id, claimant_id, allow_unmaterialized=False)

    def _materialise_run(self, prepared: PreparedPublication, owner: RunOwnerHandle) -> None:
        """Create the authoritative Run event chain once under the owner lock."""
        if not self._runs._run_dir(prepared.run.id).is_dir():  # noqa: SLF001
            self._runs.create(
                prepared.run,
                actor=prepared.creation_actor,
                payload={
                    **prepared.creation_payload,
                    "publication_id": prepared.publication_id,
                    "owner_epoch": owner.epoch,
                },
            )

    def _link_session(self, prepared: PreparedPublication) -> Session:
        session = self._sessions.get(prepared.run.session_id)
        if session is None:
            session = prepared.session
            if session is None:
                raise PublicationError("a new Run requires a Session draft")
        if session.project_id != prepared.run.project_id:
            raise PublicationError("Session and Run projects do not match")
        if prepared.run.id not in session.run_ids:
            session = session.model_copy(update={"run_ids": (*session.run_ids, prepared.run.id)})
        return self._sessions.save(session)

    def _publish_work(self, item: WorkItem) -> None:
        target = self._files.work_path(item.work_id)
        if target.exists():
            existing = WorkItem.model_validate(self._files.read_json(target))
            if existing != item:
                raise PublicationError(f"work item {item.work_id} already has another definition")
            for raw in self._files.outbox_records():
                try:
                    notification = OutboxNotification.model_validate(raw)
                except ValueError as exc:
                    raise PublicationError("outbox notification is malformed") from exc
                if notification.work_id == item.work_id:
                    return
            # The WorkItem may have survived a crash immediately before its outbox row.  Repair
            # that private pair before any broker dispatcher is allowed to observe the item.
            notification = OutboxNotification(work_id=item.work_id, run_id=item.run_id)
            self._files.write_json(
                self._files.outbox / f"{notification.notification_id}.json",
                notification.to_json_dict(),
            )
            return
        self._files.write_json(target, item.to_json_dict())
        notification = OutboxNotification(work_id=item.work_id, run_id=item.run_id)
        self._files.write_json(
            self._files.outbox / f"{notification.notification_id}.json",
            notification.to_json_dict(),
        )


def _key_digest(key: str) -> str:
    """Return a filesystem-safe digest for a publication idempotency lock name."""
    return sha256(key.encode("utf-8")).hexdigest()


__all__ = [
    "IdempotencyConflictError",
    "LifecycleBackendUnavailable",
    "LifecycleBackendUnavailableError",
    "LifecycleError",
    "LifecycleRepository",
    "LocalLifecycleRepository",
    "OwnerBusyError",
    "PublicationError",
    "RunHandle",
    "StaleOwnerError",
]
