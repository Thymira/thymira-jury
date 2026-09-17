"""AuditFinding and Evidence: what MIRA produces. Never a decision — that is the Policy Engine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import Framework, Severity
from thymira.schemas.ids import Id


class Evidence(ThymiraModel):
    """A verifiable pointer: an event, an artifact or an experiment, optionally pinned by hash."""

    kind: Literal["event", "artifact", "experiment", "tool_call", "external"]
    ref: str = Field(min_length=1, description="id, event seq ('seq:42'), or citation")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    note: str | None = None


class AuditFinding(ThymiraModel):
    """A finding with its evidence, severity and confidence, emitted by an audit agent.

    ``control_id`` names the deterministic or methodological control behind the finding
    (e.g. ``"CHAIN-001"``, ``"LEAKAGE-002"``); ``framework`` names the regulation or
    methodology it relates to. ``confidence`` is the agent's self-assessment and is an input
    to the Policy Engine, never an authorization by itself.
    """

    id: Id
    run_id: Id
    agent_id: Id | None = Field(default=None, description="audit agent; None for engine checks")
    control_id: str = Field(min_length=1)
    framework: Framework
    title: str = Field(min_length=1)
    finding: str = Field(min_length=1, description="what was observed")
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[Evidence, ...] = ()
    recommendation: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
