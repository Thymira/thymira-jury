"""Independent MIRA replay of durable plan and delegation-board facts.

MIRA receives persisted board snapshots as evidence.  These helpers intentionally repeat the
small revision, blocked-round and dependency-selection folds instead of calling the THY/core
owners' methods.  A disagreement is evidence of a malformed or stale board, never a reason for
MIRA to repair it.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from typing import TYPE_CHECKING

from thymira.events import canonical_json
from thymira.schemas import DagBoard, DagNode, GoalStatus, Id, PlanBoard

if TYPE_CHECKING:
    from collections.abc import Sequence


class BoardReplayError(ValueError):
    """Raised when durable board evidence cannot be replayed consistently."""


def _recompute_delegation_key(item: DagNode) -> str:
    """Recompute one invocation key from the board's published identity facts.

    This is deliberately an MIRA-local projection.  A board can be constructed from malformed
    evidence with ``model_construct`` or arrive from a producer that copied its own key, so the
    key must be derived again from the seven durable invocation fields before any settlement is
    considered authoritative.
    """
    fields = (
        item.run_id,
        item.task_id,
        item.agent_id,
        item.parent_agent,
        item.agent,
        item.objective,
        item.delegation_depth,
    )
    if any(value is None for value in fields):
        raise BoardReplayError("DAG node lacks facts required to recompute its delegation key")
    return sha256(
        canonical_json(
            {
                "run_id": item.run_id,
                "task_id": item.task_id,
                "agent_id": item.agent_id,
                "parent_agent": item.parent_agent,
                "agent": item.agent,
                "objective": item.objective,
                "delegation_depth": item.delegation_depth,
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class BoardReplay:
    """MIRA's independently recomputed board facts."""

    run_id: Id
    plan_revision: int
    blocked_count: int
    blocked_cause: str | None
    blocked: bool
    runnable_work_ids: tuple[Id, ...]


def replay_plan_revisions(
    history: Sequence[PlanBoard],
    *,
    block_after_same_cause: int = 3,
) -> tuple[Id, int, int, str | None, bool]:
    """Recompute plan revision and consecutive same-cause blocking from evidence.

    ``history`` is ordered by persistence time.  A single latest snapshot is valid when its
    embedded rounds are available; a multi-snapshot history must advance one revision at a time.
    The persisted count and status are checked against MIRA's fold before the recomputed values
    are returned.
    """
    if block_after_same_cause < 1:
        raise ValueError("block_after_same_cause must be positive")
    snapshots = tuple(history)
    if not snapshots:
        raise BoardReplayError("plan replay requires at least one snapshot")
    _validate_plan_history(snapshots)
    current = snapshots[-1]
    count, cause = _fold_blocked_rounds(current)
    _validate_plan_projection(current, count, cause, block_after_same_cause)
    blocked = count >= block_after_same_cause
    return current.run_id, current.revision, count, cause, blocked


def _validate_plan_history(snapshots: Sequence[PlanBoard]) -> None:
    """Check identity and monotonic revision facts in a persisted plan history."""
    first = snapshots[0]
    # A persisted history is the evidence source, so a first record at revision N cannot prove
    # that revisions before N were not lost.  Revision zero is retained only for direct callers
    # supplying an in-memory genesis snapshot; repositories never persist it.
    if first.revision not in {0, 1}:
        raise BoardReplayError("plan history starts after its revision-one genesis")
    if first.predecessor_sha256 is not None:
        raise BoardReplayError("plan history genesis has a predecessor")
    if any(snapshot.run_id != first.run_id for snapshot in snapshots):
        raise BoardReplayError("plan replay contains more than one Run")
    if any(snapshot.plan_id != first.plan_id for snapshot in snapshots):
        raise BoardReplayError("plan replay contains more than one plan")
    for previous, current in pairwise(snapshots):
        if current.revision != previous.revision + 1:
            raise BoardReplayError("plan revisions must advance exactly once per snapshot")
        expected_digest = sha256(
            canonical_json(previous.to_json_dict()).encode("utf-8")
        ).hexdigest()
        if current.predecessor_sha256 != expected_digest:
            raise BoardReplayError("plan history predecessor digest does not match its predecessor")


def _fold_blocked_rounds(board: PlanBoard) -> tuple[int, str | None]:
    """Fold the cumulative round history into the current same-cause failure streak."""
    count = 0
    cause: str | None = None
    for round_record in board.rounds:
        if round_record.succeeded or round_record.cause is None:
            count, cause = 0, None
        elif round_record.cause == cause:
            count += 1
        else:
            count, cause = 1, round_record.cause
    return count, cause


def _validate_plan_projection(
    board: PlanBoard,
    count: int,
    cause: str | None,
    block_after_same_cause: int,
) -> None:
    """Compare persisted blocked facts with MIRA's fold."""
    if board.blocked_count != count or board.blocked_cause != cause:
        raise BoardReplayError("persisted blocked-round facts disagree with independent replay")
    blocked = count >= block_after_same_cause
    if (board.goal_status is GoalStatus.BLOCKED) != blocked:
        raise BoardReplayError("persisted blocked status disagrees with independent replay")
    if board.autonomous_rounds != len(board.rounds):
        raise BoardReplayError("autonomous round count disagrees with durable round history")


