"""Run creation, inspection and listing routes for the MVP API."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission, resolve_actor
from thymira.api.routes._scope import (
    _assert_declared_actor,
    _configured_project,
    _scoped_run,
    _validate_run_id,
)
from thymira.api.schemas import (
    AnswerRiskInterviewRequest,
    CreateRunRequest,
    DelegateRequest,
    DurablePlanResponse,
    HumanDecisionRequest,
    PendingRiskQuestionResponse,
    ResumeRunRequest,
    RiskInterviewResponse,
    RunCancelRequest,
    RunPage,
    RunPlan,
    RunTraceResponse,
)
from thymira.core import (
    PHASE_ORDER,
    ExecutionDispatchError,
    RunAuditStaleError,
    RunInformationRequiredError,
    RunNotAwaitingApprovalError,
    RunNotFoundError,
    RunNotResumableError,
    SessionNotFoundError,
    SessionProjectMismatchError,
    default_activity_profile,
)
from thymira.observability import trace_url
from thymira.schemas import Actor, Agent, Id, Layer, Run, RunStatus, Task, new_id

router = APIRouter(prefix="/runs", tags=["runs"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_REQUIRE_WRITE = Depends(require_permission(Permission.WRITE))
_REQUIRE_APPROVE = Depends(require_permission(Permission.APPROVE))
_PROJECT_ID = Query(default=None)
_STATUS = Query(default=None)
_LIMIT = Query(default=50, ge=1)
_CURSOR = Query(default=None)


@router.post("", response_model=Run, status_code=201, dependencies=[_REQUIRE_WRITE])
def create_run(
    payload: CreateRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Create a Run in the API's configured project."""
    project_id = _configured_project(deps)
    actor = resolve_actor(request)
    if payload.project_id is not None and payload.project_id != project_id:
        raise problem(
            404,
            "project_not_found",
            f"Project {payload.project_id!r} is not configured for this API.",
        )

    if idempotency_key is None:
        run = _create_run(payload, deps, project_id=project_id, actor=actor)
        background_tasks.add_task(_submit_run, deps, run.id)
        return run
    key = idempotency_key.strip()
    if not key:
        raise problem(422, "invalid_idempotency_key", "Idempotency-Key must not be empty.")
    with deps.idempotency_lock:
        existing_id = deps.idempotency_journal.run_id_for(key)
        if existing_id is not None:
            try:
                return deps.run_service.get_run(existing_id)
            except RunNotFoundError as exc:
                raise problem(
                    409,
                    "idempotency_key_stale",
                    "The idempotency key points to a missing Run.",
                ) from exc
        run = _create_run(payload, deps, project_id=project_id, actor=actor)
        deps.idempotency_journal.record_run(key, run.id)
        background_tasks.add_task(_submit_run, deps, run.id)
        return run


def _create_run(
    payload: CreateRunRequest,
    deps: RuntimeDeps,
    *,
    project_id: Id,
    actor: Actor,
) -> Run:
    """Create one Run after request scope and idempotency checks have completed."""
    try:
        session = deps.session_service.resolve(
            project_id=project_id,
            client=payload.client,
            session_id=payload.session_id,
        )
    except SessionNotFoundError as exc:
        raise problem(404, "session_not_found", str(exc)) from exc
    except SessionProjectMismatchError as exc:
        raise problem(404, "session_not_found", str(exc)) from exc

    if deps.project_resolution is None:
        raise AssertionError("project resolution was checked before session resolution")
    try:
        return deps.run_service.create_run(
            session.id,
            payload.prompt,
            actor=actor,
            workspace=deps.project_resolution.workspace,
            dispatch=False,
        )
    except SessionNotFoundError as exc:
        raise problem(404, "session_not_found", str(exc)) from exc


def _submit_run(deps: RuntimeDeps, run_id: Id) -> None:
    """Execute a newly created Run after its HTTP response has been returned."""
    try:
        deps.dispatcher.submit(run_id)
    except ExecutionDispatchError as exc:
        deps.run_service.fail(run_id, actor=Actor.system(), error=str(exc))


