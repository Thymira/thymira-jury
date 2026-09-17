"""Closed vocabularies shared by every component.

Adding a value is a contract change: it goes through a pull request that also updates the
event consumers (MIRA checks, the API, the CLI), never through a local string.
"""

from __future__ import annotations

from enum import StrEnum


class Layer(StrEnum):
    """The two orchestration layers of Thymira."""

    EXECUTION = "execution"  # THY and its data-science agents
    AUDIT = "audit"  # MIRA and its governance agents


class RunStatus(StrEnum):
    """Lifecycle of a Run (MVP roadmap, section 3)."""

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    EXPERIMENTING = "EXPERIMENTING"
    AUDITING = "AUDITING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is allowed from this status."""
        return self in {RunStatus.COMPLETED, RunStatus.BLOCKED, RunStatus.FAILED}


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.PLANNING, RunStatus.FAILED}),
    RunStatus.PLANNING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.BLOCKED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.EXPERIMENTING,
            RunStatus.AUDITING,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.EXPERIMENTING: frozenset(
        {RunStatus.RUNNING, RunStatus.AUDITING, RunStatus.FAILED, RunStatus.BLOCKED}
    ),
    RunStatus.AUDITING: frozenset(
        {RunStatus.COMPLETED, RunStatus.WAITING_FOR_APPROVAL, RunStatus.BLOCKED, RunStatus.FAILED}
    ),
    RunStatus.WAITING_FOR_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.AUDITING, RunStatus.COMPLETED, RunStatus.BLOCKED}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.BLOCKED: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    """Return whether ``current -> target`` is an allowed Run transition."""
    return target in RUN_TRANSITIONS[current]


