"""Execute node: parallel fan-out over independent tasks with a fan-in barrier (THY-23).

Concurrency is proven, not assumed: the two independent wave-0 tasks share a `threading.Barrier(2)`
their scripted model call must reach together. If Execute ran them one at a time the first would
wait alone and time out (a `BrokenBarrierError` failing the test); the test passing is exactly the
evidence that both were in flight at once. A dependent third task sits in the next wave, so it is
never part of the barrier and always runs after its predecessors.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira.agents import (
    AgentCatalog,
    RequestLedger,
    RuntimeSkillCatalog,
    RuntimeSkillManifest,
    RunUsage,
    UsageLimitExceededError,
    UsageLimits,
    load_agent_specs,
    verify_runtime_skill_manifest,
)
from thymira.agents.llm.base import BaseModelT, LLMCallError, LLMResponse
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.mira.checks import replay_request_ledger
from thymira.schemas import EventType, ModelRoutePolicy, Run, TaskStatus, new_id
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import AgentTask, ThyAgentKind, ThyPhase, ThyState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from thymira.agents.llm.base import LLMMessage, LLMToolDefinition

_BARRIER_TIMEOUT = 10.0


class _KeyedProvider:
    """A thread-safe provider keyed by task instruction, optionally rendezvousing on a barrier.

    Unlike `ScriptedProvider` (order-keyed, single-threaded), this returns the output mapped to
    whichever instruction the prompt carries, so two concurrent calls are served deterministically
    regardless of which lands first. Instructions named in `barrier_keys` block on `barrier` before
    returning, so the test can require the two wave-0 tasks to be running at the same time.
    """

    provider_name = "test"
    model = "scripted"

    def __init__(
        self,
        outputs: Mapping[str, BaseModel],
        *,
        barrier: threading.Barrier | None = None,
        barrier_keys: tuple[str, ...] = (),
    ) -> None:
        self._outputs = outputs
        self._barrier = barrier
        self._barrier_keys = frozenset(barrier_keys)
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def _key_for(self, prompt: str) -> str:
        for key in self._outputs:
            if key in prompt:
                return key
        raise LLMCallError(f"no scripted output for prompt: {prompt!r}")

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModelT], system: str = ""
    ) -> tuple[BaseModelT, LLMResponse]:
        del system
        key = self._key_for(prompt)
        with self._lock:
            self.calls.append(key)
        if self._barrier is not None and key in self._barrier_keys:
            self._barrier.wait(timeout=_BARRIER_TIMEOUT)
        validated = schema.model_validate(self._outputs[key].model_dump())
        response = LLMResponse(text=validated.model_dump_json(), provider="test", model="scripted")
        return validated, response

    def complete(self, prompt: str, *, system: str = "") -> LLMResponse:
        raise LLMCallError("complete is not used in these tests")

    def complete_turn(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[LLMToolDefinition] = (),
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        del messages, tools, parallel_tool_calls
        raise LLMCallError("complete_turn is not used in these tests")


class _ReverseCompletionProvider(_KeyedProvider):
    """Complete the west call before releasing east, regardless of submission order."""

    def __init__(self, outputs: Mapping[str, BaseModel]) -> None:
        super().__init__(outputs)
        self._west_completed = threading.Event()

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModelT], system: str = ""
    ) -> tuple[BaseModelT, LLMResponse]:
        key = self._key_for(prompt)
        if key == _EAST:
            assert self._west_completed.wait(timeout=_BARRIER_TIMEOUT)
        result = super().complete_structured(prompt, schema=schema, system=system)
        if key == _WEST:
            self._west_completed.set()
        return result


class _MeteredKeyedProvider(_KeyedProvider):
    """Return fixed non-zero usage so aggregate token and cost ceilings are exercised."""

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModelT], system: str = ""
    ) -> tuple[BaseModelT, LLMResponse]:
        validated, response = super().complete_structured(prompt, schema=schema, system=system)
        return validated, response.model_copy(
            update={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}
        )


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


def _data_agent_catalog(tmp_path: Path) -> AgentCatalog:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data.yaml").write_text(
        """\
