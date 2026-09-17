"""The Experiment agent: create an experiment, log metrics/params, save artifacts (THY-16).

Same drop-in shape as THY-14/THY-15: a hand-written `AgentSpec` on the THY-01/03/04 framework
(`thymira.agents`), named `"experiment"` to match `ThyAgentKind.EXPERIMENT.value` exactly so
`execute_node`'s `catalog.get(agent_task.agent.value)` lookup resolves it. `tool_allowlist` names
the five real MLflow tracker tools (`thymira.tools.builtins.mlflow_tools`, TOOL-17), `run_python`
for runs that need to produce their own metrics before logging them, and `run_experiment`
(TOOL-16, `thymira.tools.builtins.run_experiment`) — the one-call way to train and track a
deterministic baseline on a registered dataset when a full hand-written loop is not needed.

`audit_model`, `compare_models`, `inspect_model` and `query_mlflow` join the allowlist here
(bug-hunt H8: registered tools with no caller in any catalog, `audit_model` most pointedly, since
it is the one hardened into the injected sandbox the same day this was noticed). All four are a
natural extension of what this agent already does with a trained model -- `audit_model` and
`inspect_model` run through the same injected sandbox `run_python` already does, so this adds no
new *kind* of access, only named, purpose-built operations in place of the general-purpose one.
`git_commit`/`git_log`/`git_status` are deliberately left unreached for now: unlike these four,
committing to the repository is an effect worth its own authorization decision, not a bundled
extra.

`write_file` and `export_pdf` join the allowlist for the same structural reason found live
2026-09-14: `_route_model_plan` (`thymira.thy.nodes.plan`) reassigns a *whole* `coding` task to
this agent whenever the Run's prompt asks for training and the planner omitted a dedicated
`experiment` task, carrying that task's full instruction with it -- report and PDF export
included, when the prompt asked for a single deliverable covering both. Without these two tools
the reassigned task trained successfully and then could never finish, exhausting `max_turns`
retrying a step it had no tool for (`required artifacts missing: <report>.pdf`). This does not
relax the training boundary above: a report this agent writes still documents what the tracker or
`run_python` actually produced, never chain-of-thought, and `run_experiment`/MLflow remain how it
trains and tracks a model.

`ExperimentResult` deliberately mirrors what the tracker tools actually persist, not the richer
`thymira.schemas.Experiment` record: MLflow only ever stores parameters as strings
(`MlflowLogParam`'s `value: str`), so `parameters: dict[str, str]` here, never `dict[str, Any]`.
Turning this into a real `Experiment` (coercing parameter strings back to typed values, assigning
`id`/`run_id`/`dataset_artifact_id`) is a separate mapping step this task does not own -- the
roadmap's own seam note calls out `ExperimentResult -> Experiment` as additive, later work.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.llm.routing import Role

_PROMPTS_DIR = Path(__file__).parent / "prompts"

EXPERIMENT_AGENT_SPEC = AgentSpec(
    name="experiment",
    role=Role.AGENT,
    task_kinds=("code",),
    tool_allowlist=(
        "mlflow_start_run",
        "mlflow_log_param",
        "mlflow_log_metric",
        "mlflow_log_artifact",
        "mlflow_end_run",
        "run_python",
        "run_experiment",
        "audit_model",
        "compare_models",
        "inspect_model",
        "query_mlflow",
        "write_file",
        "export_pdf",
    ),
    max_turns=32,
    max_depth=1,
    system_prompt_ref="prompts/experiment.md",
    output_schema_ref="thymira.thy.agents.experiment:ExperimentResult",
)


class ExperimentResult(BaseModel):
    """The Experiment agent's structured output: exactly what the tracker recorded."""

    parameters: dict[str, str]
    metrics: dict[str, float]
    seed: int | None = None
    model_artifact_id: str | None = None
    tracker_run_id: str


def experiment_agent_catalog() -> AgentCatalog:
    """Build the real, single-spec `AgentCatalog` entry for `EXPERIMENT_AGENT_SPEC`."""
    system_prompt = (_PROMPTS_DIR / "experiment.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (EXPERIMENT_AGENT_SPEC,),
        system_prompts={EXPERIMENT_AGENT_SPEC.name: system_prompt},
        output_schemas={EXPERIMENT_AGENT_SPEC.name: ExperimentResult},
    )


__all__ = ["EXPERIMENT_AGENT_SPEC", "ExperimentResult", "experiment_agent_catalog"]
