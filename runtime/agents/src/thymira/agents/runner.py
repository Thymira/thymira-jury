"""AgentRunner: generic spec-driven agent execution loop (THY-03, tools since THY-04).

The uniform spoke interface every specialist agent is driven through (MVP runs 3 agents, FINAL
runs 8 through the same runner). A step is one model request plus its tool calls (ADR-0005 idea
11): `Agent(retries=spec.max_turns - 1)` is PydanticAI's retry loop over `routed_model`'s
`ModelRetry` (see `model_binding.routed_model`), while `UsageLimits(request_limit=spec.max_turns)`
also bounds model requests after tools have been called. Together they enforce the step contract
without a hand-rolled loop around PydanticAI.
`ctx.tool_registry`/`ctx.tool_context` are optional: unset, a run has no tools (THY-03's original
shape); set, `build_agent_tools` (THY-04) attaches exactly `spec.tool_allowlist`, gated through the
Tool Manager + Gate, never subprocess/filesystem directly.

`PromptBuilder` (THY-05) now assembles the prompt this module sends: `assembled.system` (a pure
function of `(spec.name, environment)` — the persona line, the catalog prompt and one "when to
use" paragraph per allowlisted tool, harness basics 1) becomes the `Agent`'s `instructions`, and
`assembled.user` (history from
`current_surface(ctx.event_log.events())`, read once, before this call appends anything, plus
`task.objective`) is what `run_sync` actually sends — never `task.objective` alone. `assembled` is
also handed to `routed_model` so, when `ctx.artifact_store` is set, every real call records prompt
provenance (THY-06) instead of the plain `model.selected` append.

`ctx.usage` (THY-07, `thymira.agents.usage.RunUsage` — aliased `SharedRunUsage` here to keep it
apart from `AgentResult.usage`, PydanticAI's own per-call `pydantic_ai.usage.RunUsage`) is the
run-wide shared budget: unset, nothing is charged; set, every model call (`routed_model`) and every
tool call (`build_agent_tools`) charges into the *same* accumulator across every `AgentRunner.run`
call a caller threads it through, and `UsageLimitExceededError` propagates out of `run` uncaught — a
budget breach is a run-level concern, never folded into a per-task `AgentResult(FAILED)`.

Terminal lifecycle events carry `subject_id=task.agent_id` — the agent instance this step ran as,
not
the spec name or the task kind, so two concurrent runs of the same spec stay distinguishable. MIRA
pairs `agent.started` against `agent.completed` by that id (`a9_lifecycle_pairing`, A24's routing
window), and pairing is *set membership*: with the subject left unset the started set is `{None}`
and the completed set is `{None}`, so any one completion marked every start as finished and an
agent that died mid-step — a budget breach propagating `UsageLimitExceededError` out of `run`, a
killed process — read as paired. `thymira.mira.agents.runner` records its own audit agents the same
way; the payload also keeps `task_id`, which identifies the work, not the worker.

`agent.completed` carries this step's `input_tokens`/`output_tokens`/`cost_usd` (THY-30): tokens
from the run's aggregated `RunUsage`, cost from the shared budget's delta across the step
(`_usage_payload`). That is the per-agent evidence the `thymira.cli` activity view folds. A resumed
step does not append a second start, so its terminal event closes the original lifecycle.

A step can also park on a review the Gate left to a human rather than on a validated output: the
tool bridge raises `ApprovalRequired` when `ToolExecution.pending_approval` is set (Task 1), which
ends `agent.run_sync` cleanly with a `DeferredToolRequests` output instead of PydanticAI looping or
raising `UserError`. That step writes `agent.parked`, not a terminal completion. Its later resume
keeps the original `agent.started` and writes one `agent.completed`; its `task_status` is
`PENDING` until then, never `COMPLETED` or `FAILED`, and `end_reason` on the parked event
(`AgentEndReason`) says why the step paused:
a validated output, exhausted retries, an unanswered review, or an unresolved tool failure. That
step's own conversation is
spilled to the `ArtifactStore` (`thymira.agents.resume.save_pending_resume`) so a later call
carrying the matching `resume` argument continues it with PydanticAI's own `message_history`/
`deferred_tool_results` instead of asking the model `task.objective` again from nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import ceil
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import DeferredToolRequests, ModelSettings
from pydantic_ai.capabilities.process_history import ProcessHistory
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from thymira.agents.history import compact_superseded_write_content
from thymira.agents.llm.routing import RANK, choose, floor_for
from thymira.agents.model_binding import _messages_for_provider, routed_model
from thymira.agents.prompts import PromptBuilder, PromptEnvironment
from thymira.agents.resume import (
    build_execution_identity,
    continuation_key,
    save_pending_resume,
)
from thymira.agents.runtime_context import RUNTIME_CONTEXT_FORM, runtime_context_text
from thymira.agents.tool_bridge import build_agent_tools
from thymira.events import canonical_json, sha256_text
from thymira.observability import agent as agent_observation
from thymira.schemas import Actor, EventSurface, EventType, StopReason, TaskStatus, ToolCallStatus
from thymira.tools import tool_guidance

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic import BaseModel
    from pydantic_ai.messages import ModelMessage

    from thymira.agents.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolDefinition
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.prompts import AssembledPrompt
    from thymira.agents.request_ledger import RequestLedger
    from thymira.agents.resume import PendingResume
    from thymira.agents.runtime_catalog import RuntimeSkillCatalog
    from thymira.agents.settlement import DelegationIdentity
    from thymira.agents.spec import AgentCatalog, AgentSpec
    from thymira.agents.usage import ConcurrentRunUsage
    from thymira.agents.usage import RunUsage as SharedRunUsage
    from thymira.events import EventLog
    from thymira.observability.tracing import Handle
    from thymira.schemas import Event, ModelRoutePolicy, Task
    from thymira.state import ArtifactStore
    from thymira.tools import ToolContext, ToolRegistry

_RENDER_ERROR_CHARS = 500


class ResumeHistoryError(ValueError):
    """A parked PydanticAI history cannot be replaced without losing user-visible context."""


@dataclass(frozen=True)
class _ResumeBudgetMeasurement:
    """The provider-neutral estimate used by the resume guard."""

    tokens: int


@dataclass(frozen=True)
class _ResumeBudgetOutcome:
    """The stopped shape consumed by the normal context-budget settlement."""

    measurement: _ResumeBudgetMeasurement
    compacted: Any | None
    retries: int = 0


class _ResumeContextBudgetExceededError(RuntimeError):
    """The actual provider-facing resumed request exceeds the configured runtime cap."""

    def __init__(self, tokens: int, outcome: _ResumeBudgetOutcome) -> None:
        super().__init__(f"resumed request estimated at {tokens} tokens exceeds the context cap")
        self.outcome = outcome


def _require_intact_deferred_batches(messages: Sequence[ModelMessage]) -> None:
    """Reject a parked history where a deferred turn resolved only part of its tool calls.

    `DeferredToolResults` is all-or-nothing per turn (`thymira.agents.resume`'s own module
    docstring): every tool call one model turn deferred together is answered together, so each
    turn's own tool-call ids must end up either every one returned or every one still pending --
    never a mix. A mix means a return the tool bridge produced never reached the persisted
    conversation, so the model still sees that call as open; the runtime's own resume machinery
    then re-asks the provider to resolve it, while `ToolManager`'s one-shot ticket for the
    *other*, already-completed calls from the same turn stays spent -- reproduced against a real
    Run's event log, where exactly this split let a later, unrelated resume replay five already-
    executed reads a second time under their original, already-spent decisions (MIRA's A3).
    Failing closed here trades a clear, immediate step failure for that silent, multi-turn replay.
    """
    returned_ids = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        call_ids = [part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart)]
        if not call_ids:
            continue
        resolved = [call_id in returned_ids for call_id in call_ids]
        if any(resolved) and not all(resolved):
            missing = sorted(
                call_id for call_id, done in zip(call_ids, resolved, strict=True) if not done
            )
            raise ResumeHistoryError(
                "a resumable history split one deferred turn's tool calls between resolved and "
                f"pending: {missing} have no recorded return although sibling calls from the same "
                "turn do"
            )


def _resume_messages(messages: Sequence[ModelMessage], user_prompt: str) -> list[ModelMessage]:
    """Replace a parked conversation's original user context with the current assembled prompt.

    Resumption must continue PydanticAI's exact message history so its pending tool call and
    deferred result remain paired. The current prompt is therefore put into an existing
    ``UserPromptPart`` rather than represented by a fresh model turn, which would orphan the
    approval conversation or ask the original task a second time.
    """
    resumed = list(messages)
    _require_intact_deferred_batches(resumed)
    locations = [
        (index, part_index)
        for index, message in enumerate(resumed)
        if isinstance(message, ModelRequest)
        for part_index, part in enumerate(message.parts)
        if isinstance(part, UserPromptPart)
    ]
    if len(locations) != 1:
        raise ResumeHistoryError(
            "a resumable history must contain exactly one string UserPromptPart; "
            f"found {len(locations)}"
        )
    index, part_index = locations[0]
    part = resumed[index].parts[part_index]
    if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
        raise ResumeHistoryError("a resumable UserPromptPart must carry string content")
    parts = list(resumed[index].parts)
    parts[part_index] = replace(part, content=user_prompt)
    resumed[index] = replace(resumed[index], parts=parts)
    return resumed


def _first_unresolved_response_index(messages: Sequence[ModelMessage]) -> int:
    """Return the earliest `ModelResponse` index with a tool call this history never returned.

    That index and everything from it onward is the still-open, deferred round this resume
    exists for -- `_compact_resolved_tool_rounds` (vector 2) must never touch it or look past it.
    When every `ModelResponse` is already fully resolved (defensive: should not happen for a real
    resume, which by definition has a pending call), the whole history is protected instead --
    compacting nothing is always safe; compacting the wrong thing is not.
    """
    returned_ids = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    for index, message in enumerate(messages):
        if not isinstance(message, ModelResponse):
            continue
        call_ids = [part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart)]
        if call_ids and not all(call_id in returned_ids for call_id in call_ids):
            return index
    return 0


def _drop_oldest_resolved_round(
    messages: Sequence[ModelMessage], protected_from: int
) -> list[ModelMessage] | None:
    """Remove the earliest fully-resolved tool round-trip, or `None` when none is eligible.

    A round is eligible only when: it is a `ModelResponse` (index `i`) whose every `ToolCallPart`
    already has a matching `ToolReturnPart` elsewhere in the history; `i + 1` exists, comes before
    `protected_from`, is a `ModelRequest`, and its *entire* set of parts is exactly those matching
    `ToolReturnPart`s -- never a message that also carries the relocated `UserPromptPart` or
    anything else, which removing whole would lose. Both messages are dropped together so no
    `ToolCallPart` is ever left without its `ToolReturnPart` (the same all-or-nothing invariant
    `_require_intact_deferred_batches` enforces on the untouched history).
    """
    returned_ids = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    for index in range(protected_from - 1):
        message = messages[index]
        if not isinstance(message, ModelResponse):
            continue
        call_ids = [part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart)]
        if not call_ids or not all(call_id in returned_ids for call_id in call_ids):
            continue
        following = messages[index + 1]
        if not isinstance(following, ModelRequest):
            continue
        return_ids = [
            part.tool_call_id for part in following.parts if isinstance(part, ToolReturnPart)
        ]
        if set(return_ids) != set(call_ids) or len(following.parts) != len(call_ids):
            continue
        compacted = list(messages)
        del compacted[index : index + 2]
        return compacted
    return None


def _compact_resolved_tool_rounds(
    messages: Sequence[ModelMessage],
    *,
    fits: Callable[[Sequence[ModelMessage]], bool],
) -> tuple[list[ModelMessage], int]:
    """Drop the oldest fully-resolved tool round-trips until `fits` says stop (vector 2, THY-26).

    Only ever removes whole (`ModelResponse`, `ModelRequest`) round-trip pairs this step's own
    conversation already resolved before it parked -- never the still-open, deferred round the
    resume is about, and never a partial pair (see `_drop_oldest_resolved_round`). Stops as soon
    as `fits` reports success, or when no more rounds are eligible, whichever comes first: this
    mirrors `ContextBudget.fit`'s own compact-then-retry shape for the event-log surface (vector
    1), applied here to the resumed step's own `resume.messages` instead.

    Defense in depth: every candidate is re-checked against `_require_intact_deferred_batches`
    before it is accepted, even though `_drop_oldest_resolved_round` only ever removes whole,
    matched pairs and should never be able to produce a split batch. Should a future change to
    either function break that guarantee, this stops the corrupted candidate here -- falling back
    to the last known-good state -- rather than letting it reach the provider the way the
    incident `_require_intact_deferred_batches` itself documents once did.
    """
    compacted = list(messages)
    dropped = 0
    if fits(compacted):
        return compacted, dropped
    protected_from = _first_unresolved_response_index(compacted)
    while True:
        shrunk = _drop_oldest_resolved_round(compacted, protected_from)
        if shrunk is None:
            return compacted, dropped
        try:
            _require_intact_deferred_batches(shrunk)
        except ResumeHistoryError:
            return compacted, dropped
        removed = len(compacted) - len(shrunk)
        compacted = shrunk
        protected_from -= removed
        dropped += 1
        if fits(compacted):
            return compacted, dropped


def _estimate_provider_request(
    messages: Sequence[LLMMessage],
    *,
    system: str = "",
    tools: Sequence[LLMToolDefinition] = (),
) -> int:
    """Estimate the actual gateway payload using the provider-neutral dispatch conversion."""
    payload: dict[str, Any] = {
        "messages": [message.model_dump(mode="json") for message in messages],
        "tools": [tool.model_dump(mode="json") for tool in tools],
    }
    if system:
        payload["system"] = system
    encoded = canonical_json(payload)
    return ceil(len(encoded) / 4) if encoded else 0


class AgentEndReason(StrEnum):
    """Why a step ended, recorded as ``end_reason`` on every ``agent.completed``.

    ``COMPLETED``: a validated output. ``MAX_TURNS``: PydanticAI exhausted the step's retries or
    request limit. ``AWAITING_APPROVAL``: a tool call needs a human's answer; the step remains
    in-flight after its ``agent.parked`` checkpoint and the task is PENDING until the runtime
    re-runs it. ``TOOL_FAILURE``: the validated output followed an unresolved failed tool call.
    ``CONTEXT_BUDGET``: durable compaction could not reduce the prompt enough for dispatch, so
    the provider was not called. ``RESUME_HISTORY``: a parked history could not be transformed
    into a checkpoint-aware conversation without silently dropping or duplicating user context.
    """

    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    AWAITING_APPROVAL = "awaiting_approval"
    TOOL_FAILURE = "tool_failure"
    CONTEXT_BUDGET = "context_budget"
    RESUME_HISTORY = "resume_history"


def _step_key(spec: AgentSpec, task: Task, assembled: AssembledPrompt) -> str:
    """Hash the stable inputs that identify one isolated agent step."""
    return sha256_text(
        canonical_json(
            {
                "spec": spec.model_dump(mode="json"),
                "task_id": task.id,
                "objective": task.objective,
                "prompt": assembled.model_dump(mode="json"),
            }
        )
    )


def _validate_resume_identity(
    resume: PendingResume, spec: AgentSpec, task: Task, task_kind: str
) -> None:
    """Reject a resume whose immutable identity cannot authorize this exact continuation."""
    identity = resume.identity
    choice = identity.model_choice
    if identity.agent_id != task.agent_id or identity.task_id != task.id:
        raise ValueError("parked execution identity does not match the continuation task")
    if identity.continuation_key != continuation_key(task.agent_id, task.id):
        raise ValueError("parked execution continuation association does not validate")
    if choice.role is not spec.role or choice.task != task_kind:
        raise ValueError("parked model choice does not match the continuation role or task")
    if spec.tier is not None and choice.tier_requested is not spec.tier:
        raise ValueError("parked model choice does not match the continuation role or task")
    if RANK[choice.tier_applied] < RANK[floor_for(spec.role)]:
        raise ValueError("parked model choice is below the current role floor")


def _usage_payload(
    run_usage: RunUsage, shared: SharedRunUsage | None, cost_before: float | None
) -> dict[str, Any]:
    """This step's tokens and cost, for the `agent.completed` event (THY-30).

    Tokens come from the run's own aggregated `RunUsage` (PydanticAI sums every request's
    `RequestUsage`, so a multi-turn step reports the whole step). Cost is the *delta* of the
    shared run budget across this step: cost lives only on the real `LLMResponse` and is
    accumulated into `ctx.usage` by `routed_model`, never on any pre-call event, so the honest
    per-agent figure is what the budget grew by while this step ran. With no shared accumulator
    (an unmetered run) cost is unknown and reported as `0.0`. The fold in `thymira.cli` sums these
    per agent for its per-agent token/cost line; the totals match the run's usage exactly.

    A budget total of `None` means a model call reported no price
    (`thymira.agents.usage.RunUsage.add_cost`), so the delta is unmeasurable and the payload says
    `null` rather than claiming the step cost nothing. Only the *metered* path can say that: the
    unmetered `shared is None` case keeps reporting `0.0`, because there no budget was ever asked
    for and no ceiling depends on the answer.
    """
    cost: float | None
    if shared is None:
        cost = 0.0
    elif shared.cost_usd is None or cost_before is None:
        cost = None
    else:
        cost = shared.cost_usd - cost_before
    return {
        "input_tokens": int(run_usage.input_tokens or 0),
        "output_tokens": int(run_usage.output_tokens or 0),
        "cost_usd": cost,
    }


def _context_budget_failure(
    ctx: AgentContext,
    *,
    spec: AgentSpec,
    task: Task,
    step_key: str,
    outcome: Any,
    observation: Any,
    pydantic_usage: RunUsage,
    cost_before: float | None,
) -> AgentResult:
    """Settle a step whose durable compaction still leaves its request over budget."""
    reason = "context budget remained over limit after durable compaction"
    budget = ctx.context_budget
    if budget is None:
        raise RuntimeError("a stopped context-budget outcome requires its budget guard")
    ctx.event_log.append(
        EventType.AGENT_COMPLETED,
        ctx.actor,
        {
            "agent": spec.name,
            "task_id": task.id,
            "status": TaskStatus.FAILED.value,
            "end_reason": AgentEndReason.CONTEXT_BUDGET.value,
            "stop_reason": StopReason.FAILED.value,
            "reason": reason,
            "step_key": step_key,
            "budget_tokens": outcome.measurement.tokens,
            "budget_limit": budget.limit,
            "compaction_retries": outcome.retries,
            "compaction_event_id": (
                outcome.compacted.event_id if outcome.compacted is not None else None
            ),
            **_usage_payload(pydantic_usage, ctx.usage, cost_before),
            **_delegation_payload(ctx.delegation),
        },
        subject_id=task.agent_id,
    )
    observation.update(output={"status": TaskStatus.FAILED.value}, failed=True, reason=reason)
    return AgentResult(
        output=None,
        usage=pydantic_usage,
        task_status=TaskStatus.FAILED,
        stop_reason=StopReason.FAILED,
    )


def _resume_history_failure(
    ctx: AgentContext,
    *,
    spec: AgentSpec,
    task: Task,
    step_key: str,
    reason: str,
    observation: Any,
    pydantic_usage: RunUsage,
    cost_before: float | None,
) -> AgentResult:
    """Settle a parked step whose history cannot be replaced without losing user context."""
    ctx.event_log.append(
        EventType.AGENT_COMPLETED,
        ctx.actor,
        {
            "agent": spec.name,
            "task_id": task.id,
            "status": TaskStatus.FAILED.value,
            "end_reason": AgentEndReason.RESUME_HISTORY.value,
            "stop_reason": StopReason.FAILED.value,
            "reason": reason,
            "step_key": step_key,
            **_usage_payload(pydantic_usage, ctx.usage, cost_before),
            **_delegation_payload(ctx.delegation),
        },
        subject_id=task.agent_id,
    )
    observation.update(output={"status": TaskStatus.FAILED.value}, failed=True, reason=reason)
    return AgentResult(
        output=None,
        usage=pydantic_usage,
        task_status=TaskStatus.FAILED,
        stop_reason=StopReason.FAILED,
    )


def _environment(
    spec: AgentSpec,
    task_kind: str,
    ctx: AgentContext,
    *,
    model_choice: ModelChoice | None = None,
) -> PromptEnvironment:
    """The Run-stable prompt environment for this spec: model, workspace, tool guidance.

    `choose` is called with exactly the arguments `routed_model` uses (no provider model) so the
    persona's model id equals the one the `model.selected` event records for the same call.
    """
    choice = (
        model_choice
        if model_choice is not None
        else choose(spec.role, task_kind, requested_tier=spec.tier)
    )
    workspace = (
        str(ctx.tool_context.workspace) if ctx.tool_context is not None else "(no workspace)"
    )
    guidance = (
        tool_guidance(ctx.tool_registry, spec.tool_allowlist)
        if ctx.tool_registry is not None
        else ()
    )
    return PromptEnvironment(
        agent_name=spec.name, model=choice.model, workspace=workspace, tool_guidance=guidance
    )


def _runtime_context_or_reason(tool_context: ToolContext) -> str:
    """Render the snapshot, or the reason it could not be rendered -- never raise.

    The snapshot is evidence before it is prompt text (model-visible ⟺ logged); a store or schema
    that cannot be read is itself a fact worth recording and showing the model, not a reason to
    abort the step before `agent.started` exists.
    """
    try:
        return runtime_context_text(
            workspace=tool_context.workspace,
            artifact_store=tool_context.artifact_store,
            project_context=tool_context.project_context,
            workspace_dataset_paths=tool_context.workspace_dataset_paths,
        )
    except (OSError, ValueError) as exc:
        reason = str(exc)[:_RENDER_ERROR_CHARS]
        return (
            f"Current runtime context could not be rendered: {reason}. The artifact store or a "
            "dataset schema may be unreadable; say so in your result if it blocks the task."
        )


def _delegation_payload(delegation: DelegationIdentity | None) -> dict[str, Any]:
    """The three fields that mark a lifecycle event as belonging to a delegation, or nothing."""
    if delegation is None:
        return {}
    return {
        "parent_agent": delegation.parent_agent,
        "delegation_key": delegation.delegation_key,
        "delegation_depth": delegation.delegation_depth,
    }


def _resume_terminal_payload(resume: PendingResume | None) -> dict[str, Any]:
    """Mark a terminal event as the close of its original parked execution identity."""
    if resume is None:
        return {}
    return {
        "resumed": True,
        "execution_identity_sha256": resume.identity.digest(),
    }


@dataclass
class AgentContext:
    """Everything `AgentRunner.run` needs beyond the spec and the task.

    Isolated-context seeding (ADR-0005): a run is seeded only by `catalog` + the `task` passed to
    `run`, never the calling orchestrator's full event thread, so FINAL's compaction (THY-25) only
    ever has to shrink what the runner already reads.
    """

    catalog: AgentCatalog
    event_log: EventLog
    actor: Actor = field(default_factory=Actor.system)
    provider: LLMProvider | None = None
    """Overrides the router-resolved provider; tests inject a `ScriptedProvider` here."""
    tool_registry: ToolRegistry | None = None
    tool_context: ToolContext | None = None
    """Both unset: no tools are attached (`spec.tool_allowlist` is ignored). Both set: exactly
    `spec.tool_allowlist` is exposed to the step, through `build_agent_tools` (THY-04)."""
    artifact_store: ArtifactStore | None = None
    """Unset: `model.selected` stays THY-02 shaped. Set: every call also persists a
    credential-scrubbed LOG
    artifact of what the model saw and enriches `model.selected` with `prompt_sha256`/
    `surface_seqs` (THY-06)."""
    runtime_skill_catalog: RuntimeSkillCatalog | None = None
    runtime_skill_budget: int = 32_000
    runtime_skill_names: tuple[str, ...] = ()
    runtime_skill_request_id: str | None = None
    runtime_skill_dispatch_id: str | None = None
    request_ledger: RequestLedger | None = None
    usage: SharedRunUsage | None = None
    """Unset: nothing is charged. Set: every model/tool call charges into it, raising
    `UsageLimitExceededError` the moment a configured ceiling is crossed (THY-07)."""
    concurrent_usage: ConcurrentRunUsage | None = None
    """Optional run-wide reservation coordinator for genuinely concurrent model calls."""
    delegation: DelegationIdentity | None = None
    """Set when this step *is* a delegation. Its three fields are stamped onto `agent.started`
    and `agent.completed`, which is what lets an auditor tell a delegated step from a direct
    `AgentRunner().run` call -- and so notice a delegation that never settled."""
    current_agent: str | None = None
    """The active caller name for an orchestration wrapper that is not itself delegated.

    ``Delegator`` stamps this from the child spec on every delegated context. It binds the
    parent label at dispatch; it is never an authority or an approval.
    """
    before_model_request: Callable[[], Sequence[str]] | None = None
    """Owner-bound content appended immediately before every provider request."""
    before_model_selection: Callable[[], None] | None = None
    before_model_call: Callable[[ModelChoice], None] | None = None
    record_model_usage: Callable[[LLMResponse], None] | None = None
    context_budget: Any | None = None
    """Optional budget guard supplied by the graph; kept structural to preserve layer direction."""
    compaction_context: Any | None = None
    """The graph-owned compaction dependencies used by ``context_budget`` before a call."""
    route_policy: ModelRoutePolicy | None = None


@dataclass(frozen=True)
class AgentResult:
    """The outcome of one `AgentRunner.run` call.

    `stop_reason` is finer than `task_status`, and the runner is the only place that can tell the
    difference: `out-of-room` and `failed` are distinguished by *which* PydanticAI exception was
    caught -- `UsageLimitExceeded` against `UsageLimits(request_limit=spec.max_turns)` versus
    `UnexpectedModelBehavior` from exhausted output-validation retries -- and both used to collapse
    into one `AgentEndReason.MAX_TURNS`.
    """

    output: BaseModel | None
    usage: RunUsage
    task_status: TaskStatus
    stop_reason: StopReason


class AgentRunner:
    """Drives one `AgentSpec` through one `Task`, bounded by `spec.max_turns`."""

    def run(  # noqa: PLR0911,PLR0912,PLR0915  # one boundary owns lifecycle and settlement
        self,
        spec: AgentSpec,
        task: Task,
        ctx: AgentContext,
        *,
        resume: PendingResume | None = None,
    ) -> AgentResult:
        """Run `spec` against `task`, returning its validated output or a `FAILED` result.

        Args:
            spec: The agent spec this step runs as.
            task: The unit of work; `task.objective` becomes the model's prompt unless `resume`
                is given, and `task.agent_id` names the resume bundle a PENDING outcome is
                spilled under.
            ctx: The shared run-wide context (event log, tools, usage, artifact store).
            resume: A parked step's own conversation (`thymira.agents.resume`), from an earlier
                call that ended on `DeferredToolRequests`. Given, the model is not asked
                `task.objective` again -- PydanticAI resumes exactly that conversation with
                `resume.deferred_tool_results`, so an approved call runs with its original
                arguments and a denial is read back into the same conversation as an ordinary
                tool error. `None` (the default) is today's plain, contextless step.
        """
        output_schema = ctx.catalog.output_schema(spec.name)
        task_kind = spec.task_kinds[0]
        bound_choice = resume.identity.model_choice if resume is not None else None
        tool_context = (
            replace(ctx.tool_context, agent_id=task.agent_id, task_id=task.id)
            if ctx.tool_context is not None
            else None
        )
        artifact_store = (
            ctx.artifact_store
            if ctx.artifact_store is not None
            else tool_context.artifact_store
            if tool_context is not None
            else None
        )
        if tool_context is not None:
            ctx.event_log.append(
                EventType.AGENT_MESSAGE,
                ctx.actor,
                {
                    "text": _runtime_context_or_reason(tool_context),
                    "form": RUNTIME_CONTEXT_FORM,
                    "agent": spec.name,
                    "task_id": task.id,
                },
                surface=EventSurface.MODEL_VISIBLE,
                subject_id=task.agent_id,
            )
        if resume is not None:
            _validate_resume_identity(resume, spec, task, task_kind)
        environment = _environment(spec, task_kind, ctx, model_choice=bound_choice)
        selected_skill_names = (
            task.skill_names or spec.runtime_skill_names or ctx.runtime_skill_names
        )
        prompt_builder = PromptBuilder(
            ctx.catalog,
            ctx.runtime_skill_catalog,
            runtime_skill_budget=ctx.runtime_skill_budget,
        )
        runtime_skill_dispatch_id = ctx.runtime_skill_dispatch_id or f"{task.id}:{task.agent_id}"
        context_budget_stopped = False
        context_budget_outcome: Any | None = None
        # A runtime-skill catalog's `select()` is not idempotent -- it mints a fresh selection_id
        # and publishes a manifest on every call -- so this step builds the prompt exactly once:
        # through `fit()` whenever a budget guard is configured, fresh or resumed alike (it will
        # assemble, and may reassemble, the prompt), directly only when no budget guard is
        # configured. A second, redundant `build()` here would mint a selection nothing ever binds
        # to a request.
        if ctx.context_budget is not None and ctx.compaction_context is not None:
            outcome = ctx.context_budget.fit(
                prompt_builder,
                spec,
                task,
                ctx.event_log.events(),
                ctx.compaction_context,
                environment=environment,
                selected_skill_names=selected_skill_names,
                runtime_skill_request_id=ctx.runtime_skill_request_id,
                runtime_skill_dispatch_id=runtime_skill_dispatch_id,
                # A resume is owed the same full checkpoint fidelity the direct-build branch below
                # already gives it, until compaction actually has to run; once it does, `fit()`
                # bounds the checkpoint on every retry exactly like a non-resume call.
                full_checkpoint_fidelity=resume is not None,
            )
            context_budget_outcome = outcome
            assembled = outcome.assembled
            context_budget_stopped = outcome.stopped
        else:
            assembled = prompt_builder.build(
                spec,
                task,
                ctx.event_log.events(),
                environment=environment,
                checkpoint_max_chars=None if resume is not None else 240,
                selected_skill_names=selected_skill_names,
                runtime_skill_request_id=ctx.runtime_skill_request_id,
                runtime_skill_dispatch_id=runtime_skill_dispatch_id,
            )
        # Whatever `fit()` already achieved for the event-log surface (vector 1: the whole run's
        # history) must survive even when a guard below stops the step for a different reason
        # (vector 2: the resumed conversation's own already-accumulated tool-call history) --
        # otherwise the eventual failure event would under-report `compaction_retries` as 0 despite
        # real compaction having run.
        fit_retries = getattr(context_budget_outcome, "retries", 0)
        resume_messages: list[ModelMessage] | None = None
        resume_history_error: str | None = None
        if resume is not None:
            try:
                resume_messages = _resume_messages(resume.messages, assembled.user)
            except ResumeHistoryError as exc:
                resume_history_error = str(exc)
            if resume_messages is not None and ctx.context_budget is not None:
                budget_limit = ctx.context_budget.limit

                def _fits(candidate: Sequence[ModelMessage]) -> bool:
                    return (
                        _estimate_provider_request(
                            _messages_for_provider(list(candidate)), system=assembled.system
                        )
                        <= budget_limit
                    )

                resume_messages, rounds_dropped = _compact_resolved_tool_rounds(
                    resume_messages, fits=_fits
                )
                if rounds_dropped:
                    ctx.event_log.append(
                        EventType.AGENT_MESSAGE,
                        ctx.actor,
                        {
                            "text": (
                                f"{rounds_dropped} already-resolved tool round-trip(s) from this "
                                "step's own earlier conversation were dropped to fit the context "
                                "window on resume; their original calls and results remain in the "
                                "Run's event log."
                            ),
                            "form": "resume_history_compacted",
                            "agent": spec.name,
                            "task_id": task.id,
                            "rounds_dropped": rounds_dropped,
                        },
                        surface=EventSurface.MODEL_VISIBLE,
                        subject_id=task.agent_id,
                    )
                estimated = _estimate_provider_request(
                    _messages_for_provider(resume_messages), system=assembled.system
                )
                if estimated > ctx.context_budget.limit:
                    context_budget_stopped = True
                    context_budget_outcome = _ResumeBudgetOutcome(
                        measurement=_ResumeBudgetMeasurement(tokens=estimated),
                        compacted=next(
                            (
                                event
                                for event in reversed(ctx.event_log.events())
                                if event.type is EventType.CONTEXT_COMPACTED
                            ),
                            None,
                        ),
                        retries=fit_retries,
                    )
        step_key = (
            resume.identity.step_key if resume is not None else _step_key(spec, task, assembled)
        )

        selected_choice: ModelChoice | None = bound_choice

        def remember_model_call(choice: ModelChoice) -> None:
            nonlocal selected_choice
            selected_choice = selected_choice if selected_choice is not None else choice
            if ctx.before_model_call is not None:
                ctx.before_model_call(choice)

        if not context_budget_stopped and resume_history_error is None:
            provider_guard = None
            resume_budget = ctx.context_budget
            if resume is not None and resume_budget is not None:
                resume_limit = resume_budget.limit

                def provider_guard(
                    messages: Sequence[LLMMessage], tools: Sequence[LLMToolDefinition]
                ) -> None:
                    estimated = _estimate_provider_request(messages, tools=tools)
                    if estimated > resume_limit:
                        outcome = _ResumeBudgetOutcome(
                            measurement=_ResumeBudgetMeasurement(tokens=estimated),
                            compacted=next(
                                (
                                    event
                                    for event in reversed(ctx.event_log.events())
                                    if event.type is EventType.CONTEXT_COMPACTED
                                ),
                                None,
                            ),
                            retries=fit_retries,
                        )
                        raise _ResumeContextBudgetExceededError(estimated, outcome)

            model = routed_model(
                spec.role,
                task_kind,
                ctx.event_log,
                requested_tier=spec.tier,
                provider=ctx.provider,
                actor=ctx.actor,
                output_schema=output_schema,
                assembled=assembled,
                runtime_skill_evidence=assembled.runtime_skill_event_payload(),
                artifact_store=artifact_store,
                agent_id=task.agent_id,
                usage=ctx.usage,
                concurrent_usage=ctx.concurrent_usage,
                before_model_request=ctx.before_model_request,
                before_model_selection=ctx.before_model_selection,
                before_model_call=remember_model_call,
                before_provider_call=provider_guard,
                record_model_usage=ctx.record_model_usage,
                request_ledger=ctx.request_ledger,
                request_owner_id=f"{task.id}:{task.agent_id}",
                bound_choice=bound_choice,
                route_policy=ctx.route_policy,
            )
            tools = (
                build_agent_tools(spec, ctx.tool_registry, tool_context, usage=ctx.usage)
                if ctx.tool_registry is not None and tool_context is not None
                else ()
            )
            # ty cannot resolve `Agent.__init__`'s overloads against a heterogeneous `output_type`
            # list (verified against the pinned ty version): the annotation below is what the call
            # actually returns, confirmed against PydanticAI 2.33.0 by `reveal_type`.
            agent: PydanticAgent[None, BaseModel | DeferredToolRequests] = PydanticAgent(  # ty: ignore[invalid-assignment, no-matching-overload]
                model=model,
                # `DeferredToolRequests` is not an output tool: it lets a tool end the step with
                # `ApprovalRequired` (the bridge, on a review the Gate left pending) instead of
                # PydanticAI raising `UserError`. The output tool keeps its `final_result` name.
                output_type=[output_schema, DeferredToolRequests],
                instructions=assembled.system,
                retries=max(spec.max_turns - 1, 0),
                tools=tools,
                # Several tool calls the model requests in one turn are deferred and resumed
                # together (`thymira.agents.resume`'s all-or-nothing contract). Reproduced live
                # against a real Run: when every one of a multi-call turn needed a human review,
                # resuming re-executed all of them but one call's `ToolReturnPart` never reached
                # the persisted conversation -- the model kept that call dangling, the runtime
                # re-asked for it while the other calls' already-spent tickets stayed spent, and a
                # later resume replayed them a second time under those stale decisions (MIRA's
                # A3). One tool call per turn makes that split impossible to construct -- a
                # deferred turn is always exactly one call, so it is always all-resolved or
                # all-pending, never a mix -- closing the scenario
                # `_require_intact_deferred_batches` only detects after the fact.
                model_settings=ModelSettings(parallel_tool_calls=False),
                capabilities=(ProcessHistory(compact_superseded_write_content),),
            )

        with agent_observation(name=spec.name, objective=task.objective) as observation:
            if resume is None:
                ctx.event_log.append(
                    EventType.AGENT_STARTED,
                    ctx.actor,
                    {
                        "agent": spec.name,
                        "task_id": task.id,
                        "objective": task.objective,
                        "step_key": step_key,
                        **_delegation_payload(ctx.delegation),
                    },
                    subject_id=task.agent_id,
                )

            cost_before = ctx.usage.cost_usd if ctx.usage is not None else 0.0
            pydantic_usage = RunUsage()
            if resume_history_error is not None:
                return _resume_history_failure(
                    ctx,
                    spec=spec,
                    task=task,
                    step_key=step_key,
                    reason=resume_history_error,
                    observation=observation,
                    pydantic_usage=pydantic_usage,
                    cost_before=cost_before,
                )
            if context_budget_stopped:
                return _context_budget_failure(
                    ctx,
                    spec=spec,
                    task=task,
                    step_key=step_key,
                    outcome=context_budget_outcome,
                    observation=observation,
                    pydantic_usage=pydantic_usage,
                    cost_before=cost_before,
                )

            try:
                if resume is not None:
                    result = agent.run_sync(
                        message_history=resume_messages or [],
                        deferred_tool_results=resume.deferred_tool_results,
                        usage_limits=UsageLimits(request_limit=spec.max_turns),
                        usage=pydantic_usage,
                    )
                else:
                    result = agent.run_sync(
                        assembled.user,
                        usage_limits=UsageLimits(request_limit=spec.max_turns),
                        usage=pydantic_usage,
                    )
            except _ResumeContextBudgetExceededError as exc:
                return _context_budget_failure(
                    ctx,
                    spec=spec,
                    task=task,
                    step_key=step_key,
                    outcome=exc.outcome,
                    observation=observation,
                    pydantic_usage=pydantic_usage,
                    cost_before=cost_before,
                )
            except UsageLimitExceeded:
                # The child exhausted a bound it was given, which is not the same fact as an
                # error it could name: `out-of-room`, not `failed` (F7.1).
                return self._record_exhaustion(
                    spec,
                    task,
                    ctx,
                    observation,
                    stop_reason=StopReason.OUT_OF_ROOM,
                    step_key=step_key,
                    resume=resume,
                    usage=pydantic_usage,
                    cost_before=cost_before,
                )
            except UnexpectedModelBehavior:
                return self._record_exhaustion(
                    spec,
                    task,
                    ctx,
                    observation,
                    stop_reason=StopReason.FAILED,
                    step_key=step_key,
                    resume=resume,
                    usage=pydantic_usage,
                    cost_before=cost_before,
                )

            if isinstance(result.output, DeferredToolRequests):
                # The runtime parks a Run on one pending call at a time (the Core resolves the
                # latest pending decision, Task 3): a step that defers several calls in the same
                # turn needs one human round per call. `decision_id` stays the first -- the one
                # the runtime actually parks on -- while `decision_ids` keeps every decision the
                # step asked for, in PydanticAI's own metadata order, so none of them are lost.
                pending = next(iter(result.output.metadata.values()), {})
                decision_ids = [
                    decision_id
                    for value in result.output.metadata.values()
                    if (decision_id := value.get("decision_id")) is not None
                ]
                if artifact_store is None:
                    raise RuntimeError(
                        "cannot park a deferred tool call without durable resume storage"
                    )
                if selected_choice is None:
                    raise RuntimeError("cannot park a deferred tool call without model identity")
                identity = build_execution_identity(
                    model_choice=selected_choice,
                    agent_id=task.agent_id,
                    task_id=task.id,
                    step_key=step_key,
                    tool_call_metadata=result.output.metadata,
                )
                save_pending_resume(
                    artifact_store,
                    agent_id=task.agent_id,
                    identity=identity,
                    messages=list(result.all_messages()),
                    tool_call_metadata=result.output.metadata,
                )
                ctx.event_log.append(
                    EventType.AGENT_PARKED,
                    ctx.actor,
                    {
                        "agent": spec.name,
                        "task_id": task.id,
                        "objective": task.objective,
                        "status": TaskStatus.PENDING.value,
                        "end_reason": AgentEndReason.AWAITING_APPROVAL.value,
                        "decision_id": pending.get("decision_id"),
                        "decision_ids": decision_ids,
                        "step_key": step_key,
                        "execution_identity_sha256": identity.digest(),
                        "resume_bundle": f"resume/{task.agent_id}.json",
                        **_usage_payload(result.usage, ctx.usage, cost_before),
                        **_delegation_payload(ctx.delegation),
                    },
                    subject_id=task.agent_id,
                )
                observation.update(output={"status": TaskStatus.PENDING.value})
                return AgentResult(
                    output=None,
                    usage=result.usage,
                    task_status=TaskStatus.PENDING,
                    stop_reason=StopReason.DECLINED,
                )

            output_exit_code = getattr(result.output, "exit_code", None)
            unresolved = (
                _unresolved_tool_failures(ctx.event_log.events(), task.id)
                if isinstance(output_exit_code, int)
                else ()
            )
            if isinstance(output_exit_code, int) and output_exit_code != 0:
                unresolved = (*unresolved, ("final result", f"exit code {output_exit_code}"))
            if unresolved:
                reason = "; ".join(
                    f"{tool}: {error}" if error else tool for tool, error in unresolved
                )
                ctx.event_log.append(
                    EventType.AGENT_COMPLETED,
                    ctx.actor,
                    {
                        "agent": spec.name,
                        "task_id": task.id,
                        "status": TaskStatus.FAILED.value,
                        "end_reason": AgentEndReason.TOOL_FAILURE.value,
                        "stop_reason": StopReason.FAILED.value,
                        "step_key": step_key,
                        "reason": reason,
                        **_resume_terminal_payload(resume),
                        **_delegation_payload(ctx.delegation),
                        **_usage_payload(result.usage, ctx.usage, cost_before),
                    },
                    subject_id=task.agent_id,
                )
                observation.update(
                    output={"status": TaskStatus.FAILED.value}, failed=True, reason=reason
                )
                return AgentResult(
                    output=None,
                    usage=result.usage,
                    task_status=TaskStatus.FAILED,
                    stop_reason=StopReason.FAILED,
                )

            ctx.event_log.append(
                EventType.AGENT_COMPLETED,
                ctx.actor,
                {
                    "agent": spec.name,
                    "task_id": task.id,
                    "status": TaskStatus.COMPLETED.value,
                    "end_reason": AgentEndReason.COMPLETED.value,
                    "stop_reason": StopReason.COMPLETED.value,
                    "step_key": step_key,
                    **_resume_terminal_payload(resume),
                    **_usage_payload(result.usage, ctx.usage, cost_before),
                    **_delegation_payload(ctx.delegation),
                },
                subject_id=task.agent_id,
            )
            observation.update(output={"status": TaskStatus.COMPLETED.value})
            return AgentResult(
                output=result.output,
                usage=result.usage,
                task_status=TaskStatus.COMPLETED,
                stop_reason=StopReason.COMPLETED,
            )

    def _record_exhaustion(
        self,
        spec: AgentSpec,
        task: Task,
        ctx: AgentContext,
        observation: Handle,
        *,
        stop_reason: StopReason,
        step_key: str,
        resume: PendingResume | None,
        usage: RunUsage,
        cost_before: float | None,
    ) -> AgentResult:
        """Close a step that ended without an output, recording *which* bound ended it.

        `end_reason` stays `MAX_TURNS` for both: it is the coarse lifecycle fact every existing
        consumer reads, while `stop_reason` is the finer terminal one this step is the only place
        that can tell apart.
        """
        ctx.event_log.append(
            EventType.AGENT_COMPLETED,
            ctx.actor,
            {
                "agent": spec.name,
                "task_id": task.id,
                "status": TaskStatus.FAILED.value,
                "end_reason": AgentEndReason.MAX_TURNS.value,
                "stop_reason": stop_reason.value,
                "max_turns": spec.max_turns,
                "step_key": step_key,
                **_resume_terminal_payload(resume),
                **_usage_payload(usage, ctx.usage, cost_before),
                **_delegation_payload(ctx.delegation),
            },
            subject_id=task.agent_id,
        )
        observation.update(
            output={"status": TaskStatus.FAILED.value},
            failed=True,
            reason=f"exhausted max_turns={spec.max_turns}",
        )
        return AgentResult(
            output=None, usage=usage, task_status=TaskStatus.FAILED, stop_reason=stop_reason
        )


def _unresolved_tool_failures(
    events: Sequence[Event], task_id: str
) -> tuple[tuple[str, str | None], ...]:
    """Return tools whose latest task-scoped execution ended unsuccessfully."""
    latest: dict[str, tuple[bool, str | None]] = {}
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED or event.payload.get("task_id") != task_id:
            continue
        tool = event.payload.get("tool")
        if not isinstance(tool, str):
            continue
        status = event.payload.get("status")
        status_value = status.value if isinstance(status, ToolCallStatus) else status
        exit_code = event.payload.get("exit_code")
        failed = status_value == ToolCallStatus.FAILED.value or (
            isinstance(exit_code, int) and exit_code != 0
        )
        error = event.payload.get("error")
        latest[tool] = (failed, error if isinstance(error, str) else None)
    return tuple((tool, error) for tool, (failed, error) in latest.items() if failed)


__all__ = ["AgentContext", "AgentEndReason", "AgentResult", "AgentRunner", "ResumeHistoryError"]
