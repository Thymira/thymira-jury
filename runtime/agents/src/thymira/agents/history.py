"""Conservative compaction of model conversation history between agent turns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

TOOL_RETURN_SUCCESS_METADATA_KEY = "thymira_result_success"
"""Runtime-owned success fact attached to a tool return but never sent to the model."""

SUPERSEDED_WRITE_CONTENT = (
    "[content omitted: superseded by a later successful write_file to the same path]"
)
"""Model-visible replacement for a full file version that no longer exists in the workspace."""


@dataclass(frozen=True, slots=True)
class _WriteCall:
    """One structurally valid `write_file` call and its location in PydanticAI history."""

    message_index: int
    part_index: int
    tool_call_id: str
    path: str
    content: str


def _arguments(part: ToolCallPart) -> dict[str, Any] | None:
    """Decode one tool call's object arguments without accepting any other JSON shape."""
    raw = part.args
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_calls(messages: list[ModelMessage]) -> list[_WriteCall]:
    """Collect valid full-file writes in conversation order."""
    calls: list[_WriteCall] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, ModelResponse):
            continue
        for part_index, part in enumerate(message.parts):
            if not isinstance(part, ToolCallPart) or part.tool_name != "write_file":
                continue
            arguments = _arguments(part)
            if arguments is None:
                continue
            path = arguments.get("path")
            content = arguments.get("content")
            if not isinstance(path, str) or not path or not isinstance(content, str):
                continue
            calls.append(
                _WriteCall(
                    message_index=message_index,
                    part_index=part_index,
                    tool_call_id=part.tool_call_id,
                    path=path,
                    content=content,
                )
            )
    return calls


def _successful_return_locations(messages: list[ModelMessage]) -> dict[str, list[int]]:
    """Index runtime-confirmed successful write returns by PydanticAI call id."""
    locations: dict[str, list[int]] = {}
    for message_index, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or part.tool_name != "write_file":
                continue
            metadata = part.metadata
            if not isinstance(metadata, Mapping):
                continue
            if metadata.get(TOOL_RETURN_SUCCESS_METADATA_KEY) is not True:
                continue
            locations.setdefault(part.tool_call_id, []).append(message_index)
    return locations


def _completed_after(call: _WriteCall, returns: dict[str, list[int]]) -> bool:
    """Whether this exact model call has a later runtime-confirmed successful return."""
    return any(index > call.message_index for index in returns.get(call.tool_call_id, ()))


def _compact_part(part: ToolCallPart) -> ToolCallPart:
    """Replace only a write's content while preserving its path, purpose and call identity."""
    arguments = _arguments(part)
    if arguments is None:
        return part
    compacted = {**arguments, "content": SUPERSEDED_WRITE_CONTENT}
    rendered: str | dict[str, Any]
    if isinstance(part.args, str):
        rendered = json.dumps(compacted, ensure_ascii=False, sort_keys=True)
    else:
        rendered = compacted
    return replace(part, args=rendered)


def compact_superseded_write_content(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Omit an old full-file body only after a later write to the exact path succeeded.

    Tool-call and tool-return parts stay paired, the newest successful content stays verbatim,
    and a denied or failed replacement never makes the previous workspace version disappear from
    model context. Histories created before the runtime-owned success metadata existed are left
    untouched. The authoritative event log is independent of this provider-facing projection and
    continues to retain every original `tool.started` argument.
    """
    calls = _write_calls(messages)
    returns = _successful_return_locations(messages)
    successful_paths_seen: set[str] = set()
    compact_locations: set[tuple[int, int]] = set()
    for call in reversed(calls):
        if not _completed_after(call, returns):
            continue
        if call.path in successful_paths_seen and len(call.content) > len(SUPERSEDED_WRITE_CONTENT):
            compact_locations.add((call.message_index, call.part_index))
        successful_paths_seen.add(call.path)

    if not compact_locations:
        return list(messages)

    compacted_messages = list(messages)
    for message_index in sorted({location[0] for location in compact_locations}):
        message = messages[message_index]
        if not isinstance(message, ModelResponse):
            continue
        parts = [
            _compact_part(part)
            if (message_index, part_index) in compact_locations and isinstance(part, ToolCallPart)
            else part
            for part_index, part in enumerate(message.parts)
        ]
        compacted_messages[message_index] = replace(message, parts=parts)
    return compacted_messages


__all__ = [
    "SUPERSEDED_WRITE_CONTENT",
    "TOOL_RETURN_SUCCESS_METADATA_KEY",
    "compact_superseded_write_content",
]