class TaskStatus(StrEnum):
    """Lifecycle of a Task delegated to an agent."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StopReason(StrEnum):
    """Why a delegated sub-agent stopped — the terminal vocabulary of one settlement.

    Exactly six values, and an unknown one is rejected rather than coerced or defaulted
    (F7.1). This runtime fixes the mapping onto its own producers:

    - ``COMPLETED``: the child returned an output its declared schema validated.
    - ``OUT_OF_ROOM``: the child exhausted a bound it was given — PydanticAI's
      ``UsageLimitExceeded`` against ``UsageLimits(request_limit=spec.max_turns)``.
    - ``FAILED``: the child reported an error it could name — ``UnexpectedModelBehavior``,
      raised when output validation retries are exhausted.
    - ``DECLINED``: the child was refused the authority it needed and produced no result — a
      tool call the Gate left to a human (``AgentEndReason.AWAITING_APPROVAL``).
    - ``STOPPED``: the orchestrator stopped it before or during its run — THY skipping a task
      whose dependency did not complete.
    - ``ABNORMAL``: it died without reporting — any other exception escaping the child's run.

    The mapping is a repository decision, recorded in the Agent Note, not a verified
    equivalence with any upstream harness. :class:`TaskStatus` stays a parallel, coarser and
    stateful vocabulary: it drives the plan and the resume path, this one is terminal.
    """

    COMPLETED = "completed"
    STOPPED = "stopped"
    OUT_OF_ROOM = "out-of-room"
    DECLINED = "declined"
    FAILED = "failed"
    ABNORMAL = "abnormal"


class ToolCallStatus(StrEnum):
    """Lifecycle of a tool invocation through the Tool Manager."""

    REQUESTED = "REQUESTED"
    DENIED = "DENIED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ArtifactKind(StrEnum):
    """What an artifact is, independently of its file format."""

    DATASET = "dataset"
    MODEL = "model"
    METRICS = "metrics"
    REPORT = "report"
    PLOT = "plot"
    CODE = "code"
    LOG = "log"
    OTHER = "other"


class ExperimentStatus(StrEnum):
    """Lifecycle of an experiment tracked in MLflow."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Severity(StrEnum):
    """Severity of an audit finding."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AuditDisposition(StrEnum):
    """Audit conclusion derived from MIRA evidence; it never authorizes an action."""

    PASS = "PASS"  # noqa: S105  # a decision name, not a secret
    WARNING = "WARNING"
    REQUIRE_HUMAN_REVIEW = "REQUIRE_HUMAN_REVIEW"
    BLOCK = "BLOCK"

    @property
    def precedence(self) -> int:
        """Higher wins when several rules match (BLOCK > REQUIRE_HUMAN_REVIEW > WARNING > PASS)."""
        return _AUDIT_DISPOSITION_PRECEDENCE[self]


_AUDIT_DISPOSITION_PRECEDENCE: dict[AuditDisposition, int] = {
    AuditDisposition.PASS: 0,
    AuditDisposition.WARNING: 1,
    AuditDisposition.REQUIRE_HUMAN_REVIEW: 2,
    AuditDisposition.BLOCK: 3,
}


class AuthorizationDecision(StrEnum):
    """Policy Engine authorization conclusion for one bounded requested action."""

    ALLOW = "ALLOW"
    ALLOW_WITH_WARNING = "ALLOW_WITH_WARNING"
    REQUIRE_HUMAN_REVIEW = "REQUIRE_HUMAN_REVIEW"
    DENY = "DENY"


# Compatibility alias for Contract 0.1's published audit-disposition vocabulary.
Decision = AuditDisposition


class Framework(StrEnum):
    """Governance frameworks a finding or a policy rule can reference."""

    EU_AI_ACT = "EU_AI_ACT"
    CREDIT_RISK = "CREDIT_RISK"
    GDPR = "GDPR"
    METHODOLOGY = "METHODOLOGY"
    MODEL_RISK = "MODEL_RISK"
    INTERNAL = "INTERNAL"


class ActorKind(StrEnum):
    """Who performed an action that is recorded in the event log."""

    SYSTEM = "system"
    HUMAN = "human"
    AGENT = "agent"
    TOOL = "tool"


class EventType(StrEnum):
    """The Event API vocabulary (baseline, section 8) plus the governance events it implies."""

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_TRANSITIONED = "run.transitioned"
    AGENT_STARTED = "agent.started"
    AGENT_PARKED = "agent.parked"
    AGENT_COMPLETED = "agent.completed"
    AGENT_MESSAGE = "agent.message"
    SUBAGENT_SETTLED = "subagent.settled"
    MODEL_SELECTED = "model.selected"
    MODEL_ROUTE_POLICY_SNAPSHOTTED = "model.route_policy_snapshotted"
    MODEL_ROUTE_DENIED = "model.route_denied"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_DENIED = "tool.denied"
    EXPERIMENT_STARTED = "experiment.started"
    EXPERIMENT_COMPLETED = "experiment.completed"
    MODEL_TRAINED = "model.trained"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_INVALIDATED = "artifact.invalidated"
    AUDIT_STARTED = "audit.started"
    AUDIT_FINDING = "audit.finding"
    AUDIT_COMPLETED = "audit.completed"
    AUDIT_BLOCK = "audit.block"
    REWORK_STARTED = "rework.started"
    REWORK_ESCALATED = "rework.escalated"
    POLICY_DECISION = "policy.decision"
    HUMAN_APPROVAL_REQUESTED = "human.approval_requested"
    HUMAN_APPROVAL = "human.approval"
    ACTIVITY_PROFILE_RECORDED = "activity_profile.recorded"
    ACTIVITY_PROFILE_QUESTIONED = "activity_profile.questioned"
    ACTIVITY_PROFILE_ANSWERED = "activity_profile.answered"
    RISK_ASSESSMENT_RECORDED = "risk_assessment.recorded"
    RISK_CLASSIFIED = "risk.classified"
    PACK_BINDING_RECORDED = "pack_binding.recorded"
    CONTROL_EVALUATION_RECORDED = "control_evaluation.recorded"
    MIRA_CONTEXT_CREATED = "mira.context_created"
    CONTEXT_COMPACTED = "context.compacted"
    MODEL_INPUT_HEADER_REVISED = "model.input_header_revised"
    MODEL_INPUT_SURFACE_UPDATED = "model.input_surface_updated"
    MODEL_REQUEST_RECORDED = "model.request_recorded"
    MODEL_RESPONSE_CHUNK = "model.response_chunk"
    TURN_ENDED = "turn.ended"
    PLAN_PUBLISHED = "plan.published"
    PLAN_TODO_WRITTEN = "plan.todo_written"
    PLAN_ITEM_COMPLETED = "plan.item_completed"
    PLAN_REVIEW_REQUESTED = "plan.review_requested"
    PLAN_REVIEWED = "plan.reviewed"
    GOAL_CREATED = "goal.created"
    GOAL_EDITED = "goal.edited"
    GOAL_ROUND_RECORDED = "goal.round_recorded"
    GOAL_BLOCKED = "goal.blocked"
    TOOL_REPEAT_REMINDER = "tool.repeat_reminder"
    MIRA_BOARD_REPLAYED = "mira.board_replayed"


class ReasoningStatus(StrEnum):
    """Closed status for provider reasoning metadata without storing reasoning text."""

    ABSENT = "absent"
    PRESENT = "present"
    REDACTED = "redacted"
    INVALID = "invalid"


class ResponseOutcome(StrEnum):
    """Closed outcome vocabulary for one provider request."""

    SUCCESS = "success"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    PARSE_FAILURE = "parse_failure"
    UNKNOWN = "unknown"


class SandboxMode(StrEnum):
    """The file-effect confinement a tool call *requested*.

    Declared at full width now even though the MVP only ever requests one of them: this is a
    closed vocabulary in a frozen contract, and adding a value later breaks every consumer.
    """

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS = "danger_full_access"


class SandboxEnforcement(StrEnum):
    """How much of the requested confinement was *actually achieved*.

    Enforcement is a reported fact, not an intention (ADR-0005). ``FULL`` means the backend
    governed every file effect the mode promised; ``PARTIAL`` means it governed only a subset —
    an older kernel, a weaker backend, or no confinement at all — and ``UNUSABLE`` means it could
    not enforce and refused to run. A caller that needs an absolute boundary must never read
    ``PARTIAL`` as ``FULL``.

    Recording the value is this contract's job; acting on it is not. The MIRA control that raises
    a finding when evidence was produced under either of the weaker two is roadmap task
    ``TOOL-24`` (control ``A19``) and does not exist yet — no control reads this field today.
    """

    FULL = "full"
    PARTIAL = "partial"
    UNUSABLE = "unusable"


class EventSurface(StrEnum):
    """Whether an event's payload may ever reach a model's context.

    Fixed when the event is appended and never changed afterwards. ``LOG_ONLY`` is the default
    everywhere: an event reaches a model only by saying so, so a new event type is fail-safe.
    """

    MODEL_VISIBLE = "model_visible"
    LOG_ONLY = "log_only"


class SurfaceState(StrEnum):
    """What an event is *in the model's current view* — derived from the log, never stored.

    A :class:`EventSurface.MODEL_VISIBLE` event starts ``CURRENT`` and becomes ``SHADOWED`` once
    a later ``context.compacted`` event names its ``seq``. ``SHADOWED`` is deliberately absent
    from :class:`EventSurface`: an event is immutable once it is chained, so the state that
    changes over time is computed by folding the log and never written back to the record.
    """

    CURRENT = "current"
    SHADOWED = "shadowed"
    LOG_ONLY = "log_only"
