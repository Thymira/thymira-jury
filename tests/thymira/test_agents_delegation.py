"""Delegation contract: hub-and-spoke agent.message + depth guard (THY-08)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    AgentContext,
    AgentRunner,
    DelegationBoundaryError,
    DelegationDepthExceededError,
    Delegator,
    LLMToolCall,
    PendingDelegation,
    PendingResume,
    PromptBuilder,
    RunUsage,
    Settlement,
    UsageLimitExceededError,
    UsageLimits,
    load_agent_specs,
)
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.policies import (
    Gate,
    GateMode,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    EventType,
    ModelRoutePolicy,
    StopReason,
    Task,
    TaskStatus,
    approval_decision_id,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.thy import CodeResult, coding_agent_catalog
from thymira.tools import (
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.agents import AgentCatalog
    from thymira.schemas import Event

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _catalog(
    tmp_path: Path, *, max_depth: int = 1, tool_allowlist: tuple[str, ...] = ()
) -> AgentCatalog:
    allowlist = " ".join(f"[{', '.join(tool_allowlist)}]".split()) if tool_allowlist else "[]"
    (tmp_path / "data.yaml").write_text(
        f"""\
name: data
role: agent
task_kinds: [analyze]
tool_allowlist: {allowlist}
max_turns: 2
max_depth: {max_depth}
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset(tool_allowlist))


@dataclass(frozen=True, slots=True)
class _FailingTool:
    """A tool that always reports the same failure, for delegation-failure tests."""

    name: str
    capability: ToolCapability
    error: str
    description: str = "A tool that always fails."
    arguments_model: type | None = None
    result_model = LegacyToolValue
    seen_invocation: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        self.seen_invocation.append(invocation)
        raise ToolExecutionError(self.error)


def _tool_context(tmp_path: Path, *, run_id: str, log: InMemoryEventLog) -> ToolContext:
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_a_completed_delegation_produces_one_agent_message_with_a_summary_and_no_reasoning(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    provider = ScriptedProvider([DataProfileOutput(row_count=42, columns=("age", "income"))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=catalog, event_log=log, provider=provider
    )
    delegator = Delegator(ctx)

    task = delegator.delegate("thy", spec, "profile the dataset", depth=0).task

    messages = [e for e in log.events() if e.type == EventType.AGENT_MESSAGE]
    assert len(messages) == 1
    payload = messages[0].payload
    assert payload["depth"] == 1
    assert payload["status"] == TaskStatus.COMPLETED.value
    summary = json.loads(payload["summary"])
    assert summary == {"row_count": 42, "columns": ["age", "income"]}
    assert "reasoning" not in payload
    assert "chain_of_thought" not in payload

    assert task.status == TaskStatus.COMPLETED
    assert task.summary == payload["summary"]


def test_a_delegation_deeper_than_max_depth_is_rejected(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, max_depth=1)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([]),
    )
    delegator = Delegator(ctx)

    # depth=1 (the caller's own depth) -> child_depth=2, but this spec's max_depth is 1.
    with pytest.raises(DelegationDepthExceededError, match="max_depth"):
        delegator.delegate("data-agent", spec, "go deeper", depth=1)

    assert log.events() == []


def test_there_is_no_code_path_to_message_a_sibling_directly() -> None:
    public_methods = {name for name in vars(Delegator) if not name.startswith("_")}
    assert public_methods == {"delegate"}


def test_a_resumed_delegation_requires_the_parked_task_identity(tmp_path: Path) -> None:
    """A resume cannot silently mint a fresh Task when its persisted task was omitted."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    delegator = Delegator(
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=catalog,
            event_log=log,
            provider=ScriptedProvider([]),
        )
    )

    with pytest.raises(ValueError, match="requires its persisted task"):
        delegator.delegate(
            "thy",
            spec,
            "profile the dataset",
            depth=0,
            resume=cast("PendingResume", object()),
        )

    assert log.events() == []


def test_root_delegation_cannot_claim_a_sibling_parent(tmp_path: Path) -> None:
    """The root hub binds every child dispatch to THY's orchestrator identity."""
    catalog = _catalog(tmp_path, max_depth=1)
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([]),
    )

    with pytest.raises(DelegationBoundaryError, match="active agent 'thy'"):
        Delegator(ctx).delegate("sibling", catalog.get("data"), "do work", depth=0)

    assert log.events() == []


