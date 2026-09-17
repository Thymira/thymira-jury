"""Plan node: THY's own plan, gated for approval before Execute (THY-11).

Unlike Execute (THY-09), Plan is never a delegated sub-agent call: it is THY's own reasoning
(`Role.THY`, `task='plan'`, FRONTIER tier by the routing table), built directly on `routed_model`
rather than `AgentRunner`/`Delegator` — THY has no `AgentSpec`, so there is nothing for a
spec-driven runner to run here.

`gate.check_action(action_type='plan.proposed')` is the seam ADR-0004 calls for: THY's plan always
passes through the Policy Engine before Execute. The decision is trusted exactly as
`thymira.policies.allows_execution` trusts it: `PASS`/`WARNING` proceed; `REQUIRE_HUMAN_REVIEW` is
**not** satisfied by the `Gate`'s synchronous approver -- its answer is recorded as a
`human.approval` event (evidence), never as authority (`product-final.md` corrections C-9/C-15) --
so, under the default policy (no rule for `plan.proposed`, hence `REQUIRE_HUMAN_REVIEW`), the graph
halts at Plan until `HITL-01` provides a checkable `Approval`. A policy whose `ActionRule` passes
`plan.proposed` lets the plan advance. A seeded `state.plan` (a Core resume carrying a
`ThyProgress`) skips the model and the Gate entirely: it was already gated when it was first
proposed, and asking either again could plan the waiting call away or mint a second decision
for it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from types import GenericAlias
from typing import TYPE_CHECKING, Any

from pydantic import Field, create_model
from pydantic_ai import Agent as PydanticAgent

from thymira.agents.llm.routing import Role
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import frame_untrusted
from thymira.observability import agent as agent_observation
from thymira.observability import guardrail
from thymira.policies import allows_execution
from thymira.schemas import ExecutionAction
from thymira.thy.models import AgentTask, PlanOutput, ThyAgentKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents import AgentCatalog, RuntimeSkillCatalog
    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.agents.request_ledger import RequestLedger
    from thymira.events import EventLog
    from thymira.policies import Gate
    from thymira.schemas import ModelRoutePolicy
    from thymira.thy.models import ThyState

_AGENT_DESCRIPTIONS: dict[str, str] = {
    "data": "inspect and profile the dataset (shape, dtypes, missing values, target candidates).",
    "coding": "write and run analysis or transformation code.",
    "experiment": "train and evaluate a model as a single tracked experiment.",
    "statistics-agent": "run and interpret an inferential or descriptive statistical test.",
    "data-quality-agent": "flag leakage, imbalance, drift and sensitive attributes.",
    "visualization-agent": "produce a captioned plot artifact.",
    "ml-agent": (
        "train a model with iterative hyperparameter search across multiple trials, escalating"
        " to a tuning helper -- use only when the request explicitly asks for tuning/search, not"
        " for a single baseline run."
    ),
}

_MODEL_REQUEST_PATTERN = (
    r"\b(?:train(?:ing)?|model(?:s|ling)?|classifier|regression|random\s+forest|"
    r"predict(?:ion)?|fit(?:ting)?|metric(?:s)?|evaluate)\b"
)
_MODEL_REQUEST_RE = re.compile(_MODEL_REQUEST_PATTERN, re.IGNORECASE)
_NEGATED_SENTENCE_START_RE = re.compile(r"\b(?:do\s+not|don['\u2019]t)\b", re.IGNORECASE)
_DIRECT_MODEL_MODIFIER = (
    r"any|a|an|the|new|additional|further|more|separate|explicit|first|fresh|ever|"
    r"actually|predictive|statistical|machine|learning|classification|logistic|linear|"
    r"neural|network|deep|supervised|unsupervised"
)
_NEGATED_DIRECT_MODEL_START_RE = re.compile(
    rf"\b(?:without|no)\b"
    rf"(?=[\s:-]*(?:(?:{_DIRECT_MODEL_MODIFIER})\s+){{0,4}}{_MODEL_REQUEST_PATTERN})",
    re.IGNORECASE,
)
_SCOPE_BOUNDARY_RE = re.compile(
    r"[,.!?;\n]"
    r"|\b(?:but|however|except|yet|so|therefore|then|while|whereas|although|though|"
    r"when|during|before|after)\b"
    r"|\band\s+(?:(?:we|you)\s+)?(?:need|must|should|will|want|plan|intend)\s+to\b",
    re.IGNORECASE,
)
_DISCOURSE_ONLY_RE = re.compile(r"\b(?:however|nevertheless|nonetheless)\b", re.IGNORECASE)
"""One line per `ThyAgentKind` value THY's Plan model may ever propose. Filtered against the
actual catalog by `_plan_instructions` -- see its docstring for why a static prompt was wrong."""


def _plannable_agent_names(catalog: AgentCatalog) -> tuple[str, ...]:
    """Return the enum-backed agents this exact catalog can actually dispatch."""
    registered = frozenset(catalog.names())
    return tuple(kind.value for kind in ThyAgentKind if kind.value in registered)


def _catalog_agent_kind_type(agent_names: tuple[str, ...]) -> type[StrEnum]:
    """Create the string enum used by one catalog-scoped planning schema."""
    enum_members = {ThyAgentKind(name).name: name for name in agent_names}
    return StrEnum("_CatalogThyAgentKind", enum_members)


def _catalog_skill_name_type(skill_names: tuple[str, ...]) -> type[StrEnum]:
    """Create a collision-free string enum for one runtime skill catalog.

    Runtime skill names may contain ``-`` and ``.`` and therefore are not safe Python member
    names. Positional member keys keep the exact catalog values without lossy normalisation or
    collisions between otherwise similar names.
    """
    enum_members = {f"SKILL_{index}": name for index, name in enumerate(skill_names)}
    return StrEnum("_CatalogRuntimeSkillName", enum_members)


@lru_cache(maxsize=32)
def _catalog_plan_output_type(
    agent_names: tuple[str, ...], skill_names: tuple[str, ...] = ()
) -> type[PlanOutput]:
    """Build a cached ``PlanOutput`` subtype matching both executable catalogs.

    ``PlanOutput`` deliberately carries every globally supported ``ThyAgentKind`` so callers can
    opt into the full specialist roster. A particular Run may use a narrower catalog, however.
    Giving that Run the global JSON Schema contradicts its prompt and lets a model select an agent
    Execute cannot resolve. Runtime skill names have the same boundary: a free-form name passes
    policy but later fails while prompt construction resolves the catalog. The derived schema
    closes both gaps before the Gate; conversion back to canonical ``PlanOutput`` happens
    immediately after validation.
    """
    if not agent_names:
        raise ValueError("the THY catalog has no enum-backed agents available for planning")
    catalog_kind = _catalog_agent_kind_type(agent_names)
    if skill_names:
        catalog_skill = _catalog_skill_name_type(skill_names)
        skill_names_annotation = GenericAlias(tuple, (catalog_skill, Ellipsis))
        skill_names_field: object = ()
    else:
        skill_names_annotation = GenericAlias(tuple, (str, Ellipsis))
        skill_names_field = Field(default=(), max_length=0)
    catalog_task = create_model(
        "_CatalogAgentTask",
        __base__=AgentTask,
        agent=(catalog_kind, ...),
        skill_names=(skill_names_annotation, skill_names_field),
    )
    # The annotation is deliberately runtime-built from the dynamic task subtype. Pydantic needs
    # the concrete GenericAlias here so the JSON Schema nests the restricted agent enum.
    catalog_tasks_annotation = GenericAlias(tuple, (catalog_task, Ellipsis))
    return create_model(
        "_CatalogPlanOutput",
        __base__=PlanOutput,
        tasks=(catalog_tasks_annotation, Field(min_length=1)),
    )


def _plan_instructions(
    catalog: AgentCatalog, runtime_skill_catalog: RuntimeSkillCatalog | None = None
) -> str:
    """Build Plan instructions from the same roster that constrains its structured schema.

    A static list of all six `ThyAgentKind` agents drifted from `default_thy_catalog()`'s
    deliberate three-agent MVP scope (`test_default_thy_catalog_carries_the_three_thy_agent_
    kinds`). Filtering the prose fixed only half of that contract: the global `PlanOutput` JSON
    Schema still offered unavailable values. `_plannable_agent_names` now feeds both this prompt
    and `_catalog_plan_output_type`, so prose and machine-readable choices cannot drift again.
    """
    lines = [f"- {name}: {_AGENT_DESCRIPTIONS[name]}" for name in _plannable_agent_names(catalog)]
    preamble = (
        "You are THY, planning a data-science run. Produce an ordered, non-empty list of "
        "AgentTask items, each naming the specialist agent, the ThyGraph phase it runs in "
        "(always 'execute'), and a concrete instruction. Choose each agent for the work it "
        "owns, and only when the run needs that work. When the user requests named output files, "
        "put each exact workspace-relative path in that task's required_artifacts, including its "
        "artifact kind when known. These are acceptance criteria, not suggestions; do not invent "
        "paths. kind='report' is only for a file MIRA's audit can read as a report -- Markdown, "
        "plain text, PDF, CSV or JSON; a '.ipynb' notebook is always kind='code', produced by "
        "run_notebook, never kind='report'. Leave required_artifacts empty when no file output "
        "was requested:"
    )
    trailer = "Name only these agents, spelled exactly as listed."
    runtime_skill_names = runtime_skill_catalog.names() if runtime_skill_catalog is not None else ()
    if runtime_skill_catalog is not None and runtime_skill_names:
        trailer = (
            f"{trailer}\nFor a task that needs a runtime skill, set skill_names to one or more "
            "exact names from the catalog; leave it empty when no runtime skill is needed. "
            "Runtime skill bodies are loaded by Execute only after these names are selected.\n"
            f"{runtime_skill_catalog.index_text()}"
        )
    else:
        trailer = f"{trailer}\nNo runtime skills are available; leave every skill_names empty."
    return f"{preamble}\n" + "\n".join(lines) + f"\n{trailer}"


def _scope_has_content(text: str) -> bool:
    """Return whether a would-be negated scope contains more than discourse punctuation."""
    return bool(re.search(r"\w", _DISCOURSE_ONLY_RE.sub(" ", text)))


def _negated_scope_end(prompt: str, content_start: int, *, comma_boundary: bool) -> int:
    """Find the first grammatical boundary after a negator.

    Commas end the shorter ``no``/``without`` phrases but not a coordinated ``do not`` list.
    A contrast marker only ends either scope after real content; this keeps the parenthetical
    ``do not, however, train`` under negation while exposing ``do not train X, but train Y``.
    """
    for match in _SCOPE_BOUNDARY_RE.finditer(prompt, content_start):
        boundary = match.group()
        if boundary == "," and not comma_boundary:
            continue
        if (boundary == "," or boundary[0].isalnum()) and not _scope_has_content(
            prompt[content_start : match.start()]
        ):
            continue
        return match.start()
    return len(prompt)


def _mask_negated_scopes(prompt: str, start_re: re.Pattern[str], *, comma_boundary: bool) -> str:
    """Replace matching negated scopes with spaces while preserving all other text."""
    masked = list(prompt)
    for match in start_re.finditer(prompt):
        end = _negated_scope_end(prompt, match.end(), comma_boundary=comma_boundary)
        masked[match.start() : end] = " " * (end - match.start())
    return "".join(masked)


def _requires_experiment(prompt: str) -> bool:
    """Return whether any positive scope asks for model training or measured evaluation.

    Model terms inside ``do not`` lists are prohibitions, not requests. Remove those scopes up to
    a sentence or genuine contrast boundary. Only remove ``without``/``no`` phrases when model
    work follows the negator directly; an unrelated phrase such as ``no synthetic data while
    training`` must leave the positive training clause visible. A later contrast remains visible:
    ``do not train one model, but train another`` still requires Experiment.
    """
    positive_scope = _mask_negated_scopes(prompt, _NEGATED_SENTENCE_START_RE, comma_boundary=False)
    positive_scope = _mask_negated_scopes(
        positive_scope, _NEGATED_DIRECT_MODEL_START_RE, comma_boundary=True
    )
    return bool(_MODEL_REQUEST_RE.search(positive_scope))


def _route_model_plan(
    prompt: str, planned: PlanOutput, catalog: AgentCatalog
) -> tuple[PlanOutput | None, str | None]:
    """Ensure model work is assigned to a tracked, auditable agent.

    THY's model proposes a plan, but code owns this invariant: a request that trains or evaluates
    a model cannot be fulfilled by a generic coding task whose outputs MIRA cannot audit as an
    experiment. `ML` (`ml-agent`) satisfies this the same way `EXPERIMENT` does: `execute_node`
    folds a completed `ml-agent` settlement into a `schemas.Experiment` too (`experiments.py`'s
    `experiment_from_ml_result`), so a plan that deliberately chose it is already tracked and is
    left untouched. Existing experiment/ml tasks remain untouched; a coding task is upgraded only
    when the planner omitted both specialists entirely -- the upgrade always targets `EXPERIMENT`,
    never `ML`, since only the model can judge whether a request needs `ml-agent`'s tuning-helper
    escalation.
    """
    if not _requires_experiment(prompt) or any(
        task.agent in (ThyAgentKind.EXPERIMENT, ThyAgentKind.ML) for task in planned.tasks
    ):
        return planned, None
    if ThyAgentKind.EXPERIMENT.value not in catalog.names():
        return None, "model training requested but the experiment agent is unavailable"
    tasks = list(planned.tasks)
    coding_index = next(
        (index for index, task in enumerate(tasks) if task.agent is ThyAgentKind.CODING), None
    )
    if coding_index is not None:
        task = tasks[coding_index]
        tasks[coding_index] = task.model_copy(
            update={
                "agent": ThyAgentKind.EXPERIMENT,
                "instruction": (
                    "Use the run_experiment tool for all model training and evaluation; do not "
                    f"train the model through generic code. Original task: {task.instruction}"
                ),
            }
        )
    else:
        tasks.append(
            planned.tasks[0].model_copy(
                update={
                    "id": "model-experiment",
                    "agent": ThyAgentKind.EXPERIMENT,
                    "instruction": (
                        "Use the run_experiment tool for the requested model training and "
                        f"evaluation: {prompt}"
                    ),
                    "depends_on": tuple(task.id for task in planned.tasks),
                    "required_artifacts": (),
                }
            )
        )
    return PlanOutput(tasks=tuple(tasks)), None


def plan_node(
    catalog: AgentCatalog,
    gate: Gate,
    event_log: EventLog,
    *,
    provider: LLMProvider | None = None,
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    route_policy: ModelRoutePolicy | None = None,
    runtime_skill_catalog: RuntimeSkillCatalog | None = None,
    request_ledger: RequestLedger | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build the Plan node bound to `gate`.

    A decision that does not allow execution (`BLOCK`, or `REQUIRE_HUMAN_REVIEW`, which no
    synchronous answer can satisfy) records the reason on `state.error` and leaves `state.plan`
    empty -- `_route_after_plan` (`thymira.thy.graph`) reads that to halt the graph before
    Execute. An allowed decision populates `state.plan` and advances normally. Either way, the
    decision is appended to `state.policy_signals`. A non-empty `state.plan` (a Core resume)
    short-circuits before any of that: it advances straight to Execute without calling the model
    or the Gate again.
    """

    def _plan(state: ThyState) -> dict[str, Any]:
        if state.plan:
            # A seeded plan (a Core resume after a human answered a tool-call review) was gated
            # when it was proposed. Asking the model again could plan the waiting call away,
            # and asking the Gate again would mint a second `plan.proposed` decision for it.
            return state.advance().model_dump()
        if (
            state.execution_constraints is not None
            and ExecutionAction.PLAN in state.execution_constraints.prohibited_actions
        ):
            return state.model_copy(
                update={"error": "plan rejected: execution constraints prohibit plan.proposed"}
            ).model_dump()
        plannable_agent_names = _plannable_agent_names(catalog)
        if not plannable_agent_names:
            return state.model_copy(
                update={"error": "plan rejected: no registered THY agents are available"}
            ).model_dump()
        plannable_skill_names = (
            runtime_skill_catalog.names() if runtime_skill_catalog is not None else ()
        )
        plan_output_type = _catalog_plan_output_type(plannable_agent_names, plannable_skill_names)
        model = routed_model(
            Role.THY,
            "plan",
            event_log,
            provider=provider,
            output_schema=plan_output_type,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
            request_ledger=request_ledger,
            request_owner_id=f"{state.run.id}:thy-plan",
        )
        agent: PydanticAgent[None, PlanOutput] = PydanticAgent(
            model=model,
            output_type=plan_output_type,
            instructions=_plan_instructions(catalog, runtime_skill_catalog),
            retries=1,
        )
        constraints = state.execution_constraints
        constraint_note = (
            f"\nAuthorised execution constraints: {constraints.model_dump(mode='json')}"
            if constraints is not None
            else ""
        )
        registered = ", ".join(
            f"{d.name} (target: {d.target})" if d.target else d.name for d in state.datasets
        )
        dataset_note = (
            "\n"
            + frame_untrusted(
                f"Registered datasets: {registered}",
                label="thy-dataset-catalog",
            )
            if registered
            else ""
        )
        context_note = (
            "\n"
            + frame_untrusted(
                f"Project context (.thymira/context.md):\n{state.project_context.strip()}",
                label="thy-project-context",
            )
            if state.project_context
            else ""
        )
        with agent_observation(name="thy-plan", objective=state.run.prompt) as observation:
            result = agent.run_sync(
                f"Plan the run for: {state.run.prompt}{constraint_note}{dataset_note}{context_note}"
            )
            canonical_output = PlanOutput.model_validate(result.output.model_dump(mode="json"))
            planned, planning_error = _route_model_plan(state.run.prompt, canonical_output, catalog)
            observation.update(
                output={"task_count": len(planned.tasks) if planned is not None else 0}
            )

        if planning_error is not None:
            return state.model_copy(update={"error": planning_error}).model_dump()
        if planned is None:  # pragma: no cover - guarded by planning_error above
            raise AssertionError("model plan normalisation returned no plan without an error")

        with guardrail("policy-plan-proposed") as decision_observation:
            decision = gate.check_action(
                subject_kind="run",
                subject_id=state.run.id,
                action_type="plan.proposed",
                payload={"task_count": len(planned.tasks)},
                summary="plan.proposed",
            )
            decision_observation.update(output={"decision": decision.decision.value})
        policy_signals = (*state.policy_signals, decision)
        if not allows_execution(decision):
            return state.model_copy(
                update={
                    "error": f"plan rejected: {decision.reason}",
                    "policy_signals": policy_signals,
                }
            ).model_dump()

        next_state = state.advance()
        return next_state.model_copy(
            update={"plan": planned.tasks, "policy_signals": policy_signals}
        ).model_dump()

    return _plan


__all__ = ["plan_node"]
