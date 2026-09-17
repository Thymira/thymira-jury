"""Read-only artifact inventory and verified artifact content for one scoped Run.

The API's response boundary serves JSON only (``ExportRedactionMiddleware``): a raw ``image/png``
or ``text/csv`` body is replaced with a generic failure before its first byte is sent. Artifact
content is therefore a JSON *projection*, the same arrangement the event routes use. The bytes are
read from the Run's own store, their digest is recomputed against the manifest before anything is
returned, and the content travels as a redacted string beside the source ``sha256``. Binary content
whose base64 form redaction would alter is withheld rather than served corrupted.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Depends

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.routes._scope import _scoped_run, _validate_run_id
from thymira.api.schemas import ArtifactContentResponse, ArtifactListResponse
from thymira.events import redact_export
from thymira.schemas import Id, id_kind

if TYPE_CHECKING:
    from thymira.schemas import Artifact
    from thymira.state import ArtifactStore

router = APIRouter(prefix="/runs", tags=["artifacts"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))

MAX_ARTIFACT_CONTENT_BYTES = 2 * 1024 * 1024
"""Largest artifact whose content is returned; a larger one is refused with ``413``.

The whole projection is buffered by the redaction middleware, so the bound caps one response's
memory as well as its transfer. Metadata for every artifact stays available from the list route.
"""

_BINARY_WITHHELD = (
    "Export redaction would alter this binary content, so it is withheld rather than served "
    "corrupted."
)


def _open_store(deps: RuntimeDeps, run_id: Id) -> ArtifactStore:
    """Open the Run's artifact store, mapping an unreadable manifest to a conflict."""
    try:
        return deps.artifact_store_factory(run_id)
    except (OSError, TypeError, ValueError) as exc:
        raise problem(
            409,
            "run_integrity_error",
            "The Run's artifact manifest cannot be read.",
        ) from exc


@router.get(
    "/{run_id}/artifacts",
    response_model=ArtifactListResponse,
    dependencies=[_REQUIRE_READ],
)
def list_artifacts(run_id: Id, deps: RuntimeDeps = _DEPS) -> ArtifactListResponse:
    """List every artifact the Run's manifest records, active and superseded, oldest first."""
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    manifest = _open_store(deps, run_id).manifest()
    items = tuple(
        sorted(manifest.values(), key=lambda artifact: (artifact.created_at, artifact.name))
    )
    return ArtifactListResponse(run_id=run_id, items=items)


@router.get(
    "/{run_id}/artifacts/{artifact_id}",
    response_model=ArtifactContentResponse,
    dependencies=[_REQUIRE_READ],
)
def get_artifact(
    run_id: Id,
    artifact_id: Id,
    deps: RuntimeDeps = _DEPS,
) -> ArtifactContentResponse:
    """Return one artifact's record and a redacted projection of its digest-verified bytes.

    The route never trusts the manifest's claim about the bytes: it reads them within the size
    bound, recomputes their sha256 and length, and refuses with ``409 artifact_tampered`` when
    either disagrees. Only then is the content projected.
    """
    run_id = _validate_run_id(run_id)
    _scoped_run(deps, run_id)
    if id_kind(artifact_id) != "artifact":
        raise problem(422, "invalid_artifact_id", f"{artifact_id!r} is not an artifact identifier.")
    store = _open_store(deps, run_id)
    found = next(
        (
            (key, artifact)
            for key, artifact in store.manifest().items()
            if artifact.id == artifact_id
        ),
        None,
    )
    if found is None:
        raise problem(
            404,
            "artifact_not_found",
            f"Artifact {artifact_id!r} is not recorded for Run {run_id!r}.",
        )
    key, artifact = found
    if artifact.size_bytes > MAX_ARTIFACT_CONTENT_BYTES:
        raise problem(
            413,
            "artifact_too_large",
            f"The artifact is {artifact.size_bytes} bytes; content is served up to "
            f"{MAX_ARTIFACT_CONTENT_BYTES} bytes.",
        )
    try:
        data = store.load_bytes_bounded(key, MAX_ARTIFACT_CONTENT_BYTES)
    except (OSError, ValueError) as exc:
        message = (
            "The artifact's content cannot be read from the Run's store."
            if artifact.valid
            else "This artifact revision is no longer active and its content is not served."
        )
        raise problem(409, "artifact_unavailable", message) from exc
    if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise problem(
            409,
            "artifact_tampered",
            "The stored artifact bytes do not match the digest recorded in the manifest.",
        )
    return _content_projection(run_id, artifact, data)


def _content_projection(run_id: Id, artifact: Artifact, data: bytes) -> ArtifactContentResponse:
    """Project verified bytes as redacted UTF-8 text or, for binary content, standard base64."""
    text = _as_text(data)
    if text is not None:
        safe_text = cast("str", redact_export(text))
        return ArtifactContentResponse(
            run_id=run_id,
            artifact=artifact,
            encoding="utf-8",
            content=safe_text,
            redacted=safe_text != text,
        )
    encoded = base64.b64encode(data).decode("ascii")
    if redact_export(encoded) != encoded:
        return ArtifactContentResponse(
            run_id=run_id,
            artifact=artifact,
            encoding="base64",
            content=None,
            withheld_reason=_BINARY_WITHHELD,
        )
    return ArtifactContentResponse(
        run_id=run_id,
        artifact=artifact,
        encoding="base64",
        content=encoded,
    )


def _as_text(data: bytes) -> str | None:
    """Return ``data`` as text when it is NUL-free UTF-8, or ``None`` for binary content."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


__all__ = ["MAX_ARTIFACT_CONTENT_BYTES", "router"]
