"""The Statistics agent: run and interpret an inferential test (THY-18).

Same drop-in shape as THY-14/15/16: a hand-written `AgentSpec` on the THY-01/03/04 framework
(`thymira.agents`), FINAL-tier. Since THY-32 added `ThyAgentKind.STATISTICS` (its value equal to
this spec's `name`), `execute_node` resolves a delegated statistics task through any catalog that
holds this spec -- `full_agent_catalog()` is the composed roster that does.
`tool_allowlist` names `run_statistics` (`thymira.tools.builtins.run_statistics`, the deterministic
SciPy-backed test runner) plus `run_python` for ad-hoc computation the fixed test set does not
cover. `run_statistics` reads a dataset already registered as an Artifact via `register_dataset`
(TOOL-12), same limitation THY-14 already notes for its own tools -- wiring dataset registration
into THY's flow is separate, later work; this task's own tests register one directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

STATISTICS_AGENT_SPEC = AgentSpec(
    name="statistics-agent",
    role=Role.AGENT,
    task_kinds=("analyze",),
    tool_allowlist=("run_statistics", "run_python"),
    max_turns=6,
    max_depth=1,
    system_prompt_ref="prompts/statistics.md",
    output_schema_ref="thymira.thy.agents.statistics:StatsResult",
)


class StatsResult(BaseModel):
    """The Statistics agent's structured output: the test run and how to read it.

    `statistic`/`p_value` are the raw numbers `run_statistics` computed; `effect_size`,
    `assumptions` and `caveats` are the agent's own narrative interpretation of them --
    code never derives an effect size or checks a test's assumptions, only the model does.
    """

    test: str
    statistic: float
    p_value: float
    effect_size: str | None = None
    assumptions: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()


def statistics_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `STATISTICS_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "statistics.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (STATISTICS_AGENT_SPEC,),
        system_prompts={STATISTICS_AGENT_SPEC.name: system_prompt},
        output_schemas={STATISTICS_AGENT_SPEC.name: StatsResult},
    )


__all__ = ["STATISTICS_AGENT_SPEC", "StatsResult", "statistics_agent_catalog"]
