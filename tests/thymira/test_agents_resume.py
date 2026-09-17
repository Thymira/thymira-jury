"""`thymira.agents.resume`: persisting and reading back a parked step's own conversation.

Verifies the module in isolation -- the readiness branching (`NOT_AVAILABLE`/`WAITING`/`READY`)
and the round trip through a real `ArtifactStore` -- independently of the full `ThyGraph` wiring,
which `test_thy_execute.py` covers end to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from tests.thymira.fixtures_tools import review_gated_context
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.resume import (
    ResumeStatus,
    build_execution_identity,
    load_pending_resume,
    save_pending_resume,
)
from thymira.agents.runner import (
    ResumeHistoryError,
    _compact_resolved_tool_rounds,
    _drop_oldest_resolved_round,
    _first_unresolved_response_index,
    _resume_messages,
)
from thymira.schemas import Actor, ActorKind, EventType, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from thymira.tools import ToolContext


def _human_answer(context: ToolContext, decision_id: str, *, approved: bool) -> None:
    """Record what `Gate.resolve_pending_approval` writes when a real human answers."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def _messages() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content="do it")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="pyd-1")]
        ),
    ]


def _save_parked(
    context: ToolContext,
    agent_id: str,
    tool_call_metadata: dict[str, dict[str, str]],
) -> None:
    """Write a fully evidenced parked identity for the persistence tests."""
    choice = ModelChoice(
        role=Role.AGENT,
        task="analyze",
        tier_requested=ModelTier.STANDARD,
        tier_applied=ModelTier.STANDARD,
        model="test-model",
        reason="test fixture",
    )
    context.event_log.append(EventType.MODEL_SELECTED, Actor.system(), choice.event_payload())
    for metadata in tool_call_metadata.values():
        context.event_log.append(
            EventType.TOOL_DENIED,
            Actor.system(),
            {
                "tool_call_id": metadata["tool_call_id"],
                "task_id": "task-1",
                "decision_id": metadata["decision_id"],
                "tool_intent_sha256": metadata["tool_intent_sha256"],
                "sandbox_mode": metadata.get("sandbox_mode"),
            },
        )
        context.event_log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            {
                "decision_id": metadata["decision_id"],
                "tool_intent_sha256": metadata["tool_intent_sha256"],
                "sandbox_mode": metadata.get("sandbox_mode"),
            },
        )
    identity = build_execution_identity(
        model_choice=choice,
        agent_id=agent_id,
        task_id="task-1",
        step_key="b" * 64,
        tool_call_metadata=tool_call_metadata,
    )
    context.event_log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            "task_id": "task-1",
            "objective": "do it",
            "status": "PENDING",
            "step_key": identity.step_key,
            "execution_identity_sha256": identity.digest(),
        },
        subject_id=agent_id,
    )
    save_pending_resume(
        context.artifact_store,
        agent_id=agent_id,
        identity=identity,
        messages=_messages(),
        tool_call_metadata=tool_call_metadata,
    )


def test_resume_history_rejects_ambiguous_user_context_without_dropping_followups() -> None:
    """A history with multiple user parts is rejected instead of silently losing one."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="original")]),
        ModelResponse(parts=[ToolCallPart(tool_name="echo", args={"value": "x"})]),
        ModelRequest(parts=[UserPromptPart(content="legitimate follow-up")]),
    ]

    with pytest.raises(ResumeHistoryError, match="exactly one"):
        _resume_messages(messages, "checkpoint")


def test_resume_history_rejects_a_shape_without_a_replaceable_user_part() -> None:
    """A checkpoint is never omitted when a parked shape cannot carry it."""
    messages = [ModelResponse(parts=[ToolCallPart(tool_name="echo", args={"value": "x"})])]

    with pytest.raises(ResumeHistoryError, match="exactly one"):
        _resume_messages(messages, "checkpoint")


def test_resume_history_rejects_a_deferred_turn_split_between_resolved_and_pending() -> None:
    """One turn's tool calls must end up all-returned or all-pending, never a mix.

    Reproduced against a real Run's event log (run_5d7e0d643f634d9c97471f262ceaa0b8): a turn
    deferred two reads together, both were approved and re-executed, but only one's
    `ToolReturnPart` reached the persisted conversation. The runtime then re-asked the provider
    to resolve the dangling call while the other's already-spent ticket stayed spent, and a later,
    unrelated resume replayed it a second time under that stale decision (MIRA's A3).
    """
    messages = [
        ModelRequest(parts=[UserPromptPart(content="do it")]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="read_file", args={"path": "a.csv"}, tool_call_id="pyd-1"),
                ToolCallPart(tool_name="read_file", args={"path": "b.csv"}, tool_call_id="pyd-2"),
            ]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="read_file", content="a", tool_call_id="pyd-1")]
        ),
    ]

    with pytest.raises(ResumeHistoryError, match="pyd-2"):
        _resume_messages(messages, "checkpoint")


def test_resume_history_accepts_a_fully_resolved_deferred_turn() -> None:
    """A turn whose every deferred call was returned is an ordinary, acceptable history."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="do it")]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="read_file", args={"path": "a.csv"}, tool_call_id="pyd-1"),
                ToolCallPart(tool_name="read_file", args={"path": "b.csv"}, tool_call_id="pyd-2"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="read_file", content="a", tool_call_id="pyd-1"),
                ToolReturnPart(tool_name="read_file", content="b", tool_call_id="pyd-2"),
            ]
        ),
    ]

    resumed = _resume_messages(messages, "checkpoint")

    assert isinstance(resumed[0], ModelRequest)
    assert isinstance(resumed[0].parts[0], UserPromptPart)
    assert resumed[0].parts[0].content == "checkpoint"


