"""Implementation of the read-only ``thymira review`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_human_review, render_no_pending_approval


def review_run(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
) -> None:
    """Show the pending human decision without changing the Run."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        run = client.get_run(run_id)
        pending = client.get_pending_approval(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(
            render_error(str(error), "The review information could not be retrieved."), err=True
        )
        raise typer.Exit(code=1) from error

    if pending is None:
        typer.echo(render_no_pending_approval(run_id), err=True)
        raise typer.Exit(code=1)
    typer.echo(render_human_review(run, pending))


__all__ = ["review_run"]
