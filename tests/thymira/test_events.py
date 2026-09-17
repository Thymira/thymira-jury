"""Behaviour of ``thymira.events``: hashing, redaction and the hash-chained event log.

Ported from the thesis prototype (``tracing.py`` + ``utils.py``, tag ``thesis/mads-v0.1.0``)
onto the typed ``thymira.schemas.Event`` contract. No network, no LLM: pure functions and
on-disk JSONL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import string
import textwrap
import threading
import time
from typing import TYPE_CHECKING, cast

import pytest

from thymira.events import (
    GENESIS_HASH,
    SHADOWED_SEQS_KEY,
    ExportRedactionError,
    InMemoryEventLog,
    JsonlEventLog,
    MalformedEventFormatError,
    MissingEventFormatError,
    UnknownEventTypeError,
    UnsupportedEventFormatError,
    VerificationResult,
    canonical_json,
    create_private_file,
    current_surface,
    derive_surface,
    hash_event,
    is_credential_environment_name,
    read_events,
    redact,
    redact_export,
    redact_value,
    scrub_credentials,
    scrub_credentials_value,
    sha256_bytes,
    sha256_file,
    sha256_text,
    shadowed_seqs,
    verify_events,
    verify_log,
)
from thymira.schemas import Actor, Event, EventSurface, EventType, SurfaceState, new_id

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_canonical_json_is_order_independent_and_compact() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    # No spaces after separators, non-ASCII kept verbatim (ensure_ascii=False).
    assert canonical_json({"name": "Ramón", "n": [1, 2]}) == '{"n":[1,2],"name":"Ramón"}'


def test_sha256_helpers_agree_with_hashlib(tmp_path: Path) -> None:
    text = "audit trail ✓"
    assert sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sha256_bytes(b"\x00\x01\x02") == hashlib.sha256(b"\x00\x01\x02").hexdigest()

    path = tmp_path / "blob.bin"
    payload = b"x" * (128 * 1024 + 7)  # spans several read chunks
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_hash_event_matches_manual_recipe() -> None:
    event = Event(
        event_id=new_id("event"),
        run_id=new_id("run"),
        seq=0,
        type=EventType.RUN_STARTED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.events",
        producer_version="0.3",
        payload={"prompt": "x"},
    )
    assert hash_event(event) == sha256_text(canonical_json(event.hashable_dict()))


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_redact_masks_secrets_and_pii() -> None:
    assert redact("write to alice@example.com now") == "write to [REDACTED:EMAIL] now"
    assert "[REDACTED:API_KEY]" in redact("export KEY=sk-deadbeef0123456789")
    assert "[REDACTED:BEARER]" in redact("Authorization: Bearer eyJhbGci.OiJ9.abc123")
    assert "[REDACTED:IBAN]" in redact("pay to ES9121000418450200051332 today")
    assert "[REDACTED:DNI]" in redact("holder 12345678Z signed")
    assert "[REDACTED:DNI]" in redact("holder X1234567L signed")  # NIE
    assert "[REDACTED:PHONE]" in redact("call 612 345 678 please")
    assert "[REDACTED:PHONE]" in redact("call +1 415 555 0132 please")  # international
    assert "[REDACTED:CARD]" in redact("card 4111 1111 1111 1111 expires soon")
    # Ordinary prose with a bare number is left untouched.
    assert redact("the quick brown fox jumped over 42 dogs") == (
        "the quick brown fox jumped over 42 dogs"
    )


def test_redact_preserves_decimal_metrics_inside_json_text() -> None:
    """Keep metric precision when an agent serializes its result as JSON text."""
    metrics = {
        "roc_auc": 0.8123456789012345,
        "precision": 0.7234567890123456,
        "recall": 0.6345678901234567,
        "f1": 0.7456789012345678,
        "log_loss": 0.3567890123456789,
        "balanced_accuracy": 0.8678901234567891,
    }

    rendered = json.dumps(metrics, separators=(",", ":"))

    assert redact(rendered) == rendered


def test_redact_distinguishes_an_unformatted_card_from_a_decimal_suffix() -> None:
    """Redact an unformatted card without treating a decimal suffix as one."""
    assert redact("card 4111111111111111") == "card [REDACTED:CARD]"
    assert redact("metric=0.4111111111111111") == "metric=0.4111111111111111"
    assert redact("reference 1234567890123456") == "reference 1234567890123456"


def test_redact_preserves_a_luhn_valid_number_inside_an_identifier() -> None:
    """Do not redact a numeric suffix embedded in an internal identifier."""
    identifier = "session_bcab08b135484f2f4111111111111111"

    assert redact(identifier) == identifier


@pytest.mark.parametrize(
    "version",
    ["thymira-mira-checks@0.1", "pkg@1.2.3", "service@10.20.30", "build@2024.11"],
)
def test_redact_leaves_a_versioned_identifier_untouched(version: str) -> None:
    # A ``name@major.minor`` string is not an email: its final label is all digits, and no real
    # top-level domain is numeric. The old domain class accepted digits in that last label, so it
    # ate MIRA's own control-set version (``AuditReport.control_set`` defaults to
    # ``thymira-mira-checks@0.1``) and turned a provenance field into a redaction marker.
    assert redact(version) == version
    assert redact(f"control_set={version} recorded") == f"control_set={version} recorded"


@pytest.mark.parametrize(
    "text",
    [
        "ana@example.com",
        "a.b+tag@sub.domain.co.uk",
        "please write to ana@example.com about the run",
        "user@example.xn--p1ai",
        "иван@почта.рф",
        "user@пример.онлайн",
    ],
    ids=[
        "plain",
        "subdomains-plus-multilabel-tld",
        "inside-a-sentence",
        "idn-punycode-tld",
        "raw-idn-tld",
        "raw-idn-tld-word",
    ],
)
def test_redact_still_masks_a_real_email_address(text: str) -> None:
    # Tightening the domain must not shrink real coverage: an all-alpha rule would also drop an
    # IDN punycode top-level domain (``xn--p1ai``), so the last label only has to *start* with a
    # letter rather than be wholly alphabetic — and that letter is any Unicode letter, because
    # ``[A-Za-z]`` would leak a raw internationalized TLD the previous pattern did redact.
    out = redact(text)
    assert "[REDACTED:EMAIL]" in out
    assert "@" not in out  # the only ``@`` in each case belongs to the redacted address


def test_redact_value_masks_an_email_but_keeps_a_version_beside_it() -> None:
    payload = {"audit": {"reviewer": "ana@example.com", "control_set": "thymira-mira-checks@0.1"}}
    out = redact_value(payload)
    assert out["audit"]["reviewer"] == "[REDACTED:EMAIL]"
    # The version travelling next to it is provenance, not PII: it survives intact.
    assert out["audit"]["control_set"] == "thymira-mira-checks@0.1"


def test_redact_export_is_the_fail_closed_json_surface() -> None:
    payload = {
        "email": "ana@example.com",
        "metric": 0.8123456789012345,
        "version": "thymira-mira-checks@0.1",
    }

    out = redact_export(payload)

    assert out["email"] == "[REDACTED:EMAIL]"
    assert out["metric"] == payload["metric"]
    assert out["version"] == payload["version"]


def test_redact_export_refuses_unknown_values() -> None:
    with pytest.raises(ExportRedactionError):
        redact_export({"object": object()})


def test_redact_export_redacts_keys_and_rejects_collisions_or_nonfinite_numbers() -> None:
    assert redact_export({"alice@example.com": "alice@example.com"}) == {
        "[REDACTED:EMAIL]": "[REDACTED:EMAIL]"
    }
    with pytest.raises(ExportRedactionError, match="merge mapping keys"):
        redact_export({"alice@example.com": 1, "[REDACTED:EMAIL]": 2})
    with pytest.raises(ExportRedactionError, match="finite"):
        redact_export(float("nan"))


def test_source_scrubbing_rejects_mapping_key_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "known-provider-value")
    with pytest.raises(ValueError, match="merge mapping keys"):
        scrub_credentials_value({"known-provider-value": 1, "[REDACTED:CREDENTIAL]": 2})


def test_source_scrubbing_replaces_known_credentials_without_classifying_paths_or_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "known-provider-value")
    monkeypatch.setenv("THYMIRA_KEY_PATH", r"C:\\secrets\\provider.key")
    monkeypatch.setenv("TOKEN_LIMIT", "128")

    assert scrub_credentials("request known-provider-value") == "request [REDACTED:CREDENTIAL]"
    assert scrub_credentials_value({"value": "known-provider-value"}) == {
        "value": "[REDACTED:CREDENTIAL]"
    }
    assert is_credential_environment_name("OPENAI_API_KEY")
    assert is_credential_environment_name("OPENAI_APIKEY")
    assert is_credential_environment_name("AWSACCESSKEY")
    assert is_credential_environment_name("CLIENTSECRET")
    assert is_credential_environment_name("AWS_ACCESS_KEY_ID")
    assert not is_credential_environment_name("THYMIRA_KEY_PATH")
    assert not is_credential_environment_name("CREDENTIAL_FILE")
    assert not is_credential_environment_name("TOKEN_NAME")
    assert not is_credential_environment_name("TOKEN_LIMIT")
    assert not is_credential_environment_name("THYMIRA_MAX_TOKENS")


def test_redact_masks_an_ipv4_literal_email_domain() -> None:
    # Requiring an alphabetic top-level label would drop an IPv4-literal domain that the old
    # pattern did redact; an explicit dotted-quad branch keeps it covered.
    assert redact("mail to root@192.168.1.1 now") == "mail to [REDACTED:EMAIL] now"


def test_redact_removes_the_whole_userinfo_of_a_url_or_dsn() -> None:
    # The username and password must both go, and no misleading marker may be left in their
    # place: a half-eaten credential that reads "[REDACTED:EMAIL]" tells an auditor the payload
    # was cleaned when it was not.
    url = redact("https://alice:P@ssw0rd123@api.example.com/v1/data")
    assert url == "https://[REDACTED:CREDENTIALS]@api.example.com/v1/data"

    dsn = redact("postgresql://svc_user:hunter2@db.internal:5432/prod")
    assert dsn == "postgresql://[REDACTED:CREDENTIALS]@db.internal:5432/prod"

    for leaked in ("alice", "P@", "ssw0rd123", "svc_user", "hunter2"):
        assert leaked not in url + dsn


@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "AWS_KEY"),
        ("ghp_016C7e2f4A8b9C0d1E2f3A4b5C6d7E8f9A0b", "GITHUB_TOKEN"),
        ("xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "SLACK_TOKEN"),
        ("AIzaSyD-1234567890abcdefghijklmnopqrstu", "GOOGLE_KEY"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P",
            "JWT",
        ),
    ],
)
def test_redact_masks_common_credential_families(secret: str, label: str) -> None:
    out = redact(f"the value is {secret} ok")
    assert secret not in out
    assert f"[REDACTED:{label}]" in out


@pytest.mark.parametrize(
    "identifier",
    [
        "token_usage_by_model_tier",
        "secret_manager_backend_name",
        "key_derivation_helper_module",
        "pk_or_token_lookup_table",
        "key_abcdefghijklmnop",
    ],
)
def test_redact_leaves_a_lowercase_only_snake_case_identifier_untouched(identifier: str) -> None:
    # The API-key heuristic ``(sk|pk|key|token|secret)[-_][A-Za-z0-9_-]{16,}`` matched ordinary
    # snake_case identifiers -- ``redact("token_usage_by_model_tier")`` used to return
    # ``[REDACTED:API_KEY]``. Since wave 4 ``spill_result`` redacts oversized tool output *before*
    # storing it, the log artifact is the only surviving copy, so a false positive is silent
    # evidence loss inside the artifact that claims to hold the complete result. A body of only
    # lowercase words joined by underscores carries no digit or uppercase letter, so credential
    # material -- hex, base64, base58 -- is distinguished from it and this identifier is left alone.
    assert redact(identifier) == identifier


def test_redact_still_masks_an_api_key_whose_body_carries_a_digit_or_uppercase() -> None:
    # The requirement is one uppercase letter *or* one digit somewhere in the 16+ body. The
    # case-insensitive match is scoped to the prefix with an inline ``(?i:...)`` group, not a global
    # ``re.IGNORECASE`` flag: under the global flag ``[0-9A-Z]`` would also match lowercase and the
    # digit-or-uppercase requirement would silently accept everything, restoring the false positive.
    assert redact("key_derivation_helper_moduleZ") == "[REDACTED:API_KEY]"  # one uppercase letter
    assert redact("token_usage_by_model_tier2") == "[REDACTED:API_KEY]"  # one digit
    # The prefix stays case-insensitive: an uppercase-spelled prefix still redacts.
    assert redact("KEY-ABCDEFGHIJKLMNOP") == "[REDACTED:API_KEY]"
    assert redact("SK_ABCDEFGHIJKLMNOP") == "[REDACTED:API_KEY]"


@pytest.mark.parametrize(
    "secret",
    [
        # OpenAI-style key: mixed case and digits.
        "sk-aB3dEf0gHi1jKl2mNo3pQr4sT5u",
        # Lowercase hexadecimal (a digest, a derived key): 16 symbols, ten of them digits.
        "key-a1b2c3d4e5f6a7b8c9d0e1f2a3b4",
        # base64url with ``=`` padding (the padding sits outside the token class and is left as-is).
        "token-" + base64.urlsafe_b64encode(b"sixteen bytes!!!").decode(),
        # base64url without padding.
        "token-" + base64.urlsafe_b64encode(b"sixteen bytes!!!").decode().rstrip("="),
        # base58 (Bitcoin/IPFS alphabet): no 0/O/I/l, mixes case and digits.
        "secret-3vQB7B6MrGQZaxCuFg4oh",
    ],
    ids=["openai-sk", "lowercase-hex", "base64url-padded", "base64url-unpadded", "base58"],
)
def test_redact_still_masks_known_key_formats_after_the_digit_requirement(secret: str) -> None:
    # No known credential format loses coverage under the digit-or-uppercase requirement: over 16+
    # characters, hex, base64 and base58 each carry a digit or an uppercase letter with probability
    # indistinguishable from 1. Each of these is a realistic sample of its format.
    out = redact(f"the value is {secret} ok")
    assert "[REDACTED:API_KEY]" in out
    assert secret not in out


def _alphanumeric(length: int) -> str:
    """A deterministic alphanumeric run, longer than any bound a pattern used to carry."""
    alphabet = string.ascii_letters + string.digits
    return "".join(alphabet[index % len(alphabet)] for index in range(length))


@pytest.mark.parametrize(
    ("template", "secret", "label"),
    [
        # Real Azure AD v2 access tokens run 1200-2400 characters; Auth0 and Google id_tokens
        # are routinely over 512. None of these lengths is exotic.
        ("Authorization: Bearer {}", _alphanumeric(1200), "BEARER"),
        ("token={}", "eyJ{0}.{0}.{0}".format(_alphanumeric(1500)), "JWT"),
        ("token {} end", "ghp_" + _alphanumeric(300), "GITHUB_TOKEN"),
        ("token {} end", "xoxb-" + _alphanumeric(300), "SLACK_TOKEN"),
        ("key {} end", "sk-" + _alphanumeric(300), "API_KEY"),
        ("https://user:{}@host/v1", _alphanumeric(400), "CREDENTIALS"),
    ],
    # The payloads run to thousands of characters; without explicit ids pytest builds the test id
    # out of them, and on Windows that overflows the 32767-character limit on PYTEST_CURRENT_TEST.
    ids=["bearer", "jwt", "github-token", "slack-token", "api-key", "url-credentials"],
)
def test_redact_masks_a_secret_longer_than_the_pattern_that_matches_it(
    template: str, secret: str, label: str
) -> None:
    # An upper bound on an open-ended secret does not shorten it, it un-redacts it, in one of two
    # ways. A capped class with no trailing anchor matches the first N characters and writes the
    # rest to the log verbatim; a capped class followed by ``\b`` can never satisfy the boundary
    # while the next character is still a word character, so the match is abandoned and the whole
    # secret is written. Either way it lands on the hash chain, where it is immutable.
    out = redact(template.format(secret))
    assert secret not in out
    assert secret[-32:] not in out
    assert f"[REDACTED:{label}]" in out


def test_redact_masks_a_private_key_block() -> None:
    # The markers are assembled at run time: written out in full they would trip the repository's
    # detect-private-key pre-commit hook, which is doing exactly its job.
    marker = "PRIVATE " + "KEY-----"
    block = (
        f"-----BEGIN RSA {marker}\nMIIEowIBAAKCAQEAx7Vn9tKk\nabc123def456\n-----END RSA {marker}"
    )
    out = redact(f"key follows:\n{block}\ndone")
    assert "MIIEowIBAAKCAQEAx7Vn9tKk" not in out
    assert "[REDACTED:PRIVATE_KEY]" in out


# The markers are assembled at run time for the same reason as above: spelled out in full they
# would trip the repository's detect-private-key pre-commit hook.
_KEY_WORDS = "PRIVATE " + "KEY"
_KEY_MARKER = f"{_KEY_WORDS}-----"
# A real PEM body: 64 base64 characters per line, which is what RFC 7468 fixes. Nothing in these
# lines is a separator, so a structural match has to consume them by their shape, not by a stop
# character.
_KEY_LINES = (
    "MIIEowIBAAKCAQEAx7Vn9tKkQpJv0mzL6oQ2Yb1sVwHh8dR3nT5cB0aF7gK9lM2p",
    "Xe4uRtYbNhGfJdKsLwQzVcXmBnPoIuYtReWqAsDfGhJkLzXcVbNmQwErTyUiOpAs",
    "DfGhJkLzXcVbNmQwErTyUiOpAsDfGhJkLzXcVbNmQwErTyUiOpAsDfGhJkLzXcVb",
)
_KEY_BODY = "\n".join(_KEY_LINES)
# The evidence a redaction must not take down with it. Its longest run of base64 characters is
# nine (``model=gbm``): prose and metric rows break at every space, ``.``, ``,``, ``-`` and ``_``.
_METRIC_ROW = (
    "model=gbm auc=0.81 ks=0.43 gini=0.62 protected_attr=age "
    "disparate_impact=0.71 fairness_gap=0.19 drift_psi=0.28"
)
# Real sha256 digests -- the currency of a content-addressed runtime, and a 64-character run of
# ``[A-Za-z0-9+/=]`` that a structural body rule must refuse to read as key material.
_ARTIFACT_DIGEST = hashlib.sha256(b"model.pkl").hexdigest()
_EVENT_DIGEST = hashlib.sha256(b"event-0").hexdigest()


def _begin(label: str) -> str:
    return f"-----BEGIN {label} {_KEY_MARKER}"


def _end(label: str) -> str:
    return f"-----END {label} {_KEY_MARKER}"


# ---------------------------------------------------------------------------
# redaction: private keys, step 1 -- the banner spelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("begin", "end"),
    [
        # `gpg --export-secret-keys --armor`: PGP puts ` BLOCK` after the words, so a pattern that
        # requires them to be the last token before the dashes never matches an entire standard
        # private-key format.
        (f"-----BEGIN PGP {_KEY_WORDS} BLOCK-----", f"-----END PGP {_KEY_WORDS} BLOCK-----"),
        # The digit in SSH2 is not in `[A-Z ]`.
        (_begin("SSH2 ENCRYPTED"), _end("SSH2 ENCRYPTED")),
        # RFC 4716 uses four dashes and pads the label with spaces.
        (
            f"---- BEGIN SSH2 ENCRYPTED {_KEY_WORDS} ----",
            f"---- END SSH2 ENCRYPTED {_KEY_WORDS} ----",
        ),
        # A tab, and a double space, where the format allows any whitespace run.
        (f"-----BEGIN\tRSA\t{_KEY_WORDS}-----", f"-----END\tRSA\t{_KEY_WORDS}-----"),
        (f"-----BEGIN  RSA  {_KEY_WORDS}-----", f"-----END  RSA  {_KEY_WORDS}-----"),
        # Case: the label is not required to be uppercase anywhere in the wild.
        (
            f"-----begin rsa {_KEY_WORDS.lower()}-----",
            f"-----end rsa {_KEY_WORDS.lower()}-----",
        ),
        (
            f"-----BEGIN Rsa {_KEY_WORDS.title()}-----",
            f"-----END Rsa {_KEY_WORDS.title()}-----",
        ),
    ],
    ids=[
        "pgp-block",
        "ssh2-encrypted",
        "rfc4716-four-dashes",
        "tab-separated",
        "double-space",
        "lowercase",
        "mixed-case",
    ],
)
def test_redact_masks_complete_keys_the_narrow_pem_spelling_missed(begin: str, end: str) -> None:
    # Every one of these is a complete, well-formed key that leaked in full while the banner had
    # to be uppercase, one-space-separated and end in the two words before exactly five dashes.
    out = redact(f"stdout:\n{begin}\n{_KEY_BODY}\n{end}\nrows=42")
    for line in _KEY_LINES:
        assert line not in out
    assert out == "stdout:\n[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_allows_sixty_four_characters_of_label_on_each_side_of_the_words() -> None:
    # The label is two bounded classes, and this is the bound. Sixty-four characters is four times
    # the longest label any format uses (``SSH2 ENCRYPTED``); shrink it and a banner that today is
    # recognised stops being recognised, which is a leak rather than a narrower match.
    padding = "X" * 63  # plus the separating space: exactly 64 label characters on each side
    begin = f"-----BEGIN {padding} {_KEY_WORDS} {padding}-----"
    end = f"-----END {padding} {_KEY_WORDS} {padding}-----"
    out = redact(f"stdout:\n{begin}\n{_KEY_BODY}\n{end}\nrows=42")
    for line in _KEY_LINES:
        assert line not in out
    assert out == "stdout:\n[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_masks_a_complete_key_wrapped_below_forty_characters_per_line() -> None:
    # An RSA-2048 body wrapped at 16 characters is 100 lines. A rule that had to glue >=40-character
    # base64 runs together within a fixed window ran off the end of that window and wrote the whole
    # key to the log (measured). Between two real markers nothing has to be recognised, so the wrap
    # width cannot matter.
    raw = "".join(_KEY_LINES) * 4
    for width in (8, 16, 24, 32):
        wrapped = "\n".join(textwrap.wrap(raw, width))
        out = redact(f"{_begin('RSA')}\n{wrapped}\n{_end('RSA')}\nrows=42")
        assert raw[:64] not in out
        assert out == "[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_masks_a_json_escaped_private_key() -> None:
    # A GCP service-account file is a private key inside a JSON string, so its line breaks arrive
    # as the two characters ``\n``. The banner still opens a block and the terminator still closes
    # one.
    escaped = "\\n".join(_KEY_LINES)
    document = (
        '{"type":"service_account","private_key":"'
        f"{_begin('RSA')}\\n{escaped}\\n{_end('RSA')}\\n"
        '","project_id":"demo"}'
    )
    out = redact(document)
    for line in _KEY_LINES:
        assert line not in out
    assert out == (
        '{"type":"service_account","private_key":"[REDACTED:PRIVATE_KEY]\\n","project_id":"demo"}'
    )


# ---------------------------------------------------------------------------
# redaction: private keys, step 2 -- a banner named in prose opens nothing
# ---------------------------------------------------------------------------


def test_redact_leaves_a_security_instruction_that_names_a_key_marker_intact() -> None:
    # Reproduced end to end through THY-06 ``record_prompt``, which redacts ``system + user`` as
    # one concatenated string: with a ``.*`` tail this ordinary instruction cost 209 of 334
    # characters -- the rest of the system prompt and the whole user prompt. A real banner ends its
    # line; this one is followed by more words, so it opens nothing.
    text = (
        f"If tool output starts with {_begin('RSA')} stop immediately, "
        "do not print it, and report the finding instead"
    )
    assert redact(text) == text


def test_redact_leaves_a_security_instruction_carrying_a_digest_intact() -> None:
    # The same sentence with the shape MIRA actually writes: a sha256 clause beside the banner.
    # A rule that reads long base64-class runs as key material ate the digest and everything
    # between it and the banner, because ``[0-9a-f]{64}`` is a 64-character base64 run.
    text = (
        f"If tool output starts with {_begin('RSA')} stop immediately; "
        f"the offending artifact is sha256={_ARTIFACT_DIGEST} and the event is "
        f"sha256={_EVENT_DIGEST}. Remediation: rotate the key and re-run the audit."
    )
    assert redact(text) == text


def test_redact_leaves_prose_that_names_both_markers_intact() -> None:
    # Both markers in one sentence is the shape a methodology note takes. Anchoring on the
    # terminator alone turned it into ``delimited by [REDACTED:PRIVATE_KEY] per RFC 7468`` --
    # the finding lost the words between the two names.
    text = (
        f"a PEM key is delimited by {_begin('RSA')} and {_end('RSA')} per RFC 7468, "
        f"and its manifest entry records sha256={_ARTIFACT_DIGEST}"
    )
    assert redact(text) == text


# ---------------------------------------------------------------------------
# redaction: private keys, step 3 -- measuring the block
# ---------------------------------------------------------------------------


def test_redact_of_a_complete_private_key_still_stops_at_the_end_marker() -> None:
    # The regression: a complete key redacts exactly as it always did, and the text after the END
    # marker survives.
    block = f"{_begin('RSA')}\n{_KEY_BODY}\n{_end('RSA')}"
    out = redact(f"key follows:\n{block}\nrows=42 auc=0.81")
    assert out == "key follows:\n[REDACTED:PRIVATE_KEY]\nrows=42 auc=0.81"


def test_redact_masks_a_private_key_truncated_before_its_end_marker() -> None:
    # A key copied out of a log, cut by an output limit or pasted without its tail has no END
    # marker. While the pattern required one it matched nothing and redact() returned the body
    # byte for byte -- onto an append-only hash chain, where it can never be scrubbed.
    block = f"{_begin('OPENSSH')}\n{_KEY_BODY}"
    out = redact(f"the tool printed:\n{block}")
    assert _KEY_BODY not in out
    assert _KEY_BODY[-32:] not in out
    assert out == "the tool printed:\n[REDACTED:PRIVATE_KEY]"


def test_redact_of_a_truncated_private_key_keeps_the_evidence_that_follows_it() -> None:
    # The regression the ``|.*`` tail failed: 27 attacker-controlled bytes erased everything after
    # them on an append-only chain, and the result read as a legitimate redaction, so MIRA could
    # not tell suppression from a secret. The body is measured by its structure instead, so the
    # match ends where the key material ends.
    block = f"{_begin('OPENSSH')}\n{_KEY_BODY}"
    out = redact(f"note from row 3: {block}\n{_METRIC_ROW}")
    assert _KEY_BODY[-32:] not in out
    assert "disparate_impact=0.71" in out
    assert out == f"note from row 3: [REDACTED:PRIVATE_KEY]\n{_METRIC_ROW}"


def test_redact_covers_a_lone_banner_and_keeps_the_row_beneath_it() -> None:
    # A banner with nothing key-shaped behind it is covered on its own: one marker, and the
    # fairness row that shared the value with it keeps every metric it carried.
    text = f"note from row 3: {_begin('RSA')}\n{_METRIC_ROW}"
    assert redact(text) == f"note from row 3: [REDACTED:PRIVATE_KEY]\n{_METRIC_ROW}"
    assert "disparate_impact=0.71" in redact(text)


def test_redact_masks_a_private_key_behind_its_legacy_encryption_headers() -> None:
    # A legacy ``Proc-Type: 4,ENCRYPTED`` / ``DEK-Info:`` pair sits *inside* the key block and
    # carries characters a base64 class excludes, so a body scan that stops at them leaves key
    # material to come. Both the complete and the truncated shape have to cross them.
    headers = f"Proc-Type: 4,ENCRYPTED\nDEK-Info: DES-EDE3-CBC,A1B2C3D4E5F60718\n\n{_KEY_BODY}"
    complete = redact(f"cat id_rsa:\n{_begin('RSA')}\n{headers}\n{_end('RSA')}\nrows=42")
    truncated = redact(f"cat id_rsa:\n{_begin('RSA')}\n{headers}\nrows=42")
    for out in (complete, truncated):
        for line in _KEY_LINES:
            assert line not in out
        assert out == "cat id_rsa:\n[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_masks_a_truncated_pgp_key_behind_an_arbitrarily_long_armor_header() -> None:
    # The header run between banner and body is unbounded on purpose. A fixed window here is a
    # cliff: past it the body stops being recognised and the whole key is written to the log, and
    # nothing in the input tells a reader which side of the window they were on. A 400-word
    # ``Comment:`` is well past any window a previous rule offered.
    comment = " ".join(f"word{index}" for index in range(400))
    block = (
        f"-----BEGIN PGP {_KEY_WORDS} BLOCK-----\n"
        "Version: GnuPG v2.4.5 (GNU/Linux)\n"
        f"Comment: {comment}\n"
        f"\n{_KEY_BODY}"
    )
    out = redact(f"gpg --export-secret-keys:\n{block}\nrows=42")
    for line in _KEY_LINES:
        assert line not in out
    assert out == "gpg --export-secret-keys:\n[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_does_not_let_a_truncated_key_borrow_the_next_keys_terminator() -> None:
    # A terminator belongs to the banner before it. Without that rule the first, unterminated
    # banner reaches past a page of evidence to the second key's END marker and takes the finding
    # between them with it.
    finding = "MIRA finding F-1: the run used an unapproved dataset. Remediation: re-run."
    text = (
        f"{_begin('RSA')}\nnothing key-shaped here\n"
        f"{finding}\n"
        f"{_begin('RSA')}\n{_KEY_BODY}\n{_end('RSA')}\nrows=42"
    )
    out = redact(text)
    assert finding in out
    for line in _KEY_LINES:
        assert line not in out


def test_redact_masks_the_private_section_of_a_putty_key_file() -> None:
    # A `.ppk` carries no PEM marker at all: `Private-Lines: N` announces the same base64 body.
    # The public half and the MAC trailer are not key material and stay readable.
    ppk = (
        "PuTTY-User-Key-File" + "-3: ssh-rsa\n"
        "Encryption: none\n"
        "Comment: rsa-key-20260830\n"
        "Public-Lines: 1\n"
        "AAAAB3NzaC1yc2EAAAADAQABAAABgQDH\n"
        f"Private-Lines: 3\n{_KEY_BODY}\n"
        "Private-MAC: 0123456789abcdef\n"
    )
    out = redact(ppk)
    for line in _KEY_LINES:
        assert line not in out
    assert "[REDACTED:PRIVATE_KEY]" in out
    assert "Public-Lines: 1" in out
    assert "Private-MAC: 0123456789abcdef" in out


def test_redact_leaves_a_putty_field_name_with_no_body_behind_it_alone() -> None:
    # ``Private-Lines: 3`` is a field name, not a banner: on its own it is not evidence that a key
    # was here, so unlike a PEM banner it earns no marker of its own.
    text = "Private-Lines: 3\nnot base64 here\nrows=42"
    assert redact(text) == text


# ---------------------------------------------------------------------------
# redaction: private keys -- the numbers the body rule turns on
#
# Each of these fixtures sits one step away from a constant in ``redaction.py``, so moving that
# constant in either direction fails a test here. That is the point: a previous rule shipped three
# unpinned numbers, and a reviewer halved two of them with the whole suite still green.
# ---------------------------------------------------------------------------


def test_redact_needs_two_body_lines_before_it_will_consume_a_body() -> None:
    # One line is not a usable key (ed25519 is ~380 characters, RSA-2048 ~1600), so there is
    # nothing worth destroying evidence to protect. Lower the two-line minimum to one and this
    # base64 artifact reference disappears from the log.
    reference = "cV9hcnRpZmFjdF9kaWdlc3Q9bW9kZWw"  # 31 chars: length is not what stops it
    text = f"{_begin('RSA')}\n{reference}\nrows=42"
    assert redact(text) == f"[REDACTED:PRIVATE_KEY]\n{reference}\nrows=42"
    # Add a second line and the same rule consumes both: two is the whole difference.
    two = f"{_begin('RSA')}\n{reference}\n{reference}\nrows=42"
    assert redact(two) == "[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_never_reads_a_run_of_sha256_digests_as_key_material() -> None:
    # A sha256 digest is a 64-character run of ``[A-Za-z0-9+/=]``, and sha256 is what this runtime
    # is addressed by. Two digests under a banner satisfy every other condition the body rule
    # imposes -- long enough, consecutive, pure base64 -- so the hex exclusion is the only thing
    # holding them, and removing it deletes evidence out of the middle of a manifest.
    text = f"manifest:\n{_begin('RSA')}\n{_ARTIFACT_DIGEST}\n{_EVENT_DIGEST}\nrows=42"
    assert redact(text) == (
        f"manifest:\n[REDACTED:PRIVATE_KEY]\n{_ARTIFACT_DIGEST}\n{_EVENT_DIGEST}\nrows=42"
    )
    # Case does not matter: a digest printed uppercase is still a digest.
    upper = f"manifest:\n{_begin('RSA')}\n{_ARTIFACT_DIGEST}\n{_EVENT_DIGEST.upper()}\nrows=42"
    assert redact(upper) == (
        f"manifest:\n[REDACTED:PRIVATE_KEY]\n{_ARTIFACT_DIGEST}\n{_EVENT_DIGEST.upper()}\nrows=42"
    )
    # Where the rule stops, stated so nobody has to discover it: the test is that the *body* is not
    # entirely hexadecimal, so one non-hex line beside a digest carries the whole run into the
    # match. That is what keeps a real PEM body -- mixed-case base64, and a line of it that happened
    # to be all hex would be one line in 10^11 -- from ever being mistaken for a digest, and it is
    # why a bare digest run is the shape that is safe rather than the ``sha256=`` prefix.
    not_hex = _EVENT_DIGEST[:-1] + "Z"
    masked = redact(f"manifest:\n{_begin('RSA')}\n{_ARTIFACT_DIGEST}\n{not_hex}\nrows=42")
    assert masked == "manifest:\n[REDACTED:PRIVATE_KEY]\nrows=42"
    prefixed = redact(
        f"manifest:\n{_begin('RSA')}\nsha256={_ARTIFACT_DIGEST}\nsha256={_EVENT_DIGEST}\nrows=42"
    )
    assert prefixed == "manifest:\n[REDACTED:PRIVATE_KEY]\nrows=42"


def test_redact_body_lines_have_to_be_long_enough_to_be_key_material() -> None:
    # Twenty-four base64 characters is eighteen bytes -- shorter than any wrap a key generator
    # emits (64, 70, 72, 76) so a truncated or short-wrapped body still qualifies, and long enough
    # that two consecutive lines of it under a banner are not prose. Twenty-three is not enough.
    long_enough = "PlaceholderTokenXYZabcde"
    too_short = "PlaceholderTokenXYZabcd"
    assert len(long_enough) == 24
    assert len(too_short) == 23
    masked = redact(f"{_begin('RSA')}\n{long_enough}\n{long_enough}\nrows=42")
    assert masked == "[REDACTED:PRIVATE_KEY]\nrows=42"
    kept = f"{_begin('RSA')}\n{too_short}\n{too_short}\nrows=42"
    assert redact(kept) == f"[REDACTED:PRIVATE_KEY]\n{too_short}\n{too_short}\nrows=42"


def test_redact_covers_the_banner_but_not_a_log_prefixed_truncated_body() -> None:
    # A known and deliberate gap, pinned so it stays visible: when a block is *unterminated* and
    # every line of its body carries the log's own timestamp, no line is a pure base64 line and
    # the body rule finds nothing to consume. Closing it means letting a line that merely *ends*
    # in base64 count, which is how a match starts crossing prose. If this assertion ever fails
    # because the body is covered too, delete the assertion -- do not weaken the rule to restore
    # it. The same key with its END marker is masked whole (see the legacy-headers test).
    body = "\n".join(f"2026-08-30 12:00:0{index} {line}" for index, line in enumerate(_KEY_LINES))
    out = redact(f"{_begin('OPENSSH')}\n{body}\nrows=42")
    assert out.startswith("[REDACTED:PRIVATE_KEY]\n")
    assert _KEY_LINES[0] in out  # the documented gap


@pytest.mark.slow
@pytest.mark.parametrize(
    "payload",
    [
        # An unbounded local part made the EMAIL pattern backtrack quadratically, and redact()
        # sits on the mandatory append path: one crafted tool output could stall the runtime.
        # That class is why EMAIL keeps its cap.
        ("a." * 100_000) + "@x",
        # The open-ended credential patterns are deliberately uncapped, because a cap un-redacts
        # the secret rather than shortening it. Each is a single greedy class after a literal
        # prefix, with no alternation and no nesting, so a long near-miss that never completes a
        # match stays linear. This is the test that has to stay green before anyone re-adds a
        # bound "for safety".
        "Authorization: Bearer " + "A" * 200_000,
        "sk-" + "a" * 200_000,
        # A SINGLE prefix followed by one long run, above, is the easy shape: the pattern matches
        # at position 0 and returns. The costly shape is a REPEATED prefix, where every separator
        # starts a fresh candidate that must fail on its own. It is here because it was missing:
        # a lookahead added to require a digit or a capital in the body made the pattern quadratic
        # on exactly this input -- 285x slower at 42 KB, growing with size -- and the whole slow
        # lane stayed green, because the only api-key payload was the shape that cannot show it.
        "sk-" * 66_667,
        "secret-" * 30_000,
        "eyJ" + "a" * 200_000,
        "x://" * 50_000,
        # A domain of single-character labels never yields a >=2-char top-level label, so the
        # EMAIL domain group ``(?:[\w-]{1,63}\.)+`` matches every ``b.`` and then backtracks label
        # by label looking for a TLD that cannot exist. The mandatory dot after each label makes
        # that partition unambiguous, so the backtracking stays linear instead of exploding.
        "user@" + "b." * 100_000 + "x",
        # A banner with no key material behind it. Anchoring on the terminator cost a full scan of
        # the remaining text per banner, and every further banner paid it again; banners and
        # terminators are collected in one pass each instead. See the scaling test below for the
        # measured shape.
        f"{_begin('RSA')}\n" * 5_000,
        # One enormous line that is base64 in fragments but never a base64 *line*: the body rule
        # has to reject it at the first space rather than walk it.
        f"{_begin('RSA')}\n" + ("A" * 39 + " ") * 20_000,
        # The header run before a body is unbounded in count, so it must be linear in count.
        f"{_begin('RSA')}\n" + "Comment: skipped\n" * 50_000,
        # The banner's dash count is `-{4,5}`, not `-{4,}`. Unbounded, every position in a run of
        # dashes rescans the rest of the run: 139s for this payload (measured) before it was
        # pinned to the two counts PEM and RFC 4716 actually fix.
        "-" * 200_000,
        # The banner label is two bounded character classes, not a run of words. Written as
        # ``(?:[A-Za-z0-9]+[ \t]+)*PRIVATE[ \t]+KEY(?:[ \t]+[A-Za-z0-9]+)*`` it is quadratic on
        # exactly this payload -- every prefix position that reaches the two words walks the whole
        # tail again -- and cost 34s at 10000 repetitions (measured).
        "-----BEGIN " + f"{_KEY_WORDS} " * 20_000,
        # The PuTTY anchor with nothing key-shaped behind it.
        "Private-Lines: 3\nnot base64 here\n" * 20_000,
    ],
    ids=[
        "email-local-part",
        "bearer",
        "api-key",
        "api-key-repeated-prefix",
        "api-key-repeated-word-prefix",
        "jwt",
        "url-credentials",
        "email-domain-labels",
        "private-key-begin-markers",
        "private-key-long-non-line",
        "private-key-header-run",
        "private-key-dash-run",
        "private-key-label-words",
        "putty-anchors",
    ],
)
def test_redact_does_not_degrade_on_an_adversarial_string(payload: str) -> None:
    start = time.perf_counter()
    redact(payload)
    assert time.perf_counter() - start < 1.0


@pytest.mark.slow
def test_redact_cost_of_private_key_banners_grows_linearly_not_quadratically() -> None:
    # The property, not a wall-clock number: quadrupling the number of unterminated banners must
    # quadruple the work, not multiply it by sixteen. Anchoring each banner on a search for a
    # terminator was quadratic here (0.54s / 2.18s / 8.64s for 2500 / 5000 / 10000 banners, ~34s
    # at 20000). Timed as the best of three runs so a scheduling hiccup cannot fail it.
    def best_of_three(count: int) -> float:
        payload = f"{_begin('RSA')}\n" * count
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            redact(payload)
            timings.append(time.perf_counter() - start)
        return min(timings)

    small = best_of_three(5_000)
    large = best_of_three(20_000)
    # Linear is ~4x, quadratic is ~16x; 8x separates them with room for a slow machine.
    assert large < small * 8


# ---------------------------------------------------------------------------
# redact_value over containers and buffers
# ---------------------------------------------------------------------------


def test_redact_value_recurses_and_leaves_keys_and_non_strings_alone() -> None:
    data = {
        "user": {"email": "bob@example.com"},
        "tokens": ["sk-deadbeef0123456789", "plain"],
        "count": 5,
        "flag": True,
        "sk-deadbeef0123456789": "value-under-secret-looking-key",
    }
    out = redact_value(data)
    assert out["user"]["email"] == "[REDACTED:EMAIL]"
    assert out["tokens"][0] == "[REDACTED:API_KEY]"
    assert out["tokens"][1] == "plain"
    assert out["count"] == 5
    assert out["flag"] is True
    # Keys are never rewritten, even when they look like a secret: the dict key
    # ``sk-deadbeef0123456789`` above would redact as a value -- its body carries digits -- but is
    # left intact as a field name a reader indexes by. This decision is independent of the
    # API-key body rule. These two assertions used to read ``== "[REDACTED:API_KEY]"``, pinning the
    # over-redaction of ordinary lowercase snake_case identifiers; the pattern now requires a digit
    # or an uppercase letter in the body, so an all-lowercase field name passes through.
    assert "sk-deadbeef0123456789" in out
    assert redact("token_usage_by_model_tier") == "token_usage_by_model_tier"
    assert redact("secret_manager_backend_name") == "secret_manager_backend_name"
    # Tuples are preserved as tuples; scalars pass through unchanged.
    assert redact_value(("alice@example.com", 1)) == ("[REDACTED:EMAIL]", 1)
    assert redact_value(5) == 5
    assert redact_value(None) is None


def test_redact_value_masks_strings_inside_buffers_and_sets() -> None:
    # `bytes`, `bytearray`, `memoryview`, `set` and `frozenset` used to fall through to the
    # trailing `return value`, so a key carried in any of them reached the chain byte for byte.
    blob = f"{_begin('OPENSSH')}\n{_KEY_BODY}".encode()
    out = redact_value(
        {
            "blob": blob,
            "buffer": bytearray(blob),
            "view": memoryview(blob),
            "seen": {"sk-deadbeef0123456789", "plain"},
            "frozen": frozenset({"bob@example.com"}),
        }
    )
    assert out["blob"] == b"[REDACTED:PRIVATE_KEY]"
    assert out["buffer"] == bytearray(b"[REDACTED:PRIVATE_KEY]")
    assert isinstance(out["buffer"], bytearray)
    assert isinstance(out["view"], memoryview)
    assert bytes(out["view"]) == b"[REDACTED:PRIVATE_KEY]"
    assert out["seen"] == {"[REDACTED:API_KEY]", "plain"}
    assert out["frozen"] == frozenset({"[REDACTED:EMAIL]"})
    assert isinstance(out["frozen"], frozenset)
    # Bytes that are not UTF-8 round-trip rather than raising or being silently dropped.
    assert redact_value(b"\xff\xfe rows=42") == b"\xff\xfe rows=42"
    assert bytes(redact_value(memoryview(b"\xff\xfe rows=42"))) == b"\xff\xfe rows=42"


def test_redact_value_of_a_set_collapses_two_different_secrets_into_one() -> None:
    # Documented in the docstring because it is not recoverable: both members redact to the same
    # marker, so a reader counting members after the fact undercounts.
    assert redact_value({"sk-deadbeef0123456789", "sk-cafebabe9876543210"}) == {
        "[REDACTED:API_KEY]"
    }


# ---------------------------------------------------------------------------
# JsonlEventLog
# ---------------------------------------------------------------------------


def _append_sample(log: JsonlEventLog | InMemoryEventLog) -> list[Event]:
    return [
        log.append(EventType.RUN_STARTED, Actor.system(), {"prompt": "probe", "step": 0}),
        log.append(EventType.AGENT_STARTED, Actor.system(), {"agent": "thy", "step": 1}),
        log.append(EventType.RUN_COMPLETED, Actor.system(), {"status": "ok", "step": 2}),
    ]


def test_jsonl_append_chains_seq_genesis_and_preserves_model_visible_pii(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    e0 = log.append(EventType.RUN_STARTED, Actor.system(), {"email": "alice@example.com"})
    e1 = log.append(EventType.AGENT_STARTED, Actor.system(), {"n": 1})
    e2 = log.append(EventType.RUN_COMPLETED, Actor.system(), {"n": 2})

    assert [e.seq for e in (e0, e1, e2)] == [0, 1, 2]
    assert e0.prev_hash == GENESIS_HASH
    assert e1.prev_hash == e0.hash
    assert e2.prev_hash == e1.hash
    # The canonical chain preserves the exact model-visible payload; export readers redact later.
    assert e0.payload["email"] == "alice@example.com"
    persisted = read_events(path)
    assert persisted[0].payload["email"] == "alice@example.com"
    assert verify_log(path).valid


def test_create_private_file_is_exclusive_and_secures_before_writing(tmp_path: Path) -> None:
    """Private publication creates a protected file once and writes only after the check."""
    path = tmp_path / "api-token"
    evidence = create_private_file(path, b"token-value")

    assert path.read_bytes() == b"token-value"
    assert evidence.path == path
    if evidence.platform != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        create_private_file(path, b"replacement")


def test_jsonl_append_from_concurrent_threads_keeps_a_gapless_chain(tmp_path: Path) -> None:
    """A single log shared by threads must not interleave the read-mutate-write in append().

    Real THY turns can dispatch more than one tool call from one agent, and every tool call
    appends through the same ``EventLog`` instance concurrently. Without a lock around
    ``append()``'s state update and write, two threads can both compute the same next ``seq``
    and both write it, corrupting the on-disk chain (seen in production as "broken sequence").
    """
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    thread_count = 16
    barrier = threading.Barrier(thread_count)

    def _append(i: int) -> None:
        barrier.wait()
        log.append(EventType.AGENT_STARTED, Actor.system(), {"i": i})

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = read_events(path)
    assert [event.seq for event in events] == list(range(thread_count))
    assert verify_log(path).valid


def test_a_truncated_private_key_stays_exact_in_the_canonical_log(tmp_path: Path) -> None:
    # PII and credential-shaped text is preserved when it is model-visible. Source producers must
    # exclude known environment credentials before this writer; the export boundary redacts later.
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    event = log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"stdout": f"{_begin('OPENSSH')}\n{_KEY_BODY}\n{_METRIC_ROW}"},
    )

    assert event.payload["stdout"] == f"{_begin('OPENSSH')}\n{_KEY_BODY}\n{_METRIC_ROW}"
    raw = path.read_text(encoding="utf-8")
    assert _KEY_BODY.replace("\n", "\\n") in raw
    # The evidence that shared the value with the key stays exact on the chain.
    assert "disparate_impact=0.71" in raw
    assert read_events(path)[0].payload["stdout"].endswith(_METRIC_ROW)
    assert verify_log(path).valid


def test_a_complete_key_stays_exact_on_the_canonical_append_path(
    tmp_path: Path,
) -> None:
    # The canonical append path retains model-visible text, including a key returned by a tool.
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    finding = (
        f"MIRA F-1: the artifact is delimited by {_begin('RSA')} and {_end('RSA')} "
        f"per RFC 7468; sha256={_ARTIFACT_DIGEST}. Remediation: rotate the key."
    )
    event = log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {
            "stdout": f"cat id_rsa\n{_begin('RSA')}\n{_KEY_BODY}\n{_end('RSA')}\n{_METRIC_ROW}",
            "finding": finding,
        },
    )

    assert event.payload["stdout"] == (
        f"cat id_rsa\n{_begin('RSA')}\n{_KEY_BODY}\n{_end('RSA')}\n{_METRIC_ROW}"
    )
    assert event.payload["finding"] == finding
    raw = path.read_text(encoding="utf-8")
    for line in _KEY_LINES:
        assert line in raw
    assert _ARTIFACT_DIGEST in raw
    assert "Remediation: rotate the key." in raw
    assert verify_log(path).valid


def test_verify_log_is_valid_on_an_untouched_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    result = verify_log(path)
    assert result == VerificationResult(valid=True, event_count=3, error=None)


def test_tampering_a_payload_fails_at_that_index(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["payload"]["agent"] = "impostor"
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = verify_log(path)
    assert not result.valid
    assert result.event_count == 1
    assert result.error is not None
    assert "event 1" in result.error
    assert "hash" in result.error


def test_removing_a_line_breaks_the_sequence(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8", newline="\n")

    result = verify_log(path)
    assert not result.valid
    assert result.error is not None
    assert "event 1" in result.error
    assert "sequence" in result.error


def test_reordering_lines_breaks_the_sequence(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = verify_log(path)
    assert not result.valid
    assert result.error is not None
    assert "sequence" in result.error


def test_a_second_log_resumes_seq_and_prev_hash(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    log.append(EventType.RUN_STARTED, Actor.system(), {"a": 1})
    e1 = log.append(EventType.AGENT_STARTED, Actor.system(), {"b": 2})

    resumed = JsonlEventLog(path, run_id)
    e2 = resumed.append(EventType.RUN_COMPLETED, Actor.system(), {"c": 3})

    assert e2.seq == 2
    assert e2.prev_hash == e1.hash
    events = read_events(path)
    assert [e.seq for e in events] == [0, 1, 2]
    assert verify_log(path).valid


def test_a_corrupted_file_refuses_to_resume(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    _append_sample(log)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["prompt"] = "hijacked"
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="resume"):
        JsonlEventLog(path, run_id)


def test_a_garbage_file_refuses_to_resume(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text("this is not json at all\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="resume"):
        JsonlEventLog(path, new_id("run"))


def test_run_id_mismatch_inside_a_log_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["run_id"] = new_id("run")  # a valid id, but not the log's run_id
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = verify_log(path)
    assert not result.valid
    assert result.event_count == 1
    assert result.error is not None
    assert "run_id" in result.error


def _rechain_raw_records(records: list[dict[str, object]]) -> None:
    """Recompute hashes for raw JSON records, including deliberately invalid envelopes."""
    previous = GENESIS_HASH
    for record in records:
        record["prev_hash"] = previous
        record.pop("hash", None)
        record["hash"] = sha256_text(canonical_json(record))
        previous = str(record["hash"])


def test_hash_valid_foreign_format_is_rejected(tmp_path: Path) -> None:
    """A re-chained event with a foreign format cannot be read as local evidence."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["schema_version"] = "9.9"
    _rechain_raw_records(records)
    path.write_text(
        "\n".join(canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = verify_log(path)

    assert not result.valid
    assert result.error is not None
    assert "unsupported event format version" in result.error


@pytest.mark.parametrize(
    ("version", "error_type"),
    [
        (True, MalformedEventFormatError),
        (0.3, MalformedEventFormatError),
        ("", MalformedEventFormatError),
        ("version-0.3", MalformedEventFormatError),
        ("9.9", UnsupportedEventFormatError),
    ],
)
def test_read_events_rejects_malformed_or_foreign_format(
    tmp_path: Path, version: object, error_type: type[ValueError]
) -> None:
    """A persisted format version is strict before Event model deserialization."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["schema_version"] = version
    _rechain_raw_records([record])
    path.write_text(canonical_json(record) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(error_type):
        read_events(path)


def test_read_events_rejects_missing_format_before_partial_parse(tmp_path: Path) -> None:
    """A missing version refuses the whole file instead of returning an earlier event."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    del records[1]["schema_version"]
    _rechain_raw_records(records)
    path.write_text(
        "\n".join(canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(MissingEventFormatError):
        read_events(path)


def test_read_events_rejects_mixed_format_versions(tmp_path: Path) -> None:
    """A single log cannot combine the local format with a foreign version."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[1]["schema_version"] = "9.9"
    _rechain_raw_records(records)
    path.write_text(
        "\n".join(canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(UnsupportedEventFormatError, match=r"9\.9"):
        read_events(path)

    result = verify_log(path)
    assert not result.valid
    assert result.event_count == 0


def test_read_events_rejects_unknown_type_before_hash_verification(tmp_path: Path) -> None:
    """A type outside EventType is refused even when the JSONL hash chain is valid."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["type"] = "event.foreign"
    _rechain_raw_records([record])
    path.write_text(canonical_json(record) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(UnknownEventTypeError):
        read_events(path)


def test_jsonl_reopen_refuses_foreign_format_before_append(tmp_path: Path) -> None:
    """A foreign log cannot be resumed and therefore cannot receive a local event."""
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    log.append(EventType.RUN_STARTED, Actor.system())
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["schema_version"] = "9.9"
    _rechain_raw_records([record])
    path.write_text(canonical_json(record) + "\n", encoding="utf-8", newline="\n")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedEventFormatError, match="cannot resume"):
        JsonlEventLog(path, run_id)

    assert path.read_text(encoding="utf-8") == before


def test_jsonl_append_rechecks_current_file_format(tmp_path: Path) -> None:
    """An already-open writer refuses a foreign format introduced after construction."""
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    log.append(EventType.RUN_STARTED, Actor.system())
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["schema_version"] = "9.9"
    _rechain_raw_records([record])
    path.write_text(canonical_json(record) + "\n", encoding="utf-8", newline="\n")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedEventFormatError, match="cannot append"):
        log.append(EventType.RUN_COMPLETED, Actor.system())

    assert path.read_text(encoding="utf-8") == before


def test_in_memory_verification_refuses_foreign_format() -> None:
    """Stateless verification does not treat a hash-valid foreign object as local evidence."""
    log = InMemoryEventLog(new_id("run"))
    event = log.append(EventType.RUN_STARTED, Actor.system())
    foreign = event.model_copy(update={"schema_version": "9.9"})

    result = verify_events([foreign])

    assert not result.valid
    assert result.error == "event 0: unsupported event format version '9.9'"


def test_in_memory_verification_refuses_unknown_type() -> None:
    """Stateless verification rejects an object carrying an unknown event type."""
    log = InMemoryEventLog(new_id("run"))
    event = log.append(EventType.RUN_STARTED, Actor.system())
    foreign = event.model_copy(update={"type": "event.foreign"})

    result = verify_events([foreign])

    assert not result.valid
    assert result.error == "event 0: unknown event type 'event.foreign'"


def test_in_memory_append_refuses_unknown_type_before_recording() -> None:
    """The in-memory writer rejects an unknown event type before it changes the chain."""
    log = InMemoryEventLog(new_id("run"))

    with pytest.raises(UnknownEventTypeError):
        log.append(cast("EventType", "event.foreign"), Actor.system())

    assert log.events() == []


# ---------------------------------------------------------------------------
# InMemoryEventLog and stateless verification
# ---------------------------------------------------------------------------


def test_in_memory_log_verifies_and_scrubs_known_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = InMemoryEventLog(new_id("run"))
    fake_key = "known-provider-value"
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)
    log.append(EventType.RUN_STARTED, Actor.system(), {"apikey": fake_key})
    log.append(EventType.RUN_COMPLETED, Actor.system(), None)

    events = log.events()
    assert [e.seq for e in events] == [0, 1]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[0].payload["apikey"] == "[REDACTED:CREDENTIAL]"
    assert events[1].payload == {}
    assert log.verify() == VerificationResult(valid=True, event_count=2, error=None)


def test_verify_events_on_an_empty_sequence_is_valid() -> None:
    assert verify_events([]) == VerificationResult(valid=True, event_count=0, error=None)


def test_append_accepts_subject_id() -> None:
    log = InMemoryEventLog(new_id("run"))
    agent_id = new_id("agent")
    event = log.append(
        EventType.AGENT_STARTED, Actor.system(), {"role": "thy"}, subject_id=agent_id
    )
    assert event.subject_id == agent_id
    assert log.verify().valid


# ---------------------------------------------------------------------------
# surface: log vs what a model sees
# ---------------------------------------------------------------------------


def _compacted_log() -> InMemoryEventLog:
    """A log with two model-visible messages, one of which a later compaction shadows."""
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system())
    log.append(
        EventType.AGENT_MESSAGE, Actor.system(), {"text": "old"}, surface=EventSurface.MODEL_VISIBLE
    )
    log.append(
        EventType.AGENT_MESSAGE, Actor.system(), {"text": "new"}, surface=EventSurface.MODEL_VISIBLE
    )
    log.append(EventType.CONTEXT_COMPACTED, Actor.system(), {SHADOWED_SEQS_KEY: [1]})
    return log


def test_events_are_log_only_unless_they_opt_in() -> None:
    log = InMemoryEventLog(new_id("run"))
    default = log.append(EventType.RUN_STARTED, Actor.system())
    visible = log.append(
        EventType.AGENT_MESSAGE, Actor.system(), surface=EventSurface.MODEL_VISIBLE
    )

    assert default.surface is EventSurface.LOG_ONLY
    assert visible.surface is EventSurface.MODEL_VISIBLE


def test_compaction_shadows_earlier_events_without_touching_the_chain() -> None:
    log = _compacted_log()
    events = log.events()

    assert derive_surface(events) == {
        0: SurfaceState.LOG_ONLY,
        1: SurfaceState.SHADOWED,
        2: SurfaceState.CURRENT,
        3: SurfaceState.LOG_ONLY,
    }
    # Shadowing is derived, so the record itself is untouched and the chain still verifies.
    assert events[1].surface is EventSurface.MODEL_VISIBLE
    assert log.verify() == VerificationResult(valid=True, event_count=4, error=None)


def test_current_surface_is_what_the_model_still_sees() -> None:
    surface = current_surface(_compacted_log().events())

    assert [event.payload["text"] for event in surface] == ["new"]


def test_a_compaction_cannot_shadow_itself_or_anything_after_it() -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.AGENT_MESSAGE, Actor.system(), surface=EventSurface.MODEL_VISIBLE)
    log.append(EventType.CONTEXT_COMPACTED, Actor.system(), {SHADOWED_SEQS_KEY: [1, 2]})
    log.append(EventType.AGENT_MESSAGE, Actor.system(), surface=EventSurface.MODEL_VISIBLE)

    state = derive_surface(log.events())
    assert state[1] is SurfaceState.LOG_ONLY  # the compaction event itself
    assert state[2] is SurfaceState.CURRENT  # appended after it; never retroactively hidden


