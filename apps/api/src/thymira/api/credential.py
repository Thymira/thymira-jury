"""The API's per-process credential: one real secret every route is authenticated against.

F13.3 requires that *loopback location and endpoint names never grant authority*. This module is
the credential half of that: the composition root mints a 256-bit secret at start-up (or accepts
one the operator exported), and :class:`ProcessCredential` is the
:class:`~thymira.api.permissions.PrincipalResolver` every route dependency runs through. The
credential authenticates a *process*, not a person -- FINAL swaps a JWT/OIDC verifier behind the
same protocol -- but until it does, the holder of this token is the authenticated principal and
nothing else is.

The resolver retains only the sha256 digest, compared in constant time. The token necessarily
exists transiently while it is read from the environment, request header or protected token file;
this module does not claim to erase those input copies from process memory. It is never logged (the
entry point prints the token *file path*, never a value), never written to an event, never in a
trace and never in an error body -- a refusal names the rule it enforced, not the value it refused.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path  # noqa: TC003 - used in a runtime-evaluated signature.

from thymira.api.permissions import Principal, principal_for_role
from thymira.events import create_private_file
from thymira.state import (
    AUTHORITY_KEY_BYTES,
    AUTHORITY_KEY_FILE_NAME,
    AUTHORITY_SECRET_ENV_VAR,
    AuthorityCredentialError,
    replace_with_retry,
    resolve_authority_secret,
)

_LOGGER = logging.getLogger(__name__)

API_TOKEN_ENV_VAR = "THYMIRA_API_TOKEN"  # noqa: S105 - a variable name, not a value.
"""Environment variable carrying an operator-supplied API token."""

PRINCIPAL_ENV_VAR = "THYMIRA_API_PRINCIPAL"
"""Environment variable naming the identity the token authenticates."""

TOKEN_FILE_NAME = "api-token"  # noqa: S105 - a file name, not a credential value.
"""File, under the runtime state root, a minted token is written to."""

MINIMUM_TOKEN_LENGTH = 32
"""Shortest operator-supplied token accepted. See :func:`validate_api_token`."""

MINIMUM_DISTINCT_CHARACTERS = 8
"""Fewest distinct characters an operator-supplied token may contain."""

DEFAULT_PRINCIPAL_ID = "operator"
"""Identity recorded for the process credential when the environment names none."""

_TOKEN_BYTES = 32


class ApiCredentialError(RuntimeError):
    """Raised when an API credential cannot be established.

    The message always names the rule that failed and never the value that failed it: this error
    reaches a console, a log file and an operator's terminal scrollback.
    """


def mint_api_token() -> str:
    """Return a fresh 256-bit URL-safe API token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def validate_api_token(token: str) -> str:
    """Return ``token`` when it clears the operator floor, or raise.

    The floor is a *floor*, not a proof of entropy: the runtime cannot verify that a secret it did
    not generate was randomly chosen. It rejects the mistakes it can see -- a short token, a token
    built from a handful of repeated characters, and a value carrying surrounding whitespace that
    a shell or an editor added -- so an obviously guessable credential never becomes the only
    thing standing between an anonymous caller and a Run.

    Args:
        token: The candidate credential, exactly as the operator supplied it.

    Returns:
        The same token, unchanged.

    Raises:
        ApiCredentialError: The token does not clear the floor. The message names the rule; it
            never echoes the token.
    """
    if token != token.strip():
        msg = (
            f"the value of ${API_TOKEN_ENV_VAR} carries surrounding whitespace; "
            "export it without leading or trailing spaces, tabs or newlines"
        )
        raise ApiCredentialError(msg)
    if len(token) < MINIMUM_TOKEN_LENGTH:
        msg = f"the value of ${API_TOKEN_ENV_VAR} is shorter than {MINIMUM_TOKEN_LENGTH} characters"
        raise ApiCredentialError(msg)
    if len(set(token)) < MINIMUM_DISTINCT_CHARACTERS:
        msg = (
            f"the value of ${API_TOKEN_ENV_VAR} uses fewer than "
            f"{MINIMUM_DISTINCT_CHARACTERS} distinct characters"
        )
        raise ApiCredentialError(msg)
    return token


class ProcessCredential:
    """One token, one principal: the :class:`PrincipalResolver` for a single-process API.

    Only ``sha256(token)`` is retained by this resolver. sha256 rather than a password KDF is the
    right primitive here for the runtime-minted 256-bit random token, because there is no
    meaningful offline-guessing advantage for a KDF to provide. The digest keeps the long-lived
    resolver object, its ``repr`` and its tracebacks from carrying the token; it cannot erase
    transient copies held by the environment, HTTP framework or file reader, and it does not
    claim to resist cracking. An operator-supplied token clears :func:`validate_api_token` first,
    which rejects obvious weak values without proving entropy.
    """

    __slots__ = ("_digest", "_principal")

    def __init__(self, token: str, principal: Principal) -> None:
        """Bind ``principal`` to the digest of ``token``.

        Raises:
            ApiCredentialError: ``token`` does not clear :func:`validate_api_token`.
        """
        self._digest = hashlib.sha256(validate_api_token(token).encode("utf-8")).digest()
        self._principal = principal

    def resolve(self, token: str) -> Principal | None:
        """Return the principal when ``token`` matches, comparing in constant time."""
        candidate = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(self._digest, candidate):
            return None
        return self._principal

    def __repr__(self) -> str:
        """Describe the credential by the identity it authenticates, never by its secret."""
        return f"ProcessCredential(principal={self._principal.id!r})"


