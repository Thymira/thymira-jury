"""Project inputs every new Run reads: the context document and the declared datasets.

These routes change the project, not a Run. Each Run reads ``.thymira/context.md`` and registers
the datasets ``config.yaml`` declares when it starts, so a change here reaches the next Run, while a
Run that already registered a dataset keeps its own content-addressed copy as evidence. The context
document leaves the API as a redacted projection like every other response; when redaction changed
it, ``redacted`` is true and the runtime refuses a save that would write the masks back over the
source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Path, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.routes._scope import _configured_project
from thymira.api.schemas import (
    ProjectContextResponse,
    ProjectContextUpdate,
    ProjectDatasetListResponse,
    ProjectDatasetTargetUpdate,
    ProjectDatasetUploadResponse,
    ProjectDatasetView,
)
from thymira.core import (
    ProjectInputConflictError,
    ProjectInputError,
    ProjectInputNotFoundError,
    ProjectInputs,
    ProjectInputTooLargeError,
)
from thymira.events import redact_export

if TYPE_CHECKING:
    from thymira.core import ContextDocument, DeclaredDataset
    from thymira.schemas import Id

router = APIRouter(prefix="/project", tags=["project"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_REQUIRE_WRITE = Depends(require_permission(Permission.WRITE))
_DATASET_NAME = Path(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
_FILENAME = Query(..., min_length=1, max_length=255)
_TARGET = Query(default=None, min_length=1, max_length=200)
# The two list reads take no path, query or body parameter, so FastAPI infers no 422 for them;
# declare the one it would generate, as GET /settings does (tests/thymira/test_api_contract.py).
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "Validation Error",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}
        },
    }
}


def _inputs(deps: RuntimeDeps) -> tuple[Id, ProjectInputs]:
    """Return the configured project's id and its inputs, refusing an unconfigured API."""
    project_id = _configured_project(deps)
    resolution = deps.project_resolution
    if resolution is None:  # pragma: no cover - _configured_project already refused this
        raise problem(503, "project_not_configured", "The API has no configured project workspace.")
    return project_id, ProjectInputs(resolution.project_dir)


def _context_response(project_id: Id, document: ContextDocument) -> ProjectContextResponse:
    safe_text = cast("str", redact_export(document.text))
    return ProjectContextResponse(
        project_id=project_id,
        text=safe_text,
        sha256=document.sha256,
        exists=document.exists,
        size_bytes=len(document.text.encode("utf-8")),
        redacted=safe_text != document.text,
    )


def _dataset_view(dataset: DeclaredDataset) -> ProjectDatasetView:
    return ProjectDatasetView(
        name=dataset.name,
        path=dataset.path,
        target=dataset.target,
        present=dataset.present,
        size_bytes=dataset.size_bytes,
        modified_at=dataset.modified_at,
    )


@router.get(
    "/context",
    response_model=ProjectContextResponse,
    dependencies=[_REQUIRE_READ],
    responses=_VALIDATION_ERROR_RESPONSE,
)
def get_context(deps: RuntimeDeps = _DEPS) -> ProjectContextResponse:
    """Return the project's context document as a redacted projection with its source digest."""
    project_id, inputs = _inputs(deps)
    try:
        document = inputs.read_context()
    except (OSError, UnicodeError) as exc:
        raise problem(
            409, "context_unreadable", "The context document cannot be read as UTF-8 text."
        ) from exc
    return _context_response(project_id, document)


@router.put("/context", response_model=ProjectContextResponse, dependencies=[_REQUIRE_WRITE])
def put_context(payload: ProjectContextUpdate, deps: RuntimeDeps = _DEPS) -> ProjectContextResponse:
    """Replace the context document when it still has the digest the caller read."""
    project_id, inputs = _inputs(deps)
    try:
        document = inputs.write_context(payload.text, expected_sha256=payload.expected_sha256)
    except ProjectInputTooLargeError as exc:
        raise problem(413, "context_too_large", str(exc)) from exc
    except ProjectInputConflictError as exc:
        raise problem(409, "context_conflict", str(exc)) from exc
    except ProjectInputError as exc:
        raise problem(422, "invalid_context", str(exc)) from exc
    except (OSError, UnicodeError) as exc:
        raise problem(
            409, "context_unwritable", "The context document could not be written."
        ) from exc
    return _context_response(project_id, document)


