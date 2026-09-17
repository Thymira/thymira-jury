"""Runtime graph state and subgraph contract tests (RA-CORE-05)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from thymira.core import (
    Phase,
    RuntimeState,
    Subgraph,
    SubgraphDeps,
    deserialize_runtime_state,
    initial_runtime_state,
    serialize_runtime_state,
)
from thymira.core.usage import UsageLedger
from thymira.events import JsonlEventLog, sha256_text
from thymira.policies import Gate, load_default_policy
from thymira.policies.engine import PolicyEngine
from thymira.schemas import Run, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore, LocalRecordRepository
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyProgress
from thymira.tools import ToolManager, ToolRegistry


class _ScriptedSubgraph:
    """Small structural implementation used to prove the protocol boundary."""

    name = "scripted"

    def graph_version(self) -> str:
        """Return a stable version for this scripted graph."""
        return sha256_text("scripted-v1")

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Advance the state without using hidden global dependencies."""
        assert deps.usage_ledger.snapshot()["requests"] == 0
        return state.model_copy(update={"phase": Phase.PREPARATION})


def _deps(tmp_path: Path, run_id: str) -> SubgraphDeps:
    """Build local dependency seams for the isolated protocol test."""
    event_log = JsonlEventLog(tmp_path / "events.jsonl", run_id)
    gate = Gate(PolicyEngine(load_default_policy()), event_log)
    return SubgraphDeps(
        event_log=event_log,
        gate=gate,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        tool_manager=ToolManager(ToolRegistry()),
        record_repository=LocalRecordRepository(tmp_path),
        usage_ledger=UsageLedger(),
    )


def test_scripted_subgraph_implements_protocol_and_updates_phase(tmp_path: Path) -> None:
    """A structural subgraph receives state and dependencies, then returns a new state."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the data",
    )
    state = initial_runtime_state(run)
    subgraph = _ScriptedSubgraph()

    assert isinstance(subgraph, Subgraph)
    updated = subgraph.invoke(state, deps=_deps(tmp_path, run.id))

    assert updated.phase is Phase.PREPARATION
    assert subgraph.graph_version() == sha256_text("scripted-v1")
    assert subgraph.graph_version() == subgraph.graph_version()


def test_runtime_state_serialization_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    """The checkpoint seam preserves validated state and canonical ordering."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the data",
    )
    state = initial_runtime_state(run).model_copy(
        update={"phase": Phase.MODELING, "usage": {"tokens": 12, "requests": 1}}
    )

    first = serialize_runtime_state(state)
    second = serialize_runtime_state(
        state.model_copy(update={"usage": {"requests": 1, "tokens": 12}})
    )

    assert first == second
    assert deserialize_runtime_state(first) == state


def test_runtime_state_round_trips_the_carried_thy_progress() -> None:
    """A parked pass's own record survives the checkpoint, PENDING outcome included."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Profile the data",
    )
    task = AgentTask(
        id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
    )
    progress = ThyProgress(
        plan=(task,),
        completed=(task,),
        agent_messages=(
            Task(
                id=new_id("task"),
                run_id=run.id,
                agent_id=new_id("agent"),
                objective="profile it",
                status=TaskStatus.PENDING,
            ),
        ),
    )
    state = initial_runtime_state(run).model_copy(update={"thy_progress": progress})

    assert deserialize_runtime_state(serialize_runtime_state(state)) == state
