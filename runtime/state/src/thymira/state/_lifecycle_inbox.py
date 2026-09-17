"""Durable authenticated control-input inbox for the local lifecycle repository."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING

from thymira.schemas import (
    ControlInput,
    ControlInputState,
    EnqueueReceipt,
    InboxLane,
    RunOwnerHandle,
    new_id,
    utc_now,
)
from thymira.state._lifecycle_files import LifecycleFiles, sha256_json
from thymira.state._lifecycle_lock import FileLock
from thymira.state.lifecycle_errors import (
    IdempotencyConflictError,
    LifecycleBackendUnavailableError,
    LifecycleError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class ControlInbox:
    """Persist authenticated controls and advisory notifications before broker delivery."""

    _CLAIM_TTL = timedelta(minutes=5)

    def __init__(
        self,
        files: LifecycleFiles,
        *,
        authority_verifier: Callable[[ControlInput], bool] | None = None,
        assert_owner: Callable[[RunOwnerHandle], None] | None = None,
        publish_work: Callable[[ControlInput], None] | None = None,
    ) -> None:
        self._files = files
        self._authority_verifier = authority_verifier
        self._assert_owner = assert_owner
        self._publish_work = publish_work
        self._guard = threading.RLock()

    def enqueue(self, command: ControlInput) -> EnqueueReceipt:
        """Atomically persist one command and return its durable idempotent receipt."""
        self._validate_authority(command)
        with self._guard, FileLock(self._files.locks / "controls.lock"):
            for existing in self._files.iter_controls():
                if existing.idempotency_key == command.idempotency_key:
                    request_fields = {
                        "state",
                        "ordinal",
                        "enqueued_at",
                        "effective_lane",
                        "claim_owner",
                        "claim_token",
                        "claim_expires_at",
                        "claim_attempt",
                    }
                    if existing.run_id != command.run_id or existing.model_dump(
                        exclude=request_fields
                    ) != command.model_dump(exclude=request_fields):
                        raise IdempotencyConflictError(
                            f"idempotency key {command.idempotency_key!r} names another command"
                        )
                    if existing.state is ControlInputState.PENDING:
                        if self._publish_work is None:
                            raise LifecycleBackendUnavailableError(
                                "control input work publication is not configured; fail closed"
                            )
                        # A crash may have persisted the inbox record immediately before its
                        # durable WorkItem/outbox pair.  Re-run the idempotent repair before
                        # acknowledging a retry, so accepted controls never become orphaned.
                        self._publish_work(existing)
                    return EnqueueReceipt(
                        input_id=existing.input_id,
                        run_id=existing.run_id,
                        accepted=True,
                        duplicate=True,
                    )
                if existing.authority.proof_id == command.authority.proof_id:
                    raise IdempotencyConflictError(
                        f"authority proof {command.authority.proof_id!r} was already consumed"
                    )
            ordinal = max((item.ordinal for item in self._files.iter_controls()), default=-1) + 1
            stored = command.model_copy(
                update={"state": ControlInputState.PENDING, "ordinal": ordinal}
            )
            self._files.write_json(self._files.control_path(stored.input_id), stored.to_json_dict())
            if self._publish_work is None:
                raise LifecycleBackendUnavailableError(
                    "control input work publication is not configured; fail closed"
                )
            self._publish_work(stored)
            return EnqueueReceipt(
                input_id=stored.input_id,
                run_id=stored.run_id,
                accepted=True,
                duplicate=False,
            )

    def controls(self, run_id: str) -> tuple[ControlInput, ...]:
        """Read controls for one Run in repository-assigned order."""
        return tuple(
            sorted(
                (c for c in self._files.iter_controls() if c.run_id == run_id),
                key=lambda c: c.ordinal,
            )
        )

    def claim(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        effective_lane: InboxLane,
    ) -> ControlInput | None:
        """Atomically claim one pending control for a live owner.

        The claim and lane transfer share the inbox lock.  A repeated read by the same owner
        returns its still-live claim, while another owner must wait for its bounded claim to expire.
        Expiry reopens the input for a fresh owner claim; it never changes a terminal input.
        """
        if self._assert_owner is None:
            raise LifecycleBackendUnavailableError("control claim requires an owner validator")
        self._assert_owner(owner)
        if command.run_id != owner.run_id:
            raise PermissionError("control input targets another Run")
        target = self._files.control_path(command.input_id)
        with self._guard, FileLock(self._files.locks / "controls.lock"):
            value = self._files.read_json(target)
            if value is None:
                raise FileNotFoundError(f"control input {command.input_id} does not exist")
            stored = ControlInput.model_validate(value)
            if not _same_control_request(stored, command):
                raise LifecycleError("control input changed while its owner was claiming it")
            if stored.state in {
                ControlInputState.APPLIED,
                ControlInputState.REJECTED,
                ControlInputState.CANCELLED,
            }:
                return None
            now = utc_now()
            if stored.state is ControlInputState.CLAIMED:
                if (
                    stored.claim_owner == owner.owner_id
                    and stored.claim_token is not None
                    and stored.claim_expires_at is not None
                    and stored.claim_expires_at > now
                ):
                    return stored
                if stored.claim_expires_at is None or stored.claim_expires_at > now:
                    return None
                stored = stored.model_copy(
                    update={
                        "state": ControlInputState.PENDING,
                        "claim_owner": None,
                        "claim_token": None,
                        "claim_expires_at": None,
                    }
                )
            if stored.state is not ControlInputState.PENDING:
                return None
            claimed = stored.model_copy(
                update={
                    "state": ControlInputState.CLAIMED,
                    "effective_lane": effective_lane,
                    "claim_owner": owner.owner_id,
                    "claim_token": new_id("claim"),
                    "claim_expires_at": now + self._CLAIM_TTL,
                    "claim_attempt": stored.claim_attempt + 1,
                }
            )
            self._files.write_json(target, claimed.to_json_dict())
            return claimed

    def mark_applied(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        state: ControlInputState = ControlInputState.APPLIED,
    ) -> ControlInput:
        """Commit one owner's control result after validating the durable command identity.

        Control inputs are an inbox, rather than a second Run writer.  The owner loop invokes the
        domain consumer first and then calls this method while it still holds the Run lock.  A
        released or forged owner therefore cannot acknowledge a command.  Repeating an already
        applied command is idempotent; changing its identity or attempting to move a command
        backwards is rejected.
        """
        if self._assert_owner is None:
            raise LifecycleBackendUnavailableError(
                "control acknowledgement requires an owner validator"
            )
        self._assert_owner(owner)
        if command.run_id != owner.run_id:
            raise PermissionError("control input targets another Run")
        target = self._files.control_path(command.input_id)
        with self._guard, FileLock(self._files.locks / "controls.lock"):
            value = self._files.read_json(target)
            if value is None:
                raise FileNotFoundError(f"control input {command.input_id} does not exist")
            stored = ControlInput.model_validate(value)
            if stored.model_copy(update={"state": command.state}) != command:
                raise LifecycleBackendUnavailableError(
                    "control input changed while its owner was applying it"
                )
            if stored.state in {
                ControlInputState.APPLIED,
                ControlInputState.REJECTED,
                ControlInputState.CANCELLED,
            }:
                if stored.state is state:
                    return stored
                raise LifecycleBackendUnavailableError(
                    "control input has already reached a terminal state"
                )
            if stored.state is not ControlInputState.CLAIMED:
                raise LifecycleBackendUnavailableError(
                    "control input is not claimed in the owner inbox"
                )
            updated = stored.model_copy(update={"state": state})
            self._files.write_json(target, updated.to_json_dict())
            return updated

    def _validate_authority(self, command: ControlInput) -> None:
        """Check trusted actor and exact proof-to-command binding before persistence."""
        proof = command.authority
        if not proof.actor.authenticated:
            raise PermissionError("control input requires an authenticated authority")
        if self._authority_verifier is None:
            raise LifecycleBackendUnavailableError(
                "control input authority verification is not configured; fail closed"
            )
        if proof.run_id != command.run_id:
            raise PermissionError("authority proof targets another Run")
        if proof.session_id != command.session_id:
            raise PermissionError("authority proof targets another Session")
        if proof.input_id != command.input_id:
            raise PermissionError("authority proof targets another control input")
        if (
            proof.requested_lane is not command.requested_lane
            or proof.effective_lane is not command.effective_lane
            or proof.kind is not command.kind
            or proof.target_turn_id != command.target_turn_id
            or proof.target_work_ids != command.target_work_ids
            or proof.idempotency_key != command.idempotency_key
        ):
            raise PermissionError("authority proof scope does not match the control input")
        payload_digest = sha256_json(command.payload)
        if proof.payload_sha256 != payload_digest:
            raise PermissionError("authority proof payload binding does not match")
        binding = command.binding_sha256()
        if proof.binding_sha256 != binding:
            raise PermissionError("authority proof command binding does not match")
        if proof.expires_at <= utc_now():
            raise PermissionError("authority proof has expired")
        if not self._authority_verifier(command):
            raise PermissionError("control input authority signature is not trusted")


def _same_control_request(left: ControlInput, right: ControlInput) -> bool:
    """Compare immutable request facts while ignoring owner claim state and lane transfer."""
    claim_fields = {
        "state",
        "effective_lane",
        "claim_owner",
        "claim_token",
        "claim_expires_at",
        "claim_attempt",
    }
    return left.model_dump(exclude=claim_fields) == right.model_dump(exclude=claim_fields)


__all__ = ["ControlInbox"]
