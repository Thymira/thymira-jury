"""Implementation of the ``thymira mlflow`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_mlflow_runs


def show_mlflow(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Option("--run", help="Canonical run identifier returned by 'thymira run'."),
    ],
) -> None:
    """Browse a run's MLflow tracker runs and their metrics through the Thymira API."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        runs = client.list_mlflow_runs(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No MLflow runs could be retrieved."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_mlflow_runs(run_id, runs))


__all__ = ["show_mlflow"]
