"""Bounded signal handling for headless CLI observation."""

from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import FrameType


class ShutdownRequestedError(Exception):
    """Raised when a headless observer receives a signal it can report cleanly."""

    def __init__(self, signum: int) -> None:
        """Keep the numeric signal for the conventional shell exit status."""
        self.signum = signum
        super().__init__(f"shutdown requested by signal {signum}")


@contextmanager
def bounded_signal_shutdown() -> Iterator[None]:
    """Install immediate SIGINT/SIGTERM handlers and restore the caller's handlers on exit.

    The observer owns no Run state and therefore does not cancel a durable Run on disconnect.
    Raising from the signal handler interrupts the blocking HTTP read and gives the command a
    bounded path to restore handlers and exit with ``128 + signal``.
    """
    previous: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: FrameType | None) -> None:
        """Interrupt the HTTP observer instead of leaving a blocked process behind."""
        raise ShutdownRequestedError(signum)

    for signum in (int(signal.SIGINT), int(signal.SIGTERM)):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, cast("signal.Handlers | None", handler))


__all__ = ["ShutdownRequestedError", "bounded_signal_shutdown"]
