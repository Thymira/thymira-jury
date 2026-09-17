"""Canonical gate-less MIRA audit flow shared by standalone and Core composition."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import Field

from thymira.events import canonical_json, sha256_text
from thymira.mira.board_replay import (
    BoardReplay,
    replay_boards,
    replay_dependency_selection,
)
from thymira.mira.checks import AuditMode, AuditReport
from thymira.mira.grounding import RunEvidenceIndex, ground_findings
from thymira.mira.orchestrator import (
    MiraAuditOrchestrator,
    MiraAuditResult,
    MiraAuditSnapshot,
    MiraEvidenceAuditResult,
    MiraPreflightResult,
    _deduplicate_findings,
)
from thymira.mira.preflight import EvidenceObservation
from thymira.mira.verification import (
    DiscoveryLimits,
    IndependentAuditEvidence,
    VerificationDrop,
    _prefer_finding,
    finding_dedup_key,
    verify_candidate_findings,
)
from thymira.schemas import (
    ActivityProfile,
    Actor,
    AuditFinding,
    DagBoard,
    Event,
    EventType,
    Framework,
    PlanBoard,
    PolicyDecision,
    Run,
    TerminalAuditBinding,
    ThymiraModel,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.events import EventLog
    from thymira.mira.agents.spec import AuditAgentSpec
    from thymira.mira.audit_io import AuditAgentOutput
    from thymira.state import ArtifactStore

    type AuditAgentRunner = Callable[
        [AuditAgentSpec, str, tuple[Event, ...], AuditReport], AuditAgentOutput
    ]
    from thymira.mira.verification import FindingVerifier


class MiraGraphInput(ThymiraModel):
    """Immutable Run facts supplied to one MIRA audit phase.

    ``frameworks`` is the governance scope the project declares for this Run. It is the single
    source of truth for deterministic requirements coverage and for selecting applicable audit
    agents.
    """

    run: Run
    activity_profile: ActivityProfile
    events: tuple[Event, ...] = ()
    evidence_observations: tuple[EvidenceObservation, ...] = ()
    frameworks: tuple[Framework, ...] = ()
    audit_mode: AuditMode = AuditMode.FINAL
    audited_at: datetime
    plan_history: tuple[PlanBoard, ...] = ()
    dag_board: DagBoard | None = None
    block_after_same_cause: int = Field(default=3, ge=1)


# The sequence is metadata for provenance; the executable implementation is the methods below.
_AUDIT_FLOW_NODES: tuple[str, ...] = (
    "prep",
    "preflight",
    "evidence",
    "deterministic",
    "agents",
    "verification",
    "aggregate",
)
_AUDIT_FLOW_EDGES: tuple[tuple[str, str], ...] = (
    ("__start__", "prep"),
    ("prep", "preflight"),
    ("preflight", "evidence"),
    ("evidence", "deterministic"),
    ("deterministic", "agents"),
    ("agents", "verification"),
    ("verification", "aggregate"),
    ("aggregate", "__end__"),
)

_REGULATORY_EVIDENCE_AGENT = "reg_evidence"
_LOGGER = logging.getLogger(__name__)


def canonical_graph_definition_hash() -> str:
    """Return the stable hash of MIRA's reusable gate-less audit definition."""
    definition = {
        "nodes": list(_AUDIT_FLOW_NODES),
        "edges": [list(edge) for edge in _AUDIT_FLOW_EDGES],
    }
    return sha256_text(canonical_json(definition))


@dataclass(frozen=True, slots=True)
class MiraAuditFlowConfig:
    """Dependencies configuring one canonical MIRA flow instance."""

    orchestrator: MiraAuditOrchestrator
    event_log: EventLog
    artifact_store: ArtifactStore | None = None
    specs: tuple[AuditAgentSpec, ...] = ()
    run_agent: AuditAgentRunner | None = None
    verify_finding: FindingVerifier | None = None
    discovery: DiscoveryLimits | None = None
    before_agent_round: Callable[[], None] | None = None
    runtime_skill_names: tuple[str, ...] = ()


