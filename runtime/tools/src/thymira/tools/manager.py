"""Authorization and lifecycle orchestration for registered tools."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from thymira.events import scrub_credentials, scrub_credentials_value
from thymira.policies import (
    APPROVAL_SCOPE_DIGEST_KEY,
    DELEGATION_DEPTH_KEY,
    ApprovalScope,
    allows_execution,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    ArtifactKind,
    Decision,
    EventType,
    ExecutionConstraints,
    SandboxMode,
    ToolCall,
    ToolCallStatus,
    new_id,
    utc_now,
)
from thymira.tools.approval_ticket import (
    TicketDisposition,
    TicketOutcome,
    closed_scope_reason,
    rejection_reason,
    ticket_disposition,
    tool_intent_sha256,
    unanswered,
)
from thymira.tools.discovery_witness import (
    DISCOVERY_TOOLS,
    DiscoverySourceWitness,
    DiscoveryWitnessLimitError,
    capture_discovery_source,
)
from thymira.tools.intent import bind_tool_intent
from thymira.tools.models import (
    BudgetRefusal,
    ToolContext,
    ToolExecution,
    ToolExecutionError,
    ToolResult,
    ToolResultCode,
)
from thymira.tools.refusals import (
    MISSING_EVIDENCE_REFUSAL,
    TOOL_CALL_LIMIT_REFUSAL,
    agent_allowlist_refusal,
)
from thymira.tools.result_evidence import recordable_result
from thymira.tools.results import (
    ToolFailureValue,
    ToolResultValidationError,
    canonical_result,
    validate_tool_result,
)
from thymira.tools.sandbox.workspace_tree import WorkspaceLock, WorkspaceTreeError
from thymira.tools.spill import spill_result

if TYPE_CHECKING:
    from datetime import datetime

    from thymira.policies import ToolCapability
    from thymira.schemas import Approval, Event, PolicyDecision
    from thymira.tools.approval_ticket import Answer
    from thymira.tools.intent import ToolIntent
    from thymira.tools.models import Tool
    from thymira.tools.registry import ToolRegistry


_MAX_TOOL_NAME = 64
_MAX_DISCOVERY_DIAGNOSTIC = 256
_MAX_ARGUMENT_VALIDATION_ERRORS = 3
_MAX_ARGUMENT_VALIDATION_DETAIL = 512
_MAX_ARGUMENT_VALIDATION_LOCATION = 128
_MAX_ARGUMENT_VALIDATION_MESSAGE = 256


def _credit_of(disposition: TicketDisposition) -> tuple[Answer | None, Approval | None, str | None]:
    """The human answer a disposition carries, the Approval it rebuilds, and the scope it spends.

    All three are None together: without a human answer there is no credit, so there is no
    scope to record as spent on the execution either -- a PASS buys its own execution and closes
    nothing.
    """
    answer = disposition.answer
    if answer is None:
        return None, None, None
    return answer, answer.approval, disposition.scope_sha256


def _fallthrough_outcome(
    disposition: TicketDisposition, decision: PolicyDecision
) -> tuple[TicketOutcome, str | None]:
    """What a denial that reached the Gate has to record about the ticket, and which scope.

    A refusal the log alone settles never gets here (:meth:`ToolManager._settled_by_the_log`), so
    the two outcomes left are the engine's ``BLOCK`` and whatever the credit fold found. A closure
    that leaves the Run alive still raises a fresh review, but it is not a call nobody has
    answered yet: recording it as ``pending`` made a credit that ran out of scope
    indistinguishable on the chain from a review still waiting for a human, and the closure reason
    is precisely what a later auditor cannot re-derive from a silence.
    """
    if decision.decision is Decision.BLOCK:
        return TicketOutcome.UNAVAILABLE, None
    if disposition.closure is not None:
        return TicketOutcome.CANCELLED, disposition.scope_sha256
    return TicketOutcome.PENDING, None


class ToolManager:
    """Authorize and execute tools while recording a complete event lifecycle."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def capabilities(self) -> tuple[ToolCapability, ...]:
        """Expose declared capabilities to Core's pre-execution Gate, never executable tools."""
        return self._registry.capabilities()

    @property
    def registry(self) -> ToolRegistry:
        """Expose the wrapped registry so callers can bind the same tools to agents."""
        return self._registry

    def abort_pending(  # noqa: PLR0911  # each identity guard fails closed before append
        self,
        context: ToolContext,
        tool_call_id: str,
        *,
        actor: Actor | None = None,
        reason: str = "tool call cancelled before dispatch",
    ) -> ToolExecution | None:
        """Close one pending review as an aborted call that never reached a tool.

        This is the cancellation seam for a deferred PydanticAI tool call.  It accepts only a
        call whose own ``tool.denied`` event says ``ticket_outcome=pending`` and for which the
        log has no started or completed event.  A missing, malformed, already settled, or
        otherwise unrelated call is a no-op, so a caller cannot manufacture a result for work
        that was never issued.  The append-only completion carries the original call id and the
        exact ticket while explicitly recording ``dispatched=False``.

        The caller supplies the authenticated cancellation actor.  That actor is evidence about
        who requested the abort, not new tool authority; the event actor remains the registered
        tool actor like every other Tool Manager lifecycle fact.
        """
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        try:
            if context.event_log.verify().valid is not True:
                return None
            events = context.event_log.events()
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        if any(
            event.type is EventType.TOOL_COMPLETED
            and event.payload.get("tool_call_id") == tool_call_id
            for event in events
        ) or any(
            event.type is EventType.TOOL_STARTED
            and event.payload.get("tool_call_id") == tool_call_id
            for event in events
        ):
            return None
        lifecycle_events = [
            event
            for event in events
            if event.payload.get("tool_call_id") == tool_call_id
            and event.type
            in {EventType.TOOL_DENIED, EventType.TOOL_STARTED, EventType.TOOL_COMPLETED}
        ]
        pending = lifecycle_events[-1] if lifecycle_events else None
        if (
            pending is None
            or pending.type is not EventType.TOOL_DENIED
            or pending.payload.get("ticket_outcome") != TicketOutcome.PENDING.value
        ):
            return None
        details = _pending_abort_details(pending.payload)
        if details is None:
            return None
        (
            tool_name,
            arguments,
            ticket,
            run_id,
            agent_id,
            task_id,
            decision_id,
            sandbox_mode,
        ) = details
        if (
            context.run_id != run_id
            or context.event_log.run_id != run_id
            or context.agent_id != agent_id
            or context.task_id != task_id
            or pending.run_id != run_id
            or pending.subject_id != tool_call_id
            or pending.actor.kind is not ActorKind.TOOL
            or pending.actor.id != tool_name
            or not pending.actor.authenticated
            or pending.payload.get("agent_id") != agent_id
            or pending.payload.get("run_id") != run_id
        ):
            return None
        try:
            expected_ticket = tool_intent_sha256(
                tool_name,
                arguments,
                sandbox_mode=SandboxMode(sandbox_mode) if sandbox_mode is not None else None,
            )
        except (TypeError, ValueError):
            return None
        if ticket != expected_ticket:
            return None
        if not _ticket_issuance_matches(
            events,
            pending,
            tool_call_id=tool_call_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            ticket=ticket,
            decision_id=decision_id,
            sandbox_mode=sandbox_mode,
        ):
            return None
        safe_reason = _abort_reason(reason)
        failure = ToolFailureValue(
            text=safe_reason,
            error=safe_reason,
            code=ToolResultCode.ABORTED_BEFORE_DISPATCH,
            aborted=True,
        )
        result = ToolResult(
            success=False,
            value=failure,
            error=safe_reason,
            code=ToolResultCode.ABORTED_BEFORE_DISPATCH,
            aborted=True,
        )
        try:
            call = ToolCall(
                id=tool_call_id,
                run_id=run_id,
                agent_id=agent_id,
                task_id=task_id,
                tool_name=tool_name,
                arguments=arguments,
                status=ToolCallStatus.FAILED,
                policy_decision_id=decision_id,
                sandbox_mode=sandbox_mode,
                completed_at=context.now(),
                error=safe_reason,
            )
        except (TypeError, ValueError, ValidationError):
            return None
        abort_actor = actor or Actor.system()
        context.event_log.append(
            EventType.TOOL_COMPLETED,
            _tool_actor(tool_name),
            {
                "tool_call_id": tool_call_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "task_id": task_id,
                "tool": tool_name,
                "status": ToolCallStatus.FAILED,
                "exit_code": None,
                "result_code": result.code,
                "timeout_s": None,
                "aborted": True,
                "aborted_before_dispatch": True,
                "dispatched": False,
                "artifact_ids": [],
                "result_sha256": None,
                "error": safe_reason,
                "sandbox_mode": sandbox_mode,
                "sandbox_enforcement": None,
                "sandbox_spec": None,
                "sandbox_cleanup_confirmed": None,
                "tool_intent_sha256": ticket,
                "decision_id": decision_id,
                "ticket_outcome": TicketOutcome.CANCELLED.value,
                "cause": "aborted_before_dispatch",
                "initiator": abort_actor.to_json_dict(),
                "value": _canonical_result_value(result),
            },
            subject_id=tool_call_id,
            producer="thymira.tools",
            causation_id=pending.event_id,
        )
        return ToolExecution(call=call, result=result)

    def execute(  # noqa: PLR0911, PLR0912  # ordered fail-closed lifecycle branches stay explicit
        self,
        context: ToolContext,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolExecution:
        """Authorize and execute a registered tool, recording a paired started/completed lifecycle.

        An unknown tool name is a recorded failed call, never an exception: a model proposing a
        tool that does not exist is ordinary traffic that must leave evidence, so it produces a
        FAILED ``ToolCall`` and a ``tool.completed`` event, and nothing is authorized or executed.

        A fresh ``REQUIRE_HUMAN_REVIEW`` decision from ``check_capability`` may already carry a
        synchronous human approver's answer -- the Gate consulted it in process and wrote
        ``human.approval`` before returning -- so this method re-reads the log once through the
        same ``human_answer`` fold a retry uses: a human's yes authorizes this exact call, a
        human's no denies it as a final rejection, and an automatic approver's answer is left as
        the plain denial it always was.

        Discovery calls also receive an execution-time source witness from the manager, kept
        separate from the registered producer; a pre/post mismatch fails the call so no caller
        can treat a result as complete after the workspace changed during its scan. Once a call
        has started, no exception escapes before ``tool.completed`` is recorded. An
        expected ``ToolExecutionError`` and any other exception a tool raises are both caught and
        recorded as a failed call; the two stay distinguishable because an unexpected exception's
        type name is carried in the error text. This withdraws the older promise that programming
        errors propagate, because at this boundary it cannot hold:

        - MIRA's A9 control pairs ``tool.started`` with ``tool.completed``; an escaping exception
          leaves a started with no completed, and the append-only log can never be corrected.
        - The post-call bookkeeping (artifact diffing, usage charging) would be skipped -- the
          same fail-open shape the lifecycle exists to close.
        - The MCP surface exposes third-party tools, so "programming error" is not a category this
          boundary can tell apart from an expected failure.

        ``KeyboardInterrupt`` and ``SystemExit`` are not caught and still propagate; a traceback is
        never persisted.
        """
        # Canonical events carry the validated model-visible arguments byte-for-byte. Export and
        # trace boundaries redact their copies; redacting here would make the approval ticket and
        # event history differ from the request the model proposed. A direct caller that supplies a
        # known credential is a different case: refuse it before registry lookup, authorization or
        # execution, so one call cannot have a scrubbed event and a raw intent/execution.
        recorded_name = _recorded_tool_name(tool_name)
        real_arguments = dict(arguments or {})
        try:
            source_safe_arguments = scrub_credentials_value(real_arguments)
        except ValueError:
            source_safe_arguments = None
        if source_safe_arguments != real_arguments:
            safe_arguments = (
                source_safe_arguments if isinstance(source_safe_arguments, dict) else {}
            )
            call = ToolCall(
                id=new_id("tool"),
                run_id=context.run_id,
                agent_id=context.agent_id,
                task_id=context.task_id,
                tool_name=recorded_name,
                arguments=safe_arguments,
            )
            return self._record_failed_call(
                context,
                call,
                recorded_name,
                "tool arguments contain a credential-shaped value",
            )
        call = ToolCall(
            id=new_id("tool"),
            run_id=context.run_id,
            agent_id=context.agent_id,
            task_id=context.task_id,
            tool_name=recorded_name,
            arguments=real_arguments,
        )
        try:
            tool = self._registry.get(tool_name)
        except KeyError:
            # An unknown tool is a recorded failed call, not a crash: nothing is authorized or run,
            # but the attempt still leaves the same evidence an argument-validation failure would.
            return self._record_failed_call(
                context, call, recorded_name, f"unknown tool: {recorded_name}"
            )
        call = call.model_copy(update={"sandbox_mode": tool.capability.sandbox_mode})
        allowlist_error = _allowlist_error(context, tool_name)
        if allowlist_error is not None:
            return self._record_constraint_denial(
                context, call, tool_name, allowlist_error, decision_id=None
            )
        constraint_error = _constraint_error(context, tool_name, tool.capability.external_effects)
        if constraint_error is not None:
            constrained = _constrained_call(call, context, constraint_error)
            return self._record_constraint_denial(
                context,
                constrained,
                tool_name,
                constraint_error,
                decision_id=constrained.policy_decision_id,
            )
        try:
            real_arguments = _validate_arguments(tool, real_arguments)
        except (ToolExecutionError, KeyError, OSError, ValueError) as exc:
            return self._record_failed_call(context, call, recorded_name, _message(exc))
        validated_arguments = real_arguments
        intent = bind_tool_intent(tool_name, validated_arguments, tool.capability)
        ticket = intent.ticket
        scope = ApprovalScope(
            run_id=context.run_id,
            tool_intent_sha256=ticket,
            expires_at=context.now() + context.approval_ttl,
            delegation_depth=context.delegation_depth,
        )
        disposition = ticket_disposition(context, ticket, tool.capability)
        settled = self._settled_by_the_log(
            context, disposition, call, tool_name, validated_arguments, ticket
        )
        if settled is not None:
            return settled
        answer, approval, spent_scope = _credit_of(disposition)
        decision = (
            answer.decision
            if answer is not None
            else self._fresh_decision(
                context, call, tool, tool_name, validated_arguments, intent, scope
            )
        )
        if answer is None and decision.requires_human_approval:
            # A synchronous *human* approver may have answered inside `check_capability`; the
            # Gate recorded that answer as evidence, never as authority. Re-read the log: a
            # human's yes is the ticket for this very call and goes through the same
            # `allows_execution` check as a retry; a human's no is final; an automatic answer
            # is no answer (design decision 3) and the call stays denied.
            disposition = ticket_disposition(context, ticket, tool.capability)
            settled = self._settled_by_the_log(
                context,
                disposition,
                call,
                tool_name,
                validated_arguments,
                ticket,
                decision_id=decision.id,
            )
            if settled is not None:
                return settled
            answer, approval, spent_scope = _credit_of(disposition)
        call = call.model_copy(update={"policy_decision_id": decision.id})
        # The one authorization check for a review-gated tool, Contract 0.3's: the decision the
        # Gate recorded and a separate human Approval that names it. A ticket is only ever that
        # Approval, reconstructed from the human's own event -- never a new lenient branch.
        if not allows_execution(decision, approval):
            pending = (
                approval is None
                and decision.requires_human_approval
                and unanswered(context, decision.id)
            )
            outcome, closed_scope = _fallthrough_outcome(disposition, decision)
            return self._record_review_denial(
                context,
                call,
                tool_name,
                validated_arguments,
                ticket,
                f"tool call denied: {decision.reason}",
                pending_approval=decision if pending else None,
                ticket_outcome=outcome,
                closed_scope_sha256=closed_scope,
            )

        if context.budget_guard is not None:
            refusal = context.budget_guard()
            if refusal is not None:
                # The denial names the decision that *refused* the call, not the capability
                # decision that allowed it a moment earlier: a Run's evidence has to say what
                # stopped an execution, and the guard's own budget decision is that answer.
                if isinstance(refusal, str):
                    refusal = BudgetRefusal(refusal)
                return self._record_constraint_denial(
                    context, call, tool_name, refusal.reason, decision_id=refusal.decision_id
                )
        try:
            with WorkspaceLock(context.workspace):
                return self._execute_started(
                    context,
                    tool_name,
                    tool,
                    real_arguments,
                    validated_arguments,
                    intent,
                    call,
                    spent_scope=spent_scope,
                )
        except (OSError, WorkspaceTreeError) as exc:
            # The lock is the boundary for every workspace effect. A lock or pending-publication
            # failure happens before the lifecycle starts, so record an ordinary failed call and
            # leave the policy decision and exact approval facts intact.
            return self._record_failed_call(
                context,
                call,
                tool_name,
                f"workspace effect lock unavailable: {_message(exc)}",
            )

    def _execute_started(
        self,
        context: ToolContext,
        tool_name: str,
        tool: Tool,
        real_arguments: dict[str, Any],
        validated_arguments: dict[str, Any],
        intent: ToolIntent,
        call: ToolCall,
        *,
        spent_scope: str | None,
    ) -> ToolExecution:
        """Run the started lifecycle while the workspace effect lock is held."""
        before_artifact_ids = {artifact.id for artifact in context.artifact_store.list_active()}
        source_witness: DiscoverySourceWitness | None = None
        if tool_name in DISCOVERY_TOOLS:
            try:
                source_witness = capture_discovery_source(
                    context.workspace, tool_name, validated_arguments
                )
            except (OSError, UnicodeDecodeError, ValueError, re.error) as exc:
                return self._record_failed_call(
                    context,
                    call,
                    tool_name,
                    _discovery_failure("FS_DISCOVERY_WITNESS", exc),
                )
        context.event_log.append(
            EventType.TOOL_STARTED,
            _tool_actor(tool_name),
            {
                "tool_call_id": call.id,
                "task_id": call.task_id,
                "tool": tool_name,
                # The *validated* arguments, exactly as `human.approval_requested` and
                # `tool.denied` record them: the three events describe one ticketed call, and the
                # ticket is computed from this payload, so MIRA can recompute it from the start
                # alone. The raw request stays on the recorded `ToolCall`.
                "arguments": validated_arguments,
                **intent.evidence(),
                "decision_id": call.policy_decision_id,
                # The scope actually spent, and the depth it was spent at. `None` when no human
                # credit paid for this call: a PASS buys its own execution and closes nothing.
                # These two keys are what makes a closed scope durable evidence -- MIRA reads
                # them off the chain and recomputes them, rather than trusting the manager.
                APPROVAL_SCOPE_DIGEST_KEY: spent_scope,
                DELEGATION_DEPTH_KEY: context.delegation_depth,
                "discovery_witness_query_sha256": (
                    source_witness.query_sha256 if source_witness is not None else None
                ),
                "discovery_witness_source_sha256": (
                    source_witness.source_sha256 if source_witness is not None else None
                ),
            },
            subject_id=call.id,
        )
        running = call.model_copy(update={"status": ToolCallStatus.RUNNING})
        try:
            result = tool.execute(context.for_tool(), real_arguments)
        except ToolExecutionError as exc:
            result = ToolResult(success=False, error=_message(exc))
        except Exception as exc:  # noqa: BLE001  # a tool is third-party; an escape here strands tool.started
            # Recorded, never swallowed: the type name keeps an unexpected crash distinguishable
            # from an expected ToolExecutionError, and no traceback is persisted onto the chain.
            result = ToolResult(success=False, error=f"unexpected {_describe(exc)}")

        try:
            result = validate_tool_result(tool, result)
        except ToolResultValidationError as exc:
            error = _message(exc)
            result = _failure_result(result if isinstance(result, ToolResult) else None, error)

        return self._close_record(
            context,
            tool_name,
            running,
            result,
            before_artifact_ids,
            source_witness=source_witness,
            source_arguments=validated_arguments,
        )

    def _fresh_decision(
        self,
        context: ToolContext,
        call: ToolCall,
        tool: Tool,
        tool_name: str,
        validated_arguments: dict[str, Any],
        intent: ToolIntent,
        scope: ApprovalScope,
    ) -> PolicyDecision:
        """Ask the Gate to decide this call, recording the scope the answer would be bound to.

        The scope rides on the same details the request already carries, so the very event a
        human is shown says when the authority she is being asked for stops existing -- and a
        later reader recomputes that binding from the request's own fields.
        """
        return context.gate.check_capability(
            subject_id=call.id,
            capability=tool.capability,
            risk=context.risk_profile,
            summary=tool_name,
            details={
                "tool": tool_name,
                "arguments": validated_arguments,
                **intent.evidence(),
                **scope.to_payload(),
            },
            inherited_constraints=context.execution_constraints,
        )

    def _settled_by_the_log(
        self,
        context: ToolContext,
        disposition: TicketDisposition,
        call: ToolCall,
        tool_name: str,
        validated_arguments: dict[str, Any],
        ticket: str,
        *,
        decision_id: str | None = None,
    ) -> ToolExecution | None:
        """Deny the two calls the log alone decides, before the Gate is asked anything.

        A human's refusal is final for the Run, and a credit whose Run has ended cannot be
        replaced by a fresh review -- raising one would ask a human to answer for a Run nobody
        can resume. Every other closure (the pass that raised the credit ended, or its deadline
        passed) leaves the Run alive, so it falls through and asks again under a new decision --
        and :func:`_fallthrough_outcome` records that closure on the denial all the same, so the
        chain never confuses a credit that ran out of scope with a review nobody has answered.

        Returns:
            The recorded denial, or ``None`` when nothing on the log settles this call.
        """
        answer = disposition.answer
        if disposition.outcome is TicketOutcome.REJECTED and answer is not None:
            rejected = call.model_copy(
                update={"policy_decision_id": decision_id or answer.decision.id}
            )
            return self._record_review_denial(
                context,
                rejected,
                tool_name,
                validated_arguments,
                ticket,
                rejection_reason(answer),
                ticket_outcome=TicketOutcome.REJECTED,
            )
        closure = disposition.closure
        if closure is not None and closure.terminates_the_run:
            return self._record_review_denial(
                context,
                call,
                tool_name,
                validated_arguments,
                ticket,
                closed_scope_reason(closure),
                ticket_outcome=TicketOutcome.CANCELLED,
                closed_scope_sha256=disposition.scope_sha256,
            )
        return None

    def _settle_discovery_witness(
        self,
        context: ToolContext,
        running: ToolCall,
        result: ToolResult,
        *,
        source_witness: DiscoverySourceWitness | None,
        source_arguments: dict[str, Any] | None,
    ) -> tuple[ToolResult, str | None]:
        """Recapture the discovery source, refuse drift, and persist the witness artifact.

        Returns the possibly-failed result together with the persisted witness artifact id, or
        ``None`` when this call is not a discovery call or the witness could not be retained.
        Every failure here is recorded on the result rather than raised: the call has already
        emitted ``tool.started`` and must still reach ``tool.completed``.
        """
        if source_witness is None or not result.success or source_arguments is None:
            return result, None
        try:
            current_witness = capture_discovery_source(
                context.workspace, source_witness.tool, source_arguments
            )
        except Exception as exc:  # noqa: BLE001  # preserve the started/completed lifecycle
            return _failure_result(
                result, _discovery_failure("FS_DISCOVERY_SOURCE_DRIFT", exc)
            ), None
        drifted = (
            current_witness.query_sha256 != source_witness.query_sha256
            or current_witness.source_sha256 != source_witness.source_sha256
            or current_witness.matches != source_witness.matches
            or current_witness.skipped != source_witness.skipped
        )
        if drifted:
            return _failure_result(
                result,
                "FS_DISCOVERY_SOURCE_DRIFT: workspace changed during the discovery call",
            ), None
        try:
            witness_name = f"discovery-source/{source_witness.tool}/{new_id('artifact')}.json"
            witness_bytes = source_witness.serialized_payload()
            witness = context.artifact_store.save_bytes(
                witness_name,
                witness_bytes,
                produced_by=running.id,
                kind=ArtifactKind.LOG,
            )
        except DiscoveryWitnessLimitError as exc:
            return _failure_result(result, _discovery_failure("FS_DISCOVERY_WITNESS", exc)), None
        except Exception as exc:  # noqa: BLE001  # preserve the lifecycle on store errors
            return _failure_result(result, _discovery_failure("FS_DISCOVERY_WITNESS", exc)), None
        return result, witness.id

    def _close_record(
        self,
        context: ToolContext,
        tool_name: str,
        running: ToolCall,
        result: ToolResult,
        before_artifact_ids: set[str],
        *,
        source_witness: DiscoverySourceWitness | None = None,
        source_arguments: dict[str, Any] | None = None,
    ) -> ToolExecution:
        """Spill, announce artifacts and always close the call with a completed event.

        Split out of :meth:`execute` because it is the half that carries the lifecycle
        guarantee: everything here runs on behalf of a call that has already started, so no
        step of it may raise past the completed event.
        """
        # Everything from here to the completed event runs on behalf of a call that has already
        # started, so none of it may raise: the artifact store is a real filesystem, and
        # ``result.artifact_ids`` is as tool-controlled as the rest of the result. A failure here
        # costs the spill or the artifact announcement and is recorded on the call, never the
        # completed event itself.
        try:
            result = spill_result(
                result, context, max_inline_bytes=64 * 1024, produced_by=running.id
            )
        except Exception as exc:  # noqa: BLE001  # see above: an escape here strands tool.started
            result = _failure_result(result, f"unexpected {_describe(exc)} spilling the result")
        for declared in result.events:
            # A declared event's type and payload come from the tool, so appending one is still
            # inside the tool's reach: a payload the log cannot canonicalize would raise here,
            # after `tool.started` and before `tool.completed`, which is the very gap this method
            # closes one line above. The failure is recorded on the call and the loop stops rather
            # than half-announcing what the tool did.
            try:
                context.event_log.append(
                    declared.type,
                    _tool_actor(tool_name),
                    declared.payload,
                    subject_id=running.id,
                )
            except Exception as exc:  # noqa: BLE001  # see above: the payload is tool-controlled
                result = _failure_result(
                    result,
                    f"unexpected {_describe(exc)} recording a tool event",
                )
                break
        result, discovery_witness_artifact_id = self._settle_discovery_witness(
            context,
            running,
            result,
            source_witness=source_witness,
            source_arguments=source_arguments,
        )
        raw_artifact_ids = result.artifact_ids
        if isinstance(raw_artifact_ids, (str, bytes)) or not isinstance(
            raw_artifact_ids, (tuple, list)
        ):
            invalid_artifact_ids = (object(),)
            declared_ids = ()
        else:
            invalid_artifact_ids = tuple(
                value for value in raw_artifact_ids if not isinstance(value, str) or not value
            )
            declared_ids = tuple(value for value in raw_artifact_ids if isinstance(value, str))
        if invalid_artifact_ids:
            result = replace(
                _failure_result(
                    result,
                    f"invalid artifact_ids in tool result: {len(invalid_artifact_ids)} value(s)",
                ),
                artifact_ids=declared_ids,
            )
        try:
            new_artifacts = [
                artifact
                for artifact in context.artifact_store.list_active()
                if artifact.id not in before_artifact_ids
            ]
            artifact_ids = tuple(
                dict.fromkeys((*declared_ids, *(artifact.id for artifact in new_artifacts)))
            )
            for artifact in new_artifacts:
                context.event_log.append(
                    EventType.ARTIFACT_CREATED,
                    _tool_actor(tool_name),
                    {
                        "name": artifact.name,
                        "sha256": artifact.sha256,
                        "artifact_id": artifact.id,
                        "produced_by": artifact.produced_by,
                    },
                    subject_id=artifact.id,
                )
        except Exception as exc:  # noqa: BLE001  # see above: an escape here strands tool.started
            artifact_ids = declared_ids
            result = _failure_result(
                result, f"unexpected {_describe(exc)} recording produced artifacts"
            )

        # ``ToolResult`` remains an in-process envelope, so the final boundary validates every
        # scalar before it reaches the hash-chained event. A malformed field is an explicit failed
        # result; no value is silently dropped from evidence.
        status = ToolCallStatus.COMPLETED if result.success else ToolCallStatus.FAILED
        try:
            recorded = recordable_result(result)
            value_payload = _canonical_result_value(result) if result.value is not None else None
        except (ToolResultValidationError, TypeError, ValueError) as exc:
            error = _message(exc)
            invalid_sandbox_mode = result.sandbox_mode is not None and not isinstance(
                result.sandbox_mode, SandboxMode
            )
            result = replace(
                _failure_result(result, error),
                stdout="",
                stderr="",
                artifact_ids=(),
                exit_code=None,
                result_sha256=None,
                sandbox_mode=None,
                sandbox_enforcement=None,
                sandbox_spec=None,
                sandbox_cleanup_confirmed=None,
            )
            status = ToolCallStatus.FAILED
            recorded = recordable_result(result)
            recorded["sandbox_mode_invalid"] = invalid_sandbox_mode
            value_payload = _canonical_result_value(result)
        completed = running.model_copy(
            update={
                "status": status,
                "completed_at": _now(),
                "result_sha256": recorded["result_sha256"],
                "artifact_ids": artifact_ids,
                "exit_code": recorded["exit_code"],
                "error": recorded["error"],
                "sandbox_mode": running.sandbox_mode or recorded["sandbox_mode"],
                "sandbox_enforcement": recorded["sandbox_enforcement"],
            }
        )
        context.event_log.append(
            EventType.TOOL_COMPLETED,
            _tool_actor(tool_name),
            {
                "tool_call_id": completed.id,
                "task_id": completed.task_id,
                "tool": tool_name,
                "status": completed.status,
                "exit_code": completed.exit_code,
                "result_code": recorded["result_code"],
                "timeout_s": recorded["timeout_s"],
                "aborted": recorded["aborted"],
                "artifact_ids": list(artifact_ids),
                "result_sha256": completed.result_sha256,
                "error": completed.error,
                "sandbox_mode": recorded["sandbox_mode"],
                "sandbox_mode_invalid": recorded["sandbox_mode_invalid"],
                "requested_sandbox_mode": running.sandbox_mode,
                "sandbox_enforcement": completed.sandbox_enforcement,
                "sandbox_spec": recorded["sandbox_spec"],
                "sandbox_cleanup_confirmed": recorded["sandbox_cleanup_confirmed"],
                "sandbox_termination": recorded["sandbox_termination"],
                "sandbox_quota_evidence": recorded["sandbox_quota_evidence"],
                "discovery_witness_artifact_id": discovery_witness_artifact_id,
                "value": value_payload,
            },
            subject_id=completed.id,
        )
        return ToolExecution(call=completed, result=result)

    def _record_failed_call(
        self,
        context: ToolContext,
        call: ToolCall,
        tool_name: str,
        error: str,
    ) -> ToolExecution:
        """Record a call that failed before execution as a FAILED ToolCall plus completed event.

        Shared by the unknown-tool and argument-validation branches so both leave the same
        evidence: nothing ran, no policy decision was taken, and a single ``tool.completed`` event
        closes the record.
        """
        result = _failure_result(None, error)
        failed = call.model_copy(
            update={
                "status": ToolCallStatus.FAILED,
                "completed_at": _now(),
                "error": result.error,
            }
        )
        context.event_log.append(
            EventType.TOOL_COMPLETED,
            _tool_actor(tool_name),
            {
                "tool_call_id": failed.id,
                "task_id": failed.task_id,
                "tool": tool_name,
                "status": failed.status,
                "exit_code": None,
                "result_code": result.code,
                "timeout_s": result.timeout_s,
                "aborted": result.aborted,
                "artifact_ids": [],
                "result_sha256": None,
                "error": result.error,
                "sandbox_mode": None,
                "requested_sandbox_mode": failed.sandbox_mode,
                "sandbox_enforcement": None,
                "sandbox_spec": None,
                "sandbox_cleanup_confirmed": None,
                "sandbox_termination": None,
                "value": _canonical_result_value(result),
            },
            subject_id=failed.id,
        )
        return ToolExecution(call=failed, result=result)

    def _record_review_denial(
        self,
        context: ToolContext,
        call: ToolCall,
        tool_name: str,
        arguments: dict[str, Any],
        ticket: str,
        error: str,
        *,
        pending_approval: PolicyDecision | None = None,
        ticket_outcome: TicketOutcome,
        closed_scope_sha256: str | None = None,
    ) -> ToolExecution:
        """Record a Gate denial: a decision that does not allow execution, or a human's rejection.

        Unlike a constraint denial, this one carries the call's identity -- the tool, its
        validated ``arguments`` and ``tool_intent_sha256`` -- so a later
        ``human.approval`` or ``tool.started`` can be paired with it from the log alone.
        ``pending_approval`` is the decision a human still has to answer; with it set, the agent
        bridge ends the step instead of handing the model an error line.

        ``ticket_outcome`` names what the approval evidence amounted to, in the closed vocabulary
        the runtime enforces, and ``closed_scope_sha256`` names the scope that had closed when it
        was ``cancelled``. Both are recorded on the event, not merely returned: a refusal a later
        reader cannot attribute is not evidence of a refusal.
        """
        result = _failure_result(None, error)
        denied = call.model_copy(
            update={"status": ToolCallStatus.DENIED, "completed_at": _now(), "error": error}
        )
        context.event_log.append(
            EventType.TOOL_DENIED,
            _tool_actor(tool_name),
            {
                "tool_call_id": denied.id,
                "run_id": denied.run_id,
                "task_id": denied.task_id,
                "decision_id": denied.policy_decision_id,
                "reason": error,
                "tool": tool_name,
                "agent_id": context.agent_id,
                "result_code": result.code,
                "timeout_s": result.timeout_s,
                "aborted": result.aborted,
                "arguments": arguments,
                "tool_intent_sha256": ticket,
                "sandbox_mode": denied.sandbox_mode,
                "ticket_outcome": ticket_outcome.value,
                "closed_scope_sha256": closed_scope_sha256,
                "value": _canonical_result_value(result),
            },
            subject_id=denied.id,
        )
        return ToolExecution(call=denied, result=result, pending_approval=pending_approval)

    def _record_constraint_denial(
        self,
        context: ToolContext,
        call: ToolCall,
        tool_name: str,
        error: str,
        *,
        decision_id: str | None,
    ) -> ToolExecution:
        """Record a denial that no human can lift: a constraint, an allowlist or a budget ceiling.

        Unlike :meth:`_record_review_denial` the payload carries no call identity -- no arguments
        and no ``tool_intent_sha256`` -- because there is nothing for a human to answer. The
        allowlist and run-constraint branches refuse before the Gate is asked at all; the budget
        branch refuses *after* a capability decision allowed the call, and names the Core-owned
        budget decision that refused it (``BudgetRefusal.decision_id``) rather than the one that
        allowed it.
        """
        result = _failure_result(None, error)
        denied = call.model_copy(
            update={
                "status": ToolCallStatus.DENIED,
                "completed_at": _now(),
                "error": error,
            }
        )
        context.event_log.append(
            EventType.TOOL_DENIED,
            _tool_actor(tool_name),
            {
                "tool_call_id": denied.id,
                "task_id": denied.task_id,
                "decision_id": decision_id,
                "reason": error,
                "tool": tool_name,
                "agent_id": context.agent_id,
                "result_code": result.code,
                "timeout_s": result.timeout_s,
                "aborted": result.aborted,
                "value": _canonical_result_value(result),
            },
            subject_id=denied.id,
        )
        return ToolExecution(call=denied, result=result)


def _validate_arguments(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate arguments and return bounded, non-input diagnostics the model can repair."""
    if tool.arguments_model is None:
        return arguments
    try:
        validated = tool.arguments_model.model_validate(arguments)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        summaries: list[str] = []
        for error in errors[:_MAX_ARGUMENT_VALIDATION_ERRORS]:
            location = ".".join(str(item) for item in error.get("loc", ())) or "arguments"
            location = scrub_credentials(location)
            message = scrub_credentials(str(error.get("msg", "Invalid value")))
            location = _bounded_validation_fragment(location, _MAX_ARGUMENT_VALIDATION_LOCATION)
            message = _bounded_validation_fragment(message, _MAX_ARGUMENT_VALIDATION_MESSAGE)
            summaries.append(f"{location}: {message}")
        if len(errors) > _MAX_ARGUMENT_VALIDATION_ERRORS:
            summaries.append(f"{len(errors) - _MAX_ARGUMENT_VALIDATION_ERRORS} more errors")
        detail = "; ".join(summaries)[:_MAX_ARGUMENT_VALIDATION_DETAIL]
        raise ToolExecutionError(f"invalid arguments for tool '{tool.name}': {detail}") from exc
    except TypeError as exc:
        # Pydantic v2 deliberately does not wrap a validator-authored TypeError in
        # ValidationError. Plugin argument models are third-party code, so let that bug close as
        # the same pre-execution failure as an ordinary schema error instead of escaping the Tool
        # Manager without a tool.completed record. The exception may contain plugin data or a
        # credential, therefore only its scrubbed and bounded message reaches the model or log.
        message = scrub_credentials(_message(exc))
        message = _bounded_validation_fragment(message, _MAX_ARGUMENT_VALIDATION_MESSAGE)
        detail = _bounded_validation_fragment(
            f"arguments: validator raised TypeError: {message}",
            _MAX_ARGUMENT_VALIDATION_DETAIL,
        )
        raise ToolExecutionError(f"invalid arguments for tool '{tool.name}': {detail}") from exc
    return validated.model_dump(mode="python")


def _bounded_validation_fragment(value: str, limit: int) -> str:
    """Bound one diagnostic fragment while marking that it was shortened."""
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _allowlist_error(context: ToolContext, tool_name: str) -> str | None:
    """Return why an optional caller-owned tool allowlist rejects a registered tool."""
    if context.allowed_tools is None or tool_name in context.allowed_tools:
        return None
    return agent_allowlist_refusal(context.agent_id, tool_name)


def _constrained_call(call: ToolCall, context: ToolContext, error: str) -> ToolCall:
    """Stamp a run-constraint refusal onto the recorded call, under the decision that carries it.

    The refusal is taken before the Gate is asked, so the only decision there is to name is the
    Run's own execution decision -- the one whose recorded ``ExecutionConstraints`` produced this
    exact reason, which is what lets MIRA's A6 recompute it from the log alone. A Run holding no
    execution decision names none, and the denial is explained by its own reason instead.
    """
    return call.model_copy(
        update={
            "policy_decision_id": (
                context.execution_decision.id if context.execution_decision is not None else None
            ),
            "status": ToolCallStatus.DENIED,
            "error": error,
        }
    )


def _constraint_error(
    context: ToolContext, tool_name: str, external_effects: tuple[str, ...]
) -> str | None:
    """Return why a call violates the run-level constraints, without executing it.

    Every reason here is a hard refusal no human can lift, so the Gate is never asked about one.
    ``requires_human_review`` is deliberately absent: it is not a refusal but a *review*, and
    answering it once for the whole Run -- the blanket ``HUMAN_REVIEW_REFUSAL`` this used to
    return for every call -- meant no tool could run even after a human approved the Run's own
    execution-start review. It now travels into :meth:`Gate.check_capability` as this call's
    inherited constraints, so each call gets its own ticketed review and its own answer.
    """
    constraints = context.execution_constraints
    if constraints != ExecutionConstraints() and context.execution_decision is None:
        return "execution constraints have no recorded Gate decision"
    if (
        context.execution_decision is not None
        and context.execution_decision.execution_constraints != constraints
    ):
        return "execution constraints do not match the recorded Gate decision"
    error = constraints.denies_tool(tool_name, external_effects)
    if error is not None:
        return error
    if not set(constraints.required_evidence).issubset(context.available_evidence):
        return MISSING_EVIDENCE_REFUSAL
    if constraints.max_tool_calls is not None:
        attempted = sum(
            event.type in {EventType.TOOL_STARTED, EventType.TOOL_DENIED}
            for event in context.event_log.events()
        )
        if attempted >= constraints.max_tool_calls:
            return TOOL_CALL_LIMIT_REFUSAL
    return None


def _recorded_tool_name(tool_name: str) -> str:
    """Return a name that is safe to write onto the append-only log.

    The name of a *registered* tool is ours and always fine. This one may not be: an unknown tool
    name is whatever a model or an MCP client proposed, and since that case became a recorded
    failed call rather than an exception, the string now reaches ``Actor.id`` -- which the log
    does not redact, because an actor is not payload -- and ``ToolCall.tool_name``. Neither field
    bounds its length or its characters, and nothing written to a hash-chained log can be taken
    back. So it is bounded and stripped of control characters here, and an empty name becomes a
    placeholder rather than a ``ValidationError`` raised before a single event exists.
    """
    cleaned = "".join(
        character if character.isprintable() else "?" for character in tool_name.strip()
    )
    if not cleaned:
        return "<unnamed>"
    if len(cleaned) > _MAX_TOOL_NAME:
        return f"{cleaned[:_MAX_TOOL_NAME]}...(truncated)"
    return cleaned


def _message(exc: BaseException) -> str:
    """Render an exception's message without letting the rendering itself raise.

    ``str(exc)`` runs the exception's own ``__str__``, and a third-party tool -- the reason this
    boundary catches broadly at all -- can raise from it. That raise would happen inside the very
    handler meant to guarantee a completed record, so it is contained here.
    """
    try:
        return str(exc)
    except Exception:  # noqa: BLE001  # rendering the failure must never be the thing that fails
        return "<the exception could not be rendered>"


def _discovery_failure(prefix: str, exc: BaseException) -> str:
    """Render a bounded discovery diagnostic while retaining its stable failure prefix."""
    detail = " ".join(_message(exc).split())
    detail = detail[:_MAX_DISCOVERY_DIAGNOSTIC]
    if not detail:
        detail = "the source witness could not be recorded"
    return f"{prefix}: {type(exc).__name__}: {detail}"


def _describe(exc: BaseException) -> str:
    """Name an unexpected exception's type alongside its message.

    Only the unexpected path uses this. An expected ``ToolExecutionError`` keeps its bare message,
    because that message is the tool's own account of a failure it anticipated; prefixing it would
    change what every existing caller reads and would blur the very distinction the type name is
    carried to preserve.
    """
    return f"{type(exc).__name__}: {_message(exc)}"


def _canonical_result_value(result: ToolResult) -> dict[str, object]:
    """Serialise the manager's required canonical value after narrowing its optional type."""
    if result.value is None:
        raise ToolResultValidationError("tool result has no canonical value")
    return canonical_result(result.value, success=result.success)


def _failure_result(result: ToolResult | None, error: str) -> ToolResult:
    """Normalize every post-start failure to the manager-owned failure value envelope."""
    safe_error = error or "tool call failed"
    timeout = result is not None and result.code is ToolResultCode.TOOL_TIMEOUT
    budget: float | None = None
    if timeout and result is not None and type(result.timeout_s) in (int, float):
        timeout_budget = cast("float", result.timeout_s)
        if timeout_budget > 0:
            budget = timeout_budget
    aborted = (
        bool(result.aborted) if result is not None and isinstance(result.aborted, bool) else timeout
    )
    failure = ToolFailureValue(
        text=safe_error,
        error=safe_error,
        code=ToolResultCode.TOOL_TIMEOUT if timeout else ToolResultCode.FAILURE,
        budget_seconds=budget,
        aborted=aborted,
    )
    if result is None:
        return ToolResult(success=False, code=failure.code, error=safe_error, value=failure)
    return replace(
        result,
        success=False,
        code=failure.code,
        error=safe_error,
        value=failure,
        timeout_s=budget,
        aborted=aborted,
    )


def _abort_reason(reason: str) -> str:
    """Return a bounded, non-empty cancellation reason for the durable failure value."""
    if not isinstance(reason, str):
        return "tool call cancelled before dispatch"
    cleaned = reason.strip()
    if not cleaned:
        return "tool call cancelled before dispatch"
    return cleaned[:500]


def _pending_abort_details(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any], str, str, str, str | None, str, str | None] | None:
    """Read the immutable call identity needed to close a pending denial."""
    tool_name = payload.get("tool")
    arguments = payload.get("arguments")
    ticket = payload.get("tool_intent_sha256")
    run_id = payload.get("run_id")
    agent_id = payload.get("agent_id")
    task_id = payload.get("task_id")
    decision_id = payload.get("decision_id")
    raw_sandbox_mode = payload.get("sandbox_mode")
    try:
        sandbox_mode = (
            raw_sandbox_mode.value
            if isinstance(raw_sandbox_mode, SandboxMode)
            else SandboxMode(raw_sandbox_mode).value
            if raw_sandbox_mode is not None
            else None
        )
        sandbox_mode_valid = True
    except (TypeError, ValueError):
        sandbox_mode = None
        sandbox_mode_valid = False
    valid = (
        isinstance(tool_name, str)
        and bool(tool_name)
        and isinstance(arguments, dict)
        and isinstance(ticket, str)
        and bool(ticket)
        and isinstance(run_id, str)
        and bool(run_id)
        and isinstance(agent_id, str)
        and bool(agent_id)
        and (task_id is None or isinstance(task_id, str))
        and isinstance(decision_id, str)
        and bool(decision_id)
        and sandbox_mode_valid
    )
    if not valid:
        return None
    return tool_name, arguments, ticket, run_id, agent_id, task_id, decision_id, sandbox_mode


def _ticket_issuance_matches(
    events: list[Event],
    pending: Event,
    *,
    tool_call_id: str,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    ticket: str,
    decision_id: str,
    sandbox_mode: str | None,
) -> bool:
    """Require the pending denial to descend from one recorded review and policy decision."""
    if pending.payload.get("decision_id") != decision_id:
        return False
    decisions = [
        event
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.seq < pending.seq
        and event.run_id == run_id
        and event.subject_id == tool_call_id
        and event.payload.get("id") == decision_id
        and event.payload.get("decision") == Decision.REQUIRE_HUMAN_REVIEW
        and event.actor.kind is ActorKind.SYSTEM
        and event.actor.authenticated
    ]
    requests = [
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
        and event.seq < pending.seq
        and event.run_id == run_id
        and event.subject_id == tool_call_id
        and event.actor.kind is ActorKind.SYSTEM
        and event.actor.authenticated
        and event.payload.get("decision_id") == decision_id
        and event.payload.get("tool") == tool_name
        and event.payload.get("arguments") == arguments
        and event.payload.get("tool_intent_sha256") == ticket
        and event.payload.get("sandbox_mode") == sandbox_mode
    ]
    return len(decisions) == 1 and len(requests) == 1


def _tool_actor(tool_name: str) -> Actor:
    """Build the event actor for a registered tool."""
    return Actor(kind=ActorKind.TOOL, id=tool_name, authenticated=True)


def _now() -> datetime:
    """Return a UTC timestamp without exposing time handling in the public API."""
    return utc_now()
