"""Implementation of the ``thymira approve`` and ``thymira reject`` commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from thymira.cli.client import (
    ApiError,
    ApprovalNotPendingError,
    RunNotFoundError,
    client_from_context,
)
from thymira.cli.commands._receipt import settle_enqueue
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import (
    render_error,
    render_follow_up_review,
    render_human_decision,
    render_no_pending_approval,
)

if TYPE_CHECKING:
    from thymira.cli.client import ApiClient, RunView


def _render_follow_up_review_if_needed(client: ApiClient, run: RunView) -> None:
    """Point to the next pending review without making a recorded answer fail."""
    if run.status != "WAITING_FOR_APPROVAL":
        return
    try:
        next_pending = client.get_pending_approval(run.id)
    except ApiError:
        return
    if next_pending is not None:
        typer.echo("")
        typer.echo(render_follow_up_review(run.id, next_pending))


def approve_run(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help=(
                "Assert who is acting. The recorded identity is the authenticated principal; "
                "a value naming anyone else is refused."
            ),
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Optional note explaining the decision."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional reason explaining the decision."),
    ] = None,
) -> None:
    """Approve a pending human decision through the Thymira API."""
    _resolve_decision(ctx, run_id, approved=True, actor=actor, note=note, reason=reason)


def reject_run(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
    actor: Annotated[
        str | None,
        typer.Option(
            "--actor",
            help=(
                "Assert who is acting. The recorded identity is the authenticated principal; "
                "a value naming anyone else is refused."
            ),
        ),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Optional note explaining the decision."),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional reason explaining the decision."),
    ] = None,
) -> None:
    """Reject a pending human decision through the Thymira API."""
    _resolve_decision(ctx, run_id, approved=False, actor=actor, note=note, reason=reason)


def _resolve_decision(
    ctx: typer.Context,
    run_id: str,
    *,
    approved: bool,
    actor: str | None,
    note: str | None,
    reason: str | None,
) -> None:
    """Validate and submit one human answer, then point to any next review."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    if actor is not None and not actor.strip():
        typer.echo("Error: --actor cannot be empty.", err=True)
        raise typer.Exit(code=2)

    client = client_from_context(ctx)
    try:
        pending = client.get_pending_approval(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(
            render_error(str(error), "The pending decision could not be retrieved."), err=True
        )
        raise typer.Exit(code=1) from error

    if pending is None:
        typer.echo(render_no_pending_approval(run_id), err=True)
        raise typer.Exit(code=1)

    effective_note = reason if reason is not None else note
    try:
        if approved:
            receipt = client.approve_run(run_id, actor=actor, note=effective_note)
        else:
            receipt = client.reject_run(run_id, actor=actor, note=effective_note)
        run = settle_enqueue(client, receipt)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApprovalNotPendingError as error:
        typer.echo(render_error(str(error), "No decision was recorded."), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "The human decision could not be recorded."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(
        render_human_decision(run, approved=approved, tool_call=pending.tool_call is not None)
    )
    _render_follow_up_review_if_needed(client, run)


__all__ = ["approve_run", "reject_run"]
