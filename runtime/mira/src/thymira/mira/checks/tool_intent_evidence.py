"""Recompute a tool-call ticket independently from persisted event evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json, sha256_text
from thymira.schemas import ActorKind, SandboxMode, ToolCallStatus

if TYPE_CHECKING:
    from thymira.schemas import Event


_DESCRIPTION_KEY = "description"


def has_ticket_claim(event: Event) -> bool:
    """Whether an effect event claims a manager ticket, including a malformed value."""
    return "tool_intent_sha256" in event.payload


def has_ticket_request_evidence(event: Event) -> bool:
    """Whether a request carries any field that identifies a ticketed call."""
    return "arguments" in event.payload or has_ticket_claim(event)


def _text(value: Any) -> str | None:
    """Return a non-empty string from an untrusted payload value."""
    return value if isinstance(value, str) and value else None


def _effect_tool(event: Event) -> str | None:
    """Recover an effect's exact tool name from its payload and actor identity."""
    payload_tool = _text(event.payload.get("tool"))
    if event.actor.kind is not ActorKind.TOOL:
        return payload_tool
    actor_tool = event.actor.id
    return actor_tool if payload_tool == actor_tool else None


def _sandbox_mode(payload: dict[str, Any]) -> tuple[bool, SandboxMode | None]:
    """Parse an optional persisted sandbox mode without trusting malformed evidence."""
    raw = payload.get("sandbox_mode")
    if raw is None:
        return True, None
    try:
        return True, SandboxMode(raw)
    except (TypeError, ValueError):
        return False, None


def _recompute_ticket(tool: str, arguments: Any, mode: SandboxMode | None) -> str | None:
    """Fold the manager's effect digest from persisted, source-scrubbed evidence."""
    if not isinstance(arguments, dict):
        return None
    effect = {key: value for key, value in arguments.items() if key != _DESCRIPTION_KEY}
    identity: dict[str, Any] = {"tool": tool, "arguments": effect}
    if mode is not None:
        identity["sandbox_mode"] = mode.value
    return sha256_text(canonical_json(identity))


def verified_effect_ticket(event: Event) -> str | None:
    """Return an effect's claimed ticket only when its own evidence recomputes it."""
    claimed = _text(event.payload.get("tool_intent_sha256"))
    tool = _effect_tool(event)
    if claimed is None or tool is None:
        return None
    valid_mode, mode = _sandbox_mode(event.payload)
    if not valid_mode:
        return None
    recomputed = _recompute_ticket(tool, event.payload.get("arguments"), mode)
    return claimed if recomputed == claimed else None


@dataclass(frozen=True, slots=True)
class RecordedToolIntent:
    """One approval request's persisted call identity, verified against a later effect."""

    ticket: str
    tool: str
    arguments: dict[str, Any]
    sandbox_mode: SandboxMode | None

    @classmethod
    def from_event(cls, event: Event) -> RecordedToolIntent | None:
        """Read complete request evidence; malformed or missing fields bind no intent."""
        ticket = _text(event.payload.get("tool_intent_sha256"))
        tool = _text(event.payload.get("tool"))
        arguments = event.payload.get("arguments")
        valid_mode, mode = _sandbox_mode(event.payload)
        if ticket is None or tool is None or not isinstance(arguments, dict) or not valid_mode:
            return None
        return cls(ticket=ticket, tool=tool, arguments=dict(arguments), sandbox_mode=mode)

    def matches(self, event: Event) -> bool:
        """Whether this request and an effect independently describe the claimed ticket."""
        effect_tool = _effect_tool(event)
        if effect_tool is None or self.tool != effect_tool:
            return False
        valid_mode, effect_mode = _sandbox_mode(event.payload)
        return (
            valid_mode
            and effect_mode is self.sandbox_mode
            and _recompute_ticket(effect_tool, self.arguments, self.sandbox_mode) == self.ticket
            and verified_effect_ticket(event) == self.ticket
        )


def sandbox_modes_match(start: Event, completed: Event) -> bool:
    """Check the actual mode, or the request on a failed attempt with no actual-mode report."""
    valid_requested, requested = _sandbox_mode(start.payload)
    if valid_requested and requested is None:
        return True
    if completed.payload.get("sandbox_mode_invalid", False) is not False:
        return False
    valid_reported, reported = _sandbox_mode(completed.payload)
    if not valid_requested or not valid_reported:
        return False
    if reported is not None:
        return requested is reported
    requested_report = {"sandbox_mode": completed.payload.get("requested_sandbox_mode")}
    valid_repeated, repeated = _sandbox_mode(requested_report)
    return (
        completed.payload.get("status") == ToolCallStatus.FAILED.value
        and valid_repeated
        and repeated is not None
        and repeated is requested
    )
