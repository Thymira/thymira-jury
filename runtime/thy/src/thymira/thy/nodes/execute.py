"""Execute node: dispatch the policy-allowed plan to specialist agents in dependency order (THY-12).

Extends THY-09's own seam (iterate plan -> Delegator per AgentTask -> collect Task outcomes)
without changing its shape: a task whose `depends_on` names a task that did not COMPLETE is
recorded SKIPPED -- never silently dropped, and never actually delegated -- while independent
tasks still proceed. A FAILED task is evidence, not a reason to hide the Run from audit
(bug-hunt C1): it is recorded on `state.agent_messages`/`state.completed` and the event log exactly
like any other outcome, and Execute always advances to Summarize afterward -- `state.error` stays
reserved for a halt *before* Execute ever runs (a Gate-rejected plan, set in `thymira.thy.nodes.
plan`), never for a task that failed after being tried. An agent kind the run's catalog cannot
resolve is exactly this kind of failure, not a different one: `_unresolved_outcome` settles it as
a FAILED task carrying the registered roster, the same way any other failed delegation would,
instead of letting `AgentCatalog.get`'s `KeyError` escape `execute_node` and kill the Run before
MIRA ever audits it (the live failure this closes: run_0fd2e3cbbc5341348e97264cd8df0b36).

Tools and evidence (THY-12/THY-16 residuals closed for THY-31): when `build_thy_graph` is given a
`tool_registry` and a `tool_context`, they are threaded onto the `AgentContext`, so a delegated
agent can actually call `spec.tool_allowlist` through the Tool Manager + Gate. Two things then flow
back out of a delegated call into `ThyState`, both keyed off the store the Tool Manager already
writes:

- every artifact a task produced (the `ArtifactStore` diff around the delegation) is folded into
  `state.artifacts`, so `ThyOutput.artifact_ids` lists what the run actually produced, not only the
  report Summarize writes;
- a COMPLETED `EXPERIMENT` task's `ExperimentResult` is mapped to a `schemas.Experiment`
  (`thymira.thy.experiments.experiment_from_result`) and appended to `state.experiments`, so
  Summarize has something to compare and recommend. Without a `tool_context` there is no store to
  diff and this whole path is inert -- every caller that passed no tools keeps its exact behavior.

Parallel fan-out (THY-23): `max_concurrency` lets independent tasks run concurrently instead of one
at a time. The plan is split into dependency *waves* (`_waves`): every task whose `depends_on` all
resolved in earlier waves sits in the same wave, and `depends_on` is honoured because a dependent
task is never in the same wave as its predecessor. Within a wave the runnable tasks are delegated
concurrently, bounded by the cap, and a fan-in barrier (the wave boundary) joins them before the
next wave -- and before Summarize. Outcomes are folded back in *plan order*, so the completed list,
and child-owned terminal events are stable even when wall-clock completion order differs. Two
coordination choices preserve causal, auditable evidence under real threads:

- the hash-chained event log is single-writer by contract, so each concurrent delegation runs on
  its own child log seeded with a *frozen snapshot* of the shared log taken at the wave's start
  (all wave siblings therefore assemble their prompt from the same prefix -- provider cache
  sharing is preserved). Agent start/model selection and their request/response append to the
  canonical chain in causal runtime order; child-owned terminal events replay in plan order;
- one thread-safe reservation coordinator owns the run-wide request/token/cost ceilings. Finite
  token or cost limits serialize only the unknown-size provider calls; otherwise calls overlap up
  to the request and worker caps. Per-task accumulators remain isolated reporting evidence.

Concurrency is engaged only for a tool-less run (`tool_context is None`); a runtime-skill catalog
also requires a canonical request ledger. The `ArtifactStore` and
its manifest are single-writer too, and per-task artifact/experiment attribution is a temporal
before/after diff that is only well-defined sequentially, so a run that supplies a `tool_context`
executes sequentially regardless of `max_concurrency` (exact attribution preserved). Per-task
`ArtifactStore` isolation -- the seam that would let experiments run in parallel -- is deliberately
left to a follow-on; the concurrency cap and the wave scheduler are in place for it.

A tool call a review-gated `Gate` leaves to a human ends its task PENDING, not COMPLETE or FAILED
(THY-17): `_run_sequential` records that outcome and stops the loop right there instead of trying
the next task -- a human must answer before this pass can say any more. A seeded pass (a Core
resume carrying a `ThyProgress` back into `ThyState.completed`/`agent_messages`) is the mirror of
that halt: `_carried_outcomes` returns the decided *prefix* of the plan -- paired positionally with
`state.plan`, never by `AgentTask.id` (a plan is LLM-authored and nothing enforces id uniqueness)
-- and `_run_sequential` iterates only `state.plan[len(carried):]`, so only the one task that
asked -- the PENDING one, past the end of the carried prefix -- is ever attempted again.
`_pending_outcome` recovers that dropped pair's own `Task`, and `load_pending_resume`
(`thymira.agents.resume`) says whether every decision it deferred is now answered: not yet ->
the parked outcome carries over unchanged and nothing is delegated; answered -> `Delegator`
resumes that task's own saved PydanticAI conversation instead of asking the model its objective
again from nothing; no saved conversation at all (no `tool_context`/`ArtifactStore` when it
parked) -> falls back to a plain, contextless `delegate()`. `_run_parallel` is unaffected: it
only ever runs without a `tool_context`, where no tool call, and so no PENDING outcome, can occur.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from thymira.agents import (
    AgentContext,
    Delegator,
    PendingDelegation,
    ResumeStatus,
    load_pending_resume,
)
from thymira.agents.settlement import (
    build_settlement,
    delegation_key,
    finalize_parked_invocation,
    record_settlement,
    settle_stopped,
)
from thymira.agents.usage import ConcurrentRunUsage, RunUsage
from thymira.events import InMemoryEventLog
from thymira.observability import bind_context
from thymira.schemas import (
    Actor,
    ArtifactKind,
    EventSurface,
    EventType,
    Experiment,
    ExperimentStatus,
    StopReason,
    Task,
    TaskStatus,
    new_id,
    utc_now,
)
from thymira.thy.agents.experiment import ExperimentResult
from thymira.thy.agents.ml import ML_TUNING_HELPER_SPEC, MLResult
from thymira.thy.compaction import CompactionContext
from thymira.thy.experiments import experiment_from_ml_result, experiment_from_result
from thymira.thy.models import ThyAgentKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents import AgentCatalog, RuntimeSkillCatalog
    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.agents.settlement import Settlement
    from thymira.events import EventLog, VerificationResult
    from thymira.schemas import Artifact, Event, ModelRoutePolicy
    from thymira.state import ArtifactStore
    from thymira.thy.budget import ContextBudget
    from thymira.thy.models import AgentTask, ArtifactRequirement, ThyState
    from thymira.tools import ToolContext, ToolRegistry

_MIN_PARALLEL = 2
"""Below this, `max_concurrency` keeps Execute on its original one-task-at-a-time dispatch."""


def execute_node(
    catalog: AgentCatalog,
    event_log: EventLog,
    provider: LLMProvider | None,
    *,
    tool_registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    runtime_skill_budget: int = 32_000,
    runtime_skill_names: tuple[str, ...] = (),
    max_concurrency: int | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_execute: Callable[[], None] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    context_budget: ContextBudget | None = None,
    route_policy: ModelRoutePolicy | None = None,
    request_ledger: RequestLedger | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build the Execute node bound to `catalog`/`event_log`/`provider` and optional tools.

    Args:
        catalog: Resolves each `AgentTask.agent` to the `AgentSpec` to delegate to.
        event_log: Where every delegated call's events land.
        provider: Overrides the router-resolved provider for every delegated call.
        tool_registry: The tools a delegated agent may call. Given together with `tool_context`,
            exactly `spec.tool_allowlist` is exposed through the Tool Manager + Gate; omitted, a
            delegated agent runs with no tools (THY-09's original shape).
        tool_context: The runtime-owned tool context (workspace, Gate, `ArtifactStore`,
            risk profile). Its `ArtifactStore` is also the store this node diffs to fold produced
            artifacts and experiments back onto `ThyState`.
        runtime_skill_catalog: Optional THY runtime catalog. Bodies and references are resolved
            only for selected names after Plan has produced each task.
        runtime_skill_budget: Maximum UTF-8 bytes in one selected runtime-skill projection.
        runtime_skill_names: Names selected for delegated tasks that do not carry task-specific
            runtime skill names.
        max_concurrency: How many independent tasks in one dependency wave may run at once (THY-23).
            `None` or ``< 2`` keeps the original sequential dispatch. Concurrency is engaged only
            when `tool_context is None`; a run with tools stays sequential to preserve exact
            per-task artifact/experiment attribution. Catalog-backed calls also require
            `request_ledger` so their selection evidence is written to the canonical log before
            the gateway call.
        before_model_request: Optional owner-bound hook invoked before each provider request.
        before_execute: Optional owner-bound hook invoked when this Execute node is entered.
        before_model_selection: Optional Core budget check before routing each model call.
        before_model_call: Optional Core Gate check after routing and before provider invocation.
        record_model_usage: Optional Core hook for charging measured provider usage.
        context_budget: optional context-window guard; compaction runs before the agent call.
        before_model_request: Optional owner-bound hook invoked before each provider request.
        request_ledger: Optional request ledger shared by delegated providers in this Run.
        route_policy: Immutable session allowlist shared by sequential and parallel dispatch.
    """
    store = tool_context.artifact_store if tool_context is not None else None
    # Child logs are re-chained at the fan-in barrier. Pre-request lifecycle evidence uses the
    # canonical request ledger's event log directly, so a concurrent request cannot overtake its
    # own agent start or model selection. A missing ledger keeps runtime skills sequential: there
    # is no durable owner to bind before the provider call in that configuration.
    parallel = (
        max_concurrency is not None
        and max_concurrency >= _MIN_PARALLEL
        and tool_context is None
        and (runtime_skill_catalog is None or request_ledger is not None)
    )

    def _execute(state: ThyState) -> dict[str, Any]:
        if before_execute is not None:
            before_execute()
        compaction_context = (
            CompactionContext(
                provider=provider,
                choice=None,
                event_log=event_log,
                actor=Actor.system(),
                request_ledger=request_ledger,
                usage=state.usage,
                before_model_selection=before_model_selection,
                before_model_call=before_model_call,
                record_model_usage=record_model_usage,
                route_policy=route_policy,
            )
            if context_budget is not None
            else None
        )
        if parallel:
            return _run_parallel(
                state,
                catalog,
                event_log,
                provider,
                max_concurrency=max_concurrency or 1,
                before_model_request=before_model_request,
                before_model_selection=before_model_selection,
                before_model_call=before_model_call,
                record_model_usage=record_model_usage,
                context_budget=context_budget,
                compaction_context=compaction_context,
                route_policy=route_policy,
                request_ledger=request_ledger,
                runtime_skill_catalog=runtime_skill_catalog,
                runtime_skill_budget=runtime_skill_budget,
                runtime_skill_names=runtime_skill_names,
            )
        return _run_sequential(
            state,
            catalog,
            event_log,
            provider,
            tool_registry=tool_registry,
            tool_context=tool_context,
            runtime_skill_catalog=runtime_skill_catalog,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=runtime_skill_names,
            store=store,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            context_budget=context_budget,
            compaction_context=compaction_context,
            route_policy=route_policy,
            request_ledger=request_ledger,
        )

    return _execute


