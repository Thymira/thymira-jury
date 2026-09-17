"""Shared receipt settlement for lifecycle mutation commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from thymira.cli.client import RunView
from thymira.cli.render import render_enqueue_receipt
from thymira.cli.shutdown import ShutdownRequestedError, bounded_signal_shutdown

if TYPE_CHECKING:
    from thymira.cli.client import ApiClient, MutationAck


def settle_enqueue(client: ApiClient, ack: MutationAck) -> RunView:
    """Settle one lifecycle mutation and return the Run snapshot it produced.

    An owner-bound API answers with a durable enqueue receipt, and the Run is only known once the
    receipt's correlated turn has ended. An API that applies the mutation inside the request
    answers with the resulting Run record, which is already the settled snapshot.
    """
    if isinstance(ack, RunView):
        return ack
    typer.echo(render_enqueue_receipt(ack), err=True)
    try:
        with bounded_signal_shutdown():
            client.wait_for_enqueue(ack)
            return client.get_run(ack.run_id)
    except ShutdownRequestedError as error:
        typer.echo(
            f"Observation stopped by signal {error.signum}; the Run remains durable.",
            err=True,
        )
        raise typer.Exit(code=128 + error.signum) from error
    except KeyboardInterrupt as error:
        typer.echo("Observation interrupted; the Run remains durable.", err=True)
        raise typer.Exit(code=130) from error
    raise RuntimeError("receipt settlement did not return a Run")  # pragma: no cover


__all__ = ["settle_enqueue"]
