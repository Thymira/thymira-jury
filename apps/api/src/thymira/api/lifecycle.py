"""Authenticated lifecycle command construction at the HTTP boundary.

The API authenticates a request once, then binds the resolved principal to the exact durable
control input it asks the owner to apply.  This module only constructs signed facts; the lifecycle
repository remains the component that verifies and persists them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

from thymira.events import canonical_json, scrub_credentials_value, sha256_text
from thymira.schemas import (
    AuthorityProof,
    ControlInput,
    ControlInputKind,
    InboxLane,
    JsonObject,
    new_id,
    utc_now,
)
from thymira.state import sign_authority

if TYPE_CHECKING:
    from thymira.api.permissions import Principal


class AuthorityProofBuilder:
    """Issue one process signed proof for one exact authenticated control input."""

    def __init__(
        self,
        secret: bytes,
        *,
        issuer_process_id: str,
        issuer_key_id: str,
        ttl: timedelta = timedelta(hours=24),
    ) -> None:
        if not secret:
            raise ValueError("authority signing secret must not be empty")
        if not issuer_process_id.strip() or not issuer_key_id.strip():
            raise ValueError("authority issuer identity must not be empty")
        if ttl <= timedelta(0):
            raise ValueError("authority proof ttl must be greater than zero")
        self._secret = bytes(secret)
        self._issuer_process_id = issuer_process_id
        self._issuer_key_id = issuer_key_id
        self._ttl = ttl

    def build(
        self,
        principal: Principal,
        *,
        session_id: str,
        run_id: str,
        kind: ControlInputKind,
        payload: JsonObject,
        requested_lane: InboxLane,
        effective_lane: InboxLane | None = None,
        target_turn_id: str | None = None,
        target_work_ids: tuple[str, ...] = (),
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ControlInput:
        """Build and sign a command bound to ``principal`` and all command fields."""
        safe_payload = cast("JsonObject", scrub_credentials_value(payload))
        issued_at = utc_now()
        input_id = new_id("input")
        proof_id = new_id("proof")
        command_request_id = request_id or new_id("request")
        payload_sha256 = sha256_text(canonical_json(safe_payload))
        unsigned = AuthorityProof(
            proof_id=proof_id,
            actor=principal.actor(),
            authenticated_principal_id=principal.id,
            authenticated_role=principal.role,
            permissions=tuple(sorted(permission.value for permission in principal.permissions)),
            session_id=session_id,
            run_id=run_id,
            issuer_process_id=self._issuer_process_id,
            issuer_key_id=self._issuer_key_id,
            issued_at=issued_at,
            expires_at=issued_at + self._ttl,
            request_id=command_request_id,
            signature="unsigned",
            input_id=input_id,
            payload_sha256=payload_sha256,
            binding_sha256="0" * 64,
            requested_lane=requested_lane,
            effective_lane=effective_lane,
            kind=kind,
            target_turn_id=target_turn_id,
            target_work_ids=target_work_ids,
            idempotency_key=idempotency_key,
        )
        command = ControlInput(
            input_id=input_id,
            run_id=run_id,
            session_id=session_id,
            requested_lane=requested_lane,
            effective_lane=effective_lane,
            kind=kind,
            payload=safe_payload,
            target_turn_id=target_turn_id,
            target_work_ids=target_work_ids,
            authority=unsigned,
            idempotency_key=idempotency_key,
        )
        proof = unsigned.model_copy(update={"binding_sha256": command.binding_sha256()})
        return command.model_copy(update={"authority": sign_authority(proof, self._secret)})

    def __repr__(self) -> str:
        """Describe the issuer without exposing the signing secret."""
        return (
            f"AuthorityProofBuilder(process={self._issuer_process_id!r}, "
            f"key={self._issuer_key_id!r})"
        )


__all__ = ["AuthorityProofBuilder"]
