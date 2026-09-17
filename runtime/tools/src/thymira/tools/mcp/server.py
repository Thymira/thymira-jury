"""MCP server over the Tool Manager."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from thymira.tools.models import input_schema
from thymira.tools.results import canonical_result, result_schema

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.tools.manager import ToolManager
    from thymira.tools.models import ToolContext
    from thymira.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """MCP-compatible metadata for one registered tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)


class McpToolServer:
    """Expose registered tools while preserving the Manager as the policy boundary."""

    def __init__(
        self,
        registry: ToolRegistry,
        manager: ToolManager,
        context_factory: Callable[[], ToolContext],
    ) -> None:
        self.registry = registry
        self.manager = manager
        self.context_factory = context_factory

    def list_tools(self) -> tuple[ToolDescriptor, ...]:
        """Return descriptors with the same schemas as direct calls."""
        return tuple(
            ToolDescriptor(
                tool.name,
                tool.description,
                _mcp_input_schema(tool),
                result_schema(tool),
            )
            for tool in self.registry
        )

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Route one MCP-shaped call through ToolManager.execute."""
        execution = self.manager.execute(self.context_factory(), name, arguments)
        if execution.result.value is None:
            raise RuntimeError("Tool Manager returned no canonical result value")
        value = canonical_result(execution.result.value, success=execution.result.success)
        value_payload = value["value"]
        if not isinstance(value_payload, dict):
            raise TypeError("Tool Manager returned a non-object canonical result value")
        stdout = value_payload.get("text", "")
        declared_stdout = value_payload.get("stdout")
        if isinstance(declared_stdout, str):
            stdout = declared_stdout
        declared_stderr = value_payload.get("stderr", "")
        stderr = declared_stderr if isinstance(declared_stderr, str) else ""
        if not isinstance(stdout, str):
            raise TypeError("Tool Manager returned an invalid canonical text projection")
        return {
            "success": execution.result.success,
            "stdout": stdout,
            "stderr": stderr,
            "error": execution.result.error,
            "result_code": execution.result.code,
            "timeout_s": execution.result.timeout_s,
            "aborted": execution.result.aborted,
            "value": value,
            "artifact_ids": list(execution.call.artifact_ids),
            "status": execution.call.status,
        }

    def protocol_server(self) -> Any:
        """Build an official MCP low-level server backed by this manager.

        The synchronous ``list_tools``/``call`` methods remain useful for in-process callers,
        while this adapter supplies the actual MCP protocol handlers for stdio or another MCP
        transport. Every protocol call still enters the same manager boundary.
        """
        protocol = Server("thymira-tools")

        @protocol.list_tools()
        async def _list_tools() -> list[Tool]:
            return [
                Tool(
                    name=descriptor.name,
                    description=descriptor.description,
                    inputSchema=descriptor.input_schema,
                    outputSchema=descriptor.output_schema,
                )
                for descriptor in self.list_tools()
            ]

        @protocol.call_tool()
        async def _call_tool(
            name: str, arguments: dict[str, Any]
        ) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
            response = self.call(name, arguments)
            content = [
                TextContent(type="text", text=json.dumps(response, sort_keys=True, default=str))
            ]
            if not response["success"]:
                # The declared output schema describes successful values. A manager denial or
                # failure still carries its typed ToolFailureValue in the envelope, but MCP
                # represents that outcome as an error result rather than validating it as the
                # successful tool model.
                return CallToolResult(content=content, isError=True)
            return (
                content,
                response["value"]["value"],
            )

        return protocol

    async def run_stdio(self) -> None:
        """Serve the registered tools over the standard MCP stdio transport."""
        protocol = self.protocol_server()
        async with stdio_server() as (read_stream, write_stream):
            await protocol.run(
                read_stream,
                write_stream,
                protocol.create_initialization_options(),
            )


__all__ = ["McpToolServer", "ToolDescriptor"]


def _mcp_input_schema(tool: Any) -> dict[str, Any]:
    """Return a valid object schema also for tools that take no arguments."""
    schema = input_schema(tool)
    return schema or {"type": "object", "properties": {}}