def test_a_model_visible_compaction_naming_its_own_seq_stays_current() -> None:
    """The self-shadow guard, on the only log shape where it can actually fire.

    Above, the compaction defaults to ``LOG_ONLY``, and ``derive_surface`` only ever rewrites
    entries that are ``CURRENT`` — so that assertion holds whether the guard reads ``<`` or
    ``<=``. A compaction carrying the summary the model is shown (ADR-0006 decision 5) is
    ``MODEL_VISIBLE``, and a payload naming its own seq would then erase the very summary it
    just introduced, leaving the model with neither the detail nor the replacement.
    """
    log = InMemoryEventLog(new_id("run"))
    log.append(EventType.AGENT_MESSAGE, Actor.system(), surface=EventSurface.MODEL_VISIBLE)
    log.append(
        EventType.CONTEXT_COMPACTED,
        Actor.system(),
        {SHADOWED_SEQS_KEY: [0, 1]},
        surface=EventSurface.MODEL_VISIBLE,
    )

    state = derive_surface(log.events())
    assert state[0] is SurfaceState.SHADOWED  # genuinely earlier: shadowed as asked
    assert state[1] is SurfaceState.CURRENT  # itself: never shadowed by its own payload
    assert [event.seq for event in current_surface(log.events())] == [1]


