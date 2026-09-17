"""Production ``Subgraph`` adapters and the composition-root graph factory (RA-CORE-06).

``build_runtime_graph`` and the ``Subgraph`` protocol already exist; what was missing was a
concrete adapter for each side and a factory that wires the real handles together, so the shipped
API could execute a real Run instead of raising ``_unconfigured_graph_factory``. This module
supplies both.

Two adapters, one factory:

* :class:`ThySubgraph` runs the real ThyGraph (Inspect -> Plan -> Execute -> Summarize) and folds
  its typed ``ThyOutput`` back onto the shared ``RuntimeState``. See its docstring for the exact
  ``ThyOutput`` -> ``RuntimeState`` mapping and the error behaviour.
* :class:`MiraSubgraph` is a thin adapter over MIRA's canonical gate-less audit flow. It never
  calls the findings Gate directly: the composition graph's ``review`` node is the single
  run-level ``Gate.review_findings`` decision (RA-CORE-06 composes ``THY(execution) -> MIRA(audit)
  -> Gate.review_findings -> terminal``). An uncertain model risk judgement may use the separate
  ``MiraControlPlane`` to request missing information and pause the Run.
* :func:`build_runtime_graph_factory` builds, per Run, the event log, Gate, artifact store,
  ``SubgraphDeps``, ``StateCheckpointer``, ``RunController`` and the composed graph.

Layering: this module lives in ``thymira.core`` and imports ``thymira.thy``, ``thymira.mira`` and
``thymira.agents`` (all below ``core``); it imports nothing from ``thymira.api``.
"""

from __future__ import annotations

import shutil
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from thymira.agents import (
    AgentCatalog,
    RequestLedger,
    RuntimeSkillCatalog,
    RuntimeSkillManifest,
    load_runtime_skill_catalog,
    verify_runtime_skill_manifest,
)
from thymira.agents.llm import LLMConfigurationError, provider_for
from thymira.agents.llm.routing import UNCONFIGURED_MODEL, ModelChoice, Role, choose
from thymira.core.control_plane import MiraControlPlane, RunController, RunEventLog
from thymira.core.execution_review import RISK_UNCERTAINTY_RESUME_TARGET, reviewed_risk_profile
from thymira.core.graph.checkpoint import StateCheckpointer
from thymira.core.graph.compose import build_runtime_graph
from thymira.core.graph.compose import graph_definition_hash as _compose_graph_definition_hash
from thymira.core.graph.protocol import SubgraphDeps
from thymira.core.graph.risk_reconciliation import (
    RISK_UNCERTAINTY_REVIEW_SUMMARY,
    audit_with_reviewed_risk_classification,
    risk_uncertainty_review_resolved,
)
from thymira.core.graph.state import initial_runtime_state
from thymira.core.risk_interview import RiskInterviewModelContext
from thymira.core.usage import UsageLedgerRegistry
from thymira.events import canonical_json, sha256_text
from thymira.mira.agents import load_default_specs, load_mira_skill_catalog
from thymira.mira.agents.dispatcher import (
    REGULATORY_EVIDENCE_AGENT,
    dispatch_audit_agent,
)
from thymira.mira.agents.reg_evidence import CitationIndex
from thymira.mira.agents.runner import AuditAgentContext
from thymira.mira.agents.verification import build_finding_verifier
from thymira.mira.assurance import ASSURANCE_BUNDLE_EXPORT, assemble_run_assurance
from thymira.mira.audit_io import AuditInput
from thymira.mira.checks import AuditMode, ControlStatus
from thymira.mira.evidence import ArtifactStoreEvidenceReader
from thymira.mira.flow import MiraAuditFlow, MiraAuditFlowConfig, MiraGraphInput
from thymira.mira.flow import canonical_graph_definition_hash as _mira_graph_definition_hash
from thymira.mira.kb import load_default_regulation_store
from thymira.mira.orchestrator import MiraAuditOrchestrator, MiraPreflightResult
from thymira.mira.preflight import (
    MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX,
    RiskAssessmentModelContext,
    load_default_packs,
)
from thymira.mira.tools import build_mira_tool_registry
from thymira.mira.verification import DiscoveryLimits
from thymira.observability import agent as agent_observation
from thymira.observability import score_run, update_current
from thymira.policies import RiskProfile
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    ActivityProfile,
    Actor,
    AuditFinding,
    Decision,
    EventType,
    ExecutionConstraints,
    ModelRoutePolicy,
    PackBinding,
    RiskAssessment,
    RunCondition,
    TerminalAuditBinding,
    TurnEnded,
    WaitReason,
    new_id,
    utc_now,
)
from thymira.state import LifecycleError, OwnedArtifactStore, OwnedCheckpointRepository
from thymira.thy.agents import (
    coding_agent_catalog,
    data_agent_catalog,
    experiment_agent_catalog,
)
from thymira.thy.context import load_project_context
from thymira.thy.graph import graph_definition_hash as _thy_graph_definition_hash
from thymira.thy.graph import run_thy
from thymira.thy.models import ThyInput, ThyState
from thymira.tools import BudgetRefusal, ToolContext

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime

    from langgraph.graph.state import CompiledStateGraph
    from pydantic import BaseModel

    from thymira.agents import AgentSpec
    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.core.dispatch import GraphFactory
    from thymira.core.graph.state import RuntimeState
    from thymira.core.risk_interview import RiskInterviewService
    from thymira.core.usage import UsageLedger
    from thymira.events import EventLog
    from thymira.mira.agents import AuditAgentSpec
    from thymira.mira.audit_io import AuditAgentOutput
    from thymira.mira.checks import AuditReport
    from thymira.mira.evidence import EvidenceReader
    from thymira.mira.preflight import ReviewedPack
    from thymira.mira.verification import FindingVerifier
    from thymira.policies import Gate
    from thymira.schemas import (
        Artifact,
        DagBoard,
        Event,
        Framework,
        Id,
        PlanBoard,
        ProjectConfig,
        Run,
        WorkItem,
        WorkResult,
    )
    from thymira.state import (
        ArtifactStore,
        BoardRepository,
        CheckpointRepository,
        DagRepository,
        LocalRunStore,
        RecordRepository,
        RunHandle,
    )
    from thymira.tools import ToolManager, ToolRegistry

    type AuditAgentRunner = Callable[
        [AuditAgentSpec, str, tuple[Event, ...], AuditReport], AuditAgentOutput
    ]


class _LifecycleGraphFactory:
    """Callable production graph factory with its owner-bound terminal MIRA seam."""

    def __init__(
        self,
        builder: Callable[[Run], CompiledStateGraph],
        terminal_auditor: Callable[[RunHandle, WorkItem, WorkResult, TerminalAuditBinding], None],
    ) -> None:
        self._builder = builder
        self.audit_terminal = terminal_auditor

    def __call__(self, run: Run) -> CompiledStateGraph:
        """Build one normal composed graph."""
        return self._builder(run)


_THY_SUBGRAPH_NAME = "thy"
_MIRA_SUBGRAPH_NAME = "mira"


class ThyExecutionError(RuntimeError):
    """Raised when a THY run finishes with an error, halting the composed Run.

    ``InlineDispatcher.submit`` catches ``RuntimeError`` and re-raises it as
    ``ExecutionDispatchError``; ``RunService.create_run`` then records a terminal ``RUN_FAILED``
    transition. Subclassing ``RuntimeError`` is precisely what turns a THY error into a consistent
    FAILED Run rather than an unhandled exception that escapes ``create_run``.
    """


def default_thy_catalog() -> AgentCatalog:
    """Compose the three ``ThyAgentKind`` specialist catalogs into the default THY catalog.

    ThyGraph's Execute node resolves each ``AgentTask.agent`` (``data``/``coding``/``experiment``)
    through ``AgentCatalog.get(kind.value)``, so the composed catalog must carry all three. No
    catalog builder merges specs today, so this folds the per-agent catalogs -- each with its
    already-resolved system prompt and output schema -- into one.
    """
    return _merge_catalogs(data_agent_catalog(), coding_agent_catalog(), experiment_agent_catalog())


def _merge_catalogs(*catalogs: AgentCatalog) -> AgentCatalog:
    """Merge name-disjoint catalogs, keeping each spec's resolved prompt and output schema."""
    specs: list[AgentSpec] = []
    system_prompts: dict[str, str] = {}
    output_schemas: dict[str, type[BaseModel]] = {}
    for catalog in catalogs:
        for name in catalog.names():
            specs.append(catalog.get(name))
            system_prompts[name] = catalog.system_prompt(name)
            output_schemas[name] = catalog.output_schema(name)
    return AgentCatalog(tuple(specs), system_prompts=system_prompts, output_schemas=output_schemas)


def default_activity_profile(
    run: Run, *, project_config: ProjectConfig | None = None
) -> ActivityProfile:
    """Derive a conservative, honest ``ActivityProfile`` for one Run.

    Nothing in production authors an activity profile yet (``GovernanceConfig`` carries no such
    fields), so every fact MIRA's risk method needs is declared *undeclared* rather than
    fabricated. With no ``data_categories`` or ``potential_consequences`` the base-risk method
    returns UNKNOWN and MIRA raises its "base risk cannot be classified" finding -- the truthful
    signal that the activity has not been described, never a fabricated low-risk profile.

    ``project_config`` is accepted for the future but contributes nothing today, because
    ``GovernanceConfig`` exposes no activity facts to enrich the profile with.
    """
    # TODO(arch): RISK-01 (activity-profile authoring) replaces this honest placeholder with a
    # profile authored from the project and run, instead of this conservative undeclared default.
    del project_config
    digest = sha256_text(run.id)
    return ActivityProfile(
        id=f"profile_{digest[:32]}",
        activity_id=f"activity_{digest[32:64]}",
        version=1,
        run_id=run.id,
        purpose="undeclared",
        affected_population="undeclared",
        decision_effect="undeclared",
        autonomy="undeclared",
        human_oversight="undeclared",
        jurisdiction="undeclared",
        data_categories=("undeclared",),
        sensitive_attributes=("undeclared",),
        potential_consequences=("undeclared",),
    )


