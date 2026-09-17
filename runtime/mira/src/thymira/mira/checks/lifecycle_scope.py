"""Follow the Run across the boundary where THY hands the Tool Manager to MIRA, and back.

A Run whose recorded execution decision requires a human before every tool call carries that
requirement for as long as THY holds the tools. MIRA's own audit agents then call the same Tool
Manager under a context that inherits no constraints at all, and their reads are authorised on
their own decisions -- so the requirement has to stop applying for exactly the span Core says MIRA
holds the Run, and start applying again when reporting, rework or termination ends that scope.

Both edges are read off ``run.transitioned`` events written by ``RunController``, and nothing else
moves them. MIRA never imports :mod:`thymira.core` to do this: the two orchestrators are
independent, so the boundary is recomputed from the chain, envelope and all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json
from thymira.schemas import Actor, EventType, RunCondition, RunOutcome, RunStage, RunState

if TYPE_CHECKING:
    from thymira.schemas import Event


CORE_PRODUCER = "thymira.core"
"""The only ``producer`` whose ``run.transitioned`` events move a Run's lifecycle."""

BEGIN_AUDIT = "begin_audit"
"""The Core command that hands the Tool Manager to MIRA."""

REOPEN = "reopen"
"""The Core command that takes it back for a rework THY has to run."""

_LIFECYCLE_STAGES: dict[str, RunStage] = {
    BEGIN_AUDIT: RunStage.AUDITING,
    REOPEN: RunStage.EXECUTING,
    "begin_reporting": RunStage.REPORTING,
}
"""The active Core stage changes that enter or leave the audit boundary."""

_TERMINAL_OUTCOMES = {
    "complete": RunOutcome.COMPLETED,
    "block": RunOutcome.BLOCKED,
    "fail": RunOutcome.FAILED,
    "cancel": RunOutcome.CANCELLED,
}
"""Direct terminal exits from auditing retain the stage and record their exact outcome."""


_FLAT_STATE_FIELDS = ("stage", "condition", "wait_reason", "outcome")
"""Every ``RunState`` field ``RunTransitionEvent.payload`` also writes flat beside the state."""


def _version(value: Any) -> bool:  # any payload value, tested for a real version number
    """Whether a payload value is a version number rather than something that compares like one.

    ``bool`` is an ``int`` in Python, so ``False == 0`` and ``True == 1``: without this, a
    transition claiming ``from_version: false`` would satisfy an arithmetic check against a
    ``to_version`` of one, and a forged record would read as a coherent one.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _flat_state_agrees(payload: dict[str, Any], state: RunState) -> bool:
    """Whether every flattened state field repeats the serialised state it was copied from.

    All four, not only the two that decide the stage: a record whose flat ``wait_reason`` says a
    human is being waited on while its own state says nobody is describes two different Runs, and
    which of them MIRA is auditing is exactly the question. A field the state leaves unset has to
    be absent or ``null`` flat, so an added value is a disagreement like any other.
    """
    for name in _FLAT_STATE_FIELDS:
        recorded = getattr(state, name)
        if payload.get(name) != (recorded.value if recorded is not None else None):
            return False
    return True


def _command_state_agrees(command: str, payload: dict[str, Any], state: RunState) -> bool:
    """Match an audit boundary's resulting stage or terminal outcome, without replaying Core."""
    if command in _LIFECYCLE_STAGES:
        return state.stage is _LIFECYCLE_STAGES[command] and state.condition is RunCondition.ACTIVE
    return (
        command in _TERMINAL_OUTCOMES
        and state.condition is RunCondition.TERMINAL
        and state.outcome is _TERMINAL_OUTCOMES[command]
        and payload.get("previous_stage") == state.stage.value
    )


