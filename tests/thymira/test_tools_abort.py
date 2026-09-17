"""Canonical outcomes for deferred tool calls cancelled before dispatch."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import FakeTool, review_gated_context
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentRunner,
    AgentSpec,
    LLMToolCall,
    ScriptedProvider,
)
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.resume import (
    ResumeStatus,
    abort_pending_resume,
    build_execution_identity,
    load_pending_resume,
    save_pending_resume,
)
from thymira.policies import ToolCapability
from thymira.schemas import (
    Actor,
    ActorKind,
    EventType,
    ModelRoutePolicy,
    Task,
    TaskStatus,
    new_id,
)
from thymira.tools import (
    ToolFailureValue,
    ToolManager,
    ToolRegistry,
    ToolResult,
    ToolResultCode,
    reconstruct_value,
)

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.agents.resume import ParkedExecutionIdentity
    from thymira.tools import ToolContext, ToolExecution

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _park_real_denial(
    context: ToolContext,
    agent_id: str,
    execution: ToolExecution,
    provider_tool_id: str,
    *,
    step_key: str = "b" * 64,
) -> tuple[ParkedExecutionIdentity, dict[str, dict[str, str]]]:
    """Record the model-selection and park evidence a real THY step leaves for its own denial.

    ``execution`` must already carry a ``pending_approval`` -- the real ``tool.denied`` and
    ``human.approval_requested`` events the manager wrote for it supply the ticket and sandbox
    mode this identity binds to, exactly as the production tool bridge reads them. ``context``
    must already carry the same ``task_id`` the call was recorded under -- the identity is bound
    to it and `_validate_identity_evidence` rejects any other.
    """
    assert execution.pending_approval is not None
    assert context.task_id is not None
    task_id = context.task_id
    choice = ModelChoice(
        role=Role.AGENT,
        task="analyze",
        tier_requested=ModelTier.STANDARD,
        tier_applied=ModelTier.STANDARD,
        model="test-model",
        reason="test fixture",
    )
    context.event_log.append(EventType.MODEL_SELECTED, Actor.system(), choice.event_payload())
    denial_payload = next(
        event.payload
        for event in context.event_log.events()
        if event.type is EventType.TOOL_DENIED
        and event.payload.get("tool_call_id") == execution.call.id
    )
    tool_call_metadata = {
        provider_tool_id: {
            "decision_id": execution.pending_approval.id,
            "tool_call_id": execution.call.id,
            "tool_intent_sha256": denial_payload["tool_intent_sha256"],
            "sandbox_mode": denial_payload["sandbox_mode"],
        }
    }
    identity = build_execution_identity(
        model_choice=choice,
        agent_id=agent_id,
        task_id=task_id,
        step_key=step_key,
        tool_call_metadata=tool_call_metadata,
    )
    context.event_log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            "task_id": task_id,
            "objective": "use the tool",
            "status": "PENDING",
            "step_key": identity.step_key,
            "execution_identity_sha256": identity.digest(),
        },
        subject_id=agent_id,
    )
    return identity, tool_call_metadata


def test_pending_manager_call_is_closed_once_as_canonical_aborted_result(tmp_path: Path) -> None:
    """A pending manager call gets one durable result without invoking the producer."""
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    tool = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {})

    aborted = ToolManager(ToolRegistry((tool,))).abort_pending(
        context,
        execution.call.id,
        actor=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        reason="caller cancelled the turn",
    )

    assert aborted is not None
    assert aborted.result.success is False
    assert aborted.result.code is ToolResultCode.ABORTED_BEFORE_DISPATCH
    assert aborted.call.run_id == run_id
    assert isinstance(aborted.result.value, ToolFailureValue)
    assert aborted.result.value.aborted is True
    assert tool.seen_invocation == []
    completed = [
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    ]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["tool_call_id"] == execution.call.id
    assert payload["run_id"] == run_id
    assert payload["result_code"] == ToolResultCode.ABORTED_BEFORE_DISPATCH
    assert payload["aborted_before_dispatch"] is True
    assert payload["dispatched"] is False
    assert payload["ticket_outcome"] == "cancelled"
    assert payload["initiator"]["id"] == "reviewer"
    assert reconstruct_value(tool, payload["value"]) == aborted.result.value

    assert ToolManager(ToolRegistry((tool,))).abort_pending(context, execution.call.id) is None
    assert (
        len(
            [
                event
                for event in context.event_log.events()
                if event.type is EventType.TOOL_COMPLETED
            ]
        )
        == 1
    )
    assert context.event_log.verify().valid


def test_unknown_call_is_not_fabricated_into_an_aborted_result(tmp_path: Path) -> None:
    """Cancellation with no matching pending call leaves the event log untouched."""
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    manager = ToolManager(
        ToolRegistry((FakeTool("echo", ToolCapability(id="echo", external_effects=())),))
    )

    assert manager.abort_pending(context, new_id("tool")) is None
    assert context.event_log.events() == []


@pytest.mark.parametrize("identity_field", ["run_id", "agent_id", "task_id"])
def test_foreign_context_cannot_close_the_pending_call(tmp_path: Path, identity_field: str) -> None:
    """A context with a different run, agent or task identity cannot spend this call's ticket."""
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    tool = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    manager = ToolManager(ToolRegistry((tool,)))
    execution = manager.execute(context, "echo", {})

    if identity_field == "run_id":
        foreign = replace(context, run_id=new_id("run"))
    elif identity_field == "agent_id":
        foreign = replace(context, agent_id=new_id("agent"))
    else:
        foreign = replace(context, task_id=new_id("task"))

    assert manager.abort_pending(foreign, execution.call.id) is None
    assert not any(event.type is EventType.TOOL_COMPLETED for event in context.event_log.events())


