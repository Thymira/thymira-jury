"""Shared argument fields for model-facing tool call contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field


def _normalize_description(value: object) -> object:
    """Collapse whitespace so a rationale is non-blank and stable in recorded evidence."""
    return " ".join(value.split()) if isinstance(value, str) else value


_DESCRIPTION_GUIDANCE = (
    "Clear, concise description of what this call does in active voice, 5-10 words "
    '(shown in the UI and recorded as evidence). Examples: "Profile missing values per '
    'column"; "Train the logistic-regression baseline"; "Write the feature-engineering '
    'script".'
)
"""The one model-facing sentence every effectful tool's description argument advertises."""

Description = Annotated[
    str,
    BeforeValidator(_normalize_description),
    Field(min_length=1, max_length=200, description=_DESCRIPTION_GUIDANCE),
]

DESCRIPTION_FIELD = Field(description=_DESCRIPTION_GUIDANCE)
"""Required, bounded human rationale for an effectful tool call.

The assignment repeats the annotated guidance deliberately: pydantic lets an assigned ``Field``
override the annotated one, so a field declared ``description: Description = DESCRIPTION_FIELD``
would otherwise advertise a different sentence to the model than the type promises.
"""

__all__ = ["DESCRIPTION_FIELD", "Description"]
