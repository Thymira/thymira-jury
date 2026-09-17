"""Implementation of the ``thymira plan`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands._receipt import settle_enqueue
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_plan
from thymira.cli.shutdown import ShutdownRequestedError, bounded_signal_shutdown


def show_plan(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
) -> None:
    """Queue plan generation, then show the latest persisted plan after it settles."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        receipt = client.plan_run(run_id)
        settle_enqueue(client, receipt)
        with bounded_signal_shutdown():
            plan = client.get_durable_plan(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ShutdownRequestedError as error:
        typer.echo(
            f"Observation stopped by signal {error.signum}; the Run remains durable.",
            err=True,
        )
        raise typer.Exit(code=128 + error.signum) from error
    except KeyboardInterrupt as error:
        typer.echo("Observation interrupted; the Run remains durable.", err=True)
        raise typer.Exit(code=130) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No plan could be produced."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_plan(plan))


__all__ = ["show_plan"]
