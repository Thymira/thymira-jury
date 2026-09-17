"""HTTP boundary models for the first Thymira Run API.

These models describe requests and response envelopes only. Domain records remain in
``thymira.schemas`` so the API cannot create a second, incompatible contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from thymira.core import Phase
from thymira.mira.checks import AuditReport
from thymira.policies import PendingApproval, ToolCapability
from thymira.schemas import (
    ActivityProfile,
    Actor,
    Artifact,
    EventSurface,
    EventType,
    Experiment,
    GoalRevision,
    GoalRound,
    GoalStatus,
    Id,
    PlanArtifactRef,
    PlanBoard,
    PlanMode,
    PolicyDecision,
    PublicationReceipt,
    Run,
    SecretReference,
    SettingsSnapshot,
    TodoCompletion,
    TodoItem,
    ToolRepeatState,
)


class CreateRunRequest(BaseModel):
    """Request body accepted by ``POST /runs``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    project_id: Id | None = None
    session_id: Id | None = None
    client: str = Field(default="api", min_length=1)


class CreateRunResponse(BaseModel):
    """The committed Run snapshot and its durable publication receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    receipt: PublicationReceipt


class ResumeRunRequest(BaseModel):
    """Optional body accepted by ``POST /runs/{run_id}/resume``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunCancelRequest(BaseModel):
    """Optional body accepted by ``POST /runs/{run_id}/cancel``.

    ``actor`` is an *assertion*, never an identity: the recorded Actor is the authenticated
    principal, and a value here that names someone else is refused with 403 ``actor_mismatch``
    rather than silently ignored. ``reason`` is free text recorded beside it on the transition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str | None = Field(default=None, min_length=1)
    reason: str | None = None


class AnswerRiskInterviewRequest(BaseModel):
    """One human response to the pending activity-profile question for a Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str = Field(min_length=1)


class PendingRiskQuestionResponse(BaseModel):
    """The one activity-profile fact the caller may answer next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    profile_id: Id
    profile_version: int = Field(ge=1)
    question_number: int = Field(ge=1)


class RiskInterviewResponse(BaseModel):
    """The current event-backed profile and its pending question, when one exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run: Run
    profile: ActivityProfile
    pending_question: PendingRiskQuestionResponse | None = None
    requires_human_review: bool = False


class HumanDecisionRequest(BaseModel):
    """Optional actor assertion and note submitted to an approval endpoint.

    ``actor`` does not name who is acting -- authentication already did. It states who the caller
    believes they are, and the server refuses a request that states anyone other than the
    authenticated principal (403 ``actor_mismatch``). Omitting it is normal and correct.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str | None = Field(default=None, min_length=1)
    note: str | None = None


class DelegateRequest(BaseModel):
    """Request body accepted by ``POST /runs/{run_id}/delegate``.

    ``agent`` names the sub-agent to delegate to; ``objective`` is the child task's goal. The
    endpoint creates the Task in ``PENDING`` and returns it -- it never runs the sub-agent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str = Field(min_length=1)
    objective: str = Field(min_length=1)


class ToolDescriptor(BaseModel):
    """The read-only view of one registered tool (baseline section 7, the Tool API read side).

    ``capability`` is the tool's declared :class:`~thymira.policies.ToolCapability`: the risk
    metadata (risk tags, data access, side effects, external effects, reversibility) a caller reads
    to reason about the tool. It carries no authorization -- the Policy Engine decides.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str
    capability: ToolCapability
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ToolListResponse(BaseModel):
    """The registered tool capabilities, in registration order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ToolDescriptor, ...] = ()


class EventProjection(BaseModel):
    """An API event view whose payload is redacted and whose chain hash is source metadata.

    ``hash`` and ``prev_hash`` are intentionally absent: changing a payload for an API client
    invalidates the canonical event hash. ``source_hash`` points back to the exact event in the
    local chain, and ``projection`` makes the provenance explicit to clients and auditors.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Id
    run_id: Id
    seq: int = Field(ge=0)
    type: EventType
    schema_version: str = Field(min_length=1)
    ts: datetime
    actor: Actor
    surface: EventSurface
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    correlation_id: str | None = Field(default=None, min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    authorization_context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    subject_id: str | None = Field(default=None, min_length=1)
    projection: Literal["redacted"] = "redacted"
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SettingsPutRequest(BaseModel):
    """One compare-and-set update for a user settings namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)
    secret_refs: dict[str, SecretReference] = Field(default_factory=dict)


