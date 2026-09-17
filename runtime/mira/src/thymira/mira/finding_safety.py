"""Keep MIRA findings useful without copying identifiable text into reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.events import redact

if TYPE_CHECKING:
    from thymira.schemas import AuditFinding


def safe_finding(finding: AuditFinding) -> AuditFinding:
    """Return a finding with free text scrubbed and evidence references preserved."""
    title = redact(finding.title)
    detail = redact(finding.finding)
    recommendation = None if finding.recommendation is None else redact(finding.recommendation)
    evidence = tuple(
        item.model_copy(update={"note": None if item.note is None else redact(item.note)})
        for item in finding.evidence
    )
    if (
        title == finding.title
        and detail == finding.finding
        and recommendation == finding.recommendation
        and evidence == finding.evidence
    ):
        return finding
    return finding.model_copy(
        update={
            "title": title,
            "finding": detail,
            "recommendation": recommendation,
            "evidence": evidence,
        }
    )


def safe_findings(findings: tuple[AuditFinding, ...]) -> tuple[AuditFinding, ...]:
    """Scrub a stable tuple of findings at a MIRA report boundary."""
    return tuple(safe_finding(finding) for finding in findings)


__all__ = ["safe_finding", "safe_findings"]