class MiraAuditFlow:
    """Execute MIRA's complete audit sequence without making a policy decision.

    The flow owns all MIRA preparation, evidence evaluation, deterministic controls, agent
    discovery, adversarial verification, deduplication, report assembly, and audit events. Its
    two public phases let Core run governance preflight before THY and the evidence audit after
    THY, while standalone callers can run both phases consecutively.

    Agent calls stay sequential: every call appends to one ordered event log and shares provider
    and usage accounting boundaries. The current audit has no clear parallelism gain that would
    outweigh the loss of deterministic event order and replay reproducibility.
    """

    def __init__(self, config: MiraAuditFlowConfig) -> None:
        self._config = config
        self._snapshot: MiraAuditSnapshot | None = None
        self._preflight: MiraPreflightResult | None = None
        self._result: MiraAuditResult | None = None
        self._verification_drops: tuple[VerificationDrop, ...] = ()
        self._board_replay: BoardReplay | None = None

    @property
    def run_id(self) -> str:
        """Return the Run id owned by this flow's event log."""
        return self._config.event_log.run_id

    def preflight(self, graph_input: MiraGraphInput) -> MiraPreflightResult:
        """Build the pre-THY snapshot, evaluate governance, and record its evidence events."""
        self._validate_input(graph_input)
        if self._preflight is not None:
            return self._preflight

        snapshot = _snapshot(graph_input)
        self._replay_board_evidence(graph_input, record_event=True)
        self._snapshot = snapshot
        self._config.event_log.append(
            EventType.AUDIT_STARTED,
            Actor.system(),
            {
                "activity_profile_id": snapshot.activity_profile.id,
                "activity_profile_version": snapshot.activity_profile.version,
                "activity_profile": snapshot.activity_profile.model_dump(mode="json"),
                "audited_at": snapshot.audited_at.isoformat(),
            },
            subject_id=snapshot.activity_profile.id,
            producer="thymira.mira",
        )
        preflight = self._config.orchestrator.run_governance_preflight(snapshot)
        self._config.event_log.append(
            EventType.RISK_ASSESSMENT_RECORDED,
            Actor.system(),
            {
                "risk_assessment": preflight.risk_assessment.model_dump(mode="json"),
                "preflight_findings": [
                    finding.model_dump(mode="json") for finding in preflight.audit_findings
                ],
            },
            subject_id=preflight.risk_assessment.id,
            producer="thymira.mira",
        )
        for binding in preflight.pack_bindings:
            self._config.event_log.append(
                EventType.PACK_BINDING_RECORDED,
                Actor.system(),
                {"pack_binding": binding.model_dump(mode="json")},
                subject_id=binding.id,
                producer="thymira.mira",
            )
        self._snapshot = snapshot
        self._preflight = preflight
        return preflight

    def restore_preflight(
        self, graph_input: MiraGraphInput, preflight: MiraPreflightResult
    ) -> MiraPreflightResult:
        """Restore persisted preflight evidence without appending duplicate events.

        Core may construct a fresh graph instance while resuming a Run after human approval.
        The authoritative event log already contains the preflight result, so recovery hydrates
        this flow from that evidence instead of executing governance a second time.
        """
        self._validate_input(graph_input)
        snapshot = _snapshot(graph_input)
        self._replay_board_evidence(graph_input, record_event=False)
        if (
            preflight.risk_assessment.activity_profile_id != graph_input.activity_profile.id
            or preflight.risk_assessment.activity_profile_version
            != graph_input.activity_profile.version
        ):
            raise ValueError("persisted MIRA preflight does not match the activity profile")
        self._snapshot = snapshot
        self._preflight = preflight
        return preflight

    def audit(self, graph_input: MiraGraphInput) -> MiraAuditResult:
        """Run the post-THY audit and record one complete, gate-less MIRA result."""
        self._validate_input(graph_input)
        preflight = self._preflight
        if preflight is None:
            raise RuntimeError("MIRA evidence audit requires governance preflight")
        if self._snapshot is None:
            raise RuntimeError("MIRA evidence audit is missing its preflight snapshot")
        if (
            preflight.risk_assessment.activity_profile_id != graph_input.activity_profile.id
            or preflight.risk_assessment.activity_profile_version
            != graph_input.activity_profile.version
        ):
            raise ValueError("MIRA audit profile differs from governance preflight")

        snapshot = _snapshot(graph_input)
        self._replay_board_evidence(graph_input, record_event=False)
        evidence_audit = self._config.orchestrator.run_evidence_audit(snapshot, preflight)
        self._record_evidence_audit(evidence_audit)
        deterministic_report = self._config.orchestrator.run_deterministic(
            snapshot, store=self._config.artifact_store
        )
        independent_evidence = IndependentAuditEvidence.capture(snapshot.events)
        candidate_findings = self._run_agents(
            snapshot,
            deterministic_report,
            (
                *preflight.audit_findings,
                *evidence_audit.audit_findings,
                *deterministic_report.findings,
            ),
            evidence=independent_evidence,
        )
        outcome = verify_candidate_findings(
            candidate_findings,
            self._config.verify_finding,
            independent_evidence.events,
            deterministic_report,
        )
        findings = _deduplicate_findings(outcome.kept)
        result = self._config.orchestrator.assemble_result(
            snapshot,
            preflight,
            evidence_audit,
            deterministic_report,
            findings,
        )
        self._record_findings(findings)
        self._result = result
        self._verification_drops = outcome.drop_records
        return result

    def run(self, graph_input: MiraGraphInput) -> MiraAuditResult:
        """Run preflight and the final audit consecutively for standalone callers."""
        self.preflight(graph_input)
        return self.audit(graph_input)

    def complete(
        self,
        policy_decision: PolicyDecision | None = None,
        *,
        policy_sha256: str | None = None,
        graph_definition_hash: str | None = None,
        terminal_audit_binding: TerminalAuditBinding | None = None,
    ) -> None:
        """Record the complete MIRA report, optionally linking current policy and flow versions."""
        result = self._result
        if result is None:
            raise RuntimeError("MIRA audit completion requires an audit result")
        events = self._config.event_log.events()
        audit_revision = sum(event.type is EventType.AUDIT_COMPLETED for event in events) + 1
        report = result.audit_report.model_copy(
            update={
                "terminal_hash": events[-1].hash if events else result.audit_report.terminal_hash,
                "policy_sha256": policy_sha256,
                "graph_definition_hash": graph_definition_hash or canonical_graph_definition_hash(),
            }
        )
        payload: dict[str, Any] = {
            "status": report.status,
            "audit_report": report.model_dump(mode="json"),
            "verified_dropped": len(self._verification_drops),
            "verified_drops": [
                {
                    "finding": drop.finding.model_dump(mode="json"),
                    "reason": drop.rationale,
                }
                for drop in self._verification_drops
            ],
            "audit_revision": audit_revision,
        }
        if policy_decision is not None:
            payload["policy_decision_id"] = policy_decision.id
        if terminal_audit_binding is not None:
            payload["terminal_audit_binding"] = terminal_audit_binding.to_json_dict()
        self._config.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            payload,
            subject_id=result.audit_report.run_id,
            producer="thymira.mira",
        )

    def _validate_input(self, graph_input: MiraGraphInput) -> None:
        """Validate that an input belongs to this flow's Run and event log."""
        if graph_input.run.id != self.run_id:
            raise ValueError("MIRA input run_id must match the injected event log run_id")

    def _replay_board_evidence(
        self, graph_input: MiraGraphInput, *, record_event: bool
    ) -> BoardReplay | None:
        """Consume board evidence through MIRA's independent replay before audit controls run."""
        dag_board = graph_input.dag_board
        if not graph_input.plan_history and dag_board is None:
            return None
        if graph_input.plan_history:
            replay = replay_boards(
                graph_input.plan_history,
                dag_board,
                block_after_same_cause=graph_input.block_after_same_cause,
            )
        else:
            if dag_board is None:
                raise ValueError(
                    "MIRA board replay requires a DAG when no plan history is supplied"
                )
            replay = BoardReplay(
                run_id=dag_board.run_id,
                plan_revision=0,
                blocked_count=0,
                blocked_cause=None,
                blocked=False,
                runnable_work_ids=replay_dependency_selection(dag_board),
            )
        if replay.run_id != graph_input.run.id:
            raise ValueError("MIRA board evidence belongs to another Run")
        if self._board_replay is not None and self._board_replay != replay:
            raise ValueError("MIRA board evidence changed during one audit")
        self._board_replay = replay
        if record_event:
            self._config.event_log.append(
                EventType.MIRA_BOARD_REPLAYED,
                Actor.system(),
                {
                    "plan_revision": replay.plan_revision,
                    "blocked_count": replay.blocked_count,
                    "blocked_cause": replay.blocked_cause,
                    "blocked": replay.blocked,
                    "runnable_work_ids": list(replay.runnable_work_ids),
                },
                subject_id=replay.run_id,
                producer="thymira.mira",
            )
        return replay

    def _run_agents(
        self,
        snapshot: MiraAuditSnapshot,
        report: AuditReport,
        existing_findings: Sequence[AuditFinding],
        *,
        evidence: IndependentAuditEvidence | None = None,
    ) -> tuple[AuditFinding, ...]:
        """Run configured agents, replacing enrichment updates before one final audit pass.

        Every producer candidate is grounded against the Run's own log before it is considered
        new. The shipped runner already refuses an unbacked reference, but ``run_agent`` is an
        injection seam, and the flow is what owns the invariant: a reference the Run cannot back
        must not reach the deduplication key, the report, or the assurance bundle. Enrichment
        output bypasses this pass because it carries hash-verified knowledge-base citations that
        point outside the Run by construction, and the enricher resolves each of them itself.
        """
        independent_events = evidence.events if evidence is not None else snapshot.events
        applicable_specs = tuple(
            spec for spec in self._config.specs if spec.framework in snapshot.frameworks
        )
        if not applicable_specs:
            return tuple(existing_findings)
        runner = self._config.run_agent
        if runner is None:
            raise RuntimeError("MIRA agent specs require an injected audit runner")
        candidates = list(existing_findings)
        producer_specs = tuple(
            spec for spec in applicable_specs if spec.name != _REGULATORY_EVIDENCE_AGENT
        )
        enrichment_specs = tuple(
            spec for spec in applicable_specs if spec.name == _REGULATORY_EVIDENCE_AGENT
        )
        for round_number in range(
            self._config.discovery.max_rounds if self._config.discovery else 1
        ):
            if round_number > 0 and self._config.before_agent_round is not None:
                self._config.before_agent_round()
            new_this_pass = False
            for spec in producer_specs:
                selected_spec = self._with_configured_runtime_skills(spec)
                output = runner(
                    selected_spec,
                    snapshot.run.id,
                    independent_events,
                    report,
                )
                # Re-read the log after each agent: its own recorded tool calls are evidence it
                # may legitimately cite, and they did not exist when the round began.
                grounded, _ = ground_findings(
                    output.findings,
                    RunEvidenceIndex.from_events(self._config.event_log.events()),
                )
                indexes = {
                    finding_dedup_key(candidate): index
                    for index, candidate in enumerate(candidates)
                }
                for finding in grounded:
                    key = finding_dedup_key(finding)
                    index = indexes.get(key)
                    if index is not None:
                        candidates[index] = _prefer_finding(candidates[index], finding)
                        continue
                    indexes[key] = len(candidates)
                    candidates.append(finding)
                    new_this_pass = True
            if not new_this_pass:
                break
        for spec in enrichment_specs:
            selected_spec = self._with_configured_runtime_skills(spec)
            output = runner(
                selected_spec,
                snapshot.run.id,
                independent_events,
                report.model_copy(update={"findings": tuple(candidates)}),
            )
            for finding in output.findings:
                if not _replace_candidate(candidates, finding):
                    _LOGGER.warning(
                        "MIRA enrichment returned unknown finding id %r; discarding candidate",
                        finding.id,
                    )
        return tuple(candidates)

    def _with_configured_runtime_skills(self, spec: AuditAgentSpec) -> AuditAgentSpec:
        """Apply flow-level MIRA names when a spec leaves its selection empty."""
        if not self._config.runtime_skill_names or spec.runtime_skill_names:
            return spec
        return spec.model_copy(update={"runtime_skill_names": self._config.runtime_skill_names})

    def _record_evidence_audit(self, evidence_audit: MiraEvidenceAuditResult) -> None:
        """Record evidence-control evaluations in the authoritative MIRA event log."""
        for evaluation in evidence_audit.control_evaluations:
            self._config.event_log.append(
                EventType.CONTROL_EVALUATION_RECORDED,
                Actor.system(),
                {"control_evaluation": evaluation.model_dump(mode="json")},
                subject_id=evaluation.id,
                producer="thymira.mira",
            )

    def _record_findings(self, findings: Sequence[AuditFinding]) -> None:
        """Record one event for each final deduplicated finding."""
        for finding in findings:
            self._config.event_log.append(
                EventType.AUDIT_FINDING,
                Actor.system(),
                {"finding": finding.model_dump(mode="json")},
                subject_id=finding.id,
                producer="thymira.mira",
            )


def _snapshot(graph_input: MiraGraphInput) -> MiraAuditSnapshot:
    """Convert public graph input into the immutable orchestrator snapshot."""
    return MiraAuditSnapshot(
        run=graph_input.run,
        activity_profile=graph_input.activity_profile,
        events=graph_input.events,
        evidence_observations=graph_input.evidence_observations,
        frameworks=graph_input.frameworks,
        audit_mode=graph_input.audit_mode,
        audited_at=graph_input.audited_at,
        plan_history=graph_input.plan_history,
        dag_board=graph_input.dag_board,
        block_after_same_cause=graph_input.block_after_same_cause,
    )


def _replace_candidate(candidates: list[AuditFinding], replacement: AuditFinding) -> bool:
    """Replace one existing candidate by its authority-owned id, preserving its position."""
    for index, candidate in enumerate(candidates):
        if candidate.id == replacement.id:
            candidates[index] = replacement
            return True
    return False


__all__ = [
    "MiraAuditFlow",
    "MiraAuditFlowConfig",
    "MiraGraphInput",
    "canonical_graph_definition_hash",
]
