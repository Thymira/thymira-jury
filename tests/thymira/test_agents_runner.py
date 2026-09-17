"""AgentRunner: generic spec-driven agent execution loop (THY-03)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentEndReason,
    AgentResult,
    AgentRunner,
    AgentSpec,
    LLMMessage,
    LLMToolCall,
    LLMToolDefinition,
    RunUsage,
    UsageLimitExceededError,
    UsageLimits,
)
from thymira.agents.llm.routing import Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.runtime_context import RUNTIME_CONTEXT_FORM
from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve, load_policy_stack
from thymira.schemas import (
    Artifact,
    EventSurface,
    EventType,
    ModelRoutePolicy,
    Task,
    TaskStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import ToolContext
from thymira.tools.builtins.run_python import builtins_registry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.agents.llm.base import LLMResponse

_SYSTEM_PROMPT = "You are the data-profiling agent."

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


class _MeteredProvider(ScriptedProvider):
    """A scripted provider whose structured response reports real tokens and cost."""

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModel], system: str = ""
    ) -> tuple[BaseModel, LLMResponse]:
        """Attach non-zero token counts and a cost to the scripted structured response."""
        validated, response = super().complete_structured(prompt, schema=schema, system=system)
        return validated, response.model_copy(
            update={"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.0025}
        )


class _ParallelSettingProvider(ScriptedProvider):
    """A scripted provider that records the per-turn parallel tool-call setting it receives."""

    def __init__(self) -> None:
        super().__init__(
            [
                LLMToolCall(
                    id="call-final",
                    name="final_result",
                    arguments={"row_count": 1, "columns": ["x"]},
                )
            ]
        )
        self.parallel_tool_call_settings: list[bool | None] = []

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        """Capture the setting and delegate the deterministic response."""
        self.parallel_tool_call_settings.append(parallel_tool_calls)
        return super().complete_turn(messages, tools=tools)


def _spec(*, max_turns: int = 3) -> AgentSpec:
    return AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        max_turns=max_turns,
        max_depth=1,
        system_prompt_ref="prompts/data.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )


def _catalog(spec: AgentSpec) -> AgentCatalog:
    return AgentCatalog(
        (spec,),
        system_prompts={spec.name: _SYSTEM_PROMPT},
        output_schemas={spec.name: DataProfileOutput},
    )


def _task(run_id: str | None = None) -> Task:
    return Task(
        id=new_id("task"),
        run_id=run_id or new_id("run"),
        agent_id=new_id("agent"),
        objective="Profile the credit-risk dataset.",
    )


def test_a_valid_structured_response_appends_started_then_completed_and_is_returned() -> None:
    spec = _spec()
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = ScriptedProvider([DataProfileOutput(row_count=1000, columns=("age", "income"))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    result = AgentRunner().run(spec, task, ctx)

    assert isinstance(result, AgentResult)
    assert result.task_status == TaskStatus.COMPLETED
    assert result.output == DataProfileOutput(row_count=1000, columns=("age", "income"))

    event_types = [e.type for e in log.events()]
    started_index = event_types.index(EventType.AGENT_STARTED)
    completed_index = event_types.index(EventType.AGENT_COMPLETED)
    assert started_index < completed_index
    started = next(e for e in log.events() if e.type == EventType.AGENT_STARTED)
    completed = next(e for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert started.payload["step_key"] == completed.payload["step_key"]
    assert len(started.payload["step_key"]) == 64
    assert completed.payload["end_reason"] == AgentEndReason.COMPLETED.value


def test_a_run_exceeding_max_turns_ends_with_task_failed_never_a_silent_drop() -> None:
    spec = _spec(max_turns=2)
    task = _task()
    log = InMemoryEventLog(task.run_id)
    # Every scripted item fails validation against DataProfileOutput -- the provider never
    # returns anything the schema accepts, so every one of the `max_turns` attempts fails.
    provider = ScriptedProvider(["not json {{{", "still not valid", "nope again"])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status == TaskStatus.FAILED
    assert result.output is None

    event_types = [e.type for e in log.events()]
    assert EventType.AGENT_STARTED in event_types
    assert EventType.AGENT_COMPLETED in event_types
    completed_payload = next(e.payload for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert completed_payload["status"] == TaskStatus.FAILED.value
    assert completed_payload["end_reason"] == AgentEndReason.MAX_TURNS.value
    # Even a failed step reports its usage, so a per-agent view never loses what it spent trying.
    assert completed_payload["input_tokens"] == 0
    assert completed_payload["output_tokens"] == 0
    assert completed_payload["cost_usd"] == 0.0


def test_the_model_selected_event_fires_once_per_attempt() -> None:
    spec = _spec(max_turns=3)
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = ScriptedProvider(["bad", "bad", DataProfileOutput(row_count=1, columns=("x",))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status == TaskStatus.COMPLETED
    selected = [e for e in log.events() if e.type == EventType.MODEL_SELECTED]
    assert len(selected) == 3


def test_agent_completed_carries_the_step_tokens_and_cost() -> None:
    """`agent.completed` records the step's tokens (from the run usage) and cost (budget delta)."""
    spec = _spec()
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = _MeteredProvider([DataProfileOutput(row_count=1, columns=("x",))])
    usage = RunUsage()
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=_catalog(spec),
        event_log=log,
        provider=provider,
        usage=usage,
    )

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status == TaskStatus.COMPLETED
    completed = next(e.payload for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert completed["input_tokens"] == 120
    assert completed["output_tokens"] == 30
    assert completed["cost_usd"] == pytest.approx(0.0025)
    # The cost on the event is exactly what the shared run budget was charged for this step.
    assert usage.cost_usd == pytest.approx(0.0025)


def test_agent_completed_cost_is_zero_without_a_shared_budget() -> None:
    """With no `ctx.usage`, tokens still record but cost is unknown and reported as zero."""
    spec = _spec()
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = _MeteredProvider([DataProfileOutput(row_count=1, columns=("x",))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    AgentRunner().run(spec, task, ctx)

    completed = next(e.payload for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert completed["input_tokens"] == 120
    assert completed["output_tokens"] == 30
    assert completed["cost_usd"] == 0.0


def test_both_lifecycle_events_carry_the_agents_own_id_as_subject_id() -> None:
    """`subject_id` is what MIRA's A9 pairs on; the payload is not read by that control."""
    spec = _spec()
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = ScriptedProvider([DataProfileOutput(row_count=1, columns=("x",))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    AgentRunner().run(spec, task, ctx)

    started = next(e for e in log.events() if e.type == EventType.AGENT_STARTED)
    completed = next(e for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert started.subject_id == task.agent_id
    assert completed.subject_id == task.agent_id


def test_the_failed_completion_event_also_carries_the_agents_own_id_as_subject_id() -> None:
    """A step that gave up is still a paired lifecycle; an unsubjected end pairs with nothing."""
    spec = _spec(max_turns=2)
    task = _task()
    log = InMemoryEventLog(task.run_id)
    provider = ScriptedProvider(["not json {{{", "still not valid", "nope again"])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=_catalog(spec), event_log=log, provider=provider
    )

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status == TaskStatus.FAILED
    started = next(e for e in log.events() if e.type == EventType.AGENT_STARTED)
    completed = next(e for e in log.events() if e.type == EventType.AGENT_COMPLETED)
    assert started.subject_id == task.agent_id
    assert completed.subject_id == task.agent_id


def test_mira_detects_the_agent_that_started_and_never_completed() -> None:
    """A9 pairs `agent.started` against `agent.completed` by `subject_id` set membership.

    Two agents run against one shared budget. The first finishes; the second's model call crosses
    the run's request ceiling, so `UsageLimitExceededError` propagates out of `AgentRunner.run`
    (a budget breach is a run-level concern, never folded into a `FAILED` result) and its
    `agent.started` never gets an `agent.completed`. With every lifecycle event unsubjected, A9
    compares `{None}` against `{None}` and reads the dead agent as finished -- the one thing the
    control exists to catch. Only a per-agent `subject_id` makes it non-vacuous, which is why this
    test asserts on the *other* agent's id staying out of the finding as well.
    """
    spec = _spec()
    finished = _task()
    unfinished = _task(run_id=finished.run_id)
    log = InMemoryEventLog(finished.run_id)
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=1, columns=("x",)),
            DataProfileOutput(row_count=2, columns=("y",)),
        ]
    )
    usage = RunUsage(limits=UsageLimits(max_requests=1))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=_catalog(spec),
        event_log=log,
        provider=provider,
        usage=usage,
    )

    assert AgentRunner().run(spec, finished, ctx).task_status == TaskStatus.COMPLETED
    with pytest.raises(UsageLimitExceededError, match="max_requests"):
        AgentRunner().run(spec, unfinished, ctx)

    report = audit_run(AuditContext(log.run_id, log.events()))
    a9 = next(control for control in report.controls if control.control_id == "A9")
    assert a9.status is ControlStatus.FAILED
    assert unfinished.agent_id in a9.detail
    assert finished.agent_id not in a9.detail


def _wired_context(
    tmp_path: Path,
) -> tuple[ScriptedProvider, AgentSpec, Task, AgentContext]:
    """A runner context with tools wired: a run_python allowlist, a registry and a workspace."""
    spec = AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        tool_allowlist=("run_python",),
        max_turns=3,
        max_depth=1,
        system_prompt_ref="prompts/data.md",
        output_schema_ref="tests.thymira.fixtures_agent_output:DataProfileOutput",
    )
    task = _task()
    log = InMemoryEventLog(task.run_id)
    tool_context = ToolContext(
        run_id=task.run_id,
        agent_id=task.agent_id,
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", task.run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-1", name="final_result", arguments={"row_count": 1, "columns": ["x"]}
            )
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=_catalog(spec),
        event_log=log,
        provider=provider,
        tool_registry=builtins_registry(),
        tool_context=tool_context,
    )
    return provider, spec, task, ctx


