"""Implementation of the ``thymira status`` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import (
    render_error,
    render_review_hint,
    render_risk_interview,
    render_run_status,
    render_thy_activity,
)
from thymira.cli.thy_view import fold_thy_activity

if TYPE_CHECKING:
    from thymira.cli.client import ApiClient, RunView


def _render_activity(client: ApiClient, run_id: str) -> None:
    """Fold the Run's events into the THY activity view, echoed only when there is activity.

    Best-effort and never fatal: the run status is the command's contract, so an events fetch that
    fails (or a run with no THY activity yet) leaves the status output untouched rather than
    turning a successful ``status`` into an error.
    """
    try:
        events = tuple(client.stream_events(run_id))
    except ApiError:
        return
    view = fold_thy_activity(events)
    if view.has_activity:
        typer.echo("")
        typer.echo(render_thy_activity(view))


def _render_risk_interview(client: ApiClient, run_id: str) -> None:
    """Show the pending activity-profile question without making status fail on an older API."""
    try:
        interview = client.get_risk_interview(run_id)
    except ApiError:
        return
    if interview.pending_question is not None or interview.requires_human_review:
        typer.echo("")
        typer.echo(render_risk_interview(interview))


def _render_review_hint(client: ApiClient, run: RunView) -> None:
    """Point to the read-only review command when the Run is awaiting approval."""
    if run.status != "WAITING_FOR_APPROVAL":
        return
    try:
        pending = client.get_pending_approval(run.id)
    except ApiError:
        return
    if pending is not None:
        typer.echo("")
        typer.echo(render_review_hint(run.id))


def _render_trace(client: ApiClient, run_id: str) -> None:
    """Echo where the Run's trace can be read, when the runtime is tracing at all.

    Best-effort for the same reason as the activity view: the status is the command's contract,
    and a deployment with no Langfuse configured must not turn a working `status` into an error.
    """
    try:
        url = client.get_trace_url(run_id)
    except ApiError:
        return
    if url is not None:
        typer.echo("")
        typer.echo(f"Trace: {url}")


def show_status(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
) -> None:
    """Show the current state of a run through the Thymira API."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        run = client.get_run(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No status could be retrieved."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_run_status(run))
    _render_review_hint(client, run)
    _render_risk_interview(client, run_id)
    _render_activity(client, run_id)
    _render_trace(client, run_id)
