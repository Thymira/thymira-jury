"""When the authority a human granted for one tool call stops existing.

A ticketed approval has an identity (``tool_intent_sha256``) and a currency test (the policy
snapshot plus the execution constraints). This module covers the third question -- *scope*: the
Run lifecycle the credit was raised in, the deadline it carries, and the delegation depth it was
raised at. Every node here is a behaviour of :func:`~thymira.tools.approval_ticket.
ticket_disposition` or of :class:`~thymira.tools.ToolManager` over a real event log; nothing
asserts an implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from thymira.events import InMemoryEventLog
from thymira.policies import (
    APPROVAL_EXPIRES_AT_KEY,
    APPROVAL_SCOPE_DIGEST_KEY,
    DELEGATION_DEPTH_KEY,
    ActionRule,
    ApprovalScope,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RecordedApprovalScope,
    RiskProfile,
    ScopeClosureReason,
    ToolCapability,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    Decision,
    EventType,
    RunCondition,
    RunOutcome,
    RunStage,
    RunState,
    ToolCallStatus,
    new_id,
    utc_now,
)
from thymira.state import LocalArtifactStore
from thymira.tools import (
    LegacyToolValue,
    TicketOutcome,
    Tool,
    ToolContext,
    ToolManager,
    ToolRegistry,
    ToolResult,
    tool_intent_sha256,
)
from thymira.tools.approval_ticket import ticket_disposition

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.tools import ToolInvocation


class _EchoArguments(BaseModel):
    value: str = ""
    description: str = ""


@dataclass
class _FakeTool(Tool):
    name: str
    capability: ToolCapability
    description: str = "A fake tool for scope tests."
    arguments_model: type[BaseModel] | None = _EchoArguments
    result_model = LegacyToolValue
    seen: list[dict[str, Any]] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        self.seen.append(dict(arguments))
        return ToolResult(success=True, stdout="ok", value=LegacyToolValue(text="ok"))


@dataclass
class _Clock:
    """An injectable clock the Tool Manager reads instead of the wall clock."""

    at: datetime

    def __call__(self) -> datetime:
        return self.at

    def advance(self, delta: timedelta) -> None:
        self.at = self.at + delta


_CAPABILITY = ToolCapability(id="echo", external_effects=())
_START_RULE = ActionRule(
    id="START", action_types=("execution.start",), decision=Decision.PASS, reason="test start"
)
_REVIEW_POLICY = Policy(
    name="scope-review",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(
            id="REVIEW",
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Review the exact call.",
        ),
    ),
)
_NEVER_POLICY = Policy(
    name="scope-never",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(id="NEVER", decision=Decision.BLOCK, reason="This call is never allowed."),
    ),
)


def _context(tmp_path: Path, *, clock: _Clock, delegation_depth: int = 0) -> ToolContext:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(_REVIEW_POLICY), log, approver=None),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
        delegation_depth=delegation_depth,
        now=clock,
    )


def _human_answer(context: ToolContext, decision_id: str, *, approved: bool) -> None:
    """Record what ``Gate.resolve_pending_approval`` writes when a real human answers."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


_ACTIVE_STAGES = {
    "begin_audit": RunStage.AUDITING,
    "begin_reporting": RunStage.REPORTING,
    "reopen": RunStage.EXECUTING,
}
_TERMINAL_STATES = {
    "complete": (RunStage.REPORTING, RunOutcome.COMPLETED),
    "block": (RunStage.EXECUTING, RunOutcome.BLOCKED),
    "fail": (RunStage.EXECUTING, RunOutcome.FAILED),
    "cancel": (RunStage.EXECUTING, RunOutcome.CANCELLED),
}


def transition_payload(command: str, *, version: int = 3) -> dict[str, Any]:
    """The whole payload ``RunController`` writes for one command, state and flat copy included.

    A closure is believed only from a payload that agrees with itself, so a test that wants a real
    transition has to write a real one: the serialised ``RunState``, the flattened copy of its
    fields, the command's own stage or outcome, and the version reached.
    """
    if command in _ACTIVE_STAGES:
        state = RunState(
            stage=_ACTIVE_STAGES[command], condition=RunCondition.ACTIVE, version=version
        )
    else:
        stage, outcome = _TERMINAL_STATES[command]
        state = RunState(
            stage=stage, condition=RunCondition.TERMINAL, outcome=outcome, version=version
        )
    return {
        "command": command,
        "from_version": version - 1,
        "to_version": version,
        "previous_stage": state.stage.value,
        "stage": state.stage.value,
        "condition": state.condition.value,
        "wait_reason": None,
        "outcome": state.outcome.value if state.outcome is not None else None,
        "state": state.model_dump(mode="json"),
    }


