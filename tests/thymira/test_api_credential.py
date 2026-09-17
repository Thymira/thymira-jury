"""Unit tests for the API's per-process credential (F13.3, authentication leg).

Real: :mod:`thymira.api.credential` and the filesystem under ``tmp_path``. Nothing here starts a
server or builds an application; these tests pin the credential itself -- how it is minted, what
an operator-supplied token must satisfy, that the plaintext never survives inside the resolver,
and that a refusal never echoes the value it refused.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from thymira.api.credential import (
    API_TOKEN_ENV_VAR,
    DEFAULT_PRINCIPAL_ID,
    PRINCIPAL_ENV_VAR,
    TOKEN_FILE_NAME,
    ApiCredentialError,
    ProcessCredential,
    mint_api_token,
    resolve_process_credential,
    validate_api_token,
)
from thymira.api.permissions import BearerTokenRegistry, Permission, principal_for_role
from thymira.events import secure_file

_VALID_TOKEN = "vTQ7mK2xNpR9wLf4ZsB6hJ1dCyEgA8uX"  # noqa: S105 - a fixture value, not a secret.


def _stored_values(obj: object) -> list[object]:
    """Return every value the object holds, whether it uses ``__slots__`` or a ``__dict__``."""
    slots = list(getattr(type(obj), "__slots__", ()))
    attributes = list(vars(obj)) if hasattr(obj, "__dict__") else []
    return [getattr(obj, name) for name in slots + attributes if hasattr(obj, name)]


def test_a_minted_token_carries_at_least_256_bits() -> None:
    """``mint_api_token`` returns a fresh 256-bit URL-safe secret on every call."""
    first = mint_api_token()
    second = mint_api_token()

    assert len(first) >= 43
    assert first != second


def test_a_minted_token_satisfies_the_operator_floor() -> None:
    """The floor an operator's token must clear never rejects the runtime's own mint."""
    minted = mint_api_token()

    assert validate_api_token(minted) == minted


@pytest.mark.parametrize(
    ("token", "rule"),
    [
        ("short", "32"),
        ("a" * 31, "32"),
        ("a" * 64, "distinct"),
        ("abababab" * 8, "distinct"),
    ],
)
def test_a_token_below_the_floor_is_refused(token: str, rule: str) -> None:
    """A token under the length floor or under the variety floor is refused by rule name."""
    with pytest.raises(ApiCredentialError, match=rule):
        validate_api_token(token)


@pytest.mark.parametrize("padded", [f" {_VALID_TOKEN}", f"{_VALID_TOKEN} ", f"\t{_VALID_TOKEN}\n"])
def test_a_whitespace_padded_token_is_refused(padded: str) -> None:
    """Surrounding whitespace is refused rather than silently stripped."""
    with pytest.raises(ApiCredentialError, match="whitespace"):
        validate_api_token(padded)


def test_the_refusal_message_never_contains_the_token() -> None:
    """A refusal names the rule that failed and never echoes the value that failed it."""
    rejected = "q" * 64

    with pytest.raises(ApiCredentialError) as caught:
        validate_api_token(rejected)

    assert rejected not in str(caught.value)
    assert "q" * 8 not in str(caught.value)


def test_the_credential_never_retains_the_plaintext() -> None:
    """No attribute, and no repr, of a built credential holds the token it was built from."""
    credential = ProcessCredential(_VALID_TOKEN, principal_for_role("operator", "admin"))

    assert _VALID_TOKEN not in repr(credential)
    stored = _stored_values(credential)
    assert stored, "the credential stores nothing at all; the test would pass vacuously"
    for value in stored:
        assert _VALID_TOKEN not in str(value)
        assert _VALID_TOKEN not in repr(value)


def test_a_wrong_token_resolves_to_none() -> None:
    """Only the exact token resolves; anything else authenticates nobody."""
    credential = ProcessCredential(_VALID_TOKEN, principal_for_role("operator", "admin"))

    assert credential.resolve("wrong" + _VALID_TOKEN[5:]) is None
    assert credential.resolve("") is None
    assert credential.resolve(_VALID_TOKEN[:-1]) is None


def test_the_right_token_resolves_to_the_configured_principal() -> None:
    """The configured principal, with its role's permissions, is what the token authenticates."""
    credential = ProcessCredential(_VALID_TOKEN, principal_for_role("ops", "admin"))

    principal = credential.resolve(_VALID_TOKEN)

    assert principal is not None
    assert principal.id == "ops"
    assert principal.role == "admin"
    assert principal.allows(Permission.APPROVE)


def test_a_credential_cannot_be_built_from_a_token_below_the_floor() -> None:
    """Construction validates: a weak token never becomes a live credential."""
    with pytest.raises(ApiCredentialError):
        ProcessCredential("short", principal_for_role("ops", "admin"))


def test_the_token_file_is_written_once_and_re_read(tmp_path: Path) -> None:
    """With no environment token the runtime mints one, writes it, and it authenticates."""
    credential, path = resolve_process_credential(tmp_path)

    assert path == tmp_path / TOKEN_FILE_NAME
    assert path.is_file()
    written = path.read_text(encoding="utf-8").strip()
    principal = credential.resolve(written)
    assert principal is not None
    assert principal.id == DEFAULT_PRINCIPAL_ID
    assert principal.role == "admin"


