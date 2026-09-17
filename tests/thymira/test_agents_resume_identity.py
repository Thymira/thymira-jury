"""Independent oracles for the immutable identity of a parked agent execution."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai import Agent, DeferredToolResults
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from tests.thymira.test_thy_execute import _data_agent_catalog, _run, _task
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentRunner,
    AgentSpec,
    LLMToolCall,
    ModelRouteDeniedError,
    PendingResume,
)
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.model_binding import routed_model
from thymira.agents.resume import (
    ParkedExecutionIdentity,
    ParkedToolIdentity,
    ResumeStatus,
    build_execution_identity,
    continuation_key,
    load_pending_resume,
    save_pending_resume,
)
from thymira.events import InMemoryEventLog
from thymira.policies import ToolCapability
from thymira.schemas import (
    Actor,
    ActorKind,
    Artifact,
    ArtifactKind,
    EventType,
    ModelRoutePolicy,
    SandboxMode,
    Task,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import ThyState
from thymira.tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _choice() -> ModelChoice:
    """Build the original routed choice used by the persistence oracle."""
    return ModelChoice(
        role=Role.AGENT,
        task="analyze",
        tier_requested=ModelTier.STANDARD,
        tier_applied=ModelTier.STANDARD,
        model="parked-model",
        reason="test fixture",
    )


def _metadata(decision_id: str = "decision-1") -> dict[str, dict[str, str]]:
    """Build metadata with a ticket and the Tool Manager's separate call id."""
    return {
        "pydantic-1": {
            "decision_id": decision_id,
            "tool_call_id": "tool-1",
            "task_id": "task-1",
            "tool_intent_sha256": "a" * 64,
        }
    }


def _messages() -> list[ModelRequest | ModelResponse]:
    """Build one serialized PydanticAI turn containing the deferred call."""
    return [
        ModelRequest(parts=[UserPromptPart(content="profile it")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="pydantic-1")]
        ),
    ]


def _save_evidenced_bundle(context: ToolContext, agent_id: str, *, task_id: str = "task-1") -> None:
    """Persist a bundle and all event evidence a reader needs to verify it independently."""
    choice = _choice()
    metadata = _metadata()
    context.event_log.append(EventType.MODEL_SELECTED, Actor.system(), choice.event_payload())
    context.event_log.append(
        EventType.TOOL_DENIED,
        Actor.system(),
        {
            "tool_call_id": "tool-1",
            "decision_id": "decision-1",
            "tool_intent_sha256": "a" * 64,
            "sandbox_mode": None,
        },
    )
    context.event_log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": "decision-1",
            "tool_intent_sha256": "a" * 64,
            "sandbox_mode": None,
        },
    )
    identity = build_execution_identity(
        model_choice=choice,
        agent_id=agent_id,
        task_id=task_id,
        step_key="b" * 64,
        tool_call_metadata=metadata,
    )
    context.event_log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            "task_id": task_id,
            "objective": "profile it",
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
        tool_call_metadata=metadata,
    )


def test_missing_identity_fails_closed_before_a_resume_can_be_ready(tmp_path: Path) -> None:
    """A legacy transcript without an execution identity cannot authorize a continuation."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")
    _save_evidenced_bundle(context, agent_id)
    bundle = context.artifact_store.load_json(f"resume/{agent_id}.json")
    del bundle["execution_identity"]
    context.artifact_store.save_json(
        f"resume/{agent_id}.json", bundle, produced_by=agent_id, kind=ArtifactKind.OTHER
    )

    lookup = load_pending_resume(context.artifact_store, context, agent_id, task_id="task-1")

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None
    assert "identity" in (lookup.reason or "")


def test_terminal_lifecycle_after_a_park_refuses_resume(tmp_path: Path) -> None:
    """A terminal fact after a park closes the invocation and cannot be replayed."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")
    _save_evidenced_bundle(context, agent_id)
    context.event_log.append(
        EventType.AGENT_COMPLETED,
        Actor.system(),
        {"task_id": "task-1", "status": "FAILED"},
        subject_id=agent_id,
    )
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": "decision-1", "approved": True, "automatic": False},
        subject_id="decision-1",
    )

    lookup = load_pending_resume(context.artifact_store, context, agent_id, task_id="task-1")

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None
    assert "terminal completion" in (lookup.reason or "")


