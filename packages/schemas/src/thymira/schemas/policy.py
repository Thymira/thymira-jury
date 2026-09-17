"""PolicyDecision: the deterministic verdict of the Policy Engine."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from thymira.schemas.base import ThymiraModel
from thymira.schemas.enums import Decision
from thymira.schemas.execution import ExecutionConstraints
from thymira.schemas.ids import Id


class PolicyDecision(ThymiraModel):
    """What the engine decided, by which rule, under which policy snapshot.

    ``policy_sha256`` pins the exact policy content so an auditor can replay the decision; the
    thesis prototype's meta-auditor used the same trick (control A16).
    """

    id: Id
    run_id: Id
    subject_kind: Literal["run", "task", "tool_call", "findings"]
    subject_id: str = Field(min_length=1, description="id of the subject, or 'run' for the run")
    decision: Decision
    rule_id: str = Field(min_length=1, description="'default' when no rule matched")
    reason: str = Field(min_length=1)
    policy_name: str = Field(min_length=1, description="e.g. 'credit-risk@1.0'")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_ids: tuple[Id, ...] = ()
    execution_constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)

    @property
    def requires_human_approval(self) -> bool:
        """Whether this deterministic decision requires a separate Approval record."""
        return self.decision is Decision.REQUIRE_HUMAN_REVIEW
