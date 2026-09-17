"""Deterministic provenance capture: what ran, on what, from which inputs.

Captured when a run starts and stored as evidence so an auditor can reproduce the run. A dirty
working tree is recorded, not judged — cleanliness is evidence, not a verdict.
"""

from __future__ import annotations

import importlib.metadata
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from thymira.events import canonical_json, sha256_text
from thymira.schemas import ThymiraModel, utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_DISTRIBUTIONS: tuple[str, ...] = (
    "langgraph",
    "litellm",
    "pydantic",
    "pydantic-ai-slim",
    "scikit-learn",
    "numpy",
    "pandas",
    "mlflow",
)


class RuntimeInfo(ThymiraModel):
    """Interpreter and dependency versions."""

    python: str
    implementation: str
    executable: str
    platform: str
    dependencies: dict[str, str | None]


class SourceControl(ThymiraModel):
    """Git facts; ``available`` is False outside a repository or without git."""

    available: bool
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None


class RunEnvironment(ThymiraModel):
    """The provenance record attached to a run."""

    schema_version: str = "1.0"
    captured_at: datetime = Field(default_factory=utc_now)
    runtime: RuntimeInfo
    source_control: SourceControl
    inputs: dict[str, str | None] = Field(
        default_factory=dict, description="sha256 per named input (case, dataset, policy, …)"
    )
    notes: tuple[str, ...] = ("source control cleanliness is evidence, not a verdict",)


def distribution_versions(
    distributions: Sequence[str] = DEFAULT_DISTRIBUTIONS,
) -> dict[str, str | None]:
    """Installed version per distribution name (``None`` when not installed)."""
    versions: dict[str, str | None] = {}
    for name in distributions:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603  # fixed, trusted arguments
            ["git", *args],  # noqa: S607  # git resolved from PATH on purpose
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def source_control(cwd: Path) -> SourceControl:
    """Commit, branch and dirtiness of the working tree at ``cwd`` (gracefully degraded)."""
    commit = _git(cwd, "rev-parse", "HEAD")
    if commit is None:
        return SourceControl(available=False)
    status = _git(cwd, "status", "--porcelain")
    return SourceControl(
        available=True,
        commit=commit,
        branch=_git(cwd, "branch", "--show-current"),
        dirty=None if status is None else bool(status),
    )


def hash_inputs(**named_content: Any) -> dict[str, str]:
    """Canonical sha256 per named input (``None`` values hash as ``null``)."""
    return {name: sha256_text(canonical_json(content)) for name, content in named_content.items()}


def capture_run_environment(
    *,
    cwd: Path,
    inputs: Mapping[str, str | None] | None = None,
    distributions: Sequence[str] = DEFAULT_DISTRIBUTIONS,
) -> RunEnvironment:
    """Capture interpreter, dependencies, git state and the given input hashes."""
    return RunEnvironment(
        runtime=RuntimeInfo(
            python=platform.python_version(),
            implementation=platform.python_implementation(),
            executable=sys.executable,
            platform=platform.platform(),
            dependencies=distribution_versions(distributions),
        ),
        source_control=source_control(Path(cwd)),
        inputs=dict(inputs or {}),
    )
