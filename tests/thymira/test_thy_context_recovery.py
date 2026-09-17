"""Acceptance evidence for deterministic context recovery (DSH F8.1-6)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from pydantic_ai import DeferredToolResults
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

import thymira.thy.compaction as compaction_module
from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import EchoArguments, FakeTool
from tests.thymira.test_thy_graph import _data_agent_catalog, _run
from thymira.agents import (
    CHECKPOINT_SECTION_NAMES,
    AgentCatalog,
    AgentContext,
    AgentRunner,
    CheckpointFact,
    LLMToolCall,
    ParkedExecutionIdentity,
    PendingResume,
    PromptBuilder,
    build_execution_identity,
    build_source_checkpoint,
    merge_checkpoints,
    prune_head_marker_tail,
    recover_checkpoint,
)
from thymira.agents.llm.routing import ModelChoice, ModelTier, Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import (
    InMemoryEventLog,
    JsonlEventLog,
    current_surface,
    read_events,
    verify_log,
)
from thymira.policies import (
    Gate,
    GateMode,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    load_policy_stack,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    EventSurface,
    EventType,
    ModelRoutePolicy,
    Task,
    TaskStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.thy import (
    AgentTask,
    CompactionContext,
    CompactionSummary,
    ContextBudget,
    ThyAgentKind,
    ThyPhase,
    ThyState,
    build_thy_graph,
    compact_surface,
)
from thymira.tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.agents.llm.base import LLMResponse


_CHOICE = ModelChoice(
    role=Role.THY,
    task="summarize",
    tier_requested=ModelTier.FAST,
    tier_applied=ModelTier.FAST,
    model="test-model",
    reason="test",
)

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct compaction/build_thy_graph provider seams to one code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


@pytest.mark.parametrize("window", [0, -1, float("inf")])
def test_context_budget_rejects_non_positive_or_non_finite_windows(window: object) -> None:
    """The graph cap is an explicit finite runtime setting, never an inferred model maximum."""
    with pytest.raises(ValueError, match="positive finite"):
        ContextBudget(window=cast("int", window))


def test_pruner_has_a_stable_head_marker_tail_boundary_and_cost() -> None:
    text = "0123456789" * 10

    first = prune_head_marker_tail(text, 50)
    second = prune_head_marker_tail(text, 50)

    assert first == second
    assert first.text == "0123456789\n[… omitted 79 characters …]\n90123456789"
    assert first.head == "0123456789"
    assert first.tail == "90123456789"
    assert first.omitted_chars == 79
    assert first.input_tokens > first.output_tokens


def test_compaction_keeps_a_cross_boundary_tool_pair_together() -> None:
    log = InMemoryEventLog(new_id("run"))
    started = log.append(
        EventType.TOOL_STARTED,
        Actor.system(),
        {"tool_call_id": "call-1", "text": "tool call"},
        subject_id="call-1",
        surface=EventSurface.MODEL_VISIBLE,
    )
    middle = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "unrelated old evidence"},
        surface=EventSurface.MODEL_VISIBLE,
    )
    completed = log.append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"tool_call_id": "call-1", "text": "tool result"},
        subject_id="call-1",
        surface=EventSurface.MODEL_VISIBLE,
    )
    recent = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "recent"},
        surface=EventSurface.MODEL_VISIBLE,
    )
    context = CompactionContext(
        route_policy=TEST_ROUTE_POLICY,
        provider=ScriptedProvider([CompactionSummary(summary="old evidence summary")]),
        choice=_CHOICE,
        event_log=log,
        actor=Actor.system(),
    )

    compacted = compact_surface(log.events(), 5, context)

    assert compacted is not None
    shadowed = compacted.payload["shadowed_seqs"]
    assert middle.seq in shadowed
    assert started.seq not in shadowed
    assert completed.seq not in shadowed
    assert recent.seq not in shadowed


def test_checkpoint_has_exact_sections_and_revisioned_merge_preserves_identity() -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    event = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {
            "text": "the current implementation uses the durable request",
            "task_id": "task-1",
            "user_correction": "keep the audit trail",
        },
        subject_id="agent-1",
        surface=EventSurface.MODEL_VISIBLE,
    )
    first = build_source_checkpoint(
        log.events(), primary_request="profile the data", user_corrections=("keep the audit trail",)
    )
    correction = CheckpointFact(
        key="current_work",
        value="finish the recovery wiring",
        revision=event.seq + 1,
        source_seq=event.seq + 1,
        kind="user_correction",
    )
    second = build_source_checkpoint(log.events(), facts=(correction,), prior=first)
    merged = merge_checkpoints(first, second)

    assert tuple(merged.sections) == CHECKPOINT_SECTION_NAMES
    assert set(merged.sections) == set(CHECKPOINT_SECTION_NAMES)
    assert "task-1" in {fact.value for fact in merged.facts}
    assert "agent-1" in {fact.value for fact in merged.facts}
    assert "finish the recovery wiring" in {fact.value for fact in merged.facts}
    assert first.digest != second.digest


def test_overflow_stops_after_a_durable_unchanged_reduction_attempt(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    for index in range(5):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "history " * 30, "task_id": f"task-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    catalog = _data_agent_catalog()
    spec = catalog.get("data")
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="continue",
    )
    builder = PromptBuilder(catalog)
    provider = ScriptedProvider([CompactionSummary(summary="x" * 10_000)])
    context = CompactionContext(
        route_policy=TEST_ROUTE_POLICY,
        provider=provider,
        choice=_CHOICE,
        event_log=log,
        actor=Actor.system(),
    )

    outcome = ContextBudget(window=80, model="test-model").fit(
        builder, spec, task, log.events(), context
    )

    assert outcome.stopped
    assert not outcome.progressed
    assert outcome.retries == 1
    assert len(provider.calls) == 1
    assert len([event for event in log.events() if event.type is EventType.CONTEXT_COMPACTED]) == 1


def test_running_graph_does_not_call_agent_after_compaction_made_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production lazily resolves compaction, then settles without an agent provider call."""
    run = _run()
    log = InMemoryEventLog(run.id)
    for index in range(5):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "history " * 30, "task_id": f"task-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    provider = ScriptedProvider(
        [
            CompactionSummary(summary="x" * 10_000),
            DataProfileOutput(row_count=7, columns=("age",)),
        ]
    )
    monkeypatch.setattr(
        compaction_module,
        "LiteLLMProvider",
        lambda **_kwargs: provider,
    )
    plan = (
        AgentTask(id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="continue"),
    )
    recorded: list[LLMResponse] = []
    graph = build_thy_graph(
        _data_agent_catalog(),
        log,
        provider=None,
        record_model_usage=recorded.append,
        context_budget=ContextBudget(window=80, model="test-model"),
        route_policy=TEST_ROUTE_POLICY,
    )

    result = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert len(provider.calls) == 1
    assert len(recorded) == 1
    assert recorded[0].metadata["schema"] == "CompactionSummary"
    assert result.agent_messages[0].status is TaskStatus.FAILED
    completed = [event for event in log.events() if event.type is EventType.AGENT_COMPLETED]
    assert completed[-1].payload["status"] == TaskStatus.FAILED.value
    assert completed[-1].payload["end_reason"] == "context_budget"