def test_runner_sends_the_persona_line_and_tool_guidance_in_the_system_prompt(
    tmp_path: Path,
) -> None:
    provider, spec, task, ctx = _wired_context(tmp_path)

    AgentRunner().run(spec, task, ctx)

    system_text = provider.calls[0]["system"]
    assert f"You are the {spec.name} agent of Thymira" in system_text
    assert "Your working directory is" in system_text
    assert "Check the [exit code: N] marker" in system_text


def test_runner_forwards_its_single_tool_call_setting_to_the_provider(tmp_path: Path) -> None:
    """The A3 replay guard must reach LiteLLM rather than stopping at PydanticAI's wrapper."""
    _provider, spec, task, ctx = _wired_context(tmp_path)
    provider = _ParallelSettingProvider()

    AgentRunner().run(spec, task, replace(ctx, provider=provider))

    assert provider.parallel_tool_call_settings == [False]


def test_the_persona_model_matches_the_model_selected_event(tmp_path: Path) -> None:
    """The persona's model id is the same choice the `model.selected` event records."""
    provider, spec, task, ctx = _wired_context(tmp_path)

    AgentRunner().run(spec, task, ctx)

    selected = next(e for e in ctx.event_log.events() if e.type == EventType.MODEL_SELECTED)
    system_text = provider.calls[0]["system"]
    assert f"powered by the {selected.payload['model']} model" in system_text


