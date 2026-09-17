"""The CLI entry point forces UTF-8 stdio so rendered Run content can never crash it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thymira.cli.__main__ import _force_utf8_console

if TYPE_CHECKING:
    import pytest


@dataclass
class _ReconfigurableStream:
    """A minimal double for a `TextIOWrapper`-backed stream, recording `reconfigure` calls."""

    calls: list[dict[str, str]] = field(default_factory=list)

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


class _PlainStream:
    """A stream with no `reconfigure` method, the way some test harnesses swap stdio."""


def test_force_utf8_console_reconfigures_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _ReconfigurableStream(), _ReconfigurableStream()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    _force_utf8_console()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_force_utf8_console_tolerates_a_stream_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdout", _PlainStream())
    monkeypatch.setattr("sys.stderr", _PlainStream())

    _force_utf8_console()  # must not raise