class SettingsListResponse(BaseModel):
    """The settings namespaces visible to the API process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SettingsSnapshot, ...] = ()


class EventPage(BaseModel):
    """A bounded page of events for one Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    items: tuple[EventProjection, ...] = ()
    next_after_seq: int | None = Field(default=None, ge=0)
    has_more: bool = False


class RunPage(BaseModel):
    """A bounded page of Runs and an opaque cursor for the next page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[Run, ...] = ()
    next_cursor: str | None = None


class RunPlan(BaseModel):
    """The ordered MVP phase plan proposed for one Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    phases: tuple[Phase, ...] = ()


class PlanArtifactResponse(PlanArtifactRef):
    """Verified rendered content paired with a persisted advisory plan artifact reference."""

    content: str


class DurablePlanResponse(BaseModel):
    """The latest complete durable goal/plan board for a Run.

    The board keeps only the content-addressed artifact reference.  The API verifies the referenced
    artifact in the Run's artifact store and includes its rendered text for a read-only client view.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    plan_id: Id
    revision: int = Field(ge=0)
    goal: str | None = None
    goal_revision: GoalRevision | None = None
    goal_status: GoalStatus = GoalStatus.NONE
    items: tuple[TodoItem, ...] = ()
    completions: tuple[TodoCompletion, ...] = ()
    mode: PlanMode = PlanMode.EXECUTION
    artifact: PlanArtifactResponse | None = None
    review_feedback: tuple[str, ...] = ()
    rounds: tuple[GoalRound, ...] = ()
    autonomous_rounds: int = Field(ge=0)
    blocked_cause: str | None = None
    blocked_count: int = Field(ge=0)
    repeat_state: ToolRepeatState

    @classmethod
    def from_board(
        cls,
        board: PlanBoard,
        *,
        artifact_content: str | None = None,
    ) -> DurablePlanResponse:
        """Build a response while keeping artifact bytes outside the persisted board projection."""
        artifact = board.artifact
        if artifact is not None and artifact_content is None:
            raise ValueError("artifact content is required when a board references an artifact")
        artifact_view = (
            PlanArtifactResponse(**artifact.model_dump(), content=artifact_content)
            if artifact is not None and artifact_content is not None
            else None
        )
        return cls(
            run_id=board.run_id,
            plan_id=board.plan_id,
            revision=board.revision,
            goal=board.goal,
            goal_revision=board.goal_revision,
            goal_status=board.goal_status,
            items=board.items,
            completions=board.completions,
            mode=board.mode,
            artifact=artifact_view,
            review_feedback=board.review_feedback,
            rounds=board.rounds,
            autonomous_rounds=board.autonomous_rounds,
            blocked_cause=board.blocked_cause,
            blocked_count=board.blocked_count,
            repeat_state=board.repeat_state,
        )


class RunTraceResponse(BaseModel):
    """Where a Run's trace can be read, when tracing is configured at all.

    Deliberately not a field on `Run`: the trace is telemetry *about* a Run, not part of the
    record the Contract freezes, and it is recomputed on demand rather than stored. It has to come
    from the runtime because the URL names the Langfuse project, which only the API keys know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    trace_url: str | None = None


class RunAuditResponse(BaseModel):
    """The deterministic audit report paired with the latest run-level decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: AuditReport
    decision: PolicyDecision | None = None


class ExperimentListResponse(BaseModel):
    """Experiments persisted for one Run, in repository insertion order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    items: tuple[Experiment, ...] = ()


class ArtifactListResponse(BaseModel):
    """Every artifact a Run's manifest records, active and superseded, oldest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    items: tuple[Artifact, ...] = ()


class ArtifactContentResponse(BaseModel):
    """One digest-verified artifact and a redacted JSON projection of its bytes.

    The route recomputes the stored bytes' sha256 and length against ``artifact`` before building
    this response, so ``artifact.sha256`` names exactly the bytes that were read. ``content`` is
    those bytes as UTF-8 text or standard base64 after export redaction. ``redacted`` reports
    whether redaction changed the text, in which case ``content`` no longer hashes to
    ``artifact.sha256``. Binary content that redaction would alter is withheld: ``content`` is
    ``None`` and ``withheld_reason`` says why, rather than a corrupted body being served.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    artifact: Artifact
    encoding: Literal["utf-8", "base64"]
    content: str | None = None
    redacted: bool = False
    withheld_reason: str | None = None
    projection: Literal["redacted"] = "redacted"