def test_runner_appends_a_model_visible_runtime_context_before_the_step(tmp_path: Path) -> None:
    provider, spec, task, ctx = _wired_context(tmp_path)

    AgentRunner().run(spec, task, ctx)

    snapshots = [
        event
        for event in ctx.event_log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("form") == RUNTIME_CONTEXT_FORM
    ]
    assert len(snapshots) == 1
    assert snapshots[0].surface is EventSurface.MODEL_VISIBLE
    started = next(e for e in ctx.event_log.events() if e.type is EventType.AGENT_STARTED)
    assert snapshots[0].seq < started.seq
    assert "Current runtime context." in provider.calls[0]["prompt"]


class _UnreadableStore(LocalArtifactStore):
    """An artifact store whose manifest cannot be listed."""

    def list_active(self) -> list[Artifact]:
        raise OSError("manifest unreadable")


def test_an_unrenderable_runtime_context_is_recorded_and_the_step_still_runs(
    tmp_path: Path,
) -> None:
    provider, spec, task, ctx = _wired_context(tmp_path)
    assert ctx.tool_context is not None
    broken = _UnreadableStore(tmp_path / "artifacts", task.run_id)
    ctx.tool_context = replace(ctx.tool_context, artifact_store=broken)

    result = AgentRunner().run(spec, task, ctx)

    assert result.task_status is TaskStatus.COMPLETED
    snapshot = next(
        e
        for e in ctx.event_log.events()
        if e.type is EventType.AGENT_MESSAGE and e.payload.get("form") == RUNTIME_CONTEXT_FORM
    )
    assert snapshot.surface is EventSurface.MODEL_VISIBLE
    assert "could not be rendered: manifest unreadable" in snapshot.payload["text"]
    started = next(e for e in ctx.event_log.events() if e.type is EventType.AGENT_STARTED)
    assert snapshot.seq < started.seq
    assert "could not be rendered" in provider.calls[0]["prompt"]
