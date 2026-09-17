"""Runtime-owned facts about how one sandbox execution ended and how its tree was reaped.

The rule this module defends: **the runtime never derives a control fact from the stream the
model reads.** A child's stdout and stderr are model-visible data a hostile snippet can write
anything into -- including a convincing ``[exit code: 0]`` line or a JSON frame that looks like
a runtime record. The exit marker, the deadline verdict, the reap outcome and the backend probe
are therefore observed elsewhere entirely: the local backend reads the OS wait status of a
process it started itself, and the container backend reads the daemon's own ``docker inspect``
record and cross-checks it against ``docker start --attach``'s return code. Neither channel is
writable by the child, and neither is parsed out of the child's output.

The evidence travels to ``tool.completed`` as its own payload key, so MIRA recomputes it from
the chain instead of re-reading text (F6.8, G.2, G.3).

:class:`ProcessObservation` is the mutable scratch pad one bounded process run fills in while it
watches its own child; :class:`TerminationEvidence` is the frozen record a backend publishes
from it once the outcome is settled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONTROL_CHANNEL_CONTAINER_STATE",
    "CONTROL_CHANNEL_NONE",
    "CONTROL_CHANNEL_OS_WAIT",
    "OUTCOME_COMPLETED",
    "OUTCOME_OUTPUT_EXCEEDED",
    "OUTCOME_TIMED_OUT",
    "OUTCOME_UNAVAILABLE",
    "ProcessObservation",
    "TerminationEvidence",
]

OUTCOME_COMPLETED = "completed"
"""The child ran to its own exit inside its deadline and output budget."""

OUTCOME_TIMED_OUT = "timed_out"
"""The deadline passed before the exit was confirmed -- recorded whatever the child's own code."""

OUTCOME_OUTPUT_EXCEEDED = "output_exceeded"
"""The combined output budget was reached and the child was terminated."""

OUTCOME_UNAVAILABLE = "unavailable"
"""No child ran under the requested confinement; the runtime refused before or during start-up."""

CONTROL_CHANNEL_OS_WAIT = "os_wait"
"""The parent's own wait status for a process it started: the local backend's control channel."""

CONTROL_CHANNEL_CONTAINER_STATE = "container_state"
"""The daemon's inspected container state, cross-checked against the attach return code."""

CONTROL_CHANNEL_NONE = "none"
"""No child was observed, so no control channel carried an exit marker."""

REAP_NOT_REQUIRED = "not_required"
"""The child exited on its own; nothing was signalled."""

REAP_QUIESCED = "quiesced"
"""A reap was needed and the tree reached quiescence inside its bound."""

REAP_UNBOUNDED = "unbounded"
"""A reap was needed and quiescence was not confirmed inside its bound."""

REAP_SKIPPED_REAPED = "skipped_reaped"
"""The recorded pid had already been reaped, so no signal was sent (PID-identity protection)."""

REAP_UNAVAILABLE = "unavailable"
"""The platform offered no tree reap; only the direct child could be signalled."""

TREE_SCOPE_NONE = "none"
TREE_SCOPE_PROCESS_GROUP = "process_group"
TREE_SCOPE_PROCESS_TREE = "process_tree"
TREE_SCOPE_DIRECT_CHILD = "direct_child"
TREE_SCOPE_CONTAINER = "container"


def _finite(value: float | None) -> float | None:
    """Round a measured duration for the payload, dropping a value JSON cannot carry.

    ``canonical_json`` is plain ``json.dumps``, which writes ``NaN``/``Infinity`` -- tokens no
    conforming JSON reader accepts. A non-finite measurement is dropped here rather than
    poisoning the hash chain; MIRA reads the absence as missing evidence and fails closed.
    """
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 3)


@dataclass(slots=True)
class ProcessObservation:
    """What one bounded process run observed about its own child, filled in as it goes.

    Mutable and runtime-owned: the caller passes an empty instance in, and
    :func:`~thymira.tools.sandbox.process_capture.run_bounded_process` fills it on every exit
    path -- including the two exceptional ones -- so a timeout or an overflow carries the same
    evidence a clean exit does.
    """

    pid: int | None = None
    duration_s: float | None = None
    deadline_s: float | None = None
    deadline_exceeded: bool = False
    timed_out: bool = False
    child_exit_code: int | None = None
    reap: str = REAP_NOT_REQUIRED
    reap_deadline_s: float | None = None
    reap_duration_s: float | None = None
    tree_scope: str = TREE_SCOPE_NONE
    signalled_after_reap: bool = False


@dataclass(frozen=True, slots=True)
class TerminationEvidence:
    """How one execution ended, as facts a reader can grade against each other.

    ``timed_out`` and ``deadline_exceeded`` are recorded independently of ``child_exit_code``:
    a child that exits zero after its deadline passed is a timeout, and the zero is kept beside
    it rather than replacing it. ``control_channel_validated`` says whether the channel that
    carried the exit marker actually confirmed it -- for the container, that the daemon's
    inspected exit code equalled the attach return code, which is two observers agreeing rather
    than one being trusted.
    """

    outcome: str
    exit_source: str
    control_channel: str
    control_channel_validated: bool
    deadline_s: float | None = None
    duration_s: float | None = None
    deadline_exceeded: bool = False
    timed_out: bool = False
    child_exit_code: int | None = None
    reap: str = REAP_NOT_REQUIRED
    reap_deadline_s: float | None = None
    reap_duration_s: float | None = None
    tree_scope: str = TREE_SCOPE_NONE
    signalled_after_reap: bool = False
    probe: dict[str, Any] | None = field(default=None)

    @classmethod
    def from_observation(
        cls,
        observation: ProcessObservation,
        *,
        outcome: str,
        exit_source: str,
        control_channel: str,
        control_channel_validated: bool,
        probe: dict[str, Any] | None = None,
    ) -> TerminationEvidence:
        """Publish a backend's settled outcome over the facts one process run observed."""
        return cls(
            outcome=outcome,
            exit_source=exit_source,
            control_channel=control_channel,
            control_channel_validated=control_channel_validated,
            deadline_s=observation.deadline_s,
            duration_s=observation.duration_s,
            deadline_exceeded=observation.deadline_exceeded,
            timed_out=observation.timed_out,
            child_exit_code=observation.child_exit_code,
            reap=observation.reap,
            reap_deadline_s=observation.reap_deadline_s,
            reap_duration_s=observation.reap_duration_s,
            tree_scope=observation.tree_scope,
            signalled_after_reap=observation.signalled_after_reap,
            probe=probe,
        )

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the ``tool.completed`` event payload."""
        return {
            "outcome": self.outcome,
            "exit_source": self.exit_source,
            "control_channel": self.control_channel,
            "control_channel_validated": self.control_channel_validated,
            "deadline_s": _finite(self.deadline_s),
            "duration_s": _finite(self.duration_s),
            "deadline_exceeded": self.deadline_exceeded,
            "timed_out": self.timed_out,
            "child_exit_code": self.child_exit_code,
            "reap": self.reap,
            "reap_deadline_s": _finite(self.reap_deadline_s),
            "reap_duration_s": _finite(self.reap_duration_s),
            "tree_scope": self.tree_scope,
            "signalled_after_reap": self.signalled_after_reap,
            "probe": dict(self.probe) if self.probe is not None else None,
        }
