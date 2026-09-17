"""Append-only, hash-chained event logs and their stateless verification.

Each event stores the hash of the previous one; editing, removing or reordering any line breaks
the chain from that point on, and :func:`verify_events` reports the first broken position. The
verification is stateless: anyone holding the log can recompute it and reach the same verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from thymira.events.file_access import create_private_file, secure_directory, secure_file
from thymira.events.hashing import canonical_json, hash_event
from thymira.events.redaction import scrub_credentials_value
from thymira.schemas import (
    EVENT_LOG_FORMAT_VERSION,
    GENESIS_HASH,
    Actor,
    Event,
    EventSurface,
    EventType,
    new_id,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class EventFormatError(ValueError):
    """Base error for a serialized event that this runtime must refuse."""


class MissingEventFormatError(EventFormatError):
    """A serialized event omitted the required global format version."""


class MalformedEventFormatError(EventFormatError):
    """A serialized event carries a format version with the wrong shape or type."""


class UnsupportedEventFormatError(EventFormatError):
    """A serialized event uses a well-formed, unsupported format version."""


class UnknownEventTypeError(EventFormatError):
    """A serialized event names an event type outside the closed EventType vocabulary."""


_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_EVENT_TYPE_VALUES = frozenset(event_type.value for event_type in EventType)
_MISSING = object()


class VerificationResult(BaseModel):
    """Outcome of verifying a chain: ``event_count`` counts the events proven valid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    event_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _EventMetadata:
    """Optional envelope fields supplied when appending an event."""

    subject_id: str | None
    producer: str
    producer_version: str
    correlation_id: str | None
    causation_id: str | None
    authorization_context_sha256: str | None
    surface: EventSurface


class EventLog(Protocol):
    """What every event log offers, whatever its storage."""

    run_id: str

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
        producer: str = "thymira.events",
        producer_version: str = "0.3",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append an event, chaining it to the previous one."""
        ...

    def events(self) -> list[Event]:
        """Every event appended so far, in order."""
        ...

    def verify(self) -> VerificationResult:
        """Re-verify the whole chain."""
        ...


def validate_event_type(value: object) -> None:
    """Refuse an event type outside the closed EventType vocabulary."""
    if not isinstance(value, EventType):
        msg = f"unknown event type {value!r}"
        raise UnknownEventTypeError(msg)


def _validate_serialized_event(data: Mapping[str, object]) -> None:
    """Validate format and event-type fields before constructing an Event model."""
    version = data.get("schema_version", _MISSING)
    if version is _MISSING:
        msg = "missing event format version: schema_version is required"
        raise MissingEventFormatError(msg)
    if isinstance(version, bool) or not isinstance(version, str) or not version:
        msg = f"malformed event format version {version!r}"
        raise MalformedEventFormatError(msg)
    if _VERSION_PATTERN.fullmatch(version) is None:
        msg = f"malformed event format version {version!r}"
        raise MalformedEventFormatError(msg)
    if version != EVENT_LOG_FORMAT_VERSION:
        msg = (
            f"unsupported event format version {version!r}; "
            f"supported version is {EVENT_LOG_FORMAT_VERSION!r}"
        )
        raise UnsupportedEventFormatError(msg)
    event_type = data.get("type", _MISSING)
    if not isinstance(event_type, str) or event_type not in _EVENT_TYPE_VALUES:
        msg = f"unknown event type {event_type!r}"
        raise UnknownEventTypeError(msg)


def deserialize_event(data: object) -> Event:
    """Deserialize one persisted event after strict format and vocabulary checks.

    Raises:
        EventFormatError: If the serialized event has a missing, malformed or unsupported format,
            or an unknown event type.
        ValueError: If the record is not a valid Event for the supported format.
    """
    if not isinstance(data, Mapping):
        msg = "event record must be a JSON object"
        raise TypeError(msg)
    _validate_serialized_event(data)
    try:
        return Event.model_validate(data)
    except ValidationError as exc:
        msg = f"event record is invalid: {exc}"
        raise ValueError(msg) from exc


def _verification_format_error(event: Event, index: int) -> str | None:
    """Return a safe verification error for an event's global format fields."""
    version = getattr(event, "schema_version", _MISSING)
    if version is _MISSING:
        return f"event {index}: missing event format version"
    if isinstance(version, bool) or not isinstance(version, str) or not version:
        return f"event {index}: malformed event format version {version!r}"
    if _VERSION_PATTERN.fullmatch(version) is None:
        return f"event {index}: malformed event format version {version!r}"
    if version != EVENT_LOG_FORMAT_VERSION:
        return f"event {index}: unsupported event format version {version!r}"
    try:
        validate_event_type(getattr(event, "type", _MISSING))
    except UnknownEventTypeError as exc:
        return f"event {index}: {exc}"
    return None


