"""Results of deterministic audit controls and the report that bundles them."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from thymira.schemas import AuditFinding, Evidence, Severity, ThymiraModel


class ControlStatus(StrEnum):
    """Outcome of one control."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_EVALUATED = "NOT_EVALUATED"


class AuditMode(StrEnum):
    """When the evidence snapshot is audited relative to Run closeout."""

    IN_FLIGHT = "IN_FLIGHT"
    FINAL = "FINAL"


class ControlResult(ThymiraModel):
    """One control's verdict with the evidence it looked at."""

    control_id: str
    title: str
    status: ControlStatus
    severity: Severity
    detail: str = ""
    evidence: tuple[Evidence, ...] = ()
    failure_code: str | None = None


AuditStatus = Literal["passed", "passed_with_warnings", "failed"]


class AuditReport(ThymiraModel):
    """What MIRA's deterministic layer concludes about a run.

    The report never contains a decision: findings go to the Policy Engine, which decides.
    """

    run_id: str
    control_set: str = "thymira-mira-checks@0.1"
    status: AuditStatus
    controls: tuple[ControlResult, ...]
    findings: tuple[AuditFinding, ...] = ()
    terminal_hash: str | None = None
    manifest_sha256: str | None = None
    graph_definition_hash: str | None = None
    policy_sha256: str | None = None
    activity_profile_id: str | None = None
    activity_profile_version: int | None = None
    summary: dict[str, int] = Field(default_factory=dict)

    def to_markdown(self) -> str:
        """Human-readable assurance summary (English)."""
        lines = [
            f"# Audit report — run `{self.run_id}`",
            "",
            f"- Control set: `{self.control_set}`",
            f"- Status: **{self.status}**",
            f"- Terminal event hash: `{self.terminal_hash or 'n/a'}`",
            "",
            "| Control | Status | Severity | Detail |",
            "|---|---|---|---|",
        ]
        lines.extend(
            f"| {c.control_id} {c.title} | {c.status} | {c.severity} | {c.detail or '-'} |"
            for c in self.controls
        )
        lines += [
            "",
            (
                "*This report states what the deterministic controls verified. It does not "
                "assert that a human, legal or business review took place unless such evidence "
                "is recorded.*"
            ),
        ]
        return "\n".join(lines) + "\n"