def _core_transition(context: ToolContext, command: str) -> None:
    """Append the ``run.transitioned`` fact ``RunController`` writes for one command."""
    context.event_log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        transition_payload(command),
        subject_id=context.run_id,
        producer="thymira.core",
        producer_version="0.1",
    )


def _requests(context: ToolContext) -> list[Any]:
    return [
        event
        for event in context.event_log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    ]


def _starts(context: ToolContext) -> list[Any]:
    return [event for event in context.event_log.events() if event.type is EventType.TOOL_STARTED]


def _denials(context: ToolContext) -> list[Any]:
    return [event for event in context.event_log.events() if event.type is EventType.TOOL_DENIED]


def _answers(context: ToolContext) -> list[Any]:
    return [event for event in context.event_log.events() if event.type is EventType.HUMAN_APPROVAL]


def _reviewed_and_approved(context: ToolContext, manager: ToolManager) -> str:
    """Park the call on a review, answer it with a human yes, and return the decision id."""
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.call.status is ToolCallStatus.DENIED
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)
    return first.pending_approval.id


def test_a_credit_dies_with_the_run_it_was_raised_in(tmp_path: Path) -> None:
    """A human's unspent yes buys nothing once Core cancelled the Run that asked for it.

    F5.1's caller abort at the authorization boundary: the abort is a Core fact on the chain,
    and the credit closes against it without anyone re-asking a human.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    decision_id = _reviewed_and_approved(context, manager)
    _core_transition(context, "cancel")

    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert denied.pending_approval is None
    assert tool.seen == []
    assert _starts(context) == []
    last = _denials(context)[-1]
    assert last.payload["ticket_outcome"] == TicketOutcome.CANCELLED.value
    assert (
        last.payload["closed_scope_sha256"]
        == _requests(context)[0].payload[APPROVAL_SCOPE_DIGEST_KEY]
    )
    # The Run is dead: no fresh review was raised for anyone to answer.
    assert len(_requests(context)) == 1
    assert decision_id == _requests(context)[0].payload["decision_id"]


@pytest.mark.parametrize("command", ["begin_audit", "begin_reporting", "reopen"])
def test_a_credit_does_not_survive_the_pass_that_asked_for_it(tmp_path: Path, command: str) -> None:
    """An unspent credit is released when the THY pass that raised it ends.

    The Run is still alive, so the next identical call is not refused outright -- it takes the
    Gate path and gets a review of its own rather than spending the previous pass's answer.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    first_decision = _reviewed_and_approved(context, manager)
    _core_transition(context, command)

    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []
    assert denied.pending_approval is not None
    assert denied.pending_approval.id != first_decision
    assert len(_requests(context)) == 2


def test_a_credit_expires_and_authorizes_nothing_afterwards(tmp_path: Path) -> None:
    """F5.1 expiration at consumption: a standing credit stops paying once its deadline passes."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    first_decision = _reviewed_and_approved(context, manager)
    recorded = _requests(context)[0].payload[APPROVAL_EXPIRES_AT_KEY]
    assert datetime.fromisoformat(recorded) == clock.at + context.approval_ttl

    clock.advance(context.approval_ttl + timedelta(minutes=1))
    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []
    assert denied.pending_approval is not None
    assert denied.pending_approval.id != first_decision


def test_an_answer_recorded_after_the_deadline_is_not_a_credit(tmp_path: Path) -> None:
    """F5.1 expiration at answer time: a very stale yes never becomes currency.

    The clock the request was minted on sits far in the past, so the deadline it recorded is
    already behind the answer the human really gives now. The credit is closed even though the
    caller's own clock has not yet reached the deadline it would compute today.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(datetime(2020, 1, 1, tzinfo=UTC))
    context = _context(tmp_path, clock=clock)
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []


def test_a_never_decision_is_checked_before_any_human_answer(tmp_path: Path) -> None:
    """F5.4: a call the engine now refuses is unavailable, whatever a human answered before."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    _reviewed_and_approved(context, manager)
    never = replace(context, gate=Gate(PolicyEngine(_NEVER_POLICY), context.event_log))

    disposition = ticket_disposition(
        never, tool_intent_sha256("echo", {"value": "x"}, sandbox_mode=None), _CAPABILITY
    )

    assert disposition.outcome is TicketOutcome.UNAVAILABLE
    assert disposition.answer is None

    denied = manager.execute(never, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []
    blocking = [
        event
        for event in context.event_log.events()
        if event.type is EventType.POLICY_DECISION
        and event.payload["decision"] == Decision.BLOCK.value
    ]
    assert len(blocking) == 1
    assert denied.call.policy_decision_id == blocking[0].payload["id"]


def test_a_human_rejection_never_outranks_a_never_decision(tmp_path: Path) -> None:
    """The ordering is enforced, not incidental: no responder is read before the engine."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=False)
    never = replace(context, gate=Gate(PolicyEngine(_NEVER_POLICY), context.event_log))

    denied = manager.execute(never, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert _denials(context)[-1].payload["ticket_outcome"] == TicketOutcome.UNAVAILABLE.value
    assert tool.seen == []


@pytest.mark.parametrize(
    ("requested_depth", "calling_depth"), [(0, 1), (1, 0)], ids=["deeper", "shallower"]
)
def test_a_credit_is_spendable_only_at_the_delegation_depth_it_was_raised_at(
    tmp_path: Path, requested_depth: int, calling_depth: int
) -> None:
    """F5.4: a delegated scope cannot widen itself, in either direction."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock, delegation_depth=requested_depth)
    first_decision = _reviewed_and_approved(context, manager)
    other = replace(context, delegation_depth=calling_depth)

    denied = manager.execute(other, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []
    assert denied.pending_approval is not None
    assert denied.pending_approval.id != first_decision


@pytest.mark.parametrize(
    "mutation",
    [
        {"run_id": "run_" + "f" * 32},
        {"tool_intent_sha256": "b" * 64},
        {"expires_at": datetime(2031, 1, 1, tzinfo=UTC)},
        {"delegation_depth": 3},
    ],
    ids=["run", "ticket", "deadline", "depth"],
)
def test_the_recorded_scope_digest_covers_every_scope_field(mutation: dict[str, Any]) -> None:
    """Every field the scope binds is inside the digest, so none of them can be swapped."""
    scope = ApprovalScope(
        run_id="run_" + "a" * 32,
        tool_intent_sha256="c" * 64,
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        delegation_depth=0,
    )

    assert replace(scope, **mutation).digest() != scope.digest()
    assert scope.to_payload()[APPROVAL_SCOPE_DIGEST_KEY] == scope.digest()


def test_an_allowed_execution_records_the_scope_it_spent(tmp_path: Path) -> None:
    """G.3: the closure facts are durable evidence before any caller can report success."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock, delegation_depth=2)
    _reviewed_and_approved(context, manager)

    allowed = manager.execute(context, "echo", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen) == 1
    started = _starts(context)[-1]
    request = _requests(context)[0]
    assert started.payload[APPROVAL_SCOPE_DIGEST_KEY] == request.payload[APPROVAL_SCOPE_DIGEST_KEY]
    assert started.payload[DELEGATION_DEPTH_KEY] == 2
    # The digest survives the log's redaction of the request payload it was computed from. Every
    # field is read back off the recorded event, so a future redaction rule touching any one of
    # the four -- the depth included -- breaks this recomputation instead of passing silently.
    assert (
        request.payload[APPROVAL_SCOPE_DIGEST_KEY]
        == ApprovalScope(
            run_id=request.run_id,
            tool_intent_sha256=request.payload["tool_intent_sha256"],
            expires_at=datetime.fromisoformat(request.payload[APPROVAL_EXPIRES_AT_KEY]),
            delegation_depth=request.payload[DELEGATION_DEPTH_KEY],
        ).digest()
    )

    again = manager.execute(context, "echo", {"value": "x"})

    assert again.call.status is ToolCallStatus.DENIED
    assert len(tool.seen) == 1
    assert _denials(context)[-1].payload["ticket_outcome"] == TicketOutcome.PENDING.value


@pytest.mark.parametrize(
    "deadline",
    ["whenever", "2099-01-01T00:00:00", 12345],
    ids=["not-a-timestamp", "naive-timestamp", "not-a-string"],
)
def test_a_malformed_recorded_deadline_closes_the_scope(
    tmp_path: Path, deadline: str | int
) -> None:
    """G.2: a deadline that is present and unusable fails closed; an absent one does not.

    Unusable covers naming no instant at all, not only failing to parse: a timestamp without a
    UTC offset cannot be compared with the caller's clock, and the comparison raising a
    ``TypeError`` out of the authorization boundary is not a fail-closed refusal. Once such an
    event is on the append-only log, every later call for that ticket would hit it.
    """
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    decision_id = new_id("decision")
    ticket = tool_intent_sha256("echo", {"value": "x"})
    malformed = log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision_id,
            "tool": "echo",
            "tool_intent_sha256": ticket,
            APPROVAL_EXPIRES_AT_KEY: deadline,
        },
        subject_id=new_id("tool"),
    )
    absent = log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {"decision_id": new_id("decision"), "tool": "echo", "tool_intent_sha256": ticket},
        subject_id=new_id("tool"),
    )
    now = utc_now()

    unusable = RecordedApprovalScope.from_request(malformed)
    legacy = RecordedApprovalScope.from_request(absent)

    assert unusable is not None
    assert legacy is not None
    closed = unusable.closure(log.events(), now=now)
    assert closed is not None
    assert closed.reason is ScopeClosureReason.EXPIRED
    assert legacy.closure(log.events(), now=now) is None
    assert tmp_path.exists()


