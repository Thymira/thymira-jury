"""Implementation of the ``thymira audit`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_audit, render_error


def show_audit(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the raw AuditReport as JSON instead of a summary."),
    ] = False,
    bundle: Annotated[
        bool,
        typer.Option("--bundle", help="Print the complete verified assurance bundle as JSON."),
    ] = False,
) -> None:
    """Run MIRA's deterministic audit checks over a run through the Thymira API."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        if bundle:
            assurance = client.get_assurance(run_id)
            typer.echo(assurance.bundle_json)
            return
        audit = client.get_audit(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No audit could be retrieved."), err=True)
        raise typer.Exit(code=1) from error

    if as_json:
        typer.echo(audit.report_json)
        return
    typer.echo(render_audit(audit))


__all__ = ["show_audit"]
