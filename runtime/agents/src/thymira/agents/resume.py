"""Resuming a parked step's own PydanticAI conversation (bug-hunt follow-up, THY-17 continuation).

`AgentRunner.run` ends a step on `DeferredToolRequests` when a tool call needs a human (Task 1).
Without this module, the next attempt at that task is a brand-new `Delegator.delegate()` call: a
fresh `Task`, a prompt rebuilt from scratch, and no trace of the call that was actually deferred --
the model is simply asked its objective again and free to phrase a different call, which mints a
different `tool_intent_sha256` and needs its own human approval every single retry (reproduced
live: `.agents/notes/implemented/deferred-tool-resume.md`).

`PendingResume` and its round trip through an `ArtifactStore` fix this at the source: the exact
`all_messages()` PydanticAI built for the parked step is serialized (`ModelMessagesTypeAdapter`,
verified to round-trip losslessly) under a name derived from `task.agent_id` -- known on both the
parking and the resuming pass, so no new field on the shared `Task` schema (Contract v0.8) is
needed. `load_pending_resume` is the other half: it reports whether every decision that step
deferred now has a human's answer (`thymira.tools.unanswered`) -- `DeferredToolResults` is
all-or-nothing per turn, verified against the installed `pydantic-ai` -- and, once ready, builds an
approvals map that always says "approved": that only tells PydanticAI to call the tool function
again, and `ToolManager.execute` inside that function is what still decides allow vs. deny, exactly
as it does for a call that was never deferred at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic_ai import DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolCallPart,
    ToolReturnPart,
)

from thymira.agents.llm.routing import ModelChoice
from thymira.events import canonical_json, sha256_text
from thymira.schemas import (
    ActorKind,
    ArtifactKind,
    EventType,
    SandboxMode,
    ThymiraModel,
    ToolCallStatus,
)
from thymira.tools import (
    ToolFailureValue,
    ToolResult,
    ToolResultCode,
    canonical_result,
    tool_intent_sha256,
    unanswered,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.messages import ModelMessage

    from thymira.events import EventLog
    from thymira.schemas import Event
    from thymira.state import ArtifactStore
    from thymira.tools import ToolContext


def _bundle_name(agent_id: str) -> str:
    """The deterministic, store-relative name a step's resume bundle is spilled under."""
    return f"resume/{agent_id}.json"


def continuation_key(agent_id: str, task_id: str) -> str:
    """Return the stable association between one parked task and its resume bundle."""
    return sha256_text(canonical_json({"agent_id": agent_id, "task_id": task_id}))


class ParkedToolIdentity(ThymiraModel):
    """The exact ticketed tool request deferred by one parked PydanticAI turn."""

    pydantic_tool_call_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_mode: SandboxMode | None = None


class ParkedExecutionIdentity(ThymiraModel):
    """Immutable identity of a parked agent step and the continuation that may resume it.

    This is persisted next to the serialized PydanticAI conversation. It is deliberately separate
    from the conversation: a valid transcript alone cannot prove which model, task, or exact tool
    ticket the runtime is allowed to continue.
    """

    model_choice: ModelChoice
    tool_calls: tuple[ParkedToolIdentity, ...] = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    step_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    def digest(self) -> str:
        """Return the content digest used to correlate persistence and resume evidence."""
        return sha256_text(canonical_json(self.model_dump(mode="json")))


