"""Process-composition authority verification for durable lifecycle commands."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from thymira.events import canonical_json

if TYPE_CHECKING:
    from thymira.schemas import AuthorityProof, ControlInput


class HmacAuthorityVerifier:
    """Verify an exact API-issued authority envelope with an in-memory composition secret.

    The secret is supplied by the process composition root and is never written to lifecycle
    records.  HMAC is the narrow local composition mechanism for this slice; a deployed API may
    replace this callable with its existing key or token verifier without changing the inbox.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        issuer_process_id: str,
        issuer_key_id: str,
    ) -> None:
        if not secret:
            raise ValueError("authority verification secret must not be empty")
        if not issuer_process_id.strip() or not issuer_key_id.strip():
            raise ValueError("authority issuer identity must not be empty")
        self._secret = bytes(secret)
        self._issuer_process_id = issuer_process_id
        self._issuer_key_id = issuer_key_id

    def __call__(self, command: ControlInput) -> bool:
        """Return whether the exact command proof was signed by this composition issuer."""
        proof = command.authority
        if (
            proof.issuer_process_id != self._issuer_process_id
            or proof.issuer_key_id != self._issuer_key_id
        ):
            return False
        expected = _signature(proof, self._secret)
        return hmac.compare_digest(proof.signature, expected)


def sign_authority(proof: AuthorityProof, secret: bytes) -> AuthorityProof:
    """Return ``proof`` with an HMAC over its exact envelope for test/composition issuance."""
    if not secret:
        raise ValueError("authority verification secret must not be empty")
    return proof.model_copy(update={"signature": _signature(proof, secret)})


def _signature(proof: AuthorityProof, secret: bytes) -> str:
    """Compute the stable HMAC representation for one authority envelope."""
    message = canonical_json(proof.signing_payload()).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


__all__ = ["HmacAuthorityVerifier", "sign_authority"]
