"""Map an Experiment agent's `ExperimentResult` onto a `schemas.Experiment` (THY-16 residual).

THY-16 shipped `ExperimentResult` mirroring exactly what the MLflow tracker persisted and its own
docstring explicitly left "turning this into a real `Experiment` ... a separate mapping step this
task does not own". THY-31 is the first task that needs it: without a `schemas.Experiment` on
`ThyState.experiments`, Summarize (`thymira.thy.nodes.summarize`) takes its empty branch and the
happy-path run produces no `Recommendation` and no report artifact. This module is that mapping and
nothing more.

Two deliberate limits, both keeping the mapping honest rather than inventing evidence:

- `parameters` stay `dict[str, str]` exactly as the tracker recorded them; coercing the strings
  back to typed values (what the tracker lost) is left, because a coercion here would be a guess,
  not a fact. `Experiment.parameters` is `dict[str, Any]`, so the strings validate as they are.
- `model_artifact_id` is set only from a real `ArtifactStore` id the caller harvested, never from
  `ExperimentResult.model_artifact_id` (a tracker-side reference that is not a prefixed thymira
  `Id` and would fail `Experiment`'s `Id` validation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.schemas import Experiment, ExperimentStatus, new_id, utc_now

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Id
    from thymira.thy.agents.experiment import ExperimentResult
    from thymira.thy.agents.ml import MLResult


def experiment_from_result(
    result: ExperimentResult,
    *,
    run_id: Id,
    name: str,
    artifact_ids: Sequence[Id] = (),
    model_artifact_id: Id | None = None,
) -> Experiment:
    """Build a completed `schemas.Experiment` from an Experiment agent's `ExperimentResult`.

    Args:
        result: The tracker-shaped output the Experiment agent returned.
        run_id: The Run this experiment belongs to.
        name: A human-readable name for the experiment (the plan task's instruction).
        artifact_ids: Ids of artifacts the run produced during this experiment, already recorded
            in the `ArtifactStore` by the caller (never minted here).
        model_artifact_id: The store id of the produced model artifact, if the caller identified
            one; left `None` otherwise.
    """
    return Experiment(
        id=new_id("experiment"),
        run_id=run_id,
        name=name,
        status=ExperimentStatus.COMPLETED,
        parameters=dict(result.parameters),
        metrics=dict(result.metrics),
        seed=result.seed,
        model_artifact_id=model_artifact_id,
        artifact_ids=tuple(artifact_ids),
        tracker_run_id=result.tracker_run_id,
        completed_at=utc_now(),
    )


def experiment_from_ml_result(
    result: MLResult,
    *,
    run_id: Id,
    name: str,
    artifact_ids: Sequence[Id] = (),
    model_artifact_id: Id | None = None,
    tracker_run_id: str | None = None,
) -> Experiment:
    """Build a completed `schemas.Experiment` from an ML agent's `MLResult` (THY-19 audit fold).

    `ml-agent` never calls `run_experiment`, so it has no `ExperimentResult` and no
    `EXPERIMENT_COMPLETED` event; it logs to MLflow directly through `mlflow_start_run`/
    `mlflow_log_param`/`mlflow_log_metric`. Without this mapping, MIRA's audit of experiments never
    sees `ml-agent` training the same way it sees the `experiment` agent's, defeating the reason
    `_route_model_plan` treats `ML` as satisfying a training request at all. `chosen_family` and
    `cv_strategy` are folded into `parameters` alongside the hyperparameters so nothing the model
    reported is dropped; `tracker_run_id` is passed in by the caller (read from the
    `mlflow_start_run` tool call's recorded value) rather than guessed here, and stays `None` when
    the caller could not find one.
    """
    parameters: dict[str, str] = {
        "chosen_family": result.chosen_family,
        "cv_strategy": result.cv_strategy,
        **result.hyperparameters,
    }
    return Experiment(
        id=new_id("experiment"),
        run_id=run_id,
        name=name,
        status=ExperimentStatus.COMPLETED,
        parameters=parameters,
        metrics=dict(result.metrics),
        model_artifact_id=model_artifact_id,
        artifact_ids=tuple(artifact_ids),
        tracker_run_id=tracker_run_id,
        completed_at=utc_now(),
    )


__all__ = ["experiment_from_ml_result", "experiment_from_result"]
