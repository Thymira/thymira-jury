"""Execute node: sequential dispatch of the plan to specialist agents (THY-12)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from thymira.agents import AgentCatalog, LLMToolCall, load_agent_specs
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog, current_surface, verify_events
from thymira.policies import (
    Gate,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    ArtifactKind,
    EventSurface,
    EventType,
    Experiment,
    ExperimentStatus,
    ModelRoutePolicy,
    Run,
    StopReason,
    Task,
    TaskStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.thy.agents.experiment import ExperimentResult
from thymira.thy.agents.ml import MLResult, TuningResult
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import AgentTask, ArtifactRequirement, ThyAgentKind, ThyPhase, ThyState
from thymira.thy.nodes.execute import _delegation_instruction
from thymira.tools import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct build_thy_graph provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def _data_agent_catalog(
    tmp_path: Path, *, max_turns: int = 2, tool_allowlist: tuple[str, ...] = ()
) -> AgentCatalog:
    allowlist_line = f"tool_allowlist: [{', '.join(tool_allowlist)}]\n" if tool_allowlist else ""
    (tmp_path / "data.yaml").write_text(
        f"""\
name: data
role: agent
task_kinds: [analyze]
max_turns: {max_turns}
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
{allowlist_line}""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset(tool_allowlist))


def _task(
    task_id: str, *, agent: ThyAgentKind = ThyAgentKind.DATA, depends_on: tuple[str, ...] = ()
) -> AgentTask:
    return AgentTask(
        id=task_id,
        agent=agent,
        phase=ThyPhase.EXECUTE,
        instruction=f"do {task_id}",
        depends_on=depends_on,
    )


def test_delegation_instruction_is_unchanged_without_a_required_artifact() -> None:
    task = _task("t1")

    assert _delegation_instruction(task) == task.instruction


def test_delegation_instruction_names_each_required_artifact_and_kind() -> None:
    """A task cannot satisfy a declared deliverable it is never told exists.

    Found live (2026-09-11): the coding agent wrote the plan's declared `.md` report in full --
    `write_file` reported success -- but passed no `kind`, so the file never became a checked
    `Artifact` and `thymira.thy.graph`'s "required artifacts missing" failed the Run over a file
    that was sitting right there in the workspace. `AgentTask.required_artifacts` names exactly
    what a task must publish; nothing before this fix ever told the delegated agent so.
    """
    task = AgentTask(
        id="t1",
        agent=ThyAgentKind.CODING,
        phase=ThyPhase.EXECUTE,
        instruction="Compile the EDA report.",
        required_artifacts=(
            ArtifactRequirement(name="german_credit_eda_report.md", kind=ArtifactKind.REPORT),
            ArtifactRequirement(name="model.joblib", kind=ArtifactKind.MODEL),
        ),
    )

    instruction = _delegation_instruction(task)

    assert instruction.startswith(task.instruction)
    assert 'german_credit_eda_report.md (kind="report")' in instruction
    assert 'model.joblib (kind="model")' in instruction


def test_delegation_instruction_names_a_declared_media_type() -> None:
    """A plan-declared media type is part of what satisfies the deliverable, not just the name.

    Found live (run_b345..., run_a829...): a plan promises an exact name (`duration.png`) and the
    agent writes a differently named file (`duration_months.png`), failing the Run's own
    deliverable check. The instruction must say the exact name -- already did -- and, when the
    plan also constrains the content type, that type too.
    """
    task = AgentTask(
        id="t1",
        agent=ThyAgentKind.CODING,
        phase=ThyPhase.EXECUTE,
        instruction="Plot the loan duration distribution.",
        required_artifacts=(
            ArtifactRequirement(
                name="duration.png", kind=ArtifactKind.OTHER, media_type="image/png"
            ),
        ),
    )

    instruction = _delegation_instruction(task)

    assert 'duration.png (kind="other", media_type="image/png")' in instruction


def test_three_scripted_tasks_run_in_order_and_complete(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=1, columns=("a",)),
            DataProfileOutput(row_count=2, columns=("b",)),
            DataProfileOutput(row_count=3, columns=("c",)),
        ]
    )
    plan = (_task("t1"), _task("t2"), _task("t3"))
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None
    assert len(final_state.completed) == 3
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.COMPLETED] * 3


