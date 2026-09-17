"""MCP protocol contract tests for the P3 tool surface."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.thymira.test_tools import _context
from thymira.policies import ToolCapability
from thymira.schemas import SandboxMode
from thymira.tools import ToolManager
from thymira.tools.builtins import builtins_registry
from thymira.tools.mcp import McpToolServer

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.types import TextContent

pytestmark = pytest.mark.integration


def test_mcp_protocol_projects_registry_and_preserves_manager_events(tmp_path: Path) -> None:
    """A protocol call is authorized and recorded by the same manager as a direct call."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_python", external_effects=()),
        development=True,
    )
    registry = builtins_registry(subprocess_mode=SandboxMode.DANGER_FULL_ACCESS)
    manager = ToolManager(registry)
    server = McpToolServer(registry, manager, lambda: context)

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server.protocol_server()) as client:
            await client.initialize()
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {tool.name for tool in registry}
            assert all(tool.inputSchema for tool in listed.tools)
            assert all(tool.inputSchema.get("type") == "object" for tool in listed.tools)
            response = await client.call_tool(
                "run_python",
                {"code": "print('mcp')", "description": "Print the mcp marker"},
            )
            payload = json.loads(cast("TextContent", response.content[0]).text)
            assert payload["success"] is True
            assert "mcp" in payload["stdout"]
            assert payload["value"]["kind"] == "success"
            assert payload["value"]["value"]["text"] == payload["stdout"]

    asyncio.run(exercise())
    assert context.event_log.verify().valid
    types = [event.type.value for event in context.event_log.events()]
    assert types == ["policy.decision", "tool.started", "tool.completed"]
