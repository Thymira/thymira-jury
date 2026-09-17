"""MIRA: independent audit contracts, deterministic controls, and audit orchestration.

MIRA reads execution events and evidence. `thymira.mira.checks` holds the deterministic controls
ported from the thesis meta-auditor. GOV-01 defines the immutable audit-agent input, output, and
specification contracts. `MiraAuditFlow` is the canonical gate-less audit sequence.

MIRA reads evidence and produces assessments, bindings, and control evaluations. It never
modifies the workspace, authorizes an effect, or connects directly to THY.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from thymira.mira.assurance import (
    ASSURANCE_BUNDLE_EXPORT,
    ASSURANCE_DISCLAIMER,
    REVIEW_DISCLAIMER,
    ArtifactLineage,
    AssuranceBundle,
    AssuranceVerification,
    EvidenceSource,
    FindingReview,
    FindingReviewState,
    FindingTrace,
    assemble_run_assurance,
    build_assurance,
    derive_finding_reviews,
    latest_audit_report,
    latest_findings_decision,
    verify_assurance_bundle,
)
from thymira.mira.board_replay import (
    BoardReplay,
    BoardReplayError,
    replay_boards,
    replay_dependency_selection,
    replay_plan_revisions,
)
from thymira.mira.context import DecisionContextAnalyst, MiraContextInput, build_context_prompt
from thymira.mira.flow import (
    MiraAuditFlow,
    MiraAuditFlowConfig,
    MiraGraphInput,
    canonical_graph_definition_hash,
)
from thymira.mira.orchestrator import (
    MiraAuditOrchestrator,
    MiraAuditResult,
    MiraAuditSnapshot,
    MiraEvidenceAuditResult,
    MiraPreflightResult,
)
from thymira.mira.preflight import (
    ApplicabilityResolver,
    BaseRiskEvaluator,
    GenericPackControlRunner,
    load_default_packs,
)

if TYPE_CHECKING:
    from thymira.mira.agents import AuditAgentSpec, load_default_specs, load_specs
    from thymira.mira.audit_io import AuditAgentOutput, AuditInput

_LAZY_EXPORTS = {
    "AuditAgentOutput": ("thymira.mira.audit_io", "AuditAgentOutput"),
    "AuditAgentSpec": ("thymira.mira.agents", "AuditAgentSpec"),
    "AuditInput": ("thymira.mira.audit_io", "AuditInput"),
    "load_default_specs": ("thymira.mira.agents", "load_default_specs"),
    "load_specs": ("thymira.mira.agents", "load_specs"),
}


def __getattr__(name: str) -> object:
    """Load audit-agent contracts without widening bare ``import thymira.mira`` dependencies."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "ASSURANCE_BUNDLE_EXPORT",
    "ASSURANCE_DISCLAIMER",
    "REVIEW_DISCLAIMER",
    "ApplicabilityResolver",
    "ArtifactLineage",
    "AssuranceBundle",
    "AssuranceVerification",
    "AuditAgentOutput",
    "AuditAgentSpec",
    "AuditInput",
    "BaseRiskEvaluator",
    "BoardReplay",
    "BoardReplayError",
    "DecisionContextAnalyst",
    "EvidenceSource",
    "FindingReview",
    "FindingReviewState",
    "FindingTrace",
    "GenericPackControlRunner",
    "MiraAuditFlow",
    "MiraAuditFlowConfig",
    "MiraAuditOrchestrator",
    "MiraAuditResult",
    "MiraAuditSnapshot",
    "MiraContextInput",
    "MiraEvidenceAuditResult",
    "MiraGraphInput",
    "MiraPreflightResult",
    "assemble_run_assurance",
    "build_assurance",
    "build_context_prompt",
    "canonical_graph_definition_hash",
    "derive_finding_reviews",
    "latest_audit_report",
    "latest_findings_decision",
    "load_default_packs",
    "load_default_specs",
    "load_specs",
    "replay_boards",
    "replay_dependency_selection",
    "replay_plan_revisions",
    "verify_assurance_bundle",
]
