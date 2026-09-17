"""Delegation contract: hub-and-spoke agent.message + depth guard (THY-08).

ADR-0004 dec.5: agents talk through one hub-and-spoke channel, never peer-to-peer. `Delegator` is
the only way one agent reaches another in this member: `.delegate()` creates a `Task`, runs it
through `AgentRunner`, and records the outcome as `agent.message` — a structured summary
(`Task.summary`/`AgentResult.output`, JSON) plus a `text` rendering of it, never chain-of-thought
(this runtime never captures a model's reasoning as data in the first place, so there is nothing
else `.delegate()` could record). `text` is what `PromptBuilder` (THY-05) reads back into a later
step's history via `current_surface(events)` -- without it, the two tasks were each internally
consistent (THY-05's own tests hand-craft a `text` payload; THY-08's only ever checked `summary`)
but never wired to each other, so no agent ever actually saw what an earlier one in the same run
had done. The event also needs `surface=EventSurface.MODEL_VISIBLE`: `EventLog.append` defaults
to `LOG_ONLY` on purpose (an event reaches a model only by saying so, never by accident), and
`.delegate()` never opted in -- so `agent.message` was invisible to `current_surface` even before
`text` existed to read.

Every terminal delegation also **settles** (F7.5): `.delegate()` returns a `Settlement`, never
raises into its parent, and writes the child's `SubagentResult` to the chain as `subagent.settled`
*before* it appends the `agent.message` above. A review-pending child is parked instead: its
`agent.parked` checkpoint preserves the resume identity, while the single durable
`subagent.settled` record and keyed parent `agent.message` are written only after the continuation
reaches a terminal outcome. The settlement is the durable evidence record --
the stop reason, the canonical result, the bounded diagnostics, the delegation identity and the
depth -- while `agent.message` stays the model-visible rendering `PromptBuilder`/`current_surface`
read. The one exception is `thymira.agents.usage.UsageLimitExceededError`: a run budget breach is
a run-level fact and still propagates, but the child has already settled by then, so no auditor
ever sees a delegation that simply vanished.

`depth` is always the *caller's own* depth, not the target's: `.delegate()` always creates its
child at `depth + 1`, so a sibling relationship (same depth) is unrepresentable — not detected and
rejected, simply impossible to construct through this API. `DepthGuard` then refuses that child
depth against `spec.max_depth` (`AgentSpec`, THY-01): orchestrator (depth 0) -> sub-agent (1) ->
helper (2) is the MVP shape; a spec whose `max_depth` is 1 cannot itself delegate further.

On a FAILED result (THY-17), the raised `UnexpectedModelBehavior`/`UsageLimitExceeded` carries no
useful detail (verified empirically: just "Exceeded maximum output retries (N)", no message
history) -- but the tool calls the agent made before giving up are already on the event log
(`ToolManager`, TOOL-04), each with its own `error`. `.delegate()` reads back the events its own
`AgentRunner.run()` call just appended and uses the last failed tool call's error as the
diagnosis, falling back to the generic message only when the agent never got that far (e.g. it
failed on output validation alone, never calling a tool).

A PENDING result (Task 2) is not a failure: the step parks on a review the Gate left to a human
(`AgentRunner`'s `AWAITING_APPROVAL`), so `.delegate()` diagnoses it the same way as a FAILED one
-- the last denied tool call names what is waiting -- but the rendered text says the agent is
waiting, and the returned `Task` stays open (`error=None`, `completed_at=None`). There is no
terminal settlement until a human answers and the continuation ends.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from thymira.agents.runner import AgentRunner
from thymira.agents.settlement import (
    DelegationIdentity,
    PendingDelegation,
    bounded_diagnostics,
    build_settlement,
    delegation_key,
    record_settlement,
    record_settlement_message,
)
from thymira.agents.usage import UsageLimitExceededError
from thymira.schemas import EventType, StopReason, Task, TaskStatus, new_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from thymira.agents.resume import PendingResume
    from thymira.agents.runner import AgentContext
    from thymira.agents.settlement import Settlement
    from thymira.agents.spec import AgentSpec
    from thymira.schemas import Event

_FAILED_ERROR = "agent exceeded max_turns"
_FAILED_TOOL_STATUSES = frozenset({EventType.TOOL_COMPLETED, EventType.TOOL_DENIED})
_DIAGNOSED_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.PENDING})


@dataclass(frozen=True, slots=True)
class _Delegation:
    """The identity of one delegation, fixed before the child runs and unchanged after it."""

    task: Task
    parent_agent: str
    agent: str
    depth: int
    key: str


def _abnormal_diagnosis(exc: BaseException) -> str:
    """Name the exception a child died on, so `abnormal` is not an opaque verdict."""
    return f"{type(exc).__name__}: {exc}"


def _diagnose_failure(events: Sequence[Event]) -> str:
    """The last failed or denied tool call's own error, or the generic message if none happened.

    A `tool.completed` -- including a failed call, there is no separate `tool.failed` event --
    carries its reason as `error`; a `tool.denied` -- the Gate refusing the call outright, or a
    review left pending for a human -- carries it as `reason` instead
    (`ToolManager._record_review_denial`). Both are "what made this step end", so both are read
    here.
    """
    for event in reversed(events):
        if event.type not in _FAILED_TOOL_STATUSES:
            continue
        error = event.payload.get("error") or event.payload.get("reason")
        if error:
            return str(error)
    return _FAILED_ERROR


class DelegationDepthExceededError(RuntimeError):
    """A delegation's child depth is below 0 or exceeds the target spec's `max_depth`."""


class DelegationBoundaryError(RuntimeError):
    """A child tried to address the hub with a parent identity it does not own."""


class DepthGuard:
    """Enforces the hub-and-spoke depth ceiling against one `AgentSpec`'s declared `max_depth`."""

    def check(self, child_depth: int, spec: AgentSpec) -> None:
        """Raise unless `0 <= child_depth <= spec.max_depth`."""
        if child_depth < 0:
            msg = f"delegation depth cannot be negative, got {child_depth}"
            raise DelegationDepthExceededError(msg)
        if child_depth > spec.max_depth:
            msg = (
                f"delegation depth {child_depth} exceeds {spec.name!r}'s "
                f"max_depth ({spec.max_depth})"
            )
            raise DelegationDepthExceededError(msg)


