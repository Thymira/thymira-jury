"""Recompute the Tool Manager's one-shot ticket from the event log alone.

A3 and A6 reconstruct authorization independently. Only pure refusal facts and wording are
shared through ``thymira.schemas``; neither ledger calls the manager or its ticket fold. The ticket
is ``tool_intent_sha256`` over the tool name and the call's validated arguments. The
identical call asked twice before anyone answers leaves two pending decisions on one ticket.
The manager pools their answers, spends them against every execution of that ticket whatever
decision the execution names, and treats one human rejection anywhere among them as final.

These two ledgers fold the log the same way. Approvals and executions are counted per ticket
wherever the writer recorded one; a decision whose approval request carried no ticket -- what
``Gate.check_action`` writes -- keeps the older per-decision accounting, still one execution per
human answer. Both ledgers are folded once, in sequence order, inside their control's single
pass over the log.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from thymira.mira.checks.approval_scope_evidence import ApprovalScopeLedger
from thymira.mira.checks.lifecycle_scope import ExecutionScope
from thymira.mira.checks.ticket_pool import (
    PoolIdentity,
    TicketEvent,
    TicketRequests,
    credit_stands,
    is_human_answer,
    pool_identity,
)
from thymira.mira.checks.tool_intent_evidence import has_ticket_claim, verified_effect_ticket
from thymira.schemas import (
    AGENT_ALLOWLIST_REFUSAL,
    ActorKind,
    Decision,
    EventType,
    ExecutionConstraints,
    agent_allowlist_refusal,
    approval_decision_id,
    constraint_refusals,
)

if TYPE_CHECKING:
    from thymira.schemas import Event


TOOL_CALL_SUBJECT_KIND = "tool_call"
"""The ``PolicyDecision.subject_kind`` a decision about one tool call carries."""

RUN_SUBJECT_KIND = "run"
"""The ``PolicyDecision.subject_kind`` every decision about the Run itself carries.

Two of them can refuse one tool call, and A6 accepts a denial naming such a decision only in
those two shapes. A **non-allowing** run-level decision -- the budget ``BLOCK`` or review the
Core budget guard obtains before a call, or a run-level ``BLOCK`` -- refuses every call under
it. An **allowing** one (the ``execution.start`` ``PASS`` that carries the Run's
``ExecutionConstraints``) refuses only the calls those recorded constraints deny, so the
denial's ``reason`` has to be one this decision's own constraints produce
(:func:`~thymira.schemas.constraint_refusals`). Anything else -- a fabricated denial naming the
run's ``PASS``, or a budget ``PASS`` -- explains nothing.
"""


def _recorded_constraints(decision: Event) -> ExecutionConstraints | None:
    """The ``ExecutionConstraints`` a recorded ``policy.decision`` carries, if it carries any."""
    recorded = decision.payload.get("execution_constraints")
    if not isinstance(recorded, dict):
        return None
    try:
        return ExecutionConstraints.model_validate(recorded)
    except ValidationError:
        return None


def _pool_identity(decision: Event) -> PoolIdentity | None:
    """Read one recorded decision's own pool identity off its payload."""
    return pool_identity(
        _text(decision.payload.get("policy_sha256")), _recorded_constraints(decision)
    )


def _carries_run_wide_review(decision: Event) -> bool:
    """Whether one recorded decision was itself taken under a review-before-every-call surface."""
    constraints = _recorded_constraints(decision)
    return constraints is not None and constraints.requires_human_review


def _demands_run_wide_review(event: Event) -> bool:
    """Whether one recorded decision puts a human in front of every tool call in the Run.

    Only a decision about the *Run* can say that. A tool-call decision carrying the same recorded
    constraint is the consequence -- the Run's requirement inherited onto that one call -- and
    reading it as a second arming would let a call's own record decide the surface it is judged
    against. Constraints MIRA cannot parse arm nothing: the flag is a fact read off the record,
    and there is no record here to read.
    """
    return event.payload.get("subject_kind") == RUN_SUBJECT_KIND and _carries_run_wide_review(event)


