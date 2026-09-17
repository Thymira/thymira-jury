"""The Research agent: gather evidence via web search + Knowledge Base, cite every claim (THY-22).

Same drop-in shape as THY-14/18/20/21: a hand-written `AgentSpec` on the THY-01/03/04 framework
(`thymira.agents`), FINAL-tier. `tool_allowlist` names `search` and `kb_query` -- the web-search
and Knowledge-Base tools owned by P3/P1, not by THY (the Seam): they reach the agent through the
same Tool Manager + Gate bridge (`build_agent_tools`, THY-04) every other agent's tools do, so the
KB backend can land later without touching this spec. This agent's own tests drive it with
in-process doubles standing in for those tools.

`ResearchResult` makes the citation rule structural, not advisory: every `Claim` carries at least
one `Citation` (`min_length=1`), so the agent physically cannot return a claim with no source --
"the LLM proposes, code authorizes" applied to evidence, the same way THY-18's `StatsResult` never
carries a statistic a tool did not return. The narrative (the `statement`) is the model's; the
source it must rest on is a fact the schema refuses to let it omit.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

RESEARCH_AGENT_SPEC = AgentSpec(
    name="research-agent",
    role=Role.AGENT,
    task_kinds=("analyze",),
    tool_allowlist=("search", "kb_query"),
    max_turns=6,
    max_depth=1,
    system_prompt_ref="prompts/research.md",
    output_schema_ref="thymira.thy.agents.research:ResearchResult",
)


class Citation(BaseModel):
    """One source a claim rests on: which retrieval surfaced it, and where to find it again.

    `source_id` is the identifier the `search`/`kb_query` result carried (a hit id, a document
    key); `locator` is where a reader goes to verify it (a URL, a KB document reference). Both are
    required -- a citation with no locator is not a source a reader can check.
    """

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class Claim(BaseModel):
    """One factual claim the research produced, and the source(s) it is grounded in.

    `citations` is never empty (`min_length=1`): a claim without a source is exactly what this
    agent must not emit, so the schema, not the prompt alone, enforces it.
    """

    statement: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)


class ResearchResult(BaseModel):
    """The Research agent's structured output: claims, each carrying at least one citation."""

    claims: tuple[Claim, ...] = Field(min_length=1)


def research_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `RESEARCH_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "research.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (RESEARCH_AGENT_SPEC,),
        system_prompts={RESEARCH_AGENT_SPEC.name: system_prompt},
        output_schemas={RESEARCH_AGENT_SPEC.name: ResearchResult},
    )


__all__ = [
    "RESEARCH_AGENT_SPEC",
    "Citation",
    "Claim",
    "ResearchResult",
    "research_agent_catalog",
]