def test_running_graph_consumes_checkpoint_and_independent_log_reader_reconstructs_it(
    tmp_path: Path,
) -> None:
    run = _run()
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path, run.id)
    source_event_ids: set[str] = set()
    for index in range(4):
        source_event_ids.add(
            log.append(
                EventType.AGENT_MESSAGE,
                Actor.system(),
                {"text": "long history " * 15, "task_id": f"task-{index}"},
                surface=EventSurface.MODEL_VISIBLE,
            ).event_id
        )
    provider = ScriptedProvider(
        [
            CompactionSummary(summary="the earlier work was profiled"),
            CompactionSummary(summary="the earlier work was profiled"),
            DataProfileOutput(row_count=7, columns=("age",)),
        ]
    )
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
    graph = build_thy_graph(
        _data_agent_catalog(),
        log,
        provider=provider,
        context_budget=ContextBudget(window=300, model="test-model"),
        route_policy=TEST_ROUTE_POLICY,
    )

    result = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))
    # Rebuild from raw JSONL with a fresh reader.  This oracle does not use the producer's live
    # list or the provider's returned object to decide whether recovery was durable.
    reconstructed = read_events(path)
    checkpoint = recover_checkpoint(reconstructed)

    assert result.agent_messages[0].status is TaskStatus.COMPLETED
    assert any(event.type is EventType.CONTEXT_COMPACTED for event in reconstructed)
    assert checkpoint is not None
    assert tuple(checkpoint.sections) == CHECKPOINT_SECTION_NAMES
    assert checkpoint.sections["Primary Request and Intent"] == "profile it"
    assert source_event_ids <= set(checkpoint.source_event_ids)
    assert verify_log(path).valid
    assert len(current_surface(reconstructed)) < 7
    assert any("Continue from source checkpoint" in call["prompt"] for call in provider.calls)
    assert provider.calls[0]["tools"] == []


