"""When the authority a human granted for one tool call stops existing.

A ticketed approval already has an identity -- ``tool_intent_sha256``, the exact effect a human
was asked about -- and a currency test: the policy snapshot and the execution constraints it was
taken under must still be the ones in force. Neither of those says *when the authority stops*.
This module adds that third fact, the **scope**, and the folds that close it.

A scope is four fields recorded on the ``human.approval_requested`` event itself: the Run the
credit belongs to, the exact call it authorises, the deadline it carries, and the delegation depth
it was raised at. They are digested into ``approval_scope_sha256`` so a later reader -- the Tool
Manager, MIRA, an auditor -- can recompute the binding from the request's own constituent fields
rather than trusting a recorded value. No frozen model and no enum changes: the facts ride on the
existing payloads, which is ADR-0013's path for evidence only two members read.

A scope closes for exactly three reasons, and only on evidence Core itself produced:

- ``RUN_TERMINAL`` -- ``RunController`` recorded a terminal command (``complete``, ``block``,
  ``fail``, ``cancel``). A dead Run gets no fresh review; the credit dies with it.
- ``SCOPE_RELEASED`` -- ``RunController`` recorded the end of the THY pass that raised the
  review (``begin_audit``, ``begin_reporting``, ``reopen``). The Run lives, so the next
  identical call is not refused: it asks again.
- ``EXPIRED`` -- the deadline the request recorded has passed, or the deadline it recorded
  cannot be read at all: unparseable, not a string, or naming no instant because it carries no
  UTC offset. Unknown evidence fails closed.

Only ``Actor.system()`` events whose ``producer`` is ``thymira.core`` *and whose payload agrees
with itself* close anything. A tool, an agent or a human can append an event of any type, and
``producer`` is a free argument on ``EventLog.append``, so the envelope alone proves nothing: the
serialised ``RunState``, its flattened copy, the command and the version reached are all held
against each other. Reading a bare command as a closure would make an availability attack out of a
forged payload -- it would silence every pending review and deny every ticketed call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    Actor,
    EventType,
    RunCondition,
    RunOutcome,
    RunStage,
    RunState,
    approval_decision_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event


DEFAULT_APPROVAL_TTL = timedelta(hours=24)
"""How long a ticketed approval request stays spendable unless a caller narrows it.

A day, not an hour: a human review may legitimately wait overnight, and the deadline is the
backstop *behind* the two sharper closures -- pass-level release and Run-terminal closure -- not
the primary one. A caller that needs a tighter bound sets ``ToolContext.approval_ttl``.
"""

APPROVAL_SCOPE_DIGEST_KEY = "approval_scope_sha256"
"""Payload key carrying the digest of the four scope fields."""

APPROVAL_EXPIRES_AT_KEY = "approval_expires_at"
"""Payload key carrying the ISO-8601 deadline the credit was raised with."""

DELEGATION_DEPTH_KEY = "delegation_depth"
"""Payload key carrying the delegation depth the credit was raised at."""

CORE_PRODUCER = "thymira.core"
"""The only ``producer`` whose ``run.transitioned`` events move a Run's lifecycle."""

_CLOSING_OUTCOMES: dict[str, RunOutcome] = {
    "complete": RunOutcome.COMPLETED,
    "block": RunOutcome.BLOCKED,
    "fail": RunOutcome.FAILED,
    "cancel": RunOutcome.CANCELLED,
}
"""The terminal Core commands, each with the outcome its own transition must record."""

_RELEASING_STAGES: dict[str, RunStage] = {
    "begin_audit": RunStage.AUDITING,
    "begin_reporting": RunStage.REPORTING,
    "reopen": RunStage.EXECUTING,
}
"""The pass-level Core commands, each with the stage its own transition must record."""

_FLAT_STATE_FIELDS = ("stage", "condition", "wait_reason", "outcome")
"""Every ``RunState`` field a transition payload also writes flat beside the serialised state."""

SCOPE_CLOSING_COMMANDS: frozenset[str] = frozenset(_CLOSING_OUTCOMES)
"""Core commands that end the Run itself, and with it every credit raised inside it."""

