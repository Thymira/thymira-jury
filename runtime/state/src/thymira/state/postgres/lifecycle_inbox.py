"""Authenticated PostgreSQL lifecycle control inbox and durable broker outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from thymira.events import canonical_json
from thymira.schemas import (
    AuthorityProof,
    ControlInput,
    ControlInputState,
    EnqueueReceipt,
    RunOwnerHandle,
    utc_now,
)
from thymira.state._lifecycle_files import sha256_json
from thymira.state.lifecycle_errors import (
    IdempotencyConflictError,
    LifecycleBackendUnavailableError,
)
from thymira.state.postgres import tables
from thymira.state.postgres._serde import load_document
from thymira.state.postgres.lifecycle_util import control_values, row_document

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

    from thymira.state.postgres.lifecycle_owner import PgOwnerRegistry


class PgControlInbox:
    """Persist exact-bound controls and owner-only acknowledgements."""

    def __init__(
        self,
        engine: Engine,
        owners: PgOwnerRegistry,
        authority_verifier: Callable[[ControlInput], bool] | None,
    ) -> None:
        self._engine = engine
        self._owners = owners
        self._authority_verifier = authority_verifier

    def enqueue(self, command: ControlInput) -> EnqueueReceipt:
        """Persist an authenticated exact-bound control and its broker notification."""
        self._validate_authority(command)
        stored = command.model_copy(update={"state": ControlInputState.PENDING})
        with self._engine.begin() as conn:
            existing = (
                conn.execute(
                    sa.select(tables.lifecycle_controls)
                    .where(tables.lifecycle_controls.c.idempotency_key == command.idempotency_key)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                current = ControlInput.model_validate(row_document(existing))
                current_data = current.model_dump(exclude={"state", "ordinal", "enqueued_at"})
                stored_data = stored.model_dump(exclude={"state", "ordinal", "enqueued_at"})
                if current_data != stored_data:
                    raise IdempotencyConflictError(
                        f"idempotency key {command.idempotency_key!r} names another command"
                    )
                return EnqueueReceipt(
                    input_id=current.input_id,
                    run_id=current.run_id,
                    accepted=True,
                    duplicate=True,
                )
            proof_reused = conn.execute(
                sa.select(tables.lifecycle_controls.c.id)
                .where(tables.lifecycle_controls.c.proof_id == command.authority.proof_id)
                .with_for_update()
            ).scalar_one_or_none()
            if proof_reused is not None:
                raise IdempotencyConflictError(
                    f"authority proof {command.authority.proof_id!r} was already consumed"
                )
            ordinal = conn.execute(
                tables.lifecycle_controls.insert()
                .values(**control_values(stored))
                .returning(tables.lifecycle_controls.c.ordinal)
            ).scalar_one()
            stored = stored.model_copy(update={"ordinal": int(ordinal)})
            conn.execute(
                tables.lifecycle_controls.update()
                .where(tables.lifecycle_controls.c.id == stored.input_id)
                .values(**control_values(stored))
            )
            notification = {
                "input_id": stored.input_id,
                "run_id": stored.run_id,
                "kind": "control",
                "published": False,
            }
            conn.execute(
                tables.lifecycle_outbox.insert().values(
                    id=stored.input_id,
                    run_id=stored.run_id,
                    kind="control",
                    input_id=stored.input_id,
                    published=False,
                    created_at=stored.enqueued_at,
                    document=canonical_json(notification),
                )
            )
        return EnqueueReceipt(
            input_id=stored.input_id,
            run_id=stored.run_id,
            accepted=True,
            duplicate=False,
        )

    def controls(self, run_id: str) -> tuple[ControlInput, ...]:
        """Read all controls for one Run in repository-assigned order."""
        with self._engine.connect() as conn:
            documents = (
                conn.execute(
                    sa.select(tables.lifecycle_controls.c.document)
                    .where(tables.lifecycle_controls.c.run_id == run_id)
                    .order_by(tables.lifecycle_controls.c.ordinal.asc())
                )
                .scalars()
                .all()
            )
        return tuple(ControlInput.model_validate(load_document(value)) for value in documents)

    def mark_applied(
        self,
        command: ControlInput,
        owner: RunOwnerHandle,
        *,
        state: ControlInputState = ControlInputState.APPLIED,
    ) -> ControlInput:
        """Persist an owner-bound control acknowledgement in the durable inbox."""
        guard = self._owners.guard_for(owner.run_id)
        with guard:
            self._owners.assert_live(owner, run_id=command.run_id)
            with self._engine.begin() as conn:
                self._owners.check_epoch(conn, owner)
                row = (
                    conn.execute(
                        sa.select(tables.lifecycle_controls)
                        .where(tables.lifecycle_controls.c.id == command.input_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise FileNotFoundError(f"control input {command.input_id} does not exist")
                stored = ControlInput.model_validate(row_document(row))
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
                if stored.state is not ControlInputState.PENDING:
                    raise LifecycleBackendUnavailableError(
                        "control input is not pending in the owner inbox"
                    )
                updated = stored.model_copy(update={"state": state})
                conn.execute(
                    tables.lifecycle_controls.update()
                    .where(tables.lifecycle_controls.c.id == command.input_id)
                    .values(**control_values(updated))
                )
                return updated

    def list_outbox(self) -> tuple[dict[str, object], ...]:
        """Read durable broker notifications without consulting broker state."""
        with self._engine.connect() as conn:
            documents = (
                conn.execute(
                    sa.select(tables.lifecycle_outbox.c.document).order_by(
                        tables.lifecycle_outbox.c.created_at.asc(),
                        tables.lifecycle_outbox.c.id.asc(),
                    )
                )
                .scalars()
                .all()
            )
        return tuple(load_document(value) for value in documents)

    def mark_outbox_published(self, notification_id: str) -> None:
        """Mark one notification published after broker acknowledgement."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(tables.lifecycle_outbox.c.document)
                .where(tables.lifecycle_outbox.c.id == notification_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"outbox notification {notification_id} does not exist")
            document = load_document(row)
            document["published"] = True
            conn.execute(
                tables.lifecycle_outbox.update()
                .where(tables.lifecycle_outbox.c.id == notification_id)
                .values(published=True, document=canonical_json(document))
            )

    def _validate_authority(self, command: ControlInput) -> None:
        """Validate actor, exact proof scope, digest and signature before persistence."""
        proof: AuthorityProof = command.authority
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
        if proof.payload_sha256 != sha256_json(command.payload):
            raise PermissionError("authority proof payload binding does not match")
        if proof.binding_sha256 != command.binding_sha256():
            raise PermissionError("authority proof command binding does not match")
        if proof.expires_at <= utc_now():
            raise PermissionError("authority proof has expired")
        if not self._authority_verifier(command):
            raise PermissionError("control input authority signature is not trusted")


__all__ = ["PgControlInbox"]
