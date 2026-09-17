"""Recompute an approval's scope independently from persisted event evidence.

The Tool Manager refuses a credit whose scope has closed. That refusal is a property of the
producer, and a control that only asked the producer whether it had refused would be no control at
all. This module re-derives the same three facts from the chain alone, from records three
different writers produced: ``RunController`` writes the ``run.transitioned`` chain, the Gate
writes the approval request and its recorded scope, and the Tool Manager writes the
``tool.started`` that claims to spend it. MIRA calls none of them.

Nothing here imports :mod:`thymira.tools` or :mod:`thymira.policies`: the digest and the payload
keys are re-implemented, exactly as :mod:`thymira.mira.checks.tool_intent_evidence` re-implements
the ticket, so a scope MIRA accepts is one MIRA computed.

The checks are additive and fail closed only on evidence that is *present and wrong*. A request
that recorded no scope at all -- every log written before this evidence existed, and every
control-plane request -- keeps its previous verdict, so no honest history is retroactively
condemned. A request that recorded a scope it cannot substantiate, and a start that claims a scope
its own request disagrees with, authorise nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from thymira.events import canonical_json, sha256_text
from thymira.mira.checks.lifecycle_scope import trusted_lifecycle_command
from thymira.schemas import EventType, approval_decision_id

if TYPE_CHECKING:
    from thymira.schemas import Event


APPROVAL_SCOPE_DIGEST_KEY = "approval_scope_sha256"
"""The payload key an approval request and a start both carry the scope digest under."""

APPROVAL_EXPIRES_AT_KEY = "approval_expires_at"
"""The payload key an approval request carries its deadline under."""

DELEGATION_DEPTH_KEY = "delegation_depth"
"""The payload key a request and a start both carry the delegation depth under."""


def _text(value: Any) -> str | None:  # any payload value, narrowed to an identity
    """A non-empty string from an untrusted payload value, or ``None`` for anything else."""
    return value if isinstance(value, str) and value else None


def _depth(value: Any) -> int | None:  # any payload value, narrowed to a real depth
    """A recorded delegation depth, or ``None`` when the payload records no readable one.

    ``bool`` is an ``int`` in Python, so a payload claiming ``delegation_depth: true`` would
    otherwise read as depth one and let a forged record match an honest caller.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def claims_a_scope(event: Event) -> bool:
    """Whether an approval request carries any scope field at all, readable or not.

    The tell that separates "no scope was ever recorded" -- which changes nothing -- from
    "a scope was recorded and cannot be substantiated", which authorises nothing.
    """
    return any(
        key in event.payload
        for key in (APPROVAL_SCOPE_DIGEST_KEY, APPROVAL_EXPIRES_AT_KEY, DELEGATION_DEPTH_KEY)
    )


@dataclass(frozen=True, slots=True)
class AuditedApprovalScope:
    """One approval request's scope, recomputed from the request's own four fields."""

    ticket: str
    expires_at: datetime
    delegation_depth: int
    digest: str
    seq: int

    @classmethod
    def from_request(cls, event: Event) -> AuditedApprovalScope | None:
        """Recompute the scope one ``human.approval_requested`` recorded, or ``None``.

        ``None`` for anything MIRA cannot substantiate: a missing or unreadable field, a deadline
        that names no instant, and -- the point of the control -- a recorded
        ``approval_scope_sha256`` that disagrees with the digest of the four fields recorded
        beside it. The recorded value is compared against a recomputation, never trusted.
        """
        if event.type is not EventType.HUMAN_APPROVAL_REQUESTED:
            return None
        ticket = _text(event.payload.get("tool_intent_sha256"))
        claimed = _text(event.payload.get(APPROVAL_SCOPE_DIGEST_KEY))
        deadline = _text(event.payload.get(APPROVAL_EXPIRES_AT_KEY))
        depth = _depth(event.payload.get(DELEGATION_DEPTH_KEY))
        if ticket is None or claimed is None or deadline is None or depth is None:
            return None
        try:
            expires_at = datetime.fromisoformat(deadline)
        except ValueError:
            return None
        if expires_at.utcoffset() is None:
            # A deadline with no UTC offset names a wall-clock reading, not a moment, so it
            # cannot be held against the start's timestamp at all. Unsubstantiated, like every
            # other scope field MIRA cannot read.
            return None
        recomputed = _scope_digest(event.run_id, ticket, deadline, depth)
        if recomputed != claimed:
            return None
        return cls(
            ticket=ticket,
            expires_at=expires_at,
            delegation_depth=depth,
            digest=claimed,
            seq=event.seq,
        )


