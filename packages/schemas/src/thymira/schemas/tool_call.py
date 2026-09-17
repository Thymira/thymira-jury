"""ToolCall: one invocation that passed through the Tool Manager and the Permission Policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import SandboxEnforcement, SandboxMode, ToolCallStatus
from thymira.schemas.ids import Id


class ToolCall(ThymiraModel):
    """Arguments are stored **after** redaction; results are referenced by digest, not inlined."""

    id: Id
    run_id: Id
    agent_id: Id
    task_id: Id | None = None
    tool_name: str = Field(
        min_length=1, description="e.g. 'run_python', 'read_file', 'mlflow.log_metric'"
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="validated model-visible arguments; presentation copies redact them",
    )
    status: ToolCallStatus = ToolCallStatus.REQUESTED
    policy_decision_id: Id | None = Field(
        default=None, description="decision that allowed or denied this call"
    )
    requested_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sandbox_mode: SandboxMode | None = Field(
        default=None, description="confinement requested for this call; None when it runs no code"
    )
    sandbox_enforcement: SandboxEnforcement | None = Field(
        default=None,
        description=(
            "confinement actually achieved, as reported by the backend — never inferred from the "
            "mode, and never upgraded to 'full' by a caller"
        ),
    )
    artifact_ids: tuple[Id, ...] = ()
    exit_code: int | None = None
    error: str | None = None
