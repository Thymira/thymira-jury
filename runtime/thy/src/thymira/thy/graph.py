"""ThyGraph: the LangGraph wiring of Inspect -> Plan -> Execute -> (Rework) -> Summarize.

The four work nodes are real. Inspect (THY-10) seeds `ThyState` from `load_project_context` when
`project_dir` is given, and also registers the project's declared datasets when `artifact_store`
is given; Plan (THY-11) calls THY's own (FRONTIER, task='plan') model and gates the
result through `gate.check_action('plan.proposed')` when `gate` is given; Execute (THY-09/THY-12)
dispatches every `AgentTask` in `state.plan` through `Delegator` (THY-08) in dependency order, and
runs independent tasks concurrently when `max_concurrency` is set (THY-23); Summarize (THY-13)
compares `state.experiments`, calls THY's own (FRONTIER, task='synthesize') model for the narrative,
and writes the report artifact when `artifact_store` is given. Each of the four degrades to its bare
`state.advance()` placeholder when its own dependency (`project_dir`, `gate`, `artifact_store`) is
omitted, so every existing caller that never passed one keeps its exact original behavior.

Rework (THY-24) is the fifth node and the graph's only cycle: when a `policy.decision` asked THY to
reopen an earlier phase (`state.rework`), `_route_after_execute` visits it instead of Summarize, and
`rework_node` either reopens (looping back into Execute, within a budget) or refuses (moving on to
Summarize). With no rework signal the node is never entered -- MVP runs never set one.

Inspect, Plan, Execute and Rework are the nodes whose outcome steers the graph:
`_route_after_inspect` sends a failed intake straight to END instead of Plan -- a project whose
declared data is unusable never runs at all -- exactly as `_route_after_plan` sends a rejected
plan straight to END instead of Execute -- nothing ran yet, so there is nothing to audit.
`_route_after_execute` never does that: a task failure is recorded evidence
(`ThyState.agent_messages`), not a reason to hide the Run from MIRA (bug-hunt C1), so it only ever
chooses between a rework-signalled run (Rework) and Summarize -- except a task left PENDING (a tool
call waiting for a human, THY-17), which routes straight to END before Summarize instead: the pass
is knowingly incomplete, and `run_thy` hands the Core a `ThyProgress` to resume it with.
`_route_after_rework` loops an allowed reopen back to Execute and sends a refused one to
Summarize -- all via `add_conditional_edges`, not plain edges. `graph_definition_hash()` folds
`_CONDITIONAL_EDGES` in alongside `_GRAPH_NODES` and `_GRAPH_EDGES`, so every branch is part of
what "changes when a node is added" covers.

LangGraph returns a plain ``dict`` from ``invoke()`` even when the state schema is a pydantic
model (verified empirically, not assumed): ``run_thy`` rebuilds a typed `ThyState`/`ThyOutput`
so callers in `thymira.core` never see that dict.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from thymira.events import canonical_json, sha256_bytes, sha256_text
from thymira.schemas import ArtifactKind, TaskStatus
from thymira.thy.budget import DEFAULT_CONTEXT_WINDOW, ContextBudget
from thymira.thy.models import ArtifactRequirement, ThyInput, ThyOutput, ThyProgress, ThyState
from thymira.thy.nodes.execute import execute_node
from thymira.thy.nodes.inspect import inspect_node
from thymira.thy.nodes.plan import plan_node
from thymira.thy.nodes.rework import rework_node
from thymira.thy.nodes.summarize import summarize_node
from thymira.tools import (
    artifact_read_limit,
    resolve_artifact_media_type,
    validate_artifact_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph

    from thymira.agents import AgentCatalog, RuntimeSkillCatalog
    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.events import EventLog
    from thymira.policies import Gate
    from thymira.schemas import Artifact, ModelRoutePolicy
    from thymira.state import ArtifactStore
    from thymira.tools import ToolContext, ToolRegistry

_GRAPH_NODES: tuple[str, ...] = ("inspect", "plan", "execute", "rework", "summarize")
_GRAPH_EDGES: tuple[tuple[str, str], ...] = (
    (START, "inspect"),
    ("summarize", END),
)
_CONDITIONAL_EDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inspect", ("plan", END)),
    ("plan", ("execute", END)),
    ("execute", ("rework", "summarize", END)),
    ("rework", ("execute", "summarize")),
)


def graph_definition_hash() -> str:
    """A stable digest of ThyGraph's structure, changing whenever a node or edge does."""
    definition = {
        "nodes": list(_GRAPH_NODES),
        "edges": [list(edge) for edge in _GRAPH_EDGES],
        "conditional_edges": [[source, list(targets)] for source, targets in _CONDITIONAL_EDGES],
    }
    return sha256_text(canonical_json(definition))


