"""The span tree a real Run produces, captured in memory — no network, no credentials.

`test_observability.py` pins the contracts of `thymira.observability` in isolation. This file
asserts the thing those contracts exist for: that driving the *real* composed graph, the real
ThyGraph and the real MIRA fan-out through a `ScriptedProvider` yields one trace whose shape a
reviewer could actually read — phases under the Run, sub-agents under their phase, a generation
per model call carrying the model that answered, and each tool call as a sibling of the
generation that asked for it.

The client is a real `langfuse.Langfuse` wired to an `InMemorySpanExporter`, so what is asserted
is what would leave the process. Every client gets its own `public_key`: the SDK keys its
singleton by that value and would otherwise hand back the first test's client, exporter included.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.thymira.api_support import TEST_CREDENTIAL
from tests.thymira.fixtures_agent_output import DataProfileOutput
from thymira import observability
from thymira.agents import (
    AgentContext,
    AgentRunner,
    LLMToolCall,
    ScriptedProvider,
    load_agent_specs,
)
from thymira.agents.llm.base import LLMResponse
from thymira.api import build_default_deps
from thymira.core import build_runtime_graph_factory
from thymira.events import InMemoryEventLog
from thymira.policies import (
    ActionRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import Actor, Decision, Framework, ModelRoutePolicy, Run, Task, new_id
from thymira.state import LocalArtifactStore
from thymira.thy.graph import build_thy_graph
from thymira.thy.models import AgentTask, PlanOutput, ThyAgentKind, ThyPhase, ThyState
from thymira.tools import LegacyToolValue, Tool, ToolContext, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph
    from opentelemetry.sdk.trace import ReadableSpan
    from pydantic import BaseModel

    from thymira.mira import AuditAgentSpec
    from thymira.schemas import Id

pytestmark = pytest.mark.integration

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


# A secret shaped the way a real one is (the repo's API-key pattern deliberately ignores
# all-lowercase bodies so it does not eat ordinary identifiers) and an address, both planted in
# the Run's prompt so the assertions below prove the redaction boundary actually holds.
_SECRET = "sk-proj-AbCdEf0123456789AbCdEf0123456789"  # noqa: S105  # planted, not a real key
_EMAIL = "analyst@example.com"
_PROMPT = f"analyze the dataset for {_EMAIL} using {_SECRET}"


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Opt this file back into a running SDK, which `tests/conftest.py` disables suite-wide.

    That guard exists so a developer with real `LANGFUSE_*` keys exported cannot ship a trace of
    every test that builds an app. These tests need the SDK actually running to produce spans, and
    stay offline a different way: their client is built with an `InMemorySpanExporter` and a dummy
    key. The `Langfuse` constructor ANDs `tracing_enabled` with the environment variable, so the
    argument alone cannot re-enable it — the variable has to be set here.
    """
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    observability.reset()
    yield
    observability.reset()


class _Capture:
    """One Langfuse client and the exporter that holds everything it emitted."""

    def __init__(self, name: str) -> None:
        self.exporter = InMemorySpanExporter()
        self.client = Langfuse(
            public_key=f"pk-lf-{name}",
            secret_key=f"sk-lf-{name}",
            tracer_provider=TracerProvider(),
            span_exporter=self.exporter,
        )

    def spans(self) -> list[ReadableSpan]:
        observability.flush()
        return list(self.exporter.get_finished_spans())

    def tree(self) -> dict[str, str | None]:
        """Map each span name to its parent's name (None when the parent is outside the batch)."""
        spans = self.spans()
        names = {span.context.span_id: span.name for span in spans}
        return {
            span.name: (names.get(span.parent.span_id) if span.parent else None) for span in spans
        }


def _capture(name: str) -> _Capture:
    capture = _Capture(name)
    observability.configure(client=capture.client)
    return capture


def _attributes(spans: list[ReadableSpan], name: str) -> dict[str, Any]:
    return dict(next(span for span in spans if span.name == name).attributes or {})


# --------------------------------------------------------------------------- the whole Run

