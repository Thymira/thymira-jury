"""THY's own specialist-agent declarations: `AgentSpec` + output schema + prompt, per agent.

Owner (MVP roadmap): P2 (THY / Agents). The Data agent (`thymira.thy.agents.data`, THY-14), the
Coding/Execution agent (`thymira.thy.agents.coding`, THY-15) and the Experiment agent
(`thymira.thy.agents.experiment`, THY-16) all follow the same drop-in shape on top of the
THY-01/03/04 framework (`thymira.agents`) -- the MVP roster Execute can dispatch to.

The Statistics agent (`thymira.thy.agents.statistics`, THY-18), the Data Quality agent
(`thymira.thy.agents.data_quality`, THY-20) and the Visualization agent
(`thymira.thy.agents.visualization`, THY-21) follow the same shape and, since THY-32 opened
`ThyAgentKind` to them, are dispatchable by `execute_node` too -- provided the caller built the
graph on `full_agent_catalog()` (or another catalog holding them) rather than the MVP-only roster.

The ML agent (`thymira.thy.agents.ml`, THY-19) is also FINAL-tier, but is not a bare drop-in spec:
it is the first to delegate to a helper sub-agent (`ML_TUNING_HELPER_SPEC`, `run_ml_agent`), so it
ships a second `AgentSpec` and an orchestration wrapper alongside the usual spec + schema + prompt.

The Research agent (`thymira.thy.agents.research`, THY-22) is another FINAL-tier drop-in spec: it
gathers evidence through the `search`/`kb_query` tools (owned by P3/P1, reached through the same
tool bridge) and returns a `ResearchResult` whose every `Claim` carries at least one `Citation`.

`full_agent_catalog()` merges every specialist catalog above into one roster for callers
(`build_thy_graph`/`run_thy`) that want the fuller set. It holds `ml-agent` and `research-agent`
too, but those stay unreachable from a plan until they gain their own `ThyAgentKind` member the
same way THY-32 gave the other three theirs -- being in the catalog is necessary, not sufficient.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents import AgentCatalog
from thymira.thy.agents.coding import CODING_AGENT_SPEC, CodeResult, coding_agent_catalog
from thymira.thy.agents.data import DATA_AGENT_SPEC, DataProfile, data_agent_catalog
from thymira.thy.agents.data_quality import (
    DATA_QUALITY_AGENT_SPEC,
    DataQualityReport,
    data_quality_agent_catalog,
)
from thymira.thy.agents.experiment import (
    EXPERIMENT_AGENT_SPEC,
    ExperimentResult,
    experiment_agent_catalog,
)
from thymira.thy.agents.ml import (
    ML_AGENT_SPEC,
    ML_TUNING_HELPER_SPEC,
    MLResult,
    TuningResult,
    ml_agent_catalog,
    run_ml_agent,
)
from thymira.thy.agents.research import (
    RESEARCH_AGENT_SPEC,
    Citation,
    Claim,
    ResearchResult,
    research_agent_catalog,
)
from thymira.thy.agents.statistics import (
    STATISTICS_AGENT_SPEC,
    StatsResult,
    statistics_agent_catalog,
)
from thymira.thy.agents.visualization import (
    VISUALIZATION_AGENT_SPEC,
    Plot,
    PlotResult,
    visualization_agent_catalog,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from thymira.agents import AgentSpec


def full_agent_catalog() -> AgentCatalog:
    """Merge every specialist `AgentCatalog` -- MVP and shipped FINAL -- into one roster (THY-32).

    Builds a fresh `AgentCatalog` holding every spec from `data`/`coding`/`experiment` (MVP) and
    `statistics`/`data-quality`/`visualization`/`ml`/`research` (FINAL), with each spec's resolved
    system prompt and output schema carried over. This is the catalog a caller passes to
    `build_thy_graph`/`run_thy` to make the FINAL agents `ThyAgentKind` reaches actually
    resolvable in `execute_node`; the MVP single-agent catalogs stay available for narrower runs.

    Spec names are unique across all specialists, so the merge never collides. `ml-agent` and
    `research-agent` are included for completeness even though no `ThyAgentKind` member selects
    them yet -- an extra catalog entry is inert until its enum member exists.
    """
    catalogs = (
        data_agent_catalog(),
        coding_agent_catalog(),
        experiment_agent_catalog(),
        statistics_agent_catalog(),
        data_quality_agent_catalog(),
        visualization_agent_catalog(),
        ml_agent_catalog(),
        research_agent_catalog(),
    )
    specs: list[AgentSpec] = []
    system_prompts: dict[str, str] = {}
    output_schemas: dict[str, type[BaseModel]] = {}
    for catalog in catalogs:
        for name in catalog.names():
            specs.append(catalog.get(name))
            system_prompts[name] = catalog.system_prompt(name)
            output_schemas[name] = catalog.output_schema(name)
    return AgentCatalog(tuple(specs), system_prompts=system_prompts, output_schemas=output_schemas)


__all__ = [
    "CODING_AGENT_SPEC",
    "DATA_AGENT_SPEC",
    "DATA_QUALITY_AGENT_SPEC",
    "EXPERIMENT_AGENT_SPEC",
    "ML_AGENT_SPEC",
    "ML_TUNING_HELPER_SPEC",
    "RESEARCH_AGENT_SPEC",
    "STATISTICS_AGENT_SPEC",
    "VISUALIZATION_AGENT_SPEC",
    "Citation",
    "Claim",
    "CodeResult",
    "DataProfile",
    "DataQualityReport",
    "ExperimentResult",
    "MLResult",
    "Plot",
    "PlotResult",
    "ResearchResult",
    "StatsResult",
    "TuningResult",
    "coding_agent_catalog",
    "data_agent_catalog",
    "data_quality_agent_catalog",
    "experiment_agent_catalog",
    "full_agent_catalog",
    "ml_agent_catalog",
    "research_agent_catalog",
    "run_ml_agent",
    "statistics_agent_catalog",
    "visualization_agent_catalog",
]
