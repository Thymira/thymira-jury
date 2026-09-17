"""User settings routes backed by the runtime-owned compare-and-set store."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path

from thymira.agents.llm.routing import MODEL_OVERRIDES_NAMESPACE, set_model_overrides
from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.schemas import SettingsListResponse, SettingsPutRequest
from thymira.schemas import SettingsSnapshot
from thymira.state import SettingsConflictError

router = APIRouter(prefix="/settings", tags=["settings"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_REQUIRE_WRITE = Depends(require_permission(Permission.WRITE))
_NAMESPACE = Path(..., min_length=1, max_length=100)
# GET /settings takes no path, query or body parameters, so FastAPI has nothing to infer a 422
# from on its own (every other route documents it implicitly via such a parameter). Declare the
# same response FastAPI would generate automatically, so the operation matches the rest of the
# API surface (tests/thymira/test_api_contract.py).
_VALIDATION_ERROR_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "Validation Error",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}
        },
    }
}


@router.get(
    "",
    response_model=SettingsListResponse,
    dependencies=[_REQUIRE_READ],
    responses=_VALIDATION_ERROR_RESPONSE,
)
def list_settings(deps: RuntimeDeps = _DEPS) -> SettingsListResponse:
    """List all durable user settings snapshots in namespace order."""
    return SettingsListResponse(items=deps.settings_store.list())


@router.get(
    "/{namespace}",
    response_model=SettingsSnapshot,
    dependencies=[_REQUIRE_READ],
)
def get_settings(namespace: str = _NAMESPACE, deps: RuntimeDeps = _DEPS) -> SettingsSnapshot:
    """Return one durable user settings snapshot."""
    snapshot = deps.settings_store.get(namespace)
    if snapshot is None:
        raise problem(404, "settings_not_found", f"Settings namespace {namespace!r} was not found.")
    return snapshot


@router.put(
    "/{namespace}",
    response_model=SettingsSnapshot,
    dependencies=[_REQUIRE_WRITE],
)
def put_settings(
    payload: SettingsPutRequest,
    namespace: str = _NAMESPACE,
    deps: RuntimeDeps = _DEPS,
) -> SettingsSnapshot:
    """Commit one settings snapshot through the namespace compare-and-set boundary.

    A write to :data:`MODEL_OVERRIDES_NAMESPACE` also refreshes routing's in-process override
    cache immediately, in this same request, so the very next routed model call sees it -- no
    restart, and no per-call settings-store read on the hot path (see ``set_model_overrides``).
    """
    try:
        snapshot = deps.settings_store.put(
            namespace,
            payload.values,
            expected_revision=payload.expected_revision,
            secret_refs=payload.secret_refs,
        )
    except SettingsConflictError as exc:
        current = exc.current
        raise problem(
            409,
            "settings_conflict",
            str(exc),
            details={
                "namespace": exc.namespace,
                "expected_revision": exc.expected,
                "current_revision": 0 if current is None else current.revision,
                "current": None if current is None else current.to_json_dict(),
            },
        ) from exc
    except (TypeError, ValueError) as exc:
        raise problem(422, "invalid_settings", str(exc)) from exc
    else:
        if namespace == MODEL_OVERRIDES_NAMESPACE:
            set_model_overrides(snapshot.values)
        return snapshot


__all__ = ["router"]