def test_a_task_whose_dependency_failed_is_skipped(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, max_turns=1)
    # No scripted items at all: the very first (and only) attempt for "t1" fails outright, and
    # "t2" (which depends on "t1") must never even ask the provider for one.
    provider = ScriptedProvider([])
    plan = (_task("t1"), _task("t2", depends_on=("t1",)))
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert len(final_state.agent_messages) == 2
    first, second = final_state.agent_messages
    assert first.status is TaskStatus.FAILED
    assert second.status is TaskStatus.SKIPPED
    # Exactly the one attempt for "t1" -- "t2" is skipped without ever asking the provider.
    assert len(provider.calls) == 1


def test_a_failing_task_is_recorded_failed_and_still_reaches_summarize(tmp_path: Path) -> None:
    """A failed task is evidence, not a crash (bug-hunt C1): Execute always reaches Summarize."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, max_turns=1)
    provider = ScriptedProvider([])
    plan = (_task("t1"),)
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None
    assert final_state.agent_messages[0].status is TaskStatus.FAILED
    # No `artifact_store` was given, so Summarize is the bare fallback in graph.py, not
    # `summarize_node` -- it still reaches Summarize, which is the point being tested here.
    assert final_state.summary == "not yet implemented"
    message_events = [e for e in log.events() if e.type == EventType.AGENT_MESSAGE]
    assert len(message_events) == 1


def test_a_tool_bearing_run_persists_the_prompt_each_agent_actually_saw(tmp_path: Path) -> None:
    """THY-06 was wired into `AgentRunner` but never reached it: the context carried no store.

    `record_prompt` only fires when `AgentContext.artifact_store` is set, and Execute built its
    context without one — so every `model.selected` stayed THY-02 shaped and no prompt was ever
    persisted, on the one path that runs real agents. The store is the tool context's own, which
    Execute already holds to fold produced artifacts back onto the state.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    context = ToolContext(
        run_id=run.id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=store,
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    graph = build_thy_graph(
        catalog,
        log,
        provider=provider,
        tool_registry=ToolRegistry(()),
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )

    graph.invoke(ThyState(run=run, plan=(_task("t1"),)))

    selected = [e for e in log.events() if e.type is EventType.MODEL_SELECTED]
    assert selected, "the run made no model call"
    assert all(len(e.payload["prompt_sha256"]) == 64 for e in selected)


def test_a_review_gated_tool_call_parks_execute_before_the_next_task(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(id="c1", name="echo", arguments={"value": "x"}),
            DataProfileOutput(row_count=2, columns=("b",)),  # t2's answer: must never be consumed
        ]
    )
    plan = (_task("t1"), _task("t2"))
    graph = build_thy_graph(
        catalog,
        log,
        provider=provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=review_gated_context(tmp_path, run_id=run.id, log=log),
        route_policy=TEST_ROUTE_POLICY,
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert final_state.awaiting_approval()
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.PENDING]
    assert final_state.completed == (plan[0],)
    assert final_state.summary is None  # Summarize never ran
    assert final_state.error is None
    assert len(provider.calls) == 1


def _human_answer(context: ToolContext, decision_id: str, *, approved: bool) -> None:
    """Record what `Gate.resolve_pending_approval` writes when a real human answers."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def test_a_seeded_resume_replays_the_parked_tool_call_instead_of_asking_again(
    tmp_path: Path,
) -> None:
    """The deferred-tool resume mechanism's own regression test (bug-hunt follow-up).

    Before it existed, a resumed pass re-delegated the task from scratch: a fresh model call,
    free to phrase a different tool call than the one that actually parked. Now the human's
    answer is handed back into the exact PydanticAI conversation the call parked with, so the
    tool executes with its *original* arguments and the model is asked only to react to the real
    result -- never to redo the objective.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    context = review_gated_context(tmp_path, run_id=run.id, log=log)
    provider = ScriptedProvider([LLMToolCall(id="c1", name="echo", arguments={"value": "x"})])
    plan = (_task("t1"),)
    graph = build_thy_graph(
        catalog,
        log,
        provider=provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )

    parked = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert parked.awaiting_approval()
    assert len(echo.seen_invocation) == 0  # denied before execution, THY-17

    decision_id = next(
        event.payload["decision_id"]
        for event in log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    _human_answer(context, decision_id, approved=True)

    # Must be asked exactly once: to react to the tool's real result, never to redo "t1".
    resumed_provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    resumed_graph = build_thy_graph(
        catalog,
        log,
        provider=resumed_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )
    final_state = ThyState.model_validate(
        resumed_graph.invoke(
            ThyState(
                run=run,
                plan=plan,
                completed=parked.completed,
                agent_messages=parked.agent_messages,
            )
        )
    )

    assert not final_state.awaiting_approval()
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.COMPLETED]
    assert len(echo.seen_invocation) == 1
    assert echo.seen_arguments == [{"value": "x"}]
    assert len(resumed_provider.calls) == 1