def test_a_fresh_approval_after_an_expiry_buys_exactly_one_execution(tmp_path: Path) -> None:
    """A closed credit still accounts for the execution it already paid for.

    Dropping a closed answer out of the credit fold turned the start it had bought into an
    unattributed one, and the next human yes went on settling that invented arrears instead of
    buying the call in front of it. Over the whole log the invariant is one human answer, one
    execution -- a closure narrows what a credit may pay for, it never doubles the price.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    _reviewed_and_approved(context, manager)
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED

    clock.advance(context.approval_ttl + timedelta(minutes=1))
    expired = manager.execute(context, "echo", {"value": "x"})
    assert expired.call.status is ToolCallStatus.DENIED
    assert expired.pending_approval is not None
    _human_answer(context, expired.pending_approval.id, approved=True)

    allowed = manager.execute(context, "echo", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen) == 2
    assert len(_answers(context)) == 2
    assert len(_starts(context)) == 2
    assert len(_requests(context)) == 2


def test_a_delegates_own_approval_is_not_spent_by_its_parents_execution(tmp_path: Path) -> None:
    """F5.4 in the allow direction: a delegate approved for itself really may run.

    The depth filter narrows the *credit pool*; the spend fold has to be narrowed with it. While
    it was not, the parent's own legitimate ``tool.started`` charged the delegate's pool, ate the
    yes a human had just given the delegate, and made a second review appear from nowhere.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    root = _context(tmp_path, clock=clock)
    delegate = replace(root, delegation_depth=1)
    _reviewed_and_approved(root, manager)
    assert manager.execute(root, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED

    asked = manager.execute(delegate, "echo", {"value": "x"})
    assert asked.call.status is ToolCallStatus.DENIED
    assert asked.pending_approval is not None
    _human_answer(delegate, asked.pending_approval.id, approved=True)

    allowed = manager.execute(delegate, "echo", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen) == 2
    assert len(_answers(root)) == 2
    assert len(_starts(root)) == 2
    assert len(_requests(root)) == 2
    assert _starts(root)[-1].payload[DELEGATION_DEPTH_KEY] == 1

    again = manager.execute(delegate, "echo", {"value": "x"})

    assert again.call.status is ToolCallStatus.DENIED
    assert len(tool.seen) == 2


def test_a_delegates_execution_does_not_spend_its_parents_credit(tmp_path: Path) -> None:
    """The same narrowing in the other direction: a deeper start charges its own depth only."""
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    root = _context(tmp_path, clock=clock)
    delegate = replace(root, delegation_depth=1)
    _reviewed_and_approved(root, manager)
    asked = manager.execute(delegate, "echo", {"value": "x"})
    assert asked.pending_approval is not None
    _human_answer(delegate, asked.pending_approval.id, approved=True)
    assert manager.execute(delegate, "echo", {"value": "x"}).call.status is (
        ToolCallStatus.COMPLETED
    )

    allowed = manager.execute(root, "echo", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen) == 2
    assert len(_answers(root)) == 2
    assert len(_requests(root)) == 2
    assert [start.payload[DELEGATION_DEPTH_KEY] for start in _starts(root)] == [1, 0]


@pytest.mark.parametrize("closure", ["released", "expired"])
def test_a_closure_that_leaves_the_run_alive_is_still_recorded_on_the_denial(
    tmp_path: Path, closure: str
) -> None:
    """G.3: every closure reason is on the chain, not only the one that kills the Run.

    A denial recorded as ``pending`` is indistinguishable from "nobody has answered yet", so an
    auditor reading the log could not tell a credit that ran out of scope from a review still
    waiting for a human. The Run surviving changes what happens next -- a fresh review is raised
    -- not what the evidence says happened.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    _reviewed_and_approved(context, manager)
    if closure == "released":
        _core_transition(context, "begin_audit")
    else:
        clock.advance(context.approval_ttl + timedelta(minutes=1))

    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    # The Run lives, so a fresh review really is raised for the call to ask again.
    assert denied.pending_approval is not None
    assert len(_requests(context)) == 2
    recorded = _denials(context)[-1].payload
    assert recorded["ticket_outcome"] == TicketOutcome.CANCELLED.value
    assert (
        recorded["closed_scope_sha256"] == _requests(context)[0].payload[APPROVAL_SCOPE_DIGEST_KEY]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "cancel"},
        {"command": "cancel", "from_version": 2, "to_version": 3},
        {**transition_payload("cancel"), "stage": "planning"},
        {**transition_payload("cancel"), "from_version": 1},
        {**transition_payload("cancel"), "state": {"stage": "executing", "condition": "active"}},
    ],
    ids=["bare", "versions-only", "flat-disagrees", "version-gap", "state-disagrees"],
)
def test_a_transition_that_disagrees_with_itself_closes_nothing(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    """A forged transition is an availability attack, and the envelope alone does not stop it.

    ``producer`` is a free argument on ``EventLog.append``, so "system actor, ``thymira.core``,
    subject is the Run" is a claim anyone able to append can make. A closure is therefore believed
    only from a payload that agrees with itself -- the same standard MIRA's own control already
    applies to the same evidence class, so the two readers cannot disagree about whether the Run
    ended.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(utc_now())
    context = _context(tmp_path, clock=clock)
    _reviewed_and_approved(context, manager)
    context.event_log.append(
        EventType.RUN_TRANSITIONED,
        Actor.system(),
        payload,
        subject_id=context.run_id,
        producer="thymira.core",
        producer_version="0.1",
    )

    allowed = manager.execute(context, "echo", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen) == 1
    assert len(_requests(context)) == 1


def test_a_caller_whose_clock_names_no_instant_is_denied_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """``ToolContext.now`` is injectable, so a naive clock is a caller's mistake, not an attack.

    It still must not become a ``TypeError`` escaping ``ToolManager.execute``: the deadline such a
    clock mints names no instant, so the scope it records is unusable evidence and the credit is
    closed. The Run lives, so the call asks again.
    """
    tool = _FakeTool("echo", _CAPABILITY)
    manager = ToolManager(ToolRegistry((tool,)))
    clock = _Clock(datetime(2026, 1, 1))  # noqa: DTZ001  # the naive clock is the subject
    context = _context(tmp_path, clock=clock)
    _reviewed_and_approved(context, manager)

    denied = manager.execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert tool.seen == []
    assert _starts(context) == []
    assert denied.pending_approval is not None
    assert _denials(context)[-1].payload["ticket_outcome"] == TicketOutcome.CANCELLED.value
