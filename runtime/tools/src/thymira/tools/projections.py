"""Pure projections of canonical typed tool values for model and client surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.tools.results import canonical_value, reconstruct_value, value_text

if TYPE_CHECKING:
    from pydantic import BaseModel


def project_tool_text(value: BaseModel) -> str:
    """Project a validated typed tool value into the model-facing text representation."""
    return value_text(value)


def project_tool_value(value: BaseModel) -> dict[str, object]:
    """Project a validated typed tool value into a JSON-compatible client payload."""
    return canonical_value(value)


def project_recorded_tool_text(tool: object, payload: dict[str, object]) -> str:
    """Project a durable event value through its registry schema for an independent reader."""
    return project_tool_text(reconstruct_value(tool, payload))


__all__ = ["project_recorded_tool_text", "project_tool_text", "project_tool_value"]
