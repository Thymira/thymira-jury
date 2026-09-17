"""The one-shot approval ticket: what a human answered, and whether it is still hers to spend.

A tool call's *effect* -- the tool and its validated arguments -- is digested into a
``tool_intent_sha256``. That digest is the identity a human's answer is bound to, so an answer
authorises exactly the call it was asked about, once. This module holds the fold that
reconstructs those answers from the event log: which decisions a ticket may draw answers from,
which of those still describe what the Policy Engine would decide today, and which have already
been spent by an execution. The Tool Manager asks; nothing here records, executes or authorises
anything on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from thymira.events import canonical_json, sha256_text
from thymira.policies import (
    DELEGATION_DEPTH_KEY,
    RecordedApprovalScope,
    ScopeClosure,
    ScopeClosureReason,
    UnknownApprovalError,
    decision_from_events,
    recorded_delegation_depth,
)
from thymira.schemas import (
    ActorKind,
    Approval,
    Decision,
    EventType,
    Id,
    PolicyDecision,
    SandboxMode,
    approval_decision_id,
    approval_names_decision,
    new_id,
)

if TYPE_CHECKING:
    from collections.abc import Container, Mapping, Sequence

    from thymira.policies import CapabilityEvaluation, ToolCapability
    from thymira.schemas import Event, ExecutionConstraints
    from thymira.tools.models import ToolContext

_ID_ADAPTER = TypeAdapter(Id)


_DESCRIPTION_KEY = "description"


def tool_intent_sha256(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    sandbox_mode: SandboxMode | None = None,
) -> str:
    """Digest the effect of one tool call: the tool and its validated arguments.

    This is the identity a human's answer is bound to, and what a resumed step must reproduce
    to be handed that answer. ``description`` is left out: it is the model's narration of the
    call, not its effect, and a resumed step phrases it differently while asking for exactly
    the same call. The canonical event retains those values, so the digest can be recomputed from
    the recorded evidence alone; presentation copies redact them at their own boundary.

    Args:
        tool_name: The registered tool the call names.
        arguments: The call's validated arguments.
        sandbox_mode: The runtime-owned confinement requested for subprocess execution.

    Returns:
        The hex sha256 of the canonical JSON of the call's effect.
    """
    effect = {key: value for key, value in arguments.items() if key != _DESCRIPTION_KEY}
    identity: dict[str, Any] = {"tool": tool_name, "arguments": effect}
    if sandbox_mode is not None:
        identity["sandbox_mode"] = sandbox_mode.value
    return sha256_text(canonical_json(identity))


def _recorded_scopes(events: Sequence[Event]) -> dict[str, RecordedApprovalScope]:
    """Bind each decision to the scope the first approval request recorded for it.

    One decision authorises one call. :meth:`Gate._record` writes at most one ticketed request per
    decision, so a second one naming that decision is evidence of nothing -- and reading it as a
    rebinding would let any writer point a human's pending answer at a different call: the human
    is asked about ``echo`` and the "yes" buys a ``run_python``. The first ticketed request
    therefore wins, and a decision bound to another call never joins this ticket's pool. The scope
    -- the deadline and the delegation depth the credit was raised under -- is read off that same
    first request, so a later one can no more widen the scope than re-point the ticket.

    A request carrying no ticket binds nothing and unbinds nothing: :meth:`Gate.request_approval`
    writes one for every control-plane answer, and it names no call at all.

    Args:
        events: The Run's events, in order.

    Returns:
        The scope each decision was first asked about, by decision id.
    """
    scopes: dict[str, RecordedApprovalScope] = {}
    for event in events:
        scope = RecordedApprovalScope.from_request(event)
        if scope is not None:
            scopes.setdefault(scope.decision_id, scope)
    return scopes


def _ticket_bindings(events: Sequence[Event]) -> dict[str, str]:
    """The ``tool_intent_sha256`` each decision was first asked about, by decision id."""
    return {decision_id: scope.ticket for decision_id, scope in _recorded_scopes(events).items()}


@dataclass(frozen=True, slots=True)
class Answer:
    """The latest human answer the log holds for one exact call."""

    decision: PolicyDecision
    approval: Approval | None
    """A real, unspent approval reconstructed from the answer event; ``None`` for a rejection."""
    note: str | None = None


def rejection_reason(answer: Answer) -> str:
    """Render the denial reason for a human's rejection, with her note appended when there is one.

    Shared by both call sites that deny on ``answer.approval is None`` -- the retry path at the
    top of :meth:`ToolManager.execute` and the synchronous re-read right after a fresh
    ``REQUIRE_HUMAN_REVIEW`` decision -- so the wording stays one string, not two copies that could
    drift apart.
    """
    return "tool call rejected by a human" + (f": {answer.note}" if answer.note else "")


def _current_evaluation(context: ToolContext, capability: ToolCapability) -> CapabilityEvaluation:
    """What the Policy Engine would decide for this call right now, recording nothing.

    Asked through the engine's own non-recording seam rather than re-derived here, because the
    manager guessing at the constraints is exactly how a stale answer slipped past a refusal: two
    disjoint allow-lists merge to an empty one, an empty one reads as "unconstrained", and an old
    approval recorded under no constraints at all then looked like an answer to the narrowed call
    the engine now refuses outright. One evaluation, one answer, and the manager holds every
    stored answer against it.
    """
    return context.gate.engine.evaluate_capability(
        capability=capability,
        risk=context.risk_profile,
        inherited_constraints=context.execution_constraints,
    )


def _current_decisions(
    context: ToolContext, events: Sequence[Event], in_force: ExecutionConstraints
) -> dict[str, PolicyDecision]:
    """Index the recorded decisions that still describe what the engine would decide right now.

    A decision is *current* only while both halves of the record it was taken under still hold:
    the policy snapshot it quotes is the one now loaded, and the execution constraints it recorded
    are the ones in force for this call. Either drifting means the human answered a question the
    engine would no longer ask -- an approval given before the Run inherited a review requirement
    is not an answer to the narrower call -- so that answer can no longer buy an execution.

    The first event recording an id wins, as in :func:`decision_from_events`: a second decision
    under one id is a forgery, and letting it replace the real record would let any writer relax
    the constraints an answer is held to.
    """
    policy = context.gate.engine.policy_sha256
    current: dict[str, PolicyDecision] = {}
    seen: set[str] = set()
    for event in events:
        decision_id = event.payload.get("id")
        if event.type is not EventType.POLICY_DECISION or not isinstance(decision_id, str):
            continue
        if decision_id in seen:
            continue
        seen.add(decision_id)
        try:
            decision = PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError):
            continue
        if decision.policy_sha256 == policy and decision.execution_constraints == in_force:
            current[decision_id] = decision
    return current


def _charges_this_pool(start: Event, delegation_depth: int) -> bool:
    """Whether one ``tool.started`` is an execution the caller's own credit pool paid for.

    The pool is filtered by delegation depth -- a credit raised for the root agent is not the
    delegate's to spend -- so the executions charged against it have to be filtered the same way.
    While they were not, a parent's own legitimate start was charged to the delegate's pool, ate
    the yes a human had just given the delegate, and made a second review appear out of nowhere.

    A start recording no readable depth charges every pool, which is exactly the behaviour every
    log written before the manager recorded the key had: an execution nobody can attribute must
    never be free.
    """
    recorded = recorded_delegation_depth(start.payload.get(DELEGATION_DEPTH_KEY))
    return recorded is None or recorded == delegation_depth


def _unspent_answer(
    events: Sequence[Event],
    ticket: str,
    answers: Sequence[Event],
    current: Mapping[str, PolicyDecision],
    *,
    creditable: Container[int],
    delegation_depth: int,
) -> tuple[Event, PolicyDecision] | None:
    """Fold this ticket's answers and executions in log order and return the credit still unspent.

    One human answer buys one execution, so every ``tool.started`` this caller's pool paid for
    spends one credit from the pool standing at that point in the log. *Which* credit it spends is
    decided fail-closed, and each half of that matters:

    - A credit whose decision is still current is spent before one the Run's constraints have
      since invalidated. Otherwise an execution recorded while a strong answer was outstanding
      could be explained away as having used the weak one, leaving the strong credit over for a
      replay -- two effects for one human yes.
    - An execution recorded when no credit at all stood is a debt the next answer pays. Otherwise
      a start written before anyone answered would cost nothing, and the human's later yes would
      buy a second execution of a call that already ran.
    - An answer outside ``creditable`` -- its scope closed, or it belongs to another delegation
      depth -- is folded in as *stale* rather than dropped. It authorises nothing either way, but
      it still accounts for the execution it once bought. Dropping it turned that settled start
      into an invented arrears, and the next fresh human yes went on paying that instead of
      buying the call in front of it: one execution for two approvals.
    - A start belongs to the pool of the depth it recorded (:func:`_charges_this_pool`); a start
      that recorded none charges every pool.

    A start naming no decision, or one the log never recorded, is not special-cased: it spends
    from the same pool like every other execution of this effect at this depth. Only a still-
    current credit authorises anything; a leftover stale one buys nothing, and the latest of the
    current ones is the answer.
    """
    answered = {event.seq for event in answers}
    valid: list[tuple[Event, PolicyDecision]] = []
    stale = 0
    debt = 0
    for event in events:
        if event.seq in answered:
            if debt:
                debt -= 1
            elif event.seq not in creditable:
                stale += 1
            elif (decision := current.get(approval_decision_id(event.payload) or "")) is not None:
                valid.append((event, decision))
            else:
                stale += 1
        elif (
            event.type is EventType.TOOL_STARTED
            and event.payload.get("tool_intent_sha256") == ticket
            and _charges_this_pool(event, delegation_depth)
        ):
            if valid:
                valid.pop(0)
            elif stale:
                stale -= 1
            else:
                debt += 1
    return valid[-1] if valid else None


def _answer_note(answer: Event) -> str | None:
    """The human's own note on one answer, under either key the runtime's writers use."""
    raw = answer.payload.get("note", answer.payload.get("rationale"))
    return raw if isinstance(raw, str) and raw else None


def _rejection_answer(events: Sequence[Event], rejection: Event) -> Answer | None:
    """Turn a human's refusal into the final answer for this ticket.

    A refusal is not a credit a narrowing surface can invalidate but a human saying no to this
    exact call, so it is final for the Run whatever the constraints or the policy do next and no
    currency check applies to it. It decides nothing only when the decision it names was never
    recorded, because then there is no refusal to quote.
    """
    decision_id = approval_decision_id(rejection.payload)
    assert decision_id is not None  # noqa: S101  # filtered by the caller; documents the invariant
    try:
        decision = decision_from_events(events, decision_id)
    except UnknownApprovalError:
        return None
    return Answer(decision=decision, approval=None, note=_answer_note(rejection))


def human_answer(context: ToolContext, ticket: str, capability: ToolCapability) -> Answer | None:
    """The human answer that decides this exact call, or ``None`` when none does.

    A thin reading of :func:`ticket_disposition`, not a second fold: only
    :attr:`TicketOutcome.ALLOWED_ONCE` and :attr:`TicketOutcome.REJECTED` are a human's own
    answer, and everything else -- a call the engine now refuses, a credit whose scope closed, a
    credit already spent, no answer at all -- is a ``None`` that sends the call down the ordinary
    Gate path. Two copies of a fail-closed fold is one copy too many: the depth filter and the
    closure accounting were fixed in exactly one place because there is exactly one place.

    Args:
        context: The manager's context for this call; its log and its clock are the only evidence.
        ticket: The call's ``tool_intent_sha256``.
        capability: The called tool's capability, used to recompute the constraints in force.

    Returns:
        The human answer that decides ``ticket``, or ``None`` when there is none to act on.
    """
    disposition = ticket_disposition(context, ticket, capability)
    if disposition.outcome in (TicketOutcome.ALLOWED_ONCE, TicketOutcome.REJECTED):
        return disposition.answer
    return None


class TicketOutcome(StrEnum):
    """The closed vocabulary of what one ticketed tool call's approval evidence amounts to.

    Member-local on purpose: it is enforced exactly here, so it lives here rather than in a frozen
    contract enum every consumer would have to learn (ADR-0013). It is recorded durably all the
    same -- ``ticket_outcome`` on ``tool.denied`` -- so a later reader never has to re-derive it.
    """

    ALLOWED_ONCE = "allowed_once"
    """A human's answer authorises this exact call, once, and this call is that once."""
    REJECTED = "rejected"
    """A human refused this exact call; the refusal is final for the Run."""
    CANCELLED = "cancelled"
    """The scope the credit was raised in has closed: the Run ended, its pass ended, or the
    deadline passed. Whether the call may ask again depends on
    :attr:`~thymira.policies.ScopeClosure.terminates_the_run`."""
    UNAVAILABLE = "unavailable"
    """The Policy Engine refuses this call outright -- never, whoever answers."""
    PENDING = "pending"
    """No answer of this caller's own stands yet; the call takes the ordinary Gate path."""


