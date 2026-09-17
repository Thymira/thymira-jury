"""The credit-and-spend accounting behind one Tool Manager ticket, recomputed from events.

A ticket is ``tool_intent_sha256`` over a tool name and one call's validated arguments.
``ToolManager`` pools the human answers bound to that effect and spends one per execution of it,
holding every stored answer against what the Policy Engine would decide *now* -- so an approval
given under a policy or an execution surface that has since moved is no longer an answer to the
call being made.

MIRA cannot ask the engine anything, so it pairs credits with spends by what the decisions
themselves recorded: the policy snapshot they quote and the execution constraints they persisted.
Everything here is a pure function or a small holder over that recorded evidence; nothing reads a
live engine, a manager or a policy file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thymira.events import canonical_json
from thymira.mira.checks.tool_intent_evidence import (
    RecordedToolIntent,
    has_ticket_request_evidence,
)
from thymira.schemas import ActorKind, approval_decision_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.schemas import Event, ExecutionConstraints


def is_human_answer(event: Event) -> bool:
    """Whether a ``human.approval`` is a human's own answer, approving or refusing.

    The Tool Manager's ``human_answer`` counts these, so MIRA recomputes the same predicate
    rather than reading ``approved`` alone. The actor kind is the fail-closed tell:
    :meth:`Gate.request_approval` appends a serialised ``Approval`` -- which carries no
    ``automatic`` key at all -- under the automation actor whenever the approver is automatic, and
    the ``"automatic": true`` marker the Gate's other path writes is belt and braces. A declared
    but unauthenticated human identity is also evidence of nothing. An answer MIRA counted but the
    manager would refuse would authorise an execution the runtime never allows, and one the manager
    counts but MIRA refuses would raise a finding on a correct Run.
    """
    return (
        event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
        and isinstance(event.payload.get("approved"), bool)
    )


_POLICY_DIGEST = re.compile(r"^[0-9a-f]{64}$")
"""A recorded ``policy_sha256`` is a sha256 hex digest or it is not a policy snapshot at all."""


@dataclass(frozen=True, slots=True)
class PoolIdentity:
    """The record one decision was taken under: a policy snapshot and execution constraints.

    An answer is currency only against the record it was given under, and the manager holds every
    stored answer against both halves of that record. Either drifting means the human answered a
    question the engine would no longer ask -- an approval given before the Run inherited a review
    requirement is not an answer to the narrower call, and neither is one given under a policy
    that has since been replaced.

    There is no partial identity, which is why :func:`pool_identity` returns ``None`` rather than
    filling a half in. A decision whose recorded halves MIRA cannot read is not evidence of the
    surface anything was decided under, so it can neither fund an execution nor be funded by one.
    """

    policy_sha256: str
    constraints: str


def pool_identity(
    policy_sha256: str | None, constraints: ExecutionConstraints | None
) -> PoolIdentity | None:
    """The credit-and-spend identity of one recorded decision, or ``None`` when unreadable.

    Both halves have to be there and be valid: a ``policy_sha256`` that is a real digest, and
    execution constraints that validate. A missing or malformed half is not a surface of its own
    -- it is the absence of a record -- so it gets no identity, and everything downstream refuses
    to pair a credit with a spend across it.

    Matched with ``fullmatch``, not ``match``: ``$`` also matches before a final newline, so an
    anchored ``match`` would read a digest with a newline glued to it as the digest itself and let
    two different recorded values share one identity.
    """
    if policy_sha256 is None or not _POLICY_DIGEST.fullmatch(policy_sha256) or constraints is None:
        return None
    return PoolIdentity(policy_sha256, canonical_json(constraints.to_json_dict()))


@dataclass(frozen=True, slots=True)
class TicketEvent:
    """One credit or one spend on a ticket, in the order the log recorded it.

    Exactly one side is set: ``approved_decision`` is the decision a human said yes to,
    ``executed_ticket`` the ``tool_intent_sha256`` an execution claimed. They share one sequence
    because the fold that pairs them (:func:`credit_stands`) is about order -- which answer stood
    when which execution ran -- and two separate lists cannot say that.
    """

    approved_decision: str | None = None
    executed_ticket: str | None = None


def credit_stands(
    timeline: Sequence[TicketEvent],
    *,
    ticket: str,
    pool: PoolIdentity,
    credit_pools: Mapping[str, PoolIdentity | None],
) -> bool:
    """Whether an unspent credit taken under ``pool`` stood at the end of this history.

    The same sequential fold ``ToolManager`` runs over one ticket's answers and executions, with
    MIRA's reading of which answers are still currency: ``credit_pools`` holds the identity of
    every decision whose own approval request independently names the effect being judged, so a
    credit taken under this start's surface is valid, one taken under any other surface is stale,
    and a stale credit buys nothing. Answers for other effects are absent from the mapping, and a
    decision whose identity MIRA cannot read (``None``) is treated the same way: it is not
    evidence of any surface, so it is neither a valid credit nor a stale one, and the executions
    it would have paid for fall through to debt.

    Order is what separates the two cases the constraints alone cannot. A valid credit is spent
    before a stale one, so an execution recorded while a fresh approval was outstanding cannot be
    explained away as having used a weaker older one and leave the fresh credit over for a replay.
    An execution recorded when no credit stood at all is a debt the next answer pays instead of
    buying an execution of its own, so a start that names a weaker decision -- or one the log
    never recorded -- after the stronger approval consumes exactly the credit it forged itself
    onto. Genuine executions from before that approval existed spent what stood then and cost it
    nothing.

    Every execution claiming the ticket spends, whatever decision it named and whether or not A3
    allowed it. The start being judged is not in ``timeline``: the question is what stood
    immediately before it ran.
    """
    valid = stale = debt = 0
    for entry in timeline:
        if (approved := entry.approved_decision) is not None:
            credit_pool = credit_pools.get(approved)
            if credit_pool is None:
                continue
            if debt:
                debt -= 1
            elif credit_pool == pool:
                valid += 1
            else:
                stale += 1
        elif entry.executed_ticket == ticket:
            if valid:
                valid -= 1
            elif stale:
                stale -= 1
            else:
                debt += 1
    return valid > 0


@dataclass
class TicketRequests:
    """The first ticketed approval request recorded for each decision, and the call it named.

    Both ledgers bind answers to calls through this, and both bind them the same way. The tool
    name is kept beside the ticket because a ticket string on an event is a claim like any other:
    without it, a start could copy an approved ``echo`` call's ticket onto a ``run_python``
    execution and inherit the answer.
    """

    bound: set[str] = field(default_factory=set)
    """Decisions whose request carried ticket evidence, complete or not."""
    intents: dict[str, RecordedToolIntent] = field(default_factory=dict)
    """The call each of those requests named, for the requests that recorded a complete one."""

    def record(self, event: Event) -> None:
        """Bind the decision a request names to the exact call the human was asked about.

        A request that names no decision, or carries neither arguments nor a ticket -- the
        historical ``Gate.check_action`` and ``Gate.request_approval`` shape -- binds nothing and
        leaves an earlier binding intact. Once a request carries either ticket field it claims the
        first binding even when the other field is missing or malformed; a later complete request
        cannot repair or replace it.

        The *first* ticketed request wins, as it does in ``human_answer``: the Gate
        writes one per decision, so a second one naming that decision is evidence of nothing, and
        honouring it would let any writer re-point a human's pending answer at another call.
        """
        decision_id = approval_decision_id(event.payload)
        if (
            decision_id is None
            or decision_id in self.bound
            or not has_ticket_request_evidence(event)
        ):
            return
        # First ticket-shaped request wins even when malformed. Letting a later request replace
        # it, or treating it as the historical unticketed shape, would move the answer to a call
        # the human was never first shown.
        self.bound.add(decision_id)
        if (intent := RecordedToolIntent.from_event(event)) is not None:
            self.intents[decision_id] = intent

    def matches(self, decision_id: str, effect: Event) -> bool:
        """Whether one decision's first ticketed request and this effect name the same call."""
        intent = self.intents.get(decision_id)
        return intent is not None and intent.matches(effect)