def _run_sequential(
    state: ThyState,
    catalog: AgentCatalog,
    event_log: EventLog,
    provider: LLMProvider | None,
    *,
    tool_registry: ToolRegistry | None,
    tool_context: ToolContext | None,
    runtime_skill_catalog: RuntimeSkillCatalog | None,
    runtime_skill_budget: int,
    runtime_skill_names: tuple[str, ...],
    store: ArtifactStore | None,
    before_model_request: Callable[[], Sequence[str]] | None,
    before_model_selection: Callable[[], None] | None,
    before_model_call: Callable[[ModelChoice], None] | None,
    record_model_usage: Callable[[LLMResponse], None] | None,
    context_budget: ContextBudget | None,
    compaction_context: CompactionContext | None,
    route_policy: ModelRoutePolicy | None,
    request_ledger: RequestLedger | None,
) -> dict[str, Any]:
    """Dispatch the plan one task at a time (THY-12), folding artifacts and experiments per task."""
    ctx = AgentContext(
        catalog=catalog,
        event_log=event_log,
        provider=provider,
        usage=state.usage,
        tool_registry=tool_registry,
        tool_context=tool_context,
        runtime_skill_catalog=runtime_skill_catalog,
        runtime_skill_budget=runtime_skill_budget,
        runtime_skill_names=runtime_skill_names,
        artifact_store=store,
        before_model_request=before_model_request,
        before_model_selection=before_model_selection,
        before_model_call=before_model_call,
        record_model_usage=record_model_usage,
        context_budget=context_budget,
        compaction_context=compaction_context,
        route_policy=route_policy,
        request_ledger=request_ledger,
    )
    delegator = Delegator(ctx)
    carried = _carried_outcomes(state)
    completed = [agent_task for agent_task, _ in carried]
    agent_messages = [outcome for _, outcome in carried]
    artifacts = list(state.artifacts)
    experiments = list(state.experiments)
    status_by_id: dict[str, TaskStatus] = {
        agent_task.id: outcome.status for agent_task, outcome in carried
    }
    pending = _pending_outcome(state)

    for index, agent_task in enumerate(state.plan[len(carried) :]):
        before_ids = _artifact_ids(store)
        readiness = (
            load_pending_resume(store, tool_context, pending.agent_id, task_id=pending.id)
            if index == 0 and pending is not None
            else None
        )
        settlement: Settlement | None
        if _blocked(agent_task, status_by_id):
            settlement = _stopped_settlement(state, agent_task, event_log, ctx.actor)
            outcome = settlement.task
        elif (
            pending is not None
            and readiness is not None
            and readiness.status in {ResumeStatus.WAITING, ResumeStatus.CANCELLED}
        ):
            # At least one of this step's several deferred decisions (one turn can defer more
            # than one call) still has no human answer -- `DeferredToolResults` is all-or-nothing
            # per turn -- so nothing is delegated and the parked outcome carries over unchanged.
            # A cancelled bundle takes the same safe branch: cancellation is terminal for this
            # continuation and must never fall through to a fresh delegation.
            outcome = pending
            settlement = None
        elif (
            pending is not None
            and readiness is not None
            and readiness.status is ResumeStatus.INVALID
        ):
            outcome = _failed_resume(pending, event_log, readiness.reason or "identity is invalid")
            settlement = None
        elif (
            pending is not None
            and readiness is not None
            and readiness.status not in {ResumeStatus.READY, ResumeStatus.WAITING}
        ):
            # A carried PENDING task is never a fresh delegation. NOT_AVAILABLE means this pass
            # cannot recover the parked transcript (for example, a caller omitted the durable
            # ArtifactStore/ToolContext); any future status is treated the same way so an unknown
            # recovery result cannot silently mint a second model call.
            outcome = _failed_resume(
                pending,
                event_log,
                "resume context is unavailable"
                if readiness.status is ResumeStatus.NOT_AVAILABLE
                else f"invalid resume status {readiness.status.value}",
            )
            settlement = None
        elif index == 0 and pending is not None and agent_task.agent.value not in catalog.names():
            # A resumed graph can legitimately have a narrower catalog than the graph that
            # parked (for example after a deployment or project-agent reconfiguration). The
            # human answer is still scoped to the original call, but there is no longer an
            # authorised child spec through which to replay it. Close that exact parked
            # invocation instead of minting the unrelated FAILED Task used for a never-started
            # plan item; otherwise its keyed agent.started/agent.parked lifecycle is orphaned.
            outcome, settlement = (
                _failed_resume(
                    pending,
                    event_log,
                    _unknown_agent_error(agent_task.agent.value, catalog),
                ),
                None,
            )
        elif (unresolved := _unresolved_outcome(state, agent_task, catalog, event_log)) is not None:
            outcome = unresolved
            settlement = None
        else:
            spec = catalog.get(agent_task.agent.value)
            ready = readiness is not None and readiness.status is ResumeStatus.READY
            resume = readiness.resume if ready else None
            delegated = delegator.delegate(
                "thy",
                spec,
                _delegation_instruction(agent_task),
                depth=0,
                resume=resume,
                task=pending if resume is not None else None,
                skill_names=agent_task.skill_names,
            )
            outcome = delegated.task
            settlement = None if isinstance(delegated, PendingDelegation) else delegated
        completed.append(agent_task)
        agent_messages.append(outcome)
        status_by_id[agent_task.id] = outcome.status

        produced = _new_artifacts(store, before_ids)
        artifacts.extend(produced)
        experiment = _folded_experiment(ctx, state, agent_task, settlement, produced, event_log)
        if experiment is not None:
            experiments.append(experiment)
        if outcome.status is TaskStatus.PENDING:
            # The Run parks here: a human must answer before this task can finish, and nothing
            # after it is attempted until then (`_route_after_execute` ends the graph).
            break
        if (
            pending is not None
            and readiness is not None
            and readiness.status not in {ResumeStatus.READY, ResumeStatus.WAITING}
        ):
            # An unrecoverable carried task is terminal evidence for this Execute pass. Do not
            # continue with later plan work after failing closed on the original call.
            break

    updates: dict[str, Any] = {
        "completed": tuple(completed),
        "agent_messages": tuple(agent_messages),
        "artifacts": tuple(artifacts),
        "experiments": tuple(experiments),
    }
    return _finish(state, updates)