def test_a_delegated_child_cannot_relabel_its_parent_as_a_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegated context cannot turn its hub call into a peer or authority handoff."""
    catalog = _catalog(tmp_path, max_depth=2)
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([]),
    )
    child_contexts: list[AgentContext] = []

    def fake_run(
        _runner: AgentRunner,
        _spec: object,
        _task: object,
        child_context: AgentContext,
        *,
        resume: object = None,
    ) -> SimpleNamespace:
        del resume
        child_contexts.append(child_context)
        return SimpleNamespace(
            task_status=TaskStatus.FAILED,
            stop_reason=StopReason.FAILED,
            output=None,
        )

    monkeypatch.setattr(AgentRunner, "run", fake_run)
    delegator = Delegator(ctx)
    delegator.delegate("thy", catalog.get("data"), "first child", depth=0)
    assert child_contexts[0].delegation is not None
    assert child_contexts[0].delegation.agent == "data"

    with pytest.raises(DelegationBoundaryError, match="active agent 'data'"):
        Delegator(child_contexts[0]).delegate("sibling", catalog.get("data"), "do work", depth=1)

    assert len(log.events()) == 2


def test_a_completed_delegations_text_reaches_the_next_steps_prompt_history(
    tmp_path: Path,
) -> None:
    """THY-05 and THY-08 were each tested alone but never wired to each other until now.

    `PromptBuilder` reads `agent.message.text`, and only `Delegator` produces that event --
    if it never set `text`, no agent would ever see what an earlier one in the run had done.
    """
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider([DataProfileOutput(row_count=42, columns=("age", "income"))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=catalog, event_log=log, provider=provider
    )
    Delegator(ctx).delegate("thy", spec, "profile the dataset", depth=0)

    next_task = Task(
        id=new_id("task"), run_id=log.run_id, agent_id=new_id("agent"), objective="use the profile"
    )
    assembled = PromptBuilder(catalog).build(spec, next_task, log.events())

    assert "data completed:" in assembled.user
    assert '"row_count":42' in assembled.user
    assert assembled.user.endswith("use the profile")


def test_a_failed_delegation_diagnoses_from_the_last_failed_tool_call(tmp_path: Path) -> None:
    """THY-17: a max-turns failure carries the real tool error, not a generic message."""
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    tool_context = _tool_context(tmp_path, run_id=run_id, log=log)
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="broken", arguments={}),
            LLMToolCall(id="call-2", name="broken", arguments={}),
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        tool_registry=registry,
        tool_context=tool_context,
    )
    delegator = Delegator(ctx)

    task = delegator.delegate("thy", spec, "profile it", depth=0).task

    assert task.status is TaskStatus.FAILED
    assert task.error == "disk full"
    assert len(broken.seen_invocation) == 2


def test_a_delegated_tool_failure_keeps_lifecycle_identity_on_completion(tmp_path: Path) -> None:
    """A nonzero coding result still stamps the completion as the same delegation."""
    catalog = coding_agent_catalog()
    spec = catalog.get("coding")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([CodeResult(stdout="", exit_code=1)]),
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "run the script", depth=0))

    completion = next(event for event in log.events() if event.type is EventType.AGENT_COMPLETED)
    assert completion.payload["delegation_key"] == settlement.result.delegation_key


def test_a_failed_delegation_with_no_tool_call_falls_back_to_the_generic_message(
    tmp_path: Path,
) -> None:
    """A model that never calls a tool (pure output-validation failure) gets the generic error."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(["not valid json", "still not valid json"])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=catalog, event_log=log, provider=provider
    )
    delegator = Delegator(ctx)

    task = delegator.delegate("thy", spec, "profile it", depth=0).task

    assert task.status is TaskStatus.FAILED
    assert task.error == "agent exceeded max_turns"


def _review_tool_context(tmp_path: Path, *, run_id: str, log: InMemoryEventLog) -> ToolContext:
    """A context whose Gate escalates every local tool to a human and answers nothing."""
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, mode=GateMode.DEFERRED),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=0.1
        ),
    )


def test_a_pending_delegation_says_it_waits_for_a_human(tmp_path: Path) -> None:
    """THY-17/THY-04: a review parks the step PENDING without terminal settlement."""
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    tool_context = _review_tool_context(tmp_path, run_id=run_id, log=log)
    provider = ScriptedProvider([LLMToolCall(id="call-1", name="broken", arguments={})])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        tool_registry=registry,
        tool_context=tool_context,
    )

    task = Delegator(ctx).delegate("thy", spec, "profile it", depth=0).task

    assert task.status is TaskStatus.PENDING
    assert task.error is None
    assert task.completed_at is None
    # The runtime-context snapshot remains model-visible, but a parked task has no keyed parent
    # rendering yet: that notice belongs after the one terminal settlement on resume.
    assert not any(
        event.type is EventType.AGENT_MESSAGE and "delegation_key" in event.payload
        for event in log.events()
    )
    assert not _settlement_events(log)
    assert broken.seen_invocation == []