class Delegator:
    """Runs `spec` on `ctx`'s behalf and records the outcome — the only way to reach another agent.

    There is no second method here, or anywhere in this member, that lets a caller address an
    already-running agent directly: every delegation is a fresh `Task` through `AgentRunner`, so a
    sub-agent messaging a sibling has no code path to do so.
    """

    def __init__(self, ctx: AgentContext, *, guard: DepthGuard | None = None) -> None:
        self._ctx = ctx
        self._guard = guard or DepthGuard()
        self._runner = AgentRunner()

    def _child_context(self, task: Task, child_depth: int, child_agent: str) -> AgentContext:
        """Narrow the tool context to the delegate's own task and depth.

        A sub-agent used to run on the parent's `ToolContext` verbatim -- the parent's
        `agent_id`, no `task_id` at all, and the parent's delegation depth -- so a human's
        approval of the root agent's exact call was spendable by any delegate proposing the same
        call. An approval scope is keyed on the depth it was raised at, so the delegate has to
        carry its own; the narrowed identity also makes its tool events attributable to it.

        With no tool context there is nothing to narrow and the parent's context is passed on
        unchanged except for the active child identity used to bind any nested hub call.
        """
        if self._ctx.tool_context is None:
            return replace(self._ctx, current_agent=child_agent)
        return replace(
            self._ctx,
            current_agent=child_agent,
            tool_context=replace(
                self._ctx.tool_context,
                agent_id=task.agent_id,
                task_id=task.id,
                delegation_depth=child_depth,
            ),
        )

    def delegate(
        self,
        parent_agent: str,
        spec: AgentSpec,
        objective: str,
        depth: int,
        *,
        resume: PendingResume | None = None,
        task: Task | None = None,
        skill_names: tuple[str, ...] = (),
    ) -> Settlement | PendingDelegation:
        """Run `spec` at `depth + 1` on a fresh `Task` and settle it, whatever happened.

        Args:
            parent_agent: The delegating agent's own id (an orchestrator or a sub-agent).
            spec: The sub-agent to delegate to.
            objective: The child task's objective.
            depth: The *delegator's own* depth (orchestrator is 0); the child runs at
                `depth + 1`.
            resume: A parked predecessor step's own conversation (`thymira.agents.resume`), given
                only when this delegation continues a task that previously ended `PENDING` and
                every decision it deferred is now answered. `None` (the default) is an ordinary,
                contextless delegation.
            task: The persisted `Task` being resumed. A resumed task keeps its original task and
                agent ids so the checkpoint, resume bundle and execution identity remain one
                continuation. Omit for a fresh delegation.
            skill_names: Runtime skill names explicitly selected for this task. Bodies are loaded
                only after this selection reaches `AgentRunner`.

        Returns:
            The child's terminal `Settlement`, or a `PendingDelegation` while a human answer is
            outstanding. A child that raises, whose output does not validate, or that runs out of
            room settles; a review-pending child remains open until its continuation ends.

        The child runs on a context narrowed to its own identity and depth
        (:meth:`_child_context`), never the parent's verbatim: a delegate draws on no authority a
        human extended to the agent that delegated to it.

        Raises:
            DelegationBoundaryError: ``parent_agent`` is not the active caller identity.
            DelegationDepthExceededError: `depth + 1` exceeds `spec.max_depth`.
            UsageLimitExceededError: The run's shared budget was breached. The child has already
                settled `abnormal` on the chain when this propagates; a budget breach is a
                run-level concern, not a per-task outcome (`AgentRunner`'s documented contract).
        """
        child_depth = depth + 1
        self._guard.check(child_depth, spec)

        expected_parent = self._ctx.current_agent or "thy"
        if self._ctx.delegation is not None:
            expected_parent = self._ctx.delegation.agent
            if not expected_parent:
                raise DelegationBoundaryError(
                    "delegated context is missing its active agent identity"
                )
        if parent_agent != expected_parent:
            raise DelegationBoundaryError(
                f"delegation parent {parent_agent!r} is not the active agent {expected_parent!r}"
            )

        if resume is not None and task is None:
            raise ValueError("a resumed delegation requires its persisted task")
        if task is None:
            task = Task(
                id=new_id("task"),
                run_id=self._ctx.event_log.run_id,
                agent_id=new_id("agent"),
                objective=objective,
                skill_names=skill_names,
            )
        elif resume is None:
            raise ValueError("a task may only be supplied for a resumed delegation")
        elif task.run_id != self._ctx.event_log.run_id or task.objective != objective:
            raise ValueError("resumed task does not belong to this run or objective")
        delegation = _Delegation(
            task=task,
            parent_agent=parent_agent,
            agent=spec.name,
            depth=child_depth,
            key=delegation_key(
                run_id=task.run_id,
                task_id=task.id,
                agent_id=task.agent_id,
                parent_agent=parent_agent,
                agent=spec.name,
                objective=objective,
                delegation_depth=child_depth,
            ),
        )
        child_ctx = replace(
            self._child_context(task, child_depth, spec.name),
            delegation=DelegationIdentity(
                parent_agent=parent_agent,
                delegation_key=delegation.key,
                delegation_depth=child_depth,
                agent=spec.name,
            ),
        )
        events_before = len(self._ctx.event_log.events())
        try:
            result = self._runner.run(spec, task, child_ctx, resume=resume)
        except UsageLimitExceededError as exc:
            self._record_resume_failure(delegation, resume, exc)
            self._settle(delegation, StopReason.ABNORMAL, diagnostics=_abnormal_diagnosis(exc))
            raise
        except Exception as exc:  # noqa: BLE001  # F7.5: a child settles, it never rejects its parent
            self._record_resume_failure(delegation, resume, exc)
            return self._settle(
                delegation, StopReason.ABNORMAL, diagnostics=_abnormal_diagnosis(exc)
            )
        diagnosis = (
            _diagnose_failure(self._ctx.event_log.events()[events_before:])
            if result.task_status in _DIAGNOSED_STATUSES
            else None
        )
        if result.task_status is TaskStatus.PENDING:
            return self._park(delegation, diagnostics=diagnosis)
        return self._settle(
            delegation, result.stop_reason, completion=result.output, diagnostics=diagnosis
        )

    def _record_resume_failure(
        self, delegation: _Delegation, resume: PendingResume | None, exc: BaseException
    ) -> None:
        """Close a resumed lifecycle before its abnormal settlement when the runner raised."""
        if resume is None:
            return
        if any(
            event.type is EventType.AGENT_COMPLETED
            and event.subject_id == delegation.task.agent_id
            and event.payload.get("task_id") == delegation.task.id
            and event.payload.get("delegation_key") == delegation.key
            for event in self._ctx.event_log.events()
        ):
            return
        reason, truncated = bounded_diagnostics(_abnormal_diagnosis(exc))
        self._ctx.event_log.append(
            EventType.AGENT_COMPLETED,
            self._ctx.actor,
            {
                "agent": delegation.agent,
                "task_id": delegation.task.id,
                "status": TaskStatus.FAILED.value,
                "end_reason": StopReason.ABNORMAL.value,
                "stop_reason": StopReason.ABNORMAL.value,
                "step_key": resume.identity.step_key,
                "reason": reason,
                "diagnostics_truncated": truncated,
                "resumed": True,
                "execution_identity_sha256": resume.identity.digest(),
                "parent_agent": delegation.parent_agent,
                "delegation_key": delegation.key,
                "delegation_depth": delegation.depth,
            },
            subject_id=delegation.task.agent_id,
        )

    def _park(self, delegation: _Delegation, *, diagnostics: str | None) -> PendingDelegation:
        """Keep a review-pending task open without exposing a false terminal result."""
        del diagnostics
        return PendingDelegation(
            task=delegation.task.model_copy(
                update={"status": TaskStatus.PENDING, "summary": None, "error": None}
            )
        )

    def _settle(
        self,
        delegation: _Delegation,
        stop_reason: StopReason,
        *,
        completion: BaseModel | None = None,
        diagnostics: str | None = None,
    ) -> Settlement:
        """Record the child's terminal settlement, then the parent's rendering, in that order.

        The order is the guarantee (G.3): the durable evidence is on the canonical chain before
        the parent's `agent.message` -- the only thing a model ever sees -- and before this method
        returns, so a fresh reader of `events.jsonl` reconstructs which child settled with what
        without ever consulting a producer's return value.
        """
        settlement = build_settlement(
            task=delegation.task,
            parent_agent=delegation.parent_agent,
            agent=delegation.agent,
            delegation_depth=delegation.depth,
            key=delegation.key,
            stop_reason=stop_reason,
            completion=completion,
            diagnostics=diagnostics,
        )
        record_settlement(self._ctx.event_log, settlement.result, self._ctx.actor)
        record_settlement_message(
            self._ctx.event_log,
            result=settlement.result,
            task=settlement.task,
            actor=self._ctx.actor,
        )
        return settlement


__all__ = [
    "DelegationBoundaryError",
    "DelegationDepthExceededError",
    "Delegator",
    "DepthGuard",
    "PendingDelegation",
]
