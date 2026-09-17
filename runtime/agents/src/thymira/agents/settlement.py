"""One uniform settlement for every delegated child (F7.1/F7.2/F7.5).

A child never rejects its parent. Whatever terminal outcome happened to it — a validated output,
an exhausted budget, a refused authority after resumption, a named error, an orchestrator stop, or
an exception nobody expected — it settles as one :class:`~thymira.schemas.SubagentResult`, written
to the canonical chain as ``subagent.settled`` *before* the parent appends its own
``agent.message`` and before :meth:`~thymira.agents.delegation.Delegator.delegate` returns. A
refused authority parks the task first; its ``agent.parked`` checkpoint is not a terminal
settlement. That ordering is what lets a fresh reader of ``events.jsonl`` — MIRA, a later audit, a
recovery pass — reconstruct which child finally settled with what, rather than asking the producer
for its return value.

Three things live here on purpose, and nowhere else:

- **the bound.** ``bounded_diagnostics`` redacts *and then* truncates. The order is load-bearing:
  truncating first cuts a secret in half, and the surviving prefix is no longer a pattern any
  redactor recognises, so it reaches the log verbatim. The bound is then recorded as
  ``diagnostics_limit``/``diagnostics_truncated`` on the settlement rather than applied silently,
  so a reader can tell a short diagnostic from a cut one and MIRA can hold the declared bound
  against its own constant.
- **the delegation key.** ``delegation_key`` digests the delegation's run, parent, child spec,
  objective, depth and the freshly minted task/agent identities. The last two are the invocation
  identity: two plan tasks may legitimately repeat every human-readable instruction while still
  being distinct delegations. The parent linkage remains part of the digest, so a result cannot be
  attached to a different orchestrator by copying an objective.
- **the selector.** ``select_canonical`` is the one implementation every production consumer
  funnels through (``tests/thymira/test_agents_settlement.py`` proves it mechanically). Its rule
  is deliberately simple — candidates share one key, the latest in log order wins — because its
  value is uniqueness, not cleverness. MIRA re-derives the same rule from the log alone
  (:mod:`thymira.mira.checks.subagent_settlement`); that duplication is the independent oracle and
  is the one documented exception, the same one :mod:`thymira.mira.checks.tool_intent_evidence`
  already takes for the approval ticket.

``Settlement`` also carries the ``completion`` object the runner already validated against the
catalog's declared output schema. Consumers read that object instead of re-parsing
``Task.summary``, so the schema-constrained completion is validated exactly once, at settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from thymira.events import canonical_json, redact, sha256_text
from thymira.schemas import (
    Event,
    EventSurface,
    EventType,
    StopReason,
    SubagentResult,
    Task,
    TaskStatus,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from thymira.events import EventLog
    from thymira.schemas import Actor

DIAGNOSTICS_LIMIT = 2000
"""Characters of redacted diagnostic text a settlement may carry.

A specification, not a guess: the sibling precedent is ``AgentRunner._RENDER_ERROR_CHARS`` (500)
for a one-line render, and a diagnosis quoting a tool error and a traceback line legitimately
needs more. MIRA holds this exact number independently, so raising it here without raising it
there is a finding rather than a silent widening.
"""

_IDENTITY_DIGEST_LENGTH = 64
"""The hexadecimal length of a sha256 execution-identity digest."""

_TASK_STATUS: dict[StopReason, TaskStatus] = {
    StopReason.COMPLETED: TaskStatus.COMPLETED,
    StopReason.DECLINED: TaskStatus.PENDING,
    StopReason.STOPPED: TaskStatus.SKIPPED,
    StopReason.FAILED: TaskStatus.FAILED,
    StopReason.OUT_OF_ROOM: TaskStatus.FAILED,
    StopReason.ABNORMAL: TaskStatus.FAILED,
}
"""The coarse, stateful `TaskStatus` each terminal reason maps onto.