def test_a_later_mismatched_park_cannot_hide_terminal_evidence(tmp_path: Path) -> None:
    """A reader binds terminal checks to the exact parked task and identity digest."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")
    _save_evidenced_bundle(context, agent_id)
    context.event_log.append(
        EventType.AGENT_COMPLETED,
        Actor.system(),
        {"task_id": "task-1", "status": "FAILED"},
        subject_id=agent_id,
    )
    context.event_log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            "task_id": "task-other",
            "objective": "profile it",
            "status": "PENDING",
            "step_key": "b" * 64,
            "execution_identity_sha256": "f" * 64,
        },
        subject_id=agent_id,
    )

    lookup = load_pending_resume(context.artifact_store, context, agent_id, task_id="task-1")

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None
    assert "terminal completion" in (lookup.reason or "")


def test_duplicate_exact_park_checkpoints_are_ambiguous(tmp_path: Path) -> None:
    """A duplicate checkpoint cannot be selected as a resumable identity."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")
    _save_evidenced_bundle(context, agent_id)
    bundle = context.artifact_store.load_json(f"resume/{agent_id}.json")
    identity = ParkedExecutionIdentity.model_validate(bundle["execution_identity"])
    context.event_log.append(
        EventType.AGENT_PARKED,
        Actor.system(),
        {
            "task_id": identity.task_id,
            "objective": "profile it",
            "status": "PENDING",
            "step_key": identity.step_key,
            "execution_identity_sha256": identity.digest(),
        },
        subject_id=agent_id,
    )

    lookup = load_pending_resume(context.artifact_store, context, agent_id, task_id="task-1")

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None
    assert "ambiguous" in (lookup.reason or "")


def test_malformed_identity_fails_closed_even_when_the_transcript_is_intact(tmp_path: Path) -> None:
    """A malformed model choice is rejected before PydanticAI receives the transcript."""
    context = review_gated_context(tmp_path, run_id=new_id("run"))
    agent_id = new_id("agent")
    _save_evidenced_bundle(context, agent_id)
    bundle = context.artifact_store.load_json(f"resume/{agent_id}.json")
    bundle["execution_identity"]["model_choice"]["model"] = ""
    context.artifact_store.save_json(
        f"resume/{agent_id}.json", bundle, produced_by=agent_id, kind=ArtifactKind.OTHER
    )

    lookup = load_pending_resume(context.artifact_store, context, agent_id, task_id="task-1")

    assert lookup.status is ResumeStatus.INVALID
    assert lookup.resume is None