def build_execution_identity(
    *,
    model_choice: ModelChoice,
    agent_id: str,
    task_id: str,
    step_key: str,
    tool_call_metadata: dict[str, dict[str, Any]],
) -> ParkedExecutionIdentity:
    """Build one validated parked-step identity from the bridge's deferred-call metadata.

    The bridge adds ``tool_intent_sha256`` and ``sandbox_mode`` only after the Tool Manager has
    recorded the denial. Missing either fact is a persistence failure, never a reason to park with
    an identity that would have to be guessed on resume.
    """
    tool_calls: list[ParkedToolIdentity] = []
    for pydantic_tool_call_id, metadata in tool_call_metadata.items():
        if not isinstance(metadata, dict):
            raise TypeError("deferred tool metadata must be an object")
        decision_id = metadata.get("decision_id")
        tool_call_id = metadata.get("tool_call_id")
        ticket = metadata.get("tool_intent_sha256")
        mode = metadata.get("sandbox_mode")
        if (
            not isinstance(decision_id, str)
            or not isinstance(tool_call_id, str)
            or not isinstance(ticket, str)
        ):
            raise TypeError("deferred tool metadata lacks its exact ticket identity")
        if mode is not None and not isinstance(mode, (str, SandboxMode)):
            raise TypeError("deferred tool metadata has an invalid sandbox mode")
        tool_calls.append(
            ParkedToolIdentity(
                pydantic_tool_call_id=pydantic_tool_call_id,
                decision_id=decision_id,
                tool_call_id=tool_call_id,
                tool_intent_sha256=ticket,
                sandbox_mode=SandboxMode(mode) if isinstance(mode, str) else mode,
            )
        )
    if not tool_calls:
        raise ValueError("a parked execution must contain at least one deferred tool call")
    return ParkedExecutionIdentity(
        model_choice=model_choice,
        tool_calls=tuple(tool_calls),
        agent_id=agent_id,
        task_id=task_id,
        step_key=step_key,
        continuation_key=continuation_key(agent_id, task_id),
    )


@dataclass(frozen=True)
class PendingResume:
    """A parked step's conversation, ready to be handed back to PydanticAI."""

    messages: tuple[ModelMessage, ...]
    deferred_tool_results: DeferredToolResults
    identity: ParkedExecutionIdentity


class ResumeStatus(StrEnum):
    """Why `load_pending_resume` did, or did not, return a `PendingResume`."""

    NOT_AVAILABLE = "not_available"
    """No resume context is attached: no `ArtifactStore`/`ToolContext` was available to load.

    A bundle name that is registered but lacks its immutable identity is ``INVALID`` and fails
    closed; it never falls back to a fresh delegation.
    """
    WAITING = "waiting"
    """A bundle exists but at least one of its decisions has no recorded `human.approval` yet.
    The caller must stay parked and delegate nothing -- resuming now would ask PydanticAI to
    resolve a turn it considers only partially answered."""
    CANCELLED = "cancelled"
    """A cancellation closed the bundle; no new delegation may consume it."""
    READY = "ready"
    """Every decision the parked turn deferred now has an answer; `resume` is set."""
    INVALID = "invalid"
    """The parked identity is absent, tampered, malformed, or no longer matches its evidence."""


@dataclass(frozen=True)
class ResumeLookup:
    """The outcome of `load_pending_resume`."""

    status: ResumeStatus
    resume: PendingResume | None = None
    reason: str | None = None