def verify_events(events: Sequence[Event]) -> VerificationResult:
    """Recompute the whole chain; stop at the first event that breaks it."""
    run_id = events[0].run_id if events else None
    return _verify_chain(events, offset=0, prev_hash=GENESIS_HASH, run_id=run_id)


def _verify_chain(
    events: Sequence[Event], *, offset: int, prev_hash: str, run_id: str | None
) -> VerificationResult:
    """Verify ``events`` as the chain segment that starts at position ``offset``.

    ``prev_hash`` and ``run_id`` are the head the segment must continue from: the genesis values
    for a whole log, or the last verified event's for a tail appended since. ``event_count`` and
    error positions are absolute, so a tail verdict reads exactly like a whole-log one.
    """
    for index, event in enumerate(events, start=offset):
        error = _verification_format_error(event, index)
        if error is not None:
            return VerificationResult(valid=False, event_count=index, error=error)

    for index, event in enumerate(events, start=offset):
        if event.run_id != run_id:
            return VerificationResult(
                valid=False,
                event_count=index,
                error=f"event {index}: run_id {event.run_id} differs from {run_id}",
            )
        if event.seq != index:
            return VerificationResult(
                valid=False,
                event_count=index,
                error=f"event {index}: broken sequence (seq={event.seq})",
            )
        if event.prev_hash != prev_hash:
            return VerificationResult(
                valid=False,
                event_count=index,
                error=f"event {index}: prev_hash does not match the previous event",
            )
        if event.hash is None or event.hash != hash_event(event):
            return VerificationResult(
                valid=False,
                event_count=index,
                error=f"event {index}: hash mismatch (content modified)",
            )
        prev_hash = event.hash
    return VerificationResult(valid=True, event_count=offset + len(events))


_PARSE_CACHE_MAX_FILES = 256
_PARSE_CACHE_MAX_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class _ParsedLog:
    """One log file's bytes, parsed, and its chain verdict once asked for."""

    digest: str
    size: int
    events: tuple[Event, ...]
    verification: VerificationResult | None = None


class _ParsedLogCache:
    """Memoise parsing and chain verification per log file, keyed on the sha256 of its bytes.

    Parsing a log -- JSON decoding, contract validation and hash recomputation for every event --
    was the cost of every read: listing Runs re-read each ``events.jsonl`` several times per
    request. The same bytes always parse to the same frozen events and reach the same verdict, so
    both are kept for the latest content of each file. Every call still reads and hashes the file,
    so any change to it, even one byte, misses the cache: a tampered log is refused exactly as
    before. An append-only log grows by whole lines, so when the new bytes start with exactly the
    cached bytes (same sha256 over that prefix) only the appended lines are parsed and, if the
    cached prefix already verified, only they are chained onto its head -- a Run writing its
    2000th event no longer re-parses the 1999 before it on every read. Any other change, even one
    byte inside the prefix, is parsed and verified afresh. Eviction bounds the cache by file count
    and bytes.
    """

    def __init__(self, *, max_files: int, max_bytes: int) -> None:
        self._entries: OrderedDict[str, _ParsedLog] = OrderedDict()
        self._bytes = 0
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def load(self, path: Path) -> _ParsedLog:
        """Return the parsed log for the file's current bytes, parsing only when they changed."""
        source = Path(path)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        key = str(source.absolute())
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached.digest == digest:
                self._entries.move_to_end(key)
                return cached
        entry = _extend_parsed_log(cached, data, digest) if cached is not None else None
        if entry is None:
            entry = _ParsedLog(digest=digest, size=len(data), events=tuple(_parse_events(data)))
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous.size
            if entry.size <= self._max_bytes:
                self._entries[key] = entry
                self._bytes += entry.size
            while len(self._entries) > self._max_files or self._bytes > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= evicted.size
        return entry

    @staticmethod
    def verification(entry: _ParsedLog) -> VerificationResult:
        """Return the chain verdict for a parsed log, computing it once per distinct content."""
        if entry.verification is None:
            entry.verification = verify_events(entry.events)
        return entry.verification