@dataclass(frozen=True, slots=True)
class TicketDisposition:
    """What the log says about one ticketed call, before the Gate is asked anything.

    ``answer`` is set only for :attr:`TicketOutcome.ALLOWED_ONCE` and
    :attr:`TicketOutcome.REJECTED` -- the two outcomes a human's own answer decides.
    ``closure`` says why a credit stopped existing, and ``scope_sha256`` names the scope the
    outcome is about: the one spent on an allowed call, the closed one on a cancelled one.
    """

    outcome: TicketOutcome
    answer: Answer | None = None
    closure: ScopeClosure | None = None
    scope_sha256: str | None = None


def closed_scope_reason(closure: ScopeClosure) -> str:
    """Render the denial reason for a call whose approval scope has closed."""
    wording = {
        ScopeClosureReason.RUN_TERMINAL: "the run it was granted in has ended",
        ScopeClosureReason.SCOPE_RELEASED: "the step it was granted in has ended",
        ScopeClosureReason.EXPIRED: "it expired",
    }[closure.reason]
    return f"tool call denied: the approval for it is closed because {wording}"


def _credit_answer(
    context: ToolContext, ticket: str, decisive: Event, decision: PolicyDecision
) -> Answer:
    """Rebuild the :class:`Approval` one unspent human answer stands for."""
    note = _answer_note(decisive)
    try:
        recorded_id = _ID_ADAPTER.validate_python(decisive.payload.get("id"))
    except ValidationError:
        recorded_id = new_id("approval")
    if not recorded_id.startswith("approval_"):
        recorded_id = new_id("approval")
    approval = Approval(
        id=recorded_id,
        run_id=context.run_id,
        policy_decision_id=decision.id,
        # For a tool call the authorization context *is* the intent: the exact call.
        authorization_context_sha256=ticket,
        approved=True,
        approved_by=decisive.actor,
        rationale=note,
    )
    return Answer(decision=decision, approval=approval, note=note)