def test_a_seeded_resume_finalizes_the_original_task_when_its_agent_was_removed(
    tmp_path: Path,
) -> None:
    """A catalog change cannot orphan or replace the delegation whose call was approved."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    context = review_gated_context(tmp_path, run_id=run.id, log=log)
    parked_provider = ScriptedProvider(
        [LLMToolCall(id="c1", name="echo", arguments={"value": "x"})]
    )
    plan = (_task("t1"),)
    parked_graph = build_thy_graph(
        catalog,
        log,
        provider=parked_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )
    parked = ThyState.model_validate(parked_graph.invoke(ThyState(run=run, plan=plan)))
    pending = parked.agent_messages[0]
    parked_event = next(event for event in log.events() if event.type is EventType.AGENT_PARKED)
    key = parked_event.payload["delegation_key"]
    decision_id = next(
        event.payload["decision_id"]
        for event in log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    _human_answer(context, decision_id, approved=True)

    resumed_provider = ScriptedProvider([])
    resumed_graph = build_thy_graph(
        AgentCatalog(),
        log,
        provider=resumed_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=context,
        route_policy=TEST_ROUTE_POLICY,
    )
    final_state = ThyState.model_validate(
        resumed_graph.invoke(
            ThyState(
                run=run,
                plan=plan,
                completed=parked.completed,
                agent_messages=parked.agent_messages,
            )
        )
    )

    failed = final_state.agent_messages[0]
    assert failed.status is TaskStatus.FAILED
    assert failed.id == pending.id
    assert failed.agent_id == pending.agent_id
    assert failed.objective == pending.objective
    assert "unknown agent 'data'" in (failed.error or "")
    assert echo.seen_invocation == []
    assert resumed_provider.calls == []

    keyed = [event for event in log.events() if event.payload.get("delegation_key") == key]
    assert [event.type for event in keyed] == [
        EventType.AGENT_STARTED,
        EventType.AGENT_PARKED,
        EventType.AGENT_COMPLETED,
        EventType.SUBAGENT_SETTLED,
        EventType.AGENT_MESSAGE,
    ]
    completed, settled, message = keyed[-3:]
    assert completed.payload["task_id"] == pending.id
    assert completed.payload["stop_reason"] == StopReason.FAILED.value
    assert settled.subject_id == pending.id
    assert settled.payload["task_id"] == pending.id
    assert settled.payload["stop_reason"] == StopReason.FAILED.value
    assert message.subject_id == pending.id
    assert message.payload["task_id"] == pending.id
    assert message.payload["status"] == TaskStatus.FAILED.value
    assert verify_events(log.events()).valid


def test_a_seeded_pass_keeps_decided_outcomes_and_fails_unrecoverable_pending_task(
    tmp_path: Path,
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    plan = (_task("t1"), _task("t2"), _task("t3"))
    decided = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t1",
        status=TaskStatus.COMPLETED,
        summary='{"row_count": 1, "columns": ["a"]}',
    )
    pending = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t2",
        status=TaskStatus.PENDING,
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=2, columns=("b",))])
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    # Exactly the seed a Core resume builds: phase at its INSPECT default, the plan and the
    # outcomes carried. This graph has no `gate`, so Plan is the bare `state.advance()`
    # placeholder from `graph.py`, not `plan_node`'s short-circuit -- either way, no model call
    # happens before Execute sees the seed.
    seeded = ThyState(
        run=run, plan=plan, completed=(plan[0], plan[1]), agent_messages=(decided, pending)
    )

    final_state = ThyState.model_validate(graph.invoke(seeded))

    assert not final_state.awaiting_approval()
    assert final_state.completed == (plan[0], plan[1])
    assert [m.status for m in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    ]
    assert final_state.agent_messages[0] == decided  # carried as it was, never retried
    assert final_state.agent_messages[1].id == pending.id
    assert len(provider.calls) == 0


def test_a_seeded_pass_with_duplicate_plan_ids_keeps_both_outcomes(tmp_path: Path) -> None:
    """Carried outcomes pair positionally, never by id.

    A plan is LLM-authored and nothing enforces id uniqueness (`AgentTask`'s own docstring
    disclaims it) -- so two plan tasks sharing an id must not collapse to one carried outcome.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    plan = (_task("t1"), _task("t1"))  # deliberately duplicate ids
    decided = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t1",
        status=TaskStatus.COMPLETED,
        summary='{"row_count": 1, "columns": ["a"]}',
    )
    pending = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t1",
        status=TaskStatus.PENDING,
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=2, columns=("b",))])
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    seeded = ThyState(
        run=run, plan=plan, completed=(plan[0], plan[1]), agent_messages=(decided, pending)
    )

    final_state = ThyState.model_validate(graph.invoke(seeded))

    assert final_state.completed == plan
    assert [m.status for m in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    ]
    assert final_state.agent_messages[0] == decided  # the first outcome survives, never retried
    assert final_state.agent_messages[1] != decided  # the second keeps its own failed identity
    assert final_state.agent_messages[1].id == pending.id
    assert len(provider.calls) == 0


