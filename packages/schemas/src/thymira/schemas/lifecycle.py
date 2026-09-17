"""Typed contracts for durable lifecycle ownership and publication.

These records cross the API, state and worker boundaries.  They intentionally describe facts
and requests only; lock acquisition, publication and claim transitions live in ``thymira.state``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256

from pydantic import Field, PrivateAttr, model_validator
from pydantic.types import JsonValue

from thymira.schemas.actor import Actor
from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.ids import Id, new_id
from thymira.schemas.run import Run, Session
from thymira.schemas.run_state import RunState
from thymira.schemas.subagent import SubagentResult


class InboxLane(StrEnum):
    """Durable lane in which a control input waits for an owner."""

    NEXT_TURN = "next_turn"
    NEXT_STEP = "next_step"


class ControlInputKind(StrEnum):
    """Control operations that may cross the API-to-owner boundary."""

    FOLLOWUP = "followup"
    STEER = "steer"
    INJECT = "inject"
    RESUME = "resume"
    RISK_ANSWER = "risk_answer"
    APPROVAL_RESPONSE = "approval_response"
    CANCEL = "cancel"
    HOST_PAUSE = "host_pause"
    PLAN = "plan"
    DELEGATE = "delegate"


type JsonObject = dict[str, JsonValue]


class ControlInputState(StrEnum):
    """Durable state of a control input."""

    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkState(StrEnum):
    """Durable state of one executable work item."""

    PENDING = "pending"
    CLAIMED = "claimed"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class DispatchState(StrEnum):
    """Observed dispatch/effect state, separate from claim state."""

    NEVER_DISPATCHED = "never_dispatched"
    DISPATCHED = "dispatched"
    EFFECT_CONFIRMED = "effect_confirmed"
    EFFECT_UNKNOWN = "effect_unknown"


class ExecutionOutcomeKind(StrEnum):
    """Immutable outcome of a THY execution attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class AuditOutcomeKind(StrEnum):
    """Outcome of the MIRA audit of one execution outcome."""

    COMPLETED = "completed"
    FAILED = "failed"


class TurnEndReason(StrEnum):
    """Closed reason for a model turn."""

    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class OwnerReleaseReason(StrEnum):
    """Reason a process releases a Run owner handle."""

    COMPLETED = "completed"
    PARKED = "parked"
    FAILED = "failed"
    RECOVERED = "recovered"
    SHUTDOWN = "shutdown"


class FailureCause(ThymiraModel):
    """Bounded, chainable description of a driver or execution failure."""

    code: str = Field(min_length=1, max_length=100)
    phase: str = Field(min_length=1, max_length=100)
    exception_type: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    caused_by_id: Id | None = None
    effects_may_have_occurred: bool = False


class AuthorityProof(ThymiraModel):
    """Trusted API proof scoped to one durable control input.

    ``signature`` remains opaque to the state member: its issuer is the authenticated API
    composition root.  The state member still checks the exact binding fields and digest before
    persisting a command, so a proof cannot be copied to another payload or target.
    """

    proof_id: Id
    actor: Actor
    authenticated_principal_id: str = Field(min_length=1)
    authenticated_role: str = Field(min_length=1)
    permissions: tuple[str, ...] = ()
    session_id: Id
    run_id: Id
    issuer_process_id: str = Field(min_length=1)
    issuer_key_id: str = Field(min_length=1)
    issued_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(hours=24))
    request_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    input_id: Id
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_lane: InboxLane
    effective_lane: InboxLane | None = None
    kind: ControlInputKind
    target_turn_id: Id | None = None
    target_work_ids: tuple[Id, ...] = ()
    idempotency_key: str = Field(min_length=1)

    def signing_payload(self) -> dict[str, object]:
        """Return the exact proof envelope covered by the issuer signature."""
        payload = self.to_json_dict()
        del payload["signature"]
        return payload


