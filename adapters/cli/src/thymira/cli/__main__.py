"""Entry point for the ``thymira`` command-line interface."""

from __future__ import annotations

import sys
from importlib.metadata import version as package_version
from typing import Annotated

import typer

from thymira.cli.commands import (
    answer_risk_interview,
    approve_run,
    cancel_run,
    create_run,
    list_runs,
    reject_run,
    resume_run,
    review_run,
    show_audit,
    show_events,
    show_experiments,
    show_mlflow,
    show_plan,
    show_status,
)
from thymira.cli.env import load_env_file
from thymira.cli.render import BANNER, render_welcome

__all__ = ["BANNER", "app", "main"]
app = typer.Typer(
    add_completion=False,
    add_help_option=False,
    help="Execute and govern reproducible Data Science workflows.",
    invoke_without_command=True,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _print_help(ctx: typer.Context) -> None:
    """Print the branded root help screen."""
    typer.echo(render_welcome(ctx.get_help()))


def _help_callback(ctx: typer.Context, value: bool) -> None:
    """Print root help when the eager ``--help`` option is present."""
    if not value or ctx.resilient_parsing:
        return
    _print_help(ctx)
    raise typer.Exit


def _version_callback(value: bool) -> None:
    """Print the installed CLI version when ``--version`` is present."""
    if not value:
        return
    typer.echo(f"thymira {package_version('thymira-cli')}")
    raise typer.Exit


@app.callback()
def root(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            help="Show the installed version and exit.",
            is_eager=True,
        ),
    ] = False,
    _help: Annotated[
        bool,
        typer.Option(
            "--help",
            callback=_help_callback,
            help="Show this message and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Show the welcome screen when no subcommand is selected."""
    if ctx.invoked_subcommand is None:
        _print_help(ctx)


app.command("run", help="Create a new run.")(create_run)
app.command("answer", help="Answer a pending activity-profile question.")(answer_risk_interview)
app.command("runs", help="List runs.")(list_runs)
app.command("resume", help="Resume a run from its latest checkpoint.")(resume_run)
app.command("review", help="Read what a pending human decision would authorize.")(review_run)
app.command("events", help="Show a run's event history or follow new events.")(show_events)
app.command("approve", help="Approve a pending human decision.")(approve_run)
app.command("reject", help="Reject a pending human decision.")(reject_run)
app.command("cancel", help="Abort a run and close its pending approvals.")(cancel_run)
app.command("status", help="Show the current state of a run.")(show_status)
app.command("audit", help="Run MIRA's deterministic audit and show the result.")(show_audit)
app.command("experiment", help="List a run's experiments.")(show_experiments)
app.command("mlflow", help="Browse a run's MLflow tracker runs and their metrics.")(show_mlflow)
app.command("plan", help="Queue and show a run's durable plan without executing it.")(show_plan)


def main() -> None:
    """Run the Thymira CLI.

    The env file is read here, at the process entry point, so importing any `thymira.cli` module
    never touches the environment — a test that imports a command must not inherit whatever `.env`
    happens to sit above its working directory.
    """
    _force_utf8_console()
    load_env_file()
    app(prog_name="thymira")


def _force_utf8_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 so rendered Run content can never crash the CLI.

    Found live (2026-09-11): a MIRA/model-authored question or tool-call description containing
    ordinary non-Latin-1 text (curly quotes, accented names) made `typer.echo` raise
    `UnicodeEncodeError` on Windows, where a piped or plain-console stdout defaults to the system
    codepage (commonly cp1252) rather than UTF-8 -- the same class of bug `scripts/lint_imports.py`
    already documents for import-linter's own Rich output. `TextIOWrapper.reconfigure` is a no-op
    when the stream is already UTF-8 (every non-Windows default) and a plain stream (not a
    `TextIOWrapper`, e.g. under some test harnesses) has no such method, so this stays silent
    rather than failing the command over a display nicety.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()