def replay_dependency_selection(  # noqa: PLR0912, PLR0915  # fail closed across evidence checks
    board: DagBoard,
) -> tuple[Id, ...]:
    """Select runnable pending work from a DAG without calling the orchestrator implementation."""
    items = tuple(board.work_items)
    ids = {item.work_id for item in items}
    if len(ids) != len(items):
        raise BoardReplayError("DAG contains duplicate work ids")
    if any(item.run_id != board.run_id for item in items):
        raise BoardReplayError("DAG contains work from another Run")
    ordinals = tuple(item.ordinal for item in items)
    if len(set(ordinals)) != len(ordinals):
        raise BoardReplayError("DAG contains duplicate work ordinals")
    task_ids = tuple(item.task_id for item in items)
    if any(item.task_id is None or item.objective is None for item in items):
        raise BoardReplayError("DAG lacks a published Task invocation binding")
    if len(set(task_ids)) != len(task_ids):
        raise BoardReplayError("DAG contains duplicate Task invocation ids")
    if any(item.task_id == item.work_id for item in items):
        raise BoardReplayError("DAG queue and Task identities must remain distinct")
    if any(dependency not in ids for item in items for dependency in item.dependency_ids):
        raise BoardReplayError("DAG contains an unknown dependency")
    if any(len(item.dependency_ids) != len(set(item.dependency_ids)) for item in items):
        raise BoardReplayError("DAG contains duplicate dependencies")
    visiting: set[Id] = set()
    visited: set[Id] = set()

    def visit(work_id: Id) -> None:
        if work_id in visiting:
            raise BoardReplayError("DAG contains a dependency cycle")
        if work_id in visited:
            return
        visiting.add(work_id)
        item = next(value for value in items if value.work_id == work_id)
        for dependency in item.dependency_ids:
            visit(dependency)
        visiting.remove(work_id)
        visited.add(work_id)

    for item in items:
        visit(item.work_id)
    by_id = {item.work_id: item for item in items}
    for item in items:
        if not item.has_invocation_binding():
            raise BoardReplayError("DAG node lacks an authoritative invocation binding")
        if item.delegation_key != _recompute_delegation_key(item):
            raise BoardReplayError("DAG node delegation key does not match its invocation facts")
        if not item.dependency_ids and item.delegation_depth != 0:
            raise BoardReplayError("root work has a non-zero delegation depth")
        if item.dependency_ids:
            expected_depth = max(by_id[value].delegation_depth for value in item.dependency_ids) + 1
            if item.delegation_depth != expected_depth:
                raise BoardReplayError("DAG delegation depth is not monotonic")
    result_ids = tuple(result.task_id for result in board.results)
    if len(set(result_ids)) != len(result_ids):
        raise BoardReplayError("DAG contains duplicate settlement task ids")
    if any(result.run_id != board.run_id for result in board.results):
        raise BoardReplayError("DAG contains a settlement from another Run")
    if any(task_id not in set(task_ids) for task_id in result_ids):
        raise BoardReplayError("DAG contains a settlement for an unknown Task invocation")
    by_task_id = {item.task_id: item for item in items}
    for result in board.results:
        item = by_task_id[result.task_id]
        if not item.has_invocation_binding():
            raise BoardReplayError("DAG settlement lacks an authoritative invocation binding")
        if (
            result.objective != item.objective
            or result.delegation_depth != item.delegation_depth
            or result.agent_id != item.agent_id
            or result.agent != item.agent
            or result.parent_agent != item.parent_agent
            or result.delegation_key != item.delegation_key
        ):
            raise BoardReplayError("DAG settlement does not match its authoritative invocation")
    settled_tasks = set(result_ids)
    settled_work_ids = {item.work_id for item in items if item.task_id in settled_tasks}
    return tuple(
        item.work_id
        for item in sorted(items, key=lambda value: value.ordinal)
        if item.task_id not in settled_tasks and set(item.dependency_ids).issubset(settled_work_ids)
    )


def replay_boards(
    history: Sequence[PlanBoard],
    dag: DagBoard | None = None,
    *,
    block_after_same_cause: int = 3,
) -> BoardReplay:
    """Replay both board surfaces and return facts MIRA can compare with owner projections."""
    run_id, revision, blocked_count, blocked_cause, blocked = replay_plan_revisions(
        history,
        block_after_same_cause=block_after_same_cause,
    )
    runnable = () if dag is None else replay_dependency_selection(dag)
    if dag is not None and dag.run_id != run_id:
        raise BoardReplayError("plan and DAG evidence belong to different Runs")
    return BoardReplay(
        run_id=run_id,
        plan_revision=revision,
        blocked_count=blocked_count,
        blocked_cause=blocked_cause,
        blocked=blocked,
        runnable_work_ids=runnable,
    )


__all__ = [
    "BoardReplay",
    "BoardReplayError",
    "replay_boards",
    "replay_dependency_selection",
    "replay_plan_revisions",
]