def build_audit_agent_runner(
    event_log: EventLog,
    actor: Actor,
    *,
    provider: LLMProvider | None = None,
    evidence_reader: EvidenceReader | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_context_factory: Callable[[str, str], ToolContext] | None = None,
    citation_index: CitationIndex | None = None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    runtime_skill_budget: int = 32_000,
    runtime_skill_names: tuple[str, ...] = (),
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    route_policy: ModelRoutePolicy | None = None,
    request_ledger: RequestLedger | None = None,
) -> AuditAgentRunner:
    """Return a runner that executes one MIRA audit agent over the composed Run's evidence.

    The composition supplies the audit-agent callable across the ``thymira.mira`` boundary,
    exactly as MiraAuditFlow's ``run_agent`` seam expects. Each call derives one ``AuditInput``
    from the Run's events and current report, mints a fresh ``agent``/``task`` identity, and
    delegates to MIRA's
    explicit specialist dispatcher. The dispatcher keeps ``run_audit_agent`` as the fallback and
    uses ``citation_index`` only for the regulatory-evidence enrichment branch.

    Provider handling mirrors :class:`ThySubgraph`: ``provider`` is ``None`` in production (each
    agent's model is router-resolved through ``routed_model``) and a ``ScriptedProvider`` in tests,
    so no network call happens. The optional reader is the only content-read capability supplied
    to MIRA and remains bounded, hash-pinned, redacted, and Run-scoped. When a MIRA registry is
    supplied, the matching context factory provides the Gate, artifact store, event log, and policy
    risk facts for each agent identity; the runner still delegates calls to the shared ToolManager.
    """
    if (tool_registry is None) is not (tool_context_factory is None):
        raise ValueError("tool_registry and tool_context_factory must be provided together")

    def run_agent(
        spec: AuditAgentSpec,
        run_id: str,
        events: tuple[Event, ...],
        report: AuditReport,
    ) -> AuditAgentOutput:
        """Execute one audit agent through the bounded MIRA runner."""
        del run_id  # Derived from the evidence by ``AuditInput.from_run`` and re-validated there.
        audit_input = AuditInput.from_run(events, report)
        agent_id = new_id("agent")
        task_id = new_id("task")
        tool_context = (
            tool_context_factory(agent_id, task_id) if tool_context_factory is not None else None
        )
        context = AuditAgentContext(
            event_log=event_log,
            actor=actor,
            agent_id=agent_id,
            task_id=task_id,
            provider=provider,
            tool_registry=tool_registry,
            tool_context=tool_context,
            evidence_reader=evidence_reader,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
            request_ledger=request_ledger,
            runtime_skill_catalog=runtime_skill_catalog,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=runtime_skill_names,
        )
        with agent_observation(name=spec.name, objective=spec.framework.value) as observation:
            output = dispatch_audit_agent(
                spec,
                audit_input,
                context,
                citation_index=citation_index,
            )
            observation.update(output={"findings": len(output.findings)})
            return output

    return run_agent


def _score_controls(run_id: str, report: AuditReport) -> None:
    """Mirror deterministic MIRA verdicts after the authoritative report exists."""
    for control in report.controls:
        score_run(
            run_id=run_id,
            name=f"mira.control.{control.control_id}",
            value=control.status.value,
            data_type="CATEGORICAL",
            comment=control.title,
        )
    failed = sum(1 for control in report.controls if control.status is ControlStatus.FAILED)
    score_run(
        run_id=run_id,
        name="mira.controls.failed",
        value=float(failed),
        data_type="NUMERIC",
    )
    score_run(
        run_id=run_id,
        name="mira.audit.status",
        value=report.status,
        data_type="CATEGORICAL",
    )


