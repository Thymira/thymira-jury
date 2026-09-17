"""Unit evidence for the durable goal and orchestrator boards."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from thymira.agents.settlement import delegation_key as compute_delegation_key
from thymira.core import (
    AutonomousRoundLimitError,
    BoardDependencyError,
    GoalAuthorityError,
    GoalBoardConfig,
    GoalBoardService,
    GoalRevisionConflictError,
    OrchestratorBoard,
    OrchestratorBoardError,
)
from thymira.events import canonical_json
from thymira.mira import (
    BoardReplayError,
    replay_boards,
    replay_dependency_selection,
    replay_plan_revisions,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    AuthorityProof,
    ControlInput,
    ControlInputKind,
    DagBoard,
    DagNode,
    DispatchState,
    InboxLane,
    PlanBoard,
    PlanReviewDecision,
    RunOwnerHandle,
    StopReason,
    SubagentResult,
    TodoItem,
    TodoState,
    WorkClaim,
    WorkItem,
    WorkResult,
    new_id,
)
from thymira.state import (
    BoardConflictError,
    HmacAuthorityVerifier,
    LocalArtifactStore,
    LocalBoardRepository,
    LocalDagRepository,
    sign_authority,
)

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run_" + "0" * 32
AUTH_SECRET = b"goal-board-test-secret"


class _LifecycleStub:
    """Minimal owner repository seam proving the board delegates queue writes."""

    def __init__(self, items: tuple[WorkItem, ...]) -> None:
        self.items = {item.work_id: item for item in items}
        self.claims: dict[str, WorkClaim] = {}
        self.results: dict[str, WorkResult] = {}

    def get_work(self, work_id: str) -> WorkItem | None:
        """Return the owner-published source item for topology projection."""
        return self.items.get(work_id)

    def claim_work(self, work_id: str, claimant_id: str, limit: int = 1) -> WorkClaim | None:
        if limit != 1 or work_id in self.claims:
            return None
        item = self.items[work_id]
        if item.state.value != "pending":
            return None
        claim = WorkClaim(
            work_id=work_id,
            run_id=RUN_ID,
            claimant_id=claimant_id,
            attempt=item.attempt + 1,
            claim_token=f"claim-{work_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        self.claims[work_id] = claim
        self.items[work_id] = item.model_copy(update={"state": "claimed"})
        return claim

    def mark_dispatched(self, claim: WorkClaim, owner: RunOwnerHandle) -> WorkItem:
        del owner
        return self.items[claim.work_id]

    def settle_work(self, claim: WorkClaim, result: WorkResult, owner: RunOwnerHandle) -> WorkItem:
        del owner
        self.results[claim.work_id] = result
        item = self.items[claim.work_id]
        settled = item.model_copy(
            update={
                "state": "settled",
                "attempt": claim.attempt,
                "claim_owner": claim.claimant_id,
                "claim_token": claim.claim_token,
                "claim_expires_at": None,
                "dispatch_state": DispatchState.EFFECT_CONFIRMED,
            }
        )
        self.items[claim.work_id] = settled
        return settled

    def get_work_result(self, work_id: str) -> WorkResult | None:
        """Return the canonical owner settlement projection."""
        return self.results.get(work_id)


def _artifact_factory(root: Path):
    """Build an artifact store factory for the one test Run."""
    return lambda run_id: LocalArtifactStore(root / "artifacts" / run_id, run_id)


def _authority_verifier() -> HmacAuthorityVerifier:
    """Build the trusted API composition verifier used by owner command tests."""
    return HmacAuthorityVerifier(
        AUTH_SECRET,
        issuer_process_id="api_1",
        issuer_key_id="key_1",
    )


def _plan_command(
    operation: str,
    *,
    goal: str | None = None,
    causal_proof: str | None = None,
    expected_revision: int | None = None,
    decision: PlanReviewDecision | None = None,
    feedback: str | None = None,
) -> ControlInput:
    """Create a command whose proof binds its exact plan payload."""
    actor = Actor(
        kind=ActorKind.HUMAN,
        id="alice",
        role="root",
        authenticated=True,
    )
    payload = {
        "operation": operation,
        "goal": goal,
        "causal_proof": causal_proof,
        "expected_revision": expected_revision,
        "decision": decision.value if decision is not None else None,
        "feedback": feedback,
    }
    if operation in {"goal.create", "goal.edit"}:
        payload = {
            "operation": operation,
            "goal": goal,
            "causal_proof": causal_proof,
            "expected_revision": expected_revision,
        }
    elif operation == "plan.review":
        payload = {
            "operation": operation,
            "decision": decision.value if decision is not None else None,
            "feedback": feedback,
            "expected_revision": expected_revision,
        }
    session_id = new_id("session")
    input_id = new_id("input")
    proof = AuthorityProof(
        proof_id=new_id("proof"),
        actor=actor,
        authenticated_principal_id="alice",
        authenticated_role="root",
        permissions=("goal:write",),
        session_id=session_id,
        run_id=RUN_ID,
        issuer_process_id="api_1",
        issuer_key_id="key_1",
        request_id=f"request-{operation}",
        signature="signed-command",
        input_id=input_id,
        payload_sha256=hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        binding_sha256="0" * 64,
        requested_lane=InboxLane.NEXT_TURN,
        kind=ControlInputKind.PLAN,
        idempotency_key=f"{operation}-{input_id}",
    )
    command = ControlInput(
        input_id=input_id,
        run_id=RUN_ID,
        session_id=session_id,
        requested_lane=InboxLane.NEXT_TURN,
        kind=ControlInputKind.PLAN,
        payload=payload,
        authority=proof,
        idempotency_key=proof.idempotency_key,
    )
    bound = proof.model_copy(update={"binding_sha256": command.binding_sha256()})
    return command.model_copy(update={"authority": sign_authority(bound, AUTH_SECRET)})


def _result(item: WorkItem) -> SubagentResult:
    """Create the already-validated settlement record a worker returns to the root."""
    assert item.task_id is not None
    assert item.objective is not None
    assert item.agent_id is not None
    assert item.agent is not None
    assert item.parent_agent is not None
    assert item.delegation_key is not None
    return SubagentResult(
        run_id=RUN_ID,
        task_id=item.task_id,
        agent_id=item.agent_id,
        agent=item.agent,
        parent_agent=item.parent_agent,
        objective=item.objective,
        delegation_depth=item.delegation_depth,
        delegation_key=item.delegation_key,
        stop_reason=StopReason.COMPLETED,
        result_json="{}",
        result_schema="builtins:dict",
        diagnostics_limit=4000,
    )


def _work_item(
    *,
    ordinal: int,
    kind: str,
    objective: str,
    idempotency_key: str,
    dependency_ids: tuple[str, ...] = (),
    delegation_depth: int = 0,
) -> WorkItem:
    """Build a queue item with an independently bound Task invocation."""
    task_id = new_id("task")
    agent_id = new_id("agent")
    parent_agent = "root"
    agent = "worker"
    return WorkItem(
        work_id=new_id("work"),
        task_id=task_id,
        run_id=RUN_ID,
        ordinal=ordinal,
        kind=kind,
        objective=objective,
        idempotency_key=idempotency_key,
        dependency_ids=dependency_ids,
        delegation_depth=delegation_depth,
        agent_id=agent_id,
        agent=agent,
        parent_agent=parent_agent,
        delegation_key=compute_delegation_key(
            run_id=RUN_ID,
            task_id=task_id,
            agent_id=agent_id,
            parent_agent=parent_agent,
            agent=agent,
            objective=objective,
            delegation_depth=delegation_depth,
        ),
    )


def test_todo_replacement_retains_in_progress_and_completion(tmp_path: Path) -> None:
    """A full todo replacement cannot drop pending in-progress work or resurrect completion."""
    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "boards"),
        artifact_store_factory=_artifact_factory(tmp_path),
    )
    first = TodoItem(ordinal=0, text="profile")
    second = TodoItem(ordinal=1, text="model", state=TodoState.IN_PROGRESS)

    service.publish_plan(RUN_ID, [first, second], artifact_content="profile then model")
    published_again = service.publish_plan(
        RUN_ID,
        [first, second.model_copy(update={"state": TodoState.COMPLETED})],
        artifact_content="the model item remains owned by the active run",
    )
    assert {item.item_id for item in published_again.items} == {first.item_id, second.item_id}
    assert (
        next(item for item in published_again.items if item.item_id == second.item_id).state
        is TodoState.IN_PROGRESS
    )
    replaced = service.write_todo(RUN_ID, [first])
    assert {item.item_id for item in replaced.items} == {first.item_id, second.item_id}
    assert (
        next(item for item in replaced.items if item.item_id == second.item_id).state
        is TodoState.IN_PROGRESS
    )

    completed = service.complete_item(RUN_ID, first.item_id, summary="profiled")
    replaced_again = service.write_todo(
        RUN_ID,
        [first.model_copy(update={"state": TodoState.TODO}), second],
    )
    assert (
        next(item for item in replaced_again.items if item.item_id == first.item_id).state
        is TodoState.COMPLETED
    )
    assert len(completed.completions) == 1


def test_goal_mutation_requires_bound_authenticated_human_and_exact_revision(
    tmp_path: Path,
) -> None:
    """Create/edit records actor proof and rejects stale or non-human mutations."""
    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "boards"),
        authority_verifier=_authority_verifier(),
    )
    authority = _plan_command(
        "goal.create",
        goal="minimize false negatives",
        causal_proof="request-1",
        expected_revision=0,
    )
    created = service.create_goal(
        RUN_ID,
        "minimize false negatives",
        command=authority,
        causal_proof="request-1",
    )
    assert created.goal == "minimize false negatives"
    assert created.goal_revision is not None
    assert created.goal_revision.actor.id == "alice"
    with pytest.raises(GoalRevisionConflictError):
        service.edit_goal(
            RUN_ID,
            "stale",
            command=_plan_command(
                "goal.edit",
                goal="stale",
                causal_proof="request-2",
                expected_revision=0,
            ),
            causal_proof="request-2",
            expected_revision=0,
        )

    anonymous = authority.model_copy(
        update={
            "authority": authority.authority.model_copy(
                update={
                    "actor": Actor(kind=ActorKind.AGENT, id="agent_1", authenticated=True),
                    "authenticated_principal_id": "agent_1",
                }
            )
        }
    )
    with pytest.raises(GoalAuthorityError):
        service.edit_goal(
            RUN_ID,
            "agent goal",
            command=anonymous,
            causal_proof="request-3",
            expected_revision=created.revision,
        )


def test_goal_mutation_rejects_missing_or_tampered_trusted_verifier(tmp_path: Path) -> None:
    """A caller cannot mint root authority by constructing a matching serializable command."""
    command = _plan_command(
        "goal.create",
        goal="protected",
        causal_proof="human-request",
        expected_revision=0,
    )
    with pytest.raises(GoalAuthorityError, match="verification is not configured"):
        GoalBoardService(LocalBoardRepository(tmp_path / "untrusted")).create_goal(
            RUN_ID,
            "protected",
            command=command,
            causal_proof="human-request",
        )

    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "verified"),
        authority_verifier=_authority_verifier(),
    )
    forged = command.model_copy(
        update={"authority": command.authority.model_copy(update={"signature": "forged"})}
    )
    with pytest.raises(GoalAuthorityError, match="signature is not trusted"):
        service.create_goal(
            RUN_ID,
            "protected",
            command=forged,
            causal_proof="human-request",
        )


def test_rounds_block_only_after_same_cause_and_respect_bound(tmp_path: Path) -> None:
    """Changing a cause resets the consecutive count and rounds have a hard upper bound."""
    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "boards"),
        config=GoalBoardConfig(block_after_same_cause=3, max_autonomous_rounds=5),
    )
    first = service.record_round(RUN_ID, cause="network")
    second = service.record_round(RUN_ID, cause="schema")
    third = service.record_round(RUN_ID, cause="schema")
    assert first.blocked_count == 1
    assert second.blocked_count == 1
    assert third.blocked_count == 2
    blocked = service.record_round(RUN_ID, cause="schema")
    assert blocked.blocked_count == 3
    assert blocked.goal_status.value == "blocked"
    recovered = service.record_round(RUN_ID, cause="new-cause", succeeded=True)
    assert recovered.goal_status.value == "none"
    with pytest.raises(AutonomousRoundLimitError):
        service.record_round(RUN_ID, cause="schema")


def test_mira_replays_plan_facts_and_dag_selection_independently(tmp_path: Path) -> None:
    """MIRA recomputes revision, blocked count and runnable dependencies from evidence."""
    repository = LocalBoardRepository(tmp_path / "boards")
    service = GoalBoardService(
        repository,
        config=GoalBoardConfig(block_after_same_cause=2),
    )
    first = service.record_round(RUN_ID, cause="schema")
    blocked = service.record_round(RUN_ID, cause="schema")
    history = repository.history(RUN_ID)
    assert tuple(snapshot.revision for snapshot in history) == (first.revision, blocked.revision)
    upstream = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="upstream",
    )
    downstream = _work_item(
        ordinal=1,
        kind="model",
        objective="fit the model",
        idempotency_key="downstream",
        dependency_ids=(upstream.work_id,),
        delegation_depth=1,
    )
    replay = replay_boards(
        (first, blocked),
        DagBoard(
            run_id=RUN_ID,
            work_items=(DagNode.from_work_item(upstream), DagNode.from_work_item(downstream)),
        ),
        block_after_same_cause=2,
    )
    assert replay.plan_revision == blocked.revision
    assert replay.blocked_count == 2
    assert replay.blocked
    assert replay.runnable_work_ids == (upstream.work_id,)

    forged = blocked.model_copy(update={"blocked_count": 1})
    with pytest.raises(BoardReplayError):
        replay_boards((first, forged), block_after_same_cause=2)

    other_run_result = _result(upstream).model_copy(update={"run_id": "run_" + "1" * 32})
    with pytest.raises(BoardReplayError, match="another Run"):
        replay_boards(
            (first, blocked),
            DagBoard.model_construct(
                run_id=RUN_ID,
                work_items=(DagNode.from_work_item(upstream), DagNode.from_work_item(downstream)),
                results=(other_run_result,),
            ),
            block_after_same_cause=2,
        )


def test_repeated_canonical_calls_remind_on_canonical_arguments(tmp_path: Path) -> None:
    """Canonical JSON argument order is stable and repeated calls escalate reminders."""
    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "boards"),
        config=GoalBoardConfig(block_after_repeats=3),
    )
    service.observe_tool_call(RUN_ID, "read_file", {"path": "a", "encoding": "utf-8"})
    reminder = service.observe_tool_call(
        RUN_ID,
        "read_file",
        {"encoding": "utf-8", "path": "a"},
    )
    assert reminder.count == 2
    assert reminder.reminder_level == 1
    blocked = service.observe_tool_call(
        RUN_ID,
        "read_file",
        {"path": "a", "encoding": "utf-8"},
    )
    assert blocked.blocked


def test_plan_feedback_is_tool_result_and_explicit_review_leaves_mode(tmp_path: Path) -> None:
    """Feedback never authorizes execution; only an explicit human review changes plan mode."""
    service = GoalBoardService(
        LocalBoardRepository(tmp_path / "boards"),
        artifact_store_factory=_artifact_factory(tmp_path),
        authority_verifier=_authority_verifier(),
    )
    service.publish_plan(RUN_ID, [TodoItem(ordinal=0, text="profile")], artifact_content="plan")
    authority = _plan_command(
        "plan.review",
        decision=PlanReviewDecision.CONTINUE_PLANNING,
        feedback="add a leakage check",
        expected_revision=None,
    )
    feedback = service.review_plan(
        RUN_ID,
        command=authority,
        decision=PlanReviewDecision.CONTINUE_PLANNING,
        feedback="add a leakage check",
    )
    assert feedback.kind == "tool_result"
    assert feedback.execution_authorized is False
    planning_board = service.read(RUN_ID)
    assert planning_board is not None
    assert planning_board.mode.value == "planning"
    left = service.review_plan(
        RUN_ID,
        command=_plan_command(
            "plan.review",
            decision=PlanReviewDecision.LEAVE_PLAN_MODE,
            expected_revision=feedback.revision,
        ),
        decision=PlanReviewDecision.LEAVE_PLAN_MODE,
        expected_revision=feedback.revision,
    )
    assert left.execution_authorized is False
    execution_board = service.read(RUN_ID)
    assert execution_board is not None
    assert execution_board.mode.value == "execution"


def test_orchestrator_cas_dag_depth_and_settlement_are_root_owned(tmp_path: Path) -> None:
    """The root owns monotonic topology and claims cannot be replayed or bypass dependencies."""
    first = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="first",
    )
    second = _work_item(
        ordinal=1,
        kind="model",
        objective="fit the model",
        idempotency_key="second",
        dependency_ids=(first.work_id,),
        delegation_depth=1,
    )
    lifecycle = _LifecycleStub((first, second))
    board = OrchestratorBoard(
        LocalDagRepository(tmp_path / "dag"),
        RUN_ID,
        lifecycle=lifecycle,
        owner=RunOwnerHandle(run_id=RUN_ID, owner_id="owner", epoch=1),
        owner_assertion=lambda owner: None,
    )
    unknown = first.model_copy(update={"work_id": new_id("work")})
    with pytest.raises(OrchestratorBoardError, match="lifecycle-published"):
        board.add_work_item(unknown)
    board.add_work_item(first)
    board.add_work_item(second)
    with pytest.raises(BoardDependencyError):
        board.claim(second.work_id, claimant_id="worker")
    claim = board.claim(first.work_id, claimant_id="worker")
    result = board.settle(claim, result=_result(first))
    assert result.task_id == first.task_id
    canonical = lifecycle.get_work_result(first.work_id)
    assert canonical is not None
    assert canonical.source_work_id == first.work_id
    assert canonical.source_kind == first.kind
    assert canonical.source_task_id == first.task_id
    assert canonical.source_objective == first.objective
    second_claim = board.claim(second.work_id, claimant_id="worker")
    board.settle(second_claim, result=_result(second))
    assert [result.task_id for result in board.ordered_results()] == [first.task_id, second.task_id]
    with pytest.raises(OrchestratorBoardError):
        board.settle(second_claim, result=_result(second))


class _ConflictOnceDagRepository:
    """Inject one compare-and-set conflict to exercise post-settlement recovery."""

    def __init__(self, root: Path) -> None:
        self._repository = LocalDagRepository(root)
        self.conflict_next = False

    def get(self, run_id: str) -> DagBoard | None:
        return self._repository.get(run_id)

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        if self.conflict_next:
            self.conflict_next = False
            raise BoardConflictError("simulated competing topology writer")
        return self._repository.save(board, expected_revision=expected_revision)

    def history(self, run_id: str) -> tuple[DagBoard, ...]:
        """Delegate durable history reads to the wrapped repository."""
        return self._repository.history(run_id)


class _AlwaysConflictDagRepository:
    """Inject a permanent projection failure while retaining the owner record."""

    def __init__(self, root: Path) -> None:
        self._repository = LocalDagRepository(root)
        self.fail = False

    def get(self, run_id: str) -> DagBoard | None:
        """Read the durable topology even while writes are unavailable."""
        return self._repository.get(run_id)

    def save(self, board: DagBoard, *, expected_revision: int) -> DagBoard:
        """Fail every projection write when the fault is enabled."""
        if self.fail:
            raise BoardConflictError("permanent topology conflict")
        return self._repository.save(board, expected_revision=expected_revision)

    def history(self, run_id: str) -> tuple[DagBoard, ...]:
        """Read append-only topology history for restart recovery."""
        return self._repository.history(run_id)


def test_orchestrator_retries_dag_cas_after_authoritative_owner_settlement(tmp_path: Path) -> None:
    """A CAS race leaves one lifecycle settlement and eventually publishes its topology result."""
    item = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="cas-race",
    )
    lifecycle = _LifecycleStub((item,))
    repository = _ConflictOnceDagRepository(tmp_path / "dag-race")
    board = OrchestratorBoard(
        repository,
        RUN_ID,
        lifecycle=lifecycle,
        owner=RunOwnerHandle(run_id=RUN_ID, owner_id="owner", epoch=1),
        owner_assertion=lambda owner: None,
    )
    board.add_work_item(item)
    claim = board.claim(item.work_id, claimant_id="worker")
    repository.conflict_next = True
    board.settle(claim, result=_result(item))
    assert len(board.snapshot().results) == 1
    assert lifecycle.items[item.work_id].state == "settled"


def test_orchestrator_rebuilds_result_after_permanent_cas_failure_and_restart(
    tmp_path: Path,
) -> None:
    """A fresh board rebuilds its projection from the one owner-settled child record."""
    item = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="permanent-cas",
    )
    lifecycle = _LifecycleStub((item,))
    repository = _AlwaysConflictDagRepository(tmp_path / "dag-restart")
    owner = RunOwnerHandle(run_id=RUN_ID, owner_id="owner", epoch=1)
    board = OrchestratorBoard(
        repository,
        RUN_ID,
        lifecycle=lifecycle,
        owner=owner,
        owner_assertion=lambda live_owner: None,
    )
    board.add_work_item(item)
    claim = board.claim(item.work_id, claimant_id="worker")
    result = _result(item)
    repository.fail = True

    with pytest.raises(OrchestratorBoardError, match="did not publish"):
        board.settle(claim, result=result)

    canonical = lifecycle.get_work_result(item.work_id)
    assert canonical is not None
    assert canonical.subagent_result == result
    assert board.snapshot().results == ()

    repository.fail = False
    restarted = OrchestratorBoard(
        repository,
        RUN_ID,
        lifecycle=lifecycle,
        owner=owner,
        owner_assertion=lambda live_owner: None,
    )
    assert restarted.recover_settlement(item.work_id) == result
    assert restarted.snapshot().results == (result,)


def test_orchestrator_recovery_rejects_child_identity_changed_after_key_mint(
    tmp_path: Path,
) -> None:
    """Recovery rejects a child whose agent identity changed while retaining its original key."""
    item = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="identity-key-binding",
    )
    lifecycle = _LifecycleStub((item,))
    board = OrchestratorBoard(
        LocalDagRepository(tmp_path / "dag-identity-key"),
        RUN_ID,
        lifecycle=lifecycle,
        owner=RunOwnerHandle(run_id=RUN_ID, owner_id="owner", epoch=1),
        owner_assertion=lambda owner: None,
    )
    board.add_work_item(item)
    claim = board.claim(item.work_id, claimant_id="worker")
    original = _result(item)
    node = DagNode.from_work_item(item)
    canonical = board._work_result(original, node, claim)
    lifecycle.settle_work(claim, canonical, board._owner)
    altered = original.model_copy(update={"agent_id": new_id("agent")})
    lifecycle.results[item.work_id] = WorkResult.model_construct(
        kind=canonical.kind,
        payload=canonical.payload,
        cause=canonical.cause,
        dispatch_state=canonical.dispatch_state,
        source_work_id=canonical.source_work_id,
        source_run_id=canonical.source_run_id,
        source_attempt=canonical.source_attempt,
        source_claim_token=canonical.source_claim_token,
        source_ordinal=canonical.source_ordinal,
        source_kind=canonical.source_kind,
        source_task_id=canonical.source_task_id,
        source_objective=canonical.source_objective,
        source_delegation_depth=canonical.source_delegation_depth,
        source_agent_id=canonical.source_agent_id,
        source_agent=canonical.source_agent,
        source_parent_agent=canonical.source_parent_agent,
        source_delegation_key=canonical.source_delegation_key,
        subagent_result=altered,
    )

    with pytest.raises(OrchestratorBoardError, match="authoritative invocation"):
        board.recover_settlement(item.work_id)
    assert board.snapshot().results == ()


def test_orchestrator_rejects_a_work_item_key_that_does_not_recompute(tmp_path: Path) -> None:
    """A forged owner-published key cannot make its invocation binding self-consistent."""
    item = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="forged-node-key",
    )
    forged = item.model_copy(update={"delegation_key": "f" * 64})
    lifecycle = _LifecycleStub((forged,))
    board = OrchestratorBoard(
        LocalDagRepository(tmp_path / "dag-forged-node-key"),
        RUN_ID,
        lifecycle=lifecycle,
        owner=RunOwnerHandle(run_id=RUN_ID, owner_id="owner", epoch=1),
        owner_assertion=lambda owner: None,
    )

    with pytest.raises(OrchestratorBoardError, match="delegation key"):
        board.add_work_item(forged)
    assert board.snapshot().work_items == ()


def test_mira_replay_rejects_a_child_identity_not_published_by_the_work_item() -> None:
    """MIRA does not unlock a dependent from a same-Run but foreign child settlement."""
    upstream = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="bound-upstream",
    )
    downstream = _work_item(
        ordinal=1,
        kind="model",
        objective="fit the model",
        idempotency_key="bound-downstream",
        dependency_ids=(upstream.work_id,),
        delegation_depth=1,
    )
    foreign = _result(upstream).model_copy(
        update={
            "agent_id": new_id("agent"),
            "delegation_key": "f" * 64,
        }
    )
    board = DagBoard.model_construct(
        run_id=RUN_ID,
        work_items=(DagNode.from_work_item(upstream), DagNode.from_work_item(downstream)),
        results=(foreign,),
    )

    with pytest.raises(BoardReplayError, match="authoritative invocation"):
        replay_dependency_selection(board)


def test_mira_replay_does_not_treat_queue_work_id_as_task_identity() -> None:
    """A queue record id cannot stand in for the Task invocation id during replay."""
    upstream = _work_item(
        ordinal=0,
        kind="profile",
        objective="profile the dataset",
        idempotency_key="queue-id-binding",
    )
    child = _result(upstream).model_copy(update={"task_id": upstream.work_id})
    board = DagBoard.model_construct(
        run_id=RUN_ID,
        work_items=(DagNode.from_work_item(upstream),),
        results=(child,),
    )

    with pytest.raises(BoardReplayError, match="unknown Task invocation"):
        replay_dependency_selection(board)


def test_mira_replay_rejects_history_that_starts_after_genesis() -> None:
    """A lone late plan revision cannot prove that earlier durable history was retained."""
    with pytest.raises(BoardReplayError, match="starts after"):
        replay_plan_revisions((PlanBoard(run_id=RUN_ID, revision=7),))
