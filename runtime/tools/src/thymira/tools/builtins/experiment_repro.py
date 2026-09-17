"""Validated reproducibility evidence emitted by the default experiment child."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from thymira.schemas import ThymiraModel
from thymira.tools.model_sidecars import MAX_SIDECAR_BYTES, read_bounded_json
from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from pathlib import Path

_REQUIRED_LIBRARY_VERSIONS = frozenset(
    {"python", "numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl"}
)
DEFAULT_TEST_SIZE = 0.25


class ThreadpoolObservation(ThymiraModel):
    """A thread pool observed inside the constrained training child."""

    user_api: StrictStr = Field(min_length=1)
    internal_api: StrictStr = Field(min_length=1)
    prefix: StrictStr = Field(min_length=1)
    version: StrictStr | None = None
    num_threads: StrictInt = Field(ge=1)


class ExperimentReproducibilityEvidence(ThymiraModel):
    """The bounded, child-produced provenance for one default baseline model."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

    seed: StrictInt = Field(ge=0)
    library_versions: dict[StrictStr, StrictStr]
    estimator_args: dict[str, Any]
    split: dict[str, Any]
    single_thread: StrictBool
    observed_threadpools: tuple[ThreadpoolObservation, ...]

    @model_validator(mode="after")
    def _validate_evidence_consistency(self) -> ExperimentReproducibilityEvidence:
        """Reject malformed or self-contradictory child provenance."""
        missing = _REQUIRED_LIBRARY_VERSIONS.difference(self.library_versions)
        if missing or any(not value for value in self.library_versions.values()):
            raise ValueError("library_versions must name all required installed libraries")
        observed_single_thread = bool(self.observed_threadpools) and all(
            pool.num_threads == 1 for pool in self.observed_threadpools
        )
        if self.single_thread is not observed_single_thread:
            raise ValueError("single_thread must match the observed thread pools")
        if set(self.split) != {"test_size", "random_state", "shuffle", "stratify"}:
            raise ValueError("split settings must record the complete fixed split configuration")
        split_seed = self.split["random_state"]
        if (
            type(self.split["test_size"]) is not float
            or self.split["test_size"] != DEFAULT_TEST_SIZE
            or type(split_seed) is not int
            or split_seed != self.seed
            or self.split["shuffle"] is not True
            or self.split["stratify"] is not None
        ):
            raise ValueError("split settings must record the actual fixed seed and test size")
        classifier_seed = self.estimator_args.get("random_state")
        if type(classifier_seed) is not int or classifier_seed != self.seed:
            raise ValueError("estimator arguments must record the actual fixed seed")
        return self


def load_reproducibility_evidence(path: Path) -> ExperimentReproducibilityEvidence:
    """Read validated default-training evidence without deserializing the model artifact."""
    try:
        return read_bounded_json(
            path,
            ExperimentReproducibilityEvidence,
            max_bytes=MAX_SIDECAR_BYTES,
            label="default experiment reproducibility evidence",
        )
    except ToolExecutionError as exc:
        raise ToolExecutionError(
            "default experiment training produced missing or malformed reproducibility evidence"
        ) from exc


__all__ = [
    "DEFAULT_TEST_SIZE",
    "ExperimentReproducibilityEvidence",
    "load_reproducibility_evidence",
]
