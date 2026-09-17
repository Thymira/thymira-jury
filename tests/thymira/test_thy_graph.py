"""ThyGraph: compiles with LangGraph and runs Inspect -> Plan -> Execute -> Summarize (THY-09)."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from pypdf import PdfWriter

from tests.thymira.fixtures_agent_output import DataProfileOutput
from tests.thymira.fixtures_tools import EchoArguments, FakeTool, review_gated_context
from thymira.agents import AgentCatalog, AgentSpec, LLMToolCall
from thymira.agents.llm.routing import Role
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind, EventType, ModelRoutePolicy, Run, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import (
    AgentTask,
    ArtifactRequirement,
    ThyAgentKind,
    ThyInput,
    ThyOutput,
    ThyPhase,
    build_thy_graph,
    run_thy,
)
from thymira.thy.graph import graph_definition_hash
from thymira.thy.models import ThyState
from thymira.tools import ToolRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct build_thy_graph/run_thy provider seams to one code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _run() -> Run:
    return Run(
        id=new_id("run"), project_id=new_id("project"), session_id=new_id("session"), prompt="x"
    )


def _empty_catalog() -> AgentCatalog:
    return AgentCatalog()


def _valid_pdf_bytes() -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def test_build_thy_graph_compiles() -> None:
    graph = build_thy_graph(_empty_catalog(), InMemoryEventLog(new_id("run")))
    assert graph is not None


def test_run_thy_reports_missing_declared_artifacts_instead_of_success(
    tmp_path: Path,
) -> None:
    run = _run()
    event_log = InMemoryEventLog(run.id)
    task = AgentTask(
        id="deliver-report",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested report.",
        required_artifacts=(ArtifactRequirement(name="reports/result.md"),),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        event_log,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: reports/result.md"
    )
    assert output.required_artifacts == ("reports/result.md",)
    assert output.missing_artifacts == ("reports/result.md",)


def test_run_thy_signals_execute_before_post_execution_validation_failure() -> None:
    """The Execute boundary is observable even when final deliverable validation fails."""
    run = _run()
    task = AgentTask(
        id="missing-deliverable",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the required report.",
        required_artifacts=(ArtifactRequirement(name="reports/result.md"),),
    )
    execute_entries: list[str] = []

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        InMemoryEventLog(run.id),
        before_execute=lambda: execute_entries.append(run.id),
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: reports/result.md"
    )
    assert execute_entries == [run.id]


def test_run_thy_resolves_semantic_deliverables_by_declared_kind(tmp_path: Path) -> None:
    """A plan's semantic deliverable names resolve to the run's typed artifacts."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    artifacts = (
        store.save_bytes(
            "baseline.joblib",
            b"model",
            produced_by=run.id,
            kind=ArtifactKind.MODEL,
        ),
        store.save_json(
            "metrics.json",
            {"accuracy": 0.8},
            produced_by=run.id,
            kind=ArtifactKind.METRICS,
        ),
        store.save_text(
            "pipeline.py",
            "pipeline",
            produced_by=run.id,
            kind=ArtifactKind.CODE,
        ),
        store.save_text(
            "report.md",
            "report",
            produced_by=run.id,
            kind=ArtifactKind.REPORT,
        ),
    )
    task = AgentTask(
        id="deliver-model",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Use the existing deliverables.",
        required_artifacts=(
            ArtifactRequirement(name="trained model", kind=ArtifactKind.MODEL),
            ArtifactRequirement(name="preprocessing pipeline", kind=ArtifactKind.CODE),
            ArtifactRequirement(name="evaluation metrics", kind=ArtifactKind.METRICS),
            ArtifactRequirement(name="report", kind=ArtifactKind.REPORT),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _data_agent_catalog(),
        log,
        provider=ScriptedProvider([DataProfileOutput(row_count=1, columns=("a",))]),
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,), artifacts=artifacts),
        route_policy=TEST_ROUTE_POLICY,
    )

    assert output.error is None
    assert output.missing_artifacts == ()


