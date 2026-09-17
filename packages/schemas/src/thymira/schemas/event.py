"""Event: the append-only, hash-chained record every client can observe (Event API)."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Final

from pydantic import Field, StrictStr, field_validator

from thymira.schemas.actor import Actor
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import EventSurface, EventType
from thymira.schemas.ids import Id

GENESIS_HASH = "0" * 64
"""``prev_hash`` of the first event of a run."""

EVENT_LOG_FORMAT_VERSION: Final[str] = "0.3"
"""The one supported, globally monotonic event-log format version."""


def _reject_non_finite(value: Any, *, path: str) -> None:
    """Raise if ``value`` holds a ``nan`` or ``±inf`` anywhere inside it.

    Raises:
        ValueError: naming the path of the offending value.
    """
    if isinstance(value, float) and not isfinite(value):
        msg = (
            f"{path} is {value!r}, which JSON records as null and the hash cannot distinguish; "
            f"record it explicitly instead (for example as a string or a separate flag)"
        )
        raise ValueError(msg)
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_non_finite(item, path=f"{path}[{index}]")


class Event(ThymiraModel):
    """One fact about a run.

    ``event_id`` identifies the event independently of its run-local ``seq``. ``prev_hash`` and
    ``hash`` chain the log so any edit invalidates following events. ``schema_version`` is the
    global format version for the event log; every event in a supported log carries the same
    value. The hashing and verification live in ``thymira.events``; this model only fixes the
    envelope. Model-visible payloads remain exact on the canonical chain, with known credentials
    scrubbed at source; export projections perform PII redaction. Chain-of-thought is never a
    payload.

    ``surface`` separates the *log* (this chain — complete, append-only, what MIRA audits) from
    the *surface* (what a model is shown). Compaction never deletes: it appends a
    ``context.compacted`` event naming the seqs it shadowed, so the chain stays whole while the
    model sees less. Whether an event is currently shadowed is derived by folding the log
    (``thymira.events.derive_surface``), never stored — see :class:`SurfaceState`.
    """

    event_id: Id
    run_id: Id
    seq: int = Field(ge=0)
    type: EventType
    schema_version: StrictStr = Field(min_length=1)
    ts: datetime = Field(default_factory=utc_now)
    actor: Actor
    surface: EventSurface = Field(
        default=EventSurface.LOG_ONLY,
        description="whether this event's payload may ever be shown to a model",
    )
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    authorization_context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    subject_id: str | None = Field(
        default=None, description="id of the agent/task/tool call/artifact/finding concerned"
    )
    prev_hash: str = Field(default=GENESIS_HASH, pattern=r"^[0-9a-f]{64}$")
    hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version")
    @classmethod
    def _supported_format_version(cls, value: str) -> str:
        """Refuse an event serialized under a format this runtime cannot interpret."""
        if value != EVENT_LOG_FORMAT_VERSION:
            msg = (
                f"unsupported event format version {value!r}; "
                f"supported version is {EVENT_LOG_FORMAT_VERSION!r}"
            )
            raise ValueError(msg)
        return value

    @field_validator("payload")
    @classmethod
    def _payload_is_representable(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Refuse a payload that JSON cannot represent faithfully.

        ``model_dump(mode="json")`` maps ``nan`` and ``±inf`` to ``null``, so the hash covers
        ``null`` while the live object still holds the float: four different values collapse to one
        digest, and a diverged model's ``loss=inf`` — the strongest failure signal there is — is
        recorded as nothing at all. Evidence that cannot round-trip must not enter the chain, so
        the caller is made to decide how to record it instead.
        """
        _reject_non_finite(payload, path="payload")
        return payload

    def hashable_dict(self) -> dict[str, Any]:
        """The canonical content that the event hash covers (everything except ``hash``)."""
        data = self.to_json_dict()
        data.pop("hash", None)
        return data
