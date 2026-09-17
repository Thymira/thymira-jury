"""Who did something: the actor recorded on events, decisions and approvals."""

from __future__ import annotations

from pydantic import Field

from thymira.schemas.base import ThymiraModel
from thymira.schemas.enums import ActorKind


class Actor(ThymiraModel):
    """A system component, a human, an agent or a tool.

    ``authenticated`` is ``False`` for identities that are merely declared (for example a CLI
    flag); the thesis prototype made the same distinction and the audit layer relies on it.
    """

    kind: ActorKind
    id: str = Field(min_length=1, description="agent id, tool name, user id or 'system'")
    role: str | None = Field(default=None, description="e.g. 'data-scientist', 'risk-officer'")
    authenticated: bool = False

    @classmethod
    def system(cls) -> Actor:
        """The runtime itself."""
        return cls(kind=ActorKind.SYSTEM, id="system", authenticated=True)
