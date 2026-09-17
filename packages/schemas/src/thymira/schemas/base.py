"""Base model and shared helpers for every Thymira contract.

All contracts are immutable, reject unknown fields and serialise to JSON without surprises, so
that two components (or two services) never disagree about what a record contains.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime (the only timestamp form used)."""
    return datetime.now(UTC)


class ThymiraModel(BaseModel):
    """Strict, immutable base for all contracts.

    - ``frozen``: a record never changes after creation; produce a new one with ``model_copy``.
    - ``extra="forbid"``: an unknown field is a contract violation, never silently dropped.
    - ``populate_by_name``: aliases (if any) and field names are both accepted on input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    def to_json_dict(self) -> dict[str, object]:
        """Serialise to plain JSON-compatible types (enums as values, datetimes as ISO-8601)."""
        return self.model_dump(mode="json")