def test_a_seeded_pass_carries_failed_and_skipped_outcomes_untouched(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    plan = (_task("t1"), _task("t2"), _task("t3"))
    failed = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t1",
        status=TaskStatus.FAILED,
        error="agent exceeded max_turns",
    )
    skipped = Task(
        id=new_id("task"),
        run_id=run.id,
        agent_id=new_id("agent"),
        objective="do t2",
        status=TaskStatus.SKIPPED,
        error="a dependency did not complete",
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    seeded = ThyState(
        run=run, plan=plan, completed=(plan[0], plan[1]), agent_messages=(failed, skipped)
    )

    final_state = ThyState.model_validate(graph.invoke(seeded))

    assert final_state.completed == plan
    assert [m.status for m in final_state.agent_messages] == [
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.COMPLETED,
    ]
    assert final_state.agent_messages[0] == failed  # carried as it was, never retried
    assert final_state.agent_messages[1] == skipped  # carried as it was, never retried
    assert len(provider.calls) == 1


def test_a_carried_pending_task_without_resume_context_fails_closed(tmp_path: Path) -> None:
    """A carried PENDING task cannot become a fresh delegation when recovery context is absent."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    provider = ScriptedProvider([LLMToolCall(id="c1", name="echo", arguments={"value": "x"})])
    plan = (_task("t1"), _task("t2", depends_on=("t1",)))
    graph = build_thy_graph(
        catalog,
        log,
        provider=provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=review_gated_context(tmp_path, run_id=run.id, log=log),
        route_policy=TEST_ROUTE_POLICY,
    )

    parked = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert parked.completed == (plan[0],)  # t2 was never attempted: the loop broke at t1
    assert [m.status for m in parked.agent_messages] == [TaskStatus.PENDING]

    # A plain, tool-less pass cannot recover the parked transcript or verify the answer. It must
    # retain the exact task and ticket evidence rather than ask a second model call.
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resumed_catalog = _data_agent_catalog(resume_dir)
    resumed_provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    resumed_graph = build_thy_graph(
        resumed_catalog, log, provider=resumed_provider, route_policy=TEST_ROUTE_POLICY
    )
    resumed_seed = ThyState(
        run=run, plan=plan, completed=parked.completed, agent_messages=parked.agent_messages
    )

    final_state = ThyState.model_validate(resumed_graph.invoke(resumed_seed))

    assert not final_state.awaiting_approval()
    assert final_state.completed == (plan[0],)
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.FAILED]
    failed = final_state.agent_messages[0]
    assert failed.id == parked.agent_messages[0].id
    assert failed.agent_id == parked.agent_messages[0].agent_id
    assert failed.objective == parked.agent_messages[0].objective
    assert "resume context is unavailable" in (failed.error or "")
    original_denial = next(event for event in log.events() if event.type is EventType.TOOL_DENIED)
    assert original_denial.payload["tool_intent_sha256"] in (failed.error or "")
    assert len(resumed_provider.calls) == 0


def test_a_parallel_default_graph_also_fails_closed_for_carried_pending(
    tmp_path: Path,
) -> None:
    """Parallel dispatch cannot turn a carried review into a fresh fan-out."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    parked_provider = ScriptedProvider(
        [LLMToolCall(id="c1", name="echo", arguments={"value": "x"})]
    )
    plan = (_task("t1"), _task("t2"))
    parked_graph = build_thy_graph(
        catalog,
        log,
        provider=parked_provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=review_gated_context(tmp_path, run_id=run.id, log=log),
        route_policy=TEST_ROUTE_POLICY,
    )
    parked = ThyState.model_validate(parked_graph.invoke(ThyState(run=run, plan=plan)))

    resumed_provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resumed_graph = build_thy_graph(
        _data_agent_catalog(resume_dir),
        log,
        provider=resumed_provider,
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )
    final_state = ThyState.model_validate(
        resumed_graph.invoke(
            ThyState(
                run=run,
                plan=plan,
                completed=parked.completed,
                agent_messages=parked.agent_messages,
            )
        )
    )

    assert final_state.completed == parked.completed
    assert len(final_state.agent_messages) == 1
    failed = final_state.agent_messages[0]
    assert failed.status is TaskStatus.FAILED
    assert failed.id == parked.agent_messages[0].id
    assert len(resumed_provider.calls) == 0