class ThySubgraph:
    """Adapt ThyGraph to the core ``Subgraph`` protocol for one Run.

    Constructed per Run by the graph factory (it closes over the ``Run``). ``invoke`` runs the real
    ThyGraph through ``run_thy`` with the composition's own event log, Gate and artifact store.

    ``ThyOutput`` -> ``RuntimeState`` mapping. ``run_id``/``project_id`` are never changed (the
    composition validates identity). ``ThyOutput`` also carries ``summary``,
    ``experiment_ids`` and ``artifact_ids``, none of which has a faithful home on
    ``RuntimeState``: ``task_ids`` names schema ``Task`` ids, not experiment or artifact ids, and
    ``phase`` is a ``thymira.core.phases.Phase`` -- a different vocabulary from ``ThyPhase``, which
    ``ThyOutput`` does not even carry -- while the composition drives lifecycle through
    ``RunController``/``RunStage``, never through ``RuntimeState.phase``. Inventing a mapping would
    misrepresent the data, so on success the state is returned with its identity preserved and no
    fabricated fields; THY's experiments and artifacts stay durable in the event log and artifact
    store, where MIRA and the read surfaces already read them.

    ``ThyOutput.progress`` is the one THY fact the state does carry, under THY's own name and type
    (``RuntimeState.thy_progress``): a pass that stopped on a tool call awaiting a human hands back
    its plan and outcomes, and the next pass is seeded from them. It is THY's record, not a mapping
    onto Core vocabulary. ``rework_signal`` takes precedence over it when seeding, and the two
    never coexist: the composition clears the signal after every invoke, and a pass that parks
    during a rework re-entry has already applied the reopen, so its resume needs only the progress.

    Error path. A non-``None`` ``ThyOutput.error`` raises :class:`ThyExecutionError`. This means a
    policy-rejected plan or a failed dataset intake (Inspect) -- in both cases nothing ran, so
    there is nothing to audit -- never a failed task: `ThyGraph`'s Execute node always reaches
    Summarize regardless of a task failure (bug-hunt C1), recording the failure as evidence
    instead of hiding the Run from audit. Because ``InlineDispatcher`` converts a ``RuntimeError``
    into ``ExecutionDispatchError`` and ``RunService`` then records a terminal ``RUN_FAILED``, a
    genuine plan-rejection or failed intake still leaves the persisted Run a consistent FAILED
    terminal (proven by test) -- it just no longer happens for a Run that actually attempted work.
    """

    name = _THY_SUBGRAPH_NAME

    def __init__(
        self,
        run: Run,
        catalog: AgentCatalog,
        *,
        provider: LLMProvider | None = None,
        project_dir: Path | None = None,
        metric: str = "accuracy",
        tool_registry: ToolRegistry | None = None,
        route_policy: ModelRoutePolicy | None = None,
        runtime_skill_catalog: RuntimeSkillCatalog | None = None,
        runtime_skill_budget: int = 32_000,
        runtime_skill_names: tuple[str, ...] = (),
    ) -> None:
        self.run = run
        self.catalog = catalog
        self.provider = provider
        self.project_dir = project_dir
        self.metric = metric
        self.tool_registry = tool_registry
        # A Run already carries the immutable session snapshot. An explicit value is reserved for
        # a composition-root narrowing after live policy revocation; omission must still bind the
        # Run identity rather than leaving an injected provider seam policy-less.
        self.route_policy = run.model_route_policy if route_policy is None else route_policy
        self.runtime_skill_catalog = runtime_skill_catalog
        self.runtime_skill_budget = runtime_skill_budget
        self.runtime_skill_names = runtime_skill_names

    def graph_version(self) -> str:
        """Return ThyGraph's stable definition hash."""
        return _thy_graph_definition_hash()

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Run ThyGraph over the Run and fold its typed output back onto the runtime state."""
        if state.run_id != self.run.id:
            raise ValueError("ThySubgraph received a runtime state for a different Run")
        resolved_tool_registry = self.tool_registry
        if resolved_tool_registry is None and self.project_dir is not None:
            resolved_tool_registry = deps.tool_manager.registry
        tool_context = None
        if resolved_tool_registry is not None:
            workspace = _run_workspace(self.project_dir, self.run.id)
            _stage_declared_datasets(self.project_dir, workspace)
            tool_context = ToolContext(
                run_id=self.run.id,
                agent_id=new_id("agent"),
                workspace=workspace,
                event_log=deps.event_log,
                gate=deps.gate,
                artifact_store=deps.artifact_store,
                # A human who approved the start review's uncertainty escalation answered the
                # classification once; the tool decisions no longer re-ask for the same reasons.
                risk_profile=reviewed_risk_profile(
                    state.risk_profile or RiskProfile(),
                    state.execution_decision,
                    deps.event_log.events(),
                ),
                project_context=_project_context_text(self.project_dir),
                workspace_dataset_paths=_declared_dataset_workspace_paths(self.project_dir),
                execution_constraints=state.execution_constraints or ExecutionConstraints(),
                execution_decision=state.execution_decision,
                budget_guard=_tool_budget_guard(deps),
            )
        before_manifest = deps.artifact_store.manifest()
        initial_state = None
        if state.rework_signal is not None:
            initial_state = ThyState(
                run=self.run,
                rework=state.rework_signal,
                # The Core transition has already spent the budget unit; THY consumes the
                # signal at its single re-entry without spending it a second time.
                reopen_count=max(0, state.reopen_count - 1),
                max_reopens=state.max_reopens,
                single_rework=True,
                execution_constraints=state.execution_constraints,
            )
        elif state.thy_progress is not None:
            # A resume after a human answered a tool-call review: seed the pass with what the
            # parked pass decided, so Plan short-circuits and Execute re-runs only the task that
            # asked. Inspect re-registers nothing (idempotent) and Summarize runs at the end.
            initial_state = ThyState(
                run=self.run,
                plan=state.thy_progress.plan,
                completed=state.thy_progress.completed,
                agent_messages=state.thy_progress.agent_messages,
                experiments=state.thy_progress.experiments,
                artifacts=state.thy_progress.artifacts,
                execution_constraints=state.execution_constraints,
            )
        output = run_thy(
            ThyInput(run=self.run, execution_constraints=state.execution_constraints),
            self.catalog,
            deps.event_log,
            provider=self.provider,
            project_dir=self.project_dir,
            gate=deps.gate,
            artifact_store=deps.artifact_store,
            tool_registry=resolved_tool_registry,
            tool_context=tool_context,
            runtime_skill_catalog=self.runtime_skill_catalog,
            runtime_skill_budget=self.runtime_skill_budget,
            runtime_skill_names=self.runtime_skill_names,
            metric=self.metric,
            before_model_request=deps.before_model_request,
            before_execute=deps.before_thy_execute,
            before_model_selection=_model_budget_guard(deps),
            before_model_call=_model_call_guard(deps),
            record_model_usage=_model_usage_recorder(deps),
            request_ledger=deps.request_ledger,
            initial_state=initial_state,
            route_policy=self.route_policy,
        )
        _record_artifact_changes(deps, before_manifest)
        if output.error is not None:
            raise ThyExecutionError(
                f"run {self.run.id}: THY halted before completion: {output.error}"
            )
        update_current(
            output={
                "summary": output.summary,
                "experiments": len(output.experiment_ids),
                "artifacts": len(output.artifact_ids),
            }
        )
        return state.model_copy(update={"rework_signal": None, "thy_progress": output.progress})


def _record_artifact_changes(
    deps: SubgraphDeps,
    before_manifest: dict[str, Artifact],
) -> None:
    """Announce new revisions and preserved superseded revisions on the Run event chain."""
    after_manifest = deps.artifact_store.manifest()
    events = deps.event_log.events()
    announced = {
        event.payload.get("artifact_id")
        for event in events
        if event.type is EventType.ARTIFACT_CREATED
    }
    for artifact in after_manifest.values():
        if artifact.valid and artifact.id not in announced:
            deps.event_log.append(
                EventType.ARTIFACT_CREATED,
                Actor.system(),
                {
                    "name": artifact.name,
                    "sha256": artifact.sha256,
                    "artifact_id": artifact.id,
                    "produced_by": artifact.produced_by,
                },
                subject_id=artifact.id,
                producer="thymira.core",
                producer_version="0.1",
            )
    for previous in before_manifest.values():
        if not previous.valid:
            continue
        replacement = next(
            (artifact for artifact in after_manifest.values() if artifact.id == previous.id),
            None,
        )
        if replacement is not None and not replacement.valid:
            already_announced = any(
                event.type is EventType.ARTIFACT_INVALIDATED
                and event.payload.get("artifact_id") == previous.id
                for event in events
            )
            if not already_announced:
                deps.event_log.append(
                    EventType.ARTIFACT_INVALIDATED,
                    Actor.system(),
                    {
                        "name": previous.name,
                        "artifact_id": previous.id,
                        "reason": replacement.invalidated_reason or "superseded",
                    },
                    subject_id=previous.id,
                    producer="thymira.core",
                    producer_version="0.1",
                )


def _audit_tool_risk_profile() -> RiskProfile:
    """Classify Core's fixed local audit-evidence operation, never the audited activity.

    MIRA's tool registry is an independent audit surface.  Its capability still goes through the
    Run's Policy Engine, which blocks external effects and reviews local side effects; these facts
    only prevent uncertainty about the audited activity from turning a read-only regulation-store
    lookup into another review of that unrelated activity.
    """
    return RiskProfile(risk_level="low", activity_category="audit", confidence=1.0)


class MiraSubgraph:
    """Adapt the canonical MIRA audit flow to Core's two lifecycle checkpoints.

    ``preflight`` runs before THY and ``invoke`` runs after THY. All MIRA audit logic, including
    evidence events, agents, discovery, verification, deduplication, and report construction, lives
    in :class:`thymira.mira.MiraAuditFlow`; this adapter only maps Core state and dependencies. It
    never calls the findings Gate because Core's ``review`` node owns that one decision. When
    model-assisted inherent-risk classification is uncertain, the composition supplies the
    ``MiraControlPlane`` as the separate, policy-gated Run-control boundary.
    """

    name = _MIRA_SUBGRAPH_NAME

    def __init__(
        self,
        run: Run,
        activity_profile: ActivityProfile,
        packs: tuple[ReviewedPack, ...],
        actor: Actor,
        *,
        frameworks: tuple[Framework, ...] = (),
        specs: Sequence[AuditAgentSpec] = (),
        provider: LLMProvider | None = None,
        run_agent: AuditAgentRunner | None = None,
        verify_finding: FindingVerifier | None = None,
        discovery: DiscoveryLimits | None = None,
        tool_registry: ToolRegistry | None = None,
        citation_index: CitationIndex | None = None,
        workspace: Path | None = None,
        route_policy: ModelRoutePolicy | None = None,
        runtime_skill_catalog: RuntimeSkillCatalog | None = None,
        runtime_skill_budget: int = 32_000,
        runtime_skill_names: tuple[str, ...] = (),
        runtime_skill_manifest_verifier: Callable[[], None] | None = None,
        now: Callable[[], datetime] = utc_now,
        profile_resolver: Callable[[], ActivityProfile] | None = None,
        control_plane: MiraControlPlane | None = None,
    ) -> None:
        self.run = run
        self.activity_profile = activity_profile
        self.packs = packs
        self.actor = actor
        self.frameworks = frameworks
        self.specs = tuple(specs)
        self.provider = provider
        self.run_agent = run_agent
        self.verify_finding = verify_finding
        self.discovery = discovery
        self.tool_registry = tool_registry
        self.citation_index = citation_index
        self.workspace = workspace or Path()
        # Bind the immutable Run snapshot by default; composition may pass a narrowed policy.
        self.route_policy = run.model_route_policy if route_policy is None else route_policy
        self.runtime_skill_catalog = runtime_skill_catalog
        self.runtime_skill_budget = runtime_skill_budget
        self.runtime_skill_names = runtime_skill_names
        self.runtime_skill_manifest_verifier = runtime_skill_manifest_verifier
        self.now = now
        self._profile_resolver = profile_resolver
        self._control_plane = control_plane
        self._preflight: MiraPreflightResult | None = None
        self._flow: MiraAuditFlow | None = None
        self._preflight_profile_key: tuple[str, int, str] | None = None

    def graph_version(self) -> str:
        """Return the canonical gate-less MIRA definition hash."""
        return _mira_graph_definition_hash()

    def preflight(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Run the canonical MIRA preflight before THY starts execution."""
        if state.run_id != self.run.id:
            raise ValueError("MiraSubgraph received a runtime state for a different Run")
        self._restore_persisted_preflight(deps)
        profile_key = (
            self.activity_profile.id,
            self.activity_profile.version,
            sha256_text(canonical_json(self.activity_profile.to_json_dict())),
        )
        if self._preflight is not None and self._preflight_profile_key == profile_key:
            self._handle_risk_uncertainty(deps, self._preflight.risk_assessment)
            return state
        if self._preflight is not None:
            # A new activity-profile version changes the inherent-risk and applicability inputs;
            # rebuild MIRA's canonical flow and run preflight once for that new version.
            self._preflight = None
            self._flow = None
        flow = self._ensure_flow(deps)
        plan_history, dag_board = self._board_evidence(deps)
        self._preflight = flow.preflight(
            MiraGraphInput(
                run=self.run,
                activity_profile=self.activity_profile,
                events=tuple(deps.event_log.events()),
                frameworks=self.frameworks,
                audit_mode=AuditMode.IN_FLIGHT,
                audited_at=self.now(),
                plan_history=plan_history,
                dag_board=dag_board,
            )
        )
        self._preflight_profile_key = profile_key
        self._handle_risk_uncertainty(deps, self._preflight.risk_assessment)
        return state

    def _handle_risk_uncertainty(self, deps: SubgraphDeps, assessment: RiskAssessment) -> None:
        """Route one immutable uncertain assessment to its own scoped human review."""
        if _model_requested_human_context(assessment):
            events = tuple(deps.event_log.events())
            if not risk_uncertainty_review_resolved(events, assessment, self.activity_profile):
                if _uncertain_risk_attempts(events, assessment) >= MAX_RISK_UNCERTAINTY_ATTEMPTS:
                    self._escalate_risk_uncertainty(assessment)
                else:
                    self._request_human_context(assessment)

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Run the canonical post-THY audit and expose its findings to Core's Gate node."""
        if state.run_id != self.run.id:
            raise ValueError("MiraSubgraph received a runtime state for a different Run")
        self._restore_persisted_preflight(deps)
        if self._preflight is None or self._flow is None:
            self.preflight(state, deps=deps)
        if self._preflight is None or self._flow is None:
            raise RuntimeError("MiraSubgraph evidence audit requires governance preflight")
        events = tuple(deps.event_log.events())
        plan_history, dag_board = self._board_evidence(deps)
        result = audit_with_reviewed_risk_classification(
            self._flow,
            self._preflight,
            run=self.run,
            profile=self.activity_profile,
            events=events,
            artifact_store=deps.artifact_store,
            frameworks=self.frameworks,
            audit_mode=AuditMode.IN_FLIGHT,
            audited_at=self.now(),
            current_risk=state.risk_profile,
            uncertainty_reviewed=risk_uncertainty_review_resolved(
                events, self._preflight.risk_assessment, self.activity_profile
            ),
            plan_history=plan_history,
            dag_board=dag_board,
        )
        if self.runtime_skill_manifest_verifier is not None:
            self.runtime_skill_manifest_verifier()
        _score_controls(self.run.id, result.audit_report)
        # Core's review node owns the policy decision; MIRA still owns the complete audit fact.
        self._flow.complete(
            policy_sha256=deps.gate.engine.policy_sha256,
            graph_definition_hash=self.graph_version(),
        )
        return state.model_copy(update={"findings": result.audit_findings})

    def audit_terminal(
        self,
        state: RuntimeState,
        *,
        deps: SubgraphDeps,
        terminal_audit_binding: TerminalAuditBinding | None = None,
    ) -> RuntimeState:
        """Run MIRA's existing evidence flow after a non-successful owner turn.

        Normal graph execution reaches :meth:`invoke` and then Core's Gate.  A failed, cancelled
        or recovered turn has no findings decision to ask for, but it still has to enter MIRA so
        the failure, cancellation or recovery facts are independently graded.  This method uses
        the same flow and persisted preflight as the ordinary graph; it never calls the Gate and
        never changes Run state.
        """
        if state.run_id != self.run.id:
            raise ValueError("MiraSubgraph received a runtime state for a different Run")
        self._restore_persisted_preflight(deps)
        if self._preflight is None or self._flow is None:
            self.preflight(state, deps=deps)
        if self._preflight is None or self._flow is None:
            raise RuntimeError("terminal MIRA audit requires governance preflight")
        events = tuple(deps.event_log.events())
        result = audit_with_reviewed_risk_classification(
            self._flow,
            self._preflight,
            run=self.run,
            profile=self.activity_profile,
            events=events,
            artifact_store=deps.artifact_store,
            frameworks=self.frameworks,
            audit_mode=AuditMode.FINAL,
            audited_at=self.now(),
            current_risk=state.risk_profile,
            uncertainty_reviewed=risk_uncertainty_review_resolved(
                events, self._preflight.risk_assessment, self.activity_profile
            ),
        )
        _score_controls(self.run.id, result.audit_report)
        self._flow.complete(
            policy_sha256=deps.gate.engine.policy_sha256,
            graph_definition_hash=self.graph_version(),
            terminal_audit_binding=terminal_audit_binding,
        )
        return state.model_copy(update={"findings": result.audit_findings})

    def _restore_persisted_preflight(self, deps: SubgraphDeps) -> None:
        """Hydrate a fresh graph instance from the latest matching preflight evidence."""
        if self._profile_resolver is not None:
            profile = self._profile_resolver()
            if profile.run_id != self.run.id:
                raise ValueError("MiraSubgraph profile resolver returned another Run's profile")
            self.activity_profile = profile
        profile_key = (
            self.activity_profile.id,
            self.activity_profile.version,
            sha256_text(canonical_json(self.activity_profile.to_json_dict())),
        )
        if self._preflight is not None and self._preflight_profile_key == profile_key:
            return

        events = tuple(deps.event_log.events())
        started = _latest_matching_audit_started(events, self.activity_profile)
        if started is None:
            return
        preflight = _preflight_from_events(
            events, started, self.activity_profile, self.run.id, self.packs
        )
        if preflight is None:
            raise RuntimeError("persisted MIRA preflight evidence is incomplete")
        flow = self._ensure_flow(deps)
        plan_history, dag_board = self._board_evidence(deps)
        flow.restore_preflight(
            MiraGraphInput(
                run=self.run,
                activity_profile=self.activity_profile,
                events=events,
                frameworks=self.frameworks,
                audit_mode=AuditMode.IN_FLIGHT,
                audited_at=started.ts,
                plan_history=plan_history,
                dag_board=dag_board,
            ),
            preflight,
        )
        self._preflight = preflight
        self._preflight_profile_key = profile_key

    def _board_evidence(self, deps: SubgraphDeps) -> tuple[tuple[PlanBoard, ...], DagBoard | None]:
        """Read owner-persisted board history before each MIRA phase."""
        plan_history = tuple(deps.plan_repository.history(self.run.id))
        plan_current = deps.plan_repository.get(self.run.id)
        if (plan_current is None) != (not plan_history):
            raise RuntimeError("MIRA plan evidence has no consistent current/history pair")
        if plan_history and plan_current != plan_history[-1]:
            raise RuntimeError("MIRA plan evidence current projection diverges from history")

        dag_history = tuple(deps.dag_repository.history(self.run.id))
        dag_current = deps.dag_repository.get(self.run.id)
        if (dag_current is None) != (not dag_history):
            raise RuntimeError("MIRA DAG evidence has no consistent current/history pair")
        if dag_history and dag_current != dag_history[-1]:
            raise RuntimeError("MIRA DAG evidence current projection diverges from history")
        return plan_history, dag_current

    def _ensure_flow(self, deps: SubgraphDeps) -> MiraAuditFlow:
        """Create the one canonical flow after Core supplies its per-Run dependencies."""
        if self._flow is None:
            runner = self.run_agent
            if runner is None and self.specs:
                context_factory = None
                if self.tool_registry is not None:

                    def context_factory(agent_id: str, task_id: str) -> ToolContext:
                        """Build the per-agent context over this Run's shared handles."""
                        return ToolContext(
                            run_id=self.run.id,
                            agent_id=agent_id,
                            task_id=task_id,
                            workspace=self.workspace,
                            event_log=deps.event_log,
                            gate=deps.gate,
                            artifact_store=deps.artifact_store,
                            risk_profile=_audit_tool_risk_profile(),
                            budget_guard=_tool_budget_guard(deps),
                        )

                runner = build_audit_agent_runner(
                    deps.event_log,
                    self.actor,
                    provider=self.provider,
                    before_model_selection=_model_budget_guard(deps),
                    before_model_call=_model_call_guard(deps),
                    record_model_usage=_model_usage_recorder(deps),
                    route_policy=self.route_policy,
                    request_ledger=deps.request_ledger,
                    evidence_reader=ArtifactStoreEvidenceReader(deps.artifact_store, self.run.id),
                    tool_registry=self.tool_registry,
                    tool_context_factory=context_factory,
                    citation_index=self.citation_index,
                    runtime_skill_catalog=self.runtime_skill_catalog,
                    runtime_skill_budget=self.runtime_skill_budget,
                    runtime_skill_names=self.runtime_skill_names,
                )
            risk_model_context = self._risk_model_context(deps)
            self._flow = MiraAuditFlow(
                MiraAuditFlowConfig(
                    orchestrator=MiraAuditOrchestrator(
                        self.packs,
                        self.actor,
                        risk_model_context=risk_model_context,
                    ),
                    event_log=deps.event_log,
                    artifact_store=deps.artifact_store,
                    specs=self.specs,
                    run_agent=runner,
                    verify_finding=self.verify_finding,
                    discovery=self.discovery,
                    before_agent_round=_model_budget_guard(deps),
                    runtime_skill_names=self.runtime_skill_names,
                )
            )
        return self._flow

    def _risk_model_context(self, deps: SubgraphDeps) -> RiskAssessmentModelContext | None:
        """Build MIRA's risk-call context from the per-Run Core audit handles."""
        active_provider = self.provider
        if active_provider is None:
            active_provider = _optional_provider(Role.MIRA, "classify", self.route_policy)
            if active_provider is None:
                return None
        return RiskAssessmentModelContext(
            provider=active_provider,
            event_log=deps.event_log,
            before_model_selection=_model_budget_guard(deps),
            before_model_call=_model_call_guard(deps),
            record_model_usage=_model_usage_recorder(deps),
            request_ledger=deps.request_ledger,
            route_policy=self.route_policy,
        )

    def _request_human_context(self, assessment: RiskAssessment) -> None:
        """Submit one uncertain model classification through the policy-gated control plane."""
        if self._control_plane is None:
            return
        current = self._control_plane.controller.current_state(self.run.id).state
        if current.condition is RunCondition.WAITING and current.wait_reason in {
            WaitReason.INFORMATION,
            WaitReason.APPROVAL,
        }:
            return
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=self.run.id,
            requester=self.actor,
            action_kind=ActionKind.REQUEST_INFORMATION,
            subject_kind="run",
            subject_id=self.run.id,
            purpose="Provide the missing context needed to classify inherent risk.",
            payload={
                "risk_assessment_id": assessment.id,
                "activity_profile_id": assessment.activity_profile_id,
                "activity_profile_version": assessment.activity_profile_version,
                "missing_information": list(assessment.missing_information),
            },
            idempotency_key=(
                f"mira-risk-information:{self.run.id}:{assessment.activity_profile_id}:"
                f"{assessment.activity_profile_version}"
            ),
        )
        self._control_plane.handle(intent)

    def _escalate_risk_uncertainty(self, assessment: RiskAssessment) -> None:
        """Ask a human to accept proceeding once MIRA's own uncertainty repeats past the bound.

        Mirrors ``RiskInterviewService._escalate_question_limit``'s pattern for its own bounded
        dead end (bug-hunt: preflight-information-park-is-unrecoverable). A plain ``resume`` can
        re-attempt MIRA's inherent-risk judgement, but nothing about a retry changes the profile
        or the model's input, so a judgement that keeps landing under
        ``RISK_CONFIDENCE_THRESHOLD`` can otherwise repeat forever on the same unanswerable
        ``wait_reason: information`` -- there is no ``activity_profile.questioned`` event for a
        human to answer. Ask a human to accept the Run proceeding with an uncertain
        classification after the first attempt instead of spending tokens repeating an unchanged
        request.
        """
        if self._control_plane is None:
            return
        current = self._control_plane.controller.current_state(self.run.id).state
        if current.condition is RunCondition.WAITING and current.wait_reason in {
            WaitReason.INFORMATION,
            WaitReason.APPROVAL,
        }:
            return
        intent = ActionIntent(
            id=new_id("intent"),
            run_id=self.run.id,
            requester=self.actor,
            action_kind=ActionKind.REQUEST_APPROVAL,
            subject_kind="run",
            subject_id=self.run.id,
            purpose=RISK_UNCERTAINTY_REVIEW_SUMMARY,
            payload={
                "risk_assessment_id": assessment.id,
                "activity_profile_id": assessment.activity_profile_id,
                "activity_profile_version": assessment.activity_profile_version,
                "max_attempts": MAX_RISK_UNCERTAINTY_ATTEMPTS,
                "reason": "risk_classification_uncertain",
            },
            idempotency_key=(
                f"mira-risk-uncertainty-limit:{self.run.id}:{assessment.id}:"
                f"{assessment.activity_profile_id}:{assessment.activity_profile_version}"
            ),
        )
        result = self._control_plane.handle(intent)
        if result.decision.decision is not Decision.REQUIRE_HUMAN_REVIEW:
            raise RuntimeError("risk-uncertainty escalation did not require human review")
        if result.state is None:
            self._control_plane.controller.park_for_review(
                self.run.id,
                result.decision,
                resume_from=RISK_UNCERTAINTY_RESUME_TARGET,
            )