name: data
role: agent
task_kinds: [analyze]
max_turns: 2
max_depth: 1
system_prompt_ref: prompts/data.md
output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput
""",
        encoding="utf-8",
    )
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "data.md").write_text("You are the data agent.", encoding="utf-8")
    return load_agent_specs(tmp_path, known_capabilities=frozenset())


def _task(
    task_id: str,
    instruction: str,
    *,
    agent: ThyAgentKind = ThyAgentKind.DATA,
    depends_on: tuple[str, ...] = (),
) -> AgentTask:
    return AgentTask(
        id=task_id,
        agent=agent,
        phase=ThyPhase.EXECUTE,
        instruction=instruction,
        depends_on=depends_on,
    )


_EAST = "profile east"
_WEST = "profile west"
_MERGE = "merge east and west"

_PLAN = (
    _task("t1", _EAST),
    _task("t2", _WEST),
    _task("t3", _MERGE, depends_on=("t1", "t2")),
)

_OUTPUTS: dict[str, DataProfileOutput] = {
    _EAST: DataProfileOutput(row_count=1, columns=("east",)),
    _WEST: DataProfileOutput(row_count=2, columns=("west",)),
    _MERGE: DataProfileOutput(row_count=3, columns=("east", "west")),
}


def _assert_request_lifecycle_is_causal(log: InMemoryEventLog) -> None:
    """Assert every ledger request follows its own start and model selection."""
    events = log.events()
    requests = [event for event in events if event.type is EventType.MODEL_REQUEST_RECORDED]
    for request in requests:
        owner_id = request.payload["owners"][0]["owner_id"]
        task_id, agent_id = owner_id.split(":", maxsplit=1)
        started = next(
            event
            for event in events
            if event.type is EventType.AGENT_STARTED
            and event.payload.get("task_id") == task_id
            and event.subject_id == agent_id
        )
        selected = next(
            event
            for event in events
            if event.type is EventType.MODEL_SELECTED and event.subject_id == agent_id
        )
        response = next(
            event
            for event in events
            if event.type is EventType.MODEL_RESPONSE_CHUNK
            and event.correlation_id == request.subject_id
        )
        assert started.seq < selected.seq < request.seq < response.seq


def test_two_independent_tasks_run_concurrently_and_both_complete_before_summarize(
    tmp_path: Path,
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    barrier = threading.Barrier(2)
    provider = _KeyedProvider(_OUTPUTS, barrier=barrier, barrier_keys=(_EAST, _WEST))
    graph = build_thy_graph(
        catalog, log, provider=provider, max_concurrency=2, route_policy=TEST_ROUTE_POLICY
    )

    raw_result = graph.invoke(ThyState(run=run, plan=_PLAN))
    final_state = ThyState.model_validate(raw_result)

    # The barrier could only have released if t1 and t2 were both in flight together.
    assert not barrier.broken
    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None
    assert [m.status for m in final_state.agent_messages] == [TaskStatus.COMPLETED] * 3
    # The hash chain survived concurrent delegation (child logs merged back in plan order).
    assert log.verify().valid


def test_parallel_run_reserves_the_run_wide_request_limit_before_provider_dispatch(
    tmp_path: Path,
) -> None:
    """Sibling-local usage copies cannot each spend the same final request slot."""
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = _KeyedProvider({_EAST: _OUTPUTS[_EAST], _WEST: _OUTPUTS[_WEST]})
    graph = build_thy_graph(
        _data_agent_catalog(tmp_path),
        log,
        provider=provider,
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )
    usage = RunUsage(limits=UsageLimits(max_requests=1))

    with pytest.raises(UsageLimitExceededError, match="max_requests"):
        graph.invoke(ThyState(run=run, plan=_PLAN[:2], usage=usage))

    assert len(provider.calls) == 1
    assert usage.requests == 1


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (UsageLimits(max_tokens=15), "max_tokens"),
        (UsageLimits(max_cost_usd=0.01), "max_cost_usd"),
    ],
)
def test_parallel_run_serializes_unknown_size_requests_under_token_or_cost_ceiling(
    tmp_path: Path, limits: UsageLimits, message: str
) -> None:
    """A finite token or cost remainder is conservatively reserved by one in-flight call."""
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = _MeteredKeyedProvider({_EAST: _OUTPUTS[_EAST], _WEST: _OUTPUTS[_WEST]})
    graph = build_thy_graph(
        _data_agent_catalog(tmp_path),
        log,
        provider=provider,
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )
    usage = RunUsage(limits=limits)

    with pytest.raises(UsageLimitExceededError, match=message):
        graph.invoke(ThyState(run=run, plan=_PLAN[:2], usage=usage))

    assert len(provider.calls) == 1
    assert usage.requests == 1
    assert usage.total_tokens == 15
    assert usage.cost_usd == pytest.approx(0.01)


def test_parallel_request_evidence_is_causal_without_runtime_skills(tmp_path: Path) -> None:
    """Canonical ledger writes never overtake their child lifecycle prerequisites."""
    run = _run()
    log = InMemoryEventLog(run.id)
    graph = build_thy_graph(
        _data_agent_catalog(tmp_path),
        log,
        provider=_ReverseCompletionProvider({_EAST: _OUTPUTS[_EAST], _WEST: _OUTPUTS[_WEST]}),
        request_ledger=RequestLedger(log),
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )

    graph.invoke(ThyState(run=run, plan=_PLAN[:2]))

    _assert_request_lifecycle_is_causal(log)
    assert log.verify().valid


def test_a_dependent_task_waits_for_its_predecessors(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = _KeyedProvider(_OUTPUTS)  # no barrier: measure ordering, not simultaneity
    graph = build_thy_graph(
        catalog, log, provider=provider, max_concurrency=2, route_policy=TEST_ROUTE_POLICY
    )

    graph.invoke(ThyState(run=run, plan=_PLAN))

    # agent.started carries the objective (the instruction); agent.completed carries only the
    # runtime task id, so map objective -> task id via the start, then task id -> completed seq.
    started_seq: dict[str, int] = {}
    task_id_by_objective: dict[str, str] = {}
    for event in log.events():
        if event.type is EventType.AGENT_STARTED:
            started_seq[event.payload["objective"]] = event.seq
            task_id_by_objective[event.payload["objective"]] = event.payload["task_id"]
    completed_seq_by_task = {
        e.payload["task_id"]: e.seq for e in log.events() if e.type is EventType.AGENT_COMPLETED
    }
    completed_east = completed_seq_by_task[task_id_by_objective[_EAST]]
    completed_west = completed_seq_by_task[task_id_by_objective[_WEST]]

    # The dependent task started only after both predecessors had completed.
    assert started_seq[_MERGE] > completed_east
    assert started_seq[_MERGE] > completed_west


def test_parallel_results_match_the_sequential_run(tmp_path: Path) -> None:
    run = _run()

    seq_log = InMemoryEventLog(run.id)
    seq_catalog = _data_agent_catalog(tmp_path / "seq")
    seq_graph = build_thy_graph(
        seq_catalog, seq_log, provider=_KeyedProvider(_OUTPUTS), route_policy=TEST_ROUTE_POLICY
    )
    seq_state = ThyState.model_validate(seq_graph.invoke(ThyState(run=run, plan=_PLAN)))

    par_log = InMemoryEventLog(run.id)
    par_catalog = _data_agent_catalog(tmp_path / "par")
    par_graph = build_thy_graph(
        par_catalog,
        par_log,
        provider=_KeyedProvider(_OUTPUTS),
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )
    par_state = ThyState.model_validate(par_graph.invoke(ThyState(run=run, plan=_PLAN)))

    # Same work products, in the same plan order: the completed tasks, their statuses and outputs.
    assert [t.id for t in par_state.completed] == [t.id for t in seq_state.completed]
    assert [m.status for m in par_state.agent_messages] == [
        m.status for m in seq_state.agent_messages
    ]
    assert [m.summary for m in par_state.agent_messages] == [
        m.summary for m in seq_state.agent_messages
    ]
    assert par_state.phase is seq_state.phase


def test_parallel_results_keep_duplicate_plan_ids_and_plan_order(tmp_path: Path) -> None:
    """Each same-wave occurrence keeps its own result when completion order is reversed."""
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    provider = _ReverseCompletionProvider(
        {
            _EAST: DataProfileOutput(row_count=1, columns=("east",)),
            _WEST: DataProfileOutput(row_count=2, columns=("west",)),
        }
    )
    # `AgentTask.id` is intentionally plan-local and may repeat. The provider forces the second
    # occurrence to finish first, exercising both dimensions of the result attribution bug.
    plan = (_task("same", _EAST), _task("same", _WEST))
    graph = build_thy_graph(
        catalog, log, provider=provider, max_concurrency=2, route_policy=TEST_ROUTE_POLICY
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert provider.calls == [_WEST, _EAST]
    assert [message.summary for message in final_state.agent_messages] == [
        '{"row_count":1,"columns":["east"]}',
        '{"row_count":2,"columns":["west"]}',
    ]
    assert final_state.completed == plan
    assert log.verify().valid


def test_an_unregistered_agent_kind_fails_its_task_instead_of_raising(tmp_path: Path) -> None:
    """`_run_parallel` mirrors `_run_sequential`: same statuses, one event, folded in plan order.

    `_run_parallel` is not exercised in production (concurrency only engages for a tool-less run),
    so this is test-only coverage -- but it must behave identically to the sequential path the
    live failure hit, never regress to raising past the wave's fan-in barrier.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)  # holds only "data" -- never "statistics-agent"
    provider = _KeyedProvider({_EAST: DataProfileOutput(row_count=1, columns=("east",))})
    plan = (
        _task("t1", _EAST),
        _task("t2", "run stats", agent=ThyAgentKind.STATISTICS),
        _task("t3", "profile more", depends_on=("t2",)),
    )
    graph = build_thy_graph(
        catalog, log, provider=provider, max_concurrency=2, route_policy=TEST_ROUTE_POLICY
    )

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.phase is ThyPhase.SUMMARIZE
    assert final_state.error is None
    assert [t.id for t in final_state.completed] == ["t1", "t2", "t3"]
    assert [m.status for m in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
    ]
    unresolved = [
        event
        for event in log.events()
        if event.type is EventType.AGENT_MESSAGE
        and event.payload.get("agent") == "statistics-agent"
    ]
    assert len(unresolved) == 1
    assert unresolved[0].payload["status"] == TaskStatus.FAILED.value
    assert unresolved[0].payload["registered_agents"] == ["data"]


