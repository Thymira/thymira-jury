"""Validation helpers for canonical Thymira run identifiers."""

from __future__ import annotations

import re

RUN_ID_PATTERN = re.compile(r"^run_[0-9a-f]{32}$")
INVALID_RUN_ID_MESSAGE = "RUN_ID must look like 'run_' followed by 32 hexadecimal characters."


def validate_run_id(run_id: str) -> None:
    """Validate the canonical identifier format shared by run commands."""
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError(INVALID_RUN_ID_MESSAGE)