def test_actual_agent_resume_keeps_parked_model_after_router_configuration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graph's resumed PydanticAI calls use the persisted choice, not the changed router."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    context = review_gated_context(tmp_path, run_id=run.id, log=log)
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "parked-model")
    route_policy = ModelRoutePolicy.from_routes(("parked-model",), authority="code-owned-tests")
    parked_graph = build_thy_graph(
        catalog,
        log,
        provider=ScriptedProvider(
            [LLMToolCall(id="c1", name="echo", arguments={"value": "x"})],
            model="provider-before",
        ),
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=route_policy,
    )
    parked = parked_graph.invoke(ThyState(run=run, plan=(_task("t1"),)))
    parked_state = ThyState.model_validate(parked)
    original_selected = [event for event in log.events() if event.type is EventType.MODEL_SELECTED]
    assert original_selected
    assert original_selected[-1].payload["model"] == "parked-model"
    decision_id = next(
        event.payload["decision_id"]
        for event in log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": True, "automatic": False},
        subject_id=decision_id,
    )
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "changed-model")
    resumed_graph = build_thy_graph(
        catalog,
        log,
        provider=ScriptedProvider(
            [DataProfileOutput(row_count=1, columns=("a",))], model="provider-after"
        ),
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=route_policy,
    )

    final = ThyState.model_validate(
        resumed_graph.invoke(
            ThyState(
                run=run,
                plan=(_task("t1"),),
                completed=parked_state.completed,
                agent_messages=parked_state.agent_messages,
            )
        )
    )

    assert final.agent_messages[0].status.value == "COMPLETED"
    resumed_selected = [event for event in log.events() if event.type is EventType.MODEL_SELECTED][
        len(original_selected) :
    ]
    assert resumed_selected
    assert all(event.payload["model"] == "parked-model" for event in resumed_selected)
    resumed_start = [
        event
        for event in log.events()
        if event.type is EventType.AGENT_STARTED and event.payload.get("resumed")
    ]
    assert resumed_start == []
    parked = [event for event in log.events() if event.type is EventType.AGENT_PARKED]
    assert len(parked) == 1
    assert parked[0].payload["execution_identity_sha256"]


def test_changed_registry_mode_cannot_spend_the_parked_review(
    tmp_path: Path,
) -> None:
    """A changed sandbox mode receives a new ticket and parks for a new explicit decision."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    old_echo = FakeTool(
        "echo",
        ToolCapability(id="echo", external_effects=(), sandbox_mode=SandboxMode.WORKSPACE_WRITE),
        arguments_model=EchoArguments,
    )
    context = review_gated_context(tmp_path, run_id=run.id, log=log)
    plan = (_task("t1"),)
    parked = ThyState.model_validate(
        build_thy_graph(
            catalog,
            log,
            provider=ScriptedProvider(
                [LLMToolCall(id="c1", name="echo", arguments={"value": "x"})]
            ),
            tool_registry=ToolRegistry((old_echo,)),
            tool_context=context,
            route_policy=TEST_ROUTE_POLICY,
        ).invoke(ThyState(run=run, plan=plan))
    )
    original_request = next(
        event for event in log.events() if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    decision_id = original_request.payload["decision_id"]
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": True, "automatic": False},
        subject_id=decision_id,
    )
    new_echo = FakeTool(
        "echo",
        ToolCapability(id="echo", external_effects=(), sandbox_mode=SandboxMode.DANGER_FULL_ACCESS),
        arguments_model=EchoArguments,
    )

    resumed = ThyState.model_validate(
        build_thy_graph(
            catalog,
            log,
            provider=ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))]),
            tool_registry=ToolRegistry((new_echo,)),
            tool_context=context,
            route_policy=TEST_ROUTE_POLICY,
        ).invoke(
            ThyState(
                run=run,
                plan=plan,
                completed=parked.completed,
                agent_messages=parked.agent_messages,
            )
        )
    )

    assert resumed.awaiting_approval()
    requests = [event for event in log.events() if event.type is EventType.HUMAN_APPROVAL_REQUESTED]
    assert len(requests) == 2
    assert (
        requests[1].payload["tool_intent_sha256"] != original_request.payload["tool_intent_sha256"]
    )
    assert new_echo.seen_invocation == []


def test_resume_refuses_a_choice_below_a_newly_hardened_role_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stricter current floor fails closed without routing the parked continuation anew."""
    task = Task(
        id=new_id("task"),
        run_id=new_id("run"),
        agent_id=new_id("agent"),
        objective="profile it",
    )
    spec = AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        max_turns=2,
        max_depth=1,
        system_prompt_ref="prompts/data.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    catalog = AgentCatalog(
        (spec,),
        system_prompts={"data": "You are the data agent."},
        output_schemas={"data": DataProfileOutput},
    )
    identity = ParkedExecutionIdentity(
        model_choice=_choice(),
        tool_calls=(
            ParkedToolIdentity(
                pydantic_tool_call_id="pydantic-1",
                decision_id="decision-1",
                tool_call_id="tool-1",
                tool_intent_sha256="a" * 64,
            ),
        ),
        agent_id=task.agent_id,
        task_id=task.id,
        step_key="b" * 64,
        continuation_key=continuation_key(task.agent_id, task.id),
    )
    resume = PendingResume(
        messages=(),
        deferred_tool_results=DeferredToolResults(approvals={"pydantic-1": True}),
        identity=identity,
    )
    log = InMemoryEventLog(task.run_id)
    monkeypatch.setenv("THYMIRA_AGENT_MIN_TIER", "FRONTIER")
    with pytest.raises(ValueError, match="below the current role floor"):
        AgentRunner().run(
            spec,
            task,
            AgentContext(catalog=catalog, event_log=log),
            resume=resume,
        )

    assert not [event for event in log.events() if event.type is EventType.MODEL_SELECTED]


