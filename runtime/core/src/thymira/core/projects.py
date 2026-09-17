"""Project configuration loading and workspace-to-project resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from thymira.events import sha256_text
from thymira.schemas import Framework, Id, ProjectConfig
from thymira.state import LocalWorkspaceRegistry, canonical_workspace_path


def load_project_config(path: Path) -> ProjectConfig:
    """Load and validate a project's ``.thymira/config.yaml``."""
    config_path = Path(path)
    data: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"{config_path}: project configuration must contain a mapping"
        raise TypeError(msg)
    return ProjectConfig.model_validate(data)


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    """The stable project identity and governance data for a workspace.

    ``workspace`` is ``config.yaml``'s own directory -- the ``.thymira`` directory itself, where a
    project's governance files (``config.yaml``, ``context.md``, ``policies.yaml``) live. It is
    *not* the directory a Run's work happens in: use :attr:`project_dir` for that. The two are one
    word apart and one level apart, so anything scoping a tool, a graph or a context loader takes
    :attr:`project_dir`, never ``workspace``.
    """

    project_id: Id
    workspace: Path
    config: ProjectConfig

    @property
    def frameworks(self) -> tuple[Framework, ...]:
        """Return the governance frameworks configured for this project."""
        return self.config.governance.frameworks

    @property
    def project_dir(self) -> Path:
        """The project directory that *contains* ``.thymira`` -- the Run's working root.

        This is what ``thymira.thy.context.load_project_context`` and every ``ToolContext``
        already mean by a workspace: both append ``.thymira`` themselves
        (``load_project_context`` reads ``project_dir / ".thymira"``; ``inspect_model`` and
        ``audit_model`` write under ``invocation.workspace / ".thymira"``). Handing them
        :attr:`workspace` instead points them one level too deep -- at ``.thymira/.thymira`` --
        so Inspect silently loads no project context and a delegated agent's file tools are
        sandboxed inside the config directory, unable to reach the project's own data.
        """
        return self.workspace.parent


class ProjectResolver:
    """Resolve a workspace directory to its validated project configuration."""

    def __init__(self, registry: LocalWorkspaceRegistry | None = None) -> None:
        self._registry = registry

    def resolve(self, workspace: Path) -> ProjectResolution:
        """Load the nearest project config and derive a stable project id."""
        workspace_path = canonical_workspace_path(workspace)
        config_path = (
            workspace_path if workspace_path.is_file() else workspace_path / ".thymira/config.yaml"
        )
        config = load_project_config(config_path)
        project_root = canonical_workspace_path(config_path.parent)
        identity = f"{project_root.as_posix()}::{config.project.name}"
        project_id = f"project_{sha256_text(identity)[:32]}"
        if self._registry is not None:
            self._registry.register(project_root, project_id)
        return ProjectResolution(project_id=project_id, workspace=config_path.parent, config=config)


__all__ = ["ProjectResolution", "ProjectResolver", "load_project_config"]
