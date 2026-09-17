"""Read-only route that surfaces one Run's MLflow tracker runs through ``query_mlflow``."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.routes._scope import _scoped_run, _validate_run_id
from thymira.api.schemas import MlflowRunListResponse, MlflowRunView
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve, load_default_policy
from thymira.schemas import Actor, Experiment, Id
from thymira.tools import ToolContext, TrackerRunsValue

router = APIRouter(prefix="/runs", tags=["mlflow"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))


@router.get("/{run_id}/mlflow", response_model=MlflowRunListResponse, dependencies=[_REQUIRE_READ])
def list_mlflow_runs(run_id: Id, deps: RuntimeDeps = _DEPS) -> MlflowRunListResponse:
    """Return the tracker runs logged for one scoped Run, read via the ``query_mlflow`` tool."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    try:
        items = _browse_tracker_runs(deps, run_id)
    except (TypeError, ValueError, KeyError) as exc:
        raise problem(409, "tracker_integrity_error", str(exc)) from exc
    return MlflowRunListResponse(run_id=run_id, items=items)


def _browse_tracker_runs(deps: RuntimeDeps, run_id: Id) -> tuple[MlflowRunView, ...]:
    """Read the Run's tracker runs through ``query_mlflow``, scoped by its experiments.

    The tracker store is project-scoped, so the browse is narrowed to the ``tracker_run_id`` of
    each experiment the Run recorded. The tool runs through the Tool Manager against a throwaway
    event log, so a read of the tracker never appends to the Run's authoritative history.

    The workspace is ``resolution.project_dir``, not ``resolution.workspace`` (the ``.thymira``
    directory): ``MlflowTracker`` roots itself at ``workspace / ".mlflow"``, so this must be the
    same directory THY's own delegated tools write through, or the browse reads an empty store
    beside the one the Run actually filled.
    """
    resolution = deps.project_resolution
    if resolution is None:
        return ()
    records = deps.record_repository.list_for_run(run_id, "experiment")
    tracker_run_ids = {
        experiment.tracker_run_id
        for experiment in (cast("Experiment", record) for record in records)
        if experiment.tracker_run_id is not None
    }
    if not tracker_run_ids:
        return ()
    scratch_log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=Actor.system().id,
        workspace=resolution.project_dir,
        event_log=scratch_log,
        gate=Gate(PolicyEngine(load_default_policy()), scratch_log, approver=auto_approve),
        artifact_store=deps.artifact_store_factory(run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    execution = deps.tool_manager.execute(context, "query_mlflow")
    value = execution.result.value
    if not isinstance(value, TrackerRunsValue):
        raise TypeError("query_mlflow returned an invalid typed value")
    return tuple(
        MlflowRunView(
            tracker_run_id=row.tracker_run_id,
            experiment_name=row.experiment_name,
            status=row.status,
            params=row.params,
            metrics=row.metrics,
        )
        for row in value.runs
        if row.tracker_run_id in tracker_run_ids
    )


__all__ = ["router"]