@router.get("", response_model=RunPage, dependencies=[_REQUIRE_READ])
def list_runs(
    project_id: Id | None = _PROJECT_ID,
    status: RunStatus | None = _STATUS,
    limit: int = _LIMIT,
    cursor: str | None = _CURSOR,
    deps: RuntimeDeps = _DEPS,
) -> RunPage:
    """List Runs for the API's configured project using the local cursor contract."""
    configured_project_id = _configured_project(deps)
    if project_id is not None and project_id != configured_project_id:
        raise problem(
            404,
            "project_not_found",
            f"Project {project_id!r} is not configured for this API.",
        )
    try:
        page = deps.run_service.list_runs(
            project_id=configured_project_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    except (TypeError, ValueError) as exc:
        raise problem(422, "invalid_cursor", str(exc)) from exc
    return RunPage(items=page.items, next_cursor=page.next_cursor)


@router.get("/{run_id}", response_model=Run, dependencies=[_REQUIRE_READ])
def get_run(
    run_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Return one Run when it belongs to the API's configured project."""
    run_id = _validate_run_id(run_id)
    return _scoped_run(deps, run_id)


def _risk_interview_response(run: Run, deps: RuntimeDeps) -> RiskInterviewResponse:
    """Build the read model from event-backed intake evidence without recording a new fact."""
    project_config = deps.project_resolution.config if deps.project_resolution is not None else None
    status = deps.risk_interview.status(
        run,
        default_activity_profile(run, project_config=project_config),
    )
    question = status.pending_question
    pending = (
        PendingRiskQuestionResponse(
            field=question.field,
            question=question.question,
            profile_id=question.profile_id,
            profile_version=question.profile_version,
            question_number=question.question_number,
        )
        if question is not None
        else None
    )
    return RiskInterviewResponse(
        run=run,
        profile=status.profile,
        pending_question=pending,
        requires_human_review=status.requires_human_review,
    )


@router.get(
    "/{run_id}/risk-interview",
    response_model=RiskInterviewResponse,
    dependencies=[_REQUIRE_READ],
)
def get_risk_interview(
    run_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> RiskInterviewResponse:
    """Expose the Run's single pending activity-profile question, if any."""
    run_id = _validate_run_id(run_id)
    return _risk_interview_response(_scoped_run(deps, run_id), deps)


@router.post(
    "/{run_id}/risk-interview",
    response_model=RiskInterviewResponse,
    status_code=202,
    dependencies=[_REQUIRE_WRITE],
)
def answer_risk_interview(
    run_id: Id,
    payload: AnswerRiskInterviewRequest,
    request: Request,
    deps: RuntimeDeps = _DEPS,
) -> RiskInterviewResponse:
    """Record a human answer and continue the same Run from its intake checkpoint."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        run = deps.run_service.answer_risk_interview(
            run_id,
            answer=payload.answer,
            actor=resolve_actor(request),
        )
    except RunInformationRequiredError as exc:
        raise problem(409, "risk_interview_not_pending", str(exc)) from exc
    except ValueError as exc:
        raise problem(422, "invalid_risk_interview_answer", str(exc)) from exc
    return _risk_interview_response(run, deps)


@router.get("/{run_id}/trace", response_model=RunTraceResponse, dependencies=[_REQUIRE_READ])
def get_run_trace(
    run_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> RunTraceResponse:
    """Return the Langfuse URL for this Run's trace, or `None` when tracing is off.

    The Run is resolved first so an unknown or out-of-project id is a 404 here as it is
    everywhere else, rather than a link to a trace the caller may not read.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    return RunTraceResponse(run_id=run_id, trace_url=trace_url(run_id))


@router.post("/{run_id}/plan", response_model=RunPlan, dependencies=[_REQUIRE_WRITE])
def plan_run(
    run_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> RunPlan:
    """Return the deterministic MVP phase plan and record its approval request."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    phases = tuple(PHASE_ORDER)
    gate = deps.gate_factory(run_id)
    gate.check_action(
        subject_kind="run",
        subject_id=run_id,
        action_type="plan",
        payload={"phases": [phase.value for phase in phases]},
        summary="Approve the proposed Run phase plan.",
    )
    return RunPlan(run_id=run_id, phases=phases)


@router.get(
    "/{run_id}/plan",
    response_model=DurablePlanResponse,
    dependencies=[_REQUIRE_READ],
)
def get_durable_plan(
    run_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> DurablePlanResponse:
    """Read the latest persisted goal/plan board and verify its referenced artifact.

    A missing board is a stable 404.  Artifact lookup is read-only and content-addressed: a
    tampered or missing artifact is surfaced as a conflict instead of returning an unverified
    plan body.  The route never advances a Run or consumes a policy approval.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    board = deps.goal_board.read(run_id)
    if board is None:
        raise problem(
            404,
            "plan_not_found",
            f"No durable plan has been published for Run {run_id}.",
        )

    content: str | None = None
    if board.artifact is not None:
        artifact_store = deps.artifact_store_factory(run_id)
        artifact = artifact_store.get(board.artifact.storage_key)
        if (
            artifact is None
            or artifact.id != board.artifact.artifact_id
            or artifact.run_id != run_id
            or artifact.name != board.artifact.storage_key
            or artifact.sha256 != board.artifact.sha256
            or not artifact.valid
        ):
            raise problem(
                409,
                "plan_artifact_unavailable",
                "The persisted plan artifact reference cannot be verified.",
            )
        try:
            content = artifact_store.load_text(board.artifact.storage_key)
        except (OSError, UnicodeError, ValueError) as exc:
            raise problem(
                409,
                "plan_artifact_unavailable",
                "The persisted plan artifact cannot be read as rendered text.",
            ) from exc
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != board.artifact.sha256:
            raise problem(
                409,
                "plan_artifact_tampered",
                "The persisted plan artifact digest does not match its board reference.",
            )
    return DurablePlanResponse.from_board(board, artifact_content=content)


@router.post(
    "/{run_id}/delegate",
    response_model=Task,
    status_code=201,
    dependencies=[_REQUIRE_WRITE],
)
def delegate_task(
    run_id: Id,
    payload: DelegateRequest,
    deps: RuntimeDeps = _DEPS,
) -> Task:
    """Create a ``PENDING`` Task for a named sub-agent, linked to the scoped Run.

    This is the Agent-API ``delegate()`` method (baseline section 6). It records the delegated work
    and its target sub-agent; it does not run the sub-agent, so the Task is returned in ``PENDING``.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    agent = Agent(
        id=new_id("agent"),
        run_id=run_id,
        name=payload.agent,
        layer=Layer.EXECUTION,
    )
    deps.record_repository.save(agent)
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=agent.id,
        objective=payload.objective,
    )
    deps.record_repository.save(task)
    return task


@router.post("/{run_id}/resume", response_model=Run, status_code=202, dependencies=[_REQUIRE_WRITE])
def resume_run(
    run_id: Id,
    request: Request,
    payload: ResumeRunRequest | None = None,
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Resume one scoped Run from its latest verified graph checkpoint."""
    del payload
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        return deps.run_service.resume(run_id, actor=resolve_actor(request))
    except RunAuditStaleError as exc:
        raise problem(409, "audit_stale", str(exc)) from exc
    except RunNotAwaitingApprovalError as exc:
        raise problem(409, "approval_pending", str(exc)) from exc
    except RunInformationRequiredError as exc:
        raise problem(409, "risk_information_required", str(exc)) from exc
    except RunNotResumableError as exc:
        raise problem(409, "run_not_resumable", str(exc)) from exc
    except ValueError as exc:
        raise problem(409, "run_integrity_error", str(exc)) from exc


@router.post("/{run_id}/cancel", response_model=Run, status_code=202, dependencies=[_REQUIRE_WRITE])
def cancel_run(
    run_id: Id,
    request: Request,
    payload: RunCancelRequest | None = None,
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Abort one scoped Run, closing every approval its execution had raised.

    The caller-abort surface for F5.1: the Run leaves the state that raised its pending reviews,
    so those reviews stop being answerable and no credit granted inside it can authorise a later
    call. Repeating the call on an already terminal Run is a no-op, so a client whose response
    was lost may safely retry.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    body = payload or RunCancelRequest()
    actor = resolve_actor(request)
    _assert_declared_actor(actor, body.actor)
    try:
        return deps.run_service.cancel(run_id, actor=actor, reason=body.reason)
    except ValueError as exc:
        raise problem(409, "cancel_failed", str(exc)) from exc


def _resolve_human_decision(
    run_id: Id,
    payload: HumanDecisionRequest,
    deps: RuntimeDeps,
    actor: Actor,
    *,
    approved: bool,
) -> Run:
    """Resolve one pending Gate review through the injected HTTP answer.

    ``actor`` is the authenticated principal and nothing else, so an approval can no longer be
    recorded as ``Actor.system()`` (bug-hunt C6) or under a name nobody verified: the anonymous
    call that made either possible is refused by the route's own authentication dependency, before
    this function runs.
    """
    _assert_declared_actor(actor, payload.actor)
    gate = deps.gate_factory(
        run_id,
        approver=lambda _request: approved,
        human=actor,
    )
    try:
        return deps.run_service.resolve_approval(
            run_id,
            gate=gate,
            actor=actor,
            note=payload.note,
        )
    except RunAuditStaleError as exc:
        raise problem(409, "audit_stale", str(exc)) from exc
    except RunNotAwaitingApprovalError as exc:
        raise problem(409, "approval_not_pending", str(exc)) from exc
    except ValueError as exc:
        raise problem(409, "approval_resolution_failed", str(exc)) from exc


@router.post(
    "/{run_id}/approve", response_model=Run, status_code=202, dependencies=[_REQUIRE_APPROVE]
)
def approve_run(
    run_id: Id,
    payload: HumanDecisionRequest,
    request: Request,
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Approve the pending policy decision for one scoped Run."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    return _resolve_human_decision(
        run_id,
        payload,
        deps,
        resolve_actor(request),
        approved=True,
    )


@router.post(
    "/{run_id}/reject", response_model=Run, status_code=202, dependencies=[_REQUIRE_APPROVE]
)
def reject_run(
    run_id: Id,
    payload: HumanDecisionRequest,
    request: Request,
    deps: RuntimeDeps = _DEPS,
) -> Run:
    """Reject a pending review, resuming a tool call or blocking a findings review."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    return _resolve_human_decision(
        run_id,
        payload,
        deps,
        resolve_actor(request),
        approved=False,
    )


__all__ = ["router"]
