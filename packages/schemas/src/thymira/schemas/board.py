"""Persistent goal, plan and orchestrator-board contracts.

The lifecycle command and work-item envelopes live in :mod:`thymira.schemas.lifecycle`.  This
module contains only the board records consumed by the lifecycle owner and by read-only clients.
Plan artifacts are references to content-addressed artifacts; the board never duplicates their
content.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator

from thymira.schemas.actor import Actor
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.ids import Id, new_id
from thymira.schemas.subagent import SubagentResult


class TodoState(StrEnum):
    """Closed state of a durable todo item."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class GoalStatus(StrEnum):
    """Closed state of a user goal."""

    NONE = "none"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class PlanMode(StrEnum):
    """Whether an owner is still collecting explicit human plan review."""

    EXECUTION = "execution"
    PLANNING = "planning"


class PlanReviewDecision(StrEnum):
    """Explicit human decision for leaving plan mode."""

    CONTINUE_PLANNING = "continue_planning"
    LEAVE_PLAN_MODE = "leave_plan_mode"


class TodoItem(ThymiraModel):
    """One ordered item in the complete durable todo list."""

    item_id: Id = Field(
        validation_alias=AliasChoices("item_id", "id", "work_id", "task_id"),
        default_factory=lambda: new_id("task"),
    )
    ordinal: int = Field(ge=0)
    text: str = Field(
        min_length=1,
        max_length=2000,
        validation_alias=AliasChoices("text", "description", "objective", "title"),
    )
    state: TodoState = Field(
        default=TodoState.TODO,
        validation_alias=AliasChoices("state", "status"),
    )
    dependency_ids: tuple[Id, ...] = ()
    delegation_depth: int = Field(default=0, ge=0)

    @property
    def id(self) -> Id:
        """Return the item identity under the generic board vocabulary."""
        return self.item_id

    @property
    def work_id(self) -> Id:
        """Return the item identity under the lifecycle work vocabulary."""
        return self.item_id


class TodoCompletion(ThymiraModel):
    """Promptly recorded completion fact for one item."""

    item_id: Id
    ordinal: int = Field(ge=0)
    completed_at: datetime = Field(default_factory=utc_now)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    summary: str | None = Field(default=None, max_length=2000)
    source_revision: int = Field(ge=0)


class PlanArtifactRef(ThymiraModel):
    """Content-addressed reference to an advisory plan artifact."""

    artifact_id: Id = Field(
        validation_alias=AliasChoices("artifact_id", "id"),
        default_factory=lambda: new_id("artifact"),
    )
    run_id: Id
    revision: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_key: str = Field(min_length=1, max_length=500)
    advisory: Literal[True] = True

    @property
    def id(self) -> Id:
        """Return the artifact identity."""
        return self.artifact_id


class GoalRevision(ThymiraModel):
    """Immutable, human-provenanced revision of a goal."""

    revision_id: Id = Field(
        validation_alias=AliasChoices("revision_id", "id"),
        default_factory=lambda: new_id("revision"),
    )
    goal_id: Id = Field(default_factory=lambda: new_id("goal"))
    run_id: Id
    revision: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=4000)
    actor: Actor
    authority_proof_id: Id
    causal_proof: str = Field(min_length=1, max_length=4000)
    created_at: datetime = Field(default_factory=utc_now)


class GoalRound(ThymiraModel):
    """One bounded autonomous continuation round and its failure cause."""

    round_number: int = Field(ge=1)
    revision: int = Field(ge=0)
    cause: str | None = Field(default=None, min_length=1, max_length=1000)
    succeeded: bool = False
    recorded_at: datetime = Field(default_factory=utc_now)


class ToolRepeatState(ThymiraModel):
    """Consecutive canonical tool-call identity retained for repeat protection."""

    tool_name: str | None = Field(default=None, max_length=200)
    arguments_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    consecutive_count: int = Field(default=0, ge=0)


class PlanBoard(ThymiraModel):
    """Complete durable goal and todo board for one Run."""

    plan_id: Id = Field(default_factory=lambda: new_id("plan"))
    run_id: Id
    revision: int = Field(default=0, ge=0)
    predecessor_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    goal: str | None = Field(default=None, max_length=4000)
    goal_revision: GoalRevision | None = None
    goal_status: GoalStatus = GoalStatus.NONE
    items: tuple[TodoItem, ...] = ()
    completions: tuple[TodoCompletion, ...] = ()
    mode: PlanMode = PlanMode.EXECUTION
    artifact: PlanArtifactRef | None = None
    review_feedback: tuple[str, ...] = ()
    rounds: tuple[GoalRound, ...] = ()
    autonomous_rounds: int = Field(default=0, ge=0)
    blocked_cause: str | None = Field(default=None, min_length=1, max_length=1000)
    blocked_count: int = Field(default=0, ge=0)
    repeat_state: ToolRepeatState = Field(default_factory=ToolRepeatState)


class PlanFeedbackResult(ThymiraModel):
    """Model-visible feedback represented as a tool result, never as execution authority."""

    result_type: Literal["tool_result"] = "tool_result"
    plan_id: Id
    revision: int = Field(ge=0)
    decision: PlanReviewDecision
    feedback: str | None = Field(default=None, max_length=4000)
    execution_authorized: Literal[False] = False

    @property
    def kind(self) -> str:
        """Return the stable tool-result discriminator."""
        return self.result_type


class GoalCreatePayload(ThymiraModel):
    """Exact PLAN command payload for creating a human-owned goal."""

    operation: Literal["goal.create"]
    goal: str = Field(min_length=1, max_length=4000)
    causal_proof: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=0)


class GoalEditPayload(ThymiraModel):
    """Exact PLAN command payload for editing a human-owned goal."""

    operation: Literal["goal.edit"]
    goal: str = Field(min_length=1, max_length=4000)
    causal_proof: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=0)