def _legacy_allowlist_actor(event: Event, tool: str) -> str | None:
    """Read the quoted agent id in a pre-metadata allowlist denial, then verify it in full.

    A ``tool.denied`` written before the manager also recorded ``agent_id``/``tool`` in the
    payload carries only the rendered sentence (:func:`~thymira.schemas.agent_allowlist_refusal`,
    which quotes both sides with ``repr``). Reading it back assumes the agent id's ``repr`` opens
    and closes with a plain single quote. That is not a fail-closed guess: agent ids are
    ``Id``-typed (``^[a-z]+_[0-9a-f]{32}$``, :mod:`thymira.schemas.ids`), so an agent id can never
    contain a quote or a backslash and Python's ``repr`` of one is always single-quoted -- the
    parse below can never miss a legitimate legacy denial over this.
    """
    reason = event.payload.get("reason")
    prefix = "agent '"
    suffix = f"'{AGENT_ALLOWLIST_REFUSAL}{tool!r}"
    if not isinstance(reason, str) or not reason.startswith(prefix) or not reason.endswith(suffix):
        return None
    return _text(reason[len(prefix) : -len(suffix)])


def _text(value: Any) -> str | None:  # any payload value, narrowed to an identity
    """A non-empty string from an untrusted payload value, or ``None`` for anything else.

    Identity is only ever a non-empty string here, so a payload carrying ``decision_id: None`` --
    what a constraint denial before any decision writes -- can never match a recorded id.
    """
    return value if isinstance(value, str) and value else None


