"""Unit tests pinning that redacting a whole value applies exactly what per-string redaction does.

The process environment is read once per call instead of once per string. These tests prove the
result did not change, and that every call still sees the environment as it is when it starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from thymira.events import (
    redact,
    redact_export,
    redact_value,
    scrub_credentials,
    scrub_credentials_value,
)

if TYPE_CHECKING:
    import pytest

CREDENTIAL = "correcthorsebatterystaple42"


def _per_string_export(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key): _per_string_export(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_per_string_export(item) for item in value]
    return value


def test_redact_export_matches_redacting_every_string_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_TEST_API_KEY", CREDENTIAL)
    payload = {
        "prompt": f"use {CREDENTIAL} and write to bob@example.com",
        "messages": [{"content": CREDENTIAL}, "plain", 1, 2.5, None, True],
        f"key {CREDENTIAL}": ["x"],
        "nested": {"deep": (f"x {CREDENTIAL} y",)},
    }

    redacted = redact_export(payload)

    assert redacted == _per_string_export(payload)
    assert CREDENTIAL not in repr(redacted)


def test_redact_export_reads_a_credential_exported_after_an_earlier_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THYMIRA_LATE_SECRET", raising=False)
    payload = {"note": CREDENTIAL}
    first = redact_export(payload)

    monkeypatch.setenv("THYMIRA_LATE_SECRET", CREDENTIAL)
    second = redact_export(payload)

    assert first == {"note": CREDENTIAL}
    assert second == {"note": "[REDACTED:CREDENTIAL]"}


def test_redact_value_matches_redacting_every_string_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_TEST_TOKEN", CREDENTIAL)
    value = {
        "text": f"a {CREDENTIAL}",
        "raw": f"b {CREDENTIAL}".encode(),
        "items": [f"c {CREDENTIAL}"],
        "members": frozenset({f"d {CREDENTIAL}"}),
    }

    assert redact_value(value) == {
        "text": redact(f"a {CREDENTIAL}"),
        "raw": redact(f"b {CREDENTIAL}").encode(),
        "items": [redact(f"c {CREDENTIAL}")],
        "members": frozenset({redact(f"d {CREDENTIAL}")}),
    }


def test_scrub_credentials_value_matches_scrubbing_every_string_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_TEST_PASSWORD", CREDENTIAL)
    value = {f"k {CREDENTIAL}": [f"v {CREDENTIAL}", (CREDENTIAL,)], "n": 3}

    assert scrub_credentials_value(value) == {
        scrub_credentials(f"k {CREDENTIAL}"): [
            scrub_credentials(f"v {CREDENTIAL}"),
            (
                scrub_credentials(
                    CREDENTIAL,
                ),
            ),
        ],
        "n": 3,
    }