def test_an_unregistered_agent_kind_fails_its_task_instead_of_raising(tmp_path: Path) -> None:
    """An agent kind the run's catalog cannot resolve is a task failure, never a Run crash.

    Direct regression test for the live failure (run_0fd2e3cbbc5341348e97264cd8df0b36): a plan
    step naming `statistics-agent` used to raise `KeyError` out of `catalog.get(...)`, which
    `InlineDispatcher.resume` turned into a terminal `run.failed` before MIRA ever audited the
    work already done. Here the same shape of mismatch settles as a FAILED task and Execute still
    reaches Summarize, exactly like any other failed step (bug-hunt C1).
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)  # holds only "data" -- never "statistics-agent"
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    plan = (
        _task("t1"),
        _task("t2", agent=ThyAgentKind.STATISTICS),
        _task("t3", depends_on=("t2",)),
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.phase is ThyPhase.SUMMARIZE
    # state.error stays reserved for a halt *before* Execute ever runs -- never for a task that
    # failed after being tried.
    assert final_state.error is None
    first, second, third = final_state.agent_messages
    assert first.status is TaskStatus.COMPLETED
    assert second.status is TaskStatus.FAILED
    assert second.error is not None
    assert "statistics-agent" in second.error
    assert "data" in second.error  # the registered roster is named in the failure
    assert third.status is TaskStatus.SKIPPED  # blocked: its dependency did not COMPLETE

    unresolved = [
        event
        for event in log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("agent") == "statistics-agent"
    ]
    assert len(unresolved) == 1
    event = unresolved[0]
    assert event.payload["status"] == TaskStatus.FAILED.value
    assert event.payload["registered_agents"] == ["data"]
    assert event.surface is EventSurface.MODEL_VISIBLE
    assert event.type is not EventType.RUN_FAILED


def test_the_next_agent_sees_the_unresolved_step_in_its_history(tmp_path: Path) -> None:
    """Model-visible is a property of the durable log fold, not a flag the producer set."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)  # holds only "data"
    provider = ScriptedProvider([])
    plan = (_task("t1", agent=ThyAgentKind.STATISTICS),)
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    graph.invoke(ThyState(run=run, plan=plan))

    surface_texts = [
        str(event.payload["text"])
        for event in current_surface(log.events())
        if event.payload.get("text")
    ]
    assert any("statistics-agent" in text and "data" in text for text in surface_texts)