def test_running_graph_keeps_authorized_tool_pairs_intact_across_compaction(
    tmp_path: Path,
) -> None:
    """A compacted graph can dispatch a fresh reviewed call without splitting old evidence."""
    run = _run()
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path, run.id)
    for index in range(4):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "old context " * 15, "task_id": f"old-task-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    old_subject = "tool-call-old"
    old_decision = "decision-old"
    old_authorization = "a" * 64
    old_intent = "b" * 64
    old_pair = (
        log.append(
            EventType.HUMAN_APPROVAL_REQUESTED,
            Actor.system(),
            {
                "decision_id": old_decision,
                "authorization_context_sha256": old_authorization,
                "tool_intent_sha256": old_intent,
                "text": "old approval request",
            },
            subject_id=old_subject,
            authorization_context_sha256=old_authorization,
            surface=EventSurface.MODEL_VISIBLE,
        ),
        log.append(
            EventType.HUMAN_APPROVAL,
            Actor.system(),
            {
                "id": "approval-old",
                "policy_decision_id": old_decision,
                "authorization_context_sha256": old_authorization,
                "text": "old approval",
            },
            subject_id=old_subject,
            authorization_context_sha256=old_authorization,
            surface=EventSurface.MODEL_VISIBLE,
        ),
        log.append(
            EventType.TOOL_STARTED,
            Actor.system(),
            {
                "tool_call_id": old_subject,
                "decision_id": old_decision,
                "tool_intent_sha256": old_intent,
                "text": "old tool call",
            },
            subject_id=old_subject,
            surface=EventSurface.MODEL_VISIBLE,
        ),
        log.append(
            EventType.TOOL_COMPLETED,
            Actor.system(),
            {"tool_call_id": old_subject, "text": "old tool result"},
            subject_id=old_subject,
            surface=EventSurface.MODEL_VISIBLE,
        ),
    )
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "latest context"},
        surface=EventSurface.MODEL_VISIBLE,
    )
    provider = ScriptedProvider(
        [
            CompactionSummary(summary="the old evidence remains paired"),
            CompactionSummary(summary="the old evidence remains paired"),
            LLMToolCall(id="current-call", name="echo", arguments={"value": "x"}),
            DataProfileOutput(row_count=1, columns=("value",)),
        ]
    )
    echo = FakeTool(
        name="echo",
        capability=ToolCapability(id="echo", external_effects=()),
        arguments_model=EchoArguments,
    )
    registry = ToolRegistry((echo,))
    tool_context = ToolContext(
        run_id=run.id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(
            PolicyEngine(load_policy_stack()),
            log,
            approver=lambda _request: True,
            human=Actor(kind="human", id="reviewer", authenticated=True),
            mode=GateMode.SYNCHRONOUS,
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=0.1
        ),
    )
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
    base_catalog = _data_agent_catalog(tool_allowlist=("echo",))
    # The review-gated scenario needs one request for the call and one for the final result.
    catalog = AgentCatalog(
        (base_catalog.get("data").model_copy(update={"max_turns": 3}),),
        system_prompts={"data": base_catalog.system_prompt("data")},
        output_schemas={"data": base_catalog.output_schema("data")},
    )
    graph = build_thy_graph(
        catalog,
        log,
        provider=provider,
        tool_registry=registry,
        tool_context=tool_context,
        context_budget=ContextBudget(window=300, model="test-model"),
        route_policy=TEST_ROUTE_POLICY,
    )

    result = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))
    events = JsonlEventLog(path, run.id).events()
    compacted = next(event for event in events if event.type is EventType.CONTEXT_COMPACTED)
    shadowed = set(compacted.payload["shadowed_seqs"])
    old_seqs = {event.seq for event in old_pair}
    assert old_seqs.isdisjoint(shadowed) or old_seqs <= shadowed
    assert result.agent_messages[0].status is TaskStatus.COMPLETED
    assert len(echo.seen_arguments) == 1
    requested = [event for event in events if event.type is EventType.HUMAN_APPROVAL_REQUESTED][-1]
    approval = [event for event in events if event.type is EventType.HUMAN_APPROVAL][-1]
    started = [event for event in events if event.type is EventType.TOOL_STARTED][-1]
    completed = [event for event in events if event.type is EventType.TOOL_COMPLETED][-1]
    assert started.payload["decision_id"] == requested.payload["decision_id"]
    assert started.payload["tool_intent_sha256"] == requested.payload["tool_intent_sha256"]
    assert approval.payload["decision_id"] == requested.payload["decision_id"]
    assert approval.authorization_context_sha256 == requested.authorization_context_sha256
    assert started.payload["approval_scope_sha256"] == requested.payload["approval_scope_sha256"]
    assert completed.payload["tool_call_id"] == started.payload["tool_call_id"]
    assert verify_log(path).valid
    assert any(call["tools"] for call in provider.calls)
    assert any("Continue from source checkpoint" in call["prompt"] for call in provider.calls)