def test_an_unavailable_parked_model_never_falls_back_to_current_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted unavailable choice fails closed before LiteLLM can read a changed environment."""
    monkeypatch.setenv("THYMIRA_MODEL", "changed-model")
    choice = _choice().model_copy(update={"model": "<unconfigured>"})
    log = InMemoryEventLog(new_id("run"))
    # No session policy can authorize the unconfigured sentinel (`ModelRoutePolicy.from_routes`
    # refuses it), so the route check denies the parked choice before any provider is built.
    model = routed_model(
        Role.AGENT, "analyze", log, bound_choice=choice, route_policy=TEST_ROUTE_POLICY
    )

    with pytest.raises(ModelRouteDeniedError, match="not allowed by session policy"):
        Agent(model=model).run_sync("profile it")

    selected = [event for event in log.events() if event.type is EventType.MODEL_SELECTED]
    assert selected[-1].payload["model"] == "<unconfigured>"
    assert "changed-model" not in {event.payload["model"] for event in selected}
    denied = [event for event in log.events() if event.type is EventType.MODEL_ROUTE_DENIED]
    assert denied[-1].payload["route"] == "<unconfigured>"


class _FailingArtifactStore(LocalArtifactStore):
    """A local store double that proves a park is never reported before persistence succeeds."""

    def save_json(
        self,
        name: str,
        data: Any,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Refuse the resume bundle write."""
        del name, data, produced_by, kind, media_type, preserve_history
        raise OSError("resume persistence unavailable")


def test_failed_resume_persistence_prevents_pending_dispatch_event(tmp_path: Path) -> None:
    """A failed identity write raises before the runner can report or dispatch a parked step."""
    run_id = new_id("run")
    task = Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile")
    log = InMemoryEventLog(run_id)
    spec = AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        tool_allowlist=("echo",),
        max_turns=2,
        max_depth=1,
        system_prompt_ref="prompts/data.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    catalog = AgentCatalog(
        (spec,),
        system_prompts={"data": "You are the data agent."},
        output_schemas={"data": DataProfileOutput},
    )
    context = review_gated_context(tmp_path, run_id=run_id, log=log)
    failing_store = _FailingArtifactStore(tmp_path / "failing-artifacts", run_id)
    context = replace(context, artifact_store=failing_store)
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    agent_context = AgentContext(
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([LLMToolCall(id="c1", name="echo", arguments={"value": "x"})]),
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        artifact_store=failing_store,
        route_policy=TEST_ROUTE_POLICY,
    )

    with pytest.raises(OSError, match="resume persistence unavailable"):
        AgentRunner().run(spec, task, agent_context)

    assert not [
        event
        for event in log.events()
        if event.type is EventType.AGENT_PARKED and event.payload.get("status") == "PENDING"
    ]
    assert echo.seen_invocation == []
