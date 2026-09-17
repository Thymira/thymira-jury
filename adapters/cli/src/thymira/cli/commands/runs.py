"""Implementation of the ``thymira runs`` command."""

from __future__ import annotations

import typer

from thymira.cli.client import ApiError, client_from_context
from thymira.cli.render import render_error, render_runs


def list_runs(ctx: typer.Context) -> None:
    """List runs through the Thymira API."""
    client = client_from_context(ctx)
    try:
        page = client.list_runs()
    except ApiError as error:
        typer.echo(render_error(str(error), "No runs could be retrieved."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_runs(page.items))
