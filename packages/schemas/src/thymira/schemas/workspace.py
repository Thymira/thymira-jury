"""Typed metadata for canonical workspace registrations."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.ids import Id


class WorkspaceRegistration(ThymiraModel):
    """Registry metadata for a workspace path, independent of the user's files."""

    workspace_id: str = Field(pattern=r"^workspace_[0-9a-f]{32}$")
    identity: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    project_id: Id
    registered_at: datetime = Field(default_factory=utc_now)


__all__ = ["WorkspaceRegistration"]