def _artifact_for_requirement(
    name: str,
    kind: ArtifactKind,
    media_type: str | None,
    artifacts: Sequence[Artifact],
    used_ids: set[str],
) -> Artifact | None:
    """Resolve one delivery by exact file identity first, then by semantic typed kind."""
    try:
        required_media_type = resolve_artifact_media_type(name, media_type)
    except ValueError:
        return None
    exact = next(
        (
            artifact
            for artifact in artifacts
            if artifact.id not in used_ids
            and artifact.valid
            and artifact.name == name
            and (kind is ArtifactKind.OTHER or artifact.kind is kind)
            and _media_type_matches(required_media_type, artifact.media_type)
        ),
        None,
    )
    if exact is not None:
        return exact
    if (
        kind is ArtifactKind.OTHER
        or required_media_type is not None
        or "/" in name
        or "\\" in name
        or PurePosixPath(name).suffix
    ):
        return None

    candidates = [
        artifact
        for artifact in artifacts
        if artifact.id not in used_ids
        and artifact.kind is kind
        and artifact.valid
        and _media_type_matches(required_media_type, artifact.media_type)
    ]
    if not candidates:
        return None
    words = set(name.lower().replace("_", " ").split())
    aliases = {
        ArtifactKind.CODE: {"pipeline", "preprocess", "preprocessing", "encoder", "code"},
        ArtifactKind.MODEL: {"model", "classifier", "estimator"},
        ArtifactKind.METRICS: {"metric", "metrics", "evaluation", "summary"},
        ArtifactKind.REPORT: {"report", "analysis", "summary"},
    }.get(kind, set())
    ranked = sorted(
        candidates,
        key=lambda artifact: (
            -sum(1 for word in aliases if word in words and word in artifact.name.lower()),
            artifact.name,
        ),
    )
    score = sum(1 for word in aliases if word in words and word in ranked[0].name.lower())
    if score == 0 and len(candidates) != 1:
        return None
    return ranked[0]


def _media_type_matches(required: str | None, actual: str | None) -> bool:
    """Match MIME types case-insensitively while ignoring optional parameters."""
    if required is None:
        return True
    if actual is None:
        return False
    required_base = required.partition(";")[0].strip().casefold()
    actual_base = actual.partition(";")[0].strip().casefold()
    return required_base == actual_base


def _first_failed_task_diagnosis(final_state: ThyState) -> str | None:
    """Return "<kind> failed: <diagnostics>" for the plan's first FAILED task, in plan order.

    `final_state.completed` (the plan's `AgentTask`s) and `final_state.agent_messages` (their
    settled `Task` outcomes) are appended together, one pair per occurrence, by every Execute
    dispatch path (sequential and parallel alike) -- so the two tuples stay index-aligned and
    zipping them recovers which plan-declared agent kind produced a given outcome. `None` when
    no task failed, so a run that only fell short on the deliverable check is not blamed on a
    task that actually completed.
    """
    for agent_task, outcome in zip(final_state.completed, final_state.agent_messages, strict=False):
        if outcome.status is TaskStatus.FAILED:
            reason = outcome.error or "no diagnostics recorded"
            return f"{agent_task.agent.value} failed: {reason}"
    return None


