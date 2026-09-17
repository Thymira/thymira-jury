"""Sandbox-mode facts shared by tools that launch subprocesses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.schemas import SandboxMode

if TYPE_CHECKING:
    from thymira.policies import ToolCapability


def capability_for_sandbox_mode(capability: ToolCapability, mode: SandboxMode) -> ToolCapability:
    """Return an immutable capability that truthfully declares ``mode``.

    An unconfined subprocess can access the host filesystem and network regardless of what its
    command intended.  Its capability therefore exposes both effects to the policy engine.
    """
    if not isinstance(mode, SandboxMode):
        raise TypeError("sandbox mode must be a SandboxMode")
    if mode is not SandboxMode.DANGER_FULL_ACCESS:
        return capability.model_copy(update={"sandbox_mode": mode})
    return capability.model_copy(
        update={
            "sandbox_mode": mode,
            "risk_tags": (*capability.risk_tags, "unconfined_code"),
            "external_effects": (*capability.external_effects, "filesystem", "network"),
        }
    )


def requested_sandbox_mode(capability: ToolCapability) -> SandboxMode:
    """Return the mode recorded on a subprocess capability, rejecting incomplete declarations."""
    mode = capability.sandbox_mode
    if not isinstance(mode, SandboxMode):
        raise TypeError("subprocess capability must declare a SandboxMode")
    return mode


__all__ = ["capability_for_sandbox_mode", "requested_sandbox_mode"]