def _configured_principal() -> Principal:
    """Build the principal the process credential authenticates."""
    declared = os.environ.get(PRINCIPAL_ENV_VAR, "").strip()
    return principal_for_role(declared or DEFAULT_PRINCIPAL_ID, "admin")


def resolve_process_credential(
    state_root: Path, *, replace_existing: bool = False
) -> tuple[ProcessCredential, Path | None]:
    """Establish this process's API credential, minting and persisting one when needed.

    An exported ``THYMIRA_API_TOKEN`` wins and nothing is written to disk: the operator already
    holds the secret, so persisting a second copy of it would be strictly worse. Otherwise a fresh
    token is minted and created exclusively at ``<state_root>/api-token``. The shared events-layer
    writer establishes and verifies private permissions before publishing the token bytes; by
    default an existing path is refused rather than replaced, so a second server cannot
    accidentally invalidate a live process's credential.

    Args:
        state_root: The runtime state directory a minted token is written under.
        replace_existing: An explicit operator opt-in (the server's ``--replace-credential``
            flag) to replace a token file a stopped or crashed server left behind, instead of
            refusing to start. The replacement is atomic: the new token is written to a private
            temporary file in the same directory, then moved onto the target path with a bounded
            retry for transient Windows sharing denials, so there is no window where the path is
            missing or world-readable. A replacement is logged at WARNING with the path, never
            the token. Default ``False`` keeps every existing caller's refusal unchanged.

    Returns:
        The credential, and the path a token was written to -- ``None`` when the environment
        supplied one and nothing was written.

    Raises:
        ApiCredentialError: An exported token does not clear :func:`validate_api_token`.
        OSError: The minted token could not be created or replaced. Without
            ``replace_existing``, an existing token path is refused rather than overwritten.
    """
    supplied = os.environ.get(API_TOKEN_ENV_VAR, "").strip()
    principal = _configured_principal()
    if supplied:
        return ProcessCredential(os.environ[API_TOKEN_ENV_VAR], principal), None
    token = mint_api_token()
    credential = ProcessCredential(token, principal)
    path = state_root / TOKEN_FILE_NAME
    data = f"{token}\n".encode()
    if replace_existing:
        _replace_private_file(path, data)
    else:
        create_private_file(path, data)
    return credential, path


def _replace_private_file(path: Path, data: bytes) -> None:
    """Atomically publish ``data`` at ``path``, replacing an existing file there.

    Reached only under the explicit ``replace_existing`` opt-in. The new content is written to a
    private temporary file in the same directory via :func:`create_private_file` -- exclusive
    creation, permissions established and verified before any byte is written -- then moved onto
    ``path`` with the state layer's bounded atomic-replace helper. It retries only the transient
    Windows sharing denial caused by scanners or sync clients; there is no window where ``path``
    is missing or world-readable. A replacement of a pre-existing file is logged at WARNING with
    the path; the value is never logged.
    """
    replacing_existing = path.exists()
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    create_private_file(temp_path, data)
    try:
        replace_with_retry(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    if replacing_existing:
        _LOGGER.warning("replaced an existing API token file left by a stopped server: %s", path)


def resolve_process_authority_secret(state_root: Path) -> tuple[bytes, Path | None]:
    """Load or create the private signing key used by the lifecycle authority verifier.

    The key is separate from bearer-token records and is never included in JSON, events, reprs or
    error messages.  Reusing the protected file across restarts keeps already persisted control
    inputs verifiable by the next API process.
    """
    try:
        return resolve_authority_secret(state_root)
    except AuthorityCredentialError as exc:
        raise ApiCredentialError(str(exc)) from exc


__all__ = [
    "API_TOKEN_ENV_VAR",
    "AUTHORITY_KEY_BYTES",
    "AUTHORITY_KEY_FILE_NAME",
    "AUTHORITY_SECRET_ENV_VAR",
    "DEFAULT_PRINCIPAL_ID",
    "MINIMUM_DISTINCT_CHARACTERS",
    "MINIMUM_TOKEN_LENGTH",
    "PRINCIPAL_ENV_VAR",
    "TOKEN_FILE_NAME",
    "ApiCredentialError",
    "ProcessCredential",
    "mint_api_token",
    "resolve_process_authority_secret",
    "resolve_process_credential",
    "validate_api_token",
]