def test_forged_ticket_cannot_close_a_pending_call(tmp_path: Path) -> None:
    """A well-shaped denial with a copied or malformed ticket is refused before any append."""
    for index, ticket in enumerate(("not-a-sha256", "0" * 64)):
        run_id = new_id("run")
        context = review_gated_context(tmp_path / str(index), run_id=run_id)
        tool = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
        manager = ToolManager(ToolRegistry((tool,)))
        tool_call_id = new_id("tool")
        context.event_log.append(
            EventType.TOOL_DENIED,
            Actor(kind=ActorKind.TOOL, id="echo", authenticated=True),
            {
                "tool_call_id": tool_call_id,
                "run_id": run_id,
                "agent_id": context.agent_id,
                "task_id": context.task_id,
                "decision_id": None,
                "tool": "echo",
                "ticket_outcome": "pending",
                "arguments": {},
                "tool_intent_sha256": ticket,
                "sandbox_mode": None,
            },
            subject_id=tool_call_id,
        )

        assert manager.abort_pending(context, tool_call_id) is None
        assert not any(
            event.type is EventType.TOOL_COMPLETED for event in context.event_log.events()
        )
        assert context.event_log.verify().valid


def test_agent_deferred_call_can_be_cancelled_without_a_second_provider_turn(
    tmp_path: Path,
) -> None:
    """The real bridge, manager and resume bundle agree on one cancelled call identity."""
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    echo = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    spec = AgentSpec(
        name="coding",
        role=Role.AGENT,
        task_kinds=("code",),
        tool_allowlist=("echo",),
        max_turns=2,
        max_depth=1,
        system_prompt_ref="p",
        output_schema_ref="s",
    )
    catalog = AgentCatalog(
        (spec,),
        system_prompts={spec.name: "You are a coding agent."},
        output_schemas={spec.name: DataProfileOutput},
    )
    provider = ScriptedProvider(
        [LLMToolCall(id="provider-call-1", name="echo", arguments={"value": "x"})]
    )
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="use echo")
    agent_context = replace(context, agent_id=task.agent_id, task_id=task.id)

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=agent_context.event_log,
            provider=provider,
            tool_registry=ToolRegistry((echo,)),
            tool_context=agent_context,
            artifact_store=agent_context.artifact_store,
            route_policy=TEST_ROUTE_POLICY,
        ),
    )

    assert result.task_status is TaskStatus.PENDING
    assert len(provider.calls) == 1
    assert echo.seen_invocation == []
    bundle = context.artifact_store.load_json(f"resume/{task.agent_id}.json")
    metadata = bundle["tool_call_metadata"]
    runtime_tool_id = next(iter(metadata.values()))["tool_call_id"]
    aborted = ToolManager(ToolRegistry((echo,))).abort_pending(
        agent_context,
        runtime_tool_id,
        actor=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
    )

    assert aborted is not None
    assert abort_pending_resume(
        context.artifact_store,
        agent_id=task.agent_id,
        event_log=context.event_log,
        tool_results={runtime_tool_id: aborted.result},
    ) == (runtime_tool_id,)
    assert len(provider.calls) == 1
    assert echo.seen_invocation == []
    assert load_pending_resume(context.artifact_store, context, task.agent_id).status is (
        ResumeStatus.CANCELLED
    )