# ------------------------------------------------------------------ uniform settlement (F7.5)


def _experiment_catalog(tmp_path: Path, *, schema_ref: str) -> AgentCatalog:
    """A catalog whose 'experiment' agent outputs `schema_ref` — the real or a foreign schema."""
    (tmp_path / "experiment.yaml").write_text(
        f"""\
name: experiment
role: agent
task_kinds: [code]
max_turns: 2
max_depth: 1
system_prompt_ref: prompts/experiment.md
output_schema_ref: {schema_ref}
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "experiment.md").write_text("You are the experiment agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _experiment_plan_task() -> AgentTask:
    return AgentTask(
        id="train",
        agent=ThyAgentKind.EXPERIMENT,
        phase=ThyPhase.EXECUTE,
        instruction="train one model",
    )


def test_a_task_skipped_because_a_dependency_failed_settles_stopped(tmp_path: Path) -> None:
    """A task the orchestrator never ran settles too, at the depth a real delegation would use."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path, max_turns=1)
    provider = ScriptedProvider([])
    plan = (_task("t1"), _task("t2", depends_on=("t1",)))
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    skipped = final_state.agent_messages[1]
    assert skipped.status is TaskStatus.SKIPPED
    assert skipped.error == "a dependency did not complete"
    assert skipped.completed_at is None

    settlements = {
        event.payload["task_id"]: event.payload
        for event in log.events()
        if event.type is EventType.SUBAGENT_SETTLED
    }
    assert len(settlements) == 2
    stopped = settlements[skipped.id]
    assert stopped["stop_reason"] == StopReason.STOPPED.value
    assert stopped["delegation_depth"] == 1
    assert stopped["agent"] == ThyAgentKind.DATA.value
    assert stopped["diagnostics"] == "a dependency did not complete"


def test_the_experiment_fold_reads_the_completion_the_runner_already_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema-constrained completion is validated once, at settlement, and never re-parsed."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _experiment_catalog(
        tmp_path, schema_ref="thymira.thy.agents.experiment:ExperimentResult"
    )
    provider = ScriptedProvider(
        [
            ExperimentResult(
                parameters={"seed": "7"}, metrics={"auc": 0.8}, seed=7, tracker_run_id="mlflow-1"
            )
        ]
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)
    state = ThyState(run=run, plan=(_experiment_plan_task(),))

    def _refuse(*_args: object, **_kwargs: object) -> ExperimentResult:
        raise AssertionError("no consumer may re-parse the completion")

    monkeypatch.setattr(
        "thymira.thy.nodes.execute.ExperimentResult.model_validate_json", _refuse, raising=True
    )
    final_state = ThyState.model_validate(graph.invoke(state))

    assert len(final_state.experiments) == 1
    assert final_state.experiments[0].metrics == {"auc": 0.8}


