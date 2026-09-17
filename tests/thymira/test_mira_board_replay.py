"""Independent board replay adversarial evidence tests."""

from __future__ import annotations

from hashlib import sha256

import pytest

from thymira.events import canonical_json
from thymira.mira import BoardReplayError, replay_dependency_selection
from thymira.schemas import DagBoard, DagNode, WorkItem, new_id

RUN_ID = "run_" + "0" * 32


def _invocation_key(item: WorkItem) -> str:
    """Build the expected key locally, without importing the producer implementation."""
    assert item.task_id is not None
    assert item.agent_id is not None
    assert item.agent is not None
    assert item.parent_agent is not None
    assert item.objective is not None
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


def _bound_item() -> WorkItem:
    """Return one fully bound child invocation with a valid independently computed key."""
    item = WorkItem(
        work_id=new_id("work"),
        run_id=RUN_ID,
        ordinal=0,
        kind="profile",
        idempotency_key="mira-replay-key",
        task_id=new_id("task"),
        objective="profile the dataset",
        agent_id=new_id("agent"),
        agent="worker",
        parent_agent="root",
    )
    return item.model_copy(update={"delegation_key": _invocation_key(item)})


def test_mira_replay_recomputes_delegation_key_from_node_facts() -> None:
    """A self-consistent forged node key cannot make MIRA accept altered evidence."""
    item = _bound_item().model_copy(update={"delegation_key": "f" * 64})
    board = DagBoard.model_construct(
        run_id=RUN_ID,
        work_items=(DagNode.from_work_item(item),),
        results=(),
    )

    with pytest.raises(BoardReplayError, match="delegation key"):
        replay_dependency_selection(board)