def test_resumed_reviewed_graph_sends_checkpoint_with_the_parked_conversation(
    tmp_path: Path,
) -> None:
    """A resumed approval call receives recovered context without replacing its transcript."""
    run = _run()
    log = InMemoryEventLog(run.id)
    for index in range(4):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": f"source-marker-{index} " + "old context " * 15, "task_id": f"old-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    echo = FakeTool(
        name="echo",
        capability=ToolCapability(id="echo", external_effects=()),
        arguments_model=EchoArguments,
    )
    context = ToolContext(
        run_id=run.id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(
            PolicyEngine(load_policy_stack()),
            log,
            human=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
            mode=GateMode.DEFERRED,
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run.id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=0.1
        ),
    )
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
    first_provider = ScriptedProvider(
        [
            CompactionSummary(summary="the source checkpoint is durable"),
            LLMToolCall(id="parked-call", name="echo", arguments={"value": "x"}),
        ]
    )
    durable = compact_surface(
        log.events(),
        1,
        CompactionContext(
            route_policy=TEST_ROUTE_POLICY,
            provider=first_provider,
            choice=_CHOICE,
            event_log=log,
            actor=Actor.system(),
        ),
    )
    assert durable is not None
    assert "source-marker-0" in durable.payload["checkpoint"]["sections"]["Key Technical Concepts"]
    graph = build_thy_graph(
        _data_agent_catalog(tool_allowlist=("echo",)),
        log,
        provider=first_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        context_budget=ContextBudget(window=10_000, model="test-model"),
        route_policy=TEST_ROUTE_POLICY,
    )

    parked = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))
    assert parked.awaiting_approval()
    parked_model_prompt = first_provider.calls[-1]["prompt"]
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
    resumed_catalog = _data_agent_catalog(tool_allowlist=("echo",))
    resumed_provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("value",))])
    resumed_graph = build_thy_graph(
        resumed_catalog,
        log,
        provider=resumed_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        context_budget=ContextBudget(window=10_000, model="test-model"),
        route_policy=TEST_ROUTE_POLICY,
    )

    resumed = ThyState.model_validate(
        resumed_graph.invoke(
            ThyState(
                run=run,
                plan=plan,
                completed=parked.completed,
                agent_messages=parked.agent_messages,
            )
        )
    )

    assert resumed.agent_messages[0].status is TaskStatus.COMPLETED
    assert echo.seen_arguments == [{"value": "x"}]
    assert len(resumed_provider.calls) == 1
    prompt = resumed_provider.calls[0]["prompt"]
    assert "Continue from source checkpoint" in prompt
    assert "source-marker-0" in prompt
    assert "Primary Request and Intent:" in prompt
    assert "Critical Context:" in prompt
    assert parked_model_prompt not in prompt