def test_run_thy_does_not_use_a_json_report_for_an_explicit_pdf_requirement(
    tmp_path: Path,
) -> None:
    """A same-kind evidence JSON is not the named PDF deliverable."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_json(
        "report_facts.json",
        {"row_count": 998},
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="german_credit_eda_report.pdf",
                kind=ArtifactKind.REPORT,
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: german_credit_eda_report.pdf"
    )
    assert output.missing_artifacts == ("german_credit_eda_report.pdf",)


def test_run_thy_checks_the_media_type_of_an_exactly_named_requirement(tmp_path: Path) -> None:
    """An exact logical name cannot conceal the wrong content type."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_json(
        "german_credit_eda_report.pdf",
        {"row_count": 998},
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="german_credit_eda_report.pdf",
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: german_credit_eda_report.pdf"
    )
    assert output.missing_artifacts == ("german_credit_eda_report.pdf",)


def test_run_thy_infers_required_media_type_from_an_explicit_filename(tmp_path: Path) -> None:
    """An omitted requirement MIME cannot let JSON impersonate an exactly named PDF."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_json(
        "german_credit_eda_report.pdf",
        {"not": "a PDF"},
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="german_credit_eda_report.pdf",
                kind=ArtifactKind.REPORT,
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: german_credit_eda_report.pdf"
    )
    assert output.missing_artifacts == ("german_credit_eda_report.pdf",)


@pytest.mark.parametrize("declared_media_type", ["application/json", " ", "not-a-mime"])
def test_run_thy_rejects_a_required_mime_that_contradicts_the_filename(
    tmp_path: Path,
    declared_media_type: str,
) -> None:
    """Plan metadata cannot override the stable MIME implied by an explicit filename."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_json(
        "report.pdf",
        {"not": "a PDF"},
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="report.pdf",
                kind=ArtifactKind.REPORT,
                media_type=declared_media_type,
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: report.pdf"
    )
    assert output.missing_artifacts == ("report.pdf",)


