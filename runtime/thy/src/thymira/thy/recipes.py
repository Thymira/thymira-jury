"""Run recipes as data: `.thymira/recipes/<name>.yaml` selects and parameterises a run (THY-28).

ADR-0004 lists "run recipes as data" as explicitly post-MVP, and its seam note is the whole design:
a recipe only *composes* pieces that already exist -- `build_thy_graph`/`run_thy`, an `AgentCatalog`
and `UsageLimits` -- so it adds a config layer without touching the graph or the runner. A recipe
picks the agent subset, fixes the phase plan (a tuple of `AgentTask`s, each carrying its phase), and
declares the per-run budget and a policy reference; `run_recipe` then drives a run that uses exactly
those agents and phases and enforces that budget.

Two boundaries are kept deliberately narrow. The budget is a `thymira.agents.usage.UsageLimits`
(the runtime's single definition of that type, P4/P1-owned) carried through unchanged; the recipe
never reimplements enforcement -- it seeds the run's shared `RunUsage` with the limits, and the
existing per-call charge in the model router raises `UsageLimitExceededError` when a call crosses
them. The `policy_ref` is recorded, not resolved: a policy is P4/P1-owned, so the recipe records the
reference in `Recipe.provenance()` for the composition (`thymira.core`) to apply and capture at run
start, exactly as it captures the rest of a run's provenance -- this member records no Run state.

A recipe *is* the plan, so `run_recipe` leaves the Plan node its bare placeholder: no model planning
and no `plan.proposed` gate run for a recipe-driven run. The plan the recipe authored is the plan
that executes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import Field, ValidationError, model_validator

from thymira.agents import AgentCatalog
from thymira.agents.usage import RunUsage, UsageLimits
from thymira.schemas import ThymiraModel
from thymira.thy.graph import run_thy
from thymira.thy.models import AgentTask, ThyState

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.agents.llm.base import LLMProvider
    from thymira.events import EventLog
    from thymira.schemas import ModelRoutePolicy, Run
    from thymira.state import ArtifactStore
    from thymira.thy.models import ThyInput, ThyOutput
    from thymira.tools import ToolContext, ToolRegistry


class Recipe(ThymiraModel):
    """A run recipe: the agents, the phase plan, the budget and the policy a run is driven with.

    `agents` is the selected subset (names, matching `AgentSpec.name`); `plan` is the ordered phase
    plan, each `AgentTask` naming one of those agents and the phase it runs in; `budget` is the
    per-run `UsageLimits`; `policy_ref` names the policy the composition should apply (recorded, not
    resolved here). A plan that names an agent the recipe did not select is rejected at load.
    """

    name: str = Field(min_length=1)
    metric: str = "accuracy"
    agents: tuple[str, ...] = Field(min_length=1)
    plan: tuple[AgentTask, ...] = Field(min_length=1)
    budget: UsageLimits = Field(default_factory=UsageLimits)
    policy_ref: str | None = None

    @model_validator(mode="after")
    def _plan_uses_only_selected_agents(self) -> Recipe:
        """Reject a recipe whose plan delegates to an agent it did not select."""
        unknown = {task.agent.value for task in self.plan} - set(self.agents)
        if unknown:
            msg = f"recipe {self.name!r} plan uses agents not in its selection: {sorted(unknown)}"
            raise ValueError(msg)
        return self

    def catalog(self, base: AgentCatalog) -> AgentCatalog:
        """Restrict `base` to exactly this recipe's selected agents, keeping resolved refs.

        Raises:
            KeyError: A selected agent is not present in `base`.
        """
        specs = tuple(base.get(name) for name in self.agents)
        prompts = {name: base.system_prompt(name) for name in self.agents}
        schemas = {name: base.output_schema(name) for name in self.agents}
        return AgentCatalog(specs, system_prompts=prompts, output_schemas=schemas)

    def usage(self) -> RunUsage:
        """A fresh shared-usage accumulator carrying this recipe's per-run budget."""
        return RunUsage(limits=self.budget)

    def seed_state(self, run: Run) -> ThyState:
        """The starting `ThyState` a recipe drives: its plan pre-loaded, its budget in force."""
        return ThyState(run=run, plan=self.plan, usage=self.usage())

    def provenance(self) -> dict[str, Any]:
        """This recipe's arguments, for the composition to record in the run's provenance."""
        return {
            "recipe": self.name,
            "agents": list(self.agents),
            "phases": [task.phase.value for task in self.plan],
            "budget": self.budget.model_dump(exclude_none=True),
            "policy_ref": self.policy_ref,
        }


def load_recipe(path: Path) -> Recipe:
    """Load and validate a run recipe from a `.thymira/recipes/<name>.yaml` file.

    Args:
        path: The recipe YAML file to read.

    Returns:
        The validated `Recipe`.

    Raises:
        ValueError: The file is not a YAML mapping, or does not validate as a `Recipe` -- an unknown
            key (``extra='forbid'``), a plan naming an unselected agent, a malformed budget, and so
            on -- rejected at load, never half-applied.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"{path}: a recipe must be a YAML mapping"
        raise ValueError(msg)  # noqa: TRY004  # a malformed recipe is a config error, not a TypeError
    try:
        return Recipe.model_validate(data)
    except ValidationError as exc:
        msg = f"{path}: invalid recipe: {exc}"
        raise ValueError(msg) from exc


def run_recipe(
    recipe: Recipe,
    thy_input: ThyInput,
    base_catalog: AgentCatalog,
    event_log: EventLog,
    *,
    provider: LLMProvider | None = None,
    artifact_store: ArtifactStore | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    route_policy: ModelRoutePolicy | None = None,
) -> ThyOutput:
    """Drive a THY run from `recipe`: its agent subset, its phase plan, its per-run budget (THY-28).

    Restricts `base_catalog` to the recipe's agents and seeds `ThyState` with the recipe's plan and
    a `RunUsage` carrying its budget, then runs ThyGraph. The run therefore uses exactly the
    recipe's agents and phases, and the budget is enforced during Execute (a delegated call that
    crosses it raises). The Plan node stays its placeholder -- the recipe supplied the plan.

    Raises:
        UsageLimitExceededError: A delegated call crossed the recipe's per-run budget.
    """
    return run_thy(
        thy_input,
        recipe.catalog(base_catalog),
        event_log,
        provider=provider,
        artifact_store=artifact_store,
        tool_registry=tool_registry,
        tool_context=tool_context,
        metric=recipe.metric,
        initial_state=recipe.seed_state(thy_input.run),
        route_policy=route_policy,
    )


__all__ = ["Recipe", "load_recipe", "run_recipe"]