def abort_pending_resume(  # noqa: PLR0911  # fail-closed guards each malformed/foreign boundary
    store: ArtifactStore | None,
    *,
    agent_id: str,
    event_log: EventLog,
    tool_results: Mapping[str, ToolResult],
) -> tuple[str, ...]:
    """Persist interrupted provider returns for a cancelled deferred tool turn.

    ``tool_results`` is keyed by the runtime ``tool_call_id`` returned by the Tool Manager.  The
    parked bundle maps that id to PydanticAI's provider id, so this helper appends a matching
    ``ToolReturnPart`` without guessing an id or creating a result for a call that was never
    parked.  Each supplied result must also match a hash-verified, manager-produced
    ``tool.completed`` event in ``event_log`` and its causally linked pending denial.  A public
    ``ToolResult`` object alone is never proof of a dispatch outcome.  Only manager-owned
    ``ABORTED_BEFORE_DISPATCH`` failures are accepted.  The update is idempotent: an already
    cancelled bundle, or a call that already has a return part, is left unchanged.

    Returns:
        The runtime tool-call ids that were paired and persisted.
    """
    if store is None:
        return ()
    if not isinstance(agent_id, str) or not agent_id:
        return ()
    runtime_ids = {
        runtime_id for runtime_id in tool_results if isinstance(runtime_id, str) and runtime_id
    }
    if len(runtime_ids) != len(tool_results):
        return ()
    durable_events = _durable_abort_events(event_log, agent_id, runtime_ids)
    if durable_events is None:
        return ()
    loaded = _load_resume_bundle(store, agent_id)
    if loaded is None or not tool_results:
        return ()
    bundle, messages = loaded
    if bundle.get("cancelled") is True:
        return ()
    returns, paired = _aborted_return_parts(bundle, messages, tool_results, durable_events)
    if not returns:
        return ()
    messages.append(ModelRequest(parts=returns))
    updated = {
        **bundle,
        "messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
        "cancelled": True,
        "cancelled_tool_call_ids": paired,
    }
    try:
        store.save_json(
            _bundle_name(agent_id),
            updated,
            produced_by=agent_id,
            kind=ArtifactKind.OTHER,
        )
    except (OSError, TypeError, ValueError):
        return ()
    return tuple(paired)


def _load_resume_bundle(
    store: ArtifactStore | None, agent_id: str
) -> tuple[dict[str, Any], list[ModelMessage]] | None:
    """Load and validate a parked bundle without turning malformed state into a result."""
    if store is None or not store.exists(_bundle_name(agent_id)):
        return None
    try:
        bundle = store.load_json(_bundle_name(agent_id))
    except (OSError, TypeError, ValueError):
        return None
    metadata = bundle.get("tool_call_metadata") if isinstance(bundle, dict) else None
    raw_messages = bundle.get("messages") if isinstance(bundle, dict) else None
    if (
        not isinstance(bundle, dict)
        or not isinstance(metadata, dict)
        or not isinstance(raw_messages, list)
    ):
        return None
    try:
        messages = list(ModelMessagesTypeAdapter.validate_python(raw_messages))
    except (TypeError, ValueError):
        return None
    return bundle, messages


def _durable_abort_events(
    event_log: EventLog, agent_id: str, runtime_ids: set[str]
) -> dict[str, Event] | None:
    """Return hash-verified manager aborts linked to their original pending denials."""
    try:
        events = event_log.events()
        verification = event_log.verify()
        run_id = event_log.run_id
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if verification.valid is not True or not isinstance(run_id, str) or not run_id:
        return None
    candidates: dict[str, list[Event]] = {runtime_id: [] for runtime_id in runtime_ids}
    for event in events:
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        call_id = event.payload.get("tool_call_id")
        if isinstance(call_id, str) and call_id in candidates:
            candidates[call_id].append(event)
    durable: dict[str, Event] = {}
    for runtime_id, matching in candidates.items():
        if len(matching) != 1:
            return None
        event = matching[0]
        if not _is_durable_abort_event(event, event_log, run_id, agent_id, runtime_id, events):
            return None
        durable[runtime_id] = event
    return durable


