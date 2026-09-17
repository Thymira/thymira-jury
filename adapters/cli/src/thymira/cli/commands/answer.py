"""Implementation of the ``thymira answer`` risk-interview command."""

from __future__ import annotations

from typing import Annotated

import typer

from thymira.cli.client import ApiError, RunNotFoundError, client_from_context
from thymira.cli.commands._receipt import settle_enqueue
from thymira.cli.commands.run_ids import validate_run_id
from thymira.cli.render import render_error, render_risk_interview, render_run_update


def answer_risk_interview(
    ctx: typer.Context,
    run_id: Annotated[
        str,
        typer.Argument(help="Canonical run identifier returned by 'thymira run'."),
    ],
    answer: Annotated[
        str,
        typer.Argument(help="Answer to the currently pending risk-interview question."),
    ],
) -> None:
    """Record one answer and continue the same Run from its intake checkpoint."""
    try:
        validate_run_id(run_id)
    except ValueError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error

    client = client_from_context(ctx)
    try:
        receipt = client.answer_risk_interview(run_id, answer)
        run = settle_enqueue(client, receipt)
        interview = client.get_risk_interview(run_id)
    except RunNotFoundError as error:
        typer.echo(render_error(str(error)), err=True)
        raise typer.Exit(code=1) from error
    except ApiError as error:
        typer.echo(render_error(str(error), "No answer was recorded."), err=True)
        raise typer.Exit(code=1) from error

    typer.echo(render_run_update(run, action="updated"))
    if interview.pending_question is not None or interview.requires_human_review:
        typer.echo("")
        typer.echo(render_risk_interview(interview))