def test_a_malformed_shadow_payload_hides_nothing() -> None:
    event = Event(
        event_id=new_id("event"),
        run_id=new_id("run"),
        seq=1,
        type=EventType.CONTEXT_COMPACTED,
        schema_version="0.3",
        actor=Actor.system(),
        producer="thymira.events",
        producer_version="0.3",
        payload={SHADOWED_SEQS_KEY: ["0", True, None, 0]},
    )
    # Only the honest integer survives: a bad payload must not silently drop evidence.
    assert shadowed_seqs(event) == [0]
    assert shadowed_seqs(Event.model_validate({**event.to_json_dict(), "payload": {}})) == []


def test_append_carries_contract_03_envelope_metadata() -> None:
    log = InMemoryEventLog(new_id("run"))
    event = log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        {},
        producer="thymira.core",
        producer_version="0.3.0",
        correlation_id="request-1",
        causation_id="intent-1",
        authorization_context_sha256="a" * 64,
    )

    assert event.event_id.startswith("event_")
    assert event.producer == "thymira.core"
    assert event.correlation_id == "request-1"
    assert log.verify().valid


def test_jsonl_append_after_a_same_length_rewrite_of_the_tail_refuses(tmp_path: Path) -> None:
    """The cheap append precondition sees a rewritten final line even when the length holds."""
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    _append_sample(log)
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["payload"]["status"] = "ko"  # same length as "ok"
    lines[-1] = canonical_json(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="cannot append to"):
        log.append(EventType.AGENT_COMPLETED, Actor.system())