SCOPE_RELEASING_COMMANDS: frozenset[str] = frozenset(_RELEASING_STAGES)
"""Core commands that end the THY pass a tool-call review was raised in.

These are the pass-level release signals that already exist and are already trusted: each one is
a ``RunController`` fact meaning the execution pass which asked the human has ended. An unspent
credit does not outlive its pass.
"""


class ScopeClosureReason(StrEnum):
    """Why an approval scope stopped existing."""

    RUN_TERMINAL = "run_terminal"
    SCOPE_RELEASED = "scope_released"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ScopeClosure:
    """One closed scope: why it closed, and where in the log that became true."""

    reason: ScopeClosureReason
    at_seq: int

    @property
    def terminates_the_run(self) -> bool:
        """Whether the Run itself ended, so no fresh review can be raised for the call."""
        return self.reason is ScopeClosureReason.RUN_TERMINAL


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    """The four facts that bound one ticketed approval, as the Gate records them.

    Built by the Tool Manager before it asks the Gate for a decision, so the request the human is
    shown carries its own scope. ``expires_at`` is an absolute deadline rather than a duration:
    a reader that recomputes the digest must not need the TTL the caller happened to use.
    """

    run_id: str
    tool_intent_sha256: str
    expires_at: datetime
    delegation_depth: int

    def digest(self) -> str:
        """The hex sha256 of the canonical JSON of all four fields.

        Every field is inside it, so no part of a recorded scope can be swapped for another --
        a credit cannot be moved to a different Run, a different call, a later deadline or a
        different delegation depth without the digest disagreeing.
        """
        return sha256_text(
            canonical_json(
                {
                    "run_id": self.run_id,
                    "tool_intent_sha256": self.tool_intent_sha256,
                    "expires_at": self.expires_at.isoformat(),
                    "delegation_depth": self.delegation_depth,
                }
            )
        )

    def to_payload(self) -> dict[str, Any]:
        """The scope fields to record beside the call's identity on an approval request."""
        return {
            APPROVAL_EXPIRES_AT_KEY: self.expires_at.isoformat(),
            DELEGATION_DEPTH_KEY: self.delegation_depth,
            APPROVAL_SCOPE_DIGEST_KEY: self.digest(),
        }


def _instant(value: datetime) -> datetime | None:
    """The value as a comparable instant, or ``None`` when it names none.

    A datetime without a UTC offset is a wall-clock reading, not a moment: comparing one with an
    offset-aware deadline raises rather than answering, and "when does this authority end" is not
    a question the authorization boundary may answer with a ``TypeError``.
    """
    return value if value.utcoffset() is not None else None


def _deadline(value: object) -> tuple[datetime | None, bool]:
    """Read a recorded deadline: the parsed instant, and whether it was recorded but unusable.

    An absent deadline is not an error -- legacy requests and every control-plane request carry
    none, and those keep exactly today's behaviour. A deadline that is *present* and cannot be
    read is malformed evidence, and malformed evidence about when authority ends fails closed.

    Unreadable covers naming no instant, not only failing to parse. ``ToolContext.now`` is public
    and injectable, so a caller passing a naive clock mints a naive deadline and records it; the
    log is append-only, so from then on every call for that ticket would have hit the comparison.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, True
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, True
    return (parsed, False) if _instant(parsed) is not None else (None, True)


def recorded_delegation_depth(value: object) -> int | None:
    """Read a recorded delegation depth, or ``None`` when the payload recorded none.

    ``bool`` is an ``int`` in Python, so a payload claiming ``delegation_depth: true`` would
    otherwise read as depth one and let a forged record match a real caller.

    Public because both writers of the key read it back: the request records the depth a credit
    was raised at, and ``tool.started`` records the depth it was spent at. The Tool Manager holds
    a start against the caller's own depth with it, so one fold decides what a depth *is*.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True, slots=True)