class MlflowRunView(BaseModel):
    """One tracker run surfaced over the API exactly as ``query_mlflow`` reads it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tracker_run_id: str = Field(min_length=1)
    experiment_name: str
    status: str
    params: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)


class MlflowRunListResponse(BaseModel):
    """The tracker runs logged for one scoped Run, read through the ``query_mlflow`` tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    items: tuple[MlflowRunView, ...] = ()


class ApprovalListResponse(BaseModel):
    """The Run's unresolved human-review requests, folded by the ApprovalService.

    Each item is a :class:`~thymira.policies.PendingApproval` (decision id, rule, reason, summary
    and ``cost_so_far``): the view a client reads to know what still needs a human. The route folds
    the log through the ApprovalService rather than re-deriving the pending set itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    items: tuple[PendingApproval, ...] = ()


class ApprovalDecisionRequest(BaseModel):
    """Optional actor and reason submitted to a governance approve/reject route.

    ``actor`` is an assertion the server verifies against the authenticated principal, never an
    identity claim: a value naming anyone else is refused with 403 ``actor_mismatch``. The
    recorded ``Actor`` is always the authenticated principal, with ``authenticated=True`` and its
    real role. ``reason`` is recorded as the human's free-text rationale on the
    ``human.approval`` evidence event.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str | None = Field(default=None, min_length=1)
    reason: str | None = None


class ApprovalResolution(BaseModel):
    """The recorded human answer to one pending decision and the decision it resolved.

    ``approved`` is the human's answer, recorded as evidence by the ApprovalService. It is never an
    authorization: the resolved ``decision`` still stands or falls by the deterministic Policy
    Engine, and the Run's lifecycle transition is a separate, code-authorized step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Id
    decision_id: Id
    approved: bool
    decision: PolicyDecision


class ProjectContextResponse(BaseModel):
    """The project's context document as a redacted projection, with the digest of its source.

    ``sha256`` names the stored bytes and is what a save presents as ``expected_sha256``.
    ``redacted`` is true when export redaction changed the text: a client then shows it read-only,
    because saving the masked text would write the masks over the values behind them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: Id
    text: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exists: bool
    size_bytes: int = Field(ge=0)
    redacted: bool = False
    projection: Literal["redacted"] = "redacted"


class ProjectContextUpdate(BaseModel):
    """A compare-and-set replacement of the project's context document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(max_length=262_144)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectDatasetView(BaseModel):
    """One dataset the project declares, and what is on disk at its path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    target: str | None = None
    present: bool
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None


class ProjectDatasetListResponse(BaseModel):
    """The datasets the project declares, in declaration order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: Id
    items: tuple[ProjectDatasetView, ...] = ()


class ProjectDatasetUploadResponse(BaseModel):
    """A declared dataset after an upload, with the shape the next Run will register."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: Id
    dataset: ProjectDatasetView
    rows: int = Field(ge=0)
    columns: tuple[str, ...] = ()


class ProjectDatasetTargetUpdate(BaseModel):
    """The column a declared dataset predicts, or ``None`` for no target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str | None = Field(default=None, min_length=1, max_length=200)


class ApiError(BaseModel):
    """Stable error envelope returned by the API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AnswerRiskInterviewRequest",
    "ApiError",
    "ApprovalDecisionRequest",
    "ApprovalListResponse",
    "ApprovalResolution",
    "ArtifactContentResponse",
    "ArtifactListResponse",
    "CreateRunRequest",
    "CreateRunResponse",
    "DelegateRequest",
    "DurablePlanResponse",
    "EventPage",
    "ExperimentListResponse",
    "HumanDecisionRequest",
    "MlflowRunListResponse",
    "MlflowRunView",
    "PendingRiskQuestionResponse",
    "PlanArtifactResponse",
    "ProjectContextResponse",
    "ProjectContextUpdate",
    "ProjectDatasetListResponse",
    "ProjectDatasetTargetUpdate",
    "ProjectDatasetUploadResponse",
    "ProjectDatasetView",
    "ResumeRunRequest",
    "RiskInterviewResponse",
    "RunAuditResponse",
    "RunCancelRequest",
    "RunPage",
    "RunPlan",
    "SettingsListResponse",
    "SettingsPutRequest",
    "ToolDescriptor",
    "ToolListResponse",
]