class PlanReviewPayload(ThymiraModel):
    """Exact PLAN command payload for explicit human plan review."""

    operation: Literal["plan.review"]
    decision: PlanReviewDecision
    feedback: str | None = Field(default=None, max_length=4000)
    expected_revision: int | None = Field(default=None, ge=0)


class DagNode(ThymiraModel):
    """Immutable topology projection for one lifecycle-owned WorkItem."""

    work_id: Id
    run_id: Id
    ordinal: int = Field(ge=0)
    kind: str = Field(min_length=1)
    dependency_ids: tuple[Id, ...] = ()
    delegation_depth: int = Field(default=0, ge=0)
    # Queue identity (`work_id`/`kind`) is separate from the Task invocation that produced a
    # settlement.  These facts are copied from the owner-published WorkItem and never inferred
    # from the queue record.
    task_id: Id | None = None
    objective: str | None = Field(default=None, min_length=1)
    agent_id: Id | None = None
    agent: str | None = Field(default=None, min_length=1)
    parent_agent: str | None = Field(default=None, min_length=1)
    delegation_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_work_item(cls, item: Any) -> DagNode:
        """Project a lifecycle WorkItem without taking ownership of its state or claims."""
        return cls(
            work_id=item.work_id,
            run_id=item.run_id,
            ordinal=item.ordinal,
            kind=item.kind,
            dependency_ids=item.dependency_ids,
            delegation_depth=item.delegation_depth,
            task_id=item.task_id,
            objective=item.objective,
            agent_id=item.agent_id,
            agent=item.agent,
            parent_agent=item.parent_agent,
            delegation_key=item.delegation_key,
        )

    def has_invocation_binding(self) -> bool:
        """Return whether this node carries the complete immutable child invocation identity."""
        return all(
            value is not None
            for value in (
                self.task_id,
                self.objective,
                self.agent_id,
                self.agent,
                self.parent_agent,
                self.delegation_key,
            )
        )


class DagBoard(ThymiraModel):
    """Persisted orchestrator-owned CAS/DAG topology and typed settlement projection."""

    board_id: Id = Field(default_factory=lambda: new_id("plan"))
    run_id: Id
    revision: int = Field(default=0, ge=0)
    predecessor_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    work_items: tuple[DagNode, ...] = ()
    results: tuple[SubagentResult, ...] = ()

    @model_validator(mode="after")
    def validate_projection(self) -> DagBoard:  # noqa: PLR0912  # fail closed across board facts
        """Reject malformed topology and settlements before a board can be persisted or replayed."""
        item_ids = tuple(item.work_id for item in self.work_items)
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("DAG work item ids must be unique")
        ordinals = tuple(item.ordinal for item in self.work_items)
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("DAG work item ordinals must be unique")
        item_by_id = {item.work_id: item for item in self.work_items}
        task_ids = tuple(item.task_id for item in self.work_items)
        if any(item.task_id is None or item.objective is None for item in self.work_items):
            raise ValueError("DAG work items must carry their published Task invocation binding")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("DAG Task invocation ids must be unique")
        if any(item.task_id == item.work_id for item in self.work_items):
            raise ValueError("DAG queue and Task identities must remain distinct")
        if any(item.run_id != self.run_id for item in self.work_items):
            raise ValueError("DAG work items must belong to the board Run")
        if any(
            dependency not in item_by_id
            for item in self.work_items
            for dependency in item.dependency_ids
        ):
            raise ValueError("DAG dependencies must reference known work items")
        if any(
            len(item.dependency_ids) != len(set(item.dependency_ids)) for item in self.work_items
        ):
            raise ValueError("DAG dependency ids must be unique")
        result_ids = tuple(result.task_id for result in self.results)
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("DAG settlement task ids must be unique")
        item_by_task_id = {item.task_id: item for item in self.work_items}
        for result in self.results:
            item = item_by_task_id.get(result.task_id)
            if result.run_id != self.run_id:
                raise ValueError("DAG settlements must belong to the board Run")
            if item is None:
                raise ValueError("DAG settlement must reference a known work item")
            if not item.has_invocation_binding():
                raise ValueError("DAG work item is missing its invocation binding")
            if (
                result.objective != item.objective
                or result.delegation_depth != item.delegation_depth
                or result.agent_id != item.agent_id
                or result.agent != item.agent
                or result.parent_agent != item.parent_agent
                or result.delegation_key != item.delegation_key
            ):
                raise ValueError("DAG settlement is not bound to its work item")
        return self

    def pending(self) -> tuple[DagNode, ...]:
        """Return runnable lifecycle work in declared ordinal order."""
        settled_task_ids = {result.task_id for result in self.results}
        settled_work_ids = {
            item.work_id for item in self.work_items if item.task_id in settled_task_ids
        }
        return tuple(
            item
            for item in sorted(self.work_items, key=lambda value: value.ordinal)
            if item.task_id not in settled_task_ids
            and set(item.dependency_ids).issubset(settled_work_ids)
        )


class BoardSnapshot(ThymiraModel):
    """Read-only envelope returned by a board repository."""

    board: PlanBoard
    dag_revision: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


GoalBoard = PlanBoard


__all__ = [
    "BoardSnapshot",
    "DagBoard",
    "DagNode",
    "GoalBoard",
    "GoalCreatePayload",
    "GoalEditPayload",
    "GoalRevision",
    "GoalRound",
    "GoalStatus",
    "PlanArtifactRef",
    "PlanBoard",
    "PlanFeedbackResult",
    "PlanMode",
    "PlanReviewDecision",
    "PlanReviewPayload",
    "TodoCompletion",
    "TodoItem",
    "TodoState",
    "ToolRepeatState",
]
