"""The assurance bundle: one replayable record of report, decision, evidence and disclaimer.

An :class:`AssuranceBundle` is the stable output object a human reads to understand what MIRA
verified and what the Policy Engine deterministically decided. It assembles from an already
computed :class:`~thymira.mira.checks.AuditReport` and :class:`~thymira.schemas.PolicyDecision`;
it recomputes nothing and it authorizes nothing. The bundle pins the terminal event hash and the
policy content hash so an auditor can replay the decision, and it always carries the load-bearing
disclaimer: Thymira produces evidence and a deterministic decision, never a legal or regulatory
certification, and it never claims a human review happened without recorded evidence.

ASSUR-02 extends the same bundle in place with two things and no contract change for ASSUR-01
consumers:

- a per-:class:`~thymira.schemas.AuditFinding` **expert-review state** (``not_reviewed`` ->
  ``in_review`` -> ``reviewed`` | ``rejected``) that transitions **only** on recorded
  ``human.approval`` events which name the exact finding, so the bundle can never state that a
  finding was reviewed without the recorded evidence to back it; and
- a full **traceability index** mapping each finding to its evidence and each evidence to the
  concrete Run / Artifact / Event / KB source it cites.

A reviewed finding means only that *its evidence was reviewed*: it neither claims remediation nor
changes a Run or a :class:`~thymira.schemas.PolicyDecision`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from thymira.events import canonical_json, sha256_text, verify_events
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import (
    ActivityProfile,
    Artifact,
    Event,
    EventType,
    Evidence,
    PackBinding,
    PolicyDecision,
    RiskAssessment,
    Run,
    ThymiraModel,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from thymira.schemas import AuditFinding
    from thymira.state import ArtifactStore

# The existing "no human-review claim without evidence" disclaimer, verbatim from
# AuditReport.to_markdown. It is the required, load-bearing clause every bundle must carry.
_REQUIRED_DISCLAIMER_CLAUSE = (
    "It does not assert that a human, legal or business review took place unless such evidence "
    "is recorded."
)

#: The full assurance disclaimer. It frames the product (evidence plus a deterministic decision,
#: never a certification) and embeds the required no-human-review clause verbatim.
ASSURANCE_DISCLAIMER = (
    "This assurance bundle records what Thymira's deterministic controls verified and the "
    "deterministic policy decision they informed. It is evidence and a reproducible decision, "
    "never a legal, regulatory or professional certification. " + _REQUIRED_DISCLAIMER_CLAUSE
)

#: Rendered beside the finding-review table. A review is of the evidence, nothing more.
REVIEW_DISCLAIMER = (
    "A finding marked reviewed means only that its recorded evidence was reviewed. It does not "
    "assert that the finding was remediated, and it changes neither the Run nor the policy "
    "decision."
)

#: Payload key on a ``human.approval`` event naming the exact finding it reviews.
REVIEW_FINDING_ID_KEY = "finding_id"

#: Payload key on a ``human.approval`` event naming the target review state for that finding.
REVIEW_STATE_KEY = "review_state"

#: File name of the assurance bundle export written under a Run's ``exports`` directory. The
#: bundle is produced once, when the Run completes; reading it never regenerates it.
ASSURANCE_BUNDLE_EXPORT = "assurance-bundle.json"


class FindingReviewState(StrEnum):
    """The expert-review state of one finding, derived only from recorded approval evidence."""

    NOT_REVIEWED = "not_reviewed"
    IN_REVIEW = "in_review"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


# The only legal transitions. ``not_reviewed`` may open a review or resolve directly; ``in_review``
# resolves; ``reviewed`` and ``rejected`` are terminal. A transition outside this table raises.
_ALLOWED_TRANSITIONS: Mapping[FindingReviewState, frozenset[FindingReviewState]] = {
    FindingReviewState.NOT_REVIEWED: frozenset(
        {FindingReviewState.IN_REVIEW, FindingReviewState.REVIEWED, FindingReviewState.REJECTED}
    ),
    FindingReviewState.IN_REVIEW: frozenset(
        {FindingReviewState.REVIEWED, FindingReviewState.REJECTED}
    ),
    FindingReviewState.REVIEWED: frozenset(),
    FindingReviewState.REJECTED: frozenset(),
}

EvidenceSourceKind = Literal["event", "artifact", "experiment", "tool_call", "kb"]

# Evidence.kind -> the concrete source it resolves to. ``external`` citations are KB sources.
_EVIDENCE_SOURCE_KIND: Mapping[str, EvidenceSourceKind] = {
    "event": "event",
    "artifact": "artifact",
    "experiment": "experiment",
    "tool_call": "tool_call",
    "external": "kb",
}


class FindingReview(ThymiraModel):
    """One finding's expert-review state and the identity that last transitioned it."""

    finding_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    state: FindingReviewState = FindingReviewState.NOT_REVIEWED
    reviewed_by: str | None = None
    rationale: str | None = None


