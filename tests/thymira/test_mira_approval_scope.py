"""A3 independently refuses a start that spends an approval whose scope had closed.

The Tool Manager refuses such a call itself; this file proves the refusal is *verifiable from the
log*, by a reader that calls neither the manager nor Core. Every node forges the exact
``tool.started`` the manager declined to write and asks control A3 whether it was authorized. The
facts A3 relates come from three different writers: ``RunController`` writes the
``run.transitioned``, the Gate writes the ``human.approval_requested`` and its scope, and the Tool
Manager writes the ``tool.started``.

Real: Gate, PolicyEngine, ToolManager, RunController, LocalRunStore, LocalApprovalService. The
registered tool is an effect-free fake.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest

from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from thymira.core import RunController, RunEventLog
from thymira.core.run_state import RunTransitionKind
from thymira.events import InMemoryEventLog, canonical_json, sha256_text, verify_events
from thymira.mira.checks import AuditContext, ControlStatus, controls
from thymira.policies import (
    APPROVAL_SCOPE_DIGEST_KEY,
    DELEGATION_DEPTH_KEY,
    CapabilityRule,
    LocalApprovalService,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
)
from thymira.schemas import Actor, Decision, EventType, Run, new_id
from thymira.state import LocalRunStore
from thymira.tools import ToolContext, ToolManager, ToolRegistry, tool_intent_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from thymira.schemas import Event


_HUMAN = Actor(kind="human", id="reviewer", authenticated=True)
_RISK = RiskProfile(risk_level="medium", activity_category="analysis", confidence=1)
_PROBE = ToolCapability(id="echo", risk_tags=("probe",), external_effects=())
_EFFECT = {"value": "exact-effect"}
_TICKET = tool_intent_sha256("echo", _EFFECT)
_POLICY = Policy(
    name="mira-approval-scope",
    version="1.0",
    default_decision=Decision.PASS,
    capability_default_decision=Decision.PASS,
    capability_rules=(
        CapabilityRule(
            id="PROBE",
            risk_tags=("probe",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Review this exact probe.",
        ),
    ),
)


@dataclass(frozen=True)
class _ScopeRuntime:
    context: ToolContext
    manager: ToolManager
    controller: RunController


@pytest.fixture
def scope_runtime(tmp_path: Path) -> _ScopeRuntime:
    """Real review evidence over a real Run store, so Core writes its own transitions."""
    run = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="Verify approval scopes.",
    )
    store = LocalRunStore(tmp_path / "runs")
    store.create(run, actor=Actor.system())
    log = RunEventLog(store, run.id)
    context = review_gated_context(tmp_path, run_id=run.id, log=log, engine=PolicyEngine(_POLICY))
    tool = FakeTool("echo", _PROBE, arguments_model=EchoArguments)
    return _ScopeRuntime(
        replace(context, risk_profile=_RISK),
        ToolManager(ToolRegistry((tool,))),
        RunController(store),
    )


def _approve(context: ToolContext, decision_id: str) -> None:
    LocalApprovalService(context.event_log).resolve(
        context.run_id, decision_id, approved=True, by=_HUMAN
    )


def _assert_a3(events: Sequence[Event], expected: ControlStatus) -> None:
    assert verify_events(events).valid
    result = controls.a3_authorization_before_tool(AuditContext(events[0].run_id, events))
    assert result[0] is expected, result


def _request_for(context: ToolContext, decision_id: str) -> Event:
    return next(
        event
        for event in context.event_log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.payload.get("decision_id") == decision_id
    )


def _forge_start(context: ToolContext, decision_id: str, **payload: Any) -> None:
    """Append the exact ``tool.started`` the manager refused to write for this credit."""
    call_id = new_id("tool")
    context.event_log.append(
        EventType.TOOL_STARTED,
        Actor(kind="tool", id="echo"),
        {
            "tool_call_id": call_id,
            "tool": "echo",
            "arguments": _EFFECT,
            "tool_intent_sha256": _TICKET,
            "decision_id": decision_id,
            **payload,
        },
        subject_id=call_id,
    )


def _rechain(context: ToolContext, change: Callable[[Event, dict[str, Any]], None]) -> list[Event]:
    """Keep a valid chain while replacing only the evidence a semantic check must reject."""
    rebuilt = InMemoryEventLog(context.run_id)
    for event in context.event_log.events():
        payload = dict(event.payload)
        change(event, payload)
        rebuilt.append(
            event.type,
            event.actor,
            payload,
            subject_id=event.subject_id,
            producer=event.producer,
            producer_version=event.producer_version,
        )
    return rebuilt.events()


def _approved_credit(runtime: _ScopeRuntime) -> str:
    """Park the exact call on a review, have a human approve it, and return the decision id."""
    pending = runtime.manager.execute(runtime.context, "echo", _EFFECT)
    assert pending.pending_approval is not None
    _approve(runtime.context, pending.pending_approval.id)
    return pending.pending_approval.id


def test_a3_refuses_a_start_that_spends_a_credit_after_the_run_was_cancelled(
    scope_runtime: _ScopeRuntime,
) -> None:
    """The independent oracle catches the bypass the manager itself refuses to commit.

    Core wrote the cancel; the Gate wrote the request; the forged start claims the credit anyway.
    A3 relates the three and reports FAILED without asking any of their producers.
    """
    context, controller = scope_runtime.context, scope_runtime.controller
    decision_id = _approved_credit(scope_runtime)
    scope = _request_for(context, decision_id).payload
    controller.advance(context.run_id, RunTransitionKind.START)
    controller.advance(context.run_id, RunTransitionKind.CANCEL)

    _forge_start(
        context,
        decision_id,
        **{
            APPROVAL_SCOPE_DIGEST_KEY: scope[APPROVAL_SCOPE_DIGEST_KEY],
            DELEGATION_DEPTH_KEY: scope[DELEGATION_DEPTH_KEY],
        },
    )

    _assert_a3(context.event_log.events(), ControlStatus.FAILED)


def test_a3_accepts_the_same_credit_spent_before_the_cancel(
    scope_runtime: _ScopeRuntime,
) -> None:
    """The refusal is about the closure, not about cancellation appearing anywhere in the log."""
    context, controller = scope_runtime.context, scope_runtime.controller
    decision_id = _approved_credit(scope_runtime)

    assert scope_runtime.manager.execute(context, "echo", _EFFECT).result.success
    controller.advance(context.run_id, RunTransitionKind.START)
    controller.advance(context.run_id, RunTransitionKind.CANCEL)

    assert decision_id in {
        event.payload.get("decision_id")
        for event in context.event_log.events()
        if event.type is EventType.TOOL_STARTED
    }
    _assert_a3(context.event_log.events(), ControlStatus.PASSED)


def test_a3_refuses_a_start_whose_claimed_scope_digest_does_not_recompute(
    scope_runtime: _ScopeRuntime,
) -> None:
    """F11.3 independence: MIRA recomputes the digest from the request's own four fields.

    The request's recorded deadline is moved and its digest left in place, so the value the
    producer wrote no longer describes the fields recorded beside it. A reader that trusted the
    recorded digest would see a coherent credit; a reader that recomputes it sees a forgery, and
    the credit authorises nothing.
    """
    context = scope_runtime.context
    decision_id = _approved_credit(scope_runtime)
    assert scope_runtime.manager.execute(context, "echo", _EFFECT).result.success

    def move_the_deadline(event: Event, payload: dict[str, Any]) -> None:
        if (
            event.type is EventType.HUMAN_APPROVAL_REQUESTED
            and payload.get("decision_id") == decision_id
        ):
            payload["approval_expires_at"] = "2999-01-01T00:00:00+00:00"

    _assert_a3(_rechain(context, move_the_deadline), ControlStatus.FAILED)


@pytest.mark.parametrize("claimed_depth", [1, None], ids=["deeper", "absent"])
def test_a3_refuses_a_start_at_a_deeper_delegation_depth_than_its_request(
    scope_runtime: _ScopeRuntime, claimed_depth: int | None
) -> None:
    """The delegated-widening refusal is verifiable from the log alone, not only in the manager."""
    context = scope_runtime.context
    decision_id = _approved_credit(scope_runtime)
    scope = _request_for(context, decision_id).payload
    assert scope[DELEGATION_DEPTH_KEY] == 0
    depth: dict[str, Any] = {} if claimed_depth is None else {DELEGATION_DEPTH_KEY: claimed_depth}

    _forge_start(
        context,
        decision_id,
        **{APPROVAL_SCOPE_DIGEST_KEY: scope[APPROVAL_SCOPE_DIGEST_KEY], **depth},
    )

    _assert_a3(context.event_log.events(), ControlStatus.FAILED)


def test_a3_still_passes_a_log_that_records_no_approval_scope(
    scope_runtime: _ScopeRuntime,
) -> None:
    """The new checks are additive: no honest history is retroactively condemned.

    Every scope key is stripped from the whole log, which is exactly the shape of a Run recorded
    before this evidence existed. A3 keeps the verdict it gave that history before.
    """
    context = scope_runtime.context
    _approved_credit(scope_runtime)
    assert scope_runtime.manager.execute(context, "echo", _EFFECT).result.success

    def strip_every_scope(event: Event, payload: dict[str, Any]) -> None:
        for key in (APPROVAL_SCOPE_DIGEST_KEY, "approval_expires_at", DELEGATION_DEPTH_KEY):
            payload.pop(key, None)

    _assert_a3(_rechain(context, strip_every_scope), ControlStatus.PASSED)


def test_a3_refuses_a_request_that_records_a_scope_it_cannot_substantiate(
    scope_runtime: _ScopeRuntime,
) -> None:
    """G.2: scope evidence that is present and unreadable fails closed, unlike absent evidence."""
    context = scope_runtime.context
    decision_id = _approved_credit(scope_runtime)
    assert scope_runtime.manager.execute(context, "echo", _EFFECT).result.success

    def break_the_deadline(event: Event, payload: dict[str, Any]) -> None:
        if (
            event.type is EventType.HUMAN_APPROVAL_REQUESTED
            and payload.get("decision_id") == decision_id
        ):
            payload["approval_expires_at"] = "whenever"

    _assert_a3(_rechain(context, break_the_deadline), ControlStatus.FAILED)


def test_a3_refuses_a_request_whose_deadline_names_no_instant(
    scope_runtime: _ScopeRuntime,
) -> None:
    """G.2 in MIRA: a self-consistent scope whose deadline has no UTC offset still fails closed.

    The deadline is replaced *and* the digest recomputed over it, so the recorded scope agrees
    with itself and only the deadline itself is wrong. A reader that compared it with the start's
    timestamp would raise rather than refuse; MIRA cannot substantiate the scope, so the credit
    authorises nothing.
    """
    context = scope_runtime.context
    decision_id = _approved_credit(scope_runtime)
    assert scope_runtime.manager.execute(context, "echo", _EFFECT).result.success
    naive = "2099-01-01T00:00:00"

    def naive_digest(event: Event, payload: dict[str, Any]) -> str:
        return sha256_text(
            canonical_json(
                {
                    "run_id": event.run_id,
                    "tool_intent_sha256": payload["tool_intent_sha256"],
                    "expires_at": naive,
                    "delegation_depth": payload[DELEGATION_DEPTH_KEY],
                }
            )
        )

    def drop_the_offset(event: Event, payload: dict[str, Any]) -> None:
        if (
            event.type is EventType.HUMAN_APPROVAL_REQUESTED
            and payload.get("decision_id") == decision_id
        ):
            payload["approval_expires_at"] = naive
            payload[APPROVAL_SCOPE_DIGEST_KEY] = naive_digest(event, payload)
        elif (
            event.type is EventType.TOOL_STARTED
            and payload.get("decision_id") == decision_id
            and APPROVAL_SCOPE_DIGEST_KEY in payload
        ):
            # The start claims the rewritten scope too, so the digest and depth conjuncts hold
            # and the deadline is the only thing left for A3 to judge.
            payload[APPROVAL_SCOPE_DIGEST_KEY] = naive_digest(event, payload)

    _assert_a3(_rechain(context, drop_the_offset), ControlStatus.FAILED)
