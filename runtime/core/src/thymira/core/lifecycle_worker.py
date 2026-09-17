"""Single-owner work and control dispatch for the lifecycle repository.

The broker remains a notification mechanism.  ``LifecycleOwnerLoop`` is the one composition seam
that turns a notification into work: it claims the durable item, acquires the Run's live owner,
applies any registered control consumer through that owner, and settles the typed result.  A board
consumer receives a :class:`~thymira.state.RunHandle`, rather than serialisable owner fields, so a
copied ``(run_id, owner_id, epoch)`` value cannot write canonical state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    Actor,
    ControlInput,
    ControlInputKind,
    ControlInputState,
    DispatchState,
    Event,
    EventType,
    ExecutionOutcomeKind,
    FailureCause,
    InboxLane,
    JsonObject,
    OwnerReleaseReason,
    TerminalAuditBinding,
    TurnEnded,
    TurnEndReason,
    WorkClaim,
    WorkItem,
    WorkResult,
    WorkState,
    id_kind,
    new_id,
    utc_now,
)
from thymira.state import LifecycleError, LifecycleRepository, RunHandle

if TYPE_CHECKING:
    from collections.abc import Mapping


@runtime_checkable
class GoalBoardCommandConsumer(Protocol):
    """Narrow goal-board control hook invoked inside the Run owner loop.

    The consumer is intentionally limited to the two goal-board command kinds.  It may create or
    update a goal, plan or review projection, but it must use ``live_owner`` for any canonical Run
    facts and return only JSON-safe derived details.  The lifecycle repository remains the owner
    of work claims, settlement and control acknowledgement.
    """

    def apply_goal_control(
        self,
        command: ControlInput,
        live_owner: RunHandle,
    ) -> JsonObject:
        """Apply one validated PLAN or DELEGATE command under the live Run owner."""
        ...


@runtime_checkable
class ControlInputConsumer(Protocol):
    """Owner-bound consumer for non-board lifecycle controls.

    The API may accept every closed ``ControlInputKind`` after authentication, but it cannot
    mutate the Run itself.  The composition root registers the bounded domain consumer here; the
    owner loop acknowledges a command only after this method returns.
    """

    def apply_control(self, command: ControlInput, live_owner: RunHandle) -> JsonObject:
        """Apply one accepted control under the live Run owner."""
        ...


ControlHandler = Callable[[ControlInput, RunHandle], JsonObject]
TerminalAuditor = Callable[[RunHandle, WorkItem, WorkResult, TerminalAuditBinding], None]


class BoundedControlInputConsumer:
    """Route every non-board control kind to an explicit owner-bound handler.

    The mapping is validated at composition time against the closed schema vocabulary.  This
    prevents a worker from silently accepting a new control kind and dropping it, while keeping
    domain policy in the core services that own each operation.
    """

    def __init__(self, handlers: Mapping[ControlInputKind, ControlHandler]) -> None:
        expected = set(ControlInputKind) - {
            ControlInputKind.PLAN,
            ControlInputKind.DELEGATE,
        }
        provided = set(handlers)
        missing = expected - provided
        unexpected = provided - expected
        if missing or unexpected:
            parts: list[str] = []
            if missing:
                parts.append(f"missing handlers: {sorted(kind.value for kind in missing)}")
            if unexpected:
                parts.append(f"unexpected handlers: {sorted(kind.value for kind in unexpected)}")
            raise ValueError("invalid lifecycle control consumer: " + "; ".join(parts))
        self._handlers = dict(handlers)

    def apply_control(self, command: ControlInput, live_owner: RunHandle) -> JsonObject:
        """Apply one non-board command through its explicitly composed handler."""
        if command.kind in {ControlInputKind.PLAN, ControlInputKind.DELEGATE}:
            raise ValueError("goal-board controls require GoalBoardCommandConsumer")
        return self._handlers[command.kind](command, live_owner)


@runtime_checkable
class WorkExecutor(Protocol):
    """Execute one claimed work item with a live canonical Run writer."""

    def execute(self, work: WorkItem, live_owner: RunHandle) -> WorkResult:
        """Run the item and return its typed, durable outcome."""
        ...


class ControlInputRejectedError(LifecycleError):
    """A domain control was durably rejected before any model dispatch."""

    def __init__(
        self,
        command: ControlInput,
        cause: FailureCause,
        applied: tuple[ControlInput, ...],
    ) -> None:
        super().__init__(f"control input {command.input_id} was rejected: {cause.message}")
        self.command = command
        self.cause = cause
        self.applied = applied


class LifecycleOwnerLoop:
    """Process one durable notification through one fenced owner.

    ``LifecycleOwnerLoop`` is shared by inline and broker consumers.  It never executes a work
    item before both durable claim and live owner acquisition succeed.  If execution raises after
    dispatch was marked, the claim is left for explicit expiry recovery; the next owner records an
    ``UNKNOWN`` effect instead of silently retrying it.
    """

    def __init__(
        self,
        repository: LifecycleRepository,
        worker_id: str,
        *,
        goal_board_consumer: GoalBoardCommandConsumer | None = None,
        control_consumer: ControlInputConsumer | None = None,
        actor_factory: Callable[[], Actor] = Actor.system,
        terminal_auditor: TerminalAuditor | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self._repository = repository
        self._worker_id = worker_id
        self._goal_board_consumer = goal_board_consumer
        self._control_consumer = control_consumer
        self._actor_factory = actor_factory
        self._terminal_auditor = terminal_auditor
        self._turn_id: ContextVar[str | None] = ContextVar("thymira_lifecycle_turn", default=None)

    def process(  # noqa: PLR0911, PLR0912  # each durable lifecycle outcome has its own return
        self,
        work_id: str,
        run_id: str,
        executor: WorkExecutor | Callable[[WorkItem, RunHandle], WorkResult],
    ) -> WorkResult | None:
        """Claim, own, execute and settle one ``{work_id, run_id}`` notification.

        ``None`` means another worker owns the item or the Run owner is busy; the transport may
        acknowledge or reschedule based on its durable delivery policy.  A mismatched run id is a
        poison notification and raises before any effect is attempted.
        """
        durable_work = self._repository.get_work(work_id)
        if durable_work is None:
            return None
        if durable_work.run_id != run_id:
            raise LifecycleError("work notification run_id does not match its durable item")
        if durable_work.state is WorkState.SETTLED and durable_work.settled_result is not None:
            # Settlement is authoritative, but the turn event is a second durable write.  A
            # process can die in that small window for *any* result, including success.  The
            # broker must therefore not ACK a settled item until its closure (and terminal audit
            # for non-success outcomes) is repaired.
            return self._repair_settled_turn(durable_work)
        if (
            durable_work.state is WorkState.CLAIMED
            and durable_work.claim_expires_at is not None
            and durable_work.claim_expires_at <= utc_now()
            and durable_work.dispatch_state is not DispatchState.NEVER_DISPATCHED
        ):
            return self._recover_expired_dispatched(durable_work)
        claim = self._repository.claim_work(work_id, self._worker_id)
        if claim is None:
            return None
        if claim.run_id != run_id:
            raise LifecycleError("work notification run_id does not match its durable claim")
        # Bind the live owner identity to this exact durable claim.  A worker name alone could
        # acquire a Run lock after a stale or forged notification; the claim token is the
        # repository's durable hand-off between claim and owner acquisition.
        owner = self._repository.acquire_owner(run_id, claim.claim_token)
        if owner is None:
            return None
        handle = self._repository.run_handle(owner)
        release_reason = OwnerReleaseReason.COMPLETED
        turn_id = new_id("turn")
        turn_token: Token[str | None] = self._turn_id.set(turn_id)
        try:
            applied_controls: tuple[ControlInput, ...]
            try:
                applied_controls = self.apply_pending_goal_controls(
                    handle,
                    input_id=_work_input_id(durable_work),
                    active_turn=_work_input_id(durable_work) is None,
                )
            except ControlInputRejectedError as exc:
                result = WorkResult(
                    kind=ExecutionOutcomeKind.FAILED,
                    dispatch_state=DispatchState.NEVER_DISPATCHED,
                    cause=exc.cause,
                )
                self.settle_turn(
                    claim,
                    result,
                    handle,
                    TurnEnded(
                        turn_id=turn_id,
                        run_id=run_id,
                        claimed_input_ids=tuple(
                            command.input_id for command in (*exc.applied, exc.command)
                        ),
                        work_ids=(work_id,),
                        step_count=0,
                        end_reason=TurnEndReason.REJECTED,
                        cause=exc.cause,
                    ),
                )
                return result
            work = _require_work(self._repository, work_id)
            self._repository.mark_dispatched(claim, owner)
            if any(
                command.kind in {ControlInputKind.CANCEL, ControlInputKind.HOST_PAUSE}
                for command in applied_controls
            ):
                # The control is already the owner-authorised exit.  Do not invoke THY after a
                # terminal cancellation or host pause: a graph call here could emit model/tool
                # effects after the operator's command, and would not be an aborted call.
                result = self._apply_exit_control_outcome(
                    applied_controls,
                    WorkResult(kind=ExecutionOutcomeKind.CANCELLED),
                )
            else:
                result = (
                    executor.execute(work, handle)
                    if isinstance(executor, WorkExecutor)
                    else executor(work, handle)
                )
            turn = TurnEnded(
                turn_id=turn_id,
                run_id=run_id,
                claimed_input_ids=tuple(command.input_id for command in applied_controls),
                work_ids=(work_id,),
                step_count=0,
                end_reason=_turn_end_reason(result.kind, result.payload),
                cause=result.cause,
            )
            self.settle_turn(
                claim,
                result,
                handle,
                turn,
            )
            self._audit_terminal_exit(handle, work, result, turn)
        except BaseException:
            # A pre-dispatch failure may be reclaimed after expiry.  Once dispatch was recorded,
            # recovery must classify the external effect as unknown instead of retrying silently.
            release_reason = OwnerReleaseReason.FAILED
            raise
        else:
            return result
        finally:
            self._turn_id.reset(turn_token)
            handle.close(release_reason)

    def _recover_expired_dispatched(self, work: WorkItem) -> WorkResult | None:
        """Settle an expired dispatched claim as UNKNOWN without re-running its external effect."""
        if work.claim_owner is None or work.claim_token is None or work.claim_expires_at is None:
            raise LifecycleError("expired dispatched work has incomplete claim metadata")
        claim = WorkClaim(
            work_id=work.work_id,
            run_id=work.run_id,
            claimant_id=work.claim_owner,
            attempt=work.attempt,
            claim_token=work.claim_token,
            expires_at=work.claim_expires_at,
        )
        owner = self._repository.acquire_owner(work.run_id, claim.claim_token)
        if owner is None:
            return None
        handle = self._repository.run_handle(owner)
        try:
            try:
                recovered = self._repository.recover_expired_work(claim, owner)
            except Exception:
                current = self._repository.get_work(work.work_id)
                if current is not None and current.settled_result is not None:
                    self._repair_settled_turn_with_handle(handle, current)
                    return current.settled_result
                raise
            result = recovered.settled_result
            if result is None or result.kind is not ExecutionOutcomeKind.UNKNOWN:
                raise LifecycleError("expired dispatched work did not settle as UNKNOWN")
            # ``recover_expired_work`` writes the settlement while the caller's ``work`` is the
            # pre-recovery snapshot.  Use the returned item so its durable result is available to
            # the turn-repair path.
            self._repair_settled_turn_with_handle(handle, recovered)
            return result
        finally:
            handle.close(OwnerReleaseReason.RECOVERED)

    def _repair_settled_turn(self, work: WorkItem) -> WorkResult | None:
        """Repair a turn closure left incomplete after any durable settlement."""
        result = work.settled_result
        if result is None:
            return None
        owner = self._repository.acquire_owner(
            work.run_id,
            f"recovery-turn:{self._worker_id}",
        )
        if owner is None:
            return None
        handle = self._repository.run_handle(owner)
        try:
            self._repair_settled_turn_with_handle(handle, work)
            return result
        finally:
            handle.close(OwnerReleaseReason.RECOVERED)

    def _repair_settled_turn_with_handle(
        self,
        handle: RunHandle,
        work: WorkItem,
    ) -> None:
        """Record a missing turn closure and audit a non-success result exactly once."""
        result = work.settled_result
        if result is None:
            raise LifecycleError("settled work has no durable result")
        turn = _find_turn_ended(handle.events(), work)
        if turn is not None:
            # A process may have written the turn closure and died before the terminal MIRA
            # callback.  Keep the closure idempotent while still replaying the missing audit.
            self._audit_terminal_exit(handle, work, result, turn)
            return
        claimed_input_ids: tuple[str, ...] = ()
        input_id = work.payload.get("input_id")
        if isinstance(input_id, str):
            try:
                if id_kind(input_id) == "input":
                    claimed_input_ids = (input_id,)
            except ValueError:
                pass
        turn = TurnEnded(
            turn_id=new_id("turn"),
            run_id=work.run_id,
            claimed_input_ids=claimed_input_ids,
            work_ids=(work.work_id,),
            step_count=0,
            end_reason=_turn_end_reason(result.kind, result.payload),
            cause=result.cause,
        )
        handle.append(
            EventType.TURN_ENDED,
            self._actor_factory(),
            turn.to_json_dict(),
            expected_version=self._repository.version(work.run_id),
        )
        self._audit_terminal_exit(handle, work, result, turn)

    def _audit_terminal_exit(
        self,
        handle: RunHandle,
        work: WorkItem,
        result: WorkResult,
        turn: TurnEnded,
    ) -> None:
        """Run the composed MIRA terminal path for failed, cancelled and unknown exits."""
        if self._terminal_auditor is None or result.kind is ExecutionOutcomeKind.SUCCEEDED:
            return
        binding = _terminal_audit_binding(work, result, turn)
        events = handle.events()
        for event in events:
            if event.type is not EventType.AUDIT_COMPLETED:
                continue
            raw_binding = event.payload.get("terminal_audit_binding")
            if not isinstance(raw_binding, dict):
                continue
            try:
                completed_binding = TerminalAuditBinding.model_validate(raw_binding)
            except (TypeError, ValueError):
                continue
            if completed_binding == binding:
                return
        self._terminal_auditor(handle, work, result, binding)

    @staticmethod
    def _apply_exit_control_outcome(
        controls: tuple[ControlInput, ...], result: WorkResult
    ) -> WorkResult:
        """Close a control-driven cancellation or host pause before graph dispatch."""
        command = next(
            (
                item
                for item in controls
                if item.kind in {ControlInputKind.CANCEL, ControlInputKind.HOST_PAUSE}
            ),
            None,
        )
        if command is None:
            return result
        reason = (
            "run cancelled before graph dispatch"
            if command.kind is ControlInputKind.CANCEL
            else "run paused by host before graph dispatch"
        )
        cause = FailureCause(
            code=(
                "cancelled_before_dispatch"
                if command.kind is ControlInputKind.CANCEL
                else "host_paused_before_dispatch"
            ),
            phase="worker.control",
            exception_type="LifecycleControl",
            message=reason,
            effects_may_have_occurred=False,
        )
        return WorkResult(
            kind=ExecutionOutcomeKind.CANCELLED,
            payload={
                **result.payload,
                "control_input_id": command.input_id,
                "control_kind": command.kind.value,
                "initiator": command.authority.actor.to_json_dict(),
                "aborted_before_dispatch": True,
                "dispatched": False,
            },
            cause=cause,
            dispatch_state=DispatchState.NEVER_DISPATCHED,
        )

    def apply_pending_goal_controls(  # noqa: PLR0912  # claim, route and reject stay explicit
        self,
        live_owner: RunHandle,
        *,
        input_id: str | None = None,
        lane: InboxLane = InboxLane.NEXT_TURN,
        active_turn: bool = False,
        turn_id: str | None = None,
    ) -> tuple[ControlInput, ...]:
        """Claim and apply controls from one effective inbox lane.

        The repository claim transfers ``PENDING`` to ``CLAIMED`` atomically with the effective
        lane.  A work notification may name one input, while the live model-step hook may claim
        the next ordered ``NEXT_STEP`` inputs.  Untargeted controls in the other lane remain
        pending for their own turn.
        """
        if lane not in {InboxLane.NEXT_TURN, InboxLane.NEXT_STEP}:
            raise ValueError(f"unsupported control lane {lane!r}")
        goal_consumer = self._goal_board_consumer
        control_consumer = self._control_consumer
        if goal_consumer is None and control_consumer is None:
            pending = tuple(
                command
                for command in self._repository.controls(live_owner.run_id)
                if _claimable_control(command)
                and (input_id is None or command.input_id == input_id)
                and _effective_lane(command, active_turn=active_turn) is lane
            )
            if pending:
                raise LifecycleError(
                    f"no owner consumer is registered for control input {pending[0].input_id}"
                )
            return ()
        applied: list[ControlInput] = []
        for command in self._repository.controls(live_owner.run_id):
            if not _claimable_control(command):
                continue
            if input_id is not None and command.input_id != input_id:
                continue
            if _effective_lane(command, active_turn=active_turn) is not lane:
                continue
            if (
                active_turn
                and command.target_turn_id is not None
                and command.target_turn_id != turn_id
            ):
                continue
            if command.kind in {ControlInputKind.PLAN, ControlInputKind.DELEGATE}:
                if goal_consumer is None:
                    raise LifecycleError(
                        f"no owner consumer is registered for control kind {command.kind.value}"
                    )
                apply = goal_consumer.apply_goal_control
            else:
                if control_consumer is None:
                    raise LifecycleError(
                        f"no owner consumer is registered for control kind {command.kind.value}"
                    )
                apply = control_consumer.apply_control
            claimed = self._repository.claim_control(
                command,
                live_owner.owner_handle(),
                effective_lane=lane,
            )
            if claimed is None:
                continue
            try:
                apply(claimed, live_owner)
            except Exception as exc:
                cause = FailureCause(
                    code="control_rejected",
                    phase="lifecycle.control",
                    exception_type=type(exc).__name__,
                    message=str(exc)[:2000] or type(exc).__name__,
                )
                self._repository.mark_control_applied(
                    claimed,
                    live_owner.owner_handle(),
                    state=ControlInputState.REJECTED,
                )
                raise ControlInputRejectedError(claimed, cause, tuple(applied)) from exc
            self._repository.mark_control_applied(claimed, live_owner.owner_handle())
            applied.append(claimed)
        return tuple(applied)

    def intercept_next_model_step(
        self,
        live_owner: RunHandle,
        *,
        turn_id: str | None = None,
    ) -> tuple[str, ...]:
        """Claim live ``NEXT_STEP`` controls and return their model-visible input text.

        This is the only model-step interception seam.  It runs while the current owner holds the
        Run lock, before the provider request is assembled, so a separate ``control.apply`` work
        notification cannot consume steering input on its behalf.
        """
        claimed = self.apply_pending_goal_controls(
            live_owner,
            lane=InboxLane.NEXT_STEP,
            active_turn=True,
            turn_id=turn_id if turn_id is not None else self._turn_id.get(),
        )
        return tuple(prompt for command in claimed if (prompt := _control_prompt(command)))

    def settle_turn(
        self,
        claim: WorkClaim,
        result: WorkResult,
        live_owner: RunHandle,
        turn: TurnEnded,
    ) -> Event:
        """Persist a work result before emitting its closed ``turn.ended`` event.

        The typed result is authoritative and replayable through ``get_work_result``.  The event
        follows that durable settlement and is therefore safe to repair after a process fault;
        it is never emitted for an unsettled work item.
        """
        if turn.run_id != live_owner.run_id or claim.run_id != live_owner.run_id:
            raise LifecycleError("turn settlement targets another Run")
        self._repository.settle_work(claim, result, live_owner.owner_handle())
        return live_owner.append(
            EventType.TURN_ENDED,
            self._actor_factory(),
            turn.to_json_dict(),
            expected_version=self._repository.version(live_owner.run_id),
        )


def _require_work(repository: LifecycleRepository, work_id: str) -> WorkItem:
    """Return a claimed work item or raise outside the owner-loop exception block."""
    work = repository.get_work(work_id)
    if work is None:
        raise LifecycleError(f"claimed work item {work_id} disappeared")
    return work


def _work_input_id(work: WorkItem) -> str | None:
    """Return a control input id from a control work payload, when one is present."""
    value = work.payload.get("input_id")
    return value if isinstance(value, str) else None


def _effective_lane(command: ControlInput, *, active_turn: bool) -> InboxLane:
    """Resolve a command's durable lane at the owner boundary.

    Follow-ups always begin a later turn.  Steering and trusted injection may enter the live
    next-step lane; if no live model turn is open, the owner records the required next-turn
    rollover instead of allowing a control worker to consume it as a step.
    """
    lane = (
        InboxLane.NEXT_TURN
        if command.kind is ControlInputKind.FOLLOWUP
        else (command.effective_lane or command.requested_lane)
    )
    if not active_turn and lane is InboxLane.NEXT_STEP:
        return InboxLane.NEXT_TURN
    return lane


def _claimable_control(command: ControlInput) -> bool:
    """Return whether an inbox row can be offered for an owner claim.

    An expired claim remains durably visible as ``CLAIMED`` until the next owner performs the
    compare-and-swap requeue in the repository.  Treating it as terminal here would strand work
    after an owner crash; a live claim still belongs exclusively to its current owner.
    """
    if command.state is ControlInputState.PENDING:
        return True
    return (
        command.state is ControlInputState.CLAIMED
        and command.claim_expires_at is not None
        and command.claim_expires_at <= utc_now()
    )


def _control_prompt(command: ControlInput) -> str:
    """Return bounded model-visible content for a claimed content control."""
    if command.kind not in {
        ControlInputKind.FOLLOWUP,
        ControlInputKind.STEER,
        ControlInputKind.INJECT,
        ControlInputKind.RISK_ANSWER,
        ControlInputKind.APPROVAL_RESPONSE,
    }:
        return ""
    prompt = command.payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return f"Lifecycle control {command.kind.value} was accepted for this model step."


def _turn_end_reason(kind: object, payload: Mapping[str, object] | None = None) -> TurnEndReason:
    """Map one typed work outcome to the closed reason emitted for its owner turn."""
    value = getattr(kind, "value", kind)
    if not isinstance(value, str):
        raise LifecycleError(f"unknown work outcome kind {kind!r}")
    if payload is not None and payload.get("control_kind") == ControlInputKind.HOST_PAUSE.value:
        return TurnEndReason.PAUSED
    try:
        return {
            "succeeded": TurnEndReason.COMPLETED,
            "failed": TurnEndReason.FAILED,
            "cancelled": TurnEndReason.CANCELLED,
            "unknown": TurnEndReason.UNKNOWN,
        }[value]
    except KeyError as exc:
        raise LifecycleError(f"unknown work outcome kind {kind!r}") from exc


def _find_turn_ended(events: Sequence[Event], work: WorkItem) -> TurnEnded | None:
    """Find the validated turn closure belonging to one durable work item."""
    for event in events:
        if event.type is not EventType.TURN_ENDED:
            continue
        try:
            turn = TurnEnded.model_validate(event.payload)
        except (TypeError, ValueError):
            continue
        if turn.run_id == work.run_id and work.work_id in turn.work_ids:
            return turn
    return None


def _terminal_audit_binding(
    work: WorkItem,
    result: WorkResult,
    turn: TurnEnded,
) -> TerminalAuditBinding:
    """Bind a terminal audit to the exact settled result and closed turn."""
    if turn.run_id != work.run_id or work.work_id not in turn.work_ids:
        raise LifecycleError("terminal audit turn does not belong to settled work")
    return TerminalAuditBinding(
        run_id=work.run_id,
        work_id=work.work_id,
        turn_id=turn.turn_id,
        outcome_kind=result.kind,
        result_sha256=sha256_text(canonical_json(result.to_json_dict())),
        end_reason=turn.end_reason,
    )


__all__ = [
    "BoundedControlInputConsumer",
    "ControlHandler",
    "ControlInputConsumer",
    "ControlInputRejectedError",
    "GoalBoardCommandConsumer",
    "LifecycleOwnerLoop",
    "TerminalAuditor",
    "WorkExecutor",
]
