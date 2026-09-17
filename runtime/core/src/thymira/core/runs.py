"""Run lifecycle service backed by the authoritative local Run event log."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from thymira.agents.route_policy import record_model_route_policy_snapshot
from thymira.agents.settlement import finalize_parked_invocation
from thymira.core.control_plane import RunController, RunEventLog
from thymira.core.dispatch import ExecutionDispatchError
from thymira.core.execution_review import (
    EXECUTION_START_RESUME_TARGET,
    block_rejected_execution,
    continuation_target,
    human_approval_outcome,
    parked_review_decision,
    pending_review_decision,
    review_resume_target,
    validate_execution_resolver,
)
from thymira.core.governance_binding import final_governance_binding, policy_decision_binding
from thymira.core.lifecycle_worker import (
    ControlInputConsumer,
    GoalBoardCommandConsumer,
    LifecycleOwnerLoop,
)
from thymira.core.provenance import capture_run_environment
from thymira.core.risk_interview import RiskInterviewNotPendingError, RiskInterviewService
from thymira.core.run_state import RunStage, RunTransitionKind
from thymira.core.sessions import SessionNotFoundError
from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    Actor,
    EventType,
    Id,
    JsonObject,
    PolicyDecision,
    PublicationReceipt,
    Run,
    RunCondition,
    RunOutcome,
    RunState,
    RunStatus,
    Session,
    StopReason,
    WorkItem,
    new_id,
)
from thymira.schemas import (
    RunStage as SchemaRunStage,
)

if TYPE_CHECKING:
    from datetime import datetime

    from thymira.core.audit_freshness import AuditFreshnessService
    from thymira.core.dispatch import ExecutionDispatcher
    from thymira.core.sessions import SessionService
    from thymira.policies import Gate
    from thymira.state import LifecycleRepository, LocalRunStore, Page, RunHandle

HashProvider = Callable[[], str]
HASH_LENGTH = 64


class RunNotFoundError(LookupError):
    """Raised when a requested Run does not exist in the local store."""


class RunNotResumableError(RuntimeError):
    """Raised when a Run cannot be continued by the configured execution dispatcher."""


class RunAuditStaleError(RuntimeError):
    """Raised when a continuation would reuse an obsolete audit snapshot."""


class RunApprovalError(ValueError):
    """Base error for an invalid human decision request."""


class RunNotAwaitingApprovalError(RunApprovalError):
    """Raised when a human decision targets a Run without a pending approval."""


class RunInformationRequiredError(RunApprovalError):
    """Raised when a Run must receive its pending activity-profile answer before resuming."""


class RunService:
    """Create and read Runs while delegating every lifecycle transition to ``RunController``."""

    def __init__(
        self,
        store: LocalRunStore,
        sessions: SessionService,
        *,
        dispatcher: ExecutionDispatcher | None = None,
        policy_sha256: str | HashProvider | None = None,
        graph_definition_hash: str | HashProvider | None = None,
        risk_interview: RiskInterviewService | None = None,
        gate_factory: Callable[[Id], Gate] | None = None,
        audit_freshness: AuditFreshnessService | None = None,
        lifecycle_repository: LifecycleRepository | None = None,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._controller = RunController(store)
        self._dispatcher = dispatcher
        self._policy_sha256 = policy_sha256
        self._graph_definition_hash = graph_definition_hash
        self._risk_interview = risk_interview
        self._gate_factory = gate_factory
        self._audit_freshness = audit_freshness
        self._lifecycle = lifecycle_repository

    def create_run(
        self,
        session_id: Id | None,
        prompt: str,
        *,
        actor: Actor,
        workspace: Path,
        background: bool = False,
        dispatch: bool = True,
        idempotency_key: str | None = None,
        request_binding_sha256: str | None = None,
        project_id: Id | None = None,
        session_client: str = "runtime",
    ) -> Run:
        """Create a Run, capture provenance, and optionally submit it to the dispatcher.

        ``background`` is reserved for the queue-backed dispatcher. The MVP inline dispatcher
        remains synchronous, while the returned value is always the creation snapshot. ``dispatch``
        lets an API boundary persist the creation fact first and enqueue execution after returning
        its response.
        """
        del background
        if self._lifecycle is not None:
            run, _ = self.create_run_published(
                session_id,
                prompt,
                actor=actor,
                workspace=workspace,
                dispatch=dispatch,
                idempotency_key=idempotency_key,
                request_binding_sha256=request_binding_sha256,
                project_id=project_id,
                session_client=session_client,
            )
            return run
        self._require_event_dependencies()
        if session_id is None:
            raise SessionNotFoundError("a session is required without lifecycle publication")
        session = self._sessions.get_session(session_id)
        # Provenance is captured before the Run is built, not after, so the commit it ran from
        # reaches the record itself and not only the creation event's payload. Outside a
        # repository `available` is False and the field simply stays unset.
        environment = capture_run_environment(cwd=Path(workspace))
        run = Run(
            id=new_id("run"),
            project_id=session.project_id,
            session_id=session.id,
            prompt=prompt,
            git_commit=environment.source_control.commit,
            # A resumed Run must use this creation-time identity even when its Session row is
            # later replaced by a repository writer or the live router configuration changes.
            model_route_policy=session.model_route_policy,
        )
        self._store.create(
            run,
            actor=actor,
            payload={
                "run_environment": environment.to_json_dict(),
                "policy_sha256": self._resolve_hash(self._policy_sha256),
                "graph_definition_hash": self._resolve_hash(self._graph_definition_hash),
            },
        )
        # Publish the Session allowlist into the Run's authoritative chain before dispatch. The
        # Session remains the source snapshot and the Run keeps an immutable copy for resume;
        # this event supplies addressable session provenance without a second event log.
        record_model_route_policy_snapshot(
            RunEventLog(self._store, run.id),
            run.model_route_policy,
            actor=actor,
            subject_id=session.id,
            run_id=run.id,
        )
        # The local MVP has no cross-file transaction; create the Run before linking its Session.
        self._sessions.attach_run(session.id, run.id)
        created = self.get_run(run.id)
        if self._dispatcher is not None and dispatch:
            try:
                self._dispatcher.submit(run.id)
            except ExecutionDispatchError as exc:
                if self._controller.current_state(run.id).condition is not RunCondition.TERMINAL:
                    self.fail(run.id, actor=Actor.system(), error=str(exc))
        return created

    def create_run_published(
        self,
        session_id: Id | None,
        prompt: str,
        *,
        actor: Actor,
        workspace: Path,
        dispatch: bool = True,
        idempotency_key: str | None = None,
        request_binding_sha256: str | None = None,
        project_id: Id | None = None,
        session_client: str = "runtime",
        session_draft: Session | None = None,
    ) -> tuple[Run, PublicationReceipt]:
        """Create one staged Run and return its durable publication receipt.

        This method is the API composition path for lifecycle-aware creation.  It materialises the
        Run and inverse Session link before dispatch, so a caller can correlate the initial work
        notification with ``PublicationReceipt.work_ids``.  The owner used for publication is
        released before execution dispatch, allowing a worker or inline consumer to acquire the
        same Run's fenced writer.
        """
        if self._lifecycle is None:
            raise RuntimeError("lifecycle repository is required for staged Run publication")
        self._require_event_dependencies()
        session = self._resolve_publication_session(
            session_id,
            actor=actor,
            project_id=project_id,
            session_client=session_client,
            idempotency_key=idempotency_key,
            session_draft=session_draft,
        )
        environment = capture_run_environment(cwd=Path(workspace))
        run = Run(
            id=new_id("run"),
            project_id=session.project_id,
            session_id=session.id,
            prompt=prompt,
            git_commit=environment.source_control.commit,
            # Mirrors create_run: a resumed Run must use this creation-time identity even when its
            # Session row is later replaced by a repository writer or the live router
            # configuration changes. Omitting it silently defaults to the empty
            # ModelRoutePolicy.unavailable() (Run.model_route_policy's field default), which fails
            # every later provider seam closed for the life of the Run.
            model_route_policy=session.model_route_policy,
        )
        creation_payload: JsonObject = cast(
            "JsonObject",
            {
                "run_environment": environment.to_json_dict(),
                "policy_sha256": self._resolve_hash(self._policy_sha256),
                "graph_definition_hash": self._resolve_hash(self._graph_definition_hash),
            },
        )
        computed_binding_sha256 = _creation_request_digest(
            actor=actor,
            session_id=session.id,
            project_id=session.project_id,
            prompt=prompt,
            workspace=Path(workspace),
            session_client=session_client,
            creation_payload=cast("dict[str, object]", creation_payload),
        )
        if idempotency_key is not None:
            if (
                request_binding_sha256 is not None
                and request_binding_sha256 != computed_binding_sha256
            ):
                raise ValueError("request_binding_sha256 does not match the creation request")
            request_binding_sha256 = computed_binding_sha256
        if idempotency_key is None and request_binding_sha256 is not None:
            raise ValueError("request_binding_sha256 requires idempotency_key")
        initial_work = WorkItem(
            run_id=run.id,
            ordinal=0,
            kind="run.execute",
            idempotency_key=f"run-create:{run.id}",
            payload={"action": "submit"},
        )
        prepared = self._lifecycle.prepare_publication(
            session,
            run,
            initial_work,
            creation_payload=cast("dict[str, object]", creation_payload),
            creation_actor=actor,
            idempotency_key=idempotency_key,
            request_binding_sha256=request_binding_sha256,
        )
        handle = self._lifecycle.commit_publication(prepared)
        try:
            receipt = handle.receipt
            if receipt is None:
                raise RuntimeError("lifecycle publication returned no receipt")
        finally:
            handle.close()
        published_run_id = receipt.run_id
        if self._dispatcher is not None and dispatch:
            try:
                self._dispatcher.submit(published_run_id)
            except ExecutionDispatchError as exc:
                if (
                    self._controller.current_state(published_run_id).condition
                    is not RunCondition.TERMINAL
                ):
                    self.fail(published_run_id, actor=Actor.system(), error=str(exc))
        return self.get_run(published_run_id), receipt

    def _resolve_publication_session(
        self,
        session_id: Id | None,
        *,
        actor: Actor,
        project_id: Id | None,
        session_client: str,
        idempotency_key: str | None,
        session_draft: Session | None,
    ) -> Session:
        """Resolve an existing Session or construct an unpublished creation draft.

        The lifecycle repository persists a new Session only as the inverse link in the same
        publication commit as its Run.  An implicit session therefore receives a deterministic
        id while an idempotency key is present; retries can rebuild the same request binding
        without first writing a client-owned Session record.
        """
        if session_draft is not None:
            if session_id is not None and session_draft.id != session_id:
                raise SessionNotFoundError("session draft does not match session_id")
            if project_id is not None and session_draft.project_id != project_id:
                raise SessionNotFoundError("session draft does not match project_id")
            return session_draft
        if session_id is not None:
            return self._sessions.get_session(session_id)
        if project_id is None:
            raise SessionNotFoundError("project_id is required when session_id is omitted")
        if idempotency_key is None:
            resolved_id = new_id("session")
        else:
            seed = canonical_json(
                {
                    "actor": actor.to_json_dict(),
                    "project_id": project_id,
                    "client": session_client,
                    "idempotency_key": idempotency_key,
                }
            )
            resolved_id = f"session_{sha256_text(seed)[:32]}"
        return Session(id=resolved_id, project_id=project_id, client=session_client)

    def get_run(self, run_id: Id) -> Run:
        """Return a Run reconstructed from its verified event history."""
        try:
            stored = (
                self._lifecycle.get_run(run_id)
                if self._lifecycle is not None
                else self._store.get(run_id)
            )
            return self._hydrate(stored)
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"run {run_id!r} was not found") from exc

    def get(self, run_id: Id) -> Run:
        """Compatibility alias for :meth:`get_run`."""
        return self.get_run(run_id)

    def controller_for_owner(self, live_owner: RunHandle) -> RunController:
        """Return a transition controller whose writes require this live owner handle.

        The ordinary ``RunController(self._store)`` remains useful for read-only projections and
        legacy single-process setup. Lifecycle consumers call this method so transition events,
        state projections and event-version CAS all pass through the repository's live OS lock
        and epoch fence.
        """
        if self._lifecycle is None:
            raise RuntimeError("an owned controller requires a lifecycle repository")
        self.get_run(live_owner.run_id)
        return RunController(self._store, writer=live_owner.append_transition)

    def build_owner_loop(
        self,
        worker_id: str,
        *,
        goal_board_consumer: GoalBoardCommandConsumer | None = None,
        control_consumer: ControlInputConsumer | None = None,
    ) -> LifecycleOwnerLoop:
        """Construct the single owner loop from this service's lifecycle repository.

        API composition owns this service and only enqueues controls.  A worker composition calls
        this factory with its process identity and bounded consumers, ensuring inline and broker
        execution share the same repository claim/owner/settlement path.
        """
        if self._lifecycle is None:
            raise RuntimeError("an owner loop requires a lifecycle repository")
        return LifecycleOwnerLoop(
            self._lifecycle,
            worker_id,
            goal_board_consumer=goal_board_consumer,
            control_consumer=control_consumer,
        )

    def _resume_or_fail(self, dispatcher: ExecutionDispatcher, run_id: Id) -> None:
        """Resume through ``dispatcher``, recording a terminal FAILED instead of a raw crash.

        Mirrors ``create_run``'s handling of ``submit`` (bug-hunt: the two were asymmetric --
        ``InlineDispatcher.resume`` had no equivalent safety net, so a resume-time crash left a
        Run with no terminal event -- ``status`` stuck wherever it was, ``resume`` returning 409
        forever -- instead of a consistent, auditable ``RUN_FAILED``).
        """
        try:
            dispatcher.resume(run_id)
        except ExecutionDispatchError as exc:
            if self._controller.current_state(run_id).condition is not RunCondition.TERMINAL:
                self.fail(run_id, actor=Actor.system(), error=str(exc))

    def resume(self, run_id: Id, *, actor: Actor) -> Run:
        """Continue a Run from its latest verified checkpoint without replaying completed work.

        Terminal Runs are idempotent no-ops.  Every non-terminal continuation verifies the
        authoritative event chain before any transition or dispatcher call is made.
        """
        run = self.get_run(run_id)
        current = self._controller.current_state(run_id)
        if current.condition.value == "terminal":
            return run
        _refuse_active_resume(run_id, current.condition)
        if self._dispatcher is None:
            raise RunNotResumableError("no execution dispatcher is configured")
        parked: PolicyDecision | None = None
        if (
            current.condition.value == "waiting"
            and current.wait_reason is not None
            and current.wait_reason.value == "approval"
        ):
            parked = self._parked_decision(run_id)
            target = (
                review_resume_target(self._store.events(run_id), parked.id)
                if parked is not None
                else None
            )
            # A park that persisted its target (an execution start, the interview's question
            # limit) answers a plain resume from its recorded human outcome: still unanswered
            # refuses, a rejection blocks. Otherwise an out-of-band rejection would be
            # discarded here and the interview would re-escalate the same review forever.
            if target is not None and parked is not None:
                outcome = human_approval_outcome(self._store.events(run_id), parked)
                if outcome is None:
                    raise RunNotAwaitingApprovalError(
                        f"run {run_id!r} is waiting for human approval"
                    )
                if outcome is False:
                    block_rejected_execution(
                        self._controller, run_id, self._store.events(run_id), parked, actor
                    )
                    return self.get_run(run_id)
            elif self._pending_approval(run_id) is not None:
                raise RunNotAwaitingApprovalError(f"run {run_id!r} is waiting for human approval")
        self._enforce_audit_freshness(run_id, parked_decision=parked)
        if current.condition.value == "waiting" and current.wait_reason is not None:
            if current.wait_reason.value == "information" and (
                self._risk_interview is None or self._risk_interview.has_pending_question(run_id)
            ):
                raise RunInformationRequiredError(
                    f"run {run_id!r} is waiting for an activity-profile answer"
                )
            payload: dict[str, Any] = {"resumed_by": actor.to_json_dict()}
            if current.wait_reason.value == "information":
                # No pending interview question: MIRA's preflight parked the Run on its own
                # uncertain inherent-risk judgement (`MiraSubgraph._request_human_context`), which
                # `answer` has nothing to resolve. Re-entering preflight is the only way forward.
                payload["resume_from"] = "information"
            elif current.wait_reason.value == "approval":
                # Answered out of band: re-enter where the Run was parked. A tool-call review
                # parked it inside `thy`; a findings review parked it at `gate`.
                payload["resume_from"] = continuation_target(self._store.events(run_id), parked)
            self._controller.advance(run_id, RunTransitionKind.RESUME, payload=payload)
        elif current.condition.value == "paused":
            self._controller.advance(
                run_id,
                RunTransitionKind.RESUME,
                payload={"resumed_by": actor.to_json_dict()},
            )
        self._resume_or_fail(self._dispatcher, run_id)
        return self.get_run(run_id)

    def cancel(self, run_id: Id, *, actor: Actor, reason: str | None = None) -> Run:
        """Abort a Run at its caller's request and close every credit raised inside it.

        The production call site for ``RunTransitionKind.CANCEL``, which the state machine has
        always accepted and nothing ever invoked. The transition goes through
        :class:`RunController` like every other persistent transition (ADR-0008); this method
        adds no authority of its own, it records who asked and why.

        Cancelling is what a vanished or changed-its-mind caller leaves behind, so it must be
        safe to repeat: a terminal Run is an idempotent no-op, and a retry after a dropped
        response writes no second transition and cannot corrupt the chain. The Run hydrates to
        ``RunStatus.BLOCKED``, the Contract 0.2 compatibility view of
        ``RunOutcome.CANCELLED``.

        Args:
            run_id: The Run to abort.
            actor: Who asked for the abort; recorded on the transition.
            reason: Optional free text recorded beside the actor.

        Returns:
            The hydrated Run, terminal unless it already was.
        """
        run = self.get_run(run_id)
        if self._controller.current_state(run_id).condition.value == "terminal":
            return run
        payload: dict[str, Any] = {"cancelled_by": actor.to_json_dict()}
        if reason is not None:
            payload["reason"] = reason
        finalize_parked_invocation(
            RunEventLog(self._store, run_id),
            actor=actor,
            stop_reason=StopReason.STOPPED,
            diagnostics=reason,
        )
        self._controller.advance(run_id, RunTransitionKind.CANCEL, payload=payload)
        return self.get_run(run_id)

    def answer_risk_interview(self, run_id: Id, *, answer: str, actor: Actor) -> Run:
        """Persist one pending activity-profile answer and resume from its interview checkpoint."""
        run = self.get_run(run_id)
        current = self._controller.current_state(run_id)
        if (
            current.condition.value != "waiting"
            or current.wait_reason is None
            or current.wait_reason.value != "information"
        ):
            message = f"run {run_id!r} is not awaiting activity information"
            raise RunInformationRequiredError(message)
        if self._risk_interview is None:
            raise RunNotResumableError("no risk-interview service is configured")
        if self._dispatcher is None:
            raise RunNotResumableError("no execution dispatcher is configured")
        try:
            self._risk_interview.answer(run, answer=answer, actor=actor)
        except RiskInterviewNotPendingError as exc:
            raise RunInformationRequiredError(str(exc)) from exc
        self._controller.advance(
            run_id,
            RunTransitionKind.RESUME,
            payload={
                "answered_by": actor.to_json_dict(),
                "resume_from": "information",
            },
        )
        self._resume_or_fail(self._dispatcher, run_id)
        return self.get_run(run_id)

    def resolve_approval(
        self,
        run_id: Id,
        *,
        gate: Gate,
        actor: Actor,
        note: str | None = None,
    ) -> Run:
        """Resolve a pending Gate review and continue or block the Run accordingly.

        The review answered is the one the Run is parked on -- the decision its own
        ``wait_for_approval`` transition names -- so a second review recorded after the park
        cannot take its answer; a Run waiting with no park record falls back to its latest pending
        decision. A findings review decides the Run: an approval resumes it at the Gate, a
        rejection blocks it. A tool-call review decides one call inside an unfinished THY pass, so
        either answer resumes execution instead.
        """
        self.get_run(run_id)
        current = self._controller.current_state(run_id)
        if (
            current.condition.value != "waiting"
            or current.wait_reason is None
            or current.wait_reason.value != "approval"
        ):
            raise RunNotAwaitingApprovalError(f"run {run_id!r} is not awaiting human approval")
        decision = self._parked_decision(run_id) or self._pending_approval(run_id)
        if decision is None:
            raise RunNotAwaitingApprovalError(f"run {run_id!r} has no pending approval")
        resume_target = review_resume_target(self._store.events(run_id), decision.id)
        if resume_target == EXECUTION_START_RESUME_TARGET:
            try:
                validate_execution_resolver(gate, actor)
            except ValueError as exc:
                raise RunApprovalError(str(exc)) from exc
        self._enforce_audit_freshness(run_id, gate=gate, parked_decision=decision)
        approved = gate.resolve_pending_approval(decision, note=note)
        if (
            resume_target == EXECUTION_START_RESUME_TARGET
            and human_approval_outcome(self._store.events(run_id), decision) is not approved
        ):
            raise RunApprovalError("Gate did not record this human answer")
        if decision.subject_kind == "tool_call":
            # A tool-call review parks the Run mid-execution. Either answer resumes it: an
            # approval lets the Tool Manager run that exact call once (its ticket), a rejection
            # is recorded as a denial the agent adapts to. Neither ends the Run -- only the
            # findings review below can block it.
            self._controller.advance(
                run_id,
                RunTransitionKind.RESUME,
                payload={
                    ("approved_by" if approved else "rejected_by"): actor.to_json_dict(),
                    "approved": approved,
                    "resume_from": "execution",
                },
            )
            if self._dispatcher is not None:
                self._resume_or_fail(self._dispatcher, run_id)
            return self.get_run(run_id)
        if approved:
            self._controller.advance(
                run_id,
                RunTransitionKind.RESUME,
                payload={
                    "approved_by": actor.to_json_dict(),
                    "resume_from": resume_target or "approval",
                },
            )
            if self._dispatcher is not None:
                self._resume_or_fail(self._dispatcher, run_id)
        else:
            payload: dict[str, Any] = {
                "rejected_by": actor.to_json_dict(),
                **policy_decision_binding(decision),
            }
            if decision.subject_kind == "findings":
                payload.update(
                    final_governance_binding(
                        run_id,
                        decision,
                        self._store.events(run_id),
                    )
                )
            self._controller.advance(
                run_id,
                RunTransitionKind.BLOCK,
                payload=payload,
            )
        return self.get_run(run_id)

    def _enforce_audit_freshness(
        self,
        run_id: Id,
        *,
        gate: Gate | None = None,
        parked_decision: PolicyDecision | None = None,
    ) -> None:
        """Block a continuation when MIRA's persisted audit is no longer current."""
        if self._audit_freshness is None:
            return
        active_gate = gate
        if active_gate is None:
            if self._gate_factory is None:
                return
            active_gate = self._gate_factory(run_id)
        assessment = self._audit_freshness.authorize_continuation(
            run_id,
            active_gate,
            parked_decision=parked_decision,
        )
        if assessment is None or assessment.fresh:
            return
        current = self._controller.current_state(run_id)
        if current.condition.value != "terminal":
            self._controller.advance(
                run_id,
                RunTransitionKind.BLOCK,
                payload={
                    "audit_freshness": assessment.control.to_json_dict(),
                    "reason": assessment.control.detail,
                },
            )
        raise RunAuditStaleError(
            f"run {run_id!r} cannot continue: audit snapshot is stale: {assessment.control.detail}"
        )

    def list_runs(
        self,
        *,
        project_id: Id | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Run]:
        """List event-backed Runs through the stable local cursor contract.

        Hydration is pushed down into the store as a callback rather than applied to the returned
        page. A Run's current status comes from :meth:`RunController.current_state`, which lives in
        this ``core`` layer and cannot move down into ``state`` without inverting the import order
        (``core -> ... -> state``); and a ``status`` filter applied here, after the store has
        already paged, would return short pages and a ``next_cursor`` that disagrees with the next
        page's filter. Passing :meth:`_hydrate` lets the store resolve each Run's current status
        before it filters and pages, so ``status`` matches the current status and the page size
        and cursor contract stay correct. The store returns already-hydrated bodies.
        """
        return self._store.list_runs(
            project_id=project_id,
            status=status,
            limit=limit,
            cursor=cursor,
            hydrate=self._hydrate,
        )

    def advance(self, run_id: Id, status: RunStatus, *, at: datetime | None = None) -> Run:
        """Advance ordinary lifecycle progress through the Contract 0.3 state machine."""
        del at  # Event timestamps are assigned by the authoritative event log.
        current = self._controller.current_state(run_id)
        command = _command_for_status(current.stage, status)
        self._controller.advance(run_id, command)
        return self.get_run(run_id)

    def transition(self, run_id: Id, status: RunStatus, *, at: datetime | None = None) -> Run:
        """Compatibility alias for :meth:`advance`."""
        return self.advance(run_id, status, at=at)

    def complete(self, run_id: Id, *, actor: Actor, at: datetime | None = None) -> Run:
        """Move a Run to reporting when needed, then record its terminal completion fact."""
        del at
        current = self._controller.current_state(run_id)
        if current.stage is RunStage.AUDITING:
            self._controller.advance(run_id, RunTransitionKind.BEGIN_REPORTING)
        self._controller.advance(run_id, RunTransitionKind.COMPLETE)
        self._store.append(
            run_id,
            EventType.RUN_COMPLETED,
            actor,
            subject_id=run_id,
            expected_version=self._store.version(run_id),
        )
        return self.get_run(run_id)

    def fail(
        self,
        run_id: Id,
        *,
        actor: Actor,
        error: str | None = None,
        at: datetime | None = None,
    ) -> Run:
        """Move a Run to the failed terminal state and retain a safe error summary."""
        del at
        finalize_parked_invocation(
            RunEventLog(self._store, run_id),
            actor=actor,
            stop_reason=StopReason.ABNORMAL,
            diagnostics=error,
        )
        self._controller.advance(run_id, RunTransitionKind.FAIL, payload={"error": error})
        self._store.append(
            run_id,
            EventType.RUN_FAILED,
            actor,
            {"error": error} if error is not None else None,
            subject_id=run_id,
            expected_version=self._store.version(run_id),
        )
        return self.get_run(run_id)

    def _hydrate(self, run: Run) -> Run:
        """Expose legacy ``RunStatus`` and timestamps as a view over Contract 0.3 state."""
        state = self._controller.current_state(run.id).state
        updates: dict[str, object] = {"status": _status_for_state(state)}
        events = self._store.events(run.id)
        started = next(
            (
                event.ts
                for event in events
                if event.type is EventType.RUN_TRANSITIONED
                and event.payload.get("command") == "start"
            ),
            None,
        )
        terminal = next(
            (
                event.ts
                for event in events
                if event.type is EventType.RUN_TRANSITIONED
                and event.payload.get("condition") == "terminal"
            ),
            None,
        )
        if started is not None:
            updates["started_at"] = started
        if terminal is not None:
            updates["completed_at"] = terminal
        failed = next((event for event in events if event.type is EventType.RUN_FAILED), None)
        if failed is not None:
            updates["error"] = failed.payload.get("error")
        return run.model_copy(update=updates)

    def _require_event_dependencies(self) -> None:
        """Ensure creation contains the hashes required for reproducible provenance."""
        if self._policy_sha256 is None or self._graph_definition_hash is None:
            raise RuntimeError(
                "create_run requires policy_sha256 and graph_definition_hash providers"
            )

    def _pending_approval(self, run_id: Id) -> PolicyDecision | None:
        """Return the latest unresolved human-review decision for a verified Run.

        An answer names its decision under either recorded field name (the frozen
        ``Approval.policy_decision_id`` on the control-plane path, ``decision_id`` on the Gate's
        own), so the resolved set is folded through
        :func:`~thymira.schemas.approval_decision_id`. Reading one name only left a Run a human
        had really approved reporting as still waiting, and :meth:`resume` refused it forever.
        """
        return pending_review_decision(self._store.events(run_id), run_id)

    def _parked_decision(self, run_id: Id) -> PolicyDecision | None:
        """The decision the Run was last parked on, read off its own ``wait_for_approval``.

        The single record behind both the branch a resume takes and the review an answer
        resolves. Reading the *latest pending* decision instead let a second review recorded
        after the park take the answer meant for it: the parked review stayed unanswered, and a
        run-level one even sent the resume to the wrong node. ``None`` when the Run names no
        decision this method can read -- either it holds no ``wait_for_approval`` transition at
        all, or the latest one carries no ``policy_decision`` mapping (a park written before the
        record existed) -- which leaves :meth:`_pending_approval` as the fallback. A
        ``policy_decision`` that *is* a mapping but does not validate is a corrupt record, not a
        missing one, and raises.
        """
        return parked_review_decision(self._store.events(run_id), run_id)

    @staticmethod
    def _resolve_hash(value: str | HashProvider | None) -> str:
        """Resolve and validate one content hash."""
        resolved = value if isinstance(value, str) else value() if value is not None else None
        if (
            resolved is None
            or len(resolved) != HASH_LENGTH
            or any(char not in "0123456789abcdef" for char in resolved)
        ):
            raise ValueError("runtime evidence hashes must be 64 lowercase hexadecimal characters")
        return resolved


