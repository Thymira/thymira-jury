"""Unit evidence for the lifecycle authority signing key and the API composition that loads it.

Real: the loader, the filesystem below ``tmp_path``, the HMAC verifier and the API composition.
Faked: nothing -- these tests need no model, no graph and no HTTP client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.api import build_default_deps
from thymira.api.credential import ApiCredentialError, resolve_process_authority_secret
from thymira.api.lifecycle import AuthorityProofBuilder
from thymira.state import (
    AUTHORITY_KEY_BYTES,
    AUTHORITY_KEY_FILE_NAME,
    AUTHORITY_SECRET_ENV_VAR,
    AuthorityCredentialError,
    LocalLifecycleRepository,
    resolve_authority_secret,
)

if TYPE_CHECKING:
    from pathlib import Path

_CONFIGURED = "ab" * AUTHORITY_KEY_BYTES


def test_an_explicit_environment_key_wins_and_is_never_persisted(tmp_path: Path) -> None:
    """An operator-supplied key is used exactly and leaves no copy below the state root."""
    secret, path = resolve_authority_secret(
        tmp_path,
        environ={AUTHORITY_SECRET_ENV_VAR: _CONFIGURED},
    )

    assert secret == bytes.fromhex(_CONFIGURED)
    assert path is None
    assert not (tmp_path / AUTHORITY_KEY_FILE_NAME).exists()


def test_a_locally_minted_key_is_private_and_stable_across_calls(tmp_path: Path) -> None:
    """Without configuration the loader mints one key and reuses it on the next process start."""
    minted, path = resolve_authority_secret(tmp_path, environ={})

    assert path == tmp_path / AUTHORITY_KEY_FILE_NAME
    assert len(minted) == AUTHORITY_KEY_BYTES
    assert path.read_bytes() == minted

    reloaded, same_path = resolve_authority_secret(tmp_path, environ={})

    assert reloaded == minted
    assert same_path == path


@pytest.mark.parametrize("value", [" " + _CONFIGURED, "zz" * AUTHORITY_KEY_BYTES, "ab" * 8])
def test_a_configured_key_that_is_not_exactly_hexadecimal_fails_closed(
    tmp_path: Path, value: str
) -> None:
    """A present but invalid variable is refused rather than silently replaced by a fresh key."""
    with pytest.raises(AuthorityCredentialError):
        resolve_authority_secret(tmp_path, environ={AUTHORITY_SECRET_ENV_VAR: value})

    assert not (tmp_path / AUTHORITY_KEY_FILE_NAME).exists()


def test_the_api_credential_helper_reports_a_bad_key_as_an_api_credential_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API boundary raises its own error type so start-up reports one failure vocabulary."""
    monkeypatch.setenv(AUTHORITY_SECRET_ENV_VAR, "not-hexadecimal")

    with pytest.raises(ApiCredentialError, match=AUTHORITY_SECRET_ENV_VAR):
        resolve_process_authority_secret(tmp_path)


def test_the_default_composition_establishes_its_own_signed_lifecycle_boundary(
    tmp_path: Path,
) -> None:
    """A composition given no secret still gets a real repository and a real proof builder."""
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)

    assert isinstance(deps.lifecycle_repository, LocalLifecycleRepository)
    assert isinstance(deps.authority_proof_builder, AuthorityProofBuilder)
    assert (tmp_path / AUTHORITY_KEY_FILE_NAME).is_file()


def test_a_caller_supplied_secret_is_used_instead_of_minting_one(tmp_path: Path) -> None:
    """Two processes sharing one key must not each mint their own below the same state root."""
    deps = build_default_deps(
        tmp_path,
        principal_resolver=TEST_CREDENTIAL,
        authority_secret=bytes.fromhex(_CONFIGURED),
    )

    assert isinstance(deps.lifecycle_repository, LocalLifecycleRepository)
    assert not (tmp_path / AUTHORITY_KEY_FILE_NAME).exists()


def test_the_composition_repr_never_carries_the_signing_key(tmp_path: Path) -> None:
    """The dependency graph reaches logs and tracebacks; the authority key may not ride along."""
    deps = build_default_deps(
        tmp_path,
        principal_resolver=TEST_CREDENTIAL,
        authority_secret=bytes.fromhex(_CONFIGURED),
    )

    assert _CONFIGURED not in repr(deps)
    assert "ab\\xab" not in repr(deps)