def _failed_resume(pending: Task, event_log: EventLog, reason: str) -> Task:
    """Record a carried task as failed when its exact parked call cannot be recovered."""
    evidence = _parked_ticket_evidence(pending, event_log)
    details = f"parked task {pending.id} ({pending.agent_id}) cannot resume: {reason}"
    if evidence:
        decision_id, ticket = evidence
        details += f"; original decision_id={decision_id}; tool_intent_sha256={ticket}"
    finalized = finalize_parked_invocation(
        event_log,
        actor=Actor.system(),
        stop_reason=StopReason.FAILED,
        diagnostics=details,
    )
    if finalized is not None:
        return finalized.task
    event_log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {
            "parent_agent": "thy",
            "agent": pending.agent_id,
            "status": TaskStatus.FAILED.value,
            "summary": None,
            "text": f"{pending.agent_id} failed: {details}",
            "resume_failure": True,
        },
        subject_id=pending.id,
        surface=EventSurface.MODEL_VISIBLE,
    )
    return pending.model_copy(
        update={"status": TaskStatus.FAILED, "error": details, "completed_at": utc_now()}
    )


def _parked_ticket_evidence(pending: Task, event_log: EventLog) -> tuple[str, str] | None:
    """Find the parked task's decision and ticket from its denial evidence, if retained."""
    for event in reversed(event_log.events()):
        if event.type is not EventType.TOOL_DENIED:
            continue
        if event.payload.get("task_id") != pending.id:
            continue
        decision_id = event.payload.get("decision_id")
        ticket = event.payload.get("tool_intent_sha256")
        if isinstance(decision_id, str) and isinstance(ticket, str):
            return decision_id, ticket
    return None