def test_delegate_narrows_the_child_tool_context_to_its_own_task_and_depth(
    tmp_path: Path,
) -> None:
    """A delegate draws on no authority a human extended to the agent that delegated to it.

    The named consumer for ``ToolContext.delegation_depth``: the child used to run on the
    parent's context verbatim, so an approval raised at the root's depth was spendable by any
    delegate proposing the identical call. The child now carries its own agent id, its own task
    id and depth+1, which is what an approval scope is keyed on.
    """
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    parent_context = _tool_context(tmp_path, run_id=run_id, log=log)
    assert parent_context.delegation_depth == 0
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="broken", arguments={}),
            LLMToolCall(id="call-2", name="broken", arguments={}),
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        tool_registry=registry,
        tool_context=parent_context,
    )

    task = Delegator(ctx).delegate("thy", spec, "profile it", depth=0).task

    assert broken.seen_invocation
    seen = broken.seen_invocation[0]
    assert seen.agent_id == task.agent_id
    assert seen.agent_id != parent_context.agent_id
    assert seen.task_id == task.id
    # The parent's own context is untouched: narrowing is per delegation, not a mutation.
    assert parent_context.task_id is None
    assert parent_context.delegation_depth == 0
    started = next(event for event in log.events() if event.type is EventType.TOOL_STARTED)
    assert started.payload["delegation_depth"] == 1


# --------------------------------------------------------------------------- settlement (F7.5)


def _settlement_events(log: InMemoryEventLog) -> list[Event]:
    return [event for event in log.events() if event.type is EventType.SUBAGENT_SETTLED]


def _terminal(outcome: Settlement | PendingDelegation) -> Settlement:
    """Narrow a delegation outcome in tests that intentionally exercise terminal paths."""
    assert isinstance(outcome, Settlement)
    return outcome


def test_a_completed_child_settles_completed_with_its_validated_result(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider([DataProfileOutput(row_count=42, columns=("age", "income"))])
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=catalog, event_log=log, provider=provider
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "profile the dataset", depth=0))

    assert settlement.result.stop_reason is StopReason.COMPLETED
    assert json.loads(settlement.result.result_json or "") == {
        "row_count": 42,
        "columns": ["age", "income"],
    }
    assert settlement.result.result_schema == (
        "tests.thymira.fixtures_agent_output:DataProfileOutput"
    )
    assert settlement.result.delegation_depth == 1
    assert settlement.result.diagnostics is None
    assert isinstance(settlement.completion, DataProfileOutput)
    assert len(_settlement_events(log)) == 1


def test_a_child_that_exhausts_output_validation_settles_failed(tmp_path: Path) -> None:
    """Two invalid outputs exhaust PydanticAI's retries: `UnexpectedModelBehavior` -> `failed`."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider(["not valid json", "still not valid json"]),
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "profile it", depth=0))

    assert settlement.result.stop_reason is StopReason.FAILED
    assert settlement.result.result_json is None
    assert settlement.task.status is TaskStatus.FAILED
    assert len(_settlement_events(log)) == 1


def test_a_child_that_runs_out_of_its_request_budget_settles_out_of_room(tmp_path: Path) -> None:
    """`max_turns` tool calls exhaust `UsageLimits(request_limit=...)`: out-of-room, not failed."""
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    provider = ScriptedProvider(
        [
            LLMToolCall(id="call-1", name="broken", arguments={}),
            LLMToolCall(id="call-2", name="broken", arguments={}),
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        tool_registry=registry,
        tool_context=_tool_context(tmp_path, run_id=run_id, log=log),
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "profile it", depth=0))

    assert settlement.result.stop_reason is StopReason.OUT_OF_ROOM
    assert settlement.result.diagnostics == "disk full"
    assert settlement.task.status is TaskStatus.FAILED


def test_a_child_waiting_on_a_human_review_is_parked_until_terminal(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([LLMToolCall(id="call-1", name="broken", arguments={})]),
        tool_registry=registry,
        tool_context=_review_tool_context(tmp_path, run_id=run_id, log=log),
    )

    settlement = Delegator(ctx).delegate("thy", spec, "profile it", depth=0)

    assert isinstance(settlement, PendingDelegation)
    # THY's resume path keys on the task status, which is untouched by the finer stop reason.
    assert settlement.task.status is TaskStatus.PENDING
    assert settlement.task.error is None
    assert settlement.task.completed_at is None
    assert not _settlement_events(log)


class _ExplodingCatalog:
    """A catalog whose schema lookup raises -- the shape of any unexpected child failure."""

    def output_schema(self, _name: str) -> type:
        """Fail the way an unexpected runtime error does, before the step even starts."""
        msg = "the catalog is on fire"
        raise RuntimeError(msg)


def test_a_child_that_raises_settles_abnormal_and_never_rejects_its_parent(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=cast("AgentCatalog", _ExplodingCatalog()),
        event_log=log,
        provider=ScriptedProvider([]),
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "profile it", depth=0))

    assert settlement.result.stop_reason is StopReason.ABNORMAL
    assert settlement.result.diagnostics is not None
    assert "RuntimeError" in settlement.result.diagnostics
    assert "the catalog is on fire" in settlement.result.diagnostics
    assert settlement.task.status is TaskStatus.FAILED
    assert len(_settlement_events(log)) == 1


def test_a_run_budget_breach_settles_the_child_before_it_ends_the_run(tmp_path: Path) -> None:
    """A budget breach is a run-level fact and still propagates.

    The settlement is on the chain before it does, so an auditor never sees a delegation that
    simply vanished -- but this is the one class of child that still ends its parent, by design.
    """
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))]),
        usage=RunUsage(limits=UsageLimits(max_requests=0)),
    )

    with pytest.raises(UsageLimitExceededError):
        Delegator(ctx).delegate("thy", spec, "profile it", depth=0)

    settled = _settlement_events(log)
    assert len(settled) == 1
    assert settled[0].payload["stop_reason"] == StopReason.ABNORMAL.value


def test_the_settlement_is_on_the_chain_before_the_parents_message(tmp_path: Path) -> None:
    """G.3: the durable record exists before the parent can report anything about it."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([DataProfileOutput(row_count=42, columns=("age",))]),
    )

    settlement = _terminal(Delegator(ctx).delegate("thy", spec, "profile the dataset", depth=0))

    settled = _settlement_events(log)[0]
    message = next(
        event
        for event in log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("task_id") == settlement.task.id
    )
    assert settled.seq < message.seq
    assert message.payload["delegation_key"] == settlement.result.delegation_key
    assert message.payload["stop_reason"] == StopReason.COMPLETED.value


