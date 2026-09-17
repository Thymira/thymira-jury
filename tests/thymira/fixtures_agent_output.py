"""Dummy `BaseModel` output schemas, importable by `output_schema_ref` in agent-spec tests."""

from __future__ import annotations

from pydantic import BaseModel


class DummyOutput(BaseModel):
    """A minimal structured output for `AgentSpec`/`AgentCatalog` resolution tests."""

    ok: bool


class DataProfileOutput(BaseModel):
    """A minimal structured output for `AgentRunner` tests."""

    row_count: int
    columns: tuple[str, ...]
