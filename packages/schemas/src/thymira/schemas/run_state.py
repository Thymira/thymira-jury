"""Contract 0.3 composite operational Run state."""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, model_validator

from thymira.schemas.base import ThymiraModel


class RunStage(StrEnum):
    """Normal workflow position of a Run."""

    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    EXPERIMENTING = "experimenting"
    AUDITING = "auditing"
    REPORTING = "reporting"


class RunCondition(StrEnum):
    """Operational condition independent from a Run's workflow position."""

    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    TERMINAL = "terminal"


class WaitReason(StrEnum):
    """Reason why a Run is waiting."""

    INFORMATION = "information"
    APPROVAL = "approval"


class RunOutcome(StrEnum):
    """Terminal conclusion of a Run."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunState(ThymiraModel):
    """Validated, immutable state projection defined by Contract 0.3."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    stage: RunStage
    condition: RunCondition
    wait_reason: WaitReason | None = None
    outcome: RunOutcome | None = None
    version: int = 0

    @model_validator(mode="after")
    def validate_components(self) -> RunState:
        """Reject impossible state combinations instead of normalising them."""
        if self.version < 0:
            raise ValueError("version must be non-negative")
        if self.condition is RunCondition.WAITING and self.wait_reason is None:
            raise ValueError("waiting state requires wait_reason")
        if self.condition is not RunCondition.WAITING and self.wait_reason is not None:
            raise ValueError("wait_reason is only valid while waiting")
        if self.condition is RunCondition.TERMINAL and self.outcome is None:
            raise ValueError("terminal state requires outcome")
        if self.condition is not RunCondition.TERMINAL and self.outcome is not None:
            raise ValueError("outcome is only valid for terminal states")
        if self.stage is RunStage.CREATED and self.condition in (
            RunCondition.WAITING,
            RunCondition.PAUSED,
        ):
            raise ValueError("created stage cannot be waiting or paused")
        if self.outcome is RunOutcome.COMPLETED and self.stage is not RunStage.REPORTING:
            raise ValueError("completed outcome requires reporting stage")
        return self


CompositeRunState = RunState
"""Descriptive alias for the Contract 0.3 composite Run state."""
