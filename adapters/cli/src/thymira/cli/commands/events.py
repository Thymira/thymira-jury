"""Implementation of the ``thymira events`` command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_event, render_events_header, render_no_events
from thymira.cli.shutdown import ShutdownRequestedError, bounded_signal_shutdown


def show_events(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
    follow: Annotated[
        bool,
        typer.Option("--follow", help="Follow new events through the API event stream."),
    ] = False,
    since: Annotated[
        int,
        typer.Option("--since", min=-1, help="Only show events after this sequence number."),
    ] = -1,
) -> None:
    """Show buffered Run events or follow new events through the Thymira API."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    typer.echo(render_events_header(run_id))
    found_event = False
    try:
        with bounded_signal_shutdown():
            for event in client.stream_events(run_id, since=since, follow=follow):
                typer.echo(render_event(event))
                found_event = True
    except ShutdownRequestedError as error:
        typer.echo(
            f"Event observation stopped by signal {error.signum}; the Run remains durable.",
            err=True,
        )
        raise typer.Exit(code=128 + error.signum) from error
    except KeyboardInterrupt as error:
        typer.echo("Event observation interrupted; the Run remains durable.", err=True)
        raise typer.Exit(code=130) from error
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No events could be retrieved."), err=True)
        raise typer.Exit(code=1) from error

    if not found_event and not follow:
        typer.echo(render_no_events())
