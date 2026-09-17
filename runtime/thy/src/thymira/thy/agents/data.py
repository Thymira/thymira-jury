"""The Data agent: inspect + profile a dataset (THY-14).

The first specialist `AgentSpec` built directly in code on top of THY-01/03/04's framework
(`thymira.agents`) rather than loaded from YAML — there is no catalog directory in production yet,
and one agent does not need one. `DATA_AGENT_SPEC.name` is `"data"`, matching
`ThyAgentKind.DATA.value` exactly: `thymira.thy.nodes.execute.execute_node` resolves a delegated
`AgentTask` via `catalog.get(agent_task.agent.value)`, so any other name would leave
`ThyAgentKind.DATA` tasks unresolvable. `tool_allowlist` names `glob`/`read_file`
(`thymira.tools.builtins`, TOOL-09) for finding and reading text files, and `profile_dataset`
(TOOL-14, `thymira.tools.builtins.data_analysis`) for the registered dataset itself: profiling is
the bounded, reproducible way to look at a dataset, so the agent never reads a dataset's raw rows
with `read_file`. `profile_dataset` requires the dataset already registered as an Artifact via
`register_dataset` (TOOL-12); THY's Inspect node registers the datasets `.thymira/config.yaml`
declares before any agent runs; this agent reads them through `profile_dataset`.

`query_sql` (bug-hunt H8: registered, but no catalog named it) joins the allowlist for the same
reason `profile_dataset` is here instead of `read_file` on the raw rows: read-only SQL over a
registered dataset is bounded and reproducible in exactly the way this agent is meant to look at
data, one level more targeted than a full-dataset profile when the run only needs a slice of it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

DATA_AGENT_SPEC = AgentSpec(
    name="data",
    role=Role.AGENT,
    task_kinds=("analyze", "extract"),
    tool_allowlist=("glob", "read_file", "profile_dataset", "query_sql"),
    max_turns=10,
    max_depth=1,
    system_prompt_ref="prompts/data.md",
    output_schema_ref="thymira.thy.agents.data:DataProfile",
)


class DataProfile(BaseModel):
    """The Data agent's structured output: a dataset's shape, not its raw contents."""

    columns: tuple[str, ...] = Field(min_length=1)
    dtypes: dict[str, str]
    missing: dict[str, int]
    target_candidates: tuple[str, ...] = ()


def data_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `DATA_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "data.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (DATA_AGENT_SPEC,),
        system_prompts={DATA_AGENT_SPEC.name: system_prompt},
        output_schemas={DATA_AGENT_SPEC.name: DataProfile},
    )


__all__ = ["DATA_AGENT_SPEC", "DataProfile", "data_agent_catalog"]
