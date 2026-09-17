"""Shared project and Run scope validation for API route groups."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.api.errors import problem
from thymira.core import RunNotFoundError
from thymira.schemas import id_kind

if TYPE_CHECKING:
    from thymira.api.deps import RuntimeDeps
    from thymira.schemas import Actor, Id, Run


def _configured_project(deps: RuntimeDeps) -> Id:
    """Return the one project bound to this API instance."""
    if deps.project_resolution is None:
        raise problem(
            503,
            "project_not_configured",
            "The API has no configured project workspace.",
        )
    return deps.project_resolution.project_id


def _validate_run_id(run_id: Id) -> Id:
    """Reject a well-formed non-Run identifier on a Run route."""
    if id_kind(run_id) != "run":
        raise problem(422, "invalid_run_id", f"{run_id!r} is not a Run identifier.")
    return run_id


def _scoped_run(deps: RuntimeDeps, run_id: Id) -> Run:
    """Return one Run only when it belongs to the API's configured project."""
    project_id = _configured_project(deps)
    try:
        run = deps.run_service.get_run(run_id)
    except RunNotFoundError as exc:
        raise problem(404, "run_not_found", str(exc)) from exc
    if run.project_id != project_id:
        raise problem(404, "run_not_found", f"Run {run_id!r} was not found.")
    return run


def _assert_declared_actor(actor: Actor, declared: str | None) -> None:
    """Refuse a request whose body names an actor other than the authenticated principal.

    The body field is an *assertion*, never an identity: authentication already decided who is
    acting. A caller who states the wrong one is told so loudly rather than having their stated
    intent silently ignored -- an approval recorded under a different name than the one the
    operator believed they were using is exactly the record no one can review later.

    Raises:
        HTTPException: ``declared`` names someone other than the authenticated principal. The
            message echoes neither id: it would tell the caller nothing they did not already send.
    """
    if declared is not None and declared != actor.id:
        raise problem(
            403,
            "actor_mismatch",
            "The request names an actor other than the authenticated principal.",
        )
