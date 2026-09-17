"""Byte-exact document serialisation shared by the PostgreSQL repositories.

Records are stored as canonical JSON text rather than ``jsonb`` so that a reloaded document is
identical to the one written: hash-chained events verify after a reload and ``Any``-typed
payloads (tool arguments, experiment parameters) never suffer numeric renormalisation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from thymira.events import canonical_json

if TYPE_CHECKING:
    from thymira.schemas import ThymiraModel


def dump_document(model: ThymiraModel) -> str:
    """Serialise a contract model to canonical JSON text."""
    return canonical_json(model.to_json_dict())


def load_document(text: str) -> dict[str, object]:
    """Parse a stored document back into a JSON object."""
    value = json.loads(text)
    if not isinstance(value, dict):
        msg = "stored document is not a JSON object"
        raise TypeError(msg)
    return value


__all__ = ["dump_document", "load_document"]
