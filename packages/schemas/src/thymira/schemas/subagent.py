"""How one delegated sub-agent settled: the uniform record every child ends with (F7.5).

A child never rejects its parent. Whatever it did — returned a validated output, exhausted its
room, declined for want of authority, failed on an error it could name, was stopped by the
orchestrator, or died without reporting — it settles as one :class:`SubagentResult`, and the
parent reads that record instead of catching an exception.

Two fields carry the canonical result, and only a ``COMPLETED`` settlement has them:
``result_json`` is the completion serialised exactly once, at settlement, and ``result_schema``
names the schema that validated it (``module.path:ClassName``, the shape ``AgentSpec.
output_schema_ref`` already uses), so a later reader knows *what* was validated rather than
having to re-parse and re-decide. ``result_json`` is deliberately **not** bounded: it is
schema-constrained already, and truncating the one canonical result would corrupt it. Only the
free-text ``diagnostics`` is bounded, and the bound it was cut at is recorded beside it
(``diagnostics_limit``, ``diagnostics_truncated``) rather than applied silently.

``delegation_key`` is the invocation's identity — a digest of the run, task and agent invocation
ids, the parent, the child spec, the objective and the depth. Two plan tasks may repeat the same
human-readable instruction while remaining distinct delegations. Each invocation has exactly one
durable settlement; a retry or fork is a new invocation and therefore mints new task and agent ids
and a new key. The selector remains defensive if duplicate records for one key appear in history;
selection across distinct attempts is a higher-level consumer concern.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import StopReason
from thymira.schemas.ids import Id

DELEGATION_KEY_PATTERN = r"^[0-9a-f]{64}$"
"""A delegation key is a lowercase sha256 hex digest and nothing else."""


class SubagentResult(ThymiraModel):
    """One delegated child's terminal settlement, recorded before its parent can report success."""

    run_id: Id
    task_id: Id
    agent_id: Id
    agent: str = Field(min_length=1, description="the child spec's name")
    parent_agent: str = Field(min_length=1, description="the delegating agent's own id")
    objective: str = Field(min_length=1)
    delegation_depth: int = Field(ge=0, description="the depth the child actually ran at")
    delegation_key: str = Field(
        pattern=DELEGATION_KEY_PATTERN,
        description="digest of the persisted invocation and its canonical parent linkage",
    )
    stop_reason: StopReason
    result_json: str | None = Field(
        default=None, description="the validated completion, serialised once; COMPLETED only"
    )
    result_schema: str | None = Field(
        default=None, description="'module.path:ClassName' of the schema that validated it"
    )
    diagnostics: str | None = Field(
        default=None, description="redacted then truncated at the producer; never reasoning"
    )
    diagnostics_limit: int = Field(ge=0, description="the bound the producer applied, as a fact")
    diagnostics_truncated: bool = False
    settled_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _result_belongs_to_a_completion(self) -> SubagentResult:
        """A result and its schema exist exactly when the child completed, never otherwise."""
        completed = self.stop_reason is StopReason.COMPLETED
        if completed and self.result_json is None:
            msg = "result_json is required when stop_reason is completed"
            raise ValueError(msg)
        if completed and self.result_schema is None:
            msg = "result_schema is required when stop_reason is completed"
            raise ValueError(msg)
        if not completed and self.result_json is not None:
            msg = f"result_json is not allowed when stop_reason is {self.stop_reason.value}"
            raise ValueError(msg)
        if not completed and self.result_schema is not None:
            msg = f"result_schema is not allowed when stop_reason is {self.stop_reason.value}"
            raise ValueError(msg)
        return self


__all__ = ["DELEGATION_KEY_PATTERN", "SubagentResult"]