def test_resumed_runner_stops_before_gateway_when_checkpoint_exceeds_runtime_cap() -> None:
    """The actual routed provider sees no call when the transformed resume is over the cap."""
    run = _run()
    log = InMemoryEventLog(run.id)
    source = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "source evidence", "task_id": "source-task"},
        surface=EventSurface.MODEL_VISIBLE,
    )
    checkpoint = build_source_checkpoint(
        log.events(),
        primary_request="continue the approved task",
        current_work="huge checkpoint " * 5_000,
    )
    log.append(
        EventType.CONTEXT_COMPACTED,
        Actor.system(),
        {
            "shadowed_seqs": [source.seq],
            "text": "durable summary",
            "checkpoint": checkpoint.model_dump(mode="json"),
            "checkpoint_digest": checkpoint.digest,
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("value",))])
    catalog = _data_agent_catalog()
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="continue the approved task",
    )
    spec = catalog.get(ThyAgentKind.DATA.value)
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
        agent_id=task.agent_id,
        task_id=task.id,
        step_key="c" * 64,
        tool_call_metadata={
            "approved-call": {
                "decision_id": "decision-1",
                "tool_call_id": "tool-call-1",
                "tool_intent_sha256": "d" * 64,
                "sandbox_mode": None,
            }
        },
    )
    resume = PendingResume(
        messages=(
            ModelRequest(parts=[UserPromptPart(content="parked original")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="echo", args={"value": "x"}, tool_call_id="approved-call"
                    )
                ]
            ),
        ),
        deferred_tool_results=DeferredToolResults(approvals={"approved-call": True}),
        identity=identity,
    )

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=log,
            provider=provider,
            context_budget=ContextBudget(window=80, model="test-model"),
        ),
        resume=resume,
    )

    assert result.task_status is TaskStatus.FAILED
    assert provider.calls == []
    completed = [event for event in log.events() if event.type is EventType.AGENT_COMPLETED]
    assert completed[-1].payload["end_reason"] == "context_budget"
    assert completed[-1].payload["budget_tokens"] > 80


def _resume_test_identity(
    task: Task, tool_call_id: str
) -> tuple[ModelChoice, ParkedExecutionIdentity]:
    """Shared model choice + execution identity for the resume-compaction tests below."""
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
        agent_id=task.agent_id,
        task_id=task.id,
        step_key="c" * 64,
        tool_call_metadata={
            tool_call_id: {
                "decision_id": "decision-1",
                "tool_call_id": "tool-call-1",
                "tool_intent_sha256": "d" * 64,
                "sandbox_mode": None,
            }
        },
    )
    return choice, identity


def test_resumed_runner_compacts_before_gateway_when_rendered_surface_exceeds_runtime_cap() -> None:
    """A resumed step whose event-log surface overflows compacts it first, like a fresh step."""
    run = _run()
    log = InMemoryEventLog(run.id)
    for index in range(4):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "long history " * 100, "task_id": f"old-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    provider = ScriptedProvider(
        [
            CompactionSummary(summary="the earlier work was profiled"),
            CompactionSummary(summary="the earlier work was profiled"),
            DataProfileOutput(row_count=1, columns=("value",)),
        ]
    )
    base_catalog = _data_agent_catalog()
    # Resolving a denied deferred call is one request; the model's reaction is a second -- the same
    # "one request for the call, one for the final result" budget
    # `test_running_graph_keeps_authorized_tool_pairs_intact_across_compaction` already needs.
    catalog = AgentCatalog(
        (base_catalog.get("data").model_copy(update={"max_turns": 3}),),
        system_prompts={"data": base_catalog.system_prompt("data")},
        output_schemas={"data": base_catalog.output_schema("data")},
    )
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="continue the approved task",
    )
    spec = catalog.get(ThyAgentKind.DATA.value)
    _choice, identity = _resume_test_identity(task, "parked-call")
    resume = PendingResume(
        messages=(
            ModelRequest(parts=[UserPromptPart(content="parked original")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="parked-call")
                ]
            ),
        ),
        # Denied, not approved: pydantic-ai resolves a denial without validating or executing the
        # call, so this proves the context-budget fix without also wiring a ToolRegistry/Gate.
        deferred_tool_results=DeferredToolResults(approvals={"parked-call": False}),
        identity=identity,
    )

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=log,
            provider=provider,
            route_policy=TEST_ROUTE_POLICY,
            context_budget=ContextBudget(window=800, model="test-model"),
            compaction_context=CompactionContext(
                route_policy=TEST_ROUTE_POLICY,
                provider=provider,
                choice=_CHOICE,
                event_log=log,
                actor=Actor.system(),
            ),
        ),
        resume=resume,
    )

    # If `_require_intact_deferred_batches` had rejected the pairing, this would be FAILED with
    # end_reason=resume_history instead -- COMPLETED is only reachable with the pairing intact.
    assert result.task_status is TaskStatus.COMPLETED
    assert any(event.type is EventType.CONTEXT_COMPACTED for event in log.events())
    completed = [event for event in log.events() if event.type is EventType.AGENT_COMPLETED]
    assert completed[-1].payload["end_reason"] == "completed"