def test_each_wave_task_lands_exactly_one_settlement_on_the_shared_log(tmp_path: Path) -> None:
    """The wave path replays only the child's *new* events, so a settlement is never duplicated.

    Each concurrent delegation runs on a child log seeded with the wave-start prefix; if the
    replay copied the prefix too, every earlier wave's settlement would land again and MIRA's
    exactly-one-settlement conjunct would fail. A task stopped for a dependency settles on the
    shared log directly, in plan order.
    """
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tmp_path)
    graph = build_thy_graph(
        catalog,
        log,
        provider=_KeyedProvider(_OUTPUTS),
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=_PLAN)))

    settled_task_ids = [
        event.payload["task_id"]
        for event in log.events()
        if event.type is EventType.SUBAGENT_SETTLED
    ]
    assert len(settled_task_ids) == len(set(settled_task_ids)) == len(_PLAN)
    assert settled_task_ids == [outcome.id for outcome in final_state.agent_messages]
    assert log.verify().valid


def test_parallel_runtime_catalog_binds_parent_selection_before_each_scripted_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child prompt may be isolated, but its selected runtime bytes belong to the parent chain."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog_root = tmp_path / "runtime-skills"
    (catalog_root / "skill.md").parent.mkdir(parents=True)
    (catalog_root / "skill.md").write_text("Use the selected runtime guidance.", encoding="utf-8")
    (catalog_root / "catalog.yaml").write_text(
        "skills:\n  - name: parallel-guidance\n"
        "    description: Shared guidance\n    body_ref: skill.md\n",
        encoding="utf-8",
    )
    manifests: list[RuntimeSkillManifest] = []
    runtime_catalog = RuntimeSkillCatalog.load((catalog_root,), orchestrator="thy")
    runtime_catalog.set_manifest_sink(manifests.append)
    plan = (
        _task("t1", _EAST).model_copy(update={"skill_names": ("parallel-guidance",)}),
        _task("t2", _WEST).model_copy(update={"skill_names": ("parallel-guidance",)}),
    )
    ledger = RequestLedger(log)
    provider = ScriptedProvider(
        [
            DataProfileOutput(row_count=1, columns=("east",)),
            DataProfileOutput(row_count=2, columns=("west",)),
        ]
    )
    graph = build_thy_graph(
        _data_agent_catalog(tmp_path / "agents"),
        log,
        provider=provider,
        runtime_skill_catalog=runtime_catalog,
        request_ledger=ledger,
        max_concurrency=2,
        route_policy=TEST_ROUTE_POLICY,
    )

    final_state = ThyState.model_validate(graph.invoke(ThyState(run=run, plan=plan)))

    assert [message.status for message in final_state.agent_messages] == [
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED,
    ]
    selected = {
        event.payload["runtime_skill_selection_id"]: event
        for event in log.events()
        if event.type is EventType.MODEL_SELECTED
        and event.payload.get("runtime_skill_selection_id") is not None
    }
    requests = [event for event in log.events() if event.type is EventType.MODEL_REQUEST_RECORDED]
    assert len(selected) == len(requests) == 2
    manifests_by_selection = {manifest.selection_id: manifest for manifest in manifests}
    assert set(manifests_by_selection) == set(selected)
    for request in requests:
        binding = request.payload["runtime_skill"]
        selection = selected[binding["selection_id"]]
        assert binding["selection_event_id"] == str(selection.event_id)
        assert binding["selection_event_hash"] == selection.hash
        assert selection.seq < request.seq

    _assert_request_lifecycle_is_causal(log)

    assert log.verify().valid
    replay = replay_request_ledger(log.events())
    assert len(replay.requests) == 2
    for manifest in manifests_by_selection.values():
        assert (
            verify_runtime_skill_manifest(
                manifest,
                (catalog_root,),
                authoritative_events=log.events(),
            )
            == ()
        )