@dataclass
class ToolAuthorizationLedger:
    """The evidence A3 folds to decide whether one ``tool.started`` was authorised.

    ``requests`` binds a review decision to the exact call its approval request named.
    ``ticket_timeline`` is that call's credit-and-spend history in log order, folded by
    :func:`~thymira.mira.checks.ticket_pool.credit_stands`; an approval joins it only when its own
    request independently recomputes to the same effect. The ``decision_*`` counters serve the
    logs that carry no ticket at all, where one human answer still buys exactly one execution.
    ``scope`` is the Run's own execution surface, folded from the chain.
    """

    decisions: dict[str, Event] = field(default_factory=dict)
    duplicated_decision_ids: set[str] = field(default_factory=set)
    latest: dict[str | None, Event] = field(default_factory=dict)
    requests: TicketRequests = field(default_factory=TicketRequests)
    decision_approvals: Counter[str] = field(default_factory=Counter)
    rejected_decisions: set[str] = field(default_factory=set)
    decision_pools: dict[str, PoolIdentity | None] = field(default_factory=dict)
    """The record each decision was taken under, by decision id; ``None`` when unreadable."""
    ticket_timeline: list[TicketEvent] = field(default_factory=list)
    """Every human approval and every claimed execution, in the order the log recorded them.

    Not a pair of counters: one effect can be reviewed twice under different surfaces -- once
    before the Run inherited a review requirement and once after -- and both requests name the
    same ticket. Counting alone cannot tell an execution that ran while only the weaker answer
    existed from one that ran after the stronger one arrived, and those are the two cases this
    control has to separate.
    """
    decision_starts: Counter[int] = field(default_factory=Counter)
    """Executions paired with one decision *event*, keyed by its ``seq``.

    Not by decision id: a forged ``policy.decision`` re-using a recorded id would otherwise share
    its victim's count, and an id-less decision -- which no Gate writes, but a log may hold --
    could not be counted at all.
    """
    scope: ExecutionScope = field(default_factory=ExecutionScope)
    """Whether the Run's review requirement binds the execution being audited."""
    scopes: ApprovalScopeLedger = field(default_factory=ApprovalScopeLedger)
    """Whether the credit a start claims to spend was still in scope when the start ran.

    Recomputed from the request's own fields and from Core's own transition chain, never read
    off the Tool Manager's verdict: the producer refusing a closed credit is what this control
    exists to check, not evidence for it."""

    def record(self, event: Event) -> None:
        """Fold one event that is not a ``tool.started`` into the evidence."""
        if event.type is EventType.POLICY_DECISION:
            self._record_decision(event)
        elif event.type is EventType.RUN_TRANSITIONED:
            self.scope.record(event)
            self.scopes.record(event)
        elif event.type is EventType.HUMAN_APPROVAL_REQUESTED:
            self.requests.record(event)
            self.scopes.record(event)
        elif event.type is EventType.HUMAN_APPROVAL and is_human_answer(event):
            self._record_answer(event)

    def _record_decision(self, event: Event) -> None:
        """Index one ``policy.decision`` and arm the Run's review requirement when it carries one.

        A run-level decision whose recorded constraints require a human before any tool call
        describes THY's whole execution surface, not one call, so it arms a requirement that
        outlives the approval wait it causes. Nothing lowers it again -- a later run-level
        decision carrying empty constraints (a budget verdict, say) is about something else
        entirely, and reading it as a release would let any subsequent decision clear the
        requirement a human is still answering.
        """
        self.latest[event.subject_id] = event
        if _demands_run_wide_review(event):
            self.scope.arm()
        if (decision_id := _text(event.payload.get("id"))) is None:
            return
        if decision_id in self.decisions:
            # Ids are unique by construction, so a second decision under one id is a forgery
            # shadowing a real one. Which of the two a start meant is no longer knowable, so
            # neither of them authorises anything.
            self.duplicated_decision_ids.add(decision_id)
        self.decisions[decision_id] = event
        self.decision_pools.setdefault(decision_id, _pool_identity(event))

    def identity_of(self, decision_id: str) -> PoolIdentity | None:
        """The record one decision was taken under, or ``None`` when there is none to read.

        A decision the log never recorded reads the same as one whose own halves are unreadable,
        and for the same reason: there is no record of a surface, so naming it pays for nothing
        and counts toward nothing. That is the only reading that cannot be forged into a credit.
        """
        return self.decision_pools.get(decision_id)

    def _record_answer(self, event: Event) -> None:
        """Pool one human answer onto the call it decides, and onto its decision.

        Answers stay keyed by decision until an effect arrives. Only request intents that
        independently match that effect then join its ticket pool, so copied or malformed request
        evidence contributes neither an approval nor a final rejection to an honest call.
        """
        decision_id = approval_decision_id(event.payload)
        if decision_id is None:
            return
        if event.payload["approved"]:
            self.decision_approvals[decision_id] += 1
            self.ticket_timeline.append(TicketEvent(approved_decision=decision_id))
        else:
            self.rejected_decisions.add(decision_id)

    def authorizes(self, start: Event) -> bool:
        """Whether one ``tool.started`` ran under an allowing decision, by the manager's rule.

        The evidence is spent exactly as ``ToolManager`` spends it: every start carrying a ticket
        costs that ticket one credit whatever decision it names, and a start paired with a
        decision costs that decision its own single allowance. An unauthorised execution spends
        them too -- the manager counts ``tool.started`` events, not authorised ones -- so a replay
        can never be laundered by the attempt that preceded it. The ticket spend is appended after
        the verdict rather than before it because the question :meth:`_credit_stands` answers is
        whether a credit stood *when this start ran*; the per-decision allowance is still counted
        first, where "spent before it is read" says the same thing.
        """
        authorised = self._authorizes(start)
        if (claimed_ticket := _text(start.payload.get("tool_intent_sha256"))) is not None:
            self.ticket_timeline.append(TicketEvent(executed_ticket=claimed_ticket))
        return authorised

    def _authorizes(self, start: Event) -> bool:
        """Resolve the decision one start ran under, then ask whether it allowed the execution."""
        ticket = verified_effect_ticket(start) if has_ticket_claim(start) else None
        named = _text(start.payload.get("decision_id"))
        claimed = self.decisions.get(named) if named is not None else None
        # A start naming an id the log never recorded claims nothing: that is no better evidence
        # than naming none at all, so it falls back to its own subject like any older writer.
        decision = claimed if claimed is not None else self.latest.get(start.subject_id)
        if decision is None:
            return False
        # Count the decision spend before rejecting malformed evidence. The manager counts every
        # recorded start too, so an invalid first attempt cannot leave an answer or per-decision
        # allowance available to a later replay.
        self.decision_starts[decision.seq] += 1
        if not self._decides_this_call(decision, start, ticket):
            return False
        decision_id = _text(decision.payload.get("id"))
        if claimed is not None and claimed.subject_id != start.subject_id:
            return self._spends_a_ticket(claimed, decision_id, ticket, start)
        return self._subject_decision_allows(decision, decision_id, ticket, start)

    def _decides_this_call(self, decision: Event, start: Event, ticket: str | None) -> bool:
        """Whether the resolved decision is readable, about a tool call, and about this surface.

        Nothing here spends evidence -- the counted spends stay in :meth:`authorizes` and
        :meth:`_authorizes` -- so these are the questions that can be asked in any order:
        unverifiable ticket evidence on the start, an id the log recorded twice, a decision that
        is not about a tool call at all, and, while the Run's own review requirement applies, a
        decision that does not record it.

        That last one is the shape a stale answer arrives in. The requirement reaches each call as
        the constraints the engine merges onto that call's own decision, so a decision that does
        not carry it was taken against a surface that no longer exists -- however it was decided,
        and however genuine the human answer to it was. A separately approved older review is
        worth naming: its verdict is ``REQUIRE_HUMAN_REVIEW``, its ticket matches this exact
        effect and its answer is unspent, so nothing but the constraints it recorded tells it
        apart from the review this Run demands.
        """
        if has_ticket_claim(start) and ticket is None:
            return False
        decision_id = _text(decision.payload.get("id"))
        if decision_id is not None and decision_id in self.duplicated_decision_ids:
            return False
        if decision.payload.get("subject_kind") != TOOL_CALL_SUBJECT_KIND:
            return False
        return not self.scope.applies() or _carries_run_wide_review(decision)

    def _spends_a_ticket(
        self, claimed: Event, decision_id: str | None, ticket: str | None, start: Event
    ) -> bool:
        """Whether a start may execute under *another* call's decision: an unspent, exact ticket.

        Naming a decision is a claim, never a hint, so every part of the manager's ticket is
        recomputed: the decision had to require review, the request recorded for it had to name
        this start's own ``tool_intent_sha256`` and its tool, no human may have refused that
        ticket, and a credit taken under this decision's own recorded constraints had to be
        standing unspent when the start ran. Without those, any start could launder authorization
        through some other call's decision; with them, a claim MIRA cannot verify never falls back
        to the subject, because an unverifiable claim is not weaker evidence than no claim at all.
        """
        if ticket is None or decision_id is None:
            return False
        pool = self.identity_of(decision_id)
        return (
            pool is not None
            and claimed.payload.get("decision") == Decision.REQUIRE_HUMAN_REVIEW.value
            and self.requests.matches(decision_id, start)
            and not self._ticket_was_rejected(start)
            and self.scopes.credit_open(decision_id, start)
            and self._credit_stands(start, ticket, pool)
        )

    def _credit_stands(self, start: Event, ticket: str, pool: PoolIdentity) -> bool:
        """Whether an unspent credit taken under ``pool`` stood when this start ran.

        The pool of every decision whose own approval request independently names this effect goes
        in, so :func:`~thymira.mira.checks.ticket_pool.credit_stands` can fold the ticket's history
        knowing nothing about decisions, requests or recorded intents.
        """
        credit_pools = {
            decision_id: self.identity_of(decision_id)
            for decision_id, intent in self.requests.intents.items()
            if intent.matches(start)
        }
        return credit_stands(
            self.ticket_timeline, ticket=ticket, pool=pool, credit_pools=credit_pools
        )

    def _ticket_was_rejected(self, start: Event) -> bool:
        """Whether any human rejection belongs to a request matching this started effect."""
        return any(
            decision_id in self.rejected_decisions and intent.matches(start)
            for decision_id, intent in self.requests.intents.items()
        )

    def _subject_decision_allows(
        self, decision: Event, decision_id: str | None, ticket: str | None, start: Event
    ) -> bool:
        """Whether the decision recorded for a start's own subject allows that execution.

        Every decision is about one call, so it allows one execution: ``ToolManager`` asks the
        Gate once per ``ToolCall`` id, and a ``PASS`` a second start reuses is a call nobody
        decided. While ``self.scope.applies()``, a ``PASS`` or ``WARNING`` allows no execution:
        the requirement is a property of THY's execution
        surface, so the engine escalates every call under it and a permissive tool-call decision
        recorded against that surface is one the runtime would never have taken. A review adds the
        human answer that lifted it, spent by one execution and counted on the ticket the request
        named -- which the start has to carry, match, and run the tool of, even on its own subject
        -- or, for a writer that recorded no ticket at all, counted on the decision itself.
        """
        verdict = decision.payload.get("decision")
        if verdict in (Decision.PASS.value, Decision.WARNING.value):
            return not self.scope.applies() and self.decision_starts[decision.seq] <= 1
        if verdict != Decision.REQUIRE_HUMAN_REVIEW.value or decision_id is None:
            return False
        requested = self.requests.intents.get(decision_id)
        if decision_id not in self.requests.bound:
            return (
                decision_id not in self.rejected_decisions
                and self.decision_starts[decision.seq] <= self.decision_approvals[decision_id]
            )
        if requested is None or ticket is None or ticket != requested.ticket:
            return False
        pool = self.identity_of(decision_id)
        return (
            pool is not None
            and requested.matches(start)
            and not self._ticket_was_rejected(start)
            and self.scopes.credit_open(decision_id, start)
            and self._credit_stands(start, ticket, pool)
        )


