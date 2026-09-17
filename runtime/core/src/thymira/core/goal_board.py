"""Owner-only goal and advisory plan operations over durable board state."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json
from thymira.schemas import (
    Actor,
    ActorKind,
    ControlInput,
    ControlInputKind,
    EventType,
    GoalRevision,
    GoalRound,
    GoalStatus,
    Id,
    PlanArtifactRef,
    PlanBoard,
    PlanFeedbackResult,
    PlanMode,
    PlanReviewDecision,
    TodoCompletion,
    TodoItem,
    TodoState,
    ToolRepeatState,
    new_id,
    utc_now,
)

if TYPE_CHECKING:
    from thymira.schemas import AuthorityProof
    from thymira.state import BoardRepository
    from thymira.state.artifact_store import ArtifactStore


class GoalBoardError(RuntimeError):
    """Base error for owner-only goal and plan operations."""


class GoalAuthorityError(GoalBoardError):
    """Raised when a goal mutation lacks live root human provenance."""


class GoalRevisionConflictError(GoalBoardError):
    """Raised when a goal command carries a stale exact revision."""


class PlanReviewRequiredError(GoalBoardError):
    """Raised when a plan-mode operation has not received explicit human review."""


class AutonomousRoundLimitError(GoalBoardError):
    """Raised when an autonomous goal exceeds its configured continuation bound."""


class RepeatBlockedError(GoalBoardError):
    """Raised only by callers that request execution after the repeat limit is reached."""


@dataclass(frozen=True, slots=True)
class GoalBoardConfig:
    """Deterministic limits for rounds and repeated tool calls."""

    block_after_same_cause: int = 3
    max_autonomous_rounds: int = 4
    repeat_reminder_counts: tuple[int, ...] = (2, 3, 4)
    block_after_repeats: int = 5

    def __post_init__(self) -> None:
        """Reject unusable limits before a service can persist a board."""
        if self.block_after_same_cause < 1:
            raise ValueError("block_after_same_cause must be positive")
        if self.max_autonomous_rounds < 1:
            raise ValueError("max_autonomous_rounds must be positive")
        if self.block_after_repeats < 1:
            raise ValueError("block_after_repeats must be positive")
        if any(value < 1 for value in self.repeat_reminder_counts):
            raise ValueError("repeat reminder counts must be positive")
        if tuple(sorted(set(self.repeat_reminder_counts))) != self.repeat_reminder_counts:
            raise ValueError("repeat reminder counts must be sorted and unique")


EventRecorder = Callable[[Id, EventType, Actor, dict[str, Any]], object]


class GoalBoardService:
    """Persist complete plans and goals while keeping authorization in code."""

    def __init__(
        self,
        repository: BoardRepository,
        *,
        config: GoalBoardConfig | None = None,
        root_owner_id: str = "root",
        artifact_store_factory: Callable[[Id], ArtifactStore] | None = None,
        authority_verifier: Callable[[ControlInput], bool] | None = None,
        record_event: EventRecorder | None = None,
    ) -> None:
        if not root_owner_id.strip():
            raise ValueError("root_owner_id must not be empty")
        self._repository = repository
        self._config = config or GoalBoardConfig()
        self._root_owner_id = root_owner_id
        self._artifact_store_factory = artifact_store_factory
        self._authority_verifier = authority_verifier
        self._record_event = record_event

    def read(self, run_id: Id) -> PlanBoard | None:
        """Read the latest persisted plan without mutating any state."""
        return self._repository.get(run_id)

    def publish_plan(
        self,
        run_id: Id,
        items: Sequence[TodoItem],
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
        artifact: PlanArtifactRef | None = None,
        artifact_content: str | None = None,
        artifact_name: str = "plans/plan.json",
    ) -> PlanBoard:
        """Publish a full ordered plan and keep its pending items durable.

        Goal creation is deliberately a separate authenticated command.  A model-generated plan
        may propose work, but passing a goal through plan publication cannot manufacture a goal
        revision or human authority.
        """
        self._require_owner(owner_id)
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        normalized = self._normalize_items(items)
        if artifact is not None:
            if artifact.run_id != run_id:
                raise GoalBoardError("plan artifact belongs to another Run")
            if artifact.revision != current.revision + 1:
                raise GoalBoardError("plan artifact revision must match its board publication")
        if artifact_content is not None:
            artifact = self._save_artifact(
                run_id,
                artifact_content,
                artifact_name,
                revision=current.revision + 1,
            )
        replacement_items = self._preserve_pending(current.items, normalized)
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "items": replacement_items,
                "mode": PlanMode.PLANNING,
                "artifact": artifact,
                "goal": current.goal,
            }
        )
        saved = self._save(replacement, expected_revision=expected)
        self._event(
            run_id,
            EventType.PLAN_PUBLISHED,
            Actor.system(),
            {
                "plan_id": saved.plan_id,
                "revision": saved.revision,
                "item_ids": [item.item_id for item in replacement_items],
                "artifact": artifact.model_dump(mode="json") if artifact else None,
            },
        )
        return saved

    def write_todo(
        self,
        run_id: Id,
        items: Sequence[TodoItem],
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> PlanBoard:
        """Replace the todo list while retaining every pending in-progress item."""
        self._require_owner(owner_id)
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        replacement_items = self._preserve_pending(current.items, self._normalize_items(items))
        replacement = current.model_copy(
            update={"revision": current.revision + 1, "items": replacement_items}
        )
        saved = self._save(replacement, expected_revision=expected)
        self._event(
            run_id,
            EventType.PLAN_TODO_WRITTEN,
            Actor.system(),
            {
                "revision": saved.revision,
                "items": [item.model_dump(mode="json") for item in saved.items],
            },
        )
        return saved

    def complete_item(
        self,
        run_id: Id,
        item_id: Id,
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
        summary: str | None = None,
        result_sha256: str | None = None,
    ) -> PlanBoard:
        """Mark one item complete and append its durable completion record immediately."""
        self._require_owner(owner_id)
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        item = self._item(current, item_id)
        if item.state in {TodoState.COMPLETED, TodoState.SKIPPED}:
            return current
        completed = item.model_copy(update={"state": TodoState.COMPLETED})
        completion = TodoCompletion(
            item_id=item.item_id,
            ordinal=item.ordinal,
            source_revision=current.revision + 1,
            summary=summary,
            result_sha256=result_sha256,
        )
        items = tuple(completed if value.item_id == item_id else value for value in current.items)
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "items": items,
                "completions": (*current.completions, completion),
            }
        )
        saved = self._save(replacement, expected_revision=expected)
        self._event(
            run_id,
            EventType.PLAN_ITEM_COMPLETED,
            Actor.system(),
            {
                "item_id": item_id,
                "ordinal": item.ordinal,
                "revision": saved.revision,
                "summary": summary,
            },
        )
        return saved

    def create_goal(
        self,
        run_id: Id,
        goal: str,
        *,
        command: ControlInput,
        causal_proof: str,
        expected_revision: int = 0,
        owner_id: str = "root",
    ) -> PlanBoard:
        """Create a goal only from an authenticated root-human command."""
        self._require_owner(owner_id)
        authority = self._require_human_command(
            command,
            run_id,
            operation="goal.create",
            payload={
                "operation": "goal.create",
                "goal": goal,
                "causal_proof": causal_proof,
                "expected_revision": expected_revision,
            },
        )
        current = self._current(run_id)
        self._require_revision(current, expected_revision)
        if current.goal_revision is not None:
            raise GoalBoardError("a goal already exists; use edit_goal")
        revision = GoalRevision(
            run_id=run_id,
            revision=1,
            goal=goal,
            actor=authority.actor,
            authority_proof_id=authority.proof_id,
            causal_proof=causal_proof,
        )
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "goal": goal,
                "goal_revision": revision,
                "goal_status": GoalStatus.ACTIVE,
            }
        )
        saved = self._save(replacement, expected_revision=current.revision)
        self._event(
            run_id,
            EventType.GOAL_CREATED,
            authority.actor,
            {
                "goal_id": revision.goal_id,
                "goal": goal,
                "revision": revision.revision,
                "authority_proof_id": authority.proof_id,
                "causal_proof": causal_proof,
            },
        )
        return saved

    def edit_goal(
        self,
        run_id: Id,
        goal: str,
        *,
        command: ControlInput,
        causal_proof: str,
        expected_revision: int,
        owner_id: str = "root",
    ) -> PlanBoard:
        """Edit an existing goal under exact revision and causal-proof checks."""
        self._require_owner(owner_id)
        authority = self._require_human_command(
            command,
            run_id,
            operation="goal.edit",
            payload={
                "operation": "goal.edit",
                "goal": goal,
                "causal_proof": causal_proof,
                "expected_revision": expected_revision,
            },
        )
        current = self._current(run_id)
        self._require_revision(current, expected_revision)
        if current.goal_revision is None:
            raise GoalBoardError("cannot edit a goal before it is created")
        revision = current.goal_revision.model_copy(
            update={
                "revision_id": new_id("revision"),
                "revision": current.goal_revision.revision + 1,
                "goal": goal,
                "actor": authority.actor,
                "authority_proof_id": authority.proof_id,
                "causal_proof": causal_proof,
                "created_at": utc_now(),
            }
        )
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "goal": goal,
                "goal_revision": revision,
            }
        )
        saved = self._save(replacement, expected_revision=current.revision)
        self._event(
            run_id,
            EventType.GOAL_EDITED,
            authority.actor,
            {
                "goal_id": revision.goal_id,
                "goal": goal,
                "revision": revision.revision,
                "authority_proof_id": authority.proof_id,
                "causal_proof": causal_proof,
            },
        )
        return saved

    def record_round(
        self,
        run_id: Id,
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
        cause: str | None = None,
        succeeded: bool = False,
    ) -> PlanBoard:
        """Record a bounded round and block only after N consecutive same-cause failures."""
        self._require_owner(owner_id)
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        if current.autonomous_rounds >= self._config.max_autonomous_rounds:
            raise AutonomousRoundLimitError("autonomous goal round limit reached")
        same_cause = bool(cause) and cause == current.blocked_cause
        blocked_count = (
            current.blocked_count + 1
            if same_cause and not succeeded
            else (1 if cause and not succeeded else 0)
        )
        blocked = blocked_count >= self._config.block_after_same_cause
        round_record = GoalRound(
            round_number=current.autonomous_rounds + 1,
            revision=current.revision + 1,
            cause=cause,
            succeeded=succeeded,
        )
        next_status = (
            GoalStatus.BLOCKED
            if blocked
            else (GoalStatus.ACTIVE if current.goal is not None else GoalStatus.NONE)
            if current.goal_status is GoalStatus.BLOCKED
            else current.goal_status
        )
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "rounds": (*current.rounds, round_record),
                "autonomous_rounds": current.autonomous_rounds + 1,
                "blocked_cause": cause if cause and not succeeded else None,
                "blocked_count": blocked_count,
                "goal_status": next_status,
            }
        )
        saved = self._save(replacement, expected_revision=expected)
        self._event(
            run_id,
            EventType.GOAL_ROUND_RECORDED,
            Actor.system(),
            {
                "revision": saved.revision,
                "cause": cause,
                "blocked_count": blocked_count,
                "blocked": blocked,
            },
        )
        if blocked:
            self._event(
                run_id,
                EventType.GOAL_BLOCKED,
                Actor.system(),
                {
                    "revision": saved.revision,
                    "cause": cause,
                    "consecutive_count": blocked_count,
                },
            )
        return saved

    def observe_tool_call(
        self,
        run_id: Id,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        owner_id: str = "root",
        expected_revision: int | None = None,
    ) -> RepeatDecision:
        """Count canonical consecutive calls and persist escalating reminders."""
        self._require_owner(owner_id)
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        digest = hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()
        previous = current.repeat_state
        count = (
            previous.consecutive_count + 1
            if (previous.tool_name == tool_name and previous.arguments_sha256 == digest)
            else 1
        )
        state = ToolRepeatState(
            tool_name=tool_name,
            arguments_sha256=digest,
            consecutive_count=count,
        )
        replacement = current.model_copy(
            update={"revision": current.revision + 1, "repeat_state": state}
        )
        saved = self._save(replacement, expected_revision=expected)
        reminder_level = sum(
            count >= threshold for threshold in self._config.repeat_reminder_counts
        )
        blocked = count >= self._config.block_after_repeats
        message = (
            "repeated canonical tool call blocked"
            if blocked
            else f"repeated canonical tool call reminder {reminder_level}"
            if reminder_level
            else "tool call accepted"
        )
        if reminder_level or blocked:
            self._event(
                run_id,
                EventType.TOOL_REPEAT_REMINDER,
                Actor.system(),
                {
                    "tool": tool_name,
                    "arguments_sha256": digest,
                    "consecutive_count": count,
                    "reminder_level": reminder_level,
                    "blocked": blocked,
                    "revision": saved.revision,
                },
            )
        return RepeatDecision(tool_name, digest, count, reminder_level, blocked, message)

    def review_plan(
        self,
        run_id: Id,
        *,
        command: ControlInput,
        decision: PlanReviewDecision,
        feedback: str | None = None,
        expected_revision: int | None = None,
        owner_id: str = "root",
    ) -> PlanFeedbackResult:
        """Record plan feedback and leave planning only on an explicit human response."""
        self._require_owner(owner_id)
        authority = self._require_human_command(
            command,
            run_id,
            operation="plan.review",
            payload={
                "operation": "plan.review",
                "decision": decision.value,
                "feedback": feedback,
                "expected_revision": expected_revision,
            },
        )
        current = self._current(run_id)
        expected = current.revision if expected_revision is None else expected_revision
        if current.artifact is None:
            raise PlanReviewRequiredError("no persisted plan is available for review")
        if decision is PlanReviewDecision.LEAVE_PLAN_MODE and not authority.actor.authenticated:
            raise PlanReviewRequiredError("leaving plan mode requires live human review")
        mode = (
            PlanMode.EXECUTION
            if decision is PlanReviewDecision.LEAVE_PLAN_MODE
            else PlanMode.PLANNING
        )
        replacement = current.model_copy(
            update={
                "revision": current.revision + 1,
                "mode": mode,
                "review_feedback": (
                    (*current.review_feedback, feedback) if feedback else current.review_feedback
                ),
            }
        )
        saved = self._save(replacement, expected_revision=expected)
        self._event(
            run_id,
            EventType.PLAN_REVIEWED,
            authority.actor,
            {
                "plan_id": saved.plan_id,
                "revision": saved.revision,
                "decision": decision.value,
                "feedback": feedback,
                "execution_authorized": False,
            },
        )
        return PlanFeedbackResult(
            plan_id=saved.plan_id,
            revision=saved.revision,
            decision=decision,
            feedback=feedback,
        )

    def _save(self, board: PlanBoard, *, expected_revision: int) -> PlanBoard:
        """Persist a CAS update and translate repository conflicts into domain errors."""
        try:
            return self._repository.save(board, expected_revision=expected_revision)
        except ValueError as exc:
            raise GoalRevisionConflictError(str(exc)) from exc

    def _current(self, run_id: Id) -> PlanBoard:
        """Load or provide the revision-zero board for a Run."""
        return self._repository.get(run_id) or PlanBoard(run_id=run_id)

    def _save_artifact(
        self,
        run_id: Id,
        content: str,
        artifact_name: str,
        *,
        revision: int,
    ) -> PlanArtifactRef:
        """Save plan bytes once and retain a verified content-addressed reference."""
        if self._artifact_store_factory is None:
            raise GoalBoardError("an artifact store is required to persist plan content")
        artifact = self._artifact_store_factory(run_id).save_text(
            artifact_name,
            content,
            produced_by=run_id,
        )
        return PlanArtifactRef(
            artifact_id=artifact.id,
            run_id=run_id,
            revision=revision,
            sha256=artifact.sha256,
            storage_key=artifact.name,
        )

    @staticmethod
    def _normalize_items(items: Sequence[TodoItem]) -> tuple[TodoItem, ...]:
        """Validate unique ids and ordinals while preserving the complete submitted list."""
        values = tuple(items)
        if len({item.item_id for item in values}) != len(values):
            raise ValueError("todo item ids must be unique")
        if len({item.ordinal for item in values}) != len(values):
            raise ValueError("todo item ordinals must be unique")
        return tuple(sorted(values, key=lambda item: item.ordinal))

    @staticmethod
    def _preserve_pending(
        old: Sequence[TodoItem], replacement: Sequence[TodoItem]
    ) -> tuple[TodoItem, ...]:
        """Keep old in-progress work and prevent a completed item from being resurrected."""
        values = {item.item_id: item for item in replacement}
        for prior in old:
            if prior.state is TodoState.IN_PROGRESS and prior.item_id not in values:
                values[prior.item_id] = prior
            elif prior.state is TodoState.IN_PROGRESS:
                values[prior.item_id] = values[prior.item_id].model_copy(
                    update={"state": TodoState.IN_PROGRESS}
                )
            elif prior.state is TodoState.COMPLETED and prior.item_id in values:
                values[prior.item_id] = values[prior.item_id].model_copy(
                    update={"state": TodoState.COMPLETED}
                )
        result = tuple(sorted(values.values(), key=lambda item: item.ordinal))
        if len({item.ordinal for item in result}) != len(result):
            raise ValueError(
                "todo item ordinals must remain unique when preserving in-progress work"
            )
        return result

    @staticmethod
    def _item(board: PlanBoard, item_id: Id) -> TodoItem:
        """Find one todo item."""
        for item in board.items:
            if item.item_id == item_id:
                return item
        raise GoalBoardError(f"todo item {item_id} does not exist")

    @staticmethod
    def _require_revision(board: PlanBoard, expected_revision: int) -> None:
        """Reject stale exact revision values."""
        if board.revision != expected_revision:
            raise GoalRevisionConflictError(
                f"expected revision {expected_revision}, current revision is {board.revision}"
            )

    def _require_owner(self, owner_id: str) -> None:
        """Require the configured root owner for every board mutation."""
        if owner_id != self._root_owner_id:
            raise GoalAuthorityError("only the root orchestrator may mutate the board")

    @staticmethod
    def _require_human_authority(authority: AuthorityProof, run_id: Id) -> None:
        """Require an authenticated human proof scoped to the target Run and goal permission."""
        actor = authority.actor
        role = authority.authenticated_role.casefold()
        permissions = {value.casefold() for value in authority.permissions}
        if actor.kind is not ActorKind.HUMAN or not actor.authenticated:
            raise GoalAuthorityError("goal mutations require an authenticated human actor")
        if authority.run_id != run_id:
            raise GoalAuthorityError("authority proof must be scoped to this Run")
        if authority.actor.id != authority.authenticated_principal_id:
            raise GoalAuthorityError("authority proof actor is not bound to its principal")
        if (
            authority.actor.role is not None
            and authority.actor.role != authority.authenticated_role
        ):
            raise GoalAuthorityError("authority proof actor role is not bound to its principal")
        if authority.issued_at > utc_now():
            raise GoalAuthorityError("authority proof was issued in the future")
        if (
            role not in {"root", "owner"}
            and "root" not in permissions
            and "goal:write" not in permissions
        ):
            raise GoalAuthorityError("goal mutation requires root human permission")
        if authority.expires_at <= utc_now():
            raise GoalAuthorityError("authority proof has expired")

    def _require_human_command(
        self,
        command: ControlInput,
        run_id: Id,
        *,
        operation: str,
        payload: dict[str, Any],
    ) -> AuthorityProof:
        """Validate the exact owner command before inspecting its human authority proof."""
        if command.run_id != run_id:
            raise GoalAuthorityError("goal command targets another Run")
        if command.kind is not ControlInputKind.PLAN:
            raise GoalAuthorityError("goal mutations require the PLAN control kind")
        if command.payload != payload:
            raise GoalAuthorityError(f"{operation} command payload is not exact")
        authority = command.authority
        if (
            authority.run_id != command.run_id
            or authority.session_id != command.session_id
            or authority.input_id != command.input_id
            or authority.requested_lane is not command.requested_lane
            or authority.effective_lane is not command.effective_lane
            or authority.kind is not command.kind
            or authority.target_turn_id != command.target_turn_id
            or authority.target_work_ids != command.target_work_ids
            or authority.idempotency_key != command.idempotency_key
        ):
            raise GoalAuthorityError("authority proof is not bound to this control command")
        payload_digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if authority.payload_sha256 != payload_digest:
            raise GoalAuthorityError("authority proof payload binding does not match")
        if authority.binding_sha256 != command.binding_sha256():
            raise GoalAuthorityError("authority proof command binding does not match")
        if self._authority_verifier is None:
            raise GoalAuthorityError("goal authority verification is not configured")
        if not self._authority_verifier(command):
            raise GoalAuthorityError("goal command authority signature is not trusted")
        self._require_human_authority(authority, run_id)
        return authority

    def _event(
        self,
        run_id: Id,
        event_type: EventType,
        actor: Actor,
        payload: dict[str, Any],
    ) -> None:
        """Record optional owner evidence without making the board depend on an event backend."""
        if self._record_event is not None:
            self._record_event(run_id, event_type, actor, payload)


@dataclass(frozen=True, slots=True)
class RepeatDecision:
    """Decision returned after one canonical tool-call observation."""

    tool_name: str
    arguments_sha256: str
    count: int
    reminder_level: int
    blocked: bool
    message: str


__all__ = [
    "AutonomousRoundLimitError",
    "GoalAuthorityError",
    "GoalBoardConfig",
    "GoalBoardError",
    "GoalBoardService",
    "GoalRevisionConflictError",
    "PlanReviewRequiredError",
    "RepeatBlockedError",
    "RepeatDecision",
]