def _latest_matching_audit_started(
    events: Sequence[Event], profile: ActivityProfile
) -> Event | None:
    """Return the latest audit start carrying the exact current profile snapshot."""
    expected = sha256_text(canonical_json(profile.to_json_dict()))
    for event in reversed(events):
        if event.type is not EventType.AUDIT_STARTED:
            continue
        value = event.payload.get("activity_profile")
        if not isinstance(value, dict):
            continue
        try:
            recorded = ActivityProfile.model_validate_json(canonical_json(value))
        except (TypeError, ValueError):
            continue
        if (
            recorded.run_id == profile.run_id
            and recorded.id == profile.id
            and recorded.version == profile.version
            and sha256_text(canonical_json(recorded.to_json_dict())) == expected
        ):
            return event
    return None


MAX_RISK_UNCERTAINTY_ATTEMPTS = 1
"""Escalate model uncertainty on one activity-profile version after its first attempt.

Nothing about a plain retry changes the profile, prompt or model route, so it cannot collect the
missing human fact implied by ``risk_classification``. The first low-confidence judgement is
therefore the useful evidence; :meth:`MiraSubgraph._escalate_risk_uncertainty` immediately asks a
human to accept proceeding instead of entering an unanswerable ``wait_reason: information`` and
paying for identical retries.
"""


