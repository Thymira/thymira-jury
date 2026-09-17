"""Identifiers.

Every record carries a prefixed, random, URL-safe id (``run_3f9c…``). The prefix makes ids
self-describing in logs and prevents accidental mix-ups between tables; randomness keeps ids
unguessable across clients. Display numbers (``Run #1842``) are a presentation concern.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal

from pydantic import StringConstraints

IdKind = Literal[
    "run",
    "session",
    "agent",
    "task",
    "tool",
    "artifact",
    "experiment",
    "assessment",
    "activity",
    "profile",
    "pack",
    "binding",
    "claim",
    "control",
    "context",
    "intent",
    "input",
    "authorization",
    "approval",
    "event",
    "finding",
    "decision",
    "project",
    "request",
    "header",
    "response",
    "publication",
    "proof",
    "outcome",
    "turn",
    "work",
    "goal",
    "plan",
    "revision",
]

_ID_PATTERN = r"^[a-z]+_[0-9a-f]{32}$"
_ID_RE = re.compile(_ID_PATTERN)

Id = Annotated[str, StringConstraints(pattern=_ID_PATTERN)]


def new_id(kind: IdKind) -> str:
    """Create a new identifier of the given kind, e.g. ``new_id("run")``."""
    return f"{kind}_{uuid.uuid4().hex}"


def id_kind(value: str) -> str:
    """Return the kind prefix of an id (``"run"`` for ``"run_…"``); raises on malformed ids."""
    if not _ID_RE.match(value):
        msg = f"malformed id: {value!r}"
        raise ValueError(msg)
    return value.split("_", 1)[0]
