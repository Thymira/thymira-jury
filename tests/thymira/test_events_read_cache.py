"""Unit tests for event-log readers whose parse is reused while a file's bytes are unchanged.

Every test drives the public readers (``read_events``, ``verify_log``, ``JsonlEventLog``) over a
real log under ``tmp_path`` and checks that a change to the file is always seen, never masked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.events import JsonlEventLog, read_events, verify_log
from thymira.schemas import Actor, EventType, new_id

if TYPE_CHECKING:
    from pathlib import Path


def _log(tmp_path: Path) -> tuple[JsonlEventLog, Path]:
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path, new_id("run"))
    for note in ("alpha", "bravo", "charlie"):
        log.append(EventType.AGENT_MESSAGE, Actor.system(), {"note": note})
    return log, path


def test_read_events_returns_a_list_the_caller_owns(tmp_path: Path) -> None:
    _, path = _log(tmp_path)
    read_events(path).clear()

    assert len(read_events(path)) == 3


def test_read_events_sees_an_event_appended_after_an_earlier_read(tmp_path: Path) -> None:
    log, path = _log(tmp_path)
    read_events(path)

    log.append(EventType.AGENT_MESSAGE, Actor.system(), {"note": "delta"})

    notes = [event.payload["note"] for event in read_events(path)]
    assert notes == ["alpha", "bravo", "charlie", "delta"]


def test_verify_log_detects_tampering_after_a_verified_read(tmp_path: Path) -> None:
    _, path = _log(tmp_path)
    assert verify_log(path).valid is True

    path.write_bytes(path.read_bytes().replace(b'"bravo"', b'"omega"'))

    assert verify_log(path).valid is False


def test_reopening_a_log_refuses_tampering_after_a_cached_read(tmp_path: Path) -> None:
    log, path = _log(tmp_path)
    read_events(path)

    path.write_bytes(path.read_bytes().replace(b'"bravo"', b'"omega"'))

    with pytest.raises(ValueError, match="cannot resume"):
        JsonlEventLog(path, log.run_id)


def test_read_events_refuses_a_malformed_line_after_a_valid_read(tmp_path: Path) -> None:
    _, path = _log(tmp_path)
    read_events(path)

    with path.open("ab") as handle:
        handle.write(b"{not json}\n")

    with pytest.raises(ValueError, match="line 4"):
        read_events(path)