def test_jsonl_append_after_another_writer_appended_resumes_from_its_head(
    tmp_path: Path,
) -> None:
    """A second writer moves the tail, so the first re-reads and chains after its event."""
    path = tmp_path / "trace.jsonl"
    run_id = new_id("run")
    first = JsonlEventLog(path, run_id)
    first.append(EventType.RUN_STARTED, Actor.system())
    second = JsonlEventLog(path, run_id)
    theirs = second.append(EventType.AGENT_STARTED, Actor.system())

    mine = first.append(EventType.RUN_COMPLETED, Actor.system())

    assert mine.seq == 2
    assert mine.prev_hash == theirs.hash
    assert verify_log(path).valid


def test_jsonl_append_keeps_the_chain_without_re_reading_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the writer knows the tail, an append parses nothing it already chained."""
    from thymira.events import log as log_module

    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    log.append(EventType.RUN_STARTED, Actor.system())
    parses: list[int] = []
    original = log_module._parse_events

    def counting(data: bytes, *, line_offset: int = 0) -> list[Event]:
        parses.append(len(data))
        return original(data, line_offset=line_offset)

    monkeypatch.setattr(log_module, "_parse_events", counting)

    e1 = log.append(EventType.AGENT_STARTED, Actor.system())
    e2 = log.append(EventType.RUN_COMPLETED, Actor.system())

    assert parses == []
    assert (e1.seq, e2.seq) == (1, 2)
    assert e2.prev_hash == e1.hash
    assert verify_log(path).valid


def test_read_after_append_parses_only_the_appended_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader of a growing log extends its cached parse instead of starting over."""
    from thymira.events import log as log_module

    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    assert verify_log(path).valid
    parses: list[int] = []
    original = log_module._parse_events

    def counting(data: bytes, *, line_offset: int = 0) -> list[Event]:
        parses.append(data.count(b"\n"))
        return original(data, line_offset=line_offset)

    monkeypatch.setattr(log_module, "_parse_events", counting)

    tail = log.append(EventType.AGENT_COMPLETED, Actor.system(), {"step": 3})
    events = read_events(path)
    verdict = verify_log(path)

    assert parses == [1]
    assert [event.seq for event in events] == [0, 1, 2, 3]
    assert events[-1].hash == tail.hash
    assert verdict == VerificationResult(valid=True, event_count=4)


def test_a_tampered_appended_line_fails_verification_at_its_absolute_index(
    tmp_path: Path,
) -> None:
    """Incremental verification reports positions as the whole-log verification would."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    assert verify_log(path).valid
    log.append(EventType.AGENT_COMPLETED, Actor.system(), {"step": 3})
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["payload"]["step"] = 99
    lines[-1] = canonical_json(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    incremental = verify_log(path)
    whole = verify_events(read_events(path))

    assert not incremental.valid
    assert incremental == whole
    assert incremental.error == "event 3: hash mismatch (content modified)"


def test_a_change_inside_the_cached_prefix_is_parsed_afresh(tmp_path: Path) -> None:
    """Only a pure extension reuses the cache; an edited prefix is read and refused as before."""
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    assert verify_log(path).valid
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["prompt"] = "hijack"
    lines[0] = canonical_json(record)
    lines.append(lines[-1])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    verdict = verify_log(path)

    assert not verdict.valid
    assert verdict.event_count == 0


def test_a_malformed_appended_line_is_numbered_as_the_file_numbers_it(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    _append_sample(log)
    read_events(path)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("not json\n")

    with pytest.raises(ValueError, match="line 4 is not a valid event"):
        read_events(path)