def _is_durable_abort_event(
    event: Event,
    event_log: EventLog,
    run_id: str,
    agent_id: str,
    runtime_id: str,
    events: list[Event],
) -> bool:
    """Check the manager event envelope and its causally linked pending denial."""
    payload = event.payload
    if (
        event_log.run_id != run_id
        or event.run_id != run_id
        or event.producer != "thymira.tools"
        or event.subject_id != runtime_id
        or payload.get("tool_call_id") != runtime_id
        or payload.get("run_id") != run_id
        or payload.get("agent_id") != agent_id
        or payload.get("status") != ToolCallStatus.FAILED
        or payload.get("result_code") != ToolResultCode.ABORTED_BEFORE_DISPATCH
        or payload.get("aborted") is not True
        or payload.get("aborted_before_dispatch") is not True
        or payload.get("dispatched") is not False
        or payload.get("ticket_outcome") != "cancelled"
        or payload.get("cause") != "aborted_before_dispatch"
        or not isinstance(payload.get("tool"), str)
        or not payload["tool"]
        or not isinstance(payload.get("tool_intent_sha256"), str)
        or not isinstance(payload.get("value"), dict)
        or not _actor_matches_tool(event, payload["tool"])
        or not _valid_initiator(payload.get("initiator"))
    ):
        return False
    try:
        denial = [candidate for candidate in events if candidate.event_id == event.causation_id]
    except (AttributeError, TypeError):
        return False
    if len(denial) != 1:
        return False
    pending = denial[0]
    denial_payload = pending.payload
    if (
        pending.type is not EventType.TOOL_DENIED
        or pending.run_id != run_id
        or pending.seq >= event.seq
        or pending.subject_id != runtime_id
        or pending.actor.kind is not ActorKind.TOOL
        or not pending.actor.authenticated
        or pending.actor.id != payload["tool"]
        or denial_payload.get("tool_call_id") != runtime_id
        or denial_payload.get("run_id") != run_id
        or denial_payload.get("agent_id") != agent_id
        or denial_payload.get("tool") != payload["tool"]
        or denial_payload.get("task_id") != payload.get("task_id")
        or denial_payload.get("decision_id") != payload.get("decision_id")
        or denial_payload.get("ticket_outcome") != "pending"
        or denial_payload.get("tool_intent_sha256") != payload.get("tool_intent_sha256")
        or not isinstance(denial_payload.get("arguments"), dict)
        or denial_payload.get("sandbox_mode") != payload.get("sandbox_mode")
    ):
        return False
    raw_mode = payload.get("sandbox_mode")
    try:
        mode = SandboxMode(raw_mode) if raw_mode is not None else None
        expected_ticket = tool_intent_sha256(
            payload["tool"], denial_payload["arguments"], sandbox_mode=mode
        )
    except (TypeError, ValueError):
        return False
    return payload["tool_intent_sha256"] == expected_ticket


def _actor_matches_tool(event: Event, tool_name: str) -> bool:
    """Require the completion actor to be the authenticated registered tool actor."""
    return (
        event.actor.kind is ActorKind.TOOL
        and event.actor.id == tool_name
        and event.actor.authenticated
    )


def _valid_initiator(value: object) -> bool:
    """Validate the bounded cancellation actor reference without treating it as authority."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("kind"), str)
        and bool(value["kind"])
        and isinstance(value.get("authenticated"), bool)
    )


def _matches_durable_result(event: Event, result: ToolResult) -> bool:
    """Ensure the caller's structured result is the value recorded by the manager."""
    if not _is_aborted_before_dispatch(result):
        return False
    value = result.value
    if not isinstance(value, ToolFailureValue):
        return False
    try:
        canonical = canonical_result(value, success=False)
    except (TypeError, ValueError):
        return False
    return (
        event.payload.get("value") == canonical
        and event.payload.get("error") == result.error
        and event.payload.get("result_code") == result.code
        and event.payload.get("aborted") is result.aborted
    )