def _scope_digest(run_id: str, ticket: str, expires_at: str, delegation_depth: int) -> str:
    """Fold the producer's scope digest from persisted evidence, independently."""
    return sha256_text(
        canonical_json(
            {
                "run_id": run_id,
                "tool_intent_sha256": ticket,
                "expires_at": expires_at,
                "delegation_depth": delegation_depth,
            }
        )
    )


@dataclass
class ApprovalScopeLedger:
    """Whether the credit a start claims to spend was still in scope when the start ran.

    Folded in log order alongside the ledger that owns it. ``closures`` are the sequence numbers
    at which Core's own chain says a scope ended: every command
    :func:`~thymira.mira.checks.lifecycle_scope.trusted_lifecycle_command` recognises either ends
    the Run or ends the THY pass a review was raised in, and both close a credit raised before
    them. That function verifies the whole envelope -- the system actor, the ``thymira.core``
    producer, the Run as subject and a payload that agrees with itself -- so a transition anyone
    else wrote closes nothing here either.
    """

    scopes: dict[str, AuditedApprovalScope] = field(default_factory=dict)
    unverifiable: set[str] = field(default_factory=set)
    """Decisions whose request recorded a scope MIRA could not recompute."""
    bound: set[str] = field(default_factory=set)
    """Decisions a request has already been read for; the first request wins, as in the manager."""
    closures: list[int] = field(default_factory=list)
    """The sequence numbers at which Core recorded a lifecycle fact that closes a scope."""

    def record(self, event: Event) -> None:
        """Fold one event that is not a ``tool.started`` into the scope evidence."""
        if event.type is EventType.RUN_TRANSITIONED:
            if trusted_lifecycle_command(event) is not None:
                self.closures.append(event.seq)
        elif event.type is EventType.HUMAN_APPROVAL_REQUESTED:
            self._record_request(event)

    def _record_request(self, event: Event) -> None:
        """Read the scope off the first request recorded for each decision.

        The first request wins, exactly as it does in the manager's own fold: a second request
        naming a decision is evidence of nothing, and reading it as a rebinding would let any
        writer widen a scope a human was already shown.
        """
        decision_id = approval_decision_id(event.payload)
        if decision_id is None or decision_id in self.bound:
            return
        if not claims_a_scope(event):
            return
        self.bound.add(decision_id)
        scope = AuditedApprovalScope.from_request(event)
        if scope is None:
            self.unverifiable.add(decision_id)
        else:
            self.scopes[decision_id] = scope

    def credit_open(self, decision_id: str | None, start: Event) -> bool:
        """Whether the scope of ``decision_id`` was still open when ``start`` ran.

        A decision whose request recorded no scope is not judged here at all: it keeps the verdict
        the rest of A3 gives it. Everything else is checked against the request's own recomputed
        fields -- the start has to claim that exact digest, run at that exact delegation depth, and
        have run before the deadline and before the first Core transition that closed the scope.
        """
        if decision_id is None:
            return True
        if decision_id in self.unverifiable:
            return False
        scope = self.scopes.get(decision_id)
        if scope is None:
            return True
        return (
            not any(scope.seq < closed < start.seq for closed in self.closures)
            and _text(start.payload.get(APPROVAL_SCOPE_DIGEST_KEY)) == scope.digest
            and _depth(start.payload.get(DELEGATION_DEPTH_KEY)) == scope.delegation_depth
            and start.ts <= scope.expires_at
        )


__all__ = [
    "APPROVAL_EXPIRES_AT_KEY",
    "APPROVAL_SCOPE_DIGEST_KEY",
    "DELEGATION_DEPTH_KEY",
    "ApprovalScopeLedger",
    "AuditedApprovalScope",
    "claims_a_scope",
]
