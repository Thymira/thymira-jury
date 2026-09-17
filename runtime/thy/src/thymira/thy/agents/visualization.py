"""The Visualization agent: produce a plot artifact with a caption (THY-21).

Same drop-in shape as THY-14/18/20: a hand-written `AgentSpec` on the THY-01/03/04 framework
(`thymira.agents`), FINAL-tier. Since THY-32 added `ThyAgentKind.VISUALIZATION` (its value equal to
this spec's `name`), `execute_node` can dispatch to it through a catalog that holds this spec, such
as `full_agent_catalog()`. `tool_allowlist` names `run_python` (to render a plot, e.g. with
matplotlib, to an SVG
string) and `write_file` (to persist it) -- the pair the roadmap deliverable specifies. Neither
tool registered a real `Artifact` on its own, so `write_file` grew an optional `kind` argument
(this task) that, when given, also calls `ArtifactStore.save_batch` -- the same
`ToolManager`-diff path (TOOL-04) every other artifact-producing tool already uses. Without that,
this task's own "Done when" (a PLOT artifact with a sha256 in the manifest) was not reachable
with the tools the spec names.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

VISUALIZATION_AGENT_SPEC = AgentSpec(
    name="visualization-agent",
    role=Role.AGENT,
    task_kinds=("analyze",),
    tool_allowlist=("run_python", "write_file"),
    max_turns=6,
    max_depth=1,
    system_prompt_ref="prompts/visualization.md",
    output_schema_ref="thymira.thy.agents.visualization:PlotResult",
)


class Plot(BaseModel):
    """One rendered plot: the artifact it became and how to read it."""

    artifact_id: str = Field(min_length=1)
    caption: str = Field(min_length=1)


class PlotResult(BaseModel):
    """The Visualization agent's structured output: at least one plot, each captioned."""

    plots: tuple[Plot, ...] = Field(min_length=1)


def visualization_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `VISUALIZATION_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "visualization.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (VISUALIZATION_AGENT_SPEC,),
        system_prompts={VISUALIZATION_AGENT_SPEC.name: system_prompt},
        output_schemas={VISUALIZATION_AGENT_SPEC.name: PlotResult},
    )


__all__ = [
    "VISUALIZATION_AGENT_SPEC",
    "Plot",
    "PlotResult",
    "visualization_agent_catalog",
]