def _run_parallel(
    state: ThyState,
    catalog: AgentCatalog,
    event_log: EventLog,
    provider: LLMProvider | None,
    *,
    max_concurrency: int,
    before_model_request: Callable[[], Sequence[str]] | None,
    before_model_selection: Callable[[], None] | None,
    before_model_call: Callable[[ModelChoice], None] | None,
    record_model_usage: Callable[[LLMResponse], None] | None,
    context_budget: ContextBudget | None,
    compaction_context: CompactionContext | None,
    route_policy: ModelRoutePolicy | None,
    request_ledger: RequestLedger | None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    runtime_skill_budget: int = 32_000,
    runtime_skill_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Dispatch the plan wave by wave, running each wave's independent tasks concurrently (THY-23).

    No `ArtifactStore` is present on this path (concurrency is engaged only for a tool-less run),
    so there are no artifacts or experiments to fold. Terminal outcomes and child-owned events
    fold back in plan order; pre-request lifecycle and request-ledger events append canonically in
    their causal runtime order. Run-wide usage is reserved and charged at provider-call time.
    """
    pending = _pending_outcome(state)
    if pending is not None:
        # A previous tool-bearing pass may be resumed through a graph configured for parallel
        # dispatch. Without its exact recovery context, fail closed instead of fanning out work.
        messages = list(state.agent_messages)
        messages[-1] = _failed_resume(pending, event_log, "resume context is unavailable")
        return _finish(
            state,
            {"completed": state.completed, "agent_messages": tuple(messages)},
        )
    completed = list(state.completed)
    agent_messages = list(state.agent_messages)
    status_by_id: dict[str, TaskStatus] = {}
    concurrent_usage = ConcurrentRunUsage(state.usage)

    for wave in _waves(state.plan):
        prefix = event_log.events()
        runnable_positions = frozenset(
            position for position, task in enumerate(wave) if not _blocked(task, status_by_id)
        )
        runnable = [task for position, task in enumerate(wave) if position in runnable_positions]
        dispatchable = [task for task in runnable if task.agent.value in catalog.names()]
        outcomes = _delegate_wave(
            dispatchable,
            prefix,
            catalog,
            provider,
            concurrent_usage,
            run_id=event_log.run_id,
            max_concurrency=max_concurrency,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            context_budget=context_budget,
            compaction_context=compaction_context,
            route_policy=route_policy,
            request_ledger=request_ledger,
            runtime_skill_catalog=runtime_skill_catalog,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=runtime_skill_names,
        )
        outcome_index = 0
        for position, agent_task in enumerate(
            wave
        ):  # fold in plan order, identical to sequential accumulation
            if position not in runnable_positions:
                outcome = _stopped_settlement(state, agent_task, event_log, Actor.system()).task
            elif agent_task.agent.value in catalog.names():
                try:
                    outcome, new_events, _task_usage = outcomes[outcome_index]
                except IndexError as exc:  # pragma: no cover - delegated occurrence invariant
                    msg = f"missing parallel outcome for runnable occurrence {outcome_index}"
                    raise AssertionError(msg) from exc
                outcome_index += 1
                _replay(new_events, event_log)
            else:
                # Not blocked and never delegated: `dispatchable`'s filter above excluded it for
                # being unresolvable, and `_unresolved_outcome` records exactly that on the
                # shared log, in plan order.
                # `dispatchable`'s filter above guarantees this branch only sees an unresolvable
                # kind, so `_unresolved_outcome` always returns a Task here, never None.
                resolved_outcome = _unresolved_outcome(state, agent_task, catalog, event_log)
                if resolved_outcome is None:  # pragma: no cover - defensive, see comment above
                    msg = f"unreachable: {agent_task.agent.value!r} resolved after all"
                    raise AssertionError(msg)
                outcome = resolved_outcome
            completed.append(agent_task)
            agent_messages.append(outcome)
            status_by_id[agent_task.id] = outcome.status
        if outcome_index != len(outcomes):  # pragma: no cover - delegated occurrence invariant
            msg = f"parallel outcomes left unconsumed: {len(outcomes) - outcome_index}"
            raise AssertionError(msg)

    updates: dict[str, Any] = {
        "completed": tuple(completed),
        "agent_messages": tuple(agent_messages),
    }
    return _finish(state, updates)


def _delegate_wave(
    runnable: list[AgentTask],
    prefix: Sequence[Event],
    catalog: AgentCatalog,
    provider: LLMProvider | None,
    concurrent_usage: ConcurrentRunUsage,
    *,
    run_id: str,
    max_concurrency: int,
    before_model_request: Callable[[], Sequence[str]] | None,
    before_model_selection: Callable[[], None] | None,
    before_model_call: Callable[[ModelChoice], None] | None,
    record_model_usage: Callable[[LLMResponse], None] | None,
    context_budget: ContextBudget | None,
    compaction_context: CompactionContext | None,
    route_policy: ModelRoutePolicy | None,
    request_ledger: RequestLedger | None,
    runtime_skill_catalog: RuntimeSkillCatalog | None,
    runtime_skill_budget: int,
    runtime_skill_names: tuple[str, ...],
) -> list[tuple[Task, list[Event], RunUsage]]:
    """Delegate a wave's runnable tasks concurrently, each isolated, returning per-task outcomes.

    Each task runs on its own child event log seeded with `prefix` (the frozen wave-start history,
    so every sibling shares the same prompt prefix) and its own reporting `RunUsage`. Provider
    calls reserve and charge `concurrent_usage`, the shared run-wide authority, while the returned
    task usage is retained only for per-agent result evidence. `run_id` is the shared log's run id,
    so a child log (and the `Task` a delegation mints from it) carries the real run's id even when
    the wave-start `prefix` is empty. Every `runnable` task's agent kind is already known to resolve
    in `catalog` (the caller filters unresolvable kinds out before calling this), so `catalog.get`
    below never raises. The returned list is in ``runnable`` order; that position is the stable
    invocation identity because ``AgentTask.id`` is plan-local and explicitly may repeat.
    Completion order never changes which result belongs to which occurrence.
    """

    def _delegate_one(agent_task: AgentTask) -> tuple[Task, list[Event], RunUsage]:
        child = _seed_child_log(prefix, run_id)
        child_event_log: EventLog = child
        if request_ledger is not None:
            child_event_log = _CanonicalParallelLog(child, request_ledger.event_log)
        task_usage = RunUsage()
        ctx = AgentContext(
            catalog=catalog,
            event_log=child_event_log,
            provider=provider,
            usage=task_usage,
            concurrent_usage=concurrent_usage,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            context_budget=context_budget,
            compaction_context=(
                CompactionContext(
                    provider=compaction_context.provider,
                    choice=compaction_context.choice,
                    event_log=child,
                    actor=compaction_context.actor,
                    request_ledger=compaction_context.request_ledger,
                    request_owner_id=compaction_context.request_owner_id,
                    usage=task_usage,
                    concurrent_usage=concurrent_usage,
                    before_model_selection=compaction_context.before_model_selection,
                    before_model_call=compaction_context.before_model_call,
                    record_model_usage=compaction_context.record_model_usage,
                    route_policy=compaction_context.route_policy,
                )
                if compaction_context is not None
                else None
            ),
            route_policy=route_policy,
            runtime_skill_catalog=(
                runtime_skill_catalog.fork() if runtime_skill_catalog is not None else None
            ),
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=runtime_skill_names,
            request_ledger=request_ledger,
        )
        spec = catalog.get(agent_task.agent.value)
        outcome = (
            Delegator(ctx)
            .delegate(
                "thy",
                spec,
                _delegation_instruction(agent_task),
                depth=0,
                skill_names=agent_task.skill_names,
            )
            .task
        )
        return outcome, child.events()[len(prefix) :], task_usage

    if not runnable:
        return []
    workers = min(max_concurrency, len(runnable))
    delegate = bind_context(_delegate_one)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(delegate, task) for task in runnable]
        return [future.result() for future in futures]


def _finish(state: ThyState, updates: dict[str, Any]) -> dict[str, Any]:
    """Advance out of Execute regardless of a task failure (THY-12, bug-hunt C1).

    A failed task is evidence for MIRA and the Policy Engine, not a reason to hide the Run from
    audit: it is already recorded on `updates["agent_messages"]`/`updates["completed"]`, so the
    phase always advances toward Summarize, exactly as a fully successful Execute pass would.
    """
    return state.advance().model_copy(update=updates).model_dump()


def _waves(plan: tuple[AgentTask, ...]) -> list[list[AgentTask]]:
    """Group `plan` into dependency waves, each holding tasks that may run together (THY-23).

    A task's wave is one past the latest wave of any task it depends on that appears *earlier* in
    the plan; a `depends_on` entry that is not an earlier task (forward or unknown) does not delay
    it -- it simply leaves the task blocked at decision time, exactly as sequential dispatch skips
    it. Same-wave tasks therefore never depend on one another, so running them concurrently honours
    `depends_on`, and folding waves in order reproduces the sequential plan order.
    """
    index = {task.id: position for position, task in enumerate(plan)}
    level: dict[int, int] = {}

    def _level_of(position: int) -> int:
        if position in level:
            return level[position]
        back = [
            index[dep]
            for dep in plan[position].depends_on
            if dep in index and index[dep] < position
        ]
        level[position] = 0 if not back else 1 + max(_level_of(pos) for pos in back)
        return level[position]

    by_level: dict[int, list[AgentTask]] = defaultdict(list)
    for position, task in enumerate(plan):
        by_level[_level_of(position)].append(task)
    return [by_level[key] for key in sorted(by_level)]


def _seed_child_log(prefix: Sequence[Event], run_id: str) -> InMemoryEventLog:
    """A fresh in-memory log carrying a re-chained copy of `prefix`, for one concurrent task."""
    child = InMemoryEventLog(run_id)
    _replay(prefix, child)
    return child


class _CanonicalParallelLog:
    """Route pre-request lifecycle evidence to the durable parent event log.

    Delegated prompts still read and append against a child log so sibling calls have an isolated
    conversational surface. Agent start and model selection are different: the shared request
    ledger can append immediately from another worker, so its causal prerequisites must first land
    on that same canonical chain. Fan-in only replays child-owned terminal and message events; it
    never rewrites these prerequisites.
    """

    def __init__(self, child: EventLog, canonical: EventLog) -> None:
        self._child = child
        self._canonical = canonical
        self.run_id = child.run_id

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the contract field name
        actor: Actor,
        payload: dict[str, Any] | None = None,
        *,
        subject_id: str | None = None,
        producer: str = "thymira.events",
        producer_version: str = "0.3",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
        surface: EventSurface = EventSurface.LOG_ONLY,
    ) -> Event:
        """Append causal request prerequisites canonically and all other events to the child."""
        target = (
            self._canonical
            if type in {EventType.AGENT_STARTED, EventType.MODEL_SELECTED}
            else self._child
        )
        return target.append(
            type,
            actor,
            payload,
            subject_id=subject_id,
            producer=producer,
            producer_version=producer_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            authorization_context_sha256=authorization_context_sha256,
            surface=surface,
        )

    def events(self) -> list[Event]:
        """Return child events for prompt assembly and ordered fan-in."""
        return self._child.events()

    def verify(self) -> VerificationResult:
        """Verify the child-owned conversational chain."""
        return self._child.verify()


def _replay(events: Sequence[Event], target: EventLog) -> None:
    """Append each of `events` onto `target`, re-chaining onto its current tail, order preserved."""
    for event in events:
        target.append(
            event.type,
            event.actor,
            dict(event.payload),
            subject_id=event.subject_id,
            producer=event.producer,
            producer_version=event.producer_version,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            authorization_context_sha256=event.authorization_context_sha256,
            surface=event.surface,
        )


def _carried_outcomes(state: ThyState) -> list[tuple[AgentTask, Task]]:
    """The decided prefix of the plan an earlier pass already ran, paired positionally.

    A seeded state (`ThyProgress`, a Core resume) pairs `completed[i]` with `agent_messages[i]`
    *positionally*, never by `AgentTask.id`: a plan is LLM-authored and nothing enforces id
    uniqueness across it (`AgentTask`'s own docstring disclaims it), so keying by id would
    collapse two plan tasks that happen to share one, silently dropping a recorded outcome.
    Every decided outcome -- COMPLETED, FAILED or SKIPPED -- is kept exactly as it was and never
    retried: a resume re-offers a human's answer to the one call that asked, it is not a second
    attempt at the plan. `_run_sequential` always stops at the first PENDING outcome, so the seed
    it produces never carries one past that point; a trailing PENDING pair here is that task and
    is dropped so it gets re-run, and any other PENDING pair (which a well-formed seed never has)
    is dropped defensively rather than trusted.
    """
    decided = list(zip(state.completed, state.agent_messages, strict=True))
    if decided and decided[-1][1].status is TaskStatus.PENDING:
        decided.pop()
    return [pair for pair in decided if pair[1].status is not TaskStatus.PENDING]


def _pending_outcome(state: ThyState) -> Task | None:
    """The trailing PENDING outcome `_carried_outcomes` drops, or `None` if there is none.

    Mirrors `_carried_outcomes`'s own check exactly, so the two never disagree about which task
    (if any) a resumed pass must continue rather than delegate fresh.
    """
    if (
        state.completed
        and state.agent_messages
        and state.agent_messages[-1].status is TaskStatus.PENDING
    ):
        return state.agent_messages[-1]
    return None


def _blocked(agent_task: AgentTask, status_by_id: dict[str, TaskStatus]) -> bool:
    return any(status_by_id.get(dep) is not TaskStatus.COMPLETED for dep in agent_task.depends_on)


_SKIP_REASON = "a dependency did not complete"


def _required_artifact_line(requirement: ArtifactRequirement) -> str:
    """Render one required-artifact bullet, naming its media type only when the plan declared one.

    The exact name is what a task most often gets wrong live (a plan promises `duration.png`, an
    agent writes `duration_months.png`), so it always leads; `kind` always follows since Plan
    defaults it rather than leaving it unset. `media_type` is genuinely optional on
    `ArtifactRequirement` and only worth stating when the plan actually constrained it.
    """
    media_type = (
        "" if requirement.media_type is None else f', media_type="{requirement.media_type}"'
    )
    return f'- {requirement.name} (kind="{requirement.kind.value}"{media_type})'


def _delegation_instruction(agent_task: AgentTask) -> str:
    """The task's instruction, plus a note on any artifact the plan requires it to publish.

    `AgentTask.required_artifacts` is Plan's own declared requirement (a name and an
    `ArtifactKind`), checked against the `ArtifactStore` only *after* Execute finishes
    (`thymira.thy.graph`'s "required artifacts missing" check) -- but until now nothing before
    that point ever told the delegated agent the check exists, or that `write_file`'s optional
    `kind` argument (or one `run_python.output_artifacts` entry's own `kind`) is what satisfies
    it. A `write_file` call with no `kind` never registers an `Artifact`, so a task can do the
    exact right work and still fail the Run's own deliverable check with no visible defect in the
    output (found live: a coding agent wrote the full declared report, `write_file` returned
    success, but the call carried no `kind`, so "required artifacts missing" failed the Run over
    a file that was sitting right there in the workspace). This is additive only: a task with no
    declared requirement gets its instruction back unchanged.
    """
    if not agent_task.required_artifacts:
        return agent_task.instruction
    lines = "\n".join(
        _required_artifact_line(requirement) for requirement in agent_task.required_artifacts
    )
    return (
        f"{agent_task.instruction}\n\n"
        "This task's plan declares the following required deliverable(s); the Run is not "
        "considered complete until each one is registered as a real Artifact, not merely "
        "written to the workspace -- pass the matching kind shown below to write_file's `kind` "
        "argument, or to the corresponding entry's `kind` in run_python's `output_artifacts`:\n"
        f"{lines}"
    )


def _stopped_settlement(
    state: ThyState, agent_task: AgentTask, event_log: EventLog, actor: Actor
) -> Settlement:
    """Settle a task the orchestrator stopped, through the same writer a real delegation uses.

    A skipped task used to exist only in graph state; it now also settles on the chain as
    `stopped`, at the depth (1) the delegation it replaces would have run at, so an auditor sees
    every planned task account for itself rather than only the ones that were dispatched.
    """
    return settle_stopped(
        task=Task(
            id=new_id("task"),
            run_id=state.run.id,
            agent_id=new_id("agent"),
            objective=agent_task.instruction,
        ),
        parent_agent="thy",
        agent=agent_task.agent.value,
        delegation_depth=1,
        reason=_SKIP_REASON,
        event_log=event_log,
        actor=actor,
    )


def _unresolved_outcome(
    state: ThyState, agent_task: AgentTask, catalog: AgentCatalog, event_log: EventLog
) -> Task | None:
    """Settle `agent_task` as a FAILED task, model-visible, when `catalog` cannot resolve its kind.

    Returns `None` when `agent_task.agent.value` is registered in `catalog` -- nothing to do here,
    the caller delegates normally. Otherwise mints a FAILED `Task` and appends an `agent.message`
    event shaped like the one `Delegator.delegate` appends for a real failure (`parent_agent`,
    `agent`, `status`, `text` -- plus `registered_agents`, naming the roster this run's catalog
    actually holds), so `PromptBuilder`'s surface fold (`thymira.agents.prompts`, which reads
    `payload["text"]`) shows the next agent exactly what happened, the same way it would for any
    other failed step. This is what keeps an unresolvable agent kind a task-level failure instead
    of the `KeyError` that used to escape `catalog.get(...)` and kill the whole Run before MIRA
    ever audited it (the live failure this closes: run_0fd2e3cbbc5341348e97264cd8df0b36).
    """
    kind = agent_task.agent.value
    if kind in catalog.names():
        return None
    registered = list(catalog.names())
    error = _unknown_agent_error(kind, catalog)
    actor = Actor.system()
    task = Task(
        id=new_id("task"),
        run_id=state.run.id,
        agent_id=new_id("agent"),
        objective=agent_task.instruction,
    )
    key = delegation_key(
        run_id=task.run_id,
        task_id=task.id,
        agent_id=task.agent_id,
        parent_agent="thy",
        agent=kind,
        objective=task.objective,
        delegation_depth=1,
    )
    settlement = build_settlement(
        task=task,
        parent_agent="thy",
        agent=kind,
        delegation_depth=1,
        key=key,
        stop_reason=StopReason.ABNORMAL,
        diagnostics=error,
    )
    record_settlement(event_log, settlement.result, actor)
    task = settlement.task
    event_log.append(
        EventType.AGENT_MESSAGE,
        actor,
        {
            "parent_agent": settlement.result.parent_agent,
            "agent": settlement.result.agent,
            "depth": settlement.result.delegation_depth,
            "status": task.status.value,
            "summary": task.summary,
            "error": task.error,
            "text": f"{kind} failed: {task.error}",
            "task_id": task.id,
            "delegation_key": settlement.result.delegation_key,
            "stop_reason": settlement.result.stop_reason.value,
            "registered_agents": registered,
        },
        subject_id=task.id,
        surface=EventSurface.MODEL_VISIBLE,
    )
    return task


def _unknown_agent_error(kind: str, catalog: AgentCatalog) -> str:
    """Describe one plan kind the current Run catalog cannot dispatch."""
    return (
        f"unknown agent {kind!r}: this run's catalog registers "
        f"{', '.join(catalog.names()) or 'no agents'}"
    )


def _artifact_ids(store: ArtifactStore | None) -> frozenset[str]:
    """Ids of the store's active artifacts right now, or an empty set when there is no store."""
    if store is None:
        return frozenset()
    return frozenset(artifact.id for artifact in store.list_active())


def _new_artifacts(store: ArtifactStore | None, before_ids: frozenset[str]) -> list[Artifact]:
    """Active artifacts that appeared since `before_ids` was taken (empty without a store)."""
    if store is None:
        return []
    return [artifact for artifact in store.list_active() if artifact.id not in before_ids]


def _folded_experiment(
    ctx: AgentContext,
    state: ThyState,
    agent_task: AgentTask,
    settlement: Settlement | None,
    produced: list[Artifact],
    event_log: EventLog,
) -> Experiment | None:
    """Try every audit fold a settled task can produce: `EXPERIMENT`, then `ML` (THY-19).

    Also the one call site that triggers `ml-agent`'s tuning-helper follow-up delegation
    (`_maybe_delegate_ml_tuning`): both concerns only apply to a task that just settled, so folding
    lives here to keep `_run_sequential`'s own per-task statement count from growing with every
    specialist that needs post-settlement handling. A carried-over or parked outcome has no fresh
    `settlement` to fold or to delegate from. The two folds are mutually exclusive by
    `agent_task.agent`, so trying both and taking whichever returns non-`None` never masks a real
    result.
    """
    if settlement is None:
        return None
    _maybe_delegate_ml_tuning(ctx, agent_task, settlement)
    return _experiment_or_none(
        state, agent_task, settlement, produced, event_log
    ) or _ml_experiment_or_none(state, agent_task, settlement, produced, event_log)


def _experiment_or_none(
    state: ThyState,
    agent_task: AgentTask,
    settlement: Settlement,
    produced: list[Artifact],
    event_log: EventLog,
) -> Experiment | None:
    """Map a completed Experiment task to a `schemas.Experiment`, or `None`.

    Reads the completion the runner already validated against the catalog's declared output
    schema (`Settlement.completion`) instead of re-parsing `Task.summary`: the schema-constrained
    completion is validated exactly once, at settlement (F7.5). When the experiment tool emitted a
    completed record, that durable event is preferred because it contains the actual metrics. The
    fallback completion is set only for a COMPLETED child, so the narrowing below also covers "did
    not complete"; a
    completion that is not an `ExperimentResult` -- a catalog that declares a different schema for
    the Experiment slot -- folds nothing.
    """
    if agent_task.agent is not ThyAgentKind.EXPERIMENT:
        return None
    for event in reversed(event_log.events()):
        if event.type is not EventType.EXPERIMENT_COMPLETED:
            continue
        payload = event.payload.get("experiment")
        if not isinstance(payload, dict):
            continue
        try:
            recorded = Experiment.model_validate(payload)
        except (TypeError, ValueError):
            continue
        if recorded.run_id == state.run.id and recorded.status is ExperimentStatus.COMPLETED:
            return recorded
    result = settlement.completion
    if not isinstance(result, ExperimentResult):
        return None
    model_artifact = next((a for a in produced if a.kind is ArtifactKind.MODEL), None)
    return experiment_from_result(
        result,
        run_id=state.run.id,
        name=agent_task.instruction,
        artifact_ids=tuple(a.id for a in produced),
        model_artifact_id=model_artifact.id if model_artifact is not None else None,
    )


def _maybe_delegate_ml_tuning(
    ctx: AgentContext, agent_task: AgentTask, settlement: Settlement | None
) -> None:
    """Delegate `ml-tuning-helper` when a completed `ml-agent` task asked for one (THY-19).

    Mirrors the guard `run_ml_agent` (`thymira.thy.agents.ml`) uses for the same decision: only a
    freshly COMPLETED `MLResult` with `needs_tuning_help` and a `tuning_objective` triggers the
    helper. `execute_node` never calls `run_ml_agent` itself -- the primary `ml-agent` run still
    needs the generic pending/resume/carry-over path every other kind gets, not the plain
    `AgentRunner().run()` that wrapper uses -- so this follow-up step, run after the primary
    settles, is what actually reaches the helper tier. A fresh `Delegator` narrowed to
    `current_agent="ml-agent"` is required, not the caller's own top-level one: `Delegator.delegate`
    rejects a `parent_agent` that does not match the active caller identity
    (`DelegationBoundaryError`), and the top-level delegator's identity is `"thy"`, exactly what
    `run_ml_agent` sidesteps by building its own `primary_context` first. The helper's own
    contribution lands only as an `agent.message` event on the shared log; nothing here merges its
    `TuningResult` into the returned outcome, matching `run_ml_agent`'s documented choice.
    """
    if (
        settlement is None
        or agent_task.agent is not ThyAgentKind.ML
        or settlement.task.status is not TaskStatus.COMPLETED
    ):
        return
    result = settlement.completion
    if not isinstance(result, MLResult) or not result.needs_tuning_help:
        return
    if result.tuning_objective is None:
        return
    Delegator(replace(ctx, current_agent=ThyAgentKind.ML.value)).delegate(
        ThyAgentKind.ML.value,
        ML_TUNING_HELPER_SPEC,
        result.tuning_objective,
        depth=1,
    )


def _ml_tracker_run_id(event_log: EventLog, task_id: str) -> str | None:
    """Return the `mlflow_start_run` tracker id this task minted, or `None` if it never called it.

    `ml-agent` logs to MLflow directly through `mlflow_start_run`/`mlflow_log_*` rather than
    through `run_experiment`, so there is no `EXPERIMENT_COMPLETED` event to read a tracker id
    from the way `_experiment_or_none` does. The last matching `tool.completed` event's recorded
    value carries the same `tracker_run_id` the tool returned to the model (`MlflowMutationValue`,
    `runtime/tools/builtins/mlflow_tools.py`); reading it back is a fact, not a guess -- absent a
    match, the caller records the experiment without one rather than inventing an id.
    """
    for event in reversed(event_log.events()):
        if event.type is not EventType.TOOL_COMPLETED or event.payload.get("task_id") != task_id:
            continue
        if event.payload.get("tool") != "mlflow_start_run":
            continue
        value = event.payload.get("value")
        if isinstance(value, dict):
            tracker_run_id = value.get("tracker_run_id")
            if isinstance(tracker_run_id, str) and tracker_run_id:
                return tracker_run_id
        return None
    return None


def _ml_experiment_or_none(
    state: ThyState,
    agent_task: AgentTask,
    settlement: Settlement,
    produced: list[Artifact],
    event_log: EventLog,
) -> Experiment | None:
    """Map a completed `ml-agent` task to a `schemas.Experiment`, or `None` (THY-19 audit fold).

    Without this, `ml-agent` training would be invisible to MIRA's audit even though
    `_route_model_plan` treats it as an auditable alternative to `EXPERIMENT`: the two folds must
    stay in lockstep. See `experiment_from_ml_result` for what the mapping keeps and drops.
    """
    if agent_task.agent is not ThyAgentKind.ML:
        return None
    result = settlement.completion
    if not isinstance(result, MLResult):
        return None
    model_artifact = next((a for a in produced if a.kind is ArtifactKind.MODEL), None)
    return experiment_from_ml_result(
        result,
        run_id=state.run.id,
        name=agent_task.instruction,
        artifact_ids=tuple(a.id for a in produced),
        model_artifact_id=model_artifact.id if model_artifact is not None else None,
        tracker_run_id=_ml_tracker_run_id(event_log, settlement.task.id),
    )


__all__ = ["execute_node"]
