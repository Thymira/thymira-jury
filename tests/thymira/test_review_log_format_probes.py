"""Independent format-boundary probes for the F1.2 review."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.events import (
    GENESIS_HASH,
    JsonlEventLog,
    MalformedEventFormatError,
    MissingEventFormatError,
    UnknownEventTypeError,
    UnsupportedEventFormatError,
    canonical_json,
    deserialize_event,
    sha256_text,
    verify_events,
)
from thymira.schemas import Actor, EventSurface, EventType, new_id
from thymira.state.postgres import events as pg_events

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", None, MalformedEventFormatError),
        ("schema_version", True, MalformedEventFormatError),
        ("schema_version", 3, MalformedEventFormatError),
        ("schema_version", "03", MalformedEventFormatError),
        ("schema_version", "0.4", UnsupportedEventFormatError),
        ("type", "event.foreign", UnknownEventTypeError),
    ],
)
def test_review_deserializer_refuses_each_serialized_foreign_boundary(
    tmp_path: Path, field: str, value: object, error: type[ValueError]
) -> None:
    """No version default or type coercion occurs before Event construction."""
    del tmp_path
    event = pg_events.build_event(
        new_id("run"),
        0,
        "0" * 64,
        EventType.RUN_STARTED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    record = event.to_json_dict()
    if field == "schema_version" and value is None:
        del record[field]
        error = MissingEventFormatError
    else:
        record[field] = value

    with pytest.raises(error):
        deserialize_event(record)


def test_review_in_memory_refuses_a_foreign_object_before_hashing() -> None:
    """A model_copy bypass cannot turn a re-hashed foreign Event into local evidence."""
    event = pg_events.build_event(
        new_id("run"),
        0,
        "0" * 64,
        EventType.RUN_STARTED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    foreign = event.model_copy(update={"schema_version": "9.9"})

    result = verify_events([foreign])

    assert not result.valid
    assert result.event_count == 0
    assert result.error == "event 0: unsupported event format version '9.9'"


def test_review_jsonl_reopen_and_open_append_refuse_a_rehashed_mixed_file(tmp_path: Path) -> None:
    """A foreign line refuses before either resume or an existing writer can extend it."""
    path = tmp_path / "events.jsonl"
    run_id = new_id("run")
    log = JsonlEventLog(path, run_id)
    log.append(EventType.RUN_STARTED, Actor.system())
    log.append(EventType.RUN_COMPLETED, Actor.system())
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[1]["schema_version"] = "0.4"
    previous = "0" * 64
    for record in records:
        record["prev_hash"] = previous
        record.pop("hash", None)
        record["hash"] = sha256_text(canonical_json(record))
        previous = record["hash"]
    path.write_text(
        "\n".join(canonical_json(record) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    before = path.read_text(encoding="utf-8")

    with pytest.raises(UnsupportedEventFormatError):
        JsonlEventLog(path, run_id)
    with pytest.raises(UnsupportedEventFormatError):
        log.append(EventType.RUN_STARTED, Actor.system())

    assert path.read_text(encoding="utf-8") == before


@dataclass
class _Result:
    """Minimal SQLAlchemy result double for the two PostgreSQL reader paths."""

    documents: tuple[str, ...]

    def scalar_one_or_none(self) -> str | None:
        return self.documents[-1] if self.documents else None

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[str]:
        return list(self.documents)


@dataclass
class _Connection:
    """Connection double that returns a stable persisted document sequence."""

    document: str | tuple[str, ...]
    executed: list[Any] = field(default_factory=list)

    def execute(self, statement: Any, *parameters: Any) -> _Result:
        self.executed.append(statement)
        del parameters
        documents = (self.document,) if isinstance(self.document, str) else self.document
        return _Result(documents)


def test_review_pg_read_refuses_a_foreign_serialized_document() -> None:
    """The non-DB PostgreSQL read seam reaches the global deserializer."""
    event = pg_events.build_event(
        new_id("run"),
        0,
        "0" * 64,
        EventType.RUN_STARTED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    record = event.to_json_dict()
    record["schema_version"] = "9.9"

    with pytest.raises(UnsupportedEventFormatError):
        pg_events._read_events(cast("Any", _Connection(canonical_json(record))), event.run_id)


def _rechain_records(records: list[dict[str, Any]]) -> None:
    """Recompute raw hashes so a modified serialized prefix remains hash-valid."""
    previous = GENESIS_HASH
    for record in records:
        record["prev_hash"] = previous
        record.pop("hash", None)
        record["hash"] = sha256_text(canonical_json(record))
        previous = record["hash"]


@pytest.mark.parametrize(
    ("version_change", "error"),
    [
        ("foreign", UnsupportedEventFormatError),
        ("malformed", MalformedEventFormatError),
        ("missing", MissingEventFormatError),
    ],
)
def test_review_pg_append_validates_the_full_persisted_prefix_before_insert(
    version_change: str, error: type[ValueError], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad earlier row is refused while the append transaction is still locked."""
    run_id = new_id("run")
    first = pg_events.build_event(
        run_id,
        0,
        GENESIS_HASH,
        EventType.RUN_STARTED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    second = pg_events.build_event(
        run_id,
        1,
        first.hash or GENESIS_HASH,
        EventType.RUN_COMPLETED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    records = [first.to_json_dict(), second.to_json_dict()]
    if version_change == "foreign":
        records[0]["schema_version"] = "9.9"
    elif version_change == "malformed":
        records[0]["schema_version"] = True
    else:
        del records[0]["schema_version"]
    _rechain_records(records)

    inserted: list[object] = []
    monkeypatch.setattr(pg_events, "_insert_event", lambda _conn, event: inserted.append(event))
    connection = _Connection(tuple(canonical_json(record) for record in records))

    with pytest.raises(error):
        pg_events.append_event_on(
            cast("Any", connection), run_id, EventType.RUN_STARTED, Actor.system()
        )

    assert len(connection.executed) == 2  # advisory lock, then the full prefix read
    assert inserted == []


def test_review_pg_append_refuses_a_rechained_invalid_sequence_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-chained prefix with a broken sequence cannot be extended from its head."""
    run_id = new_id("run")
    first = pg_events.build_event(
        run_id,
        0,
        GENESIS_HASH,
        EventType.RUN_STARTED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    second = pg_events.build_event(
        run_id,
        1,
        first.hash or GENESIS_HASH,
        EventType.RUN_COMPLETED,
        Actor.system(),
        None,
        subject_id=None,
        producer="review",
        producer_version="1",
        correlation_id=None,
        causation_id=None,
        authorization_context_sha256=None,
        surface=EventSurface.LOG_ONLY,
    )
    records = [first.to_json_dict(), second.to_json_dict()]
    records[0]["seq"] = 2
    _rechain_records(records)

    inserted: list[object] = []
    monkeypatch.setattr(pg_events, "_insert_event", lambda _conn, event: inserted.append(event))
    connection = _Connection(tuple(canonical_json(record) for record in records))

    with pytest.raises(ValueError, match=r"cannot append.*broken sequence"):
        pg_events.append_event_on(
            cast("Any", connection), run_id, EventType.RUN_STARTED, Actor.system()
        )

    assert len(connection.executed) == 2
    assert inserted == []
