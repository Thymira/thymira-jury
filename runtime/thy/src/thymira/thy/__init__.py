"""THY: the production / data-science orchestrator.

ThyGraph (Inspect -> Plan -> Execute -> Summarize) coordinates the Data, Coding/Execution and
Experiment agents in the MVP.

Owner (MVP roadmap): P2 (THY / Agents). All four ThyGraph nodes are real (THY-09..THY-13):
`load_project_context` (THY-10) seeds Inspect; `plan_node` (THY-11) gates THY's own plan through
the Policy Engine; `execute_node` (THY-09/THY-12) dispatches `ThyState.plan` through
`Delegator`/`AgentRunner` in dependency order; `summarize_node`/`compare_experiments` (THY-13)
produce the `Recommendation` and its report artifact. Each stays a bare phase-advance placeholder
when its own build-time dependency (`project_dir`, `gate`, `artifact_store`) is omitted from
`build_thy_graph`/`run_thy`.
"""

from thymira.thy.agents import (
    CODING_AGENT_SPEC,
    DATA_AGENT_SPEC,
    DATA_QUALITY_AGENT_SPEC,
    EXPERIMENT_AGENT_SPEC,
    ML_AGENT_SPEC,
    ML_TUNING_HELPER_SPEC,
    RESEARCH_AGENT_SPEC,
    STATISTICS_AGENT_SPEC,
    VISUALIZATION_AGENT_SPEC,
    Citation,
    Claim,
    CodeResult,
    DataProfile,
    DataQualityReport,
    ExperimentResult,
    MLResult,
    Plot,
    PlotResult,
    ResearchResult,
    StatsResult,
    TuningResult,
    coding_agent_catalog,
    data_agent_catalog,
    data_quality_agent_catalog,
    experiment_agent_catalog,
    full_agent_catalog,
    ml_agent_catalog,
    research_agent_catalog,
    run_ml_agent,
    statistics_agent_catalog,
    visualization_agent_catalog,
)
from thymira.thy.budget import (
    DEFAULT_CONTEXT_WINDOW,
    BudgetOutcome,
    ContextBudget,
    MeasurementBaseline,
    TokenMeasurement,
    estimate_prompt_tokens,
)
from thymira.thy.compaction import (
    CompactionContext,
    CompactionSummary,
    ContextCheckpoint,
    build_source_checkpoint,
    compact_surface,
    estimate_events_tokens,
    estimate_tokens,
    prune_head_marker_tail,
    recover_checkpoint,
)
from thymira.thy.context import ProjectContext, apply_agents_overlay, load_project_context
from thymira.thy.experiments import experiment_from_result
from thymira.thy.graph import build_thy_graph, graph_definition_hash, run_thy
from thymira.thy.models import (
    DEFAULT_MAX_REOPENS,
    THY_PHASE_ORDER,
    AgentTask,
    ArtifactRequirement,
    PlanOutput,
    Recommendation,
    RegisteredDataset,
    ReworkSignal,
    SynthesisNarrative,
    ThyAgentKind,
    ThyInput,
    ThyOutput,
    ThyPhase,
    ThyProgress,
    ThyState,
)
from thymira.thy.nodes.execute import execute_node
from thymira.thy.nodes.inspect import inspect_node
from thymira.thy.nodes.plan import plan_node
from thymira.thy.nodes.summarize import compare_experiments, summarize_node
from thymira.thy.recipes import Recipe, load_recipe, run_recipe

__all__ = [
    "CODING_AGENT_SPEC",
    "DATA_AGENT_SPEC",
    "DATA_QUALITY_AGENT_SPEC",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_REOPENS",
    "EXPERIMENT_AGENT_SPEC",
    "ML_AGENT_SPEC",
    "ML_TUNING_HELPER_SPEC",
    "RESEARCH_AGENT_SPEC",
    "STATISTICS_AGENT_SPEC",
    "THY_PHASE_ORDER",
    "VISUALIZATION_AGENT_SPEC",
    "AgentTask",
    "ArtifactRequirement",
    "BudgetOutcome",
    "Citation",
    "Claim",
    "CodeResult",
    "CompactionContext",
    "CompactionSummary",
    "ContextBudget",
    "ContextCheckpoint",
    "DataProfile",
    "DataQualityReport",
    "ExperimentResult",
    "MLResult",
    "MeasurementBaseline",
    "PlanOutput",
    "Plot",
    "PlotResult",
    "ProjectContext",
    "Recipe",
    "Recommendation",
    "RegisteredDataset",
    "ResearchResult",
    "ReworkSignal",
    "StatsResult",
    "SynthesisNarrative",
    "ThyAgentKind",
    "ThyInput",
    "ThyOutput",
    "ThyPhase",
    "ThyProgress",
    "ThyState",
    "TokenMeasurement",
    "TuningResult",
    "apply_agents_overlay",
    "build_source_checkpoint",
    "build_thy_graph",
    "coding_agent_catalog",
    "compact_surface",
    "compare_experiments",
    "data_agent_catalog",
    "data_quality_agent_catalog",
    "estimate_events_tokens",
    "estimate_prompt_tokens",
    "estimate_tokens",
    "execute_node",
    "experiment_agent_catalog",
    "experiment_from_result",
    "full_agent_catalog",
    "graph_definition_hash",
    "inspect_node",
    "load_project_context",
    "load_recipe",
    "ml_agent_catalog",
    "plan_node",
    "prune_head_marker_tail",
    "recover_checkpoint",
    "research_agent_catalog",
    "run_ml_agent",
    "run_recipe",
    "run_thy",
    "statistics_agent_catalog",
    "summarize_node",
]
