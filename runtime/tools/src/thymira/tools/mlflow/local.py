"""A dependency-free local tracker with an MLflow-shaped contract."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from thymira.schemas import new_id
from thymira.tools.mlflow.base import TrackerRun


class MlflowTracker:
    """Persist tracker runs as canonical JSON under workspace/.mlflow."""

    def __init__(self, workspace: Path) -> None:
        self.root = Path(workspace) / ".mlflow"
        self.root.mkdir(parents=True, exist_ok=True)

    def start_run(self, experiment_name: str, *, tags: dict[str, str] | None = None) -> str:
        """Create a RUNNING tracker record."""
        run_id = new_id("run")
        self._write(
            run_id,
            {
                "run_id": run_id,
                "experiment_name": experiment_name,
                "status": "RUNNING",
                "params": {},
                "metrics": {},
                "tags": tags or {},
                "artifacts": [],
            },
        )
        return run_id

    def log_param(self, run_id: str, key: str, value: Any) -> None:
        """Store a string parameter."""
        record = self._read(run_id)
        record["params"][key] = str(value)
        self._write(run_id, record)

    def log_metric(self, run_id: str, key: str, value: float) -> None:
        """Store one finite numeric metric."""
        metric = float(value)
        if not math.isfinite(metric):
            raise ValueError("metric must be finite")
        record = self._read(run_id)
        record["metrics"][key] = metric
        self._write(run_id, record)

    def log_artifact(self, run_id: str, path: Path) -> None:
        """Copy a file into the run's local artifact directory."""
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        self.log_artifact_bytes(run_id, source.name, source.read_bytes())

    def log_artifact_bytes(self, run_id: str, name: str, data: bytes) -> None:
        """Publish already validated bytes without depending on a staging path."""
        if not name or Path(name).name != name:
            raise ValueError("tracker artifact name must be a file name")
        if not isinstance(data, bytes):
            raise TypeError("tracker artifact data must be bytes")
        record = self._read(run_id)
        destination = self.root / "artifacts" / run_id / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        record["artifacts"].append(destination.relative_to(self.root).as_posix())
        self._write(run_id, record)

    def end_run(self, run_id: str, *, status: str = "FINISHED") -> None:
        """Finish a tracker run."""
        record = self._read(run_id)
        record["status"] = status
        self._write(run_id, record)

    def query_runs(
        self,
        *,
        experiment_name: str | None = None,
        run_id: str | None = None,
    ) -> tuple[TrackerRun, ...]:
        """Return matching runs in deterministic order.

        A ``run_id`` of ``None`` means "no id filter" and lists every run; any provided id -- an
        empty string included -- is looked up as a single record, so an empty id matches nothing
        instead of silently falling through to every run in the store.
        """
        paths = (
            [self.root / f"{run_id}.json"]
            if run_id is not None
            else sorted(self.root.glob("run_*.json"))
        )
        runs: list[TrackerRun] = []
        for path in paths:
            if not path.is_file():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if experiment_name is not None and record["experiment_name"] != experiment_name:
                continue
            runs.append(
                TrackerRun(
                    run_id=record["run_id"],
                    experiment_name=record["experiment_name"],
                    status=record["status"],
                    params=dict(record["params"]),
                    metrics={key: float(value) for key, value in record["metrics"].items()},
                    artifacts=tuple(record["artifacts"]),
                    tags={key: str(value) for key, value in record.get("tags", {}).items()},
                )
            )
        return tuple(runs)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"

    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        if not path.is_file():
            raise KeyError(f"unknown tracker run: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, run_id: str, record: dict[str, Any]) -> None:
        self._path(run_id).write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )


__all__ = ["MlflowTracker"]
