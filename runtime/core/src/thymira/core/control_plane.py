"""The minimal MIRA-to-Run authorization path.

MIRA may submit an :class:`ActionIntent`; it cannot write a Run state. This module composes the
deterministic policy decision, Gate approval evidence, exact authorization scope, and the one
persisted transition writer. Local JSONL append and projection replacement remain separate file
operations owned by ``LocalRunStore``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from thymira.agents.settlement import has_open_parked_invocation
from thymira.core.phases import Phase, can_reopen
from thymira.core.run_state import (
    RunProjection,
    RunTransitionCommand,
    RunTransitionKind,
    RunTransitionResult,
    apply_transition,
    initial_run_state,
)
from thymira.events import (
    VerificationResult,
    canonical_json,
    hash_authorization_context,
    scrub_credentials_value,
    verify_events,
)
from thymira.mira.checks import (
    InvariantObservation,
    InvariantRegistry,
    build_invariant_registry,
)
from thymira.mira.flow import canonical_graph_definition_hash
from thymira.policies import Gate, PolicyEngine
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    Approval,
    AuthorizationContext,
    AuthorizationDecision,
    Decision,
    Event,
    EventSurface,
    EventType,
    PolicyDecision,
    RunState,
    approval_names_decision,
    new_id,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.policies import Approver
    from thymira.state import LocalRunStore
    from thymira.thy.models import ReworkSignal


class AuthorizationError(ValueError):
    """Base error for a context that cannot authorize a persisted transition."""


class AuthorizationScopeError(AuthorizationError):
    """Raised when an intent, decision, approval, or context has a different scope."""


class ExpiredAuthorizationError(AuthorizationError):
    """Raised when an authorization context has passed its explicit expiry."""


class ReusedAuthorizationError(AuthorizationError):
    """Raised when an authorization context already appears on a transition event."""


@dataclass(frozen=True, slots=True)
class ControlPlaneResult:
    """The recorded authorization evidence and optional transition from one intent."""

    decision: PolicyDecision
    context: AuthorizationContext
    approval: Approval | None
    event: Event | None
    state: RunProjection | None


_TRANSITIONS: dict[ActionKind, RunTransitionKind] = {
    ActionKind.REQUEST_INFORMATION: RunTransitionKind.WAIT_FOR_INFORMATION,
    ActionKind.PAUSE_RUN: RunTransitionKind.PAUSE,
    ActionKind.RESUME_RUN: RunTransitionKind.RESUME,
    ActionKind.REQUEST_APPROVAL: RunTransitionKind.WAIT_FOR_APPROVAL,
    ActionKind.REVIEW_FINDINGS: RunTransitionKind.WAIT_FOR_APPROVAL,
    ActionKind.REOPEN_WORK: RunTransitionKind.REOPEN,
}

_ADVANCE_COMMANDS = frozenset(
    {
        RunTransitionKind.START,
        RunTransitionKind.BEGIN_EXECUTION,
        RunTransitionKind.BEGIN_EXPERIMENT,
        RunTransitionKind.BEGIN_AUDIT,
        RunTransitionKind.BEGIN_REPORTING,
        RunTransitionKind.REOPEN,
        RunTransitionKind.RESUME,
        RunTransitionKind.COMPLETE,
        RunTransitionKind.BLOCK,
        RunTransitionKind.FAIL,
        RunTransitionKind.CANCEL,
    }
)

_AUTHORIZATION_DECISIONS: dict[Decision, AuthorizationDecision] = {
    Decision.PASS: AuthorizationDecision.ALLOW,
    Decision.WARNING: AuthorizationDecision.ALLOW_WITH_WARNING,
    Decision.REQUIRE_HUMAN_REVIEW: AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
    Decision.BLOCK: AuthorizationDecision.DENY,
}


class RunEventLog:
    """Expose one local Run's authoritative log to the Gate.

    The MVP has one writer per Run. Each Gate append asks ``LocalRunStore`` for the current
    event-derived version, then lets the store rebuild its convenience projection.
    """

    def __init__(
        self,
        store: LocalRunStore,
        run_id: str,
        *,
        invariant_registry: InvariantRegistry | None = None,
        invariant_store: object | None = None,
        writer: Callable[..., Event] | None = None,
    ) -> None:
        self._store = store
        self.run_id = run_id
        self._invariant_registry = invariant_registry or build_invariant_registry()
        self._invariant_store = invariant_store
        self._last_invariant_observations: tuple[InvariantObservation, ...] = ()
        self._writer = writer

    def append(
        self,
        type: EventType,  # noqa: A002  # `type` is the event-contract field name
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
        """Append a typed Gate fact through the local single-writer persistence boundary."""
        if type is EventType.AUDIT_COMPLETED and payload is not None and "audit_report" in payload:
            payload = {
                **payload,
                "graph_definition_hash": canonical_graph_definition_hash(),
            }
        if self._writer is not None:
            event = self._writer(
                type,
                actor,
                payload,
                expected_version=None,
                subject_id=subject_id,
                producer=producer,
                producer_version=producer_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
                authorization_context_sha256=authorization_context_sha256,
                surface=surface,
            )
        else:
            event = self._store.append(
                self.run_id,
                type,
                actor,
                payload,
                expected_version=None,
                subject_id=subject_id,
                producer=producer,
                producer_version=producer_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
                authorization_context_sha256=authorization_context_sha256,
                surface=surface,
            )
        self._last_invariant_observations = self._invariant_registry.observe_append(
            run_id=self.run_id,
            event=event,
            events=self.events(),
            store=self._invariant_store,
        )
        return event

    def events(self) -> list[Event]:
        """Return the verified event history for this Run."""
        return self._store.events(self.run_id)

    def verify(self) -> VerificationResult:
        """Re-verify the event history exposed to the Gate."""
        return verify_events(self.events())

    @property
    def invariant_observations(self) -> tuple[InvariantObservation, ...]:
        """Return relationship observations produced by the most recent relevant append."""
        return self._last_invariant_observations


class RunController:
    """The only component that accepts an authorized persistent Run transition."""

    def __init__(
        self,
        store: LocalRunStore,
        *,
        now: Callable[[], datetime] = utc_now,
        writer: Callable[..., Event] | None = None,
    ) -> None:
        """Build a controller, optionally bound to an owner-validated event writer.

        ``writer`` is the lifecycle ``RunHandle.append`` facade.  It is deliberately a callable
        seam instead of a serialisable owner argument: a controller reconstructed from wire fields
        cannot bypass the live OS lock.  Existing callers retain the local-store writer for
        read-only and single-process compatibility; composed lifecycle workers pass the bound
        writer explicitly.
        """
        self._writer = writer
        self._store = store
        self._now = now

    def current_state(self, run_id: str) -> RunProjection:
        """Replay authoritative transition events into the current validated Run state."""
        state, _ = self._state_and_version(run_id)
        return state

    def _state_and_version(self, run_id: str) -> tuple[RunProjection, int]:
        """Replay one event snapshot and retain its append version for the CAS write."""
        state = initial_run_state(run_id)
        events = self._store.events(run_id)
        for event in events:
            if event.type is not EventType.RUN_TRANSITIONED:
                continue
            state = self._replay_transition(state, event)
        return state, len(events)

    def transition(
        self,
        *,
        intent: ActionIntent,
        decision: PolicyDecision,
        context: AuthorizationContext,
        approval: Approval | None,
    ) -> tuple[Event, RunProjection]:
        """Validate exact authorization scope, then append one authoritative transition event."""
        current, expected_version = self._state_and_version(intent.run_id)
        context_hash = hash_authorization_context(context)
        self._validate_authorization(intent, decision, context, approval, current, context_hash)
        transition = _TRANSITIONS[context.action_kind]
        result = apply_transition(
            current,
            RunTransitionCommand(transition, current.version),
        )
        event = self._append_transition(
            intent.run_id,
            result,
            expected_version=expected_version,
            payload={
                "intent": intent.model_dump(mode="json"),
                "policy_decision": decision.model_dump(mode="json"),
                "authorization_context": context.model_dump(mode="json"),
                "approval_id": approval.id if approval is not None else None,
            },
            subject_id=intent.subject_id,
            causation_id=intent.id,
            authorization_context_sha256=context_hash,
        )
        return event, result.state

    def advance(
        self,
        run_id: str,
        command: RunTransitionKind,
        *,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Event, RunProjection]:
        """Persist ordinary forward progress without routing it through policy.

        Only lifecycle progress commands are accepted here. ``RESUME`` is included for the
        RunService checkpoint continuation path; bounded MIRA actions continue to use
        :meth:`transition`, which requires a policy decision and (when required) approval.
        """
        if command not in _ADVANCE_COMMANDS:
            raise ValueError(f"{command.value} is not an ordinary progress command")
        if command in {
            RunTransitionKind.COMPLETE,
            RunTransitionKind.BLOCK,
            RunTransitionKind.FAIL,
            RunTransitionKind.CANCEL,
        } and has_open_parked_invocation(self._store.events(run_id)):
            raise ValueError("cannot terminalize a Run with an open parked delegation")
        return self._advance(run_id, command, expected_version=expected_version, payload=payload)

    def pause_owned(
        self,
        run_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Event, RunProjection]:
        """Persist an owner-authenticated host pause through the injected writer.

        Host pause is accepted by the lifecycle inbox after the API verifies its authority, then
        applied by the owner loop.  Keeping this narrow method separate from :meth:`advance`
        prevents a generic caller from treating a control action as ordinary progress, while the
        injected writer still enforces the live process-owned Run handle.
        """
        if self._writer is None:
            raise ValueError("owner-authenticated pause requires a live Run writer")
        return self._advance(run_id, RunTransitionKind.PAUSE, payload=payload)

    def _advance(
        self,
        run_id: str,
        command: RunTransitionKind,
        *,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Event, RunProjection]:
        """Apply one already-authorized lifecycle progress command."""
        current, read_version = self._state_and_version(run_id)
        version = current.version if expected_version is None else expected_version
        result = apply_transition(current, RunTransitionCommand(command, version))
        event = self._append_transition(
            run_id,
            result,
            expected_version=read_version,
            payload=payload,
        )
        return event, result.state

    def park_for_review(
        self,
        run_id: str,
        decision: PolicyDecision,
        *,
        resume_from: str | None = None,
    ) -> tuple[Event, RunProjection]:
        """Park a Run after a matching Gate review requires human approval.

        The decision must already be recorded by the Gate for this Run. This narrow operation
        keeps ``WAIT_FOR_APPROVAL`` out of the ordinary progress path while allowing the
        composition graph to persist the result of ``Gate.review_findings``.
        """
        if decision.run_id != run_id:
            raise AuthorizationScopeError("policy decision must target the Run being parked")
        if decision.decision is not Decision.REQUIRE_HUMAN_REVIEW:
            raise AuthorizationScopeError("only a human-review decision can park a Run")
        expected_payload = scrub_credentials_value(decision.to_json_dict())
        if not any(
            event.type is EventType.POLICY_DECISION and event.payload == expected_payload
            for event in self._store.events(run_id)
        ):
            raise AuthorizationScopeError("policy decision does not have a matching Gate record")
        current, expected_version = self._state_and_version(run_id)
        result = apply_transition(
            current,
            RunTransitionCommand(RunTransitionKind.WAIT_FOR_APPROVAL, current.version),
        )
        event = self._append_transition(
            run_id,
            result,
            expected_version=expected_version,
            payload={
                "policy_decision": decision.to_json_dict(),
                **({"resume_from": resume_from} if resume_from is not None else {}),
            },
            subject_id=run_id,
        )
        return event, result.state

    def reopen(
        self,
        run_id: str,
        signal: ReworkSignal,
        *,
        reopen_count: int,
        max_reopens: int,
        approval_id: str | None = None,
        source_audit_terminal_hash: str | None = None,
    ) -> tuple[Event, RunProjection]:
        """Persist one Core-authorized rework transition.

        The findings decision is already a Gate/Policy Engine fact. ``BLOCK`` can never enter
        this method, and a human-review decision must have an exact approved event in the same
        Run log. This is the only place that turns the accepted signal into a persistent
        ``run.transitioned`` fact.
        """
        if signal.decision.run_id != run_id:
            raise AuthorizationScopeError("rework decision must target the Run being reopened")
        if signal.decision.decision is Decision.BLOCK:
            raise AuthorizationScopeError("an audit BLOCK cannot authorize rework")
        events = self._store.events(run_id)
        if signal.decision.decision not in (
            Decision.PASS,
            Decision.WARNING,
            Decision.REQUIRE_HUMAN_REVIEW,
        ):
            raise AuthorizationScopeError("the policy decision cannot authorize rework")
        if not any(
            event.type is EventType.POLICY_DECISION
            and event.payload == scrub_credentials_value(signal.decision.to_json_dict())
            for event in events
        ):
            raise AuthorizationScopeError("rework decision does not have a matching Gate record")
        if signal.decision.decision is Decision.REQUIRE_HUMAN_REVIEW and not any(
            event.type is EventType.HUMAN_APPROVAL
            and approval_names_decision(event.payload, signal.decision.id)
            and event.payload.get("approved") is True
            and ("automatic" not in event.payload or event.payload["automatic"] is False)
            and event.actor.kind is ActorKind.HUMAN
            and event.actor.authenticated
            for event in events
        ):
            raise AuthorizationScopeError("approved human evidence is required before rework")
        try:
            current_phase = Phase(signal.from_phase)
            target_phase = Phase(signal.target_phase)
        except ValueError as exc:
            raise AuthorizationScopeError(
                "rework signal names an unknown analytical phase"
            ) from exc
        allowed, reason = can_reopen(
            current_phase,
            target_phase,
            reopen_count=reopen_count,
            max_reopens=max_reopens,
            justification=signal.justification,
        )
        if not allowed:
            raise AuthorizationScopeError(reason)
        current, expected_version = self._state_and_version(run_id)
        result = apply_transition(
            current,
            RunTransitionCommand(RunTransitionKind.REOPEN, current.version),
        )
        event = self._append_transition(
            run_id,
            result,
            expected_version=expected_version,
            payload={
                "policy_decision": signal.decision.to_json_dict(),
                "rework_signal": signal.to_json_dict(),
                "approval_id": approval_id,
                "source_audit_terminal_hash": source_audit_terminal_hash,
            },
            subject_id=run_id,
        )
        return event, result.state

    def _append_transition(
        self,
        run_id: str,
        result: RunTransitionResult,
        *,
        expected_version: int,
        payload: dict[str, Any] | None = None,
        subject_id: str | None = None,
        causation_id: str | None = None,
        authorization_context_sha256: str | None = None,
    ) -> Event:
        """Append one transition result through the local single-writer store."""
        event_payload: dict[str, Any] = {
            **result.event.payload(),
            "state": result.state.state.model_dump(mode="json"),
        }
        if payload:
            event_payload.update(payload)
        if self._writer is not None:
            return self._writer(
                EventType.RUN_TRANSITIONED,
                Actor.system(),
                event_payload,
                expected_version=expected_version,
                subject_id=subject_id or run_id,
                producer="thymira.core",
                producer_version="0.1",
                causation_id=causation_id,
                authorization_context_sha256=authorization_context_sha256,
            )
        return self._store.append(
            run_id,
            EventType.RUN_TRANSITIONED,
            Actor.system(),
            event_payload,
            expected_version=expected_version,
            subject_id=subject_id or run_id,
            producer="thymira.core",
            producer_version="0.1",
            causation_id=causation_id,
            authorization_context_sha256=authorization_context_sha256,
        )

    def _replay_transition(self, state: RunProjection, event: Event) -> RunProjection:
        """Reapply one stored transition and reject a semantically invalid event history."""
        try:
            command = RunTransitionKind(event.payload["command"])
            expected_version = event.payload["from_version"]
            result = apply_transition(state, RunTransitionCommand(command, expected_version))
            persisted = RunState.model_validate_json(canonical_json(event.payload["state"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"run {state.run_id}: invalid run.transitioned event {event.event_id}"
            ) from exc
        if persisted != result.state.state:
            raise ValueError(
                f"run {state.run_id}: transition event {event.event_id} has a wrong state"
            )
        return result.state

    def _validate_authorization(  # noqa: PLR0912  # exact authorization checks must stay explicit
        self,
        intent: ActionIntent,
        decision: PolicyDecision,
        context: AuthorizationContext,
        approval: Approval | None,
        current: RunProjection,
        context_hash: str,
    ) -> None:
        """Reject every path that is not the exact, current authorization scope."""
        if intent.action_kind not in _TRANSITIONS:
            raise AuthorizationScopeError(f"unsupported control-plane action: {intent.action_kind}")
        if context.expires_at is not None and context.expires_at <= self._now():
            raise ExpiredAuthorizationError("authorization context has expired")
        if any(
            event.type is EventType.RUN_TRANSITIONED
            and event.authorization_context_sha256 == context_hash
            for event in self._store.events(intent.run_id)
        ):
            raise ReusedAuthorizationError("authorization context has already been used")
        if (
            context.run_id != intent.run_id
            or context.intent_id != intent.id
            or context.subject_kind != intent.subject_kind
            or context.subject_id != intent.subject_id
            or context.action_kind != intent.action_kind
        ):
            raise AuthorizationScopeError("authorization context does not match the ActionIntent")
        if (
            decision.run_id != intent.run_id
            or decision.id != context.policy_decision_id
            or decision.subject_kind != intent.subject_kind
            or decision.subject_id != intent.subject_id
            or decision.policy_sha256 != context.policy_sha256
            or _AUTHORIZATION_DECISIONS[decision.decision] is not context.decision
        ):
            raise AuthorizationScopeError("authorization context does not match the PolicyDecision")
        if not any(
            event.type is EventType.POLICY_DECISION
            and event.causation_id == intent.id
            and event.payload == scrub_credentials_value(decision.to_json_dict())
            for event in self._store.events(intent.run_id)
        ):
            raise AuthorizationScopeError("PolicyDecision does not have a matching Gate record")
        expected_state_version = context.constraints.get("expected_state_version")
        if expected_state_version != current.version:
            raise AuthorizationScopeError("authorization context was created for a stale Run state")
        if context.constraints.get("transition") != _TRANSITIONS[context.action_kind].value:
            raise AuthorizationScopeError("authorization context has a wrong transition scope")
        if context.decision is AuthorizationDecision.DENY:
            raise AuthorizationScopeError("denied authorization contexts cannot transition a Run")
        if context.requires_approval is not (
            context.decision is AuthorizationDecision.REQUIRE_HUMAN_REVIEW
        ):
            raise AuthorizationScopeError(
                "authorization context has an invalid approval requirement"
            )
        if context.requires_approval:
            if approval is None:
                raise AuthorizationScopeError("authorization context is pending human approval")
            if (
                approval.run_id != context.run_id
                or approval.policy_decision_id != context.policy_decision_id
                or approval.authorization_context_sha256 != context_hash
                or not approval.approved
            ):
                raise AuthorizationScopeError("approval does not authorize this exact context")
            if (
                approval.approved_by.kind is ActorKind.HUMAN
                and not approval.approved_by.authenticated
            ):
                raise AuthorizationScopeError("approval requires an authenticated human actor")
            if approval.approved_by.kind not in (ActorKind.HUMAN, ActorKind.SYSTEM):
                raise AuthorizationScopeError("approval requires a trusted actor")
            # Canonical event payloads preserve model-visible values while the event writer scrubs
            # known credentials. Compare in that same source-scrubbed vocabulary, and bind the
            # event actor to the approved-by identity, so an approval cannot be rebound.
            if not any(
                event.type is EventType.HUMAN_APPROVAL
                and event.authorization_context_sha256 == context_hash
                and event.payload == scrub_credentials_value(approval.to_json_dict())
                and event.actor == approval.approved_by
                and event.actor.kind in (ActorKind.HUMAN, ActorKind.SYSTEM)
                and (event.actor.kind is ActorKind.SYSTEM or event.actor.authenticated)
                for event in self._store.events(intent.run_id)
            ):
                raise AuthorizationScopeError("approval does not have a matching Gate record")
        elif approval is not None:
            raise AuthorizationScopeError("an approval cannot expand an authorization scope")


class MiraControlPlane:
    """Compose MIRA proposals with policy, Gate evidence, and the RunController only."""

    def __init__(
        self,
        store: LocalRunStore,
        engine: PolicyEngine,
        *,
        approver: Approver | None = None,
        human: Actor | None = None,
        authorization_ttl: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if authorization_ttl <= timedelta():
            raise ValueError("authorization_ttl must be positive")
        self._store = store
        self._engine = engine
        self._approver = approver
        self._human = human
        self._authorization_ttl = authorization_ttl
        self._now = now
        self.controller = RunController(store, now=now)

    def handle(self, intent: ActionIntent) -> ControlPlaneResult:
        """Process one MIRA request without giving MIRA authority over the Run."""
        if intent.action_kind not in _TRANSITIONS:
            raise ValueError(f"unsupported control-plane action: {intent.action_kind}")
        gate = Gate(
            self._engine,
            RunEventLog(self._store, intent.run_id),
            approver=self._approver,
            human=self._human,
        )
        decision = gate.check_intent(intent)
        context = self._context(intent, decision)
        approval = gate.request_approval(
            context,
            decision,
            summary=intent.purpose,
            request_payload=intent.payload,
        )
        if context.decision is AuthorizationDecision.DENY or (
            context.requires_approval and (approval is None or not approval.approved)
        ):
            return ControlPlaneResult(decision, context, approval, None, None)
        event, state = self.controller.transition(
            intent=intent,
            decision=decision,
            context=context,
            approval=approval,
        )
        return ControlPlaneResult(decision, context, approval, event, state)

    def _context(self, intent: ActionIntent, decision: PolicyDecision) -> AuthorizationContext:
        """Create the exact, short-lived scope for one policy-approved transition request."""
        state = self.controller.current_state(intent.run_id)
        authorization_decision = _AUTHORIZATION_DECISIONS[decision.decision]
        return AuthorizationContext(
            id=new_id("authorization"),
            run_id=intent.run_id,
            intent_id=intent.id,
            policy_decision_id=decision.id,
            policy_sha256=decision.policy_sha256,
            decision=authorization_decision,
            subject_kind=intent.subject_kind,
            subject_id=intent.subject_id,
            action_kind=intent.action_kind,
            constraints={
                "expected_state_version": state.version,
                "transition": _TRANSITIONS[intent.action_kind].value,
            },
            requires_approval=authorization_decision is AuthorizationDecision.REQUIRE_HUMAN_REVIEW,
            expires_at=self._now() + self._authorization_ttl,
        )