class RecordedApprovalScope:
    """The scope one ``human.approval_requested`` event actually recorded.

    A reader's view, so every field is optional: a request written before this evidence existed,
    or by the control plane, records none of it and is treated exactly as it was before.
    """

    decision_id: str
    ticket: str
    seq: int
    expires_at: datetime | None = None
    delegation_depth: int | None = None
    digest: str | None = None
    unusable_deadline: bool = False
    """A deadline was recorded and cannot be read; the scope is closed rather than unbounded."""

    @classmethod
    def from_request(cls, event: Event) -> RecordedApprovalScope | None:
        """Read the scope off one approval request, or ``None`` when it names no exact call.

        A request carrying no ticket binds nothing: ``Gate.request_approval`` writes one for
        every control-plane answer, and it names no call at all.
        """
        if event.type is not EventType.HUMAN_APPROVAL_REQUESTED:
            return None
        decision_id = approval_decision_id(event.payload)
        ticket = event.payload.get("tool_intent_sha256")
        if decision_id is None or not isinstance(ticket, str) or not ticket:
            return None
        expires_at, unusable = _deadline(event.payload.get(APPROVAL_EXPIRES_AT_KEY))
        recorded_digest = event.payload.get(APPROVAL_SCOPE_DIGEST_KEY)
        return cls(
            decision_id=decision_id,
            ticket=ticket,
            seq=event.seq,
            expires_at=expires_at,
            delegation_depth=recorded_delegation_depth(event.payload.get(DELEGATION_DEPTH_KEY)),
            digest=recorded_digest if isinstance(recorded_digest, str) else None,
            unusable_deadline=unusable,
        )

    def answered_late(self, ts: datetime) -> bool:
        """Whether an answer recorded at ``ts`` arrived after this scope's own deadline."""
        return self.expires_at is not None and ts > self.expires_at

    def spendable_at_depth(self, delegation_depth: int) -> bool:
        """Whether a caller at ``delegation_depth`` may spend a credit raised under this scope.

        An exact match, so a delegate must be approved on its own: a scope granted to the root
        agent is not widened by delegation, and a scope granted to a delegate is not inherited
        back by its parent. A request that recorded no depth binds no depth, which is the
        pre-existing behaviour for every log written before this evidence existed.
        """
        return self.delegation_depth is None or self.delegation_depth == delegation_depth

    def closure(self, events: Sequence[Event], *, now: datetime) -> ScopeClosure | None:
        """Whether this scope has closed, and why, given the log and the caller's clock."""
        if self.unusable_deadline:
            return ScopeClosure(ScopeClosureReason.EXPIRED, self.seq)
        return scope_closure(events, after_seq=self.seq, expires_at=self.expires_at, now=now)


def _version(value: object) -> bool:
    """Whether a payload value is a version number rather than something that compares like one.

    ``bool`` is an ``int`` in Python, so ``False == 0`` and ``True == 1``: without this, a
    transition claiming ``from_version: false`` would satisfy an arithmetic check against a
    ``to_version`` of one, and a forged record would read as a coherent one.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _flat_state_agrees(payload: dict[str, Any], state: RunState) -> bool:
    """Whether every flattened state field repeats the serialised state it was copied from.

    All four, not only the ones the command decides: a record whose flat ``wait_reason`` says a
    human is being waited on while its own state says nobody is describes two different Runs, and
    which of them raised the credit is exactly the question. A field the state leaves unset has to
    be absent or ``null`` flat, so an added value is a disagreement like any other.
    """
    for name in _FLAT_STATE_FIELDS:
        recorded = getattr(state, name)
        if payload.get(name) != (recorded.value if recorded is not None else None):
            return False
    return True


def _command_state_agrees(command: str, payload: dict[str, Any], state: RunState) -> bool:
    """Whether the state a transition recorded is the one its own command produces."""
    if command in _RELEASING_STAGES:
        return state.stage is _RELEASING_STAGES[command] and state.condition is RunCondition.ACTIVE
    return (
        command in _CLOSING_OUTCOMES
        and state.condition is RunCondition.TERMINAL
        and state.outcome is _CLOSING_OUTCOMES[command]
        and payload.get("previous_stage") == state.stage.value
    )


def _coherent_transition_state(payload: dict[str, Any], command: str) -> bool:
    """Whether a transition's serialised state, its flattened copy and the command all agree.

    ``RunController`` writes the resulting ``RunState`` and a flattened copy of its fields side by
    side, so the two must say the same thing: a record where they differ was assembled rather than
    produced. The envelope alone does not settle it, because ``producer`` is a free argument on
    ``EventLog.append`` -- any runtime member, and anyone reconstructing ``events.jsonl``, can
    write ``thymira.core`` on an event Core never produced. A bare ``command`` and nothing else
    would otherwise silence every pending review and make the Tool Manager deny outright, and it
    would leave this fold believing an event MIRA's own control refuses to read as a lifecycle
    fact at all (``thymira.mira.checks.lifecycle_scope.trusted_lifecycle_command``). The two
    re-derive the same predicate independently, as MIRA re-derives every other producer fact; they
    do not share it.

    The state is read exactly as ``RunController`` writes it -- validated from its canonical JSON,
    because ``RunState`` is strict and the payload holds the serialised enum values -- and a state
    that does not validate at all is evidence of nothing.
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