def test_cancelled_resume_bundle_contains_the_matching_provider_return(tmp_path: Path) -> None:
    """Resume persistence pairs the canonical manager result with PydanticAI's call id."""
    run_id = new_id("run")
    context = replace(review_gated_context(tmp_path, run_id=run_id), task_id=new_id("task"))
    tool = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    manager = ToolManager(ToolRegistry((tool,)))
    execution = manager.execute(context, "echo", {})
    assert execution.pending_approval is not None
    agent_id = context.agent_id
    runtime_tool_id = execution.call.id
    provider_tool_id = "provider-call-1"
    messages = [
        ModelRequest(parts=[UserPromptPart(content="use the tool")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="echo",
                    args={"value": "x"},
                    tool_call_id=provider_tool_id,
                )
            ]
        ),
    ]
    identity, tool_call_metadata = _park_real_denial(context, agent_id, execution, provider_tool_id)
    save_pending_resume(
        context.artifact_store,
        agent_id=agent_id,
        identity=identity,
        messages=messages,
        tool_call_metadata=tool_call_metadata,
    )
    aborted = manager.abort_pending(
        context,
        runtime_tool_id,
        actor=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        reason="caller cancelled the turn",
    )
    assert aborted is not None
    failure_value = aborted.result.value
    assert isinstance(failure_value, ToolFailureValue)

    paired = abort_pending_resume(
        context.artifact_store,
        agent_id=agent_id,
        event_log=context.event_log,
        tool_results={runtime_tool_id: aborted.result},
    )

    assert paired == (runtime_tool_id,)
    bundle = context.artifact_store.load_json(f"resume/{agent_id}.json")
    replayed = ModelMessagesTypeAdapter.validate_python(bundle["messages"])
    returns = [
        part for message in replayed for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    assert len(returns) == 1
    assert returns[0].tool_call_id == provider_tool_id
    assert returns[0].tool_name == "echo"
    assert returns[0].outcome == "interrupted"
    assert returns[0].content == {
        "kind": "failure",
        "value": failure_value.model_dump(mode="json"),
    }
    assert load_pending_resume(context.artifact_store, context, agent_id).status is (
        ResumeStatus.CANCELLED
    )
    # A caller with no `ToolContext` gets the conservative status, never a diagnosis that would
    # require the identity-evidence checks `load_pending_resume` needs a context to run.
    assert load_pending_resume(context.artifact_store, None, agent_id).status is (
        ResumeStatus.NOT_AVAILABLE
    )
    assert (
        abort_pending_resume(
            context.artifact_store,
            agent_id=agent_id,
            event_log=context.event_log,
            tool_results={runtime_tool_id: aborted.result},
        )
        == ()
    )


def test_caller_constructed_result_without_manager_event_cannot_cancel_bundle(
    tmp_path: Path,
) -> None:
    """A structured result without a durable manager completion cannot close the bundle."""
    run_id = new_id("run")
    context = review_gated_context(tmp_path, run_id=run_id)
    agent_id = new_id("agent")
    tool_call_metadata = {
        "provider-call-1": {
            "decision_id": new_id("decision"),
            "tool_call_id": "tool-real",
            "tool_intent_sha256": "0" * 64,
            "sandbox_mode": None,
        }
    }
    choice = ModelChoice(
        role=Role.AGENT,
        task="analyze",
        tier_requested=ModelTier.STANDARD,
        tier_applied=ModelTier.STANDARD,
        model="test-model",
        reason="test fixture",
    )
    identity = build_execution_identity(
        model_choice=choice,
        agent_id=agent_id,
        task_id="task-1",
        step_key="b" * 64,
        tool_call_metadata=tool_call_metadata,
    )
    save_pending_resume(
        context.artifact_store,
        agent_id=agent_id,
        identity=identity,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="use the tool")]),
            ModelResponse(
                parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="provider-call-1")]
            ),
        ],
        tool_call_metadata=tool_call_metadata,
    )
    result = ToolResult(
        success=False,
        value=ToolFailureValue(
            text="cancelled", error="cancelled", code=ToolResultCode.ABORTED_BEFORE_DISPATCH
        ),
        code=ToolResultCode.ABORTED_BEFORE_DISPATCH,
        aborted=True,
    )

    assert (
        abort_pending_resume(
            context.artifact_store,
            agent_id=agent_id,
            event_log=context.event_log,
            tool_results={"tool-other": result},
        )
        == ()
    )
    # No `agent.parked`/`model.selected`/`tool.denied` evidence backs this bundle at all --
    # `load_pending_resume` must fail closed to INVALID rather than assume it is merely WAITING
    # for an answer it cannot even verify was ever requested.
    lookup = load_pending_resume(context.artifact_store, context, agent_id)
    assert lookup.status is ResumeStatus.INVALID


