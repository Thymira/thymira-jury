"""Read-only audit and experiment routes for one scoped Run."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.routes._scope import _scoped_run, _validate_run_id
from thymira.api.schemas import ExperimentListResponse, RunAuditResponse
from thymira.events import canonical_json, verify_events
from thymira.mira import (
    ASSURANCE_BUNDLE_EXPORT,
    AssuranceBundle,
    verify_assurance_bundle,
)
from thymira.schemas import Event, EventType, Experiment, Id, PolicyDecision

router = APIRouter(prefix="/runs", tags=["audit"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))


def _run_decision(events: list[Event], run_id: Id) -> PolicyDecision | None:
    """Return the newest findings decision for the scoped Run."""
    decision: PolicyDecision | None = None
    for event in events:
        if event.type is not EventType.POLICY_DECISION:
            continue
        try:
            candidate = PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError) as exc:
            raise problem(
                409, "run_integrity_error", "A policy decision event is invalid."
            ) from exc
        if candidate.run_id == run_id and candidate.subject_kind == "findings":
            decision = candidate
    return decision


@router.get("/{run_id}/audit", response_model=RunAuditResponse, dependencies=[_REQUIRE_READ])
def get_audit(run_id: Id, deps: RuntimeDeps = _DEPS) -> RunAuditResponse:
    """Return the latest applicable report persisted by MIRA without running an audit."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        events = deps.event_store.read(run_id)
    except ValueError as exc:
        raise problem(409, "run_integrity_error", str(exc)) from exc
    verification = verify_events(events)
    if not verification.valid:
        raise problem(409, "run_integrity_error", verification.error or "The event log is invalid.")
    completed = [event for event in events if event.type is EventType.AUDIT_COMPLETED]
    if not completed:
        raise problem(404, "audit_not_found", f"No persisted audit exists for Run {run_id!r}.")
    try:
        assessment = deps.audit_freshness.assess(run_id)
    except (OSError, TypeError, ValueError) as exc:
        raise problem(
            409,
            "audit_integrity_error",
            "The persisted audit could not be assessed.",
        ) from exc
    if assessment is None:
        raise problem(404, "audit_not_found", f"No persisted audit exists for Run {run_id!r}.")
    if not assessment.fresh:
        raise problem(409, "audit_stale", assessment.control.detail)
    decision = _run_decision(events, run_id)
    return RunAuditResponse(report=assessment.report, decision=decision)


@router.get(
    "/{run_id}/assurance",
    response_model=AssuranceBundle,
    dependencies=[_REQUIRE_READ],
)
def get_assurance(run_id: Id, deps: RuntimeDeps = _DEPS) -> AssuranceBundle:
    """Return the assurance bundle the runtime wrote when the Run completed, re-verified.

    This route is strictly read-only: the bundle is produced once, by the composition, at Run
    completion (ASSUR-01). A Run with no persisted bundle is reported as such -- reading never
    generates one.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        payload = deps.run_store.read_export(run_id, ASSURANCE_BUNDLE_EXPORT)
        events = deps.event_store.read(run_id)
        artifact_store = deps.artifact_store_factory(run_id)
    except (OSError, TypeError, ValueError) as exc:
        raise problem(409, "run_integrity_error", str(exc)) from exc
    if payload is None:
        raise problem(
            404,
            "assurance_not_found",
            f"No assurance bundle was produced for Run {run_id!r}.",
        )
    try:
        bundle = AssuranceBundle.model_validate_json(canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise problem(
            409, "assurance_integrity_error", "The persisted assurance bundle is malformed."
        ) from exc
    bundle_check = verify_assurance_bundle(bundle, events=events, store=artifact_store)
    if not bundle_check.valid:
        raise problem(
            409,
            "assurance_integrity_error",
            bundle_check.error or "Bundle verification failed.",
        )
    return bundle


@router.get(
    "/{run_id}/experiments",
    response_model=ExperimentListResponse,
    dependencies=[_REQUIRE_READ],
)
def list_experiments(run_id: Id, deps: RuntimeDeps = _DEPS) -> ExperimentListResponse:
    """List the Run's persisted experiments in repository insertion order."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        records = deps.record_repository.list_for_run(run_id, "experiment")
    except (TypeError, ValueError) as exc:
        raise problem(409, "record_integrity_error", str(exc)) from exc
    experiments = tuple(cast("Experiment", record) for record in records)
    return ExperimentListResponse(run_id=run_id, items=experiments)


__all__ = ["router"]