def test_repeated_instruction_delegations_keep_distinct_invocations(
    tmp_path: Path,
) -> None:
    """A repeated instruction creates a distinct invocation with its own settlement key."""
    catalog = _catalog(tmp_path)
    spec = catalog.get("data")
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=42, columns=("age",)),
            DataProfileOutput(row_count=42, columns=("age",)),
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY, catalog=catalog, event_log=log, provider=provider
    )
    delegator = Delegator(ctx)

    fresh = _terminal(delegator.delegate("thy", spec, "profile the dataset", depth=0))
    fork = _terminal(delegator.delegate("thy", spec, "profile the dataset", depth=0))

    assert fresh.result.delegation_key != fork.result.delegation_key
    assert fresh.result.task_id != fork.result.task_id
    assert fresh.result.agent_id != fork.result.agent_id
    assert fresh.result.delegation_depth == fork.result.delegation_depth == 1
    assert fresh.result.stop_reason is fork.result.stop_reason is StopReason.COMPLETED
    assert fresh.result.objective == fork.result.objective == "profile the dataset"


def test_a_forked_child_inherits_no_approval_credit(tmp_path: Path) -> None:
    """Neither a fork nor a fresh delegation gains authority its parent was granted.

    This reads wave 1's mechanism (`ApprovalScope.spendable_at_depth`, keyed on the depth a
    credit was raised at) through `ToolManager.execute`; it duplicates none of it.
    """
    catalog = _catalog(tmp_path, tool_allowlist=("broken",))
    spec = catalog.get("data")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    broken = _FailingTool("broken", ToolCapability(id="broken", external_effects=()), "disk full")
    registry = ToolRegistry((cast("Tool", broken),))
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=ScriptedProvider([LLMToolCall(id="call-1", name="broken", arguments={})]),
        tool_registry=registry,
        tool_context=_review_tool_context(tmp_path, run_id=run_id, log=log),
    )

    # The root's own depth-0 call is parked for a human, and the human answers it.
    first = Delegator(ctx).delegate("thy", spec, "profile it", depth=-1)
    assert isinstance(first, PendingDelegation)
    first_parked = next(event for event in log.events() if event.type is EventType.AGENT_PARKED)
    assert first_parked.payload["delegation_depth"] == 0
    requested = next(
        event for event in log.events() if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    assert requested.payload["delegation_depth"] == 0
    decision_id = approval_decision_id(requested.payload)
    assert decision_id is not None
    log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": True, "automatic": False},
        subject_id=decision_id,
    )

    # A forked child proposing the identical call at depth 1 gets none of that credit.
    ctx.provider = ScriptedProvider([LLMToolCall(id="call-2", name="broken", arguments={})])
    forked = Delegator(ctx).delegate("thy", spec, "profile it", depth=0)

    assert isinstance(forked, PendingDelegation)
    forked_parked = [event for event in log.events() if event.type is EventType.AGENT_PARKED][-1]
    assert forked_parked.payload["delegation_depth"] == 1
    assert broken.seen_invocation == []
