"""Implementation of the ``thymira cancel`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands._receipt import settle_enqueue
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_run_update


def cancel_run(
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
                "Assert who is aborting the run. The recorded identity is the authenticated "
                "principal; a value naming anyone else is refused."
            ),
        ),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Why the run is being aborted."),
    ] = None,
) -> None:
    """Abort a run and close its pending approvals, through the Thymira API."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        receipt = client.cancel_run(run_id, actor=actor, reason=reason)
        run = settle_enqueue(client, receipt)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No run was cancelled."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_run_update(run, action="cancelled"))