def _model_requested_human_context(assessment: RiskAssessment) -> bool:
    """Return whether model uncertainty, rather than a call fallback, needs human context."""
    return assessment.justification.startswith(MODEL_UNCERTAINTY_JUSTIFICATION_PREFIX)


def _uncertain_risk_attempts(events: Sequence[Event], assessment: RiskAssessment) -> int:
    """Count MIRA's own uncertain inherent-risk judgements for this exact profile version.

    Includes the just-recorded ``assessment`` itself, so the bound is reached the moment the
    Nth uncertain judgement lands rather than requiring one further, wasted model call to notice.
    """
    count = 0
    for event in events:
        if event.type is not EventType.RISK_ASSESSMENT_RECORDED:
            continue
        payload = event.payload.get("risk_assessment")
        if not isinstance(payload, dict):
            continue
        try:
            recorded = RiskAssessment.model_validate_json(canonical_json(payload))
        except (TypeError, ValueError):
            continue
        if (
            recorded.activity_profile_id == assessment.activity_profile_id
            and recorded.activity_profile_version == assessment.activity_profile_version
            and _model_requested_human_context(recorded)
        ):
            count += 1
    return count


def _preflight_from_events(  # noqa: PLR0911, PLR0912  # fail-closed event validation
    events: Sequence[Event],
    started: Event,
    profile: ActivityProfile,
    run_id: str,
    packs: Sequence[ReviewedPack],
) -> MiraPreflightResult | None:
    """Rebuild MIRA's preflight result from its persisted, hash-chained evidence."""
    scoped: list[Event] = []
    for event in events:
        if event.seq <= started.seq:
            continue
        if event.type is EventType.AUDIT_STARTED:
            break
        scoped.append(event)

    risk_event = next(
        (event for event in reversed(scoped) if event.type is EventType.RISK_ASSESSMENT_RECORDED),
        None,
    )
    if risk_event is None:
        return None
    risk_value = risk_event.payload.get("risk_assessment")
    findings_value = risk_event.payload.get("preflight_findings")
    if (
        not isinstance(risk_value, dict)
        or not isinstance(findings_value, list)
        or any(not isinstance(value, dict) for value in findings_value)
    ):
        return None
    try:
        risk = RiskAssessment.model_validate_json(canonical_json(risk_value))
        findings = tuple(
            AuditFinding.model_validate_json(canonical_json(value)) for value in findings_value
        )
    except (TypeError, ValueError):
        return None
    if (
        risk.run_id != run_id
        or risk.activity_profile_id != profile.id
        or risk.activity_profile_version != profile.version
        or any(finding.run_id != run_id for finding in findings)
    ):
        return None

    bindings: list[PackBinding] = []
    for event in scoped:
        if event.type is not EventType.PACK_BINDING_RECORDED:
            continue
        value = event.payload.get("pack_binding")
        if not isinstance(value, dict):
            return None
        try:
            binding = PackBinding.model_validate_json(canonical_json(value))
        except (TypeError, ValueError):
            return None
        if (
            binding.activity_profile_id != profile.id
            or binding.activity_profile_version != profile.version
        ):
            continue
        bindings.append(binding)

    expected_packs = {pack.id: pack for pack in packs}
    if {binding.pack_id for binding in bindings} != set(expected_packs):
        return None
    if len(bindings) != len(expected_packs):
        return None
    if any(binding.pack_version != expected_packs[binding.pack_id].version for binding in bindings):
        return None
    return MiraPreflightResult(
        risk_assessment=risk,
        pack_bindings=tuple(bindings),
        audit_findings=findings,
    )


class _SubgraphIdentity:
    """A name and version standing in for a subgraph when only its identity is needed.

    ``ThySubgraph``/``MiraSubgraph`` need a ``Run`` to construct, but the composed graph hash
    depends only on their (Run-independent) ``name`` and ``graph_version()``. This descriptor lets
    :func:`composed_graph_definition_hash` reuse ``compose.graph_definition_hash`` without a Run.
    """

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self._version = version

    def graph_version(self) -> str:
        """Return the stored graph version."""
        return self._version

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Never called: this descriptor exists only to carry an identity for hashing."""
        del state, deps
        raise NotImplementedError("identity descriptor cannot execute a subgraph")


def composed_graph_definition_hash() -> str:
    """Return the stable hash of the production composed runtime graph (Run-independent).

    Equals ``compose.graph_definition_hash(ThySubgraph(...), MiraSubgraph(...))`` for any Run,
    because the composed hash folds in only the two child ``name``/``graph_version()`` pairs, which
    are the same for every Run. Suitable as the default provenance hash the API records at Run
    creation.
    """
    return _compose_graph_definition_hash(
        _SubgraphIdentity(_THY_SUBGRAPH_NAME, _thy_graph_definition_hash()),
        _SubgraphIdentity(_MIRA_SUBGRAPH_NAME, _mira_graph_definition_hash()),
    )


def _runtime_manifest_export_name(base_name: str, selection_id: str | None) -> str:
    """Resolve the immutable export name for one catalog state."""
    if selection_id is None:
        return base_name
    path = Path(base_name)
    return f"{path.stem}-{selection_id}{path.suffix}"


def _verify_persisted_runtime_skill_manifest(
    run_store: LocalRunStore,
    run_id: str,
    *,
    base_name: str,
    orchestrator: str,
    roots: Sequence[Path],
    events: Sequence[Event],
) -> None:
    """Verify one persisted catalog export against scoped append-only selection evidence."""
    evidence = [
        event
        for event in events
        if event.type is EventType.MODEL_SELECTED
        and event.payload.get("runtime_skill_orchestrator") == orchestrator
        and event.payload.get("runtime_skill_selection_id") is not None
    ]
    selection_id = evidence[-1].payload["runtime_skill_selection_id"] if evidence else None
    if selection_id is not None and not isinstance(selection_id, str):
        raise RuntimeError(f"{orchestrator} runtime catalog selection identity is invalid")
    export_name = _runtime_manifest_export_name(base_name, selection_id)
    payload = run_store.read_export(run_id, export_name)
    if payload is None:
        raise RuntimeError(f"missing {orchestrator} runtime catalog export: {export_name}")
    try:
        manifest = RuntimeSkillManifest.model_validate(payload)
    except ValueError as exc:
        raise RuntimeError(f"invalid {orchestrator} runtime catalog export: {export_name}") from exc
    verification_roots = tuple(roots) or tuple(Path(layer) for layer in manifest.layers)
    errors = verify_runtime_skill_manifest(
        manifest,
        verification_roots,
        authoritative_events=events,
    )
    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(f"{orchestrator} runtime catalog verification failed: {detail}")


def _build_runtime_skill_manifest_verifier(
    run_store: LocalRunStore,
    run_id: str,
    deps: SubgraphDeps,
    *,
    thy_catalog: RuntimeSkillCatalog | None,
    mira_catalog: RuntimeSkillCatalog | None,
    thy_roots: Sequence[Path],
    mira_roots: Sequence[Path],
) -> Callable[[], None] | None:
    """Build MIRA's independent verifier for the immutable THY/MIRA catalog exports."""
    if thy_catalog is None and mira_catalog is None:
        return None

    def verify_runtime_skill_manifests() -> None:
        """Verify persisted catalog states against the current append-only event history."""
        events = deps.event_log.events()
        if thy_catalog is not None:
            _verify_persisted_runtime_skill_manifest(
                run_store,
                run_id,
                base_name="thy-runtime-catalog.json",
                orchestrator="thy",
                roots=thy_roots,
                events=events,
            )
        if mira_catalog is not None:
            _verify_persisted_runtime_skill_manifest(
                run_store,
                run_id,
                base_name="mira-runtime-catalog.json",
                orchestrator="mira",
                roots=mira_roots,
                events=events,
            )

    return verify_runtime_skill_manifests