def test_run_thy_accepts_the_exact_artifact_with_the_required_kind_and_media_type(
    tmp_path: Path,
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_bytes(
        "german_credit_eda_report.pdf",
        _valid_pdf_bytes(),
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="Application/PDF; version=1.4",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="german_credit_eda_report.pdf",
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error is None
    assert output.missing_artifacts == ()


def test_run_thy_rejects_invalid_pdf_bytes_even_when_metadata_matches(tmp_path: Path) -> None:
    """Completion independently checks bytes instead of trusting a tool's Artifact metadata."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_bytes(
        "report.pdf",
        b"%PDF-1.4\n",
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="report.pdf",
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: report.pdf"
    )
    assert output.missing_artifacts == ("report.pdf",)


def test_run_thy_rejects_conflicting_contracts_for_the_same_artifact_name() -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    tasks = (
        AgentTask(
            id="pdf-contract",
            agent=ThyAgentKind.DATA,
            phase=ThyPhase.EXECUTE,
            instruction="Create a PDF.",
            required_artifacts=(
                ArtifactRequirement(
                    name="report.pdf",
                    kind=ArtifactKind.REPORT,
                    media_type="application/pdf",
                ),
            ),
        ),
        AgentTask(
            id="json-contract",
            agent=ThyAgentKind.DATA,
            phase=ThyPhase.EXECUTE,
            instruction="Create JSON under the same name.",
            required_artifacts=(
                ArtifactRequirement(
                    name="report.pdf",
                    kind=ArtifactKind.REPORT,
                    media_type="application/json",
                ),
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        initial_state=ThyState(run=run, plan=tasks),
    )

    assert output.error == "conflicting artifact requirements: report.pdf"
    assert output.missing_artifacts == ("report.pdf",)


def test_run_thy_does_not_mix_stale_metadata_with_a_new_artifact_revision(
    tmp_path: Path,
) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    old_pdf = store.save_bytes(
        "report.pdf",
        _valid_pdf_bytes(),
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    store.save_json(
        "report.pdf",
        {"replacement": True},
        produced_by=run.id,
        kind=ArtifactKind.OTHER,
        media_type="application/json",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="report.pdf",
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,), artifacts=(old_pdf,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: report.pdf"
    )
    assert output.missing_artifacts == ("report.pdf",)


def test_run_thy_does_not_semantically_match_a_requirement_with_a_media_type(
    tmp_path: Path,
) -> None:
    """A declared MIME type makes an extensionless requirement an exact identity."""
    run = _run()
    log = InMemoryEventLog(run.id)
    store = LocalArtifactStore(tmp_path / "artifacts", run.id)
    store.save_bytes(
        "different-report.pdf",
        b"%PDF-1.4\n",
        produced_by=run.id,
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    task = AgentTask(
        id="deliver-pdf",
        agent=ThyAgentKind.DATA,
        phase=ThyPhase.EXECUTE,
        instruction="Create the requested PDF.",
        required_artifacts=(
            ArtifactRequirement(
                name="final report",
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
            ),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        _empty_catalog(),
        log,
        artifact_store=store,
        initial_state=ThyState(run=run, plan=(task,)),
    )

    assert output.error == (
        "data failed: unknown agent 'data': this run's catalog registers no agents; "
        "required artifacts missing: final report"
    )
    assert output.missing_artifacts == ("final report",)


def test_run_thy_reaches_summarize() -> None:
    output = run_thy(ThyInput(run=_run()), _empty_catalog(), InMemoryEventLog(new_id("run")))
    assert isinstance(output, ThyOutput)
    assert output.summary == "not yet implemented"
    assert output.error is None


def test_run_thy_output_is_keyed_to_the_input_run() -> None:
    run = _run()
    output = run_thy(ThyInput(run=run), _empty_catalog(), InMemoryEventLog(run.id))
    assert output.run_id == run.id


def test_invoking_the_compiled_graph_directly_advances_every_phase() -> None:
    graph = build_thy_graph(_empty_catalog(), InMemoryEventLog(new_id("run")))
    raw_result = graph.invoke(ThyState(run=_run()))
    final_state = ThyState.model_validate(raw_result)
    assert final_state.phase is ThyPhase.SUMMARIZE


def test_run_thy_reuses_a_precompiled_graph_when_passed_one() -> None:
    catalog = _empty_catalog()
    log = InMemoryEventLog(new_id("run"))
    graph = build_thy_graph(catalog, log)
    output = run_thy(ThyInput(run=_run()), catalog, log, graph=graph)
    assert output.summary == "not yet implemented"


def _data_agent_catalog(*, tool_allowlist: tuple[str, ...] = ()) -> AgentCatalog:
    spec = AgentSpec(
        name="data",
        role=Role.AGENT,
        task_kinds=("analyze",),
        tool_allowlist=tool_allowlist,
        max_turns=2,
        max_depth=1,
        system_prompt_ref="p",
        output_schema_ref="s",
    )
    return AgentCatalog(
        (spec,),
        system_prompts={"data": "You are the data agent."},
        output_schemas={"data": DataProfileOutput},
    )


def test_a_run_driven_by_scripted_agents_traverses_every_phase_and_yields_a_thy_output() -> None:
    catalog = _data_agent_catalog()
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = ScriptedProvider([DataProfileOutput(row_count=7, columns=("age",))])
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )
    graph = build_thy_graph(catalog, log, provider=provider, route_policy=TEST_ROUTE_POLICY)

    raw_result = graph.invoke(ThyState(run=run, plan=plan))
    final_state = ThyState.model_validate(raw_result)

    assert final_state.phase is ThyPhase.SUMMARIZE
    assert len(final_state.completed) == 1
    assert len(final_state.agent_messages) == 1
    assert final_state.agent_messages[0].status is TaskStatus.COMPLETED
    assert final_state.usage.requests == 1

    output = ThyOutput(run_id=final_state.run.id, summary=final_state.summary)
    assert isinstance(output, ThyOutput)
    message_events = [e for e in log.events() if e.type == EventType.AGENT_MESSAGE]
    assert len(message_events) == 1


def test_run_thy_prefixes_missing_artifacts_with_the_first_failed_tasks_diagnosis() -> None:
    """The halt message names the real cause, not just the deliverable check it also failed.

    A registered agent's own failure (here, ``max_turns`` exhausted) is the real cause.
    """
    catalog = _data_agent_catalog()
    run = _run()
    log = InMemoryEventLog(run.id)
    provider = ScriptedProvider([])  # exhausted on the agent's very first call
    plan = (
        AgentTask(
            id="t1",
            agent=ThyAgentKind.DATA,
            phase=ThyPhase.EXECUTE,
            instruction="profile it",
            required_artifacts=(ArtifactRequirement(name="profile.json"),),
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        catalog,
        log,
        provider=provider,
        route_policy=TEST_ROUTE_POLICY,
        initial_state=ThyState(run=run, plan=plan),
    )

    assert output.error == (
        "data failed: agent exceeded max_turns; required artifacts missing: profile.json"
    )
    assert output.missing_artifacts == ("profile.json",)


def test_graph_definition_hash_is_stable() -> None:
    assert graph_definition_hash() == graph_definition_hash()


def test_graph_definition_hash_changes_when_a_node_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import thymira.thy.graph as graph_module

    before = graph_definition_hash()
    monkeypatch.setattr(graph_module, "_GRAPH_NODES", (*graph_module._GRAPH_NODES, "extra"))
    after = graph_definition_hash()

    assert before != after


def test_run_thy_hands_back_progress_only_when_a_task_awaits_a_human(tmp_path: Path) -> None:
    run = _run()
    log = InMemoryEventLog(run.id)
    catalog = _data_agent_catalog(tool_allowlist=("echo",))
    echo = FakeTool(
        "echo", ToolCapability(id="echo", external_effects=()), arguments_model=EchoArguments
    )
    provider = ScriptedProvider([LLMToolCall(id="c1", name="echo", arguments={"value": "x"})])
    plan = (
        AgentTask(
            id="t1", agent=ThyAgentKind.DATA, phase=ThyPhase.EXECUTE, instruction="profile it"
        ),
    )

    output = run_thy(
        ThyInput(run=run),
        catalog,
        log,
        provider=provider,
        tool_registry=ToolRegistry((echo,)),
        tool_context=review_gated_context(tmp_path, run_id=run.id, log=log),
        initial_state=ThyState(run=run, plan=plan),
        route_policy=TEST_ROUTE_POLICY,
    )

    assert output.error is None
    assert output.progress is not None
    assert output.progress.plan == plan
    assert [m.status for m in output.progress.agent_messages] == [TaskStatus.PENDING]

    plain_output = run_thy(ThyInput(run=_run()), _empty_catalog(), InMemoryEventLog(new_id("run")))
    assert plain_output.progress is None


class _CallerState(BaseModel):
    """The state of a graph that invokes ThyGraph from one of its nodes."""

    run_id: str


def test_thy_owns_no_checkpoint_of_the_graph_that_invokes_it() -> None:
    """ThyGraph inside another graph's node joins neither its checkpointer nor its namespace.

    Compiled with LangGraph's default, an inner graph borrows the caller's checkpointer: a
    resumed caller then replays a checkpoint of its own into ThyState's channels and the pass
    dies there. THY seeds every pass explicitly through ``run_thy(initial_state=...)``, so it must
    stay outside the caller's persistence -- including under ``durability="sync"``, which the Core
    dispatcher uses and which reaches into checkpoint state the inner graph does not have.
    """
    run = _run()
    saver = InMemorySaver()
    outputs: list[ThyOutput] = []

    def call_thy(state: _CallerState) -> _CallerState:
        """Run one THY pass from inside the caller's node."""
        outputs.append(run_thy(ThyInput(run=run), _empty_catalog(), InMemoryEventLog(run.id)))
        return state

    builder = StateGraph(_CallerState)
    builder.add_node("thy", call_thy)
    builder.add_edge(START, "thy")
    builder.add_edge("thy", END)
    caller = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": run.id}}

    caller.invoke(_CallerState(run_id=run.id), config, durability="sync")
    caller.invoke(_CallerState(run_id=run.id), config, durability="sync")

    assert [output.error for output in outputs] == [None, None]
    assert [output.run_id for output in outputs] == [run.id, run.id]
    namespaces = {saved.config["configurable"].get("checkpoint_ns") for saved in saver.list(None)}
    assert namespaces == {""}