def _extend_parsed_log(cached: _ParsedLog, data: bytes, digest: str) -> _ParsedLog | None:
    """Parse only the lines appended after ``cached``; ``None`` when ``data`` is not that.

    The cached content must be a whole-line prefix of ``data`` -- same length or longer, ending
    on a newline, same sha256 over those bytes. The chain verdict carries over only when the
    prefix already verified valid; a prefix never verified, or verified invalid, leaves the
    verdict to be computed on demand over the whole log as before.
    """
    size = cached.size
    if size == 0 or len(data) <= size or data[size - 1 : size] != b"\n":
        return None
    if hashlib.sha256(data[:size]).hexdigest() != cached.digest:
        return None
    tail = tuple(_parse_events(data[size:], line_offset=data.count(b"\n", 0, size)))
    verification: VerificationResult | None = None
    if cached.verification is not None and cached.verification.valid and cached.events:
        head = cached.events[-1]
        verification = _verify_chain(
            tail,
            offset=len(cached.events),
            prev_hash=head.hash or GENESIS_HASH,
            run_id=head.run_id,
        )
    return _ParsedLog(
        digest=digest,
        size=len(data),
        events=cached.events + tail,
        verification=verification,
    )


def _parse_events(data: bytes, *, line_offset: int = 0) -> list[Event]:
    """Parse JSONL bytes into events; raises ``ValueError`` on malformed content.

    ``line_offset`` is how many lines precede ``data`` in its file, so error messages number
    lines as the file does.
    """
    events: list[Event] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=line_offset + 1):
        if not line.strip():
            continue
        try:
            events.append(deserialize_event(json.loads(line)))
        except EventFormatError as exc:
            msg = f"line {number} has an invalid event format: {exc}"
            raise type(exc)(msg) from exc
        except (json.JSONDecodeError, TypeError, ValidationError) as exc:
            msg = f"line {number} is not a valid event: {exc}"
            raise ValueError(msg) from exc
    return events


_PARSED_LOGS = _ParsedLogCache(max_files=_PARSE_CACHE_MAX_FILES, max_bytes=_PARSE_CACHE_MAX_BYTES)


def read_events(path: Path) -> list[Event]:
    """Parse a JSONL log into events; raises ``ValueError`` on malformed content.

    The file is read on every call; its parse is reused only while its bytes are unchanged (see
    :class:`_ParsedLogCache`). The returned list belongs to the caller.
    """
    return list(_PARSED_LOGS.load(path).events)


def verify_log(path: Path) -> VerificationResult:
    """Verify a JSONL log on disk; the verdict is recomputed whenever the file's bytes change."""
    try:
        entry = _PARSED_LOGS.load(path)
    except (OSError, ValueError) as exc:
        return VerificationResult(
            valid=False, event_count=0, error=f"could not read the log: {exc}"
        )
    return _PARSED_LOGS.verification(entry)