`TaskStatus` drives the plan, `ThyProgress` and the resume path; `StopReason` is terminal and
finer. Keeping the mapping in one table is what lets the two stay parallel vocabularies instead
of one replacing the other.
"""

_OPEN_TASK_REASONS = frozenset({StopReason.DECLINED, StopReason.STOPPED})
"""Reasons that leave the `Task` open: a declined child waits for a human's answer and a stopped
one was never run, so neither carries a completion time."""


@dataclass(frozen=True, slots=True)
class DelegationIdentity:
    """What makes a step a delegation rather than a direct ``AgentRunner.run``.

    The first three fields are stamped by the runner onto ``agent.started``/``agent.completed``
    so an auditor can tell delegated work from a direct ``AgentRunner.run`` call. ``agent`` is
    active-context data used to bind nested hub calls and is intentionally not a fourth persisted
    lifecycle field.
    """

    parent_agent: str
    delegation_key: str
    delegation_depth: int
    agent: str
    """The child spec name retained in the active context for parent binding."""


@dataclass(frozen=True, slots=True)
class Settlement:
    """One child's terminal settlement, its `Task` record, and the completion already validated."""

    result: SubagentResult
    task: Task
    completion: BaseModel | None = None


@dataclass(frozen=True, slots=True)
class PendingDelegation:
    """A child parked for an external answer, before it has a terminal settlement."""

    task: Task


def render_settlement_text(
    agent_name: str, status: TaskStatus, summary: str | None, error: str | None
) -> str:
    """Render one bounded settlement for a later model-visible parent message."""
    if status is TaskStatus.COMPLETED:
        return f"{agent_name} completed: {summary}"
    if status is TaskStatus.PENDING:
        return f"{agent_name} is waiting for a human to approve a tool call: {error}"
    return f"{agent_name} failed: {error}"


def delegation_key(
    *,
    run_id: str,
    task_id: str,
    agent_id: str,
    parent_agent: str,
    agent: str,
    objective: str,
    delegation_depth: int,
) -> str:
    """Digest the persisted task invocation and its canonical parent linkage."""
    return sha256_text(
        canonical_json(
            {
                "run_id": run_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "parent_agent": parent_agent,
                "agent": agent,
                "objective": objective,
                "delegation_depth": delegation_depth,
            }
        )
    )


def bounded_diagnostics(text: str | None) -> tuple[str | None, bool]:
    """Redact ``text`` and *then* cut it to :data:`DIAGNOSTICS_LIMIT`, saying whether it was cut.

    The order is the guarantee: truncating first would leave the head of a secret behind, past
    the reach of every pattern that would have recognised the whole one.
    """
    if text is None:
        return None, False
    redacted = redact(text)
    if len(redacted) <= DIAGNOSTICS_LIMIT:
        return redacted, False
    return redacted[:DIAGNOSTICS_LIMIT], True


def build_settlement(
    *,
    task: Task,
    parent_agent: str,
    agent: str,
    delegation_depth: int,
    key: str,
    stop_reason: StopReason,
    completion: BaseModel | None = None,
    diagnostics: str | None = None,
) -> Settlement:
    """Build the one settlement a child ends with, serialising its completion exactly once.

    Args:
        task: The child's freshly minted task (identity and objective).
        parent_agent: The delegating agent's own id.
        agent: The child spec's name.
        delegation_depth: The depth the child actually ran at.
        key: The delegation key :func:`delegation_key` minted for this delegation.
        stop_reason: Which of the six terminal reasons the child stopped for.
        completion: The output the runner already validated against the declared schema; present
            only for :attr:`~thymira.schemas.StopReason.COMPLETED`.
        diagnostics: Free-text diagnosis, bounded and redacted here.

    Returns:
        The settlement, carrying the `SubagentResult` to record, the updated `Task` its callers
        keep storing, and the validated completion object so no consumer re-parses it.
    """
    completed = stop_reason is StopReason.COMPLETED
    result_json = completion.model_dump_json() if completed and completion is not None else None
    result_schema = (
        f"{type(completion).__module__}:{type(completion).__qualname__}"
        if result_json is not None
        else None
    )
    bounded, truncated = bounded_diagnostics(diagnostics)
    status = _TASK_STATUS[stop_reason]
    settled = SubagentResult(
        run_id=task.run_id,
        task_id=task.id,
        agent_id=task.agent_id,
        agent=agent,
        parent_agent=parent_agent,
        objective=task.objective,
        delegation_depth=delegation_depth,
        delegation_key=key,
        stop_reason=stop_reason,
        result_json=result_json,
        result_schema=result_schema,
        diagnostics=bounded,
        diagnostics_limit=DIAGNOSTICS_LIMIT,
        diagnostics_truncated=truncated,
    )
    return Settlement(
        result=settled,
        task=task.model_copy(
            update={
                "status": status,
                "summary": result_json,
                # A declined task has not failed: its reason lives on the message, not the record.
                "error": None if stop_reason is StopReason.DECLINED else bounded,
                "completed_at": None if stop_reason in _OPEN_TASK_REASONS else utc_now(),
            }
        ),
        completion=completion if completed else None,
    )


