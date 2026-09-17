"""ProjectConfig: the typed form of ``.thymira/config.yaml`` (baseline, section 15)."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import Field, field_validator, model_validator

from thymira.schemas.base import ThymiraModel
from thymira.schemas.enums import Framework


class ProjectInfo(ThymiraModel):
    """Identity of an agent-aware data-science project."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    domain: str = Field(min_length=1, description="e.g. 'credit_risk'")


class ExperimentsConfig(ThymiraModel):
    """Experiment tracking settings."""

    tracking: str = "mlflow"


class AgentsConfig(ThymiraModel):
    """Which orchestrator serves the project by default."""

    default: str = "thy"


class GovernanceConfig(ThymiraModel):
    """Audit requirements for the project."""

    audit_required: bool = True
    frameworks: tuple[Framework, ...] = ()


class DatasetConfig(ThymiraModel):
    """One dataset the project declares; THY registers it at Inspect, before any agent runs.

    ``path`` is relative to the project directory (the directory that holds ``.thymira/``) and
    may not leave it. ``target`` names the column the project predicts, when it has one, so the
    planner and the agents do not have to guess it.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    path: str = Field(min_length=1)
    target: str | None = None

    @field_validator("path")
    @classmethod
    def _relative_and_contained(cls, value: str) -> str:
        """Refuse an absolute path or one that climbs out of the project directory."""
        posix = PurePosixPath(value.replace("\\", "/"))
        windows = PureWindowsPath(value)
        if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
            msg = (
                "dataset path must be relative to the project directory and may not contain "
                f"'..': {value!r}"
            )
            raise ValueError(msg)
        return value


class ProjectConfig(ThymiraModel):
    """The whole ``.thymira/config.yaml``; unknown keys are rejected on purpose."""

    project: ProjectInfo
    experiments: ExperimentsConfig = Field(default_factory=ExperimentsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    datasets: tuple[DatasetConfig, ...] = ()

    @model_validator(mode="after")
    def _unique_dataset_names(self) -> ProjectConfig:
        """Refuse two declared datasets that share a name (Inspect registers each by name)."""
        seen: set[str] = set()
        for dataset in self.datasets:
            if dataset.name in seen:
                msg = f"duplicate dataset name: {dataset.name!r}"
                raise ValueError(msg)
            seen.add(dataset.name)
        return self
