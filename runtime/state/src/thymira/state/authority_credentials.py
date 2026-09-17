"""Entry-point loading for the durable lifecycle authority signing key.

The API and lifecycle worker are separate processes, so they must resolve the same stable key
without deriving it from a bearer credential.  This module intentionally performs no environment
file loading: callers at the process boundary choose whether to pass an explicit environment or
the real process environment.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from thymira.events import create_private_file, secure_file

if TYPE_CHECKING:
    from collections.abc import Mapping


AUTHORITY_SECRET_ENV_VAR = "THYMIRA_AUTHORITY_SECRET"  # noqa: S105 - environment name only.
"""Environment variable for an operator-supplied shared signing key."""

AUTHORITY_KEY_FILE_NAME = "authority-key"
"""Private state-root file used when no explicit environment key is supplied."""

AUTHORITY_KEY_BYTES = 32
"""Size of a key minted by the local composition root."""

AUTHORITY_SECRET_HEX_LENGTH = AUTHORITY_KEY_BYTES * 2
"""Exact hexadecimal character length accepted from the process environment."""


class AuthorityCredentialError(ValueError):
    """Raised when a lifecycle authority key cannot be loaded safely."""


def resolve_authority_secret(
    state_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[bytes, Path | None]:
    """Load an explicit key or create a stable private key below ``state_root``.

    An explicitly present environment variable always wins, including an invalid value: the
    loader fails closed instead of silently replacing operator configuration.  When the variable
    is absent, the key is loaded from ``authority-key`` or minted there with exclusive private
    file creation.  The returned path is ``None`` for an environment-supplied key.

    Args:
        state_root: The process's private runtime state directory.
        environ: Optional environment mapping for process-entry tests.  The default reads the
            current process environment; this function never loads ``.env`` itself.

    Returns:
        The signing key and the persisted key path, or ``None`` when supplied by the environment.

    Raises:
        AuthorityCredentialError: The configured or persisted key is invalid.
    """
    environment = os.environ if environ is None else environ
    if AUTHORITY_SECRET_ENV_VAR in environment:
        return _environment_secret(environment[AUTHORITY_SECRET_ENV_VAR]), None

    path = Path(state_root) / AUTHORITY_KEY_FILE_NAME
    if path.exists():
        secure_file(path)
        return _persisted_secret(path), path

    secret = secrets.token_bytes(AUTHORITY_KEY_BYTES)
    try:
        create_private_file(path, secret)
    except FileExistsError:
        # Another process may have established the shared key between exists/read and create.
        secure_file(path)
        return _persisted_secret(path), path
    return secret, path


def _environment_secret(value: str) -> bytes:
    """Validate an operator-supplied environment value without retaining a second copy."""
    if value != value.strip():
        raise AuthorityCredentialError(
            f"the value of ${AUTHORITY_SECRET_ENV_VAR} carries surrounding whitespace"
        )
    if len(value) != AUTHORITY_SECRET_HEX_LENGTH:
        raise AuthorityCredentialError(
            f"the value of ${AUTHORITY_SECRET_ENV_VAR} must be exactly "
            f"{AUTHORITY_SECRET_HEX_LENGTH} hexadecimal characters"
        )
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise AuthorityCredentialError(
            f"the value of ${AUTHORITY_SECRET_ENV_VAR} must be hexadecimal"
        ) from exc


def _persisted_secret(path: Path) -> bytes:
    """Read and validate one exact locally minted key."""
    secret = path.read_bytes()
    if len(secret) != AUTHORITY_KEY_BYTES:
        raise AuthorityCredentialError("the persisted lifecycle authority key has an invalid size")
    return secret


__all__ = [
    "AUTHORITY_KEY_BYTES",
    "AUTHORITY_KEY_FILE_NAME",
    "AUTHORITY_SECRET_ENV_VAR",
    "AUTHORITY_SECRET_HEX_LENGTH",
    "AuthorityCredentialError",
    "resolve_authority_secret",
]