def test_resumed_runner_still_stops_when_prior_tool_turns_alone_exceed_runtime_cap() -> None:
    """Vector 1 (event-log surface) compacts; vector 2 (this step's own history) still fails."""
    run = _run()
    log = InMemoryEventLog(run.id)
    for index in range(4):
        log.append(
            EventType.AGENT_MESSAGE,
            Actor.system(),
            {"text": "long history " * 15, "task_id": f"old-{index}"},
            surface=EventSurface.MODEL_VISIBLE,
        )
    provider = ScriptedProvider(
        [
            CompactionSummary(summary="the earlier work was profiled"),
            CompactionSummary(summary="the earlier work was profiled"),
        ]
    )
    catalog = _data_agent_catalog()
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="continue the approved task",
    )
    spec = catalog.get(ThyAgentKind.DATA.value)
    _choice, identity = _resume_test_identity(task, "parked-call")
    resume = PendingResume(
        messages=(
            ModelRequest(parts=[UserPromptPart(content="parked original")]),
            # An earlier, already-resolved tool round trip from *this same step* (vector 2):
            # Thymira's own event log never sees this, so no amount of event-log compaction can
            # shrink it.
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="echo", args={"value": "first"}, tool_call_id="already-done-call"
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="echo",
                        content="stale tool output " * 5_000,
                        tool_call_id="already-done-call",
                    )
                ]
            ),
            # The one call this park/resume is actually about.
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="parked-call")
                ]
            ),
        ),
        deferred_tool_results=DeferredToolResults(approvals={"parked-call": False}),
        identity=identity,
    )

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=log,
            provider=provider,
            context_budget=ContextBudget(window=300, model="test-model"),
            compaction_context=CompactionContext(
                route_policy=TEST_ROUTE_POLICY,
                provider=provider,
                choice=_CHOICE,
                event_log=log,
                actor=Actor.system(),
            ),
        ),
        resume=resume,
    )

    assert result.task_status is TaskStatus.FAILED
    completed = [event for event in log.events() if event.type is EventType.AGENT_COMPLETED]
    assert completed[-1].payload["end_reason"] == "context_budget"
    # Vector 1 genuinely compacted before vector 2 alone sank the step -- regression guard for the
    # `fit_retries` fix: this used to be hardcoded to 0 whenever a resume's own safety net tripped.
    assert completed[-1].payload["compaction_retries"] > 0
    assert any(event.type is EventType.CONTEXT_COMPACTED for event in log.events())