def _ticket_answers(events: Sequence[Event], bound: Container[str | None]) -> list[Event]:
    """Every real human answer the log holds for the decisions bound to one ticket.

    An event a system, agent or tool actor recorded is evidence of nothing, whatever its payload
    says: :meth:`Gate.request_approval` appends a serialised ``Approval`` -- which carries no
    ``automatic`` key at all -- under the automation actor whenever the approver is automatic, so
    the actor is the fail-closed tell and the ``"automatic": true`` marker the Gate's other path
    writes is belt and braces. A declared but unauthenticated human identity is also evidence of
    nothing; only an authenticated human can spend a ticket.
    """
    return [
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL
        and approval_decision_id(event.payload) in bound
        and isinstance(event.payload.get("approved"), bool)
        and event.actor.kind is ActorKind.HUMAN
        and event.actor.authenticated
        and ("automatic" not in event.payload or event.payload["automatic"] is False)
    ]


def _live_credits(
    context: ToolContext,
    answers: Sequence[Event],
    bound: Mapping[str, RecordedApprovalScope],
    events: Sequence[Event],
) -> tuple[list[Event], ScopeClosure | None, str | None]:
    """Split a ticket's approving answers into the ones still in scope and the ones closed out.

    Three separate refusals, and they mean different things to the caller:

    - the scope closed (the Run ended, its pass ended, or the deadline passed) -- reported back,
      because a Run-terminal closure must not raise a fresh review for a dead Run;
    - the answer itself was recorded after its own deadline -- the same closure, at answer time,
      so a very stale yes never becomes currency;
    - the credit belongs to another delegation depth -- *not* a closure. Nothing ended; this
      caller simply was not the one a human authorised, so it asks for itself.

    A run-terminal closure outranks any other, because it is the one the manager may not answer
    with a fresh review.
    """
    now = context.now()
    live: list[Event] = []
    closed: ScopeClosure | None = None
    closed_scope: str | None = None
    for event in answers:
        scope = bound[approval_decision_id(event.payload) or ""]
        closure = scope.closure(events, now=now)
        if closure is None and scope.answered_late(event.ts):
            closure = ScopeClosure(ScopeClosureReason.EXPIRED, event.seq)
        if closure is not None:
            if closed is None or (closure.terminates_the_run and not closed.terminates_the_run):
                closed, closed_scope = closure, scope.digest
            continue
        if scope.spendable_at_depth(context.delegation_depth):
            live.append(event)
    return live, closed, closed_scope