@dataclass
class DenialLedger:
    """What A6 folds out of the log to decide whether one ``tool.denied`` is explained.

    A subject is explained by a decision recorded for it -- one that permanently denies, or a
    review still waiting for an answer. A retry of a call answered under *another* subject's
    review is explained by that review's own ticket instead, and the denial the manager writes
    for a call a human refused is explained by that refusal, which is why the requests and the
    decisions carrying human rejections are folded here too.
    """

    permanently_denied_subjects: set[str | None] = field(default_factory=set)
    seen_decision_ids: set[str] = field(default_factory=set)
    pending_decisions: dict[str, str | None] = field(default_factory=dict)
    pending_subjects: dict[str | None, set[str]] = field(default_factory=dict)
    non_allowing: set[str] = field(default_factory=set)
    run_level_decisions: dict[str, Event] = field(default_factory=dict)
    duplicated_decision_ids: set[str] = field(default_factory=set)
    requests: TicketRequests = field(default_factory=TicketRequests)
    rejected_decisions: set[str] = field(default_factory=set)

    def record(self, event: Event) -> None:
        """Fold one event that is not a ``tool.denied`` into the evidence."""
        if event.type is EventType.POLICY_DECISION:
            self._record_decision(event)
        elif event.type is EventType.HUMAN_APPROVAL_REQUESTED:
            self.requests.record(event)
        elif event.type is EventType.HUMAN_APPROVAL and is_human_answer(event):
            self._record_answer(event)

    def _record_decision(self, event: Event) -> None:
        """Fold one ``policy.decision`` into the subject, run-level and non-allowing bookkeeping."""
        decision_id = _text(event.payload.get("id"))
        if decision_id is not None:
            if decision_id in self.seen_decision_ids:
                # As in A3: a second decision under one id is a forgery shadowing a real one, and
                # a denial naming an ambiguous id is explained by neither of them.
                self.duplicated_decision_ids.add(decision_id)
            self.seen_decision_ids.add(decision_id)
            if event.payload.get("subject_kind") == RUN_SUBJECT_KIND:
                self.run_level_decisions[decision_id] = event
        decision = event.payload.get("decision")
        if decision in (Decision.PASS.value, Decision.WARNING.value):
            return
        if decision_id is not None:
            self.non_allowing.add(decision_id)
        if decision == Decision.REQUIRE_HUMAN_REVIEW.value and decision_id is not None:
            self.pending_decisions[decision_id] = event.subject_id
            self.pending_subjects.setdefault(event.subject_id, set()).add(decision_id)
        else:
            self.permanently_denied_subjects.add(event.subject_id)

    def _record_answer(self, event: Event) -> None:
        """Retire a review a human approved; remember the call a human refused.

        Only a human, non-automatic answer reaches here, so the Gate's own automatic answer
        leaves the review in place and the manager's fail-closed refusal that follows it stays
        explained. A rejection never retires a review either: it keeps refusing the retries it
        causes, and its decision remains recorded so a denial joins it only when that decision's
        request independently matches the denied effect. Thus a sibling approval cannot erase a
        real rejection, while a copied digest cannot contaminate an honest ticket.
        """
        decision_id = approval_decision_id(event.payload)
        if decision_id is None:
            return
        if event.payload["approved"]:
            self._retire_review(decision_id)
        else:
            self.rejected_decisions.add(decision_id)

    def _retire_review(self, decision_id: str) -> None:
        """Drop a review a human approved: after that answer it denies nothing."""
        if decision_id not in self.pending_decisions:
            return
        self.non_allowing.discard(decision_id)
        subject_id = self.pending_decisions.pop(decision_id)
        subject_decisions = self.pending_subjects[subject_id]
        subject_decisions.remove(decision_id)
        if not subject_decisions:
            del self.pending_subjects[subject_id]

    def explains(self, event: Event) -> bool:
        """Whether the log holds the refusal that produced this denial."""
        if has_ticket_claim(event) and verified_effect_ticket(event) is None:
            return False
        if self._ticket_required(event) and not has_ticket_claim(event):
            return False
        return (
            self._subject_was_refused(event)
            or self._names_a_refusing_decision(event)
            or self._names_a_refused_call(event)
        )

    def _ticket_required(self, event: Event) -> bool:
        """Whether the denial points at a request that recorded ticket evidence."""
        decision_id = _text(event.payload.get("decision_id"))
        if decision_id in self.requests.bound:
            return True
        return any(
            pending in self.requests.bound
            for pending in self.pending_subjects.get(event.subject_id, ())
        )

    def _subject_was_refused(self, event: Event) -> bool:
        """Whether a decision recorded for the denial's own subject refuses it."""
        named = _text(event.payload.get("decision_id"))
        if named is not None and self.pending_decisions.get(named) == event.subject_id:
            if named not in self.requests.bound:
                return True
            return self.requests.matches(named, event)
        if event.subject_id in self.permanently_denied_subjects:
            return True
        pending = self.pending_subjects.get(event.subject_id, ())
        if any(decision_id not in self.requests.bound for decision_id in pending):
            return True
        return any(self.requests.matches(decision_id, event) for decision_id in pending)

    def _names_a_refusing_decision(self, event: Event) -> bool:
        """Whether the decision the denial names is one that really refused this call.

        A run-level decision qualifies in two shapes and no others (see :data:`RUN_SUBJECT_KIND`):
        it does not allow execution at all, or it is the ``execution.start`` decision whose
        *recorded* ``ExecutionConstraints`` produce exactly the refusal the denial quotes. Any
        other decision has to still refuse this exact call
        (:meth:`_names_a_denying_ticket`). A decision id the log recorded twice qualifies for
        nothing: the forgery and the real decision are no longer distinguishable.
        """
        decision_id = _text(event.payload.get("decision_id"))
        if decision_id is None or decision_id in self.duplicated_decision_ids:
            return False
        return self._run_level_refusal(decision_id, event) or self._names_a_denying_ticket(event)

    def _run_level_refusal(self, decision_id: str, event: Event) -> bool:
        """Whether a run-level decision refused this call, by its verdict or by its constraints.

        The constraint check recomputes :func:`~thymira.schemas.constraint_refusals` for the tool
        this denial names. The canonical payload preserves the model-visible tool name and the
        actor id must match it exactly; presentation copies redact the field at their boundary.
        """
        recorded = self.run_level_decisions.get(decision_id)
        if recorded is None:
            return False
        if decision_id in self.non_allowing:
            return True
        constraints = _recorded_constraints(recorded)
        reason = event.payload.get("reason")
        if constraints is None or not isinstance(reason, str):
            return False
        candidates = [_text(event.payload.get("tool"))]
        if event.actor.kind is ActorKind.TOOL:
            candidates.append(event.actor.id)
        return any(
            tool is not None and reason in constraint_refusals(constraints, tool)
            for tool in candidates
        )

    def _names_a_refused_call(self, event: Event) -> bool:
        """Whether the call this denial recorded was refused without any decision to name.

        Two shapes: a human's rejection is final for that ticket, and an agent-allowlist refusal
        happens before the Gate is asked at all, so it names no decision and carries only the
        manager's own reason. Redaction can turn a tool name that looks like a secret -- a PEM
        banner is the case that surfaced this -- into a marker in the payload's own ``tool`` field
        while leaving it untouched inside the longer ``reason`` sentence and on ``event.actor.id``
        (never redacted: the actor is not part of the payload). Both candidates are tried, so a
        redacted-looking tool name still lets a legitimate refusal explain its own denial.
        """
        if any(
            intent.matches(event)
            for decision_id, intent in self.requests.intents.items()
            if decision_id in self.rejected_decisions
        ):
            return True
        if _text(event.payload.get("decision_id")) is not None:
            return False
        agent_id = _text(event.payload.get("agent_id"))
        reason = event.payload.get("reason")
        candidates = [_text(event.payload.get("tool"))]
        if event.actor.kind is ActorKind.TOOL:
            candidates.append(event.actor.id)
        for tool in candidates:
            if tool is None:
                continue
            resolved_agent_id = (
                agent_id if agent_id is not None else _legacy_allowlist_actor(event, tool)
            )
            if resolved_agent_id is not None and reason == agent_allowlist_refusal(
                resolved_agent_id, tool
            ):
                return True
        return False

    def _names_a_denying_ticket(self, event: Event) -> bool:
        """Whether a denial names a decision that still refuses this exact call.

        Both halves are verified, for the same reason A3 verifies a start's claim: the decision
        must still be non-allowing -- a review a human approved leaves that set -- and the
        approval request recorded for it must have named this denial's own
        ``tool_intent_sha256``. Without the ticket, an unrelated ``BLOCK`` minted for some other
        subject would explain any denial quoting its id. A denial naming its own subject's
        decision needs none of this: the subject bookkeeping already explains it.
        """
        decision_id = _text(event.payload.get("decision_id"))
        return (
            decision_id is not None
            and decision_id in self.non_allowing
            and self.requests.matches(decision_id, event)
        )