def _aborted_return_parts(
    bundle: dict[str, Any],
    messages: list[ModelMessage],
    tool_results: Mapping[str, ToolResult],
    durable_events: Mapping[str, Event],
) -> tuple[list[ToolReturnPart], list[str]]:
    """Build one provider return per matching manager result in original metadata order."""
    metadata = bundle["tool_call_metadata"]
    expected = [
        raw_metadata.get("tool_call_id")
        for raw_metadata in metadata.values()
        if isinstance(raw_metadata, dict)
    ]
    if (
        len(expected) != len(metadata)
        or any(not isinstance(item, str) or not item for item in expected)
        or len(set(expected)) != len(expected)
        or set(tool_results) != set(expected)
    ):
        return [], []
    calls = {
        part.tool_call_id: part
        for message in messages
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolCallPart)
    }
    returned = {
        part.tool_call_id
        for message in messages
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolReturnPart)
    }
    returns: list[ToolReturnPart] = []
    paired: list[str] = []
    for provider_id, raw_metadata in metadata.items():
        if not isinstance(provider_id, str) or not isinstance(raw_metadata, dict):
            continue
        runtime_id = raw_metadata.get("tool_call_id")
        result = tool_results.get(runtime_id) if isinstance(runtime_id, str) else None
        durable_event = durable_events.get(runtime_id) if isinstance(runtime_id, str) else None
        if (
            result is None
            or durable_event is None
            or provider_id in returned
            or provider_id not in calls
            or raw_metadata.get("decision_id") != durable_event.payload.get("decision_id")
            or calls[provider_id].tool_name != durable_event.payload.get("tool")
            or not _is_aborted_before_dispatch(result)
            or not _matches_durable_result(durable_event, result)
        ):
            continue
        value = result.value
        if not isinstance(value, ToolFailureValue):
            continue
        try:
            content = canonical_result(value, success=False)
        except (TypeError, ValueError):
            continue
        returns.append(
            ToolReturnPart(
                tool_name=calls[provider_id].tool_name,
                content=content,
                tool_call_id=provider_id,
                outcome="interrupted",
            )
        )
        paired.append(runtime_id)
    if set(paired) != set(expected):
        return [], []
    return returns, paired


def save_pending_resume(
    store: ArtifactStore,
    *,
    agent_id: str,
    identity: ParkedExecutionIdentity,
    messages: list[ModelMessage],
    tool_call_metadata: dict[str, dict[str, Any]],
) -> None:
    """Spill a parked step's conversation so a later pass can continue it.

    Args:
        store: The Run's `ArtifactStore`.
        agent_id: The step's `Task.agent_id` -- the name `load_pending_resume` looks the bundle
            up by again on the resuming pass.
        identity: The immutable model, ticket, mode and continuation binding for this step.
        messages: `AgentRunResult.all_messages()` from the call that ended in
            `DeferredToolRequests`.
        tool_call_metadata: `DeferredToolRequests.metadata` -- keyed by PydanticAI's own
            `tool_call_id`, each value the `{"decision_id", "tool_call_id", "tool_intent_sha256",
            "sandbox_mode"}` dict `thymira.agents.tool_bridge` attached when it raised
            `ApprovalRequired`.
    """
    if identity.agent_id != agent_id:
        raise ValueError("parked execution identity does not belong to the resume agent")
    bundle = {
        "messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
        "tool_call_metadata": tool_call_metadata,
        "execution_identity": identity.model_dump(mode="json"),
    }
    store.save_json(_bundle_name(agent_id), bundle, produced_by=agent_id, kind=ArtifactKind.OTHER)