def _artifact_content_matches_requirement(  # noqa: PLR0911  # every mismatch fails closed
    artifact: Artifact,
    *,
    requirement_name: str,
    requirement_media_type: str | None,
    artifact_store: ArtifactStore | None,
) -> bool:
    """Revalidate typed bytes independently of the tool or plugin that published metadata."""
    try:
        required_media_type = resolve_artifact_media_type(requirement_name, requirement_media_type)
        actual_media_type = resolve_artifact_media_type(artifact.name, artifact.media_type)
    except ValueError:
        return False
    if artifact_store is None:
        return required_media_type is None
    if required_media_type is not None and actual_media_type != required_media_type:
        return False
    try:
        current = artifact_store.get(artifact.name)
        if (
            current is None
            or not current.valid
            or current.id != artifact.id
            or current.sha256 != artifact.sha256
        ):
            return False
        validation_media_type = required_media_type or actual_media_type
        data = artifact_store.load_bytes_bounded(
            artifact.name, artifact_read_limit(validation_media_type)
        )
        if sha256_bytes(data) != artifact.sha256:
            return False
        validate_artifact_bytes(artifact.name, data, validation_media_type)
    except (KeyError, OSError, ValueError):
        return False
    return True


def _route_after_inspect(state: ThyState) -> str:
    """Plan only when the intake succeeded; a project whose declared data is unusable ends here."""
    return "plan" if state.error is None else END


def _route_after_plan(state: ThyState) -> str:
    """Execute only when the plan's decision allows execution; a halted plan goes to END."""
    return "execute" if state.error is None else END


def _route_after_execute(state: ThyState) -> str:
    """Route after Execute: a pending task ends the graph, a rework reopens, otherwise Summarize.

    A task failure never halts here (bug-hunt C1): it is already recorded on
    `state.agent_messages`, so MIRA and the Policy Engine get to see and decide on it instead of
    the graph unilaterally discarding the Run. A `ReworkSignal` (`state.rework`) means a
    `policy.decision` asked THY to reopen a phase (THY-24), so the graph visits `rework` to decide
    whether the reopen is allowed; with no signal it proceeds straight to Summarize. A task left
    PENDING -- a tool call waiting for a human -- ends the graph before Summarize instead: the
    pass is knowingly incomplete, and the Core carries `ThyOutput.progress` into the pass that
    finishes it.
    """
    if state.awaiting_approval():
        return END
    if state.rework is not None:
        return "rework"
    return "summarize"


def _route_after_rework(state: ThyState) -> str:
    """Route after Rework: an allowed reopen re-enters Execute, a refused one moves to Summarize.

    `rework_node` keeps `state.rework` set when it reopened (so the graph loops back through
    Execute) and clears it when it refused (budget exhausted or a non-reopenable target), so the
    presence of the signal is exactly the "loop again" decision -- bounded by the budget, never
    infinite. A Core-authorized single rework clears the signal after the current Execute pass and
    therefore proceeds to Summarize without an extra duplicate execution.
    """
    return "execute" if state.rework is not None else "summarize"


def _inspect(state: ThyState) -> dict[str, Any]:
    """Fallback when `build_thy_graph` gets no `project_dir`: no project to load.

    Returns the field values themselves (``dict(...)``), not a JSON-shaped ``model_dump()``: the
    latter recursively serialises a non-`ThymiraModel` field such as `state.usage` (the mutable,
    run-scoped `RunUsage` shared by reference for the whole run) into a plain dict, so LangGraph
    would reconstruct a brand-new `RunUsage` for the next node instead of carrying the same
    accumulator forward, silently detaching every later charge from the caller's instance.
    """
    return dict(state.advance())


def _plan(state: ThyState) -> dict[str, Any]:
    """Fallback when `build_thy_graph` gets no `gate`: nothing to gate the plan through.

    See `_inspect` for why this returns field values (``dict(...)``) rather than `model_dump()`.
    """
    return dict(state.advance())