def _command_for_status(stage: RunStage, status: RunStatus) -> RunTransitionKind:
    """Translate the compatibility status API to one unambiguous state command."""
    commands: dict[tuple[RunStage, RunStatus], RunTransitionKind] = {
        (RunStage.CREATED, RunStatus.PLANNING): RunTransitionKind.START,
        (RunStage.PLANNING, RunStatus.RUNNING): RunTransitionKind.BEGIN_EXECUTION,
        (RunStage.EXECUTING, RunStatus.EXPERIMENTING): RunTransitionKind.BEGIN_EXPERIMENT,
        (RunStage.EXECUTING, RunStatus.AUDITING): RunTransitionKind.BEGIN_AUDIT,
        (RunStage.EXPERIMENTING, RunStatus.RUNNING): RunTransitionKind.BEGIN_EXECUTION,
        (RunStage.EXPERIMENTING, RunStatus.AUDITING): RunTransitionKind.BEGIN_AUDIT,
    }
    try:
        return commands[(stage, status)]
    except KeyError as exc:
        raise ValueError(
            f"run transition cannot advance from {stage.value} to compatibility status {status}"
        ) from exc


def _refuse_active_resume(run_id: Id, condition: RunCondition) -> None:
    """Prevent a second dispatcher from racing the owner of an active Run."""
    if condition is RunCondition.ACTIVE:
        raise RunNotResumableError(f"run {run_id!r} is already active")