def load_pending_resume(  # noqa: PLR0911  # each readiness/validity branch fails closed on its own
    store: ArtifactStore | None,
    tool_context: ToolContext | None,
    agent_id: str,
    *,
    task_id: str | None = None,
) -> ResumeLookup:
    """Look up `agent_id`'s parked conversation and whether it is ready to continue.

    Args:
        store: The Run's `ArtifactStore`, or `None` if the current pass has no tools attached.
        tool_context: The Run's `ToolContext`, or `None` likewise -- needed to check each
            decision for a recorded human answer.
        agent_id: The parked step's `Task.agent_id`, from the trailing PENDING pair
            `thymira.thy.nodes.execute._carried_outcomes` drops.
        task_id: The parked step's `Task.id`, when available. It binds a resumed bundle to the
            checkpoint continuation rather than accepting a bundle for another task using the same
            agent id.
    """
    if store is None or tool_context is None:
        return ResumeLookup(ResumeStatus.NOT_AVAILABLE)
    bundle_name = _bundle_name(agent_id)
    if not store.exists(bundle_name):
        return ResumeLookup(ResumeStatus.INVALID, reason="parked execution identity is missing")
    try:
        if problems := store.verify():
            return ResumeLookup(
                ResumeStatus.INVALID,
                reason=f"parked resume evidence failed integrity verification: {problems[0]}",
            )
        raw_bundle = store.load_json(bundle_name)
        if isinstance(raw_bundle, dict) and raw_bundle.get("cancelled") is True:
            return ResumeLookup(ResumeStatus.CANCELLED)
        identity, tool_call_metadata, messages = _read_resume_bundle(
            store, bundle_name, tool_context, agent_id, task_id
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        return ResumeLookup(
            ResumeStatus.INVALID, reason=f"invalid parked execution identity: {exc}"
        )
    decision_ids = {item.decision_id for item in identity.tool_calls}
    if any(unanswered(tool_context, decision_id) for decision_id in decision_ids):
        return ResumeLookup(ResumeStatus.WAITING)
    approvals: dict[str, bool | ToolApproved | ToolDenied] = dict.fromkeys(tool_call_metadata, True)
    resume = PendingResume(
        messages=tuple(messages),
        deferred_tool_results=DeferredToolResults(approvals=approvals),
        identity=identity,
    )
    return ResumeLookup(ResumeStatus.READY, resume)


def _read_resume_bundle(
    store: ArtifactStore,
    bundle_name: str,
    tool_context: ToolContext,
    agent_id: str,
    task_id: str | None,
) -> tuple[ParkedExecutionIdentity, dict[str, dict[str, Any]], list[ModelMessage]]:
    """Decode and validate one persisted bundle before the caller computes readiness."""
    bundle = store.load_json(bundle_name)
    if not isinstance(bundle, dict):
        raise TypeError("resume bundle must be an object")
    identity = ParkedExecutionIdentity.model_validate(bundle["execution_identity"])
    tool_call_metadata = bundle["tool_call_metadata"]
    if not isinstance(tool_call_metadata, dict):
        raise TypeError("deferred tool metadata must be an object")
    _validate_identity_evidence(identity, tool_call_metadata, tool_context, agent_id, task_id)
    messages = ModelMessagesTypeAdapter.validate_python(bundle["messages"])
    return identity, tool_call_metadata, messages


def _validate_identity_evidence(  # noqa: PLR0912  # each evidence binding is fail-closed
    identity: ParkedExecutionIdentity,
    tool_call_metadata: dict[str, Any],
    tool_context: ToolContext,
    agent_id: str,
    task_id: str | None,
) -> None:
    """Verify a persisted identity against the event evidence that created the park."""
    if identity.agent_id != agent_id:
        raise ValueError("parked identity belongs to a different agent")
    if task_id is not None and identity.task_id != task_id:
        raise ValueError("parked identity belongs to a different task")
    if identity.continuation_key != continuation_key(identity.agent_id, identity.task_id):
        raise ValueError("parked continuation association does not validate")
    metadata_ids = set(tool_call_metadata)
    identity_ids = {item.pydantic_tool_call_id for item in identity.tool_calls}
    if metadata_ids != identity_ids:
        raise ValueError("parked identity and deferred metadata name different tool calls")
    for pydantic_tool_call_id, metadata in tool_call_metadata.items():
        if not isinstance(metadata, dict):
            raise TypeError("deferred tool metadata must be an object")
        item = next(
            item
            for item in identity.tool_calls
            if item.pydantic_tool_call_id == pydantic_tool_call_id
        )
        if metadata.get("decision_id") != item.decision_id:
            raise ValueError("parked identity and deferred metadata name different decisions")
        if metadata.get("tool_call_id") != item.tool_call_id:
            raise ValueError("parked identity and deferred metadata name different tools")
        if metadata.get("tool_intent_sha256") != item.tool_intent_sha256:
            raise ValueError("parked identity and deferred metadata name different tickets")
        if _mode_value(metadata.get("sandbox_mode")) != _mode_value(item.sandbox_mode):
            raise ValueError("parked identity and deferred metadata name different modes")
    events = tool_context.event_log.events()
    parked_events = [
        event
        for event in events
        if event.type is EventType.AGENT_PARKED
        and event.subject_id == agent_id
        and event.payload.get("task_id") == identity.task_id
        and event.payload.get("status") == "PENDING"
        and event.payload.get("step_key") == identity.step_key
        and event.payload.get("execution_identity_sha256") == identity.digest()
    ]
    if len(parked_events) != 1:
        if not parked_events:
            raise ValueError("parked identity has no matching pending agent checkpoint")
        raise ValueError("parked identity has ambiguous pending agent checkpoints")
    parked = parked_events[0]
    for event in events:
        if event.seq <= parked.seq:
            continue
        if (
            event.type is EventType.AGENT_COMPLETED
            and event.subject_id == agent_id
            and event.payload.get("task_id") == identity.task_id
            and event.payload.get("status") != "PENDING"
        ):
            raise ValueError("parked execution already has a terminal completion")
        if (
            event.type is EventType.SUBAGENT_SETTLED
            and event.payload.get("task_id") == identity.task_id
        ):
            raise ValueError("parked execution already has a terminal settlement")
    expected_choice = identity.model_choice.event_payload()
    if not any(
        event.type is EventType.MODEL_SELECTED
        and all(event.payload.get(key) == value for key, value in expected_choice.items())
        for event in events
    ):
        raise ValueError("parked model choice has no matching model.selected evidence")
    for item in identity.tool_calls:
        denied = next(
            (
                event
                for event in reversed(events)
                if event.type is EventType.TOOL_DENIED
                and event.payload.get("tool_call_id") == item.tool_call_id
            ),
            None,
        )
        if denied is None:
            raise ValueError(f"parked ticket has no denial evidence for {item.tool_call_id}")
        if (
            denied.payload.get("decision_id") != item.decision_id
            or denied.payload.get("task_id") != identity.task_id
            or denied.payload.get("tool_intent_sha256") != item.tool_intent_sha256
            or _mode_value(denied.payload.get("sandbox_mode")) != _mode_value(item.sandbox_mode)
        ):
            raise ValueError(f"parked ticket evidence does not match {item.tool_call_id}")
        if not any(
            event.type is EventType.HUMAN_APPROVAL_REQUESTED
            and event.payload.get("decision_id") == item.decision_id
            and event.payload.get("tool_intent_sha256") == item.tool_intent_sha256
            and _mode_value(event.payload.get("sandbox_mode")) == _mode_value(item.sandbox_mode)
            for event in events
        ):
            raise ValueError(f"parked approval request does not match {item.tool_call_id}")


def _mode_value(value: object) -> str | None:
    """Return a sandbox mode's stable serialized value for evidence comparisons."""
    if isinstance(value, SandboxMode):
        return value.value
    return value if isinstance(value, str) else None


__all__ = [
    "ParkedExecutionIdentity",
    "ParkedToolIdentity",
    "PendingResume",
    "ResumeLookup",
    "ResumeStatus",
    "abort_pending_resume",
    "build_execution_identity",
    "continuation_key",
    "load_pending_resume",
    "save_pending_resume",
]


def _is_aborted_before_dispatch(result: ToolResult) -> bool:
    """Check the manager-owned closed result identity before projecting it into history."""
    return (
        not result.success
        and result.code is ToolResultCode.ABORTED_BEFORE_DISPATCH
        and result.aborted
        and isinstance(result.value, ToolFailureValue)
        and result.value.code is ToolResultCode.ABORTED_BEFORE_DISPATCH
        and result.value.aborted
    )
