"""Shared credential-name classification for source and durable-data boundaries."""

from __future__ import annotations

_CREDENTIAL_NAME_FRAGMENTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)
"""Case-insensitive name fragments that identify a credential-bearing field."""

# Configuration names that include a credential fragment but identify a path, label or bound are
# not themselves secret material. Keep this allowlist narrow: an access key ending in ``_ID`` is
# still credential-shaped, while ``THYMIRA_KEY_PATH`` and ``TOKEN_LIMIT`` remain useful settings.
_NON_CREDENTIAL_NAME_SUFFIXES = (
    "_PATH",
    "_FILE",
    "_NAME",
    "_COUNT",
    "_LIMIT",
    "_TOKENS",
)


def is_credential_environment_name(name: str) -> bool:
    """Return whether a field or environment name is credential-shaped.

    The name is shared by source redaction, sandbox environment filtering and settings-schema
    validation. It classifies names only; a raw value still requires the caller to reject it at its
    owning boundary.
    """
    folded = name.strip().upper()
    if not folded or folded.endswith(_NON_CREDENTIAL_NAME_SUFFIXES):
        return False
    return any(fragment in folded for fragment in _CREDENTIAL_NAME_FRAGMENTS)


__all__ = ["is_credential_environment_name"]