def settle_stopped(
    *,
    task: Task,
    parent_agent: str,
    agent: str,
    delegation_depth: int,
    reason: str,
    event_log: EventLog,
    actor: Actor,
) -> Settlement:
    """Settle a child the orchestrator stopped before it ever ran, through the same constructor.

    A task skipped because a dependency did not complete is a terminal outcome like any other; it
    settles as ``stopped`` on the chain rather than being recorded only in graph state.
    """
    key = delegation_key(
        run_id=task.run_id,
        task_id=task.id,
        agent_id=task.agent_id,
        parent_agent=parent_agent,
        agent=agent,
        objective=task.objective,
        delegation_depth=delegation_depth,
    )
    settlement = build_settlement(
        task=task,
        parent_agent=parent_agent,
        agent=agent,
        delegation_depth=delegation_depth,
        key=key,
        stop_reason=StopReason.STOPPED,
        diagnostics=reason,
    )
    record_settlement(event_log, settlement.result, actor)
    return settlement


def record_settlement(event_log: EventLog, result: SubagentResult, actor: Actor) -> Event:
    """Append the settlement to the canonical chain, before the parent can report success.

    ``log_only`` by default: the settlement is *evidence*, and the model-visible rendering of the
    same outcome stays on ``agent.message`` where `PromptBuilder`/`current_surface` read it.
    """
    return event_log.append(
        EventType.SUBAGENT_SETTLED,
        actor,
        result.to_json_dict(),
        subject_id=result.task_id,
    )


def record_settlement_message(
    event_log: EventLog,
    *,
    result: SubagentResult,
    task: Task,
    actor: Actor,
) -> Event:
    """Append the keyed parent rendering after its durable settlement."""
    return event_log.append(
        EventType.AGENT_MESSAGE,
        actor,
        {
            "parent_agent": result.parent_agent,
            "agent": result.agent,
            "depth": result.delegation_depth,
            "status": task.status.value,
            "summary": task.summary,
            "text": render_settlement_text(
                result.agent, task.status, task.summary, result.diagnostics
            ),
            "task_id": task.id,
            "delegation_key": result.delegation_key,
            "stop_reason": result.stop_reason.value,
        },
        subject_id=task.id,
        surface=EventSurface.MODEL_VISIBLE,
    )


def _matching_lifecycle(event: Event, parked: Event) -> bool:
    """Match lifecycle evidence to one parked task without trusting human-readable objectives."""
    return (
        event.subject_id == parked.subject_id
        and event.payload.get("task_id") == parked.payload.get("task_id")
        and event.payload.get("delegation_key") == parked.payload.get("delegation_key")
    )


def _terminal_completion(event: Event, parked: Event) -> bool:
    """Whether ``event`` is a terminal lifecycle close for ``parked``."""
    return (
        event.type is EventType.AGENT_COMPLETED
        and _matching_lifecycle(event, parked)
        and event.payload.get("status") != TaskStatus.PENDING.value
    )


def _matching_record(event: Event, parked: Event) -> bool:
    """Match a settlement or parent rendering to one parked delegation key."""
    return event.payload.get("task_id") == parked.payload.get("task_id") and event.payload.get(
        "delegation_key"
    ) == parked.payload.get("delegation_key")


def _latest_parks(events: Sequence[Event]) -> list[Event]:
    """Return the latest pending checkpoint for each logical delegation invocation."""
    latest: dict[tuple[object, object, object], Event] = {}
    for event in events:
        if event.type is not EventType.AGENT_PARKED:
            continue
        if event.payload.get("status") != TaskStatus.PENDING.value:
            continue
        identity = (
            event.subject_id,
            event.payload.get("task_id"),
            event.payload.get("delegation_key"),
        )
        latest[identity] = event
    return sorted(latest.values(), key=lambda event: event.seq)