def _core_command(event: Event) -> str | None:
    """The Core lifecycle command one ``run.transitioned`` really records, or ``None``.

    The envelope is checked before the payload -- only ``Actor.system()`` with the
    ``thymira.core`` producer, about the Run itself, records a transition -- and then the payload
    is checked against itself (:func:`_coherent_transition_state`), because the envelope is
    forgeable by anyone who can append an event. Anything else is a claim about the Run, not the
    Run's own record of itself.
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


def lifecycle_closure(events: Sequence[Event], *, after_seq: int) -> ScopeClosure | None:
    """The first Core transition after ``after_seq`` that closed a scope raised there.

    Args:
        events: The Run's events, in order.
        after_seq: The ``seq`` of the approval request whose scope is being tested.

    Returns:
        The closure the first qualifying transition caused, or ``None`` while none has.
    """
    for event in events:
        if event.seq <= after_seq:
            continue
        command = _core_command(event)
        if command is None:
            continue
        if command in SCOPE_CLOSING_COMMANDS:
            return ScopeClosure(ScopeClosureReason.RUN_TERMINAL, event.seq)
        if command in SCOPE_RELEASING_COMMANDS:
            return ScopeClosure(ScopeClosureReason.SCOPE_RELEASED, event.seq)
    return None


def scope_closure(
    events: Sequence[Event],
    *,
    after_seq: int,
    expires_at: datetime | None,
    now: datetime,
) -> ScopeClosure | None:
    """Whether a scope raised at ``after_seq`` has closed, by lifecycle or by deadline.

    The lifecycle is asked first because it is the sharper fact and it names a ``seq`` in the
    chain; the deadline is the backstop behind it. A scope with no recorded deadline can only
    ever close on the lifecycle, which is what keeps every log written before this evidence
    existed behaving exactly as it did.

    Args:
        events: The Run's events, in order.
        after_seq: The ``seq`` of the approval request whose scope is being tested.
        expires_at: The deadline the request recorded, or ``None`` when it recorded none.
        now: The caller's clock.

    Returns:
        The closure, or ``None`` while the scope is still open.
    """
    closed = lifecycle_closure(events, after_seq=after_seq)
    if closed is not None:
        return closed
    if expires_at is not None and (_instant(now) is None or now > expires_at):
        # A caller whose own clock names no instant cannot show it is inside the deadline, and an
        # unanswerable currency question closes the credit rather than raising out of the fold.
        return ScopeClosure(ScopeClosureReason.EXPIRED, after_seq)
    return None


__all__ = [
    "APPROVAL_EXPIRES_AT_KEY",
    "APPROVAL_SCOPE_DIGEST_KEY",
    "DEFAULT_APPROVAL_TTL",
    "DELEGATION_DEPTH_KEY",
    "SCOPE_CLOSING_COMMANDS",
    "SCOPE_RELEASING_COMMANDS",
    "ApprovalScope",
    "RecordedApprovalScope",
    "ScopeClosure",
    "ScopeClosureReason",
    "lifecycle_closure",
    "recorded_delegation_depth",
    "scope_closure",
]
