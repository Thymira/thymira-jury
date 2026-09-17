"""Adversarial finding verification and the discovery-loop limit (MIRA-03).

MIRA-01 established a fixed verification/aggregation boundary: every candidate finding flows into
one ``_deduplicate_findings`` call before Core's ``Gate.review_findings`` boundary.
MIRA-03 turns that boundary from a pass-through into an adversarial one without moving it, so no
upstream node and no Gate contract changes.

Two MIRA-owned values live here, deliberately free of any ``thymira.agents`` / ``thymira.tools``
import so ``thymira.mira.flow`` keeps its injection boundary:

- :class:`FindingVerdict` and the :data:`FindingVerifier` callable type -- the second-pass judge
  the flow receives by injection (its concrete, provider-backed implementation is
  ``thymira.mira.agents.verification.build_finding_verifier``, which may import P2); and
- :class:`DiscoveryLimits` plus :func:`finding_dedup_key`, which bound and drive the discovery
  loop that re-runs audit agents until a pass yields no new finding.

**Only agent-authored candidates are subject to verification.** A code-authored deterministic or
preflight finding (``agent_id is None``) is never dropped by an LLM judge -- "LLM proposes, code
authorizes" applies to suppression exactly as it applies to authorization. The judge may only drop
findings a *first* LLM proposed; the Policy Engine still decides over whatever survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field

from thymira.mira.finding_safety import safe_finding
from thymira.schemas import AuditFinding, Severity, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.mira.checks import AuditReport
    from thymira.schemas import Event


class FindingVerdict(ThymiraModel):
    """A second-pass judge's ruling on one candidate finding."""

    supported: bool
    rationale: str = Field(default="")


#: A second-pass judge: given a candidate finding and the immutable audit evidence, decide whether
#: the recorded evidence supports it. Injected into ``thymira.mira.flow`` so the flow never
#: imports the provider-backed implementation.
type FindingVerifier = Callable[[AuditFinding, "tuple[Event, ...]", "AuditReport"], FindingVerdict]


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    """Bound the discovery loop: how many times audit agents may re-run seeking new findings."""

    max_rounds: int = 4

    def __post_init__(self) -> None:
        """Reject a non-positive round budget."""
        if self.max_rounds < 1:
            raise ValueError("DiscoveryLimits.max_rounds must be at least 1")


@dataclass(frozen=True, slots=True)
class IndependentAuditEvidence:
    """The deep-copied Run evidence shared by MIRA producers and its critic.

    The flow captures this value before invoking any audit agent. Producers may append their own
    lifecycle or tool events to the live log for grounding, but neither those events nor another
    producer's finding can enter this input. Keeping a copied tuple here also prevents a mutable
    event payload supplied by a caller from changing after the critic boundary was established.
    """

    events: tuple[Event, ...]

    @classmethod
    def capture(cls, events: Sequence[Event]) -> IndependentAuditEvidence:
        """Capture an immutable evidence snapshot before MIRA model work begins."""
        return cls(events=tuple(event.model_copy(deep=True) for event in events))


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class VerificationDrop:
    """One agent finding rejected by the second-pass judge and the judge's rationale."""

    finding: AuditFinding
    rationale: str


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """The candidate findings a verification pass kept and the agent findings it dropped."""

    kept: tuple[AuditFinding, ...]
    dropped: tuple[AuditFinding, ...]
    drop_records: tuple[VerificationDrop, ...] = ()


def finding_dedup_key(finding: AuditFinding) -> tuple[str, str, tuple[str, ...]]:
    """Return the identity a finding deduplicates on -- identical to the orchestrator's key.

    The discovery loop calls a finding "new" when this key has not been seen before, and the Gate
    node's ``_deduplicate_findings`` collapses on the same key, so the two never disagree on what
    counts as the same underlying observation.
    """
    return (
        finding.control_id,
        finding.finding,
        tuple(f"{evidence.kind}:{evidence.ref}" for evidence in finding.evidence),
    )


def _prefer_finding(existing: AuditFinding, candidate: AuditFinding) -> AuditFinding:
    """Return the more severe finding, preserving the first record on a severity tie."""
    if _SEVERITY_RANK[candidate.severity] > _SEVERITY_RANK[existing.severity]:
        return candidate
    return existing


def verify_candidate_findings(
    candidates: Sequence[AuditFinding],
    verify: FindingVerifier | None,
    events: tuple[Event, ...],
    report: AuditReport,
) -> VerificationOutcome:
    """Drop unsupported agent-authored candidates; keep every code-authored finding unconditionally.

    With no verifier the outcome is a pass-through (the MVP identity). Otherwise each candidate
    whose ``agent_id`` is set is judged; a finding the judge does not support is dropped and never
    reaches the Policy Engine. Findings with no ``agent_id`` -- deterministic and preflight
    controls -- are always kept, because an LLM judge may not suppress a code-authored finding.
    """
    if verify is None:
        return VerificationOutcome(kept=tuple(candidates), dropped=())
    kept: list[AuditFinding] = []
    dropped: list[AuditFinding] = []
    drop_records: list[VerificationDrop] = []
    for candidate in candidates:
        finding = safe_finding(candidate)
        if finding.agent_id is None:
            kept.append(finding)
            continue
        verdict = verify(finding, events, report)
        if verdict.supported:
            kept.append(finding)
        else:
            dropped.append(finding)
            drop_records.append(VerificationDrop(finding=finding, rationale=verdict.rationale))
    return VerificationOutcome(
        kept=tuple(kept),
        dropped=tuple(dropped),
        drop_records=tuple(drop_records),
    )


__all__ = [
    "DiscoveryLimits",
    "FindingVerdict",
    "FindingVerifier",
    "IndependentAuditEvidence",
    "VerificationDrop",
    "VerificationOutcome",
    "finding_dedup_key",
    "verify_candidate_findings",
]
