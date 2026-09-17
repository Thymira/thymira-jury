"""Experiment: a tracked training/evaluation with parameters, metrics and produced artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from thymira.schemas.base import ThymiraModel, utc_now
from thymira.schemas.enums import ExperimentStatus
from thymira.schemas.ids import Id


class Experiment(ThymiraModel):
    """Thymira view of an experiment; MLflow holds the full record (``tracker_run_id``)."""

    id: Id
    run_id: Id
    name: str = Field(min_length=1)
    status: ExperimentStatus = ExperimentStatus.RUNNING
    parameters: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    seed: int | None = None
    dataset_artifact_id: Id | None = None
    model_artifact_id: Id | None = None
    artifact_ids: tuple[Id, ...] = ()
    tracker: str = Field(default="mlflow", description="experiment tracker backend")
    tracker_run_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error: str | None = None