def test_a_second_process_cannot_replace_the_existing_token(tmp_path: Path) -> None:
    """A live process's token remains valid when another process starts on the same state root."""
    first, path = resolve_process_credential(tmp_path)

    assert path is not None
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        resolve_process_credential(tmp_path)

    assert path.read_bytes() == original
    assert first.resolve(original.decode("utf-8").strip()) is not None


def test_replace_existing_replaces_a_leftover_token(tmp_path: Path) -> None:
    """The explicit opt-in mints a new token and atomically replaces a stopped server's leftover.

    This is the recovery path for the exact production failure the opt-in exists for: a server
    stopped (or crashed) and left ``api-token`` behind, and the next start must not refuse forever.
    """
    first, path = resolve_process_credential(tmp_path)
    assert path is not None
    original = path.read_bytes()

    second, replaced_path = resolve_process_credential(tmp_path, replace_existing=True)

    assert replaced_path == path
    written = path.read_bytes()
    assert written != original
    new_token = written.decode("utf-8").strip()
    assert second.resolve(new_token) is not None
    assert first.resolve(new_token) is None
    evidence = secure_file(path)
    assert evidence.path == path
    if evidence.platform != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_replace_existing_retries_a_transient_atomic_replace_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief scanner or sync-client lock does not strand an explicit server restart."""
    _, path = resolve_process_credential(tmp_path)
    assert path is not None
    original = path.read_bytes()
    original_replace = Path.replace
    attempts = 0

    def transient_denial(source: Path, target: Path) -> Path:
        nonlocal attempts
        if Path(target) == path and attempts == 0:
            attempts += 1
            raise PermissionError(5, "transient sharing denial", str(path))
        attempts += 1
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", transient_denial)

    credential, replaced_path = resolve_process_credential(tmp_path, replace_existing=True)

    assert attempts == 2
    assert replaced_path == path
    written = path.read_bytes()
    assert written != original
    assert credential.resolve(written.decode("utf-8").strip()) is not None


def test_replace_existing_mints_normally_when_no_token_exists(tmp_path: Path) -> None:
    """The opt-in behaves like the default path when there is nothing to replace."""
    credential, path = resolve_process_credential(tmp_path, replace_existing=True)

    assert path is not None
    assert path.is_file()
    written = path.read_text(encoding="utf-8").strip()
    assert credential.resolve(written) is not None


def test_a_reparse_state_root_is_refused_before_token_bytes_are_written(tmp_path: Path) -> None:
    """A symlinked state root cannot redirect the credential file outside the requested root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    try:
        redirected.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(OSError, match="reparse"):
        resolve_process_credential(redirected)

    assert not (outside / TOKEN_FILE_NAME).exists()


def test_the_minted_token_file_holds_no_trailing_platform_newline(tmp_path: Path) -> None:
    """The file is written with a LF newline, so a reader on any platform gets the same bytes."""
    _, path = resolve_process_credential(tmp_path)

    assert path is not None
    assert b"\r" not in path.read_bytes()


def test_an_environment_supplied_token_is_used_and_no_file_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who already holds the secret gets no copy of it on disk."""
    monkeypatch.setenv(API_TOKEN_ENV_VAR, _VALID_TOKEN)

    credential, path = resolve_process_credential(tmp_path)

    assert path is None
    assert not (tmp_path / TOKEN_FILE_NAME).exists()
    assert credential.resolve(_VALID_TOKEN) is not None


def test_an_environment_token_below_the_floor_fails_the_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weak operator token refuses the process rather than starting an easy-to-guess API."""
    monkeypatch.setenv(API_TOKEN_ENV_VAR, "abc")

    with pytest.raises(ApiCredentialError, match="32"):
        resolve_process_credential(tmp_path)
    assert not (tmp_path / TOKEN_FILE_NAME).exists()


def test_a_blank_environment_token_mints_instead_of_refusing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported-but-empty variable is treated as unset, the way a shell produces it."""
    monkeypatch.setenv(API_TOKEN_ENV_VAR, "")

    _, path = resolve_process_credential(tmp_path)

    assert path is not None


def test_the_environment_names_the_principal_the_token_authenticates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``THYMIRA_API_PRINCIPAL`` names the recorded identity; the role stays admin."""
    monkeypatch.setenv(API_TOKEN_ENV_VAR, _VALID_TOKEN)
    monkeypatch.setenv(PRINCIPAL_ENV_VAR, "  val  ")

    credential, _ = resolve_process_credential(tmp_path)

    principal = credential.resolve(_VALID_TOKEN)
    assert principal is not None
    assert principal.id == "val"


def test_the_digest_is_the_sha256_of_the_token() -> None:
    """The stored material is exactly the token's sha256 digest -- no plaintext, no salt table."""
    credential = ProcessCredential(_VALID_TOKEN, principal_for_role("ops", "admin"))
    expected = hashlib.sha256(_VALID_TOKEN.encode("utf-8")).digest()

    assert expected in _stored_values(credential)


def test_a_bearer_token_registry_never_prints_its_tokens() -> None:
    """``RuntimeDeps`` is a dataclass whose generated repr would otherwise print every token."""
    registry = BearerTokenRegistry({_VALID_TOKEN: principal_for_role("ops", "admin")})

    text = repr(registry)

    assert _VALID_TOKEN not in text
    assert "1 token" in text