class _ChainState:
    """Shared chaining logic: builds the next event from the current head."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._seq = 0
        self._prev_hash = GENESIS_HASH
        self._lock = threading.Lock()

    def _restore(self, events: Sequence[Event]) -> None:
        if events:
            self._seq = events[-1].seq + 1
            self._prev_hash = events[-1].hash or GENESIS_HASH

    def _next(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None,
        *,
        metadata: _EventMetadata,
    ) -> Event:
        validate_event_type(type)
        draft = Event(
            event_id=new_id("event"),
            run_id=self.run_id,
            seq=self._seq,
            type=type,
            schema_version=EVENT_LOG_FORMAT_VERSION,
            ts=utc_now(),
            actor=actor,
            producer=metadata.producer,
            producer_version=metadata.producer_version,
            correlation_id=metadata.correlation_id,
            causation_id=metadata.causation_id,
            authorization_context_sha256=metadata.authorization_context_sha256,
            surface=metadata.surface,
            # The canonical chain preserves model-visible PII and tool evidence exactly. Known
            # credential values are the source-level exception: they must never become evidence
            # merely because a caller accidentally handed one to an event writer.
            payload=scrub_credentials_value(dict(payload or {})),
            subject_id=metadata.subject_id,
            prev_hash=self._prev_hash,
        )
        event = draft.model_copy(update={"hash": hash_event(draft)})
        self._seq += 1
        self._prev_hash = event.hash or GENESIS_HASH
        return event


class InMemoryEventLog(_ChainState):
    """A chained log kept in memory — for tests and short-lived runs."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self._events: list[Event] = []

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
        producer: str = "thymira.events",
        producer_version: str = "0.3",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append an event with PII intact and source credentials scrubbed."""
        with self._lock:
            event = self._next(
                type,
                actor,
                payload,
                metadata=_EventMetadata(
                    subject_id=subject_id,
                    producer=producer,
                    producer_version=producer_version,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    authorization_context_sha256=authorization_context_sha256,
                    surface=surface,
                ),
            )
            self._events.append(event)
        return event

    def events(self) -> list[Event]:
        """All events in order."""
        return list(self._events)

    def verify(self) -> VerificationResult:
        """Recompute the chain."""
        return verify_events(self._events)


class JsonlEventLog(_ChainState):
    """A chained log persisted as one JSON object per line (append-only).

    Re-opening an existing file resumes the chain only if the file verifies; a tampered or
    unreadable file is refused so nothing is ever appended to invalid evidence.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        super().__init__(run_id)
        self.path = Path(path)
        # This low-level writer is publicly constructible outside LocalRunStore. Secure its parent
        # and file before verification or the first append so the canonical raw log never has an
        # unprotected creation window. The permission primitive is owned by this package rather
        # than runtime/state to keep the guarantee at the writer boundary.
        secure_directory(self.path.parent)
        if self.path.exists():
            secure_file(self.path)
        else:
            create_private_file(self.path, b"")
        existing = self._load_existing("resume")
        self._restore(existing)
        self._size, self._tail = _read_tail(self.path)

    def _tail_unchanged(self) -> bool:
        """Whether the file still ends exactly where, and with what, this writer last saw.

        A cheap precondition for appending without re-reading the log: same byte length and the
        same final line as after our last append or load. Any rewrite that keeps the length but
        changes an event re-hashes it and every later one, so the final line always differs and
        the full verification runs as before. A rewrite that leaves the final line and length
        intact but alters an earlier line is not caught here; it is caught by every reader,
        because reads and ``verify`` recompute the chain from the bytes.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size != self._size:
            return False
        if size == 0:
            return True
        with self.path.open("rb") as handle:
            handle.seek(size - len(self._tail))
            return handle.read() == self._tail

    def _load_existing(self, action: str) -> list[Event]:
        """Read and verify the current file before resuming or appending to it."""
        if not self.path.exists() or not self.path.stat().st_size:
            return []
        secure_file(self.path)
        try:
            entry = _PARSED_LOGS.load(self.path)
        except EventFormatError as exc:
            msg = f"cannot {action} {self.path}: {exc}"
            raise type(exc)(msg) from exc
        except (OSError, ValueError) as exc:
            msg = f"cannot {action} {self.path}: {exc}"
            raise ValueError(msg) from exc
        result = _PARSED_LOGS.verification(entry)
        if not result.valid:
            msg = f"cannot {action} {self.path}: {result.error}"
            raise ValueError(msg)
        existing = list(entry.events)
        if existing and existing[0].run_id != self.run_id:
            msg = f"cannot {action} {self.path}: it belongs to run {existing[0].run_id}"
            raise ValueError(msg)
        return existing

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
        producer: str = "thymira.events",
        producer_version: str = "0.3",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append an event with PII intact, persist it, and return it with its hash.

        The file is re-read and re-verified only when it no longer ends where this writer left
        it (see :meth:`_tail_unchanged`); otherwise the chain head kept in memory is the head on
        disk and the append costs one line, not one parse of the whole log.
        """
        with self._lock:
            if not self._tail_unchanged():
                existing = self._load_existing("append to")
                self._seq = 0
                self._prev_hash = GENESIS_HASH
                self._restore(existing)
                self._size, self._tail = _read_tail(self.path)
            secure_file(self.path)
            event = self._next(
                type,
                actor,
                payload,
                metadata=_EventMetadata(
                    subject_id=subject_id,
                    producer=producer,
                    producer_version=producer_version,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    authorization_context_sha256=authorization_context_sha256,
                    surface=surface,
                ),
            )
            line = (canonical_json(event.to_json_dict()) + "\n").encode("utf-8")
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.path.parent)
            self._size += len(line)
            self._tail = line
        return event

    def events(self) -> list[Event]:
        """All events currently on disk."""
        if not self.path.exists():
            return []
        secure_file(self.path)
        return read_events(self.path)

    def verify(self) -> VerificationResult:
        """Recompute the chain from disk."""
        return verify_log(self.path)


_TAIL_CHUNK = 64 * 1024


def _read_tail(path: Path) -> tuple[int, bytes]:
    """Return the file's byte length and its final line (newline included when present)."""
    size = path.stat().st_size
    if size == 0:
        return 0, b""
    buffer = b""
    end = size
    with path.open("rb") as handle:
        while end > 0:
            start = max(0, end - _TAIL_CHUNK)
            handle.seek(start)
            buffer = handle.read(end - start) + buffer
            # The newline before the final one bounds the last line; the final byte may itself
            # be that line's terminator, so it is excluded from the search.
            cut = buffer.rfind(b"\n", 0, len(buffer) - 1)
            if cut != -1:
                return size, buffer[cut + 1 :]
            end = start
    return size, buffer


def _fsync_directory(path: Path) -> None:
    """Flush a log directory entry when directory handles are supported."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)