def _summarize(state: ThyState) -> dict[str, Any]:
    """Fallback when `build_thy_graph` gets no `artifact_store`: nowhere to write the report.

    Terminal phase: unlike the other three nodes, this one does not call ``advance()`` —
    SUMMARIZE is the last phase in `THY_PHASE_ORDER`, and advancing past it raises. See
    `_inspect` for why this returns field values (``dict(...)``) rather than `model_dump()`.
    """
    return dict(state.model_copy(update={"summary": state.summary or "not yet implemented"}))


def build_thy_graph(
    catalog: AgentCatalog,
    event_log: EventLog,
    *,
    provider: LLMProvider | None = None,
    project_dir: Path | None = None,
    gate: Gate | None = None,
    artifact_store: ArtifactStore | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    runtime_skill_budget: int = 32_000,
    runtime_skill_names: tuple[str, ...] = (),
    metric: str = "accuracy",
    max_concurrency: int | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_execute: Callable[[], None] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    context_budget: ContextBudget | None = None,
    route_policy: ModelRoutePolicy | None = None,
    request_ledger: RequestLedger | None = None,
) -> CompiledStateGraph:
    """Build and compile ThyGraph: START -> inspect -> plan -> execute -> summarize -> END.

    Args:
        catalog: Resolves each `AgentTask.agent` (by `ThyAgentKind.value`) to the `AgentSpec`
            Execute delegates to.
        event_log: Where every delegated call's events land.
        provider: Overrides the router-resolved provider for every delegated call — tests inject
            a `ScriptedProvider` here so no network call happens. Production omits it.
        project_dir: The project Inspect loads `.thymira/config.yaml`/`context.md` from. Omitted,
            Inspect stays the bare phase-advance placeholder — no project to load.
        gate: Routes THY's plan through `Gate.check_action('plan.proposed')`. Omitted, Plan stays
            the bare phase-advance placeholder — nothing to gate.
        artifact_store: Where Summarize writes `analysis.md` and where Inspect registers the
            project's declared datasets. Omitted, Summarize stays the bare phase-advance
            placeholder — nowhere to write the report.
        tool_registry: The tools a delegated agent may call in Execute. Given together with
            `tool_context`, exactly `spec.tool_allowlist` is exposed through the Tool Manager +
            Gate; omitted, delegated agents run with no tools.
        tool_context: The runtime-owned tool context Execute passes to each delegated call, and
            whose `ArtifactStore` it diffs to fold produced artifacts and experiments back onto
            `ThyState`. Pair its `ArtifactStore`/`event_log` with the ones given here.
        runtime_skill_catalog: Optional THY runtime catalog. Names and descriptions are added to
            Plan; selected bodies and references are loaded by Execute after name selection.
        runtime_skill_budget: Maximum UTF-8 bytes in one selected runtime-skill projection.
        runtime_skill_names: Names selected for delegated tasks that do not carry task-specific
            runtime skill names.
        metric: The metric Summarize ranks `state.experiments` by.
        max_concurrency: How many independent tasks Execute may run at once (THY-23). Omitted (or
            ``< 2``), Execute dispatches sequentially, exactly as before; concurrency is engaged
            only when no `tool_context` is given.
        before_model_request: Optional owner-bound hook invoked before each THY provider request.
        before_execute: Optional owner-bound hook invoked at the Plan -> Execute boundary.
        before_model_selection: Optional Core budget check before routing each model call.
        before_model_call: Optional Core Gate check after routing and before provider invocation.
        record_model_usage: Optional Core hook for charging measured provider usage.
        context_budget: runtime context cap used by Execute's agent provider calls; omitted, the
            provider-neutral default cap is used. It is a safety budget, not an inferred model
            maximum; callers with a configured context setting should pass it explicitly.
        before_model_request: Optional owner-bound hook invoked before each THY provider request.
        request_ledger: Optional request ledger shared by delegated providers in this Run.
        route_policy: Immutable session allowlist checked before provider invocation.
    """
    if context_budget is None:
        context_budget = ContextBudget(window=DEFAULT_CONTEXT_WINDOW)
    builder = StateGraph(ThyState)
    builder.add_node(
        "inspect",
        inspect_node(project_dir, artifact_store=artifact_store)
        if project_dir is not None
        else _inspect,
    )
    builder.add_node(
        "plan",
        plan_node(
            catalog,
            gate,
            event_log,
            provider=provider,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
            request_ledger=request_ledger,
            runtime_skill_catalog=runtime_skill_catalog,
        )
        if gate is not None
        else _plan,
    )
    builder.add_node(
        "execute",
        execute_node(
            catalog,
            event_log,
            provider,
            tool_registry=tool_registry,
            tool_context=tool_context,
            runtime_skill_catalog=runtime_skill_catalog,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=runtime_skill_names,
            max_concurrency=max_concurrency,
            before_model_request=before_model_request,
            before_execute=before_execute,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            context_budget=context_budget,
            route_policy=route_policy,
            request_ledger=request_ledger,
        ),
    )
    builder.add_node("rework", rework_node())
    builder.add_node(
        "summarize",
        summarize_node(
            artifact_store,
            event_log,
            provider=provider,
            metric=metric,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
        )
        if artifact_store is not None
        else _summarize,
    )
    for source, target in _GRAPH_EDGES:
        builder.add_edge(source, target)
    builder.add_conditional_edges("inspect", _route_after_inspect, ["plan", END])
    builder.add_conditional_edges("plan", _route_after_plan)
    builder.add_conditional_edges("execute", _route_after_execute)
    builder.add_conditional_edges("rework", _route_after_rework)
    # THY owns no durable LangGraph state and must never inherit the composition's: durability is
    # the Core's, at the boundary of its own `thy` node (see `run_thy`).
    return builder.compile(checkpointer=False)


