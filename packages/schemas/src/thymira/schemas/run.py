"""Run and Session: the backbone objects of Thymira (MVP roadmap, section 3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import Decision, RunStatus, can_transition
from thymira.schemas.ids import Id
from thymira.schemas.model_routes import ModelRoutePolicy


class Session(ThymiraModel):
    """A client conversation that may start several runs.

    The session records which client opened it; it never holds run state (that lives in the
    runtime), so the same run can be continued from another client.
    """

    id: Id
    project_id: Id
    client: str = Field(min_length=1, description="cli, web, vscode, mcp, sdk, …")
    created_at: datetime = Field(default_factory=utc_now)
    run_ids: tuple[Id, ...] = ()
    model_route_policy: ModelRoutePolicy = Field(default_factory=ModelRoutePolicy.unavailable)

    @property
    def route_policy(self) -> ModelRoutePolicy:
        """Return the immutable model-route snapshot attached at session creation."""
        return self.model_route_policy


class Run(ThymiraModel):
    """One unit of work requested by a user and executed by THY, then audited by MIRA.

    Child records (agents, tasks, tool calls, experiments, artifacts, findings) reference the
    run by id; the run lists their ids so a client can render it from a single read.
    """

    id: Id
    project_id: Id
    session_id: Id
    prompt: str = Field(min_length=1)
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    git_commit: str | None = Field(
        default=None, description="workspace commit the run started from"
    )
    agent_ids: tuple[Id, ...] = ()
    task_ids: tuple[Id, ...] = ()
    tool_call_ids: tuple[Id, ...] = ()
    experiment_ids: tuple[Id, ...] = ()
    artifact_ids: tuple[Id, ...] = ()
    finding_ids: tuple[Id, ...] = ()
    policy_decision_id: Id | None = Field(
        default=None, description="the run-level decision produced by the Policy Engine"
    )
    final_decision: Decision | None = None
    error: str | None = None
    model_route_policy: ModelRoutePolicy = Field(default_factory=ModelRoutePolicy.unavailable)

    def with_status(self, status: RunStatus, *, at: datetime | None = None) -> Run:
        """Return a copy in ``status``; raises ``ValueError`` on a forbidden transition."""
        if not can_transition(self.status, status):
            msg = f"run {self.id}: transition {self.status} -> {status} is not allowed"
            raise ValueError(msg)
        stamp = at or utc_now()
        updates: dict[str, object] = {"status": status}
        if status is RunStatus.PLANNING and self.started_at is None:
            updates["started_at"] = stamp
        if status.is_terminal:
            updates["completed_at"] = stamp
        return self.model_copy(update=updates)