def test_the_experiment_fold_prefers_the_authoritative_tool_record(
    tmp_path: Path,
) -> None:
    """The persisted tool experiment remains authoritative over an empty agent summary."""
    run = _run()
    log = InMemoryEventLog(run.id)
    recorded = Experiment(
        id=new_id("experiment"),
        run_id=run.id,
        name="logreg",
        status=ExperimentStatus.COMPLETED,
        metrics={"accuracy": 0.78},
        tracker_run_id="mlflow-1",
    )
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {"experiment": recorded.model_dump(mode="json")},
    )
    catalog = _experiment_catalog(
        tmp_path, schema_ref="thymira.thy.agents.experiment:ExperimentResult"
    )
    provider = ScriptedProvider(
        [ExperimentResult(parameters={}, metrics={}, tracker_run_id="mlflow-1")]
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    final_state = ThyState.model_validate(
        graph.invoke(ThyState(run=run, plan=(_experiment_plan_task(),)))
    )

    assert len(final_state.experiments) == 1
    assert final_state.experiments[0].metrics == {"accuracy": 0.78}


def test_a_malformed_experiment_completion_can_no_longer_reach_the_fold(tmp_path: Path) -> None:
    """A completion that is not an `ExperimentResult` folds nothing, by narrowing not by parsing."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _experiment_catalog(
        tmp_path, schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput"
    )
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))])
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    final_state = ThyState.model_validate(
        graph.invoke(ThyState(run=run, plan=(_experiment_plan_task(),)))
    )

    assert final_state.agent_messages[0].status is TaskStatus.COMPLETED
    assert final_state.experiments == ()


# ------------------------------------------------------------------------ ml-agent audit (THY-19)


def _ml_catalog(tmp_path: Path) -> AgentCatalog:
    """A catalog with both `ml-agent` and its `ml-tuning-helper` (the pair a delegation needs)."""
    (tmp_path / "ml.yaml").write_text(
        """\
name: ml-agent
role: agent
task_kinds: [code]
max_turns: 1
max_depth: 1
system_prompt_ref: prompts/ml.md
output_schema_ref: thymira.thy.agents.ml:MLResult
""",
        encoding="utf-8",
    )
    (tmp_path / "ml_tuning_helper.yaml").write_text(
        """\
name: ml-tuning-helper
role: agent
task_kinds: [code]
max_turns: 1
max_depth: 2
system_prompt_ref: prompts/ml_tuning_helper.md
output_schema_ref: thymira.thy.agents.ml:TuningResult
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "ml.md").write_text("You are the ML agent.", encoding="utf-8")
    (prompts_dir / "ml_tuning_helper.md").write_text(
        "You are the ML tuning helper.", encoding="utf-8"
    )
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _ml_plan_task() -> AgentTask:
    return AgentTask(
        id="train", agent=ThyAgentKind.ML, phase=ThyPhase.EXECUTE, instruction="train one model"
    )


def test_a_completed_ml_agent_task_folds_into_an_auditable_experiment(tmp_path: Path) -> None:
    """`ml-agent` never calls `run_experiment`, but MIRA must still see it as a tracked experiment.

    `_route_model_plan` treats `ML` as an auditable alternative to `EXPERIMENT`; if this fold did
    not exist, a plan that deliberately chose `ml-agent` for training would leave `MIRA` with
    nothing to audit even though the guardrail let it through.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _ml_catalog(tmp_path)
    provider = ScriptedProvider(
        [
            MLResult(
                chosen_family="random_forest",
                hyperparameters={"n_estimators": "200"},
                cv_strategy="5-fold",
                metrics={"auc": 0.81},
            )
        ]
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=(_ml_plan_task(),))))

    assert final_state.agent_messages[0].status is TaskStatus.COMPLETED
    assert len(final_state.experiments) == 1
    experiment = final_state.experiments[0]
    assert experiment.metrics == {"auc": 0.81}
    assert experiment.parameters["chosen_family"] == "random_forest"
    assert experiment.parameters["cv_strategy"] == "5-fold"
    assert experiment.parameters["n_estimators"] == "200"


def test_ml_agent_needing_tuning_help_delegates_the_tuning_helper(tmp_path: Path) -> None:
    """`MLResult.needs_tuning_help` reaches the helper tier THY-19 added (`ml-tuning-helper`).

    `execute_node` never calls `run_ml_agent` -- the primary run still needs the generic
    pending/resume path -- so this follow-up delegation, not that wrapper, is what makes the
    helper reachable from a real Run.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _ml_catalog(tmp_path)
    provider = ScriptedProvider(
        [
            MLResult(
                chosen_family="gradient_boosting",
                hyperparameters={},
                cv_strategy="5-fold",
                metrics={"auc": 0.74},
                needs_tuning_help=True,
                tuning_objective="tune learning_rate and max_depth",
            ),
            TuningResult(hyperparameters={"learning_rate": "0.05"}, best_score=0.79, trials=20),
        ]
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=(_ml_plan_task(),))))

    assert final_state.agent_messages[0].status is TaskStatus.COMPLETED
    settled_agents = [
        event.payload["agent"] for event in log.events() if event.type is EventType.SUBAGENT_SETTLED
    ]
    assert ThyAgentKind.ML.value in settled_agents
    assert "ml-tuning-helper" in settled_agents
