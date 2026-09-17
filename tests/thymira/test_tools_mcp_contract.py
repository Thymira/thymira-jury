"""Golden contract test for the P3 tool descriptor seam and its MCP exposure (TOOL-29).

Real: the full builtins registry, the Tool Manager, the Policy Engine/Gate, and the official MCP
`Server`/`ClientSession` machinery wired in-process over the SDK's in-memory transport. Faked:
nothing external is involved -- there is no model provider and no network in this file.

Guards two things so the MCP surface can never silently drift from the native path (TOOL-03):

1. Every tool the registry advertises exposes a non-empty description and a schema MCP can
   actually serve, and the registry and its MCP projection agree on both, name for name.
2. A call routed through the MCP protocol is authorized and recorded by the exact same
   `ToolManager` boundary as a direct call -- same policy.decision/tool.started/tool.completed
   event sequence for a valid call, and for an invalid one, the two layers fail closed in their
   own distinctive way (the manager records a FAILED call, MCP's own schema gate never lets the
   call reach the manager at all).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.thymira.test_tools import _context
from thymira.policies import ToolCapability
from thymira.schemas import ToolCallStatus
from thymira.tools import ToolContext, ToolManager, input_schema
from thymira.tools.builtins import builtins_registry
from thymira.tools.mcp import McpToolServer

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.types import TextContent

    from thymira.schemas import Event

pytestmark = pytest.mark.integration

# Payload keys that a fresh `ToolContext` mints independently per call (a new run id, a new
# `ToolCall` id, the id of the `PolicyDecision` its own Gate recorded, and -- since list_files
# persists a discovery artifact under a fresh `new_id('artifact')`-named key every call (F3.2)
# -- the artifact's own store-relative name, id and producing agent, plus the per-call discovery
# result id and the manager's independent source-witness id) and that therefore must differ
# between two otherwise-identical calls made through two separate contexts.
_VOLATILE_PAYLOAD_KEYS = frozenset(
    {
        "id",
        "tool_call_id",
        "run_id",
        "subject_id",
        "decision_id",
        "name",
        "artifact_id",
        "artifact_ids",
        "discovery_artifact_id",
        "discovery_witness_artifact_id",
        "produced_by",
    }
)


def _unreachable_context() -> ToolContext:
    """Fail loudly if ``McpToolServer.list_tools`` ever needed a context to build a schema."""
    raise AssertionError("list_tools must project the registry without executing anything")


def _without_volatile_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the per-context/per-call identifiers from one JSON-object-shaped payload."""
    return {key: value for key, value in payload.items() if key not in _VOLATILE_PAYLOAD_KEYS}


def _normalise_discovery_text(text: str) -> str:
    """Strip the per-call discovery ids embedded in a discovery tool's result text, if present."""
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(decoded, dict):
        return text
    stripped = _without_volatile_keys(decoded)
    return text if stripped == decoded else json.dumps(stripped, sort_keys=True)


def _stable_payload(event: Event) -> dict[str, Any]:
    """Return an event payload without the identifiers a fresh context mints per call."""
    payload = _without_volatile_keys(event.payload)
    value = payload.get("value")
    inner = value.get("value") if isinstance(value, dict) else None
    text = inner.get("text") if isinstance(inner, dict) else None
    if isinstance(value, dict) and isinstance(inner, dict) and isinstance(text, str):
        payload = {
            **payload,
            "value": {**value, "value": {**inner, "text": _normalise_discovery_text(text)}},
        }
    return payload


def test_registry_and_mcp_projection_form_a_matching_contract() -> None:
    """Every registered tool has a real description and a schema MCP can serve unchanged."""
    registry = builtins_registry()
    manager = ToolManager(registry)
    server = McpToolServer(registry, manager, _unreachable_context)

    tools = list(registry)
    assert tools, "the builtins registry must not be empty"

    descriptors = {descriptor.name: descriptor for descriptor in server.list_tools()}
    assert {tool.name for tool in tools} == set(descriptors)
    assert len(descriptors) == len(tools), "duplicate names would collapse the MCP projection"

    for tool in tools:
        assert tool.description.strip(), f"{tool.name} has no description"
        descriptor = descriptors[tool.name]
        assert descriptor.description == tool.description

        raw_schema = input_schema(tool)
        expected_schema = raw_schema or {"type": "object", "properties": {}}
        assert descriptor.input_schema == expected_schema

        # The schema MCP actually hands out must always be a valid, JSON-serializable object
        # schema, even for the tools whose `arguments_model` is `None`.
        assert json.dumps(descriptor.input_schema)
        assert descriptor.input_schema["type"] == "object"
        properties = descriptor.input_schema.get("properties", {})
        assert isinstance(properties, dict)
        for required_field in descriptor.input_schema.get("required", ()):
            assert required_field in properties


