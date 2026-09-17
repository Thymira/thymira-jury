"""Implementation of the ``thymira run`` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from thymira.cli.client import (
    ApiError,
    ApiTimeoutError,
    client_from_context,
    is_terminal_event,
    terminal_exit_code,
    terminal_response,
)
from thymira.cli.render import render_error, render_run_created
from thymira.cli.shutdown import ShutdownRequestedError, bounded_signal_shutdown

if TYPE_CHECKING:
    from thymira.cli.client import ApiClient, EventView, RunView


def create_run(
    ctx: typer.Context,
    prompt: Annotated[
        str,
        typer.Argument(help="Describe the Data Science work to execute."),
    ],
    headless: Annotated[
        bool,
        typer.Option(
            "--headless",
            help="Wait for the durable turn end; print only its response to stdout.",
        ),
    ] = False,
) -> None:
    """Create a new run through the Thymira API and optionally observe it to completion."""
    client = client_from_context(ctx)
    try:
        run = client.create_run(prompt)
    except ApiTimeoutError as error:
        typer.echo(render_error(str(error), "Check the run list before retrying."), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No run was created."), err=True)
        raise typer.Exit(code=1) from error

    if not headless:
        typer.echo(render_run_created(run))
        return

    typer.echo(f"Run {run.id} submitted; waiting for its durable turn end.", err=True)
    try:
        with bounded_signal_shutdown():
            terminal, response = _observe_run(client, run)
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
        typer.echo(
            render_error(str(error), "The Run remains durable; inspect its events."),
            err=True,
        )
        raise typer.Exit(code=1) from error

    response = response or terminal_response(terminal)
    if response:
        typer.echo(response)
    raise typer.Exit(code=terminal_exit_code(terminal))


def _observe_run(client: ApiClient, run: RunView) -> tuple[EventView, str]:
    """Observe the publication's correlated turn end and report progress on stderr."""
    receipt = run.publication_receipt
    if receipt is None:
        raise ApiError("The create response did not include a publication receipt.")
    response = ""
    for event in client.stream_events(
        run.id,
        follow=True,
        require_turn_end=True,
        work_ids=receipt.work_ids,
    ):
        typer.echo(f"event {event.seq}: {event.type}", err=True)
        response = terminal_response(event) or response
        if is_terminal_event(
            event,
            run_id=run.id,
            work_ids=receipt.work_ids,
            require_turn_end=True,
        ):
            return event, response
    raise ApiError("The event stream closed before a durable turn end was recorded.")