def build_runtime_graph_factory(  # noqa: PLR0915  # one composition root owns all graph dependencies
    run_store: LocalRunStore,
    *,
    gate_factory: Callable[[Id], Gate],
    artifact_store_factory: Callable[[Id], ArtifactStore],
    tool_manager: ToolManager,
    record_repository: RecordRepository,
    checkpoint_repository: CheckpointRepository,
    provider: LLMProvider | None = None,
    project_dir: Path | None = None,
    project_config: ProjectConfig | None = None,
    catalog: AgentCatalog | None = None,
    thy_runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    mira_runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    thy_skill_roots: Sequence[Path] = (),
    mira_skill_roots: Sequence[Path] = (),
    thy_skill_names: tuple[str, ...] = (),
    mira_skill_names: tuple[str, ...] = (),
    runtime_skill_budget: int = 32_000,
    specs: Sequence[AuditAgentSpec] | None = None,
    activity_profile_factory: Callable[[Run], ActivityProfile] | None = None,
    risk_interview: RiskInterviewService | None = None,
    verify_finding: FindingVerifier | None = None,
    discovery: DiscoveryLimits | None = None,
    mira_tool_registry: ToolRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    mira_citation_index: CitationIndex | None = None,
    metric: str = "accuracy",
    usage_ledgers: UsageLedgerRegistry | None = None,
    plan_repository: BoardRepository,
    dag_repository: DagRepository,
    owner_provider: Callable[[Id], RunHandle | None] | None = None,
    owner_binder: Callable[[RunHandle], AbstractContextManager[None]] | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    route_policy: ModelRoutePolicy | Callable[[Run], ModelRoutePolicy | None] | None = None,
) -> GraphFactory:
    """Build the production graph factory that composes THY -> MIRA -> Gate for each Run.

    Returned callable builds, per Run: the authoritative event log (``RunEventLog`` over the same
    ``LocalRunStore``), the Gate, the artifact store, the ``SubgraphDeps``, the
    ``StateCheckpointer`` and ``RunController``, and the composed graph via ``build_runtime_graph``.
    The catalog, the reviewed packs and the audit-agent specs are Run-independent and built once
    here.

    Args:
        run_store: The authoritative single-writer local Run store.
        gate_factory: Builds one Run's Gate (the composition's single ``review_findings`` caller;
            THY's plan gate uses the same Gate). Reuse the API's own ``gate_factory``.
        artifact_store_factory: Builds one Run's artifact store.
        tool_manager: The policy-gated tool manager shared across Runs.
        tool_registry: The THY tool registry. When supplied, delegated THY agents expose their
            spec allowlists through the shared Tool Manager and Gate.
        record_repository: The experiment/record repository shared across Runs.
        checkpoint_repository: Backs the per-Run ``StateCheckpointer``.
        provider: Overrides the router-resolved model for every model call -- THY's agents and
            MIRA's audit agents alike. ``None`` in production; tests inject a ``ScriptedProvider``
            so no network call happens.
        project_dir: The project directory Inspect loads context from. ``None`` leaves Inspect a
            bare phase-advance placeholder.
        project_config: Passed to the default activity-profile derivation (contributes nothing
            today; see :func:`default_activity_profile`).
        catalog: Overrides :func:`default_thy_catalog` (tests inject a controlled catalog).
        thy_runtime_skill_catalog: Optional preloaded THY runtime catalog. When omitted, THY
            loads a fresh catalog from ``thy_skill_roots`` for every graph build.
        mira_runtime_skill_catalog: Optional preloaded MIRA runtime catalog. When omitted, MIRA
            loads a fresh catalog from ``mira_skill_roots`` for every graph build.
        thy_skill_roots: Ordered low-to-high precedence roots for the THY runtime catalog.
        mira_skill_roots: Ordered low-to-high precedence roots for the MIRA runtime catalog.
        thy_skill_names: Runtime skill names selected for THY tasks that do not select their own.
        mira_skill_names: Runtime skill names selected for MIRA audit agents that do not select
            their own.
        runtime_skill_budget: Maximum UTF-8 bytes in one selected runtime-skill projection.
        specs: Overrides the shipped audit-agent roster (:func:`load_default_specs`). Tests inject
            a controlled roster; an empty sequence disables the fan-out.
        activity_profile_factory: Overrides :func:`default_activity_profile` (RISK-01 seam).
        risk_interview: Event-backed minimal intake and classification service. The API composition
            supplies it; omitted only for focused legacy graph tests.
        verify_finding: Optional adversarial judge for agent-authored findings. When omitted and
            audit specs are enabled, Core builds the routed MIRA verifier.
        discovery: Optional bound for repeated agent discovery rounds. The default is the bounded
            MIRA-03 limit.
        mira_tool_registry: The explicit MIRA-only registry. When omitted, Core builds the
            one-tool local registry over the packaged deterministic corpus.
        mira_citation_index: Optional hash-verified citation index for the regulatory-evidence
            specialist. When omitted and that spec is enabled, Core builds one from the same
            packaged local corpus used by the default MIRA registry.
        metric: The ranking metric Summarize uses.
        usage_ledgers: Where each Run's measured usage lives across the graphs built for it. The
            default keeps one registry per factory, which is what the composition root wants: it
            builds the factory once and the dispatcher rebuilds a Run's graph on every resume, so
            a ledger owned by the graph would restart at zero after each park and hand the budget
            decisions a Run that had spent nothing.
        plan_repository: Owner-composed durable goal/plan repository used as MIRA evidence.
        dag_repository: Owner-composed durable DAG repository used as MIRA evidence.
        owner_provider: Resolves the live process-owned Run writer for worker graph composition.
        owner_binder: Binds that writer while a terminal MIRA audit builds its owner-scoped Gate.
        before_model_request: Optional owner-bound hook invoked before each THY provider request.
        route_policy: The immutable session route snapshot, or a resolver returning one per Run.
    """
    resolved_ledgers = usage_ledgers if usage_ledgers is not None else UsageLedgerRegistry()
    resolved_catalog = catalog if catalog is not None else default_thy_catalog()
    declared_frameworks = project_config.governance.frameworks if project_config is not None else ()
    configured_specs = tuple(specs) if specs is not None else load_default_specs()
    resolved_specs = tuple(
        spec for spec in configured_specs if spec.framework in declared_frameworks
    )
    packs = load_default_packs()
    resolved_discovery = discovery if discovery is not None else DiscoveryLimits()
    needs_citation_index = any(spec.name == REGULATORY_EVIDENCE_AGENT for spec in resolved_specs)
    default_regulation_store = (
        load_default_regulation_store()
        if mira_tool_registry is None or (needs_citation_index and mira_citation_index is None)
        else None
    )
    if mira_tool_registry is None:
        if default_regulation_store is None:
            default_regulation_store = load_default_regulation_store()
        resolved_mira_tool_registry = build_mira_tool_registry(default_regulation_store)
    else:
        resolved_mira_tool_registry = mira_tool_registry
    resolved_citation_index = mira_citation_index
    if needs_citation_index and resolved_citation_index is None:
        if default_regulation_store is None:
            default_regulation_store = load_default_regulation_store()
        resolved_citation_index = CitationIndex.from_chunks(default_regulation_store.chunks())

    def _resolve_profile(run: Run) -> ActivityProfile:
        if activity_profile_factory is not None:
            return activity_profile_factory(run)
        return default_activity_profile(run, project_config=project_config)

    def _resolve_route_policy(run: Run) -> ModelRoutePolicy | None:
        """Resolve the same Run-bound route narrowing for every factory-owned model seam."""
        if route_policy is None:
            return run.model_route_policy
        if isinstance(route_policy, ModelRoutePolicy):
            return route_policy
        return route_policy(run)

    def _build(run: Run) -> CompiledStateGraph:
        owner = owner_provider(run.id) if owner_provider is not None else None
        if owner is not None:
            owner.assert_live()
        artifact_store = artifact_store_factory(run.id)
        checkpoint_store = checkpoint_repository
        if owner is not None:
            artifact_store = OwnedArtifactStore(artifact_store, owner)
            checkpoint_store = OwnedCheckpointRepository(checkpoint_store, owner)
        run_event_log = RunEventLog(
            run_store,
            run.id,
            invariant_store=artifact_store,
            writer=owner.append if owner is not None else None,
        )
        resolved_route_policy = _resolve_route_policy(run)
        resolved_thy_skills = (
            thy_runtime_skill_catalog
            if thy_runtime_skill_catalog is not None
            else load_runtime_skill_catalog(tuple(thy_skill_roots), orchestrator="thy")
            if thy_skill_roots
            else None
        )
        resolved_mira_skills = (
            mira_runtime_skill_catalog
            if mira_runtime_skill_catalog is not None
            else load_mira_skill_catalog(tuple(mira_skill_roots))
            if mira_skill_roots
            else None
        )

        def publish_manifest(name: str, manifest: RuntimeSkillManifest) -> None:
            """Publish one immutable catalog state through the Run export seam."""
            export_name = _runtime_manifest_export_name(name, manifest.selection_id)
            run_store.create_export(run.id, export_name, manifest.model_dump(mode="json"))

        deps = SubgraphDeps(
            event_log=run_event_log,
            gate=gate_factory(run.id),
            artifact_store=artifact_store,
            tool_manager=tool_manager,
            record_repository=record_repository,
            # The Run's own ledger, not this graph's: a park rebuilds the graph, and a ledger
            # that started over would show the Policy Engine a Run that had spent nothing.
            usage_ledger=resolved_ledgers.for_run(run.id),
            plan_repository=plan_repository,
            dag_repository=dag_repository,
            request_ledger=RequestLedger(run_event_log),
            before_model_request=before_model_request,
        )
        if resolved_thy_skills is not None:
            resolved_thy_skills.set_manifest_sink(
                partial(publish_manifest, "thy-runtime-catalog.json")
            )
            publish_manifest("thy-runtime-catalog.json", resolved_thy_skills.manifest())
        if resolved_mira_skills is not None:
            resolved_mira_skills.set_manifest_sink(
                partial(publish_manifest, "mira-runtime-catalog.json")
            )
            publish_manifest("mira-runtime-catalog.json", resolved_mira_skills.manifest())
        runtime_skill_manifest_verifier = _build_runtime_skill_manifest_verifier(
            run_store,
            run.id,
            deps,
            thy_catalog=resolved_thy_skills,
            mira_catalog=resolved_mira_skills,
            thy_roots=thy_skill_roots,
            mira_roots=mira_skill_roots,
        )
        resolved_verifier = verify_finding
        if resolved_verifier is None and resolved_specs:
            verifier_provider = (
                provider
                or provider_for(
                    Role.MIRA,
                    "audit_judgement",
                    route_policy=resolved_route_policy,
                )[0]
            )
            resolved_verifier = build_finding_verifier(
                verifier_provider,
                event_log=deps.event_log,
                before_model_selection=_model_budget_guard(deps),
                before_model_call=_model_call_guard(deps),
                record_model_usage=_model_usage_recorder(deps),
                request_ledger=deps.request_ledger,
                route_policy=resolved_route_policy,
            )
        thy = ThySubgraph(
            run,
            resolved_catalog,
            provider=provider,
            project_dir=project_dir,
            metric=metric,
            tool_registry=tool_registry,
            route_policy=resolved_route_policy,
            runtime_skill_catalog=resolved_thy_skills,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=thy_skill_names,
        )
        initial_profile = _resolve_profile(run)
        mira = MiraSubgraph(
            run,
            initial_profile,
            packs,
            Actor.system(),
            frameworks=declared_frameworks,
            specs=resolved_specs,
            provider=provider,
            profile_resolver=(
                lambda: (
                    risk_interview.current_profile(run, initial_profile)
                    if risk_interview is not None
                    else initial_profile
                )
            ),
            verify_finding=resolved_verifier,
            discovery=resolved_discovery,
            tool_registry=resolved_mira_tool_registry,
            citation_index=resolved_citation_index,
            workspace=project_dir,
            route_policy=resolved_route_policy,
            control_plane=MiraControlPlane(run_store, deps.gate.engine),
            runtime_skill_catalog=resolved_mira_skills,
            runtime_skill_budget=runtime_skill_budget,
            runtime_skill_names=mira_skill_names,
            runtime_skill_manifest_verifier=runtime_skill_manifest_verifier,
        )
        interview_node = (
            partial(
                _run_interview,
                risk_interview,
                run,
                initial_profile,
                provider,
                project_dir,
                route_policy=resolved_route_policy,
            )
            if risk_interview is not None
            else None
        )
        classification_node = (
            partial(
                _run_risk_classification,
                risk_interview,
                run,
                initial_profile,
                provider,
                resolved_route_policy,
            )
            if risk_interview is not None
            else None
        )
        execution_gate_node = _authorize_execution if risk_interview is not None else None
        return build_runtime_graph(
            thy,
            mira,
            interview=interview_node,
            preflight=lambda state, deps: mira.preflight(state, deps=deps),
            classify=classification_node,
            execution_gate=execution_gate_node,
            checkpointer=StateCheckpointer(checkpoint_store),
            deps=deps,
            controller=RunController(
                run_store,
                writer=owner.append_transition if owner is not None else None,
            ),
            assurance=partial(write_run_assurance, run, run_store, owner=owner),
        )

    def _audit_terminal(
        owner: RunHandle,
        work: WorkItem,
        result: WorkResult,
        terminal_audit_binding: TerminalAuditBinding,
    ) -> None:
        """Audit a settled non-successful owner turn with the same MIRA composition."""
        if terminal_audit_binding.run_id != owner.run_id:
            raise LifecycleError("terminal audit binding targets another Run")
        if terminal_audit_binding.work_id != work.work_id:
            raise LifecycleError("terminal audit binding targets another work item")
        if terminal_audit_binding.outcome_kind is not result.kind:
            raise LifecycleError("terminal audit binding does not match the settled outcome")
        expected_result_sha256 = sha256_text(canonical_json(result.to_json_dict()))
        if terminal_audit_binding.result_sha256 != expected_result_sha256:
            raise LifecycleError("terminal audit binding does not match the settled result")
        matching_turn = None
        for event in owner.events():
            if event.type is not EventType.TURN_ENDED:
                continue
            try:
                turn = TurnEnded.model_validate(event.payload)
            except (TypeError, ValueError):
                continue
            if (
                turn.turn_id == terminal_audit_binding.turn_id
                and turn.run_id == owner.run_id
                and work.work_id in turn.work_ids
            ):
                matching_turn = turn
                break
        if matching_turn is None or (
            matching_turn.end_reason is not terminal_audit_binding.end_reason
        ):
            raise LifecycleError("terminal audit binding does not match a durable turn closure")
        owner.assert_live()
        event_log = RunEventLog(
            run_store,
            owner.run_id,
            writer=owner.append,
        )
        terminal_run = run_store.get(owner.run_id)
        terminal_route_policy = _resolve_route_policy(terminal_run)
        artifact_store = OwnedArtifactStore(artifact_store_factory(owner.run_id), owner)
        terminal_deps = SubgraphDeps(
            event_log=event_log,
            gate=gate_factory(owner.run_id),
            artifact_store=artifact_store,
            tool_manager=tool_manager,
            record_repository=record_repository,
            usage_ledger=resolved_ledgers.for_run(owner.run_id),
            request_ledger=RequestLedger(event_log),
        )
        terminal_verifier = verify_finding
        if terminal_verifier is None and resolved_specs:
            verifier_provider = (
                provider
                or provider_for(
                    Role.MIRA,
                    "audit_judgement",
                    route_policy=terminal_route_policy,
                )[0]
            )
            terminal_verifier = build_finding_verifier(
                verifier_provider,
                event_log=terminal_deps.event_log,
                before_model_selection=_model_budget_guard(terminal_deps),
                before_model_call=_model_call_guard(terminal_deps),
                record_model_usage=_model_usage_recorder(terminal_deps),
                request_ledger=terminal_deps.request_ledger,
                route_policy=terminal_route_policy,
            )
        terminal_mira = MiraSubgraph(
            run=terminal_run,
            activity_profile=_resolve_profile(terminal_run),
            packs=packs,
            actor=Actor.system(),
            frameworks=declared_frameworks,
            specs=resolved_specs,
            provider=provider,
            profile_resolver=(
                lambda: (
                    risk_interview.current_profile(
                        run_store.get(owner.run_id),
                        _resolve_profile(run_store.get(owner.run_id)),
                    )
                    if risk_interview is not None
                    else _resolve_profile(run_store.get(owner.run_id))
                )
            ),
            verify_finding=terminal_verifier,
            discovery=resolved_discovery,
            tool_registry=resolved_mira_tool_registry,
            citation_index=resolved_citation_index,
            workspace=project_dir,
            route_policy=terminal_route_policy,
        )
        terminal_mira.audit_terminal(
            initial_runtime_state(terminal_run),
            deps=terminal_deps,
            terminal_audit_binding=terminal_audit_binding,
        )

    def _bound_terminal_audit(
        owner: RunHandle,
        work: WorkItem,
        result: WorkResult,
        terminal_audit_binding: TerminalAuditBinding,
    ) -> None:
        """Bind the owner before constructing a Gate-backed terminal MIRA flow."""
        if owner_binder is None:
            _audit_terminal(owner, work, result, terminal_audit_binding)
            return
        with owner_binder(owner):
            _audit_terminal(owner, work, result, terminal_audit_binding)

    return _LifecycleGraphFactory(_build, _bound_terminal_audit)