_PLAN_OUTPUT = PlanOutput(
    tasks=(
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
)

_PLAN_PASSES = Policy(
    name="plan-passes",
    version="1.0",
    action_rules=(
        ActionRule(
            id="TEST-PLAN",
            action_types=("plan.proposed",),
            decision=Decision.PASS,
            reason="test policy: THY's plan needs no review",
        ),
    ),
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n"
        "governance:\n  frameworks: [INTERNAL]\n",
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _data_agent_catalog(directory: Path) -> Any:
    directory.mkdir(parents=True)
    (directory / "data.yaml").write_text(
        "name: data\n"
        "role: agent\n"
        "task_kinds: [analyze]\n"
        "max_turns: 2\n"
        "max_depth: 1\n"
        "system_prompt_ref: prompts/data.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
        newline="\n",
    )
    prompts = directory / "prompts"
    prompts.mkdir()
    (prompts / "data.md").write_text("You are the data agent.", encoding="utf-8", newline="\n")
    return load_agent_specs(directory, known_capabilities=frozenset())


def _internal_audit_spec() -> AuditAgentSpec:
    from thymira.mira import AuditAgentSpec as Spec

    return Spec(
        name="internal-audit",
        framework=Framework.INTERNAL,
        task_kinds=("audit_judgement",),
        max_turns=1,
        system_prompt="Return candidate findings only.",
    )


def _drive_a_run(tmp_path: Path) -> Run:
    """Run the production factory end to end over a scripted provider, exactly as the graph does."""
    workspace = _workspace(tmp_path)
    catalog = _data_agent_catalog(tmp_path / "catalog")
    provider = ScriptedProvider(
        [
            _PLAN_OUTPUT,
            DataProfileOutput(row_count=1, columns=("a",)),
            {"findings": []},
        ]
    )
    engine = PolicyEngine(_PLAN_PASSES)
    dependency_ref: list = []

    def factory(run: Run) -> CompiledStateGraph:
        deps = dependency_ref[0]

        def gate_factory(run_id: Id) -> Gate:
            return Gate(engine, deps.event_store.open(run_id))

        return build_runtime_graph_factory(
            deps.run_store,
            gate_factory=gate_factory,
            artifact_store_factory=deps.artifact_store_factory,
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            checkpoint_repository=deps.checkpoint_repository,
            provider=provider,
            catalog=catalog,
            specs=(_internal_audit_spec(),),
            project_config=deps.project_resolution.config,
            plan_repository=deps.board_repository,
            dag_repository=deps.dag_repository,
            route_policy=TEST_ROUTE_POLICY,
        )(run)

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=factory,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    assert deps.project_resolution is not None
    session = deps.session_service.create(
        project_id=deps.project_resolution.project_id, client="test"
    )
    return deps.run_service.create_run(
        session.id, _PROMPT, actor=Actor.system(), workspace=workspace
    )


def test_a_whole_run_is_one_trace_whose_tree_follows_the_composition(tmp_path: Path) -> None:
    capture = _capture("whole-run")

    run = _drive_a_run(tmp_path)

    spans = capture.spans()
    assert {span.context.trace_id for span in spans} == {
        int(capture.client.create_trace_id(seed=run.id), 16)
    }
    tree = capture.tree()
    assert tree["run"] is None
    assert tree["thy"] == "run"
    assert tree["mira"] == "run"
    assert tree["thy-plan"] == "thy"
    assert tree["policy-plan-proposed"] == "thy"
    assert tree["data"] == "thy"
    assert tree["mira-preflight"] == "mira"
    assert tree["deterministic-controls"] == "mira"
    assert tree["internal-audit"] == "mira"
    assert tree["policy-review-findings"] == "run"


def test_every_model_call_is_a_generation_under_the_agent_that_made_it(tmp_path: Path) -> None:
    capture = _capture("generations")

    _drive_a_run(tmp_path)

    tree = capture.tree()
    assert tree["thy:plan"] == "thy-plan"
    assert tree["agent:analyze"] == "data"
    assert tree["mira:audit_judgement"] == "internal-audit"


def test_a_generation_carries_the_model_that_answered_and_its_token_usage(
    tmp_path: Path,
) -> None:
    """Not the router's synthetic `routed:{role}:{task}` binding — the id the provider reported."""
    capture = _capture("model-id")

    _drive_a_run(tmp_path)

    attributes = _attributes(capture.spans(), "thy:plan")
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.model.name"] == "scripted"
    assert "usage_details" in str(attributes)
    assert not any(str(value).startswith("routed:") for value in attributes.values())


def test_a_generation_records_the_routing_decision(tmp_path: Path) -> None:
    """Which tier the router applied, and why, is a Thymira fact no Langfuse parser can infer."""
    capture = _capture("routing")

    _drive_a_run(tmp_path)

    plan = _attributes(capture.spans(), "thy:plan")
    assert plan["langfuse.observation.model.name"]
    assert plan["langfuse.observation.metadata.tier_applied"]
    assert plan["langfuse.observation.metadata.routing_reason"]


def test_a_generation_carries_the_sha256_of_the_prompt_that_was_sent(tmp_path: Path) -> None:
    """The same key `model.selected` records, so a span leads back to the exact prompt.

    Only a context carrying an `ArtifactStore` persists that prompt (THY-06); without one
    `record_prompt` never runs and the generation carries no sha256 rather than a fabricated one.
    """
    capture = _capture("prompt-sha")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    catalog = _coding_agent_catalog(tmp_path / "catalog")
    provider = ScriptedProvider(
        [LLMToolCall(id="c1", name="final_result", arguments={"row_count": 1, "columns": ["a"]})]
    )

    AgentRunner().run(
        catalog.get("coding"),
        Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="profile it"),
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=catalog,
            event_log=log,
            provider=provider,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        ),
    )

    attributes = _attributes(capture.spans(), "agent:code")
    recorded = str(attributes["langfuse.observation.metadata.prompt_sha256"])
    assert len(recorded) == 64
    selected = next(e for e in log.events() if e.type.value == "model.selected")
    assert selected.payload["prompt_sha256"] == recorded