class ControlInput(ThymiraModel):
    """A durable command waiting for the Run owner to apply it."""

    input_id: Id = Field(default_factory=lambda: new_id("input"))
    run_id: Id
    session_id: Id
    requested_lane: InboxLane
    effective_lane: InboxLane | None = None
    kind: ControlInputKind
    payload: JsonObject = Field(default_factory=dict)
    target_turn_id: Id | None = None
    target_work_ids: tuple[Id, ...] = ()
    authority: AuthorityProof
    idempotency_key: str = Field(min_length=1)
    enqueued_at: datetime = Field(default_factory=utc_now)
    state: ControlInputState = ControlInputState.PENDING
    ordinal: int = Field(default=0, ge=0)
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    claim_attempt: int = Field(default=0, ge=0)

    def binding_sha256(self) -> str:
        """Return the digest that binds authority to this exact command and target."""
        value = {
            "input_id": self.input_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "requested_lane": self.requested_lane.value,
            "effective_lane": self.effective_lane.value if self.effective_lane else None,
            "kind": self.kind.value,
            "payload": self.payload,
            "target_turn_id": self.target_turn_id,
            "target_work_ids": self.target_work_ids,
            "idempotency_key": self.idempotency_key,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(encoded.encode("utf-8")).hexdigest()


class EnqueueReceipt(ThymiraModel):
    """Durable acknowledgement for one control input."""

    input_id: Id
    run_id: Id
    accepted: bool
    duplicate: bool = False
    durable_at: datetime = Field(default_factory=utc_now)


class RunOwnerHandle(ThymiraModel):
    """Serializable identity of a live owner, fenced by a monotonic epoch.

    The state implementation attaches a private, non-serialisable OS lock to this record.  A
    caller constructing matching public fields therefore cannot acquire writer authority.
    """

    run_id: Id
    owner_id: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    acquired_at: datetime = Field(default_factory=utc_now)
    _lock_resource: object | None = PrivateAttr(default=None)
    _repository_root: str | None = PrivateAttr(default=None)


class WorkResult(ThymiraModel):
    """Durable result of one claimed work item and optional child invocation settlement."""

    kind: ExecutionOutcomeKind
    payload: JsonObject = Field(default_factory=dict)
    cause: FailureCause | None = None
    dispatch_state: DispatchState = DispatchState.EFFECT_CONFIRMED
    source_work_id: Id | None = None
    source_run_id: Id | None = None
    source_task_id: Id | None = None
    source_attempt: int | None = Field(default=None, ge=1)
    source_claim_token: str | None = Field(default=None, min_length=1)
    source_ordinal: int | None = Field(default=None, ge=0)
    source_kind: str | None = Field(default=None, min_length=1)
    source_objective: str | None = Field(default=None, min_length=1)
    source_delegation_depth: int | None = Field(default=None, ge=0)
    source_agent_id: Id | None = None
    source_agent: str | None = Field(default=None, min_length=1)
    source_parent_agent: str | None = Field(default=None, min_length=1)
    source_delegation_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subagent_result: SubagentResult | None = None

    @model_validator(mode="after")
    def _validate_subagent_binding(self) -> WorkResult:
        """Require complete child source facts and bind them to the child result envelope."""
        child = self.subagent_result
        source = (
            self.source_work_id,
            self.source_run_id,
            self.source_task_id,
            self.source_attempt,
            self.source_claim_token,
            self.source_ordinal,
            self.source_kind,
            self.source_objective,
            self.source_delegation_depth,
            self.source_agent_id,
            self.source_agent,
            self.source_parent_agent,
            self.source_delegation_key,
        )
        if child is None:
            if any(value is not None for value in source):
                raise ValueError("source binding requires subagent_result")
            return self
        if any(value is None for value in source):
            raise ValueError("delegated WorkResult requires a complete source binding")
        if (
            child.task_id != self.source_task_id
            or child.run_id != self.source_run_id
            or child.delegation_depth != self.source_delegation_depth
            or child.agent_id != self.source_agent_id
            or child.agent != self.source_agent
            or child.parent_agent != self.source_parent_agent
            or child.delegation_key != self.source_delegation_key
            or child.objective != self.source_objective
        ):
            raise ValueError("subagent_result does not match its WorkResult source binding")
        return self


class WorkItem(ThymiraModel):
    """Durable unit of work announced to a worker by ``work_id`` and ``run_id`` only."""

    work_id: Id = Field(default_factory=lambda: new_id("work"))
    run_id: Id
    ordinal: int = Field(ge=0)
    kind: str = Field(min_length=1)
    state: WorkState = WorkState.PENDING
    attempt: int = Field(default=0, ge=0)
    claim_owner: str | None = None
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    dispatch_state: DispatchState = DispatchState.NEVER_DISPATCHED
    idempotency_key: str = Field(min_length=1)
    dependency_ids: tuple[Id, ...] = ()
    delegation_depth: int = Field(default=0, ge=0)
    # ``work_id``/``kind`` identify queue dispatch.  A delegated child also carries its
    # orchestrator invocation identity, which is deliberately distinct from those dispatch
    # fields and is the only source for reconstructing a child settlement.
    task_id: Id | None = None
    objective: str | None = Field(default=None, min_length=1)
    agent_id: Id | None = None
    agent: str | None = Field(default=None, min_length=1)
    parent_agent: str | None = Field(default=None, min_length=1)
    delegation_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: JsonObject = Field(default_factory=dict)
    settled_result: WorkResult | None = None


class WorkClaim(ThymiraModel):
    """One atomic claim of a pending work item."""

    work_id: Id
    run_id: Id
    claimant_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    claim_token: str = Field(min_length=1)
    expires_at: datetime


class TurnRecord(ThymiraModel):
    """Durable ownership and terminal reason for one model turn."""

    turn_id: Id
    run_id: Id
    claimed_input_ids: tuple[Id, ...] = ()
    work_ids: tuple[Id, ...] = ()
    step_count: int = Field(default=0, ge=0)
    end_reason: TurnEndReason | None = None
    cause: FailureCause | None = None


class TurnEnded(ThymiraModel):
    """Validated payload for the canonical ``turn.ended`` event.

    An open ``TurnRecord`` may omit its terminal reason while a worker is running.  The event
    emitted after settlement is closed by construction, so transport and readers can reject an
    ambiguous turn completion without conflating it with a terminal Run state or an SSE sentinel.
    """

    turn_id: Id
    run_id: Id
    claimed_input_ids: tuple[Id, ...] = ()
    work_ids: tuple[Id, ...] = ()
    step_count: int = Field(ge=0)
    end_reason: TurnEndReason
    cause: FailureCause | None = None


class TerminalAuditBinding(ThymiraModel):
    """Exact durable scope of a terminal MIRA audit.

    A terminal audit is evidence about one settled work result and its closed turn.  The result
    digest prevents a completion written for the same work and turn after a different outcome
    from being reused during recovery.
    """

    run_id: Id
    work_id: Id
    turn_id: Id
    outcome_kind: ExecutionOutcomeKind
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    end_reason: TurnEndReason


class ExecutionOutcome(ThymiraModel):
    """Immutable execution outcome retained before audit and policy settlement."""

    outcome_id: Id
    run_id: Id
    turn_id: Id
    kind: ExecutionOutcomeKind
    cause: FailureCause | None = None
    last_checkpoint_id: str | None = None
    unknown_work_ids: tuple[Id, ...] = ()
    recorded_by_epoch: int = Field(ge=1)


class AuditOutcome(ThymiraModel):
    """Immutable MIRA result linked to one execution outcome."""

    outcome_id: Id
    run_id: Id
    source_execution_outcome_id: Id
    kind: AuditOutcomeKind
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_artifact_id: Id | None = None
    report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cause: FailureCause | None = None


class TerminalBinding(ThymiraModel):
    """Causal link from execution through MIRA and policy to the terminal Run state."""

    run_id: Id
    execution_outcome_id: Id
    audit_outcome_id: Id
    audit_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    policy_decision_id: Id
    policy_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_event_id: Id
    terminal_state: RunState


class PublicationReceipt(ThymiraModel):
    """Acknowledgement that a staged Run/session/work publication became visible."""

    publication_id: Id
    run_id: Id
    session_id: Id
    work_ids: tuple[Id, ...] = ()
    committed_at: datetime = Field(default_factory=utc_now)


class PreparedPublication(ThymiraModel):
    """Private, durable publication manifest awaiting commit or owner recovery."""

    publication_id: Id = Field(default_factory=lambda: new_id("publication"))
    run: Run
    session: Session | None = None
    initial_work: tuple[WorkItem, ...] = ()
    creation_payload: JsonObject = Field(default_factory=dict)
    creation_actor: Actor = Field(default_factory=Actor.system)
    idempotency_key: str | None = Field(default=None, min_length=1)
    request_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    staged_at: datetime = Field(default_factory=utc_now)


class OutboxNotification(ThymiraModel):
    """Durable broker notification whose payload carries no authority or mutable Run state."""

    notification_id: Id = Field(default_factory=lambda: new_id("event"))
    work_id: Id
    run_id: Id
    published: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    def work_notification(self) -> WorkNotification:
        """Project the durable outbox row to the broker's exact two-field message."""
        return WorkNotification(work_id=self.work_id, run_id=self.run_id)


class WorkNotification(ThymiraModel):
    """Closed broker payload identifying durable work without carrying mutable authority."""

    work_id: Id
    run_id: Id


__all__ = [
    "AuditOutcome",
    "AuditOutcomeKind",
    "AuthorityProof",
    "ControlInput",
    "ControlInputKind",
    "ControlInputState",
    "DispatchState",
    "EnqueueReceipt",
    "ExecutionOutcome",
    "ExecutionOutcomeKind",
    "FailureCause",
    "InboxLane",
    "JsonObject",
    "JsonValue",
    "OutboxNotification",
    "OwnerReleaseReason",
    "PreparedPublication",
    "PublicationReceipt",
    "RunOwnerHandle",
    "TerminalAuditBinding",
    "TerminalBinding",
    "TurnEndReason",
    "TurnEnded",
    "TurnRecord",
    "WorkClaim",
    "WorkItem",
    "WorkNotification",
    "WorkResult",
    "WorkState",
]