def ticket_disposition(  # noqa: PLR0911  # the fail-closed order is the guarantee; each refusal returns where it is decided
    context: ToolContext, ticket: str, capability: ToolCapability
) -> TicketDisposition:
    """Fold the log for what already decides this exact call, in fail-closed order.

    The order is the guarantee, not a convenience. A never decision is taken **before any
    responder is read**: the engine is asked first, and a ``BLOCK`` returns
    :attr:`TicketOutcome.UNAVAILABLE` without a single ``human.approval`` event being consulted.
    Nothing a human said earlier, and nothing a human says later, can move that. Then a rejection,
    which is final for the Run wherever it sits among the answers -- a model that asks for the
    identical call twice before anyone answers leaves two pending decisions, and trusting order
    would let an approval of the second overturn a human who already refused the first. Then
    scope: a credit whose Run ended, whose pass ended, whose deadline passed, or which belongs to
    another delegation depth, is not this call's to spend. Only what survives all three is held
    against the currency test and the credit fold, exactly as before.

    Which decisions this ticket may draw answers from is decided by :func:`_recorded_scopes`, not
    by scanning the requests for a matching digest: one decision binds the one call it was first
    asked about, so a later request naming that decision cannot move a pending answer onto another
    call.

    Args:
        context: The manager's context for this call; its log and its clock are the only evidence.
        ticket: The call's ``tool_intent_sha256``.
        capability: The called tool's capability, used to recompute the constraints in force.

    Returns:
        What the log already decides about this call, and the scope that decision is about.
    """
    events = context.event_log.events()
    bound = {
        decision_id: scope
        for decision_id, scope in _recorded_scopes(events).items()
        if scope.ticket == ticket
    }
    evaluated = _current_evaluation(context, capability)
    # Never, first: the engine is asked before any answer is read, so no listener order and no
    # recorded yes can move the refusal. The call falls through to the Gate, which records that
    # BLOCK as the decision denying it -- the refusal has to be evidence, not a silence.
    if evaluated.decision is Decision.BLOCK:
        return TicketDisposition(TicketOutcome.UNAVAILABLE)
    answers = _ticket_answers(events, bound)
    if not answers:
        return TicketDisposition(TicketOutcome.PENDING)
    rejection = next((event for event in answers if event.payload["approved"] is False), None)
    if rejection is not None:
        refused = _rejection_answer(events, rejection)
        if refused is None:
            return TicketDisposition(TicketOutcome.PENDING)
        return TicketDisposition(TicketOutcome.REJECTED, answer=refused)
    # Every remaining answer approves: any rejection at all returned on the line above.
    live, closed, closed_scope = _live_credits(context, answers, bound, events)
    if not live:
        if closed is not None:
            return TicketDisposition(
                TicketOutcome.CANCELLED, closure=closed, scope_sha256=closed_scope
            )
        return TicketDisposition(TicketOutcome.PENDING)
    current = _current_decisions(context, events, evaluated.execution_constraints)
    # Every answer is folded, but only the live ones may become a credit: a closed or
    # wrong-depth answer still accounts for the execution it already bought, so the next fresh
    # yes buys the call in front of it instead of settling a start that was already paid for.
    unspent = _unspent_answer(
        events,
        ticket,
        answers,
        current,
        creditable={event.seq for event in live},
        delegation_depth=context.delegation_depth,
    )
    if unspent is None:
        return TicketDisposition(TicketOutcome.PENDING)
    decisive, decision = unspent
    spent = bound.get(decision.id)
    return TicketDisposition(
        TicketOutcome.ALLOWED_ONCE,
        answer=_credit_answer(context, ticket, decisive, decision),
        scope_sha256=spent.digest if spent is not None else None,
    )


def unanswered(context: ToolContext, decision_id: str) -> bool:
    """Whether no valid approval answer -- automatic or authenticated human -- names the decision.

    An automatic or system answer counts as an answer here even though it never authorizes
    anything (:func:`human_answer` ignores it), so a decision a Gate answered in process is never
    reported as still pending: "pending" means a *human* has yet to be asked, and a Run parked on
    a decision an automatic approver already answered would wait for someone who will never come
    (design decision 3).
    """
    return not any(
        event.type is EventType.HUMAN_APPROVAL
        and approval_names_decision(event.payload, decision_id)
        and isinstance(event.payload.get("approved"), bool)
        and (
            event.actor.kind is ActorKind.SYSTEM
            or (
                event.actor.kind is ActorKind.HUMAN
                and event.actor.authenticated
                and ("automatic" not in event.payload or event.payload["automatic"] is False)
            )
        )
        for event in context.event_log.events()
    )
