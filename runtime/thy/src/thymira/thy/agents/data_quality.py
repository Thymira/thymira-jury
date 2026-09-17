"""The Data Quality agent: flag leakage, imbalance, drift and sensitive attributes (THY-20).

Same drop-in shape as THY-14/18: a hand-written `AgentSpec` on the THY-01/03/04 framework
(`thymira.agents`), FINAL-tier. Since THY-32 added `ThyAgentKind.DATA_QUALITY` (its value equal to
this spec's `name`), `execute_node` can dispatch to it through a catalog that holds this spec, such
as `full_agent_catalog()`. `tool_allowlist` names `read_file`
(`thymira.tools.builtins.files`, TOOL-09) to inspect the raw dataset and `run_python` for the
checks a fixed tool does not cover (correlation with the target, class balance, distribution
drift). Unlike THY-18's `StatsResult`, `DataQualityReport`'s findings are the model's own
narrative judgement -- there is no deterministic tool computing "leakage risk" or "sensitive
attribute" the way `run_statistics` computes a p-value, so nothing here is cross-checked against a
tool result the way `StatsResult.statistic` is.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

DATA_QUALITY_AGENT_SPEC = AgentSpec(
    name="data-quality-agent",
    role=Role.AGENT,
    task_kinds=("analyze",),
    tool_allowlist=("read_file", "run_python"),
    max_turns=6,
    max_depth=1,
    system_prompt_ref="prompts/data_quality.md",
    output_schema_ref="thymira.thy.agents.data_quality:DataQualityReport",
)


class DataQualityReport(BaseModel):
    """The Data Quality agent's structured output: what could make a model untrustworthy."""

    leakage_risks: tuple[str, ...] = ()
    imbalance: tuple[str, ...] = ()
    drift_signals: tuple[str, ...] = ()
    sensitive_attributes: tuple[str, ...] = ()


def data_quality_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `DATA_QUALITY_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "data_quality.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (DATA_QUALITY_AGENT_SPEC,),
        system_prompts={DATA_QUALITY_AGENT_SPEC.name: system_prompt},
        output_schemas={DATA_QUALITY_AGENT_SPEC.name: DataQualityReport},
    )


__all__ = ["DATA_QUALITY_AGENT_SPEC", "DataQualityReport", "data_quality_agent_catalog"]