def _is_identity_digest(value: object) -> bool:
    """Whether a parked execution digest has the canonical sha256 spelling."""
    return (
        isinstance(value, str)
        and len(value) == _IDENTITY_DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _parked_key_recomputes(events: Sequence[Event], parked: Event) -> bool:
    """Check a park's immutable invocation binding against its one preceding start."""
    starts = [
        event
        for event in events
        if event.type is EventType.AGENT_STARTED
        and event.seq < parked.seq
        and _matching_lifecycle(event, parked)
    ]
    if len(starts) != 1:
        return False
    start = starts[0]
    objective = start.payload.get("objective")
    task_id = parked.payload.get("task_id")
    agent_id = parked.subject_id
    parent_agent = parked.payload.get("parent_agent")
    agent = parked.payload.get("agent")
    depth = parked.payload.get("delegation_depth")
    key = parked.payload.get("delegation_key")
    if (
        not isinstance(objective, str)
        or not objective
        or parked.payload.get("objective") != objective
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(agent_id, str)
        or not agent_id
        or not isinstance(parent_agent, str)
        or not parent_agent
        or not isinstance(agent, str)
        or not agent
        or not isinstance(depth, int)
        or isinstance(depth, bool)
        or depth < 0
        or not isinstance(key, str)
        or not key
    ):
        return False
    return (
        delegation_key(
            run_id=events[0].run_id,
            task_id=task_id,
            agent_id=agent_id,
            parent_agent=parent_agent,
            agent=agent,
            objective=objective,
            delegation_depth=depth,
        )
        == key
    )


def _valid_parked_prefix(
    events: Sequence[Event], parked: Event, completion: Event, settlement: Event
) -> bool:
    """Check the terminal records agree with one parked invocation and each other."""
    try:
        completion_reason = StopReason(completion.payload.get("stop_reason"))
        result = SubagentResult.model_validate(settlement.payload)
    except (TypeError, ValueError, ValidationError):
        return False
    if result.stop_reason is not completion_reason:
        return False
    return (
        _is_identity_digest(parked.payload.get("execution_identity_sha256"))
        and _parked_key_recomputes(events, parked)
        and result.run_id == events[0].run_id
        and result.task_id == parked.payload.get("task_id")
        and result.agent_id == parked.subject_id
        and result.delegation_key == parked.payload.get("delegation_key")
        and result.parent_agent == parked.payload.get("parent_agent")
        and result.agent == parked.payload.get("agent")
        and result.delegation_depth == parked.payload.get("delegation_depth")
    )


def _parked_prefix_closed(events: Sequence[Event], parked: Event) -> bool:
    """Require the complete terminal lifecycle, settlement and parent notice after a park."""
    starts = [
        event
        for event in events
        if event.type is EventType.AGENT_STARTED
        and event.seq < parked.seq
        and _matching_lifecycle(event, parked)
    ]
    if len(starts) != 1:
        return False
    completions = [
        event for event in events if event.seq > parked.seq and _terminal_completion(event, parked)
    ]
    if len(completions) != 1:
        return False
    settlements = [
        event
        for event in events
        if event.seq > completions[0].seq
        and event.type is EventType.SUBAGENT_SETTLED
        and _matching_record(event, parked)
    ]
    if len(settlements) != 1:
        return False
    if not _valid_parked_prefix(events, parked, completions[0], settlements[0]):
        return False
    messages = [
        event
        for event in events
        if event.seq > settlements[0].seq
        and event.type is EventType.AGENT_MESSAGE
        and _matching_record(event, parked)
    ]
    if len(messages) != 1:
        return False
    message = messages[0]
    result = SubagentResult.model_validate(settlements[0].payload)
    return (
        message.payload.get("status") == _TASK_STATUS[result.stop_reason].value
        and message.payload.get("stop_reason") == result.stop_reason.value
    )


def has_open_parked_invocation(events: Sequence[Event]) -> bool:
    """Return whether a parked invocation lacks a complete terminal evidence prefix."""
    parks = _latest_parks(events)
    return any(not _parked_prefix_closed(events, parked) for parked in parks)


def _task_from_result(result: SubagentResult) -> Task:
    """Reconstruct the terminal task carried by an already persisted settlement."""
    return Task(
        id=result.task_id,
        run_id=result.run_id,
        agent_id=result.agent_id,
        objective=result.objective,
        status=_TASK_STATUS[result.stop_reason],
        summary=result.result_json,
        error=None if result.stop_reason is StopReason.DECLINED else result.diagnostics,
        completed_at=None if result.stop_reason in _OPEN_TASK_REASONS else result.settled_at,
    )


def finalize_parked_invocation(  # noqa: PLR0912, PLR0915  # bounded lifecycle repair orchestration
    event_log: EventLog,
    *,
    actor: Actor,
    stop_reason: StopReason,
    diagnostics: str | None = None,
) -> Settlement | None:
    """Close one open parked delegation before its Run crosses a terminal boundary.

    The append order is deliberately ``agent.completed`` -> ``subagent.settled`` -> keyed
    ``agent.message``. A parked checkpoint remains in-flight and carries no settlement; this
    finalizer is the bounded crash-repair seam used by Run cancellation/failure and unrecoverable
    resume paths. Repeating it after the first complete prefix is idempotent, while a partial
    settlement/message prefix is rejected instead of inventing a second invocation.
    """
    if stop_reason not in {StopReason.STOPPED, StopReason.ABNORMAL, StopReason.FAILED}:
        raise ValueError("parked finalization requires a stopped, abnormal, or failed reason")
    events = event_log.events()
    parks = _latest_parks(events)
    open_parks: list[Event] = []
    for parked in parks:
        if _parked_prefix_closed(events, parked):
            continue
        open_parks.append(parked)
    if not open_parks:
        return None
    if len(open_parks) > 1:
        raise ValueError("multiple parked delegations require explicit lifecycle ownership")
    parked = open_parks[-1]
    task_id = parked.payload.get("task_id")
    agent_id = parked.subject_id
    key = parked.payload.get("delegation_key")
    agent = parked.payload.get("agent")
    parent_agent = parked.payload.get("parent_agent")
    depth = parked.payload.get("delegation_depth")
    if not all(
        isinstance(value, str) and value for value in (task_id, agent_id, key, agent, parent_agent)
    ):
        raise ValueError("parked delegation is missing its immutable identity")
    task_id = cast("str", task_id)
    agent_id = cast("str", agent_id)
    key = cast("str", key)
    agent = cast("str", agent)
    parent_agent = cast("str", parent_agent)
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        raise ValueError("parked delegation has an invalid depth")
    execution_identity_sha256 = parked.payload.get("execution_identity_sha256")
    if not _is_identity_digest(execution_identity_sha256):
        raise ValueError("parked delegation is missing a valid execution identity digest")
    execution_identity_sha256 = cast("str", execution_identity_sha256)
    prior_starts = [
        event
        for event in events
        if event.type is EventType.AGENT_STARTED
        and event.seq < parked.seq
        and _matching_lifecycle(event, parked)
    ]
    if len(prior_starts) != 1:
        raise ValueError("parked delegation does not have exactly one matching start")
    start = prior_starts[0]
    objective = start.payload.get("objective")
    if not isinstance(objective, str) or not objective:
        raise ValueError("parked delegation start has no objective")
    if parked.payload.get("objective") != objective:
        raise ValueError("parked delegation objective does not match its start")
    for field in ("agent", "parent_agent", "delegation_depth"):
        if start.payload.get(field) != parked.payload.get(field):
            raise ValueError(f"parked delegation {field} changed after it started")
    if (
        delegation_key(
            run_id=event_log.run_id,
            task_id=task_id,
            agent_id=agent_id,
            parent_agent=parent_agent,
            agent=agent,
            objective=objective,
            delegation_depth=depth,
        )
        != key
    ):
        raise ValueError("parked delegation key does not recompute")
    related_settlements = [
        event
        for event in events
        if event.seq > parked.seq
        and event.type is EventType.SUBAGENT_SETTLED
        and _matching_record(event, parked)
    ]
    related_messages = [
        event
        for event in events
        if event.seq > parked.seq
        and event.type is EventType.AGENT_MESSAGE
        and _matching_record(event, parked)
    ]
    terminal_events = [
        event for event in events if event.seq > parked.seq and _terminal_completion(event, parked)
    ]
    if len(terminal_events) > 1:
        raise ValueError("parked delegation has duplicate terminal completions")
    terminal = terminal_events[-1] if terminal_events else None
    effective_reason = stop_reason
    if terminal is not None:
        try:
            effective_reason = StopReason(terminal.payload.get("stop_reason"))
        except ValueError as exc:
            raise ValueError("parked delegation terminal completion has an invalid reason") from exc
        if effective_reason not in {
            StopReason.STOPPED,
            StopReason.ABNORMAL,
            StopReason.FAILED,
        }:
            raise ValueError("parked delegation terminal completion has an incompatible reason")
    if related_messages and not related_settlements:
        raise ValueError("parked delegation has a parent message without settlement")
    if related_settlements and terminal is None:
        raise ValueError("parked delegation has a settlement without terminal completion")
    if len(related_settlements) > 1:
        raise ValueError("parked delegation has duplicate terminal settlements")
    if len(related_messages) > 1:
        raise ValueError("parked delegation has duplicate parent messages")
    if terminal is not None and any(event.seq <= terminal.seq for event in related_settlements):
        raise ValueError("parked delegation settlement precedes terminal completion")
    if related_settlements and any(
        event.seq <= related_settlements[0].seq for event in related_messages
    ):
        raise ValueError("parked delegation parent message precedes settlement")
    if terminal is None:
        bounded, truncated = bounded_diagnostics(diagnostics)
        event_log.append(
            EventType.AGENT_COMPLETED,
            actor,
            {
                "agent": agent,
                "task_id": task_id,
                "status": _TASK_STATUS[effective_reason].value,
                "end_reason": effective_reason.value,
                "stop_reason": effective_reason.value,
                "step_key": parked.payload.get("step_key"),
                "execution_identity_sha256": execution_identity_sha256,
                "reason": bounded,
                "diagnostics_truncated": truncated,
                **{
                    field: parked.payload[field]
                    for field in ("parent_agent", "delegation_key", "delegation_depth")
                    if field in parked.payload
                },
            },
            subject_id=agent_id,
        )
        events = event_log.events()
    if related_settlements:
        try:
            result = SubagentResult.model_validate(related_settlements[0].payload)
        except ValidationError as exc:
            raise ValueError("parked delegation settlement is malformed") from exc
        task = _task_from_result(result)
    else:
        settlement = build_settlement(
            task=Task(
                id=task_id,
                run_id=event_log.run_id,
                agent_id=agent_id,
                objective=objective,
            ),
            parent_agent=parent_agent,
            agent=agent,
            delegation_depth=depth,
            key=key,
            stop_reason=effective_reason,
            diagnostics=diagnostics,
        )
        record_settlement(event_log, settlement.result, actor)
        result = settlement.result
        task = settlement.task
    if result.stop_reason is not effective_reason:
        raise ValueError("parked delegation settlement disagrees with terminal completion")
    if not related_messages:
        record_settlement_message(event_log, result=result, task=task, actor=actor)
    return Settlement(result=result, task=task)


def settlements_from_events(events: Sequence[Event]) -> list[SubagentResult]:
    """Read every settlement back off the chain, in log order.

    A ``subagent.settled`` payload that does not validate is *skipped*, never coerced: an
    unreadable record is not a settlement, and reading it as one would be the fail-open this
    whole path exists to avoid. MIRA reports the same payload as a finding.
    """
    settlements: list[SubagentResult] = []
    for event in events:
        if event.type is not EventType.SUBAGENT_SETTLED:
            continue
        try:
            settlements.append(SubagentResult.model_validate(event.payload))
        except ValidationError:
            continue
    return settlements


def select_canonical(candidates: Sequence[SubagentResult]) -> SubagentResult | None:
    """Pick the one canonical settlement of a delegation from every candidate recorded for it.

    Args:
        candidates: Settlements for a single delegation, in log order.

    Returns:
        The latest candidate, or ``None`` when there is none.

    Raises:
        ValueError: The candidates do not share one ``delegation_key``. A mixed set is a caller
            bug, and resolving it silently would let a selection cross two delegations.
    """
    if not candidates:
        return None
    keys = {candidate.delegation_key for candidate in candidates}
    if len(keys) > 1:
        msg = f"candidates span {len(keys)} delegation keys; a selection is per delegation"
        raise ValueError(msg)
    return candidates[-1]


__all__ = [
    "DIAGNOSTICS_LIMIT",
    "DelegationIdentity",
    "PendingDelegation",
    "Settlement",
    "bounded_diagnostics",
    "build_settlement",
    "delegation_key",
    "finalize_parked_invocation",
    "has_open_parked_invocation",
    "record_settlement",
    "record_settlement_message",
    "render_settlement_text",
    "select_canonical",
    "settle_stopped",
    "settlements_from_events",
]