def test_the_offered_tools_and_the_calls_use_the_shape_langfuse_parses(tmp_path: Path) -> None:
    """Langfuse fills its Available Tools and Tool Calls views by parsing known formats.

    Its own OpenAI integration records `{"tools": [...], "messages": [...]}` as the input and
    `{"role": ..., "tool_calls": [...]}` as the output (`langfuse/openai.py`). The same counts in
    custom metadata leave both views empty, so the shape is the contract — hence asserting on it.
    """
    capture = _capture("tool-shape")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    registry = ToolRegistry((_EchoTool(),))
    context = ToolContext(
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
    catalog = _coding_agent_catalog(tmp_path / "catalog")
    provider = ScriptedProvider(
        [
            LLMToolCall(id="c1", name="echo", arguments={"value": "hi"}),
            LLMToolCall(id="c2", name="final_result", arguments={"row_count": 1, "columns": ["a"]}),
        ]
    )
    AgentRunner().run(
        catalog.get("coding"),
        Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="echo it"),
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=catalog,
            event_log=log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    generations = [dict(s.attributes or {}) for s in capture.spans() if s.name == "agent:code"]
    inputs = [json.loads(str(a["langfuse.observation.input"])) for a in generations]
    outputs = [str(a.get("langfuse.observation.output") or "") for a in generations]

    offered = next(i for i in inputs if isinstance(i, dict))
    assert [tool["function"]["name"] for tool in offered["tools"]] == ["echo"]
    assert offered["messages"]
    # The structured-output tool is not a tool the agent chose, so it is offered as neither.
    assert "final_result" not in json.dumps(offered["tools"])

    called = json.loads(next(o for o in outputs if "tool_calls" in o))
    assert called["role"] == "assistant"
    assert [c["function"]["name"] for c in called["tool_calls"]] == ["echo"]
    assert called["tool_calls"][0]["type"] == "function"
    # Arguments travel as a JSON string, the way an OpenAI response carries them.
    assert json.loads(called["tool_calls"][0]["function"]["arguments"]) == {"value": "hi"}
    # The turn that produced the structured answer records that, and no tool call.
    assert any(
        a.get("langfuse.observation.metadata.answered_directly") is True for a in generations
    )


def test_the_observation_types_make_the_run_render_as_an_agent_graph(tmp_path: Path) -> None:
    """Langfuse infers the graph from types alone: it needs something other than span/generation."""
    capture = _capture("types")

    _drive_a_run(tmp_path)

    spans = capture.spans()
    types = {span.name: (span.attributes or {}).get("langfuse.observation.type") for span in spans}
    assert types["run"] == "span"
    assert types["thy"] == "agent"
    assert types["data"] == "agent"
    # Not `chain`, which Langfuse documents as a link between steps: MIRA's controls link nothing
    # and feed no model. `evaluator` is the closest documented fit, and the type only affects
    # filtering — so a reviewer filtering for chains is at least not told a lie.
    assert types["deterministic-controls"] == "evaluator"
    assert types["policy-review-findings"] == "guardrail"


def test_the_root_records_how_the_run_ended(tmp_path: Path) -> None:
    """Without this the trace list shows a prompt and a blank Output for every Run."""
    capture = _capture("outcome")

    _drive_a_run(tmp_path)

    output = _attributes(capture.spans(), "run")["langfuse.observation.output"]
    assert "decision" in output


def test_the_two_orchestrator_phases_record_what_they_produced(tmp_path: Path) -> None:
    """`thy` and `mira` are the coarsest breakdown of a Run; empty they tell a reviewer nothing.

    `thy`'s summary is written by `ThySubgraph.invoke`, not by the composing node: the adapter
    returns the runtime state unchanged, so `RuntimeState.plan` is empty at the composing node and
    anything derived from it there would report zero planned tasks for a Run that planned and ran
    several. The code that knows the outcome is the code that records it.
    """
    capture = _capture("phase-output")

    _drive_a_run(tmp_path)

    spans = capture.spans()
    thy = _attributes(spans, "thy")["langfuse.observation.output"]
    assert "summary" in thy
    assert "experiments" in thy
    assert "findings" in _attributes(spans, "mira")["langfuse.observation.output"]


def test_the_trace_carries_the_session_and_the_project_but_no_user(tmp_path: Path) -> None:
    capture = _capture("attributes")

    run = _drive_a_run(tmp_path)

    attributes = _attributes(capture.spans(), "run")
    assert attributes["session.id"] == run.session_id
    assert attributes["langfuse.trace.tags"] == (run.project_id,)
    assert "user.id" not in attributes


def test_no_span_carries_the_prompt_s_secret_or_address(tmp_path: Path) -> None:
    """The redaction boundary is the same one `EventLog.append` uses, applied before export."""
    capture = _capture("redaction")

    _drive_a_run(tmp_path)

    exported = str([dict(span.attributes or {}) for span in capture.spans()])
    assert _SECRET not in exported
    assert _EMAIL not in exported
    assert "[REDACTED:API_KEY]" in exported


# --------------------------------------------------------------------------- tool nesting


class _EchoTool(Tool):
    """A tool that always succeeds, so the span shape is the only thing under test."""

    name = "echo"
    description = "Echo a value."
    capability = ToolCapability(id="echo", external_effects=())
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue

    def execute(self, invocation: Any, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(success=True, stdout="ok", value=LegacyToolValue(text="ok"))


class _ToolThenAnswer(ScriptedProvider):
    """Ask for the tool on the first turn, answer on the second — one real agent loop."""


def test_a_tool_call_is_a_sibling_of_the_generation_that_requested_it(tmp_path: Path) -> None:
    """Langfuse's own rule: the tool nests under the orchestrating agent, beside the generation."""
    capture = _capture("tool-nesting")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    registry = ToolRegistry((_EchoTool(),))
    context = ToolContext(
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
    catalog = _coding_agent_catalog(tmp_path / "catalog")
    spec = catalog.get("coding")
    # One real agent loop: the model asks for `echo`, reads the result, then answers.
    provider = ScriptedProvider(
        [
            LLMToolCall(id="c1", name="echo", arguments={}),
            LLMToolCall(id="c2", name="final_result", arguments={"row_count": 1, "columns": ["a"]}),
        ]
    )
    ctx = AgentContext(
        route_policy=TEST_ROUTE_POLICY,
        catalog=catalog,
        event_log=log,
        provider=provider,
        tool_registry=registry,
        tool_context=context,
    )

    AgentRunner().run(
        spec,
        Task(id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="echo it"),
        ctx,
    )

    tree = capture.tree()
    assert tree["echo"] == "coding"
    assert tree["agent:code"] == "coding"


def _coding_agent_catalog(directory: Path) -> Any:
    """One `coding` agent allowed exactly the `echo` tool, so a real tool turn can be driven."""
    directory.mkdir(parents=True)
    (directory / "coding.yaml").write_text(
        "name: coding\n"
        "role: agent\n"
        "task_kinds: [code]\n"
        "tool_allowlist: [echo]\n"
        "max_turns: 3\n"
        "max_depth: 1\n"
        "system_prompt_ref: prompts/coding.md\n"
        "output_schema_ref: tests.thymira.fixtures_agent_output:DataProfileOutput\n",
        encoding="utf-8",
        newline="\n",
    )
    prompts = directory / "prompts"
    prompts.mkdir()
    (prompts / "coding.md").write_text("You are the coding agent.", encoding="utf-8", newline="\n")
    return load_agent_specs(directory, known_capabilities=frozenset({"echo"}))


# --------------------------------------------------------------------------- parallel fan-out


def test_a_parallel_wave_keeps_its_sub_agents_inside_the_run_s_trace(tmp_path: Path) -> None:
    """Sub-agents dispatched on a bare thread pool still belong to the Run that dispatched them.

    THY's parallel Execute wave submits to a raw `ThreadPoolExecutor`, which does not carry
    contextvars; without `bind_context` each worker would open a trace of its own.
    """
    capture = _capture("parallel")
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    catalog = _data_agent_catalog(tmp_path / "catalog")
    outputs = {
        "profile east": DataProfileOutput(row_count=1, columns=("east",)),
        "profile west": DataProfileOutput(row_count=2, columns=("west",)),
    }
    barrier = threading.Barrier(2, timeout=30)
    provider = _KeyedProvider(outputs, barrier=barrier)
    graph = build_thy_graph(
        catalog, log, provider=provider, max_concurrency=2, route_policy=TEST_ROUTE_POLICY
    )
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile east"
        ),
        AgentTask(
            id="t2", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile west"
        ),
    )
    run = Run(id=run_id, project_id=new_id("project"), session_id=new_id("session"), prompt="p")

    with observability.run_trace(
        run_id=run.id, session_id=run.session_id, project_id=run.project_id, prompt=run.prompt
    ):
        graph.invoke(ThyState(run=run, plan=plan))

    spans = capture.spans()
    # The barrier could only have released with both sub-agents genuinely in flight at once.
    assert not barrier.broken
    assert len({span.context.trace_id for span in spans}) == 1
    assert capture.tree()["data"] == "run"


class _KeyedProvider:
    """A thread-safe provider keyed by instruction, rendezvousing so both tasks really overlap."""

    provider_name = "test"
    model = "scripted"

    def __init__(
        self, outputs: Mapping[str, BaseModel], *, barrier: threading.Barrier | None = None
    ) -> None:
        self._outputs = outputs
        self._barrier = barrier

    def complete_structured(
        self, prompt: str, *, schema: type[BaseModel], system: str = ""
    ) -> tuple[BaseModel, Any]:
        del system
        key = next(candidate for candidate in self._outputs if candidate in prompt)
        if self._barrier is not None:
            self._barrier.wait()
        validated = schema.model_validate(self._outputs[key].model_dump())
        return validated, LLMResponse(
            text=validated.model_dump_json(), provider="test", model="scripted"
        )

    def complete(self, prompt: str, *, system: str = "") -> Any:
        raise AssertionError("complete is not used in this test")

    def complete_turn(
        self,
        messages: Any,
        *,
        tools: Any = (),
        parallel_tool_calls: bool | None = None,
    ) -> Any:
        del messages, tools, parallel_tool_calls
        raise AssertionError("complete_turn is not used in this test")


# --------------------------------------------------------------------------- failure paths


class _LeakyError(RuntimeError):
    """An exception whose message carries exactly what must never leave the process."""


def test_a_failing_body_exports_neither_the_exception_s_text_nor_a_stacktrace() -> None:
    """Closing an observation with the live exception is what OpenTelemetry expects, and it leaks.

    `use_span` defaults to `record_exception=True` and `set_status_on_exception=True`, the SDK
    overrides neither, and nothing redacts the result: `_redacted` and the SDK's `mask=` hook both
    reach `input`, `output` and `metadata` only, while the exporter copies `events=` and `status=`
    verbatim. A pydantic `ValidationError` out of a structured-output parse would be enough on its
    own to ship the model's raw answer, so the class name is all that may be recorded.
    """
    capture = _capture("failure")

    with pytest.raises(_LeakyError), observability.agent(name="worker", objective="work"):
        raise _LeakyError(f"the model answered {_SECRET} for {_EMAIL}")

    span = next(one for one in capture.spans() if one.name == "worker")
    attributes = dict(span.attributes or {})
    exported = f"{attributes}{list(span.events)}{span.status.description}"
    assert _SECRET not in exported
    assert _EMAIL not in exported
    assert list(span.events) == []
    assert attributes["langfuse.observation.level"] == "ERROR"
    assert attributes["langfuse.observation.status_message"] == "_LeakyError"


# --------------------------------------------------------------------------- trace provenance


def test_the_version_and_metadata_reach_every_observation_not_only_the_root() -> None:
    """Langfuse only counts an observation in an aggregation over an attribute it carries.

    A cost-by-version figure computed from a root that holds the version and generations that do
    not would be zero, which is why these are propagated rather than set on the root alone.
    """
    capture = _capture("provenance")

    with (
        observability.run_trace(
            run_id="run_v",
            session_id="session_v",
            project_id="project_v",
            prompt="hello",
            version="0f1e2d3c",
            metadata={"policy_sha256": "a" * 64},
        ),
        observability.phase("thy"),
        observability.generation(role="agent", task="analyze", input_="x", model="m"),
    ):
        pass

    spans = capture.spans()
    for name in ("run", "thy", "agent:analyze"):
        attributes = _attributes(spans, name)
        assert attributes["langfuse.version"] == "0f1e2d3c"
        assert attributes["langfuse.trace.metadata.policy_sha256"] == "a" * 64


def test_a_run_with_no_commit_simply_carries_no_version() -> None:
    """Outside a git repository provenance has nothing to report, which is not an error."""
    capture = _capture("no-version")

    with observability.run_trace(
        run_id="run_n", session_id="session_n", project_id="project_n", prompt="hello"
    ):
        pass

    assert "langfuse.version" not in _attributes(capture.spans(), "run")


def test_the_reasoning_effort_lands_on_model_parameters_not_in_metadata() -> None:
    """Langfuse serialises `model_parameters` onto its own attribute and can price a tier on it.

    A setting that changes both the answer and the bill belongs there, not in a JSON blob that
    nothing can key on.
    """
    capture = _capture("parameters")

    with observability.generation(
        role="agent",
        task="analyze",
        input_="x",
        model="m",
        model_parameters={"reasoning_effort": "high"},
    ):
        pass

    attributes = _attributes(capture.spans(), "agent:analyze")
    assert "high" in str(attributes["langfuse.observation.model.parameters"])
