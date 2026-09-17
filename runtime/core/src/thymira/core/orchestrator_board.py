"""Root-owned CAS/DAG topology over the lifecycle owner's WorkItem boundary.

The board persists dependency topology, delegation depth and typed settlement projections.  The
lifecycle repository remains the only WorkItem claim and effect-settlement writer; this module
never creates a second queue or stores a second claim token.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from thymira.agents.settlement import select_canonical
from thymira.events import canonical_json
from thymira.schemas import (
    DagBoard,
    DagNode,
    DispatchState,
    ExecutionOutcomeKind,
    Id,
    RunOwnerHandle,
    StopReason,
    SubagentResult,
    WorkClaim,
    WorkItem,
    WorkResult,
    WorkState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.state import DagRepository


class LifecycleWorkWriter(Protocol):
    """The lifecycle owner commands consumed by the orchestrator board."""

    def get_work(self, work_id: str) -> WorkItem | None:
        """Read the lifecycle-published source before projecting its topology."""
        ...

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Read the lifecycle owner's canonical settlement for one work item."""
        ...

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        """Claim one lifecycle-published WorkItem."""
        ...

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        """Record dispatch through the live lifecycle owner."""
        ...

    def settle_work(self, claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> WorkItem:
        """Persist the lifecycle execution outcome through the live owner."""
        ...


class OrchestratorBoardError(RuntimeError):
    """Base error for invalid orchestrator board operations."""


class BoardAuthorityError(OrchestratorBoardError):
    """Raised when a non-root caller attempts a topology mutation or peer message."""


class BoardDependencyError(OrchestratorBoardError):
    """Raised when a work item references an unknown node or introduces a cycle."""


class OrchestratorBoard:
    """Own topology and settlement projection while lifecycle owns executable work state."""

    def __init__(
        self,
        repository: DagRepository,
        run_id: Id,
        *,
        lifecycle: LifecycleWorkWriter,
        owner: RunOwnerHandle,
        owner_assertion: Callable[[RunOwnerHandle], None],
        root_owner_id: str = "root",
    ) -> None:
        if not root_owner_id.strip():
            raise ValueError("root_owner_id must not be empty")
        if owner.run_id != run_id:
            raise ValueError("live owner belongs to another Run")
        self._repository = repository
        self._run_id = run_id
        self._lifecycle = lifecycle
        self._owner = owner
        self._owner_assertion = owner_assertion
        self._root_owner_id = root_owner_id

    def snapshot(self) -> DagBoard:
        """Return the current topology snapshot, or an empty revision-zero board."""
        board = self._repository.get(self._run_id)
        if board is None:
            return DagBoard(run_id=self._run_id)
        return board

    @property
    def revision(self) -> int:
        """Return the durable topology CAS revision."""
        return self.snapshot().revision

    def add_work_item(
        self,
        item: WorkItem,
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> DagBoard:
        """Register a lifecycle-published WorkItem's immutable topology."""
        self._require_root(owner_id)
        self._assert_live_owner()
        if item.run_id != self._run_id:
            raise ValueError("work item belongs to another Run")
        if item.state is not WorkState.PENDING:
            raise OrchestratorBoardError("new topology nodes must reference pending work")
        published = self._lifecycle.get_work(item.work_id)
        if published is None or published != item:
            raise OrchestratorBoardError(
                "DAG topology must project the exact lifecycle-published WorkItem"
            )
        current = self.snapshot()
        expected = current.revision if expected_revision is None else expected_revision
        node = DagNode.from_work_item(item)
        if not node.has_invocation_binding():
            raise OrchestratorBoardError(
                "lifecycle WorkItem lacks its immutable Task invocation binding"
            )
        if node.task_id == node.work_id:
            raise OrchestratorBoardError("queue and Task identities must remain distinct")
        self._validate_node_invocation_key(node)
        if any(existing.work_id == node.work_id for existing in current.work_items):
            raise BoardDependencyError(f"work item {node.work_id} already exists")
        nodes = (*current.work_items, node)
        self._validate_dag(nodes)
        self._validate_depth(nodes)
        return self._save(current, work_items=nodes, expected_revision=expected)

    def add_work(
        self,
        item: WorkItem,
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> DagBoard:
        """Register work that the lifecycle repository already published durably."""
        return self.add_work_item(item, owner_id=owner_id, expected_revision=expected_revision)

    def claim(
        self,
        work_id: Id,
        *,
        claimant_id: str,
        owner_id: str = "root",
    ) -> WorkClaim:
        """Claim one runnable node through the lifecycle owner repository."""
        self._require_root(owner_id)
        self._assert_live_owner()
        if not claimant_id.strip():
            raise ValueError("claimant_id must not be empty")
        current = self.snapshot()
        node = self._node(current, work_id)
        if node not in current.pending():
            settled_work_ids = {
                value.work_id
                for value in current.work_items
                if value.task_id in {result.task_id for result in current.results}
            }
            if any(value.work_id == work_id for value in current.work_items) and not set(
                node.dependency_ids
            ).issubset(settled_work_ids):
                raise BoardDependencyError(f"work item {work_id} has unsettled dependencies")
            raise OrchestratorBoardError(f"work item {work_id} is not runnable")
        claim = self._lifecycle.claim_work(work_id, claimant_id, limit=1)
        if claim is None:
            raise OrchestratorBoardError(f"work item {work_id} is not claimable")
        if claim.run_id != self._run_id or claim.work_id != node.work_id:
            raise OrchestratorBoardError("lifecycle claim is not bound to this Run and node")
        self._lifecycle.mark_dispatched(claim, self._owner)
        return claim

    def settle(
        self,
        claim: WorkClaim,
        *,
        result: SubagentResult,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> SubagentResult:
        """Settle the lifecycle claim, then append its one typed result to the CAS board."""
        self._require_root(owner_id)
        self._assert_live_owner()
        current = self.snapshot()
        if expected_revision is not None and expected_revision != current.revision:
            raise OrchestratorBoardError(
                f"expected DAG revision {expected_revision}, current is {current.revision}"
            )
        node = self._node(current, claim.work_id)
        prior = next((value for value in current.results if value.task_id == node.task_id), None)
        if prior is not None:
            if prior != result:
                raise OrchestratorBoardError("a work item already has a different settlement")
            return prior
        if (
            claim.run_id != self._run_id
            or result.run_id != self._run_id
            or result.task_id != node.task_id
            or result.objective != node.objective
            or result.delegation_depth != node.delegation_depth
        ):
            raise OrchestratorBoardError("child result is not bound to the claimed work item")
        self._validate_result_binding(node, result, claim=claim)
        work_result = self._work_result(result, node, claim)
        settled = self._lifecycle.settle_work(claim, work_result, self._owner)
        if (
            settled.run_id != self._run_id
            or settled.work_id != claim.work_id
            or settled.state not in (WorkState.SETTLED, WorkState.SETTLED.value)
        ):
            raise OrchestratorBoardError("lifecycle owner did not return the settled work item")
        canonical_result = self._lifecycle.get_work_result(claim.work_id)
        if canonical_result is None or canonical_result.kind != work_result.kind:
            raise OrchestratorBoardError(
                "lifecycle owner did not expose the canonical settlement result"
            )
        self._validate_canonical_work_result(
            canonical_result,
            node,
            expected_attempt=claim.attempt,
            expected_claim_token=claim.claim_token,
        )
        canonical_child = self._canonical_child(canonical_result)
        if canonical_child != result or canonical_result != work_result:
            raise OrchestratorBoardError(
                "lifecycle owner returned a settlement with a different source binding"
            )
        expected = current.revision if expected_revision is None else expected_revision
        self._append_settlement(current, result, expected_revision=expected)
        return result

    def recover_settlement(
        self,
        work_id: Id,
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> SubagentResult:
        """Rebuild a missing DAG result from the owner's canonical settled work record.

        This is the restart path after the owner settled a claim but a topology CAS remained
        unavailable.  It never reclaims or settles the work a second time.  The lifecycle record
        is the source of truth and must carry the complete validated child plus its source facts.
        """
        self._require_root(owner_id)
        self._assert_live_owner()
        current = self.snapshot()
        if expected_revision is not None and expected_revision != current.revision:
            raise OrchestratorBoardError(
                f"expected DAG revision {expected_revision}, current is {current.revision}"
            )
        node = self._node(current, work_id)
        canonical = self._lifecycle.get_work_result(work_id)
        if canonical is None:
            raise OrchestratorBoardError("owner has no canonical settlement for this work item")
        published = self._lifecycle.get_work(work_id)
        if (
            published is None
            or published.state not in (WorkState.SETTLED, WorkState.SETTLED.value)
            or DagNode.from_work_item(published) != node
        ):
            raise OrchestratorBoardError(
                "owner work record no longer matches the authoritative DAG invocation"
            )
        self._validate_canonical_work_result(
            canonical,
            node,
            expected_attempt=published.attempt,
            expected_claim_token=published.claim_token,
        )
        child = self._canonical_child(canonical)
        self._validate_result_binding(node, child)
        prior = next((value for value in current.results if value.task_id == node.task_id), None)
        if prior is not None:
            if prior != child:
                raise OrchestratorBoardError("a work item already has a different settlement")
            return prior
        expected = current.revision if expected_revision is None else expected_revision
        self._append_settlement(current, child, expected_revision=expected)
        return child

    def ordered_results(self) -> tuple[SubagentResult, ...]:
        """Return settled results by durable topology ordinal, independent of finish order."""
        board = self.snapshot()
        ordinals = {item.task_id: item.ordinal for item in board.work_items}
        return tuple(sorted(board.results, key=lambda value: ordinals[value.task_id]))

    def settlement_notice(self, work_id: Id) -> SubagentResult | None:
        """Read and validate one owner settlement before exposing it to the parent."""
        board = self.snapshot()
        node = self._node(board, work_id)
        owner_result = self._lifecycle.get_work_result(work_id)
        if owner_result is None:
            if any(value.task_id == node.task_id for value in board.results):
                raise OrchestratorBoardError(
                    "settled board work has no durable parent-inbox notice"
                )
            return None
        published = self._lifecycle.get_work(work_id)
        if (
            published is None
            or published.state not in (WorkState.SETTLED, WorkState.SETTLED.value)
            or DagNode.from_work_item(published) != node
        ):
            raise OrchestratorBoardError(
                "owner work record no longer matches the authoritative DAG invocation"
            )
        self._validate_canonical_work_result(
            owner_result,
            node,
            expected_attempt=published.attempt,
            expected_claim_token=published.claim_token,
        )
        notice = self._canonical_child(owner_result)
        self._validate_result_binding(node, notice)
        projected = next((value for value in board.results if value.task_id == node.task_id), None)
        if projected is None or projected != notice:
            raise OrchestratorBoardError(
                "parent-inbox notice and orchestrator-board settlement disagree"
            )
        return notice

    def canonical_settlement(self, delegation_key: str) -> SubagentResult | None:
        """Select one canonical durable parent result after independent owner reconciliation."""
        if not delegation_key.strip():
            raise ValueError("delegation_key must not be empty")
        candidates = tuple(
            notice
            for node in self.snapshot().work_items
            if (notice := self.settlement_notice(node.work_id)) is not None
            and notice.delegation_key == delegation_key
        )
        return select_canonical(candidates)

    def recover_settlements(self, *, owner_id: str = "root") -> tuple[SubagentResult, ...]:
        """Reconcile every owner settlement before returning the parent's ordered view."""
        self._require_root(owner_id)
        self._assert_live_owner()
        for node in self.snapshot().work_items:
            if self._lifecycle.get_work_result(node.work_id) is not None:
                self.recover_settlement(node.work_id, owner_id=owner_id)
        return self.ordered_results()

    def peer_message(self, *_args: object, **_kwargs: object) -> None:
        """Reject sibling messaging so the orchestrator remains the only peer boundary."""
        raise BoardAuthorityError("peer messaging is not a lifecycle capability")

    def _save(
        self,
        current: DagBoard,
        *,
        work_items: tuple[DagNode, ...] | None = None,
        results: tuple[SubagentResult, ...] | None = None,
        expected_revision: int,
    ) -> DagBoard:
        """Build one strictly newer topology snapshot and persist it through repository CAS."""
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "work_items": current.work_items if work_items is None else work_items,
                "results": current.results if results is None else results,
            }
        )
        return self._repository.save(replacement, expected_revision=expected_revision)

    def _append_settlement(
        self,
        current: DagBoard,
        result: SubagentResult,
        *,
        expected_revision: int,
    ) -> DagBoard:
        """Publish an owner-settled result with bounded idempotent CAS recovery.

        Lifecycle settlement is authoritative and happens first.  A competing topology writer
        may win the following CAS, so reload and retry the append; if the result is already
        present, recovery returns successfully without settling the claim a second time.
        """
        candidate = current
        expected = expected_revision
        for _ in range(3):
            try:
                return self._save(
                    candidate,
                    results=(*candidate.results, result),
                    expected_revision=expected,
                )
            except ValueError as exc:
                latest = self.snapshot()
                prior = next(
                    (value for value in latest.results if value.task_id == result.task_id),
                    None,
                )
                if prior is not None:
                    if prior != result:
                        raise OrchestratorBoardError(
                            "a work item already has a different settlement"
                        ) from exc
                    return latest
                candidate = latest
                expected = latest.revision
        raise OrchestratorBoardError(
            "DAG CAS did not publish the owner settlement after three attempts; "
            "lifecycle settlement remains authoritative"
        )

    def _assert_live_owner(self) -> None:
        """Require the owner composition's non-serializable live proof before every mutation."""
        try:
            self._owner_assertion(self._owner)
        except (PermissionError, RuntimeError, ValueError) as exc:
            raise BoardAuthorityError("DAG mutation requires the live Run owner") from exc

    def _require_root(self, owner_id: str) -> None:
        """Require the configured root orchestrator identity."""
        if owner_id != self._root_owner_id:
            raise BoardAuthorityError("only the root orchestrator may mutate the DAG board")

    @staticmethod
    def _node(board: DagBoard, work_id: Id) -> DagNode:
        """Find one topology node or raise a stable board error."""
        for item in board.work_items:
            if item.work_id == work_id:
                return item
        raise OrchestratorBoardError(f"work item {work_id} does not exist")

    @staticmethod
    def _validate_dag(items: tuple[DagNode, ...]) -> None:
        """Reject missing dependency nodes and cycles before a topology snapshot is persisted."""
        ids = {item.work_id for item in items}
        if any(dependency not in ids for item in items for dependency in item.dependency_ids):
            raise BoardDependencyError("dependency references an unknown work item")
        edges = {item.work_id: item.dependency_ids for item in items}
        visiting: set[Id] = set()
        visited: set[Id] = set()

        def visit(node: Id) -> None:
            if node in visiting:
                raise BoardDependencyError("work dependencies must form a DAG")
            if node in visited:
                return
            visiting.add(node)
            for dependency in edges[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in ids:
            visit(node)

    @staticmethod
    def _validate_depth(items: tuple[DagNode, ...]) -> None:
        """Require immutable node depth to derive from its persisted parent topology."""
        by_id = {item.work_id: item for item in items}
        for item in items:
            if not item.dependency_ids and item.delegation_depth != 0:
                raise BoardDependencyError("root work must have delegation depth zero")
            if item.dependency_ids:
                expected = max(by_id[value].delegation_depth for value in item.dependency_ids) + 1
                if item.delegation_depth != expected:
                    raise BoardDependencyError(
                        "delegation depth must equal its deepest parent depth plus one"
                    )

    @staticmethod
    def _work_result(result: SubagentResult, node: DagNode, claim: WorkClaim) -> WorkResult:
        """Translate a child stop reason into the lifecycle execution-outcome vocabulary."""
        kind = {
            StopReason.COMPLETED: ExecutionOutcomeKind.SUCCEEDED,
            StopReason.FAILED: ExecutionOutcomeKind.FAILED,
            StopReason.ABNORMAL: ExecutionOutcomeKind.UNKNOWN,
            StopReason.STOPPED: ExecutionOutcomeKind.CANCELLED,
            StopReason.OUT_OF_ROOM: ExecutionOutcomeKind.CANCELLED,
            StopReason.DECLINED: ExecutionOutcomeKind.CANCELLED,
        }[result.stop_reason]
        if not node.has_invocation_binding():
            raise OrchestratorBoardError("work item is missing its immutable invocation binding")
        return WorkResult(
            kind=kind,
            dispatch_state=DispatchState.EFFECT_CONFIRMED,
            source_work_id=node.work_id,
            source_run_id=node.run_id,
            source_attempt=claim.attempt,
            source_claim_token=claim.claim_token,
            source_ordinal=node.ordinal,
            source_kind=node.kind,
            source_task_id=node.task_id,
            source_objective=node.objective,
            source_delegation_depth=node.delegation_depth,
            source_agent_id=node.agent_id,
            source_agent=node.agent,
            source_parent_agent=node.parent_agent,
            source_delegation_key=node.delegation_key,
            subagent_result=result,
        )

    @staticmethod
    def _canonical_child(canonical: WorkResult) -> SubagentResult:
        """Read the full child settlement from the owner record, failing closed when absent."""
        if canonical.subagent_result is None:
            raise OrchestratorBoardError(
                "canonical owner settlement does not contain a SubagentResult"
            )
        return canonical.subagent_result

    @staticmethod
    def _validate_canonical_work_result(
        canonical: WorkResult,
        node: DagNode,
        *,
        expected_attempt: int,
        expected_claim_token: str | None,
    ) -> None:
        """Require the owner's durable envelope to retain every published invocation fact."""
        child = canonical.subagent_result
        if (
            child is None
            or canonical.source_work_id != node.work_id
            or canonical.source_run_id != node.run_id
            or canonical.source_ordinal != node.ordinal
            or canonical.source_kind != node.kind
            or canonical.source_task_id != node.task_id
            or canonical.source_objective != node.objective
            or canonical.source_delegation_depth != node.delegation_depth
            or canonical.source_agent_id != node.agent_id
            or canonical.source_agent != node.agent
            or canonical.source_parent_agent != node.parent_agent
            or canonical.source_delegation_key != node.delegation_key
            or canonical.source_attempt != expected_attempt
            or canonical.source_claim_token != expected_claim_token
            or canonical.dispatch_state is not DispatchState.EFFECT_CONFIRMED
        ):
            raise OrchestratorBoardError(
                "canonical owner settlement does not match the authoritative work invocation"
            )

    def _validate_result_binding(
        self,
        node: DagNode,
        result: SubagentResult,
        *,
        claim: WorkClaim | None = None,
    ) -> None:
        """Require every result identity to match the authoritative topology and claim."""
        if not node.has_invocation_binding():
            raise OrchestratorBoardError("work item is missing its immutable invocation binding")
        self._validate_node_invocation_key(node)
        if (
            result.run_id != self._run_id
            or result.task_id != node.task_id
            or result.objective != node.objective
            or result.delegation_depth != node.delegation_depth
            or result.agent_id != node.agent_id
            or result.agent != node.agent
            or result.parent_agent != node.parent_agent
            or result.delegation_key != node.delegation_key
        ):
            raise OrchestratorBoardError(
                "child result is not bound to the authoritative invocation"
            )
        if claim is not None and (
            claim.run_id != node.run_id or claim.work_id != node.work_id or claim.attempt < 1
        ):
            raise OrchestratorBoardError("claim is not bound to the authoritative work item")

    @staticmethod
    def _delegation_key(
        *,
        run_id: str,
        task_id: str,
        agent_id: str,
        parent_agent: str,
        agent: str,
        objective: str,
        delegation_depth: int,
    ) -> str:
        """Recompute invocation identity without trusting the worker's delegation helper."""
        return sha256(
            canonical_json(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "parent_agent": parent_agent,
                    "agent": agent,
                    "objective": objective,
                    "delegation_depth": delegation_depth,
                }
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _validate_node_invocation_key(cls, node: DagNode) -> None:
        """Require the owner-published node key to match its immutable invocation facts."""
        if (
            node.task_id is None
            or node.objective is None
            or node.agent_id is None
            or node.agent is None
            or node.parent_agent is None
            or node.delegation_key is None
        ):
            raise OrchestratorBoardError("work item is missing its immutable invocation binding")
        expected = cls._delegation_key(
            run_id=node.run_id,
            task_id=node.task_id,
            agent_id=node.agent_id,
            parent_agent=node.parent_agent,
            agent=node.agent,
            objective=node.objective,
            delegation_depth=node.delegation_depth,
        )
        if node.delegation_key != expected:
            raise OrchestratorBoardError(
                "authoritative work item delegation key does not match its invocation facts"
            )


@dataclass(frozen=True, slots=True)
class RepeatDecision:
    """Decision returned by the repeat guard after one canonical tool call."""

    tool_name: str
    arguments_sha256: str
    count: int
    reminder_level: int
    blocked: bool
    message: str


__all__ = [
    "BoardAuthorityError",
    "BoardDependencyError",
    "OrchestratorBoard",
    "OrchestratorBoardError",
    "RepeatDecision",
]