def _coherent_transition_state(payload: dict[str, Any], command: str) -> bool:
    """Whether a transition's serialised state, its flattened copy and the command all agree.

    ``RunController`` writes the resulting ``RunState`` and a flattened copy of its fields side by
    side (``RunTransitionEvent.payload``), so the two must say the same thing: a record where they
    differ was assembled rather than produced, and MIRA hands the tools over on this evidence
    alone. Every field that bears on who holds them is checked -- the stage the command implies,
    the active condition or exact terminal outcome, every flattened copy
    (:func:`_flat_state_agrees`) and the version reached, whose predecessor is the version it
    came from.

    The state is read exactly as ``RunController._replay_transition`` writes it -- validated from
    its canonical JSON, because ``RunState`` is strict and the payload holds the serialised enum
    values -- and a state that does not validate at all is evidence of nothing.
    """
    try:
        state = RunState.model_validate_json(canonical_json(payload["state"]))
    except (KeyError, TypeError, ValueError):
        return False
    to_version, from_version = payload.get("to_version"), payload.get("from_version")
    if not _version(to_version) or not _version(from_version):
        return False
    return (
        _command_state_agrees(command, payload, state)
        and _flat_state_agrees(payload, state)
        and to_version == state.version
        and from_version == to_version - 1
    )


def trusted_lifecycle_command(event: Event) -> str | None:
    """The Core lifecycle command one ``run.transitioned`` really records, or ``None``.

    Only ``RunController`` may enter or end the audit scope, so every part of the envelope is
    verified before the event is
    believed. Rejected, each for its own reason:

    - not a ``run.transitioned`` at all, or a command other than entering audit, reopening,
      reporting or terminating: waits and resumes preserve scope, and an ``audit.started``
      MIRA's own preflight writes says nothing about who holds the tools;
    - an actor that is not ``Actor.system()``, or a ``producer`` other than ``thymira.core``: a
      tool, an agent or a human can append an event of any type, and a transition they wrote is a
      claim about the Run, not the Run's own record of itself;
    - a ``subject_id`` that is not the Run: a transition is about the Run itself, never a task,
      an agent or a call;
    - a payload that disagrees with itself or with the command
      (:func:`_coherent_transition_state`): a "begin_audit" leaving the Run executing, a flattened
      field the serialised state contradicts, or a version the record cannot have reached.
    """
    if event.type is not EventType.RUN_TRANSITIONED:
        return None
    if event.actor != Actor.system() or event.producer != CORE_PRODUCER:
        return None
    if event.subject_id != event.run_id:
        return None
    command = event.payload.get("command")
    if not isinstance(command, str):
        return None
    return command if _coherent_transition_state(event.payload, command) else None


@dataclass
class ExecutionScope:
    """Whether the Run's review requirement binds the execution being audited, folded in order.

    ``review_required`` is armed by the caller from the Run's own recorded decision and is never
    lowered again: a later run-level decision carrying empty constraints -- a budget verdict, say
    -- is about something else entirely, and reading it as a release would let any subsequent
    decision clear a requirement a human is still answering. What moves is *applicability*: the
    requirement describes THY's execution surface, so it is suspended while Core says MIRA holds
    the Run and re-armed when reporting, rework or termination ends that scope.

    With no transition evidence at all the requirement simply applies, which is the fail-closed
    reading for a bare Run, and THY's first pass is exactly that: Core records ``begin_execution``
    only after the pass returns, so a Run still in ``planning`` is THY's.
    """

    review_required: bool = False
    """Whether a run-level decision recorded constraints demanding a human before any tool call."""
    audited: bool = False
    """Whether Core has handed the Run to MIRA and not yet taken it back."""

    def arm(self) -> None:
        """Record that the Run's own decision requires a human before every tool call."""
        self.review_required = True

    def record(self, event: Event) -> None:
        """Move the boundary if -- and only if -- Core's own record says it moved."""
        command = trusted_lifecycle_command(event)
        if command is not None:
            self.audited = command == BEGIN_AUDIT

    def applies(self) -> bool:
        """Whether the Run's review requirement binds the execution being audited."""
        return self.review_required and not self.audited
