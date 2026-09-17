"""The Coding/Execution agent: generate and execute Python, read the result (THY-15).

Same drop-in shape as the Data agent (THY-14): a hand-written `AgentSpec` on the THY-01/03/04
framework (`thymira.agents`), named `"coding"` to match `ThyAgentKind.CODING.value` exactly so
`execute_node`'s `catalog.get(agent_task.agent.value)` lookup resolves it. `tool_allowlist` names
the real P3 tools (`write_file`, `run_python`, `read_file`) that drive the write -> run -> read
loop the roadmap deliverable describes, plus `export_pdf` for a declared deliverable that names a
`.pdf` path: it renders a Markdown file this agent already wrote (and any image it embeds) into a
registered PDF Artifact. Git remains an explicit tool surface for an integrated worktree harness;
an isolated Run workspace deliberately has no repository for an agent to diff.

`max_turns=40` (raised from 24, then from 8; the 24 floor found live 2026-09-12 was itself
exhausted live 2026-09-13): each tool call this agent makes charges a turn, whatever the Policy
Engine decides about it, so a Run whose inherent-risk classification never resolves (a real,
reproduced case: MIRA's own confidence landing just under `RISK_CONFIDENCE_THRESHOLD`) makes every
one of them `REQUIRE_HUMAN_REVIEW`. A real report-compilation task hit this twice: first six
approval round trips against a floor of 8, then -- after that floor was raised to 24 -- a longer
run under the same condition still ended `coding failed: agent exceeded max_turns` (recorded
`end_reason=context_budget`, since the context ceiling was reached first; see
`thymira.thy.budget.DEFAULT_CONTEXT_WINDOW`), with the correct content never reaching a
`write_file` call, only visible as `required artifacts missing` once THY halted. 40 is again a
deliberately generous floor for the same write -> run -> read loop under maximal review friction,
not a tuned minimum; revisit if a real task still exhausts it. Raising this number alone does not
address why the task needed so many turns in the first place -- see `prompts/coding.md`'s guidance
against re-reading an already-verified file, added from the same finding.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

CODING_AGENT_SPEC = AgentSpec(
    name="coding",
    role=Role.AGENT,
    task_kinds=("code",),
    tool_allowlist=("write_file", "run_python", "read_file", "export_pdf", "run_notebook"),
    max_turns=40,
    max_depth=1,
    system_prompt_ref="prompts/coding.md",
    output_schema_ref="thymira.thy.agents.coding:CodeResult",
)


class CodeResult(BaseModel):
    """The Coding agent's structured output: what actually ran, not what was intended."""

    stdout: str
    exit_code: int
    artifacts: tuple[str, ...] = ()


def coding_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `CODING_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "coding.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (CODING_AGENT_SPEC,),
        system_prompts={CODING_AGENT_SPEC.name: system_prompt},
        output_schemas={CODING_AGENT_SPEC.name: CodeResult},
    )


__all__ = ["CODING_AGENT_SPEC", "CodeResult", "coding_agent_catalog"]
