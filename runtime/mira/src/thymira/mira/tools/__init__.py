"""MIRA-owned tool implementations that depend on the shared Tool protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from thymira.mira.tools.search_regulation import SearchRegulation, SearchRegulationArguments
from thymira.tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from thymira.mira.kb import RegulationStore


def build_mira_tool_registry(regulation_store: RegulationStore) -> ToolRegistry:
    """Build MIRA's explicit read-only registry around the configured regulation store."""
    return ToolRegistry((cast("Tool", SearchRegulation(regulation_store)),))


__all__ = ["SearchRegulation", "SearchRegulationArguments", "build_mira_tool_registry"]
