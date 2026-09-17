"""The ML agent: model selection, training, tuning; may delegate to a helper (THY-19).

Same drop-in shape as THY-14/18/20/21 for the primary spec -- `ML_AGENT_SPEC`, FINAL-tier, not
part of `ThyAgentKind`. What is new: this is the first agent to exercise THY-08's
sub-agent -> helper delegation (`orchestrator(0) -> sub-agent(1) -> helper(2)`), which nothing in
this member calls from inside an agent's own turn -- `AgentRunner`'s tool-calling loop only ever
runs `spec.tool_allowlist`, never `Delegator`, so the model cannot trigger a second delegation
itself mid-turn. `MLResult` therefore carries two extra fields no other specialist output has,
`needs_tuning_help`/`tuning_objective`: the model's own signal, read by `run_ml_agent` (the
orchestration wrapper below, not the model) *after* the primary run completes, exactly the way
`execute_node` already reads `PlanOutput` and decides to delegate -- a delegation decision is
always orchestration code reading structured output, never a tool call an agent makes on its own.

`ML_TUNING_HELPER_SPEC` is the delegate target: a second, narrower `AgentSpec` (`run_python` only,
`max_depth=2` -- an `AgentSpec`'s `max_depth` is the depth *it itself* is allowed to run at, not
how deep it may delegate further, see `Delegator.delegate`/`DepthGuard.check`). Both specs must
share one `AgentCatalog` (`ml_agent_catalog()`): `Delegator.delegate`'s inner `AgentRunner.run`
resolves the helper by name against the *same* `ctx.catalog` the primary run was given.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel

from thymira.agents import (
    AgentCatalog,
    AgentContext,
    AgentResult,
    AgentRunner,
    AgentSpec,
    Delegator,
)
from thymira.agents.llm.routing import Role
from thymira.schemas import Task, TaskStatus

_PROMPTS_DIR = Path(__file__).parent / "prompts"

ML_AGENT_SPEC = AgentSpec(
    name="ml-agent",
    role=Role.AGENT,
    task_kinds=("code",),
    tool_allowlist=(
        "run_python",
        "mlflow_start_run",
        "mlflow_log_param",
        "mlflow_log_metric",
        "mlflow_log_artifact",
        "mlflow_end_run",
    ),
    max_turns=10,
    max_depth=1,
    system_prompt_ref="prompts/ml.md",
    output_schema_ref="thymira.thy.agents.ml:MLResult",
)

ML_TUNING_HELPER_SPEC = AgentSpec(
    name="ml-tuning-helper",
    role=Role.AGENT,
    task_kinds=("code",),
    tool_allowlist=("run_python",),
    max_turns=6,
    max_depth=2,
    system_prompt_ref="prompts/ml_tuning_helper.md",
    output_schema_ref="thymira.thy.agents.ml:TuningResult",
)


class MLResult(BaseModel):
    """The ML agent's structured output: what it trained, and whether it wants tuning help."""

    chosen_family: str
    hyperparameters: dict[str, str]
    cv_strategy: str
    metrics: dict[str, float]
    needs_tuning_help: bool = False
    tuning_objective: str | None = None


class TuningResult(BaseModel):
    """The tuning helper's structured output: what it found, for the `agent.message` record."""

    hyperparameters: dict[str, str]
    best_score: float
    trials: int


def ml_agent_catalog() -> AgentCatalog:
    """Build the `AgentCatalog` entry for both `ML_AGENT_SPEC` and `ML_TUNING_HELPER_SPEC`.

    Both specs share one catalog, not two: `Delegator.delegate`'s inner `AgentRunner.run` looks
    the helper up by name against the caller's own `ctx.catalog`, so a helper missing from it
    would fail with an unresolvable output schema the moment the primary agent tries to delegate.
    """
    ml_prompt = (_PROMPTS_DIR / "ml.md").read_text(encoding="utf-8")
    helper_prompt = (_PROMPTS_DIR / "ml_tuning_helper.md").read_text(encoding="utf-8")
    return AgentCatalog(
        (ML_AGENT_SPEC, ML_TUNING_HELPER_SPEC),
        system_prompts={
            ML_AGENT_SPEC.name: ml_prompt,
            ML_TUNING_HELPER_SPEC.name: helper_prompt,
        },
        output_schemas={
            ML_AGENT_SPEC.name: MLResult,
            ML_TUNING_HELPER_SPEC.name: TuningResult,
        },
    )


def run_ml_agent(task: Task, ctx: AgentContext, *, depth: int = 1) -> AgentResult:
    """Run the ML agent, delegating to the tuning helper if it asks for one.

    `depth` is the ML agent's *own* depth (1 when THY delegates to it the usual way, matching
    every other specialist agent's `max_depth=1`). The helper's contribution is recorded only as
    an `agent.message` event on `ctx.event_log` -- this function returns the primary run's own
    `MLResult` unchanged, never a value merged with the helper's `TuningResult`; nothing in THY-19's
    own deliverable asks for that merge, and inventing one here would be scope this task does not
    own.
    """
    primary_context = (
        ctx
        if ctx.current_agent is not None or ctx.delegation is not None
        else replace(ctx, current_agent=ML_AGENT_SPEC.name)
    )
    result = AgentRunner().run(ML_AGENT_SPEC, task, primary_context)
    if (
        result.task_status is TaskStatus.COMPLETED
        and isinstance(result.output, MLResult)
        and result.output.needs_tuning_help
        and result.output.tuning_objective is not None
    ):
        Delegator(primary_context).delegate(
            ML_AGENT_SPEC.name,
            ML_TUNING_HELPER_SPEC,
            result.output.tuning_objective,
            depth=depth,
        )
    return result


__all__ = [
    "ML_AGENT_SPEC",
    "ML_TUNING_HELPER_SPEC",
    "MLResult",
    "TuningResult",
    "ml_agent_catalog",
    "run_ml_agent",
]