def test_resume_history_accepts_a_fully_pending_deferred_turn() -> None:
    """A turn whose deferred calls are still entirely unresolved is the ordinary parked shape."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="do it")]),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="read_file", args={"path": "a.csv"}, tool_call_id="pyd-1"),
                ToolCallPart(tool_name="read_file", args={"path": "b.csv"}, tool_call_id="pyd-2"),
            ]
        ),
    ]

    resumed = _resume_messages(messages, "checkpoint")

    assert isinstance(resumed[0], ModelRequest)
    assert isinstance(resumed[0].parts[0], UserPromptPart)
    assert resumed[0].parts[0].content == "checkpoint"


def test_not_available_without_a_store_or_a_tool_context(tmp_path: Path) -> None:
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")

    assert load_pending_resume(None, context, agent_id).status is ResumeStatus.NOT_AVAILABLE
    assert (
        load_pending_resume(context.artifact_store, None, agent_id).status
        is ResumeStatus.NOT_AVAILABLE
    )


def test_not_available_when_no_bundle_was_ever_written(tmp_path: Path) -> None:
    context = review_gated_context(tmp_path, run_id=new_id("run"))

    lookup = load_pending_resume(context.artifact_store, context, new_id("agent"))

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None


def test_waits_until_the_decision_is_answered_then_becomes_ready(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    agent_id = new_id("agent")
    decision_id = new_id("decision")
    metadata = {
        "pyd-1": {
            "decision_id": decision_id,
            "tool_call_id": "tool-1",
            "tool_intent_sha256": "a" * 64,
        }
    }
    _save_parked(
        context,
        agent_id,
        metadata,
    )

    still_waiting = load_pending_resume(context.artifact_store, context, agent_id)
    assert still_waiting.status is ResumeStatus.WAITING
    assert still_waiting.resume is None

    _human_answer(context, decision_id, approved=True)
    ready = load_pending_resume(context.artifact_store, context, agent_id)

    assert ready.status is ResumeStatus.READY
    assert ready.resume is not None
    assert ready.resume.deferred_tool_results.approvals == {"pyd-1": True}
    assert len(ready.resume.messages) == 2
    replayed_call = ready.resume.messages[1].parts[0]
    assert isinstance(replayed_call, ToolCallPart)
    assert replayed_call.tool_name == "echo"
    assert replayed_call.args == {"value": "x"}


def test_a_denial_still_becomes_ready_the_tool_manager_decides_allow_or_deny_not_this_lookup(
    tmp_path: Path,
) -> None:
    """Confirm `approvals` always says "approved" regardless of the human's real verdict.

    It only unblocks PydanticAI into calling the tool function again. `ToolManager.execute`
    inside that function is what still decides allow vs. deny -- exactly as for a call that was
    never deferred (see the Agent Note).
    """
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    agent_id = new_id("agent")
    decision_id = new_id("decision")
    metadata = {
        "pyd-1": {
            "decision_id": decision_id,
            "tool_call_id": "tool-1",
            "tool_intent_sha256": "a" * 64,
        }
    }
    _save_parked(
        context,
        agent_id,
        metadata,
    )

    _human_answer(context, decision_id, approved=False)
    ready = load_pending_resume(context.artifact_store, context, agent_id)

    assert ready.status is ResumeStatus.READY
    assert ready.resume is not None
    assert ready.resume.deferred_tool_results.approvals == {"pyd-1": True}


def test_waits_for_every_decision_a_multi_call_turn_deferred_not_just_the_first(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    agent_id = new_id("agent")
    first_decision, second_decision = new_id("decision"), new_id("decision")
    metadata = {
        "pyd-1": {
            "decision_id": first_decision,
            "tool_call_id": "tool-1",
            "tool_intent_sha256": "a" * 64,
        },
        "pyd-2": {
            "decision_id": second_decision,
            "tool_call_id": "tool-2",
            "tool_intent_sha256": "b" * 64,
        },
    }
    _save_parked(
        context,
        agent_id,
        metadata,
    )

    _human_answer(context, first_decision, approved=True)
    assert load_pending_resume(context.artifact_store, context, agent_id).status is (
        ResumeStatus.WAITING
    )

    _human_answer(context, second_decision, approved=True)
    lookup = load_pending_resume(context.artifact_store, context, agent_id)

    assert lookup.status is ResumeStatus.READY
    assert lookup.resume is not None
    assert lookup.resume.deferred_tool_results.approvals == {"pyd-1": True, "pyd-2": True}


# ------------------------------------------- resume-history compaction (vector 2, THY-26)


def _resolved_round(call_id: str, content: str = "ok") -> tuple[ModelResponse, ModelRequest]:
    """One fully-resolved (call, return) round-trip pair for the tests below."""
    return (
        ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id=call_id)]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="echo", content=content, tool_call_id=call_id)]
        ),
    )


def test_first_unresolved_response_index_finds_the_still_open_round() -> None:
    call_a, return_a = _resolved_round("a")
    pending = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="b")])
    messages = [ModelRequest(parts=[UserPromptPart(content="go")]), call_a, return_a, pending]

    assert _first_unresolved_response_index(messages) == 3


def test_first_unresolved_response_index_protects_everything_when_all_resolved() -> None:
    call_a, return_a = _resolved_round("a")
    messages = [ModelRequest(parts=[UserPromptPart(content="go")]), call_a, return_a]

    assert _first_unresolved_response_index(messages) == 0


def test_drop_oldest_resolved_round_removes_the_pair_and_only_the_pair() -> None:
    call_a, return_a = _resolved_round("a")
    pending = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="b")])
    prompt = ModelRequest(parts=[UserPromptPart(content="go")])
    messages = [prompt, call_a, return_a, pending]

    shrunk = _drop_oldest_resolved_round(messages, protected_from=3)

    assert shrunk == [prompt, pending]


def test_drop_oldest_resolved_round_never_crosses_the_protected_boundary() -> None:
    """A resolved round at or after `protected_from` is never touched."""
    call_a, return_a = _resolved_round("a")
    messages = [ModelRequest(parts=[UserPromptPart(content="go")]), call_a, return_a]

    # protected_from=1 means the round starting at index 1 is the still-open one -- nothing here
    # is eligible even though it is, in isolation, a fully-resolved pair.
    assert _drop_oldest_resolved_round(messages, protected_from=1) is None


def test_drop_oldest_resolved_round_skips_a_request_that_mixes_other_parts() -> None:
    """A `ModelRequest` carrying the return plus something else is never removed whole."""
    prompt = ModelRequest(parts=[UserPromptPart(content="go")])
    call_a = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="a")])
    mixed_return = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="echo", content="ok", tool_call_id="a"),
            UserPromptPart(content="also here"),
        ]
    )
    pending = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="b")])
    messages = [prompt, call_a, mixed_return, pending]

    assert _drop_oldest_resolved_round(messages, protected_from=3) is None


def test_compact_resolved_tool_rounds_stops_as_soon_as_it_fits() -> None:
    """Two droppable rounds; only the first is removed once `fits` reports success."""
    call_a, return_a = _resolved_round("a")
    call_b, return_b = _resolved_round("b")
    pending = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="c")])
    prompt = ModelRequest(parts=[UserPromptPart(content="go")])
    messages = [prompt, call_a, return_a, call_b, return_b, pending]
    seen_lengths: list[int] = []

    def fits(candidate: Sequence[ModelRequest | ModelResponse]) -> bool:
        seen_lengths.append(len(candidate))
        return len(candidate) <= 4

    compacted, dropped = _compact_resolved_tool_rounds(messages, fits=fits)

    assert dropped == 1
    assert compacted == [prompt, call_b, return_b, pending]


def test_compact_resolved_tool_rounds_gives_up_when_nothing_more_is_eligible() -> None:
    """A conversation with no droppable round is returned unchanged, `dropped == 0`."""
    pending = ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="a")])
    messages = [ModelRequest(parts=[UserPromptPart(content="go")]), pending]

    compacted, dropped = _compact_resolved_tool_rounds(messages, fits=lambda _candidate: False)

    assert dropped == 0
    assert compacted == messages
