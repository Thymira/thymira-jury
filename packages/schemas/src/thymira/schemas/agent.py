"""Agent and Task: who works inside a run and what they were asked to do."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import Layer, TaskStatus
from thymira.schemas.ids import Id


class Agent(ThymiraModel):
    """An agent instance participating in a run (THY, MIRA, or one of their sub-agents)."""

    id: Id
    run_id: Id
    name: str = Field(min_length=1, description="e.g. 'thy', 'data-agent', 'methodology-agent'")
    layer: Layer
    parent_agent_id: Id | None = Field(default=None, description="orchestrator that delegated")
    model: str | None = Field(default=None, description="LiteLLM model identifier actually used")
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class Task(ThymiraModel):
    """A unit of delegated work with its outcome; tasks form the run's plan."""

    id: Id
    run_id: Id
    agent_id: Id = Field(description="agent responsible for the task")
    objective: str = Field(min_length=1)
    skill_names: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    depends_on: tuple[Id, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    summary: str | None = Field(
        default=None, description="structured outcome, never chain-of-thought"
    )
    artifact_ids: tuple[Id, ...] = ()
    error: str | None = None