def run_thy(
    thy_input: ThyInput,
    catalog: AgentCatalog,
    event_log: EventLog,
    *,
    provider: LLMProvider | None = None,
    project_dir: Path | None = None,
    gate: Gate | None = None,
    artifact_store: ArtifactStore | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    runtime_skill_budget: int = 32_000,
    runtime_skill_names: tuple[str, ...] = (),
    metric: str = "accuracy",
    max_concurrency: int | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_execute: Callable[[], None] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    context_budget: ContextBudget | None = None,
    route_policy: ModelRoutePolicy | None = None,
    request_ledger: RequestLedger | None = None,
    initial_state: ThyState | None = None,
    graph: CompiledStateGraph | None = None,
) -> ThyOutput:
    """Run ThyGraph over `thy_input.run` and return a typed `ThyOutput`.

    Rebuilds the graph on every call unless `graph` is passed in (tests, or a caller that wants
    to compile once and reuse it) — `build_thy_graph()` has no side effects, so both are safe.
    `max_concurrency` is threaded onto Execute's fan-out cap (THY-23; see `build_thy_graph`).
    `initial_state` lets a caller seed the run before it starts -- a recipe (THY-28) that pre-loads
    the plan and per-run usage budget, for example; its `run` must be `thy_input.run`, and omitted a
    bare `ThyState(run=...)` is used exactly as before. It is also how the Core seeds a resumed
    pass: a `ThyProgress` from an earlier `ThyOutput.progress`, folded back onto a fresh `ThyState`
    so Plan short-circuits on the seeded plan and Execute re-runs only the task that asked.

    Raises:
        ValueError: `initial_state` is for a different Run than `thy_input.run`.
    """
    compiled = graph or build_thy_graph(
        catalog,
        event_log,
        provider=provider,
        project_dir=project_dir,
        gate=gate,
        artifact_store=artifact_store,
        tool_registry=tool_registry,
        tool_context=tool_context,
        runtime_skill_catalog=runtime_skill_catalog,
        runtime_skill_budget=runtime_skill_budget,
        runtime_skill_names=runtime_skill_names,
        metric=metric,
        max_concurrency=max_concurrency,
        before_model_request=before_model_request,
        before_execute=before_execute,
        before_model_selection=before_model_selection,
        before_model_call=before_model_call,
        record_model_usage=record_model_usage,
        context_budget=context_budget,
        route_policy=route_policy,
        request_ledger=request_ledger,
    )
    start_state = (
        initial_state
        if initial_state is not None
        else ThyState(run=thy_input.run, execution_constraints=thy_input.execution_constraints)
    )
    if start_state.run.id != thy_input.run.id:
        msg = "initial_state.run must be the same Run as thy_input.run"
        raise ValueError(msg)
    # Invoke at THY's own root, never inside the composition's Pregel context. This explicit
    # checkpoint coordinate is the load-bearing half: passing a `configurable` of our own makes
    # LangGraph drop the *inherited* ambient one -- checkpointer, namespace and `durability="sync"`
    # alike -- so every pass starts from `start_state`, which is what makes a seeded resume
    # deterministic. Without it, a ThyGraph running inside the Core's `thy` node borrows the
    # parent's checkpointer and a resumed composition replays the *parent's* checkpoint into
    # ThyGraph's channels; and the inherited `durability="sync"` crashes a graph that has no
    # checkpointer on the *first* pass with `AttributeError: _put_checkpoint_fut`. The
    # `checkpointer=False` at compile time (see `_build`) is belt-and-braces on top of that.
    raw_result = compiled.invoke(start_state, {"configurable": {"thread_id": thy_input.run.id}})
    final_state = ThyState.model_validate(raw_result)
    requirement_map: dict[str, ArtifactRequirement] = {}
    conflicting_requirements: list[str] = []
    for task in final_state.plan:
        for requirement in task.required_artifacts:
            previous = requirement_map.get(requirement.name)
            if previous is not None and previous != requirement:
                if requirement.name not in conflicting_requirements:
                    conflicting_requirements.append(requirement.name)
                continue
            requirement_map[requirement.name] = requirement
    requirements = tuple(requirement_map)
    available = (
        artifact_store.list_active() if artifact_store is not None else list(final_state.artifacts)
    )
    used_ids: set[str] = set()
    missing: list[str] = list(conflicting_requirements)
    for name, requirement in requirement_map.items():
        if name in conflicting_requirements:
            continue
        resolved = _artifact_for_requirement(
            name,
            requirement.kind,
            requirement.media_type,
            available,
            used_ids,
        )
        if resolved is None or not _artifact_content_matches_requirement(
            resolved,
            requirement_name=name,
            requirement_media_type=requirement.media_type,
            artifact_store=artifact_store,
        ):
            missing.append(name)
        else:
            used_ids.add(resolved.id)
    error = final_state.error
    if error is None and conflicting_requirements:
        error = "conflicting artifact requirements: " + ", ".join(conflicting_requirements)
    elif error is None and missing and not final_state.awaiting_approval():
        missing_error = "required artifacts missing: " + ", ".join(missing)
        diagnosis = _first_failed_task_diagnosis(final_state)
        error = f"{diagnosis}; {missing_error}" if diagnosis is not None else missing_error
    progress = (
        ThyProgress(
            plan=final_state.plan,
            completed=final_state.completed,
            agent_messages=final_state.agent_messages,
            experiments=final_state.experiments,
            artifacts=final_state.artifacts,
        )
        if final_state.awaiting_approval()
        else None
    )
    return ThyOutput(
        run_id=final_state.run.id,
        summary=final_state.summary,
        recommendation=final_state.recommendation,
        experiment_ids=tuple(e.id for e in final_state.experiments),
        artifact_ids=tuple(a.id for a in final_state.artifacts),
        required_artifacts=requirements,
        missing_artifacts=tuple(missing),
        error=error,
        usage={
            "requests": final_state.usage.requests,
            "tokens": final_state.usage.total_tokens,
            "cost_usd": final_state.usage.cost_usd,
        },
        progress=progress,
    )


__all__ = ["build_thy_graph", "graph_definition_hash", "run_thy"]
