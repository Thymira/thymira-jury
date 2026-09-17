"""Inspect node: seed ThyState with the project's config, context and datasets (THY-10).

Inspect is the Run's intake. It loads `.thymira/config.yaml` and `.thymira/context.md`, and it
registers every dataset the config declares (`ProjectConfig.datasets`) into the Run's
`ArtifactStore` through `register_dataset`, so the artifacts a sub-agent later reads
(`profile_dataset`, `run_experiment`) exist before any agent runs and the runtime-context
snapshot can list them. Registration is a runtime intake step, not a tool call: it goes through
no Tool Manager and no Gate -- the Policy Engine decides what agents may do with the data, not
whether the project may declare it. Inspect announces nothing itself, unlike Summarize (which
appends `analysis.md`'s `artifact.created` directly): in the composed runtime it is Core's own
manifest diff (`thymira.core.graph.adapters._record_artifact_changes`) that announces what
Inspect registered, once ThyGraph returns.

A declared dataset that cannot be registered (missing file, ragged CSV) is a configuration
error, so the node records the reason on `ThyState.error` and `_route_after_inspect`
(`thymira.thy.graph`) halts the graph before Plan: nothing ran, so there is nothing to audit.
Registration is idempotent -- a dataset whose schema artifact is already active is not written
again -- so a resumed Run re-enters Inspect safely.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from thymira.thy.context import load_project_context
from thymira.thy.models import RegisteredDataset
from thymira.tools.datasets import register_dataset, schema_artifact_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from thymira.schemas import DatasetConfig
    from thymira.state import ArtifactStore
    from thymira.thy.models import ThyState


def inspect_node(
    project_dir: Path, *, artifact_store: ArtifactStore | None = None
) -> Callable[..., dict[str, Any]]:
    """Build the Inspect node bound to `project_dir`.

    Seeds `domain`, `governance_frameworks` and `project_context` from
    `load_project_context(project_dir)`; a project without `.thymira` leaves them at `ThyState`'s
    defaults rather than failing the run. With an `artifact_store`, also registers the datasets
    the config declares and records them, name and declared target, on `ThyState.datasets`;
    without one (a lightweight graph), declared datasets are left alone.
    """

    def _inspect(state: ThyState) -> dict[str, Any]:
        context = load_project_context(project_dir)
        updates: dict[str, Any] = {"project_context": context.context_md or None}
        if context.config is not None:
            updates["domain"] = context.config.project.domain
            updates["governance_frameworks"] = context.config.governance.frameworks
            if artifact_store is not None and context.config.datasets:
                registered, error = _register_declared(
                    context.config.datasets, project_dir, artifact_store, produced_by=state.run.id
                )
                updates["datasets"] = registered
                if error is not None:
                    return state.model_copy(update={**updates, "error": error}).model_dump()
        next_state = state.advance()
        return next_state.model_copy(update=updates).model_dump()

    return _inspect


def _register_declared(
    datasets: tuple[DatasetConfig, ...],
    project_dir: Path,
    store: ArtifactStore,
    *,
    produced_by: str,
) -> tuple[tuple[RegisteredDataset, ...], str | None]:
    """Register each declared dataset once; stop at the first one that cannot be registered.

    The whole per-dataset attempt -- resolving the path, the pre-scan `is_file` check, and
    `register_dataset` itself -- is wrapped: a `ValueError` (a bad file, a ragged CSV, a file too
    large) or an `OSError` (a missing permission, a store failure) either way lands on
    `ThyState.error` as a configuration problem, never as a raised exception that would take the
    Run down uncaught.
    """
    active = {artifact.name for artifact in store.list_active()}
    registered: list[RegisteredDataset] = []
    for dataset in datasets:
        if schema_artifact_name(dataset.name) in active:
            registered.append(RegisteredDataset(name=dataset.name, target=dataset.target))
            continue
        try:
            source = project_dir.joinpath(*PurePosixPath(dataset.path.replace("\\", "/")).parts)
            if not source.is_file():
                return tuple(registered), (
                    f"dataset {dataset.name!r} declared in .thymira/config.yaml is missing: "
                    f"{dataset.path}"
                )
            register_dataset(
                store,
                source,
                dataset.name,
                produced_by=produced_by,
                source_path=dataset.path,
            )
        except (ValueError, OSError) as exc:
            return tuple(registered), (f"dataset {dataset.name!r} could not be registered: {exc}")
        registered.append(RegisteredDataset(name=dataset.name, target=dataset.target))
    return tuple(registered), None


__all__ = ["inspect_node"]