def _status_for_state(state: RunState) -> RunStatus:
    """Map composite state to the legacy public status until Contract 0.3 is fully exposed."""
    if state.condition is RunCondition.TERMINAL:
        if state.outcome is None:
            raise ValueError("terminal Run state must contain an outcome")
        return {
            RunOutcome.COMPLETED: RunStatus.COMPLETED,
            RunOutcome.BLOCKED: RunStatus.BLOCKED,
            RunOutcome.FAILED: RunStatus.FAILED,
            # Contract 0.2 has no CANCELLED value; BLOCKED is the safe compatibility view.
            RunOutcome.CANCELLED: RunStatus.BLOCKED,
        }[state.outcome]
    if state.condition is RunCondition.WAITING:
        return RunStatus.WAITING_FOR_APPROVAL
    return {
        SchemaRunStage.CREATED: RunStatus.CREATED,
        SchemaRunStage.PLANNING: RunStatus.PLANNING,
        SchemaRunStage.EXECUTING: RunStatus.RUNNING,
        SchemaRunStage.EXPERIMENTING: RunStatus.EXPERIMENTING,
        SchemaRunStage.AUDITING: RunStatus.AUDITING,
        SchemaRunStage.REPORTING: RunStatus.AUDITING,
    }[state.stage]


def _creation_request_digest(
    *,
    actor: Actor,
    session_id: Id,
    project_id: Id,
    prompt: str,
    workspace: Path,
    session_client: str,
    creation_payload: dict[str, object],
) -> str:
    """Hash the authenticated creation scope while excluding volatile capture timestamps.

    ``run_environment`` is durable provenance, not caller request input; it contains a capture
    timestamp and may therefore differ on a retry of the same request.  Policy and graph hashes
    remain part of the binding because they define the creation semantics selected by the runtime.
    """
    request_payload = {
        key: value for key, value in creation_payload.items() if key != "run_environment"
    }
    return sha256_text(
        canonical_json(
            {
                "actor": actor.to_json_dict(),
                "session_id": session_id,
                "project_id": project_id,
                "prompt": prompt,
                "workspace": str(workspace.resolve()),
                "session_client": session_client,
                "creation_payload": request_payload,
            }
        )
    )


__all__ = [
    "RunApprovalError",
    "RunInformationRequiredError",
    "RunNotAwaitingApprovalError",
    "RunNotFoundError",
    "RunNotResumableError",
    "RunService",
]
