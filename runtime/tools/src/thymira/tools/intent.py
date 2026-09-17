"""Runtime-owned identity for one policy-gated tool effect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.tools.approval_ticket import tool_intent_sha256

if TYPE_CHECKING:
    from thymira.policies import ToolCapability
    from thymira.schemas import SandboxMode


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """The persisted fields that bind approval to one exact tool effect."""

    ticket: str
    sandbox_mode: SandboxMode | None

    def evidence(self) -> dict[str, str | None]:
        """Return the mode-bearing identity fields written on effect evidence."""
        return {
            "tool_intent_sha256": self.ticket,
            "sandbox_mode": self.sandbox_mode.value if self.sandbox_mode is not None else None,
        }


def bind_tool_intent(
    tool_name: str, arguments: dict[str, Any], capability: ToolCapability
) -> ToolIntent:
    """Bind validated arguments to the runtime-owned requested sandbox mode."""
    mode = capability.sandbox_mode
    return ToolIntent(
        ticket=tool_intent_sha256(tool_name, arguments, sandbox_mode=mode),
        sandbox_mode=mode,
    )


__all__ = ["ToolIntent", "bind_tool_intent"]
