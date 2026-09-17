"""Stable experiment tracker seam."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path


class TrackerRun:
    """A serializable read view of one tracker run."""

    def __init__(
        self,
        run_id: str,
        experiment_name: str,
        status: str,
        params: dict[str, str],
        metrics: dict[str, float],
        artifacts: tuple[str, ...],
        tags: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.experiment_name = experiment_name
        self.status = status
        self.params = params
        self.metrics = metrics
        self.artifacts = artifacts
        self.tags = dict(tags or {})


@runtime_checkable
class ExperimentTracker(Protocol):
    """The backend-independent experiment tracking contract."""

    def start_run(self, experiment_name: str, *, tags: dict[str, str] | None = None) -> str:
        """Start a run and return its tracker id."""
        ...

    def log_param(self, run_id: str, key: str, value: Any) -> None:
        """Log one parameter."""
        ...

    def log_metric(self, run_id: str, key: str, value: float) -> None:
        """Log one metric."""
        ...

    def log_artifact(self, run_id: str, path: Path) -> None:
        """Copy and associate one artifact file."""
        ...

    def end_run(self, run_id: str, *, status: str = "FINISHED") -> None:
        """Finish one run."""
        ...

    def query_runs(
        self,
        *,
        experiment_name: str | None = None,
        run_id: str | None = None,
    ) -> tuple[TrackerRun, ...]:
        """Read runs by optional experiment or tracker id."""
        ...
