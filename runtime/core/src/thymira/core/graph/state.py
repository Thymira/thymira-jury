"""Serializable runtime state passed between the core graph subgraphs."""

from __future__ import annotations

import json
from typing import Any, TypedDict

from pydantic import Field

from thymira.core.phases import Phase
from thymira.events import canonical_json
from thymira.policies import RiskProfile
from thymira.schemas import (
    AuditFinding,
    ExecutionConstraints,
    Id,
    PolicyDecision,
    Run,
    RunStatus,
    Task,
    ThymiraModel,
)
from thymira.thy.models import ReworkSignal, ThyProgress


class RunGraphState(TypedDict):
    """The legacy runtime context used to start the original LangGraph boundary.

    The persisted :class:`~thymira.schemas.Run` remains the lifecycle source of truth. This
    compatibility shape is kept while callers migrate to :class:`RuntimeState`.
    """

    run_id: Id
    session_id: Id
    project_id: Id
    prompt: str
    status: RunStatus


class RuntimeState(ThymiraModel):
    """The neutral, checkpointable state shared by runtime graph subgraphs.

    The model deliberately contains only core-owned phase information and shared schema
    contracts. THY and MIRA keep their internal states private and are adapted at the core
    boundary later by the composition graph.
    """

    run_id: Id
    project_id: Id
    phase: Phase = Phase.UNDERSTANDING
    plan: tuple[Task, ...] = ()
    task_ids: tuple[Id, ...] = ()
    usage: dict[str, int | float | None] = Field(default_factory=dict)
    findings: tuple[AuditFinding, ...] = ()
    decision: PolicyDecision | None = None
    risk_profile: RiskProfile | None = None
    execution_constraints: ExecutionConstraints | None = None
    execution_decision: PolicyDecision | None = None
    rework_signal: ReworkSignal | None = None
    reopen_count: int = Field(default=0, ge=0)
    max_reopens: int = Field(default=2, ge=0)
    rework_refusal: str | None = None
    thy_progress: ThyProgress | None = None
    """THY's own record of a pass that stopped on a tool call awaiting a human, carried under
    THY's name into the next `thy` pass and cleared when a pass finishes."""


def initial_graph_state(run: Run) -> RunGraphState:
    """Build the legacy LangGraph state from a validated runtime ``Run``."""
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "project_id": run.project_id,
        "prompt": run.prompt,
        "status": run.status,
    }


def initial_runtime_state(run: Run) -> RuntimeState:
    """Build the neutral graph state from a validated runtime ``Run``."""
    return RuntimeState(run_id=run.id, project_id=run.project_id)


def serialize_runtime_state(state: RuntimeState) -> str:
    """Encode a runtime state as deterministic canonical JSON for checkpointing."""
    return canonical_json(state.to_json_dict())


def deserialize_runtime_state(payload: str) -> RuntimeState:
    """Decode a canonical JSON checkpoint payload into a validated runtime state."""
    raw: Any = json.loads(payload)
    if not isinstance(raw, dict):
        msg = "runtime state checkpoint must contain a JSON object"
        raise TypeError(msg)
    return RuntimeState.model_validate(raw)


__all__ = [
    "RunGraphState",
    "RuntimeState",
    "deserialize_runtime_state",
    "initial_graph_state",
    "initial_runtime_state",
    "serialize_runtime_state",
]