def write_run_assurance(
    run: Run,
    run_store: LocalRunStore,
    state: RuntimeState,
    deps: SubgraphDeps,
    *,
    owner: RunHandle | None = None,
) -> None:
    """Persist the Run's assurance bundle once, at completion (ASSUR-01).

    Producing the bundle here -- and only here -- keeps every read surface free of side effects:
    a reader loads this export and re-verifies it, it never rebuilds it. A Run that recorded no
    audit report or no findings decision has nothing to assure, and writes nothing.
    """
    del state
    if owner is not None:
        owner.assert_live()
    events = deps.event_log.events()
    bundle = assemble_run_assurance(
        run.id,
        events=events,
        run=run,
        store=deps.artifact_store,
    )
    if bundle is None:
        return
    run_store.write_export(run.id, ASSURANCE_BUNDLE_EXPORT, bundle.to_json_dict())


def _run_interview(
    interview: RiskInterviewService,
    run: Run,
    profile: ActivityProfile,
    provider: LLMProvider | None,
    project_dir: Path | None,
    state: RuntimeState,
    deps: SubgraphDeps,
    *,
    route_policy: ModelRoutePolicy | None,
) -> RuntimeState:
    """Persist and request the next activity fact without changing graph identity."""
    active_provider = provider
    if active_provider is None:
        # The interview's static question remains available when model configuration is absent.
        active_provider = _optional_provider(Role.AGENT, "extract", route_policy)
    interview.begin(
        run,
        profile,
        context_document=_project_context_text(project_dir),
        provider=active_provider,
        event_log=deps.event_log,
        before_model_selection=_model_budget_guard(deps),
        before_model_call=_model_call_guard(deps),
        record_model_usage=_model_usage_recorder(deps),
        request_ledger=deps.request_ledger,
        route_policy=route_policy,
    )
    return state


def _run_risk_classification(
    interview: RiskInterviewService,
    run: Run,
    profile: ActivityProfile,
    provider: LLMProvider | None,
    route_policy: ModelRoutePolicy | None,
    state: RuntimeState,
    deps: SubgraphDeps,
) -> RuntimeState:
    """Record one risk classification before THY can expose any tool to an agent."""
    active_provider = (
        provider
        or provider_for(
            Role.AGENT,
            "classify",
            route_policy=route_policy,
        )[0]
    )
    risk = interview.classify(
        run,
        profile,
        deps.event_log,
        active_provider,
        before_model_selection=_model_budget_guard(deps),
        before_model_call=_model_call_guard(deps),
        record_model_usage=_model_usage_recorder(deps),
        route_policy=route_policy,
        request_ledger=deps.request_ledger,
    )
    return state.model_copy(update={"risk_profile": risk})


def _authorize_execution(state: RuntimeState, deps: SubgraphDeps) -> RuntimeState:
    """Translate recorded risk facts into one Gate-authorised execution contract."""
    if state.risk_profile is None:
        raise RuntimeError("risk classification is required before execution can be authorised")
    decision = deps.gate.authorize_execution(state.risk_profile, deps.tool_manager.capabilities())
    return state.model_copy(
        update={
            "execution_constraints": decision.execution_constraints,
            "execution_decision": decision,
        }
    )


def _model_budget_guard(deps: SubgraphDeps) -> Callable[[], None]:
    """Check the run budget before the router selects a model for another request."""
    return _model_budget_guard_for(deps.gate, deps.usage_ledger)


def _model_budget_guard_for(gate: Gate, usage_ledger: UsageLedger) -> Callable[[], None]:
    """Build the model-selection budget guard for a log, Gate and Run ledger."""

    def guard() -> None:
        usage = usage_ledger.snapshot()
        decision = gate.check_budget(usage, summary="model budget", cost_so_far=usage)
        if decision.decision not in (Decision.PASS, Decision.WARNING):
            raise RuntimeError(
                f"model call blocked by budget decision {decision.id}: {decision.reason}"
            )

    return guard


