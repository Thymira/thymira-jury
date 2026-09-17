"""Summarize node: model comparison + scientific recommendation + report artifact (THY-13).

`compare_experiments` is the deterministic half ("the LLM proposes, code authorizes"): it alone
picks `best_model`/`metrics` by ranking `state.experiments` on a configured metric. THY's own
(FRONTIER, task='synthesize') call never sees that choice as a decision to make -- it only ever
supplies the tradeoffs/limitations narrative (`SynthesisNarrative`), merged with the deterministic
facts into the final `Recommendation`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent as PydanticAgent

from thymira.agents.llm.routing import Role
from thymira.agents.model_binding import routed_model
from thymira.agents.prompt_framing import frame_untrusted
from thymira.observability import agent as agent_observation
from thymira.schemas import Actor, ArtifactKind, EventType
from thymira.thy.models import Recommendation, SynthesisNarrative

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from thymira.agents.llm.base import LLMProvider, LLMResponse
    from thymira.agents.llm.routing import ModelChoice
    from thymira.events import EventLog
    from thymira.schemas import Experiment, ModelRoutePolicy
    from thymira.state import ArtifactStore
    from thymira.thy.models import ThyState

_SUMMARIZE_INSTRUCTIONS = (
    "You are THY, writing the recommendation for a completed data-science run. You are given "
    "the best model and its metrics, already chosen by deterministic comparison -- never "
    "propose a different one. Write only the tradeoffs of that choice and its limitations."
)


def compare_experiments(experiments: Sequence[Experiment], *, metric: str) -> Experiment:
    """Rank `experiments` by `metric` (higher is better) and return the best one.

    Raises:
        ValueError: `experiments` is empty, or none of them report `metric`.
    """
    scored = [experiment for experiment in experiments if metric in experiment.metrics]
    if not scored:
        msg = f"no experiment reports the metric {metric!r}"
        raise ValueError(msg)
    return max(scored, key=lambda experiment: experiment.metrics[metric])


def summarize_node(
    artifact_store: ArtifactStore,
    event_log: EventLog,
    *,
    provider: LLMProvider | None = None,
    metric: str = "accuracy",
    before_model_request: Callable[[], Sequence[str]] | None = None,
    before_model_selection: Callable[[], None] | None = None,
    before_model_call: Callable[[ModelChoice], None] | None = None,
    record_model_usage: Callable[[LLMResponse], None] | None = None,
    route_policy: ModelRoutePolicy | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build the Summarize node bound to `artifact_store`.

    Terminal phase: does not call `state.advance()` (SUMMARIZE is already the last `ThyPhase`).
    A run with no experiments or none reporting the comparison metric degrades to a summary
    saying so, without writing a report artifact nobody could substantiate. Unscored attempts
    remain available for MIRA to audit, including a refused sandbox execution.
    """

    def _summarize(state: ThyState) -> dict[str, Any]:
        if not state.experiments:
            return state.model_copy(
                update={"summary": "no experiments to compare; nothing to recommend"}
            ).model_dump()
        if not any(metric in experiment.metrics for experiment in state.experiments):
            return state.model_copy(
                update={
                    "summary": f"no experiments report {metric!r}; nothing to recommend",
                    "recommendation": None,
                }
            ).model_dump()

        best = compare_experiments(state.experiments, metric=metric)
        model = routed_model(
            Role.THY,
            "synthesize",
            event_log,
            provider=provider,
            output_schema=SynthesisNarrative,
            before_model_request=before_model_request,
            before_model_selection=before_model_selection,
            before_model_call=before_model_call,
            record_model_usage=record_model_usage,
            route_policy=route_policy,
        )
        agent: PydanticAgent[None, SynthesisNarrative] = PydanticAgent(
            model=model, output_type=SynthesisNarrative, instructions=_SUMMARIZE_INSTRUCTIONS
        )
        with agent_observation(name="thy-summarize", objective=state.run.prompt) as observation:
            result = agent.run_sync(
                frame_untrusted(
                    f"Best model: {best.name!r}. Metrics: {best.metrics}.",
                    label="thy-experiment-results",
                )
            )
            observation.update(output={"best_model": best.name})
        recommendation = Recommendation(
            best_model=best.name,
            metrics=best.metrics,
            tradeoffs=result.output.tradeoffs,
            limitations=result.output.limitations,
        )

        rendered = (
            f"# Analysis\n\n"
            f"**Best model**: {recommendation.best_model}\n\n"
            f"**Metrics**: {recommendation.metrics}\n\n"
            f"## Tradeoffs\n\n{recommendation.tradeoffs}\n\n"
            f"## Limitations\n\n{recommendation.limitations}\n"
        )
        artifact = artifact_store.save_text(
            "analysis.md",
            rendered,
            produced_by=state.run.id,
            kind=ArtifactKind.REPORT,
            media_type="text/markdown",
        )
        event_log.append(
            EventType.ARTIFACT_CREATED,
            Actor.system(),
            {
                "name": artifact.name,
                "sha256": artifact.sha256,
                "artifact_id": artifact.id,
                "produced_by": artifact.produced_by,
            },
            subject_id=artifact.id,
            producer="thymira.thy",
        )
        return state.model_copy(
            update={
                "summary": rendered,
                "recommendation": recommendation,
                "artifacts": (*state.artifacts, artifact),
            }
        ).model_dump()

    return _summarize


__all__ = ["compare_experiments", "summarize_node"]
