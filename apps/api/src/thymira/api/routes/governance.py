"""Governance routes: pending approvals and the service-backed approve/reject seam.

The run-lifecycle approve/reject in :mod:`thymira.api.routes.runs` transition a parked Run through
the deterministic :class:`~thymira.core.RunController`. These governance routes are the HITL-01
seam instead: they read the pending human-review requests folded from the log and record a human's
answer as evidence through an :class:`~thymira.policies.ApprovalService`. They never transition the
Run. The human's answer is evidence, never authorization -- authorization stays the deterministic
Policy Engine, and the lifecycle transition is a separate code-authorized step (ADR-0005 idea 7).

They live at a decision-scoped path (``/runs/{id}/approvals/{decision_id}/approve``) because the
ApprovalService resolves by decision id and the run-lifecycle ``/runs/{id}/approve`` route is owned,
unchanged, by :mod:`thymira.api.routes.runs`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission, resolve_actor
from thymira.api.routes._scope import (
    _assert_declared_actor,
    _scoped_run,
    _validate_run_id,
)
from thymira.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalResolution,
)
from thymira.policies import LocalApprovalService, UnknownApprovalError
from thymira.schemas import Id  # noqa: TC001 - FastAPI resolves route annotations at runtime.

router = APIRouter(prefix="/runs", tags=["governance"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_REQUIRE_APPROVE = Depends(require_permission(Permission.APPROVE))


def _approval_service(deps: RuntimeDeps, run_id: Id) -> LocalApprovalService:
    """Bind an ApprovalService to the scoped Run's authoritative event log."""
    return LocalApprovalService(deps.event_store.open(run_id))


@router.get(
    "/{run_id}/approvals", response_model=ApprovalListResponse, dependencies=[_REQUIRE_READ]
)
def list_approvals(run_id: Id, deps: RuntimeDeps = _DEPS) -> ApprovalListResponse:
    """List the Run's unresolved human-review requests, folded by the ApprovalService."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    pending = _approval_service(deps, run_id).pending(run_id)
    return ApprovalListResponse(run_id=run_id, items=pending)


def _resolve_decision(
    run_id: Id,
    decision_id: Id,
    request: Request,
    payload: ApprovalDecisionRequest | None,
    deps: RuntimeDeps,
    *,
    approved: bool,
) -> ApprovalResolution:
    """Record one human answer through the ApprovalService and return the resolved decision."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    body = payload or ApprovalDecisionRequest()
    actor = resolve_actor(request)
    _assert_declared_actor(actor, body.actor)
    try:
        decision = _approval_service(deps, run_id).resolve(
            run_id,
            decision_id,
            approved=approved,
            by=actor,
            rationale=body.reason,
        )
    except UnknownApprovalError as exc:
        raise problem(409, "approval_not_pending", str(exc)) from exc
    return ApprovalResolution(
        run_id=run_id,
        decision_id=decision_id,
        approved=approved,
        decision=decision,
    )


@router.post(
    "/{run_id}/approvals/{decision_id}/approve",
    response_model=ApprovalResolution,
    dependencies=[_REQUIRE_APPROVE],
)
def approve_decision(
    run_id: Id,
    decision_id: Id,
    request: Request,
    payload: ApprovalDecisionRequest | None = None,
    deps: RuntimeDeps = _DEPS,
) -> ApprovalResolution:
    """Record a human approval for one pending decision through the ApprovalService."""
    return _resolve_decision(run_id, decision_id, request, payload, deps, approved=True)


@router.post(
    "/{run_id}/approvals/{decision_id}/reject",
    response_model=ApprovalResolution,
    dependencies=[_REQUIRE_APPROVE],
)
def reject_decision(
    run_id: Id,
    decision_id: Id,
    request: Request,
    payload: ApprovalDecisionRequest | None = None,
    deps: RuntimeDeps = _DEPS,
) -> ApprovalResolution:
    """Record a human rejection for one pending decision through the ApprovalService."""
    return _resolve_decision(run_id, decision_id, request, payload, deps, approved=False)


__all__ = ["router"]