def _model_call_guard(deps: SubgraphDeps) -> Callable[[ModelChoice], None]:
    """Check the routed model before the provider is invoked."""
    return _model_call_guard_for(deps.event_log, deps.gate, deps.usage_ledger)


def _model_call_guard_for(
    event_log: EventLog, gate: Gate, usage_ledger: UsageLedger
) -> Callable[[ModelChoice], None]:
    """Build the post-routing Gate guard for a log, Gate and Run ledger."""

    def guard(choice: ModelChoice) -> None:
        model = choice.model
        role = choice.role.value
        tier = choice.tier_applied.value
        usage = usage_ledger.snapshot()
        # The run-wide ceiling was decided and recorded before routing. A role-scoped ceiling
        # is asked -- and its decision recorded -- only when the policy declares one; without a
        # rule for the role the engine can only answer PASS, and an uninformative event per
        # model call is one more line every reader of a long Run must parse.
        if gate.engine.declares_budget_scope(role):
            scoped = gate.check_budget(
                usage,
                scope=role,
                summary=f"{role} model budget",
                cost_so_far=usage,
            )
            if scoped.decision not in (Decision.PASS, Decision.WARNING):
                raise RuntimeError(
                    f"model call blocked by budget decision {scoped.id}: {scoped.reason}"
                )
        decision = gate.check_model(
            subject_id=event_log.run_id,
            role=role,
            model=model,
            tier=tier,
            summary=f"{role} model {model}",
            cost_so_far=usage,
        )
        if decision.decision not in (Decision.PASS, Decision.WARNING):
            raise RuntimeError(
                f"model call blocked by policy decision {decision.id}: {decision.reason}"
            )

    return guard


def _model_usage_recorder(deps: SubgraphDeps) -> Callable[[LLMResponse], None]:
    """Charge measured provider usage into Core's run-scoped ledger."""
    return _model_usage_recorder_for(deps.usage_ledger)


def _model_usage_recorder_for(usage_ledger: UsageLedger) -> Callable[[LLMResponse], None]:
    """Build a measured-usage recorder for one Run ledger."""

    def record(response: LLMResponse) -> None:
        usage_ledger.charge(
            requests=1,
            tokens=int(response.input_tokens) + int(response.output_tokens),
            cost_usd=response.cost_usd,
        )

    return record


def build_risk_interview_model_context(
    event_log: EventLog,
    gate: Gate,
    usage_ledger: UsageLedger,
    *,
    provider: LLMProvider | None = None,
    route_policy: ModelRoutePolicy | None = None,
    request_ledger: RequestLedger | None = None,
) -> RiskInterviewModelContext | None:
    """Build interview model handles with the same guards as Core classification."""
    active_provider = provider
    if active_provider is None:
        active_provider = _optional_provider(Role.AGENT, "extract", route_policy)
        if active_provider is None:
            return None
    return RiskInterviewModelContext(
        provider=active_provider,
        event_log=event_log,
        before_model_selection=_model_budget_guard_for(gate, usage_ledger),
        before_model_call=_model_call_guard_for(event_log, gate, usage_ledger),
        record_model_usage=_model_usage_recorder_for(usage_ledger),
        request_ledger=request_ledger or RequestLedger(event_log),
        route_policy=route_policy,
    )


_WORKSPACES_DIR = Path(".thymira") / "runtime" / "workspaces"


def _run_workspace(project_dir: Path | None, run_id: str) -> Path:
    """This Run's own scratch directory, isolated from every other Run against the project.

    Bug-hunt H2: `ToolContext.workspace` used to be the project directory itself, shared by every
    Run -- `run_python` leaves a `.thymira/tool_<id>.py` per call, `write_file` leaves scripts and
    reports at the root, and two Runs racing on the same filename silently clobbered each other
    with no event marking it. Giving each Run its own directory under
    `.thymira/runtime/workspaces/<run_id>` (a sibling of the already per-Run `artifacts/<run_id>`)
    makes that collision structurally impossible instead of merely unlikely.

    The project's declared context still comes from `project_dir` itself
    (`_project_context_text`); a registered dataset's *raw file* does not reach this workspace on
    its own -- `_register_declared` (`thymira.thy.nodes.inspect`) reads straight from
    `project_dir` and the name-based tools (`profile_dataset`/`query_sql`/`run_experiment`) read
    straight from the `ArtifactStore`, so neither ever needed this directory -- but
    `read_file`/`run_python`, the only tools a `coding` step has, resolve every path under it and
    have no other way to reach the file at all. See `_stage_declared_datasets`, called right after
    this workspace is built. Only the *scratch* area
    a coding step writes new files into is otherwise isolated, so an agent's own outputs from an
    earlier Run are never mistaken for the project's own files. A caller with no `project_dir` (a
    lightweight graph with no real project) keeps the prior fallback: a workspace of the process's
    own current directory.
    """
    if project_dir is None:
        return Path()
    workspace = project_dir / _WORKSPACES_DIR / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _stage_declared_datasets(project_dir: Path | None, workspace: Path) -> None:
    """Copy every dataset the project declares into the Run's own workspace, same relative path.

    Bug-hunt follow-up, found live against `examples/credit-risk` running a real EDA prompt right
    after the deferred-tool-resume fix let the `data` agent finish cleanly: the `coding` step's
    every guess at a dataset path (`data/german_credit.csv`, `datasets/german_credit.csv`,
    `german_credit.csv`) failed with `FileNotFoundError`. H2 (`_run_workspace`) isolated the
    workspace from `project_dir` to stop write collisions, but nothing had ever made a registered
    dataset's raw content reachable from inside it -- `read_file`/`run_python` (the only tools a
    `coding` step has) resolve paths under the workspace alone, and `coding`'s own allowlist has no
    name-based dataset tool (`profile_dataset`, `query_sql`) to fall back on.

    Copying at the project's own declared relative path (rather than, say, the artifact store's
    `datasets/<name>.csv` naming) means `pd.read_csv('data/german_credit.csv')` -- the natural
    thing to write for a project laid out that way -- resolves without the model needing to be
    told a different convention. A destination that already exists is left alone: a `coding` step
    may have legitimately modified its own copy on an earlier pass of the same Run, and a later
    pass (a resume) must not silently overwrite that with the pristine original.

    Best-effort and silent on a missing or unreadable source: `_register_declared`
    (`thymira.thy.nodes.inspect`), which runs inside `ThyGraph` proper, is the authoritative
    registration and is what reports a bad dataset declaration as `ThyState.error`.
    """
    if project_dir is None:
        return
    context = load_project_context(project_dir)
    if context.config is None:
        return
    for dataset in context.config.datasets:
        parts = PurePosixPath(dataset.path.replace("\\", "/")).parts
        source = project_dir.joinpath(*parts)
        destination = workspace.joinpath(*parts)
        if destination.exists() or not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _declared_dataset_workspace_paths(project_dir: Path | None) -> tuple[tuple[str, str], ...]:
    """Return logical dataset names paired with their canonical workspace-relative paths.

    Core stages every declared input under its original project-relative path. The same mapping
    belongs in the coding runtime context so an artifact-store key can never be mistaken for a
    path usable by `read_file` or `run_python`.
    """
    if project_dir is None:
        return ()
    context = load_project_context(project_dir)
    if context.config is None:
        return ()
    return tuple(
        (dataset.name, PurePosixPath(dataset.path.replace("\\", "/")).as_posix())
        for dataset in context.config.datasets
    )


def _optional_provider(
    role: Role, task: str, route_policy: ModelRoutePolicy | None
) -> LLMProvider | None:
    """Resolve a provider for an optional model-assistance seam, or ``None`` when there is none.

    An unconfigured tier is not a route: nothing was selected, so the session snapshot has nothing
    to authorize and the caller's documented fallback applies (the interview's static question,
    an unclassified risk profile). A route that *is* configured but absent from the snapshot still
    fails closed through :func:`enforce_model_route` inside ``provider_for``.
    """
    if choose(role, task).model == UNCONFIGURED_MODEL:
        return None
    try:
        return provider_for(role, task, route_policy=route_policy)[0]
    except LLMConfigurationError:
        return None


def _project_context_text(project_dir: Path | None) -> str | None:
    """The project's `.thymira/context.md`, if it declares one (bug-hunt H4).

    Loaded directly here rather than read off `ThyState.project_context`: this `ToolContext` is
    built once, before ThyGraph's own Inspect node runs and populates that field, so reading it
    here would see whatever it held on the *previous* pass, or nothing on the first one. Every
    delegated step already gets one snapshot per call through this same `ToolContext`
    (`runtime_context_text`), so loading it directly is what actually gets it to Execute's
    sub-agents instead of leaving it sat in `ThyState` for a step no one reads it from.
    """
    if project_dir is None:
        return None
    return load_project_context(project_dir).context_md


def _tool_budget_guard(deps: SubgraphDeps) -> Callable[[], BudgetRefusal | None]:
    """Check and charge one tool attempt before its implementation executes.

    The refusal carries the budget decision it was taken under, so the ``tool.denied`` the
    manager records names the decision that refused the call and MIRA's A6 can explain it.
    """

    def guard() -> BudgetRefusal | None:
        current = deps.usage_ledger.snapshot()
        proposed = dict(current)
        proposed["tool_calls"] = int(current.get("tool_calls") or 0) + 1
        decision = deps.gate.check_budget(
            proposed,
            summary="tool budget",
            cost_so_far=current,
        )
        if decision.decision not in (Decision.PASS, Decision.WARNING):
            return BudgetRefusal(
                reason=f"tool call blocked by budget decision {decision.id}: {decision.reason}",
                decision_id=decision.id,
            )
        deps.usage_ledger.charge_tool()
        return None

    return guard


__all__ = [
    "MiraSubgraph",
    "ThyExecutionError",
    "ThySubgraph",
    "build_audit_agent_runner",
    "build_risk_interview_model_context",
    "build_runtime_graph_factory",
    "composed_graph_definition_hash",
    "default_activity_profile",
    "default_thy_catalog",
    "write_run_assurance",
]