def test_invalid_arguments_are_rejected_as_a_failed_call_without_authorization(
    tmp_path: Path,
) -> None:
    """A direct call whose arguments fail the schema never reaches the Policy Engine."""
    context = _context(tmp_path, capability=ToolCapability(id="read_file", external_effects=()))
    context.workspace.mkdir(parents=True, exist_ok=True)
    registry = builtins_registry()
    manager = ToolManager(registry)

    execution = manager.execute(context, "read_file", {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert execution.call.policy_decision_id is None
    types = [event.type.value for event in context.event_log.events()]
    assert types == ["tool.completed"]
    assert context.event_log.verify().valid


def test_mcp_call_rejects_invalid_arguments_before_the_tool_manager_ever_sees_them(
    tmp_path: Path,
) -> None:
    """MCP's own inputSchema gate fails the same call closed, and never records a tool call.

    The SDK validates ``arguments`` against the ``inputSchema`` it listed *before* invoking our
    handler, so an invalid MCP call never becomes a ``ToolCall`` and never reaches the Gate --
    unlike the direct path, which always records a FAILED call (previous test).
    """
    context = _context(tmp_path, capability=ToolCapability(id="read_file", external_effects=()))
    context.workspace.mkdir(parents=True, exist_ok=True)
    registry = builtins_registry()
    manager = ToolManager(registry)
    server = McpToolServer(registry, manager, lambda: context)

    async def exercise() -> bool:
        async with create_connected_server_and_client_session(server.protocol_server()) as client:
            await client.initialize()
            response = await client.call_tool("read_file", {})
            return response.isError

    assert asyncio.run(exercise()) is True
    assert context.event_log.events() == []


def test_valid_call_round_trips_through_mcp_with_the_same_event_sequence_as_a_direct_call(
    tmp_path: Path,
) -> None:
    """List -> call -> gated execution over MCP records the same evidence as a direct call."""
    registry = builtins_registry()
    manager = ToolManager(registry)
    capability = ToolCapability(id="list_files", external_effects=())

    direct_context = _context(tmp_path / "direct", capability=capability)
    direct_context.workspace.mkdir(parents=True, exist_ok=True)
    direct_execution = manager.execute(direct_context, "list_files", {})

    mcp_context = _context(tmp_path / "mcp", capability=capability)
    mcp_context.workspace.mkdir(parents=True, exist_ok=True)
    server = McpToolServer(registry, manager, lambda: mcp_context)

    async def exercise() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server.protocol_server()) as client:
            await client.initialize()
            listed = await client.list_tools()
            assert "list_files" in {tool.name for tool in listed.tools}
            response = await client.call_tool("list_files", {})
            return json.loads(cast("TextContent", response.content[0]).text)

    mcp_payload = asyncio.run(exercise())

    assert direct_execution.result.success is True
    assert mcp_payload["success"] is True
    assert mcp_payload["status"] == direct_execution.call.status.value
    assert mcp_payload["value"]["kind"] == "success"

    direct_events = direct_context.event_log.events()
    mcp_events = mcp_context.event_log.events()
    direct_types = [event.type.value for event in direct_events]
    mcp_types = [event.type.value for event in mcp_events]
    # list_files persists its complete ordered result as a discovery artifact (F3.2), so a
    # Successful discovery carries one `artifact.created` event for the producer's result and
    # one for the manager's independent source witness between `tool.started` and
    # `tool.completed`; direct and MCP dispatch record identical evidence regardless of the
    # transport.
    assert (
        direct_types
        == mcp_types
        == [
            "policy.decision",
            "tool.started",
            "artifact.created",
            "artifact.created",
            "tool.completed",
        ]
    )

    # Two independently-minted contexts still authorize and record the identical call once the
    # per-context identifiers are stripped: same decision, same rule, same tool, same status.
    assert [_stable_payload(event) for event in direct_events] == [
        _stable_payload(event) for event in mcp_events
    ]

    assert direct_context.event_log.verify().valid
    assert mcp_context.event_log.verify().valid