def test_resumed_runner_compacts_its_own_resolved_tool_rounds_to_avoid_context_budget() -> None:
    """Vector 2: an already-resolved round from this step's own history is dropped, not fatal.

    Same fixture as `test_resumed_runner_still_stops_when_prior_tool_turns_alone_exceed_runtime_cap`
    (an earlier, fully-resolved "already-done-call" round ahead of the still-open "parked-call"),
    but with a window wide enough that dropping just that resolved round -- never the deferred one
    -- is enough to fit. Proves the step completes instead of failing closed, and that the pending
    call's own denial is still honoured afterwards (`_require_intact_deferred_batches` would have
    rejected a corrupted splice).
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = ScriptedProvider(
        [DataProfileOutput(row_count=1, columns=("value",))],
    )
    base_catalog = _data_agent_catalog()
    catalog = AgentCatalog(
        (base_catalog.get("data").model_copy(update={"max_turns": 3}),),
        system_prompts={"data": base_catalog.system_prompt("data")},
        output_schemas={"data": base_catalog.output_schema("data")},
    )
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="continue the approved task",
    )
    spec = catalog.get(ThyAgentKind.DATA.value)
    _choice, identity = _resume_test_identity(task, "parked-call")
    resume = PendingResume(
        messages=(
            ModelRequest(parts=[UserPromptPart(content="parked original")]),
            # An earlier, already-resolved tool round trip from *this same step* (vector 2): huge
            # enough alone to overflow the window, but safe to drop -- it is fully resolved and
            # sits strictly before the still-open round below.
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="echo", args={"value": "first"}, tool_call_id="already-done-call"
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="echo",
                        content="stale tool output " * 5_000,
                        tool_call_id="already-done-call",
                    )
                ]
            ),
            # The one call this park/resume is actually about -- must survive untouched.
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="parked-call")
                ]
            ),
        ),
        deferred_tool_results=DeferredToolResults(approvals={"parked-call": False}),
        identity=identity,
    )

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=log,
            provider=provider,
            route_policy=TEST_ROUTE_POLICY,
            context_budget=ContextBudget(window=800, model="test-model"),
            compaction_context=CompactionContext(
                route_policy=TEST_ROUTE_POLICY,
                provider=provider,
                choice=_CHOICE,
                event_log=log,
                actor=Actor.system(),
            ),
        ),
        resume=resume,
    )

    assert result.task_status is TaskStatus.COMPLETED
    completed = [event for event in log.events() if event.type is EventType.AGENT_COMPLETED]
    assert completed[-1].payload["end_reason"] == "completed"
    compacted = [
        event.payload
        for event in log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("form") == "resume_history_compacted"
    ]
    assert len(compacted) == 1
    assert compacted[0]["rounds_dropped"] == 1


def test_resumed_runner_preserves_full_checkpoint_fidelity_when_already_under_runtime_cap() -> None:
    """A resume that already fits gets the untruncated checkpoint, not the bounded projection."""
    run = _run()
    log = InMemoryEventLog(run.id)
    source = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "source evidence", "task_id": "source-task"},
        surface=EventSurface.MODEL_VISIBLE,
    )
    marker = "UNIQUE_TAIL_MARKER_82f1"
    checkpoint = build_source_checkpoint(
        log.events(),
        primary_request="continue the approved task",
        current_work=("padding word " * 400) + marker + (" more padding" * 400),
    )
    log.append(
        EventType.CONTEXT_COMPACTED,
        Actor.system(),
        {
            "shadowed_seqs": [source.seq],
            "text": "durable summary",
            "checkpoint": checkpoint.model_dump(mode="json"),
            "checkpoint_digest": checkpoint.digest,
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("value",))])
    catalog = _data_agent_catalog()
    task = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="continue the approved task",
    )
    spec = catalog.get(ThyAgentKind.DATA.value)
    _choice, identity = _resume_test_identity(task, "parked-call")
    resume = PendingResume(
        messages=(
            ModelRequest(parts=[UserPromptPart(content="parked original")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="echo", args={"value": "x"}, tool_call_id="parked-call")
                ]
            ),
        ),
        deferred_tool_results=DeferredToolResults(approvals={"parked-call": False}),
        identity=identity,
    )

    result = AgentRunner().run(
        spec,
        task,
        AgentContext(
            catalog=catalog,
            event_log=log,
            provider=provider,
            route_policy=TEST_ROUTE_POLICY,
            context_budget=ContextBudget(window=20_000, model="test-model"),
            compaction_context=CompactionContext(
                route_policy=TEST_ROUTE_POLICY,
                provider=provider,
                choice=_CHOICE,
                event_log=log,
                actor=Actor.system(),
            ),
        ),
        resume=resume,
    )

    assert result.task_status is TaskStatus.COMPLETED
    # Already fits: fit() takes the early-return path, no compaction call spent.
    assert len(provider.calls) == 1
    prompt = provider.calls[0]["prompt"]
    assert marker in prompt
    assert "[… omitted" not in prompt