def test_failed_resume_persistence_does_not_close_or_resume_the_provider_turn(
    tmp_path: Path,
) -> None:
    """A transcript write failure leaves the parked call waiting, never falsely resumable."""
    run_id = new_id("run")
    context = replace(review_gated_context(tmp_path, run_id=run_id), task_id=new_id("task"))
    tool = FakeTool("echo", ToolCapability(id="echo", external_effects=()))
    manager = ToolManager(ToolRegistry((tool,)))
    execution = manager.execute(context, "echo", {})
    assert execution.pending_approval is not None
    agent_id = context.agent_id
    runtime_tool_id = execution.call.id
    identity, tool_call_metadata = _park_real_denial(
        context, agent_id, execution, "provider-call-1"
    )
    save_pending_resume(
        context.artifact_store,
        agent_id=agent_id,
        identity=identity,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="use the tool")]),
            ModelResponse(
                parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="provider-call-1")]
            ),
        ],
        tool_call_metadata=tool_call_metadata,
    )
    aborted = manager.abort_pending(
        context,
        runtime_tool_id,
        actor=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
    )
    assert aborted is not None

    with patch.object(context.artifact_store, "save_json", side_effect=OSError("disk full")):
        assert (
            abort_pending_resume(
                context.artifact_store,
                agent_id=agent_id,
                event_log=context.event_log,
                tool_results={runtime_tool_id: aborted.result},
            )
            == ()
        )

    bundle = context.artifact_store.load_json(f"resume/{agent_id}.json")
    assert bundle.get("cancelled") is None
    assert load_pending_resume(context.artifact_store, context, agent_id).status is (
        ResumeStatus.WAITING
    )