class EvidenceSource(ThymiraModel):
    """One evidence reference resolved to the concrete source it cites."""

    source_kind: EvidenceSourceKind
    ref: str = Field(min_length=1)
    sha256: str | None = None
    note: str | None = None


class FindingTrace(ThymiraModel):
    """One finding mapped to the concrete sources every piece of its evidence cites."""

    finding_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    sources: tuple[EvidenceSource, ...] = ()


class ArtifactLineage(ThymiraModel):
    """The immutable lineage edge for one artifact revision."""

    artifact_id: str = Field(min_length=1)
    input_artifact_ids: tuple[str, ...] = ()
    execution_key: str | None = None
    valid: bool = True
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class AssuranceVerification:
    """Result of replaying an assurance bundle against its embedded or external evidence."""

    valid: bool
    errors: tuple[str, ...] = ()

    @property
    def error(self) -> str | None:
        """Return a compact first failure for API and CLI callers."""
        return self.errors[0] if self.errors else None


class AssuranceBundle(ThymiraModel):
    """A replayable assurance record: audit report, policy decision, evidence index, disclaimer.

    The bundle assembles from evidence MIRA already produced; it never recomputes a control, and
    a finding or a decision inside it authorizes nothing. ``terminal_hash`` and
    :attr:`policy_sha256` together let an auditor replay the decision. The disclaimer cannot be
    omitted: a bundle whose disclaimer drops the required no-human-review clause cannot be built.

    ``finding_reviews`` carries the per-finding expert-review state (ASSUR-02), one entry per
    report finding, derived only from recorded ``human.approval`` events; it defaults empty, in
    which case every finding is treated as ``not_reviewed``.
    """

    run_id: str = Field(min_length=1)
    report: AuditReport
    decision: PolicyDecision
    evidence_index: tuple[Evidence, ...] = ()
    terminal_hash: str | None = None
    disclaimer: str = ASSURANCE_DISCLAIMER
    finding_reviews: tuple[FindingReview, ...] = ()
    run: Run | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    activity_profile: ActivityProfile | None = None
    risk_assessment: RiskAssessment | None = None
    pack_bindings: tuple[PackBinding, ...] = ()
    verified_events: tuple[Event, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    decisions: tuple[PolicyDecision, ...] = ()
    approvals: tuple[Event, ...] = ()
    lineage: tuple[ArtifactLineage, ...] = ()
    flow_version: str | None = None
    policy_version: str | None = None
    manifest_sha256: str | None = None
    event_head_hash: str | None = None
    bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_consistency(self) -> AssuranceBundle:
        """Bind the bundle to one run and pin the exact hashes needed to replay the decision."""
        if self.report.run_id != self.run_id:
            raise ValueError("report.run_id must match the bundle run_id")
        if self.decision.run_id != self.run_id:
            raise ValueError("decision.run_id must match the bundle run_id")
        if self.terminal_hash != self.report.terminal_hash:
            raise ValueError("terminal_hash must equal the audit report terminal_hash")
        if _REQUIRED_DISCLAIMER_CLAUSE not in self.disclaimer:
            raise ValueError("assurance disclaimer must carry the no-human-review clause")
        _validate_finding_reviews(self)
        _validate_evidence_inventory(self)
        if self.bundle_sha256 is not None and self.bundle_sha256 != self.content_sha256:
            raise ValueError("assurance bundle digest does not match its content")
        return self

    @property
    def content_sha256(self) -> str:
        """Hash the bundle contents without the self-referential digest field."""
        return sha256_text(canonical_json(self.model_dump(mode="json", exclude={"bundle_sha256"})))

    @property
    def policy_sha256(self) -> str:
        """The policy content hash from the decision, pinned so the decision can be replayed."""
        return self.decision.policy_sha256

    @property
    def review_state_by_finding(self) -> dict[str, FindingReviewState]:
        """Every finding's review state, defaulting a finding with no review to ``not_reviewed``."""
        explicit = {review.finding_id: review.state for review in self.finding_reviews}
        return {
            finding.id: explicit.get(finding.id, FindingReviewState.NOT_REVIEWED)
            for finding in self.report.findings
        }

    @property
    def review_summary(self) -> dict[str, int]:
        """Bundle summary derived from the finding review states: one count per state."""
        counts = {state.value: 0 for state in FindingReviewState}
        for state in self.review_state_by_finding.values():
            counts[state.value] += 1
        return counts

    @property
    def traceability(self) -> tuple[FindingTrace, ...]:
        """Map each finding to its evidence and each evidence to the source it cites."""
        return tuple(_finding_trace(finding, self.run_id) for finding in self.report.findings)

    @property
    def untraceable_finding_ids(self) -> tuple[str, ...]:
        """Findings whose evidence resolves to no traceable source (none, in a well-formed run)."""
        return tuple(trace.finding_id for trace in self.traceability if not trace.sources)

    def to_markdown(self) -> str:
        """Render the report, decision, evidence index, review states, traceability, disclaimer."""
        lines = [
            self.report.to_markdown().rstrip("\n"),
            "",
            "## Policy decision",
            "",
            f"- Decision: **{self.decision.decision.value}**",
            f"- Rule: `{self.decision.rule_id}`",
            f"- Policy: `{self.decision.policy_name}`",
            f"- Policy sha256: `{self.policy_sha256}`",
            f"- Terminal event hash: `{self.terminal_hash or 'n/a'}`",
            f"- Reason: {self.decision.reason}",
            "",
            "## Evidence index",
            "",
        ]
        if self.evidence_index:
            lines += ["| # | Kind | Reference | sha256 |", "|---|---|---|---|"]
            lines += [
                f"| {index} | {item.kind} | `{item.ref}` | `{item.sha256 or 'n/a'}` |"
                for index, item in enumerate(self.evidence_index, start=1)
            ]
        else:
            lines.append("_No evidence is referenced by the findings._")
        lines += self._review_markdown()
        lines += self._traceability_markdown()
        lines += ["", f"> {self.disclaimer}"]
        return "\n".join(lines) + "\n"

    def _review_markdown(self) -> list[str]:
        """Render the per-finding review states and the review-derived summary."""
        lines = ["", "## Finding review", ""]
        summary = self.review_summary
        lines.append(
            "- Summary: " + ", ".join(f"{count} {state}" for state, count in summary.items())
        )
        states = self.review_state_by_finding
        if self.report.findings:
            lines += ["", "| Finding | Control | State |", "|---|---|---|"]
            lines += [
                f"| {finding.title} | `{finding.control_id}` | {states[finding.id].value} |"
                for finding in self.report.findings
            ]
        else:
            lines += ["", "_No findings to review._"]
        lines += ["", f"> {REVIEW_DISCLAIMER}"]
        return lines

    def _traceability_markdown(self) -> list[str]:
        """Render finding -> evidence -> concrete source for every finding."""
        lines = ["", "## Traceability", ""]
        if not self.report.findings:
            lines.append("_No findings to trace._")
            return lines
        lines += ["| Finding | Control | Source | Reference | sha256 |", "|---|---|---|---|---|"]
        for trace in self.traceability:
            if not trace.sources:
                lines.append(
                    f"| `{trace.finding_id}` | `{trace.control_id}` | _none_ | _none_ | _none_ |"
                )
                continue
            lines += [
                f"| `{trace.finding_id}` | `{trace.control_id}` | {source.source_kind} | "
                f"`{source.ref}` | `{source.sha256 or 'n/a'}` |"
                for source in trace.sources
            ]
        return lines


def _validate_finding_reviews(bundle: AssuranceBundle) -> None:
    """Validate finding review identities without allowing duplicates or foreign findings."""
    if not bundle.finding_reviews:
        return
    finding_ids = {finding.id for finding in bundle.report.findings}
    seen: set[str] = set()
    for review in bundle.finding_reviews:
        if review.finding_id not in finding_ids:
            raise ValueError(f"review references unknown finding {review.finding_id!r}")
        if review.finding_id in seen:
            raise ValueError(f"duplicate review for finding {review.finding_id!r}")
        seen.add(review.finding_id)


def _validate_evidence_inventory(  # noqa: PLR0912  # one fail-closed check per bundle invariant
    bundle: AssuranceBundle,
) -> None:
    """Validate the optional full Run/evidence inventory carried by a final bundle."""
    if bundle.run is not None and bundle.run.id != bundle.run_id:
        raise ValueError("run.id must match the bundle run_id")
    if bundle.activity_profile is not None and bundle.activity_profile.run_id not in {
        None,
        bundle.run_id,
    }:
        raise ValueError("activity_profile must belong to the bundle Run")
    _validate_events(bundle)
    if any(artifact.run_id != bundle.run_id for artifact in bundle.artifacts):
        raise ValueError("artifacts must belong to the bundle Run")
    if bundle.risk_assessment is not None and bundle.risk_assessment.run_id not in {
        None,
        bundle.run_id,
    }:
        raise ValueError("risk_assessment must belong to the bundle Run")
    if (
        bundle.flow_version is not None
        and bundle.report.graph_definition_hash is not None
        and bundle.flow_version != bundle.report.graph_definition_hash
    ):
        raise ValueError("flow_version must match the audit report graph definition hash")
    if bundle.policy_version is not None and bundle.policy_version != bundle.decision.policy_name:
        raise ValueError("policy_version must match the bundle decision policy")
    if (
        bundle.manifest_sha256 is not None
        and bundle.report.manifest_sha256 is not None
        and bundle.manifest_sha256 != bundle.report.manifest_sha256
    ):
        raise ValueError("manifest_sha256 must match the audit report manifest hash")
    if any(decision.run_id != bundle.run_id for decision in bundle.decisions):
        raise ValueError("decisions must belong to the bundle Run")
    if any(approval.run_id != bundle.run_id for approval in bundle.approvals):
        raise ValueError("approvals must belong to the bundle Run")
    if bundle.decisions and bundle.decision.id not in {item.id for item in bundle.decisions}:
        raise ValueError("decisions must include the bundle decision")
    if bundle.decisions and not any(
        item.to_json_dict() == bundle.decision.to_json_dict() for item in bundle.decisions
    ):
        raise ValueError("decisions must include the exact bundle decision")
    if (
        bundle.verified_events
        and bundle.decisions
        and any(
            not _contains_exact_decision(bundle.verified_events, decision)
            for decision in bundle.decisions
        )
    ):
        raise ValueError("decisions must be present exactly in verified_events")
    if (
        bundle.verified_events
        and bundle.approvals
        and any(
            not _contains_exact_event(bundle.verified_events, approval)
            for approval in bundle.approvals
        )
    ):
        raise ValueError("approvals must be present exactly in verified_events")
    if bundle.verified_events:
        _validate_embedded_indexes(bundle)
    if bundle.lineage and {item.artifact_id for item in bundle.lineage} != {
        item.id for item in bundle.artifacts
    }:
        raise ValueError("lineage must contain one entry for every bundled artifact")
    if bundle.lineage and _model_identity(bundle.lineage) != _model_identity(
        _lineage_from_artifacts(bundle.artifacts)
    ):
        raise ValueError("lineage does not match the bundled artifact manifest")


def _validate_embedded_indexes(bundle: AssuranceBundle) -> None:
    """Ensure derived governance indexes remain exact views of the embedded event chain."""
    events = bundle.verified_events
    if bundle.configuration != _configuration_from_events(events):
        raise ValueError("configuration does not match the embedded Run-start event")
    if bundle.activity_profile != _profile_from_events(events):
        raise ValueError("activity_profile does not match the embedded audit evidence")
    if bundle.risk_assessment != _risk_assessment_from_events(events):
        raise ValueError("risk_assessment does not match the embedded audit evidence")
    if _model_identity(bundle.pack_bindings) != _model_identity(_packs_from_events(events)):
        raise ValueError("pack_bindings do not match the embedded audit evidence")
    if _model_identity(bundle.decisions) != _model_identity(_decisions_from_events(events)):
        raise ValueError("decisions do not match the embedded policy events")
    embedded_approvals = tuple(event for event in events if event.type is EventType.HUMAN_APPROVAL)
    if _event_identity(bundle.approvals) != _event_identity(embedded_approvals):
        raise ValueError("approvals do not match the embedded human-approval events")


def _validate_events(bundle: AssuranceBundle) -> None:
    """Validate the embedded event chain and its audit snapshot anchor."""
    if not bundle.verified_events:
        return
    if any(event.run_id != bundle.run_id for event in bundle.verified_events):
        raise ValueError("verified events must belong to the bundle Run")
    if not verify_events(bundle.verified_events).valid:
        raise ValueError("verified_events must contain a valid event chain")
    hashes = {event.hash for event in bundle.verified_events}
    if bundle.terminal_hash not in hashes:
        raise ValueError("terminal_hash must be present in verified_events")
    if bundle.event_head_hash != bundle.verified_events[-1].hash:
        raise ValueError("event_head_hash must equal the embedded event-chain head")


def latest_audit_report(events: Sequence[Event]) -> AuditReport | None:
    """Return the newest audit report MIRA persisted as an ``audit.completed`` payload.

    Events that carry no ``audit_report`` key are lifecycle markers appended by the composition
    for lightweight subgraphs, not reports; they are skipped rather than treated as evidence.

    Args:
        events: The Run's verified event history, in sequence order.

    Returns:
        The newest persisted :class:`~thymira.mira.checks.AuditReport`, or ``None`` when the Run
        recorded no report.

    Raises:
        ValueError: An ``audit.completed`` event carries a malformed report payload.
    """
    for event in reversed(events):
        if event.type is not EventType.AUDIT_COMPLETED:
            continue
        payload = event.payload.get("audit_report")
        if not isinstance(payload, dict):
            continue
        try:
            return AuditReport.model_validate_json(canonical_json(payload))
        except (TypeError, ValueError) as exc:
            raise ValueError("the persisted audit report is malformed") from exc
    return None


def latest_findings_decision(events: Sequence[Event], run_id: str) -> PolicyDecision | None:
    """Return the newest ``findings`` policy decision recorded for ``run_id``.

    Args:
        events: The Run's verified event history, in sequence order.
        run_id: The Run whose findings decision is wanted.

    Returns:
        The newest matching :class:`~thymira.schemas.PolicyDecision`, or ``None`` when the Gate
        recorded none.

    Raises:
        ValueError: A ``policy.decision`` event carries a malformed payload.
    """
    decision: PolicyDecision | None = None
    for event in events:
        if event.type is not EventType.POLICY_DECISION:
            continue
        try:
            candidate = PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("a persisted policy decision is malformed") from exc
        if candidate.run_id == run_id and candidate.subject_kind == "findings":
            decision = candidate
    return decision


def assemble_run_assurance(
    run_id: str,
    *,
    events: Sequence[Event],
    run: Run | None = None,
    store: ArtifactStore | None = None,
) -> AssuranceBundle | None:
    """Assemble one Run's assurance bundle from the evidence that Run itself recorded.

    This is the single assembly path: the composition calls it once, when the Run reaches a
    terminal state, and the read surface only reads and re-verifies what it wrote. It recomputes
    nothing and authorizes nothing.

    Args:
        run_id: The Run the bundle describes.
        events: The Run's verified event history, in sequence order.
        run: The complete Run record to embed, when the caller holds it.
        store: The Run's artifact store, source of the embedded manifest and its digest.

    Returns:
        The assembled :class:`AssuranceBundle`, or ``None`` when the Run recorded no audit report
        or no findings decision and therefore has nothing to assure yet.

    Raises:
        ValueError: The recorded report or a recorded decision is malformed, or the report
            carries no terminal event hash.
    """
    report = latest_audit_report(events)
    decision = latest_findings_decision(events, run_id)
    if report is None or decision is None:
        return None
    artifacts = (
        tuple(artifact for _, artifact in sorted(store.manifest().items()))
        if store is not None
        else ()
    )
    return build_assurance(
        run_id,
        report,
        decision,
        review_events=events,
        run=run,
        events=tuple(events),
        artifacts=artifacts,
        manifest_sha256=artifact_manifest_sha256(store),
    )


def build_assurance(
    run_id: str,
    report: AuditReport,
    decision: PolicyDecision,
    *,
    review_events: Sequence[Event] = (),
    run: Run | None = None,
    configuration: dict[str, Any] | None = None,
    activity_profile: ActivityProfile | None = None,
    risk_assessment: RiskAssessment | None = None,
    pack_bindings: Sequence[PackBinding] = (),
    events: Sequence[Event] = (),
    artifacts: Sequence[Artifact] = (),
    decisions: Sequence[PolicyDecision] = (),
    approvals: Sequence[Event] = (),
    lineage: Sequence[ArtifactLineage] = (),
    flow_version: str | None = None,
    policy_version: str | None = None,
    manifest_sha256: str | None = None,
) -> AssuranceBundle:
    """Assemble the assurance bundle for one run from its report and its policy decision.

    The evidence index is every distinct :class:`~thymira.schemas.Evidence` cited by the report's
    findings, deduplicated in first-seen order. The bundle pins ``report.terminal_hash`` and the
    decision's ``policy_sha256`` so the decision can be replayed. Every finding's expert-review
    state is folded from ``review_events`` -- only recorded ``human.approval`` events that name a
    finding transition it, and every finding starts ``not_reviewed``.

    Args:
        run_id: The run the bundle describes; must match the report and the decision.
        report: The deterministic audit report, source of the findings and the terminal hash.
        decision: The Policy Engine's decision over those findings, source of ``policy_sha256``.
        review_events: Recorded events whose ``human.approval`` entries drive finding reviews.
        run: Optional complete Run record for the bundle.
        configuration: Redacted run-start configuration and provenance.
        activity_profile: Profile used by the selected audit.
        risk_assessment: Inherent-risk assessment used by preflight.
        pack_bindings: Reviewed packs bound to the profile.
        events: Complete verified event history to embed.
        artifacts: Complete artifact manifest, including superseded revisions.
        decisions: All policy decisions to index.
        approvals: Human approval events to index.
        lineage: Explicit artifact lineage entries.
        flow_version: Audit-flow version/hash.
        policy_version: Policy label/version.
        manifest_sha256: Artifact-manifest digest at the audit snapshot.

    Returns:
        A validated :class:`AssuranceBundle` carrying the disclaimer, the evidence index, the
        per-finding review states and the replay hashes.

    Raises:
        ValueError: If the report has no terminal hash, the run ids disagree, or a review event
            requests an illegal finding-review transition.
    """
    if report.terminal_hash is None:
        raise ValueError("cannot build an assurance bundle without a terminal event hash")
    event_tuple = tuple(events)
    decision_tuple = tuple(decisions) or _decisions_from_events(event_tuple)
    approval_tuple = tuple(approvals) or tuple(
        event for event in event_tuple if event.type is EventType.HUMAN_APPROVAL
    )
    bundle = AssuranceBundle(
        run_id=run_id,
        report=report,
        decision=decision,
        evidence_index=_deduplicate_evidence(report.findings),
        terminal_hash=report.terminal_hash,
        finding_reviews=derive_finding_reviews(report.findings, review_events),
        run=run,
        configuration=(configuration or _configuration_from_events(event_tuple)),
        activity_profile=activity_profile or _profile_from_events(event_tuple),
        risk_assessment=risk_assessment or _risk_assessment_from_events(event_tuple),
        pack_bindings=tuple(pack_bindings) or _packs_from_events(event_tuple),
        verified_events=event_tuple,
        artifacts=tuple(artifacts),
        decisions=decision_tuple,
        approvals=approval_tuple,
        lineage=tuple(lineage) or _lineage_from_artifacts(artifacts),
        flow_version=flow_version or report.graph_definition_hash,
        policy_version=policy_version or decision.policy_name,
        manifest_sha256=manifest_sha256 or report.manifest_sha256,
        event_head_hash=event_tuple[-1].hash if event_tuple else None,
    )
    return AssuranceBundle.model_validate(
        bundle.model_dump(mode="python") | {"bundle_sha256": bundle.content_sha256}
    )


def verify_assurance_bundle(
    bundle: AssuranceBundle,
    *,
    events: Sequence[Event] = (),
    artifacts: Sequence[Artifact] = (),
    store: ArtifactStore | None = None,
) -> AssuranceVerification:
    """Replay a bundle and fail closed when its content or referenced evidence was altered."""
    errors: list[str] = []
    errors.extend(_bundle_validation_errors(bundle))
    errors.extend(_event_verification_errors(bundle, events))
    errors.extend(_artifact_verification_errors(bundle, artifacts, store))
    if bundle.decisions and bundle.decision.id not in {item.id for item in bundle.decisions}:
        errors.append("bundle decision is absent from its decisions index")
    return AssuranceVerification(valid=not errors, errors=tuple(errors))


def _bundle_validation_errors(bundle: AssuranceBundle) -> list[str]:
    """Validate the bundle's own model and digest without raising to the caller."""
    try:
        AssuranceBundle.model_validate_json(canonical_json(bundle.to_json_dict()))
    except (TypeError, ValueError) as exc:
        return [f"bundle validation failed: {exc}"]
    return []


def _event_verification_errors(bundle: AssuranceBundle, events: Sequence[Event]) -> list[str]:
    """Verify an embedded chain or a redacted export against the supplied canonical chain."""
    errors: list[str] = []
    embedded = tuple(bundle.verified_events)
    external = tuple(events)
    if embedded and not verify_events(embedded).valid:
        errors.append("embedded event chain is invalid")
    if external:
        if not verify_events(external).valid:
            errors.append("external event chain is invalid")
        if embedded and _event_identity(external) != _event_identity(embedded):
            errors.append("external events differ from the bundle event chain")
    if not embedded and not external:
        errors.append("bundle has no canonical event chain to verify against")
    chain = embedded or external
    if chain and bundle.event_head_hash != chain[-1].hash:
        errors.append("bundle event_head_hash differs from the canonical event chain")
    if chain and not any(event.hash == bundle.terminal_hash for event in chain):
        errors.append("audit terminal hash is absent from the canonical event chain")
    if chain and not _contains_exact_audit(chain, bundle.report):
        errors.append("bundle report is not the exact persisted audit report")
    if chain and not _contains_exact_decision(chain, bundle.decision):
        errors.append("bundle decision is not the exact persisted policy decision")
    return errors


def _artifact_verification_errors(
    bundle: AssuranceBundle,
    artifacts: Sequence[Artifact],
    store: ArtifactStore | None,
) -> list[str]:
    """Verify the embedded and optional external artifact manifests."""
    errors: list[str] = []
    expected_artifacts = tuple(artifacts)
    if store is not None:
        errors.extend(store.verify())
        expected_artifacts = tuple(artifact for _, artifact in sorted(store.manifest().items()))
    if (store is not None or artifacts) and _artifact_identity(
        expected_artifacts
    ) != _artifact_identity(bundle.artifacts):
        errors.append("bundle artifact manifest differs from the verified current manifest")
    if bundle.manifest_sha256 is not None and bundle.manifest_sha256 != _manifest_sha256(
        bundle.artifacts
    ):
        errors.append("bundle manifest sha256 does not match its embedded artifacts")
    if (
        bundle.manifest_sha256 is not None
        and (store is not None or artifacts)
        and bundle.manifest_sha256 != _manifest_sha256(expected_artifacts)
    ):
        errors.append("bundle manifest sha256 does not match the supplied artifacts")
    return errors


def derive_finding_reviews(
    findings: Sequence[AuditFinding],
    review_events: Sequence[Event],
) -> tuple[FindingReview, ...]:
    """Fold recorded approval events into one review state per finding.

    Only ``human.approval`` events carrying both a ``finding_id`` naming a finding in ``findings``
    and a ``review_state`` transition it; an event for any other finding is ignored. Each finding
    starts ``not_reviewed``, and transitions are applied in event order.

    Raises:
        ValueError: An event names an unknown review state or requests an illegal transition.
    """
    states = {finding.id: FindingReviewState.NOT_REVIEWED for finding in findings}
    control_ids = {finding.id: finding.control_id for finding in findings}
    meta: dict[str, tuple[str | None, str | None]] = {}
    for event in review_events:
        if event.type is not EventType.HUMAN_APPROVAL:
            continue
        finding_id = event.payload.get(REVIEW_FINDING_ID_KEY)
        raw_state = event.payload.get(REVIEW_STATE_KEY)
        if not isinstance(finding_id, str) or raw_state is None or finding_id not in states:
            continue
        try:
            target = FindingReviewState(raw_state)
        except ValueError as exc:
            raise ValueError(
                f"human.approval for finding {finding_id!r} names an unknown review state "
                f"{raw_state!r}"
            ) from exc
        current = states[finding_id]
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise ValueError(
                f"illegal review transition for finding {finding_id!r}: "
                f"{current.value} -> {target.value}"
            )
        states[finding_id] = target
        meta[finding_id] = (event.actor.id, _rationale(event.payload))
    return tuple(
        FindingReview(
            finding_id=finding_id,
            control_id=control_ids[finding_id],
            state=state,
            reviewed_by=meta.get(finding_id, (None, None))[0],
            rationale=meta.get(finding_id, (None, None))[1],
        )
        for finding_id, state in states.items()
    )


def _rationale(payload: Mapping[str, object]) -> str | None:
    """Return the review rationale from an approval payload, if it recorded one."""
    value = payload.get("rationale")
    return value if isinstance(value, str) and value else None


def _finding_trace(finding: AuditFinding, run_id: str) -> FindingTrace:
    """Resolve one finding's evidence to the concrete sources it cites."""
    sources = tuple(_evidence_source(evidence) for evidence in finding.evidence)
    return FindingTrace(
        finding_id=finding.id,
        control_id=finding.control_id,
        run_id=run_id,
        sources=sources,
    )


def _evidence_source(evidence: Evidence) -> EvidenceSource:
    """Resolve one evidence reference to its concrete Run/Artifact/Event/KB source descriptor."""
    return EvidenceSource(
        source_kind=_EVIDENCE_SOURCE_KIND[evidence.kind],
        ref=evidence.ref,
        sha256=evidence.sha256,
        note=evidence.note,
    )


def _deduplicate_evidence(findings: Sequence[AuditFinding]) -> tuple[Evidence, ...]:
    """Return every distinct evidence reference cited by the findings, in first-seen order."""
    unique: dict[tuple[str, str, str | None, str | None], Evidence] = {}
    for finding in findings:
        for evidence in finding.evidence:
            key = (evidence.kind, evidence.ref, evidence.sha256, evidence.note)
            unique.setdefault(key, evidence)
    return tuple(unique.values())


def _decisions_from_events(events: Sequence[Event]) -> tuple[PolicyDecision, ...]:
    """Parse every recorded policy decision, preserving event order and removing duplicates."""
    decisions: list[PolicyDecision] = []
    seen: set[str] = set()
    for event in events:
        if event.type is not EventType.POLICY_DECISION:
            continue
        try:
            decision = PolicyDecision.model_validate(event.payload)
        except (TypeError, ValueError):
            continue
        if decision.id not in seen:
            decisions.append(decision)
            seen.add(decision.id)
    return tuple(decisions)


def _configuration_from_events(events: Sequence[Event]) -> dict[str, Any]:
    """Copy the redacted run-start configuration/provenance without treating it as authority."""
    for event in events:
        if event.type is EventType.RUN_STARTED:
            return {key: value for key, value in event.payload.items() if key != "run"}
    return {}


def _profile_from_events(events: Sequence[Event]) -> ActivityProfile | None:
    """Recover a recorded activity profile when a caller did not pass it explicitly."""
    for event in reversed(events):
        value = event.payload.get("activity_profile")
        if isinstance(value, dict):
            try:
                return ActivityProfile.model_validate_json(canonical_json(value))
            except (TypeError, ValueError):
                return None
    return None


def _risk_assessment_from_events(events: Sequence[Event]) -> RiskAssessment | None:
    """Recover the latest persisted inherent-risk assessment."""
    for event in reversed(events):
        value = event.payload.get("risk_assessment")
        if isinstance(value, dict):
            try:
                return RiskAssessment.model_validate_json(canonical_json(value))
            except (TypeError, ValueError):
                return None
    return None


def _packs_from_events(events: Sequence[Event]) -> tuple[PackBinding, ...]:
    """Recover the pack bindings recorded by MIRA preflight."""
    bindings: list[PackBinding] = []
    for event in events:
        value = event.payload.get("pack_binding")
        if not isinstance(value, dict):
            continue
        try:
            bindings.append(PackBinding.model_validate_json(canonical_json(value)))
        except (TypeError, ValueError):
            continue
    return tuple(bindings)


def _lineage_from_artifacts(artifacts: Sequence[Artifact]) -> tuple[ArtifactLineage, ...]:
    """Project the artifact manifest's revision links into a compact bundle index."""
    replacements = {
        input_id: artifact.id for artifact in artifacts for input_id in artifact.input_artifact_ids
    }
    return tuple(
        ArtifactLineage(
            artifact_id=artifact.id,
            input_artifact_ids=artifact.input_artifact_ids,
            execution_key=artifact.execution_key,
            valid=artifact.valid,
            superseded_by=replacements.get(artifact.id),
        )
        for artifact in artifacts
    )


def _event_identity(events: Sequence[Event]) -> tuple[tuple[int, str | None, str], ...]:
    """Return the complete event identity used to compare two snapshots."""
    return tuple((event.seq, event.hash, canonical_json(event.to_json_dict())) for event in events)


def _contains_exact_audit(events: Sequence[Event], report: AuditReport) -> bool:
    """Check that the report is present byte-for-byte as an ``audit.completed`` payload."""
    return any(
        event.type is EventType.AUDIT_COMPLETED
        and isinstance(event.payload.get("audit_report"), dict)
        and _model_matches(event.payload["audit_report"], report)
        for event in events
    )


def _contains_exact_decision(events: Sequence[Event], decision: PolicyDecision) -> bool:
    """Check that the selected decision is present byte-for-byte as a policy event."""
    return any(
        event.type is EventType.POLICY_DECISION and _model_matches(event.payload, decision)
        for event in events
    )


def _contains_exact_event(events: Sequence[Event], expected: Event) -> bool:
    """Check that an indexed event is the exact event embedded in the verified chain."""
    expected_json = expected.to_json_dict()
    return any(event.to_json_dict() == expected_json for event in events)


def _model_matches(payload: object, model: ThymiraModel) -> bool:
    """Compare a stored JSON object to one validated contract."""
    return isinstance(payload, dict) and payload == model.to_json_dict()


def _model_identity(models: Sequence[ThymiraModel]) -> tuple[str, ...]:
    """Return canonical model payloads for exact index comparisons."""
    return tuple(canonical_json(model.to_json_dict()) for model in models)


def _artifact_identity(artifacts: Sequence[Artifact]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return a stable name-to-record projection for an artifact manifest."""
    return tuple(
        (artifact.name, artifact.to_json_dict())
        for artifact in sorted(artifacts, key=lambda item: item.name)
    )


def _manifest_sha256(artifacts: Sequence[Artifact]) -> str:
    """Hash a complete artifact manifest using the same canonical representation as MIRA."""
    manifest = {
        artifact.name: artifact.to_json_dict()
        for artifact in sorted(artifacts, key=lambda item: item.name)
    }
    return sha256_text(canonical_json(manifest))


__all__ = [
    "ASSURANCE_BUNDLE_EXPORT",
    "ASSURANCE_DISCLAIMER",
    "REVIEW_DISCLAIMER",
    "REVIEW_FINDING_ID_KEY",
    "REVIEW_STATE_KEY",
    "ArtifactLineage",
    "AssuranceBundle",
    "AssuranceVerification",
    "EvidenceSource",
    "EvidenceSourceKind",
    "FindingReview",
    "FindingReviewState",
    "FindingTrace",
    "assemble_run_assurance",
    "build_assurance",
    "derive_finding_reviews",
    "latest_audit_report",
    "latest_findings_decision",
    "verify_assurance_bundle",
]
