"""The runtime-context snapshot: the dynamic facts a step must see, as one logged message.

The system prompt stays stable (`PromptEnvironment`); everything that changes during a Run --
registered datasets, artifacts, the sandbox mode -- travels as a `user`-role message so the
provider's prefix cache survives and the log records exactly what the model was told. A later
snapshot supersedes an earlier one: `PromptBuilder` keeps only the latest in the fold, and the
text says so, following dsh's runtime-context message.

`thymira.tools.datasets` is imported inside `runtime_context_text` rather than at module load:
`prompts` imports `RUNTIME_CONTEXT_FORM` from here and is loaded eagerly by
`thymira.agents.__init__`, so a top-level `thymira.tools` import would break the THY-33 boundary
(a bare `import thymira.agents` must not pull in the Tool Manager). The snapshot is only ever
rendered from inside `AgentRunner.run`, by which point `thymira.tools` is already loaded.
"""

from __future__ import annotations

from importlib import metadata
from typing import TYPE_CHECKING

from thymira.schemas import ArtifactKind

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.state import ArtifactStore

RUNTIME_CONTEXT_FORM = "runtime_context"
MAX_COLUMNS_SHOWN = 8
MAX_ARTIFACTS_SHOWN = 40
MAX_PROJECT_CONTEXT_CHARS = 4000
_SCHEMA_PREFIX = "datasets/"
_SCHEMA_SUFFIX = ".schema.json"

# The interpreter `run_python` shares with the runtime (bug-hunt C4): a coding step used to
# discover what is installed by crashing on `ModuleNotFoundError`. Reporting exactly what is
# on the path -- and, just as importantly, naming what is not -- lets a step pick a library
# instead of guessing.
_KNOWN_LIBRARIES = (
    "pandas",
    "numpy",
    "scipy",
    "scikit-learn",
    "statsmodels",
    "matplotlib",
    "polars",
    "duckdb",
    "mlflow",
    "joblib",
)


def _installed_libraries_line() -> str:
    """Report which of the known data-science libraries this interpreter actually has.

    Uses `importlib.metadata` -- the same resolution `run_python`'s subprocess uses, since it
    shares the runtime's interpreter -- rather than a list copied from `pyproject.toml`, so this
    line can never drift from what is actually importable.
    """
    present: list[str] = []
    missing: list[str] = []
    for name in _KNOWN_LIBRARIES:
        try:
            present.append(f"{name}=={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            missing.append(name)
    parts = [f"Installed libraries (run_python's interpreter): {', '.join(present) or 'none'}"]
    if missing:
        parts.append(f"Not installed, do not import: {', '.join(missing)}")
    return " ".join(parts)


def runtime_context_text(
    *,
    workspace: Path,
    artifact_store: ArtifactStore,
    sandbox_mode: str = "workspace-write",
    project_context: str | None = None,
    workspace_dataset_paths: tuple[tuple[str, str], ...] = (),
) -> str:
    """Render the snapshot for one step from the artifact store's current contents.

    Args:
        workspace: The step's working directory, named verbatim in the snapshot.
        artifact_store: The run's store; its active artifacts become the dataset and artifact
            lines, dataset schemas (``datasets/<name>.schema.json``) validated as
            :class:`~thymira.tools.datasets.DatasetSchema`.
        sandbox_mode: The file-effect mode the snapshot reports to the model.
        project_context: The project's own `.thymira/context.md`, when it declares one
            (bug-hunt H4: the runtime loaded this at Inspect and it never reached a delegated
            step's prompt). Truncated to :data:`MAX_PROJECT_CONTEXT_CHARS` -- domain notes, not
            an unbounded document, are what this line is for.
        workspace_dataset_paths: Logical dataset names paired with their project-declared,
            workspace-relative paths. These are the only dataset paths filesystem tools may use;
            artifact-store names remain durable evidence identifiers, not local paths.

    Returns:
        The multi-line snapshot text, opening with the supersession sentence.
    """
    from thymira.tools.datasets import (  # noqa: PLC0415  # lazy: keep `thymira.agents` tool-free (THY-33)
        DatasetSchema,
    )

    active = sorted(artifact_store.list_active(), key=lambda artifact: artifact.name)
    datasets: list[str] = []
    for artifact in active:
        name = artifact.name
        if name.startswith(_SCHEMA_PREFIX) and name.endswith(_SCHEMA_SUFFIX):
            schema = DatasetSchema.model_validate(artifact_store.load_json(name))
            columns = ", ".join(schema.columns[:MAX_COLUMNS_SHOWN])
            if len(schema.columns) > MAX_COLUMNS_SHOWN:
                columns += ", …"
            datasets.append(
                f"{schema.name} ({schema.row_count} rows, {len(schema.columns)} columns: {columns})"
            )
    artifacts: list[str] = []
    for artifact in active:
        name = artifact.name
        if name.startswith(_SCHEMA_PREFIX) and name.endswith(_SCHEMA_SUFFIX):
            continue
        if artifact.kind is ArtifactKind.DATASET:
            continue
        artifacts.append(f"{name} ({artifact.kind.value})")
    dataset_line = (
        "Registered datasets: "
        + "; ".join(datasets)
        + ". Each one's raw file is also readable by read_file/run_python under the workspace, "
        "at the same relative path the project declares it under (e.g. data/<file>.csv)."
        if datasets
        else (
            "Registered datasets: none — a dataset must be registered before profile_dataset "
            "or run_experiment can use it."
        )
    )
    if artifacts:
        shown = artifacts[:MAX_ARTIFACTS_SHOWN]
        rest = len(artifacts) - len(shown)
        artifact_line = "Artifacts present: " + ", ".join(shown)
        if rest:
            artifact_line += f" … and {rest} more"
    else:
        artifact_line = "Artifacts present: none"
    lines = [
        "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.",
        "",
        f"Workspace: {workspace}",
        (
            f"File sandbox: {sandbox_mode} — code tools may modify files under the workspace "
            "only; a blocked file operation is a policy denial, not a bug in the code."
        ),
        _installed_libraries_line(),
        dataset_line,
        artifact_line,
    ]
    if workspace_dataset_paths:
        lines.append(
            "Workspace dataset paths (use these exact paths with read_file/run_python; "
            "artifact-store keys are not filesystem paths):\n"
            + "\n".join(f"- {name}: {path}" for name, path in sorted(workspace_dataset_paths))
        )
    if project_context:
        trimmed = project_context.strip()
        if len(trimmed) > MAX_PROJECT_CONTEXT_CHARS:
            trimmed = trimmed[:MAX_PROJECT_CONTEXT_CHARS] + " …[truncated]"
        lines.append(f"Project context (.thymira/context.md):\n{trimmed}")
    return "\n".join(lines)


__all__ = ["RUNTIME_CONTEXT_FORM", "runtime_context_text"]
