"""Structural contract for runtime graph subgraphs and their injected dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents.request_ledger import RequestLedger
    from thymira.core.graph.state import RuntimeState
    from thymira.core.usage import UsageLedger
    from thymira.events import EventLog
    from thymira.policies import Gate
    from thymira.schemas import DagBoard, Id, PlanBoard
    from thymira.state import ArtifactStore, BoardRepository, DagRepository, RecordRepository
    from thymira.tools import ToolManager


class _EmptyBoardRepository:
    """Read-only empty evidence source for isolated synthetic subgraph tests.

    The production composition always replaces these defaults with the owner-composed durable
    repositories.  Keeping the concrete no-op source here lets legacy unit subgraphs exercise
    unrelated graph behavior without silently making a partial board claim.
    """

    def get(self, run_id: Id) -> PlanBoard | None:
        """Return no plan for a synthetic graph."""
        del run_id

    def history(self, run_id: Id) -> tuple[PlanBoard, ...]:
        """Return no plan history for a synthetic graph."""
        del run_id
        return ()

    def save(self, board: PlanBoard, *, expected_revision: int) -> PlanBoard:
        """Reject writes because this source is read-only by construction."""
        del board, expected_revision
        raise RuntimeError("synthetic board evidence repository is read-only")


class _EmptyDagRepository:
    """Read-only empty DAG evidence source for isolated synthetic subgraph tests."""

    def get(self, run_id: Id) -> DagBoard | None:
        """Return no DAG for a synthetic graph."""
        del run_id

    def history(self, run_id: Id) -> tuple[DagBoard, ...]:
        """Return no DAG history for a synthetic graph."""
        del run_id
        return ()

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Reject writes because this source is read-only by construction."""
        del board, expected_revision
        raise RuntimeError("synthetic DAG evidence repository is read-only")


@dataclass(frozen=True, slots=True)
class SubgraphDeps:
    """Runtime handles explicitly supplied to a subgraph invocation."""

    event_log: EventLog
    gate: Gate
    artifact_store: ArtifactStore
    tool_manager: ToolManager
    record_repository: RecordRepository
    usage_ledger: UsageLedger
    # Real Core composition injects the owner-shared repositories.  The concrete empty defaults
    # exist only for direct subgraph tests that intentionally have no board evidence.
    plan_repository: BoardRepository = field(default_factory=_EmptyBoardRepository)
    dag_repository: DagRepository = field(default_factory=_EmptyDagRepository)
    request_ledger: RequestLedger | None = None
    before_model_request: Callable[[], Sequence[str]] | None = None
    before_thy_execute: Callable[[], None] | None = None


@runtime_checkable
class Subgraph(Protocol):
    """The structural interface implemented by a THY or MIRA graph adapter."""

    name: str

    def graph_version(self) -> str:
        """Return the stable hash identifying the graph definition."""
        ...

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Process one runtime state and return its updated immutable snapshot."""
        ...


__all__ = ["Subgraph", "SubgraphDeps"]