@router.get(
    "/datasets",
    response_model=ProjectDatasetListResponse,
    dependencies=[_REQUIRE_READ],
    responses=_VALIDATION_ERROR_RESPONSE,
)
def list_datasets(deps: RuntimeDeps = _DEPS) -> ProjectDatasetListResponse:
    """List the datasets the project declares and whether each file is present."""
    project_id, inputs = _inputs(deps)
    try:
        datasets = inputs.datasets()
    except ProjectInputError as exc:
        raise problem(409, "project_config_invalid", str(exc)) from exc
    return ProjectDatasetListResponse(
        project_id=project_id, items=tuple(_dataset_view(dataset) for dataset in datasets)
    )


@router.put(
    "/datasets/{name}",
    response_model=ProjectDatasetUploadResponse,
    dependencies=[_REQUIRE_WRITE],
)
async def upload_dataset(
    request: Request,
    name: str = _DATASET_NAME,
    filename: str = _FILENAME,
    target: str | None = _TARGET,
    deps: RuntimeDeps = _DEPS,
) -> ProjectDatasetUploadResponse:
    """Stream a CSV or Parquet file into the project and declare it under ``name``.

    The request body is the file itself. The upload is staged beside its destination, refused
    once it passes the registration ceiling, read exactly as Inspect will read it, and only then
    published and declared, so every declared file is one the next Run can register as a source.
    """
    project_id, inputs = _inputs(deps)
    try:
        upload = await run_in_threadpool(inputs.begin_upload, name, filename)
    except ProjectInputError as exc:
        raise problem(422, "invalid_dataset", str(exc)) from exc
    except OSError as exc:
        raise problem(409, "dataset_unwritable", "The upload could not be staged.") from exc
    try:
        async for chunk in request.stream():
            upload.write(chunk)
        result = await run_in_threadpool(upload.commit, target=target)
    except ProjectInputTooLargeError as exc:
        raise problem(413, "dataset_too_large", str(exc)) from exc
    except ProjectInputError as exc:
        raise problem(422, "invalid_dataset", str(exc)) from exc
    except ClientDisconnect as exc:
        raise problem(
            400, "upload_interrupted", "The upload ended before the file arrived."
        ) from exc
    except OSError as exc:
        raise problem(409, "dataset_unwritable", "The dataset could not be written.") from exc
    finally:
        await run_in_threadpool(upload.abort)
    return ProjectDatasetUploadResponse(
        project_id=project_id,
        dataset=_dataset_view(result.dataset),
        rows=result.rows,
        columns=result.columns,
    )


@router.patch(
    "/datasets/{name}",
    response_model=ProjectDatasetView,
    dependencies=[_REQUIRE_WRITE],
)
def update_dataset_target(
    payload: ProjectDatasetTargetUpdate,
    name: str = _DATASET_NAME,
    deps: RuntimeDeps = _DEPS,
) -> ProjectDatasetView:
    """Set or clear the column a declared dataset predicts, checked against the file's header."""
    _, inputs = _inputs(deps)
    try:
        dataset = inputs.set_target(name, payload.target)
    except ProjectInputNotFoundError as exc:
        raise problem(404, "dataset_not_found", str(exc)) from exc
    except ProjectInputError as exc:
        raise problem(422, "invalid_dataset", str(exc)) from exc
    except OSError as exc:
        raise problem(
            409, "project_config_unwritable", "config.yaml could not be written."
        ) from exc
    return _dataset_view(dataset)


@router.delete(
    "/datasets/{name}",
    response_model=ProjectDatasetListResponse,
    dependencies=[_REQUIRE_WRITE],
)
def remove_dataset(
    name: str = _DATASET_NAME, deps: RuntimeDeps = _DEPS
) -> ProjectDatasetListResponse:
    """Stop declaring a dataset; the file stays on disk because past Runs' evidence names it."""
    project_id, inputs = _inputs(deps)
    try:
        datasets = inputs.remove_dataset(name)
    except ProjectInputNotFoundError as exc:
        raise problem(404, "dataset_not_found", str(exc)) from exc
    except ProjectInputError as exc:
        raise problem(422, "invalid_dataset", str(exc)) from exc
    except OSError as exc:
        raise problem(
            409, "project_config_unwritable", "config.yaml could not be written."
        ) from exc
    return ProjectDatasetListResponse(
        project_id=project_id, items=tuple(_dataset_view(dataset) for dataset in datasets)
    )


__all__ = ["router"]
