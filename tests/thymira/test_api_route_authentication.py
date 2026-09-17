"""Integration tests for F13.3's authentication leg: every API route needs a real credential.

Real: the FastAPI application, its own route table, the local runtime dependency graph over
``tmp_path`` and -- for the oracle -- the production ``build_local_app`` composition root and the
``events.jsonl`` it writes. Faked: the execution dispatcher, so nothing here invokes a model.

The sweep is parametrized over the application's OWN route table rather than a hand-written list:
a route added later is swept automatically, and one that declares no permission fails both
``create_app`` and :func:`test_every_route_declares_a_permission_or_is_the_health_check`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.routing import APIRoute

from tests.thymira.api_support import api_credential
from thymira.api.app import create_app
from thymira.api.deps import build_default_deps
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.route_table import (
    UNAUTHENTICATED_ROUTES,
    declared_permission,
    iter_api_routes,
    iter_served_routes,
    route_operations,
    verify_route_permissions,
)
from thymira.schemas import Id, new_id

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one create dispatch."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Record one resume dispatch."""
        del run_id


def _workspace(tmp_path: Path) -> Path:
    """Create a minimal configured project workspace."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _app(tmp_path: Path) -> FastAPI:
    """Build the real application over a configured workspace."""
    _, credential = api_credential()
    return create_app(
        build_default_deps(
            tmp_path / "runtime",
            workspace=_workspace(tmp_path),
            dispatcher=_RecordingDispatcher(),
            principal_resolver=credential,
        )
    )


def test_the_route_enumerator_sees_every_documented_operation(tmp_path: Path) -> None:
    """The enumerator's inventory equals FastAPI's own, computed by a separate code path.

    Load-bearing. On FastAPI 0.141 an included router appears in ``app.routes`` as an
    ``_IncludedRouter`` wrapper whose real routes hang off ``original_router``; a naive
    ``isinstance(route, APIRoute)`` walk of ``app.routes`` finds exactly one route (``/healthz``)
    and every sweep below would pass vacuously over it. This equality is what keeps the sweep
    honest, and turns a FastAPI upgrade that changes that internal shape into a loud failure.
    """
    app = _app(tmp_path)

    enumerated = {
        operation for route in iter_api_routes(app) for operation in route_operations(route)
    }
    documented = {
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }

    assert enumerated == documented
    assert len(enumerated) > 20


def test_every_route_declares_a_permission_or_is_the_health_check(tmp_path: Path) -> None:
    """The only route that may serve an anonymous caller is the liveness probe."""
    app = _app(tmp_path)

    anonymous = {
        operation
        for route in iter_api_routes(app)
        if declared_permission(route) is None
        for operation in route_operations(route)
    }

    assert anonymous == set(UNAUTHENTICATED_ROUTES)
    assert frozenset({("GET", "/healthz")}) == UNAUTHENTICATED_ROUTES


def test_every_guarded_http_operation_rejects_a_missing_credential(tmp_path: Path) -> None:
    """The route inventory's guards are exercised through every served HTTP operation.

    This is intentionally driven by the application's own route table. A new guarded endpoint
    joins the refusal sweep automatically, while a route accidentally added without a dependency
    either fails app construction or returns a non-401 response here.
    """
    from fastapi.testclient import TestClient

    app = _app(tmp_path)
    client = TestClient(app)
    seen: set[tuple[str, str]] = set()
    for entry in iter_served_routes(app):
        if not isinstance(entry.route, APIRoute):
            continue
        for method, path in route_operations(entry.route, path=entry.path):
            if (method, path) in UNAUTHENTICATED_ROUTES:
                continue
            assert declared_permission(entry.route) is not None
            probe_path = path.replace("{run_id}", new_id("run"))
            probe_path = probe_path.replace("{decision_id}", new_id("decision"))
            probe_path = probe_path.replace("{name}", "missing-tool")
            probe_path = probe_path.replace("{artifact_id}", new_id("artifact"))
            body: dict[str, object] | None = None
            if method == "POST":
                if path == "/runs":
                    body = {"prompt": "authentication probe"}
                elif path.endswith("/delegate"):
                    body = {"agent": "data-agent", "objective": "authentication probe"}
                elif path.endswith("/risk-interview"):
                    body = {"answer": "authentication probe"}
                else:
                    body = {}
            response = client.request(method, probe_path, json=body)
            assert response.status_code == 401, f"{method} {path}: {response.text}"
            assert response.json()["code"] == "unauthenticated"
            seen.add((method, path))

    assert len(seen) > 20


def test_create_app_refuses_a_router_whose_route_declares_no_permission(tmp_path: Path) -> None:
    """A route added without a permission fails the process instead of serving anonymously."""
    app = _app(tmp_path)
    router = APIRouter()

    @router.get("/leak")
    def leak() -> dict[str, str]:
        """Serve a value to whoever asks."""
        return {"leaked": "yes"}

    app.include_router(router)

    with pytest.raises(RuntimeError, match="GET /leak"):
        verify_route_permissions(app)


def test_a_starlette_route_that_takes_no_dependencies_is_refused_too() -> None:
    """A plain Starlette route cannot carry a permission, so it cannot be served either."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def handler(request: Request) -> Response:  # pragma: no cover - never invoked.
        del request
        return Response()

    app.router.add_route("/raw", handler, methods=["GET"])

    with pytest.raises(RuntimeError, match="GET /raw"):
        verify_route_permissions(app)


def test_the_interactive_documentation_routes_are_not_served(tmp_path: Path) -> None:
    """``/docs``, ``/redoc`` and ``/openapi.json`` would disclose the surface anonymously."""
    app = _app(tmp_path)

    served = {getattr(route, "path", None) for route in app.routes}

    assert "/openapi.json" not in served
    assert "/docs" not in served
    assert "/redoc" not in served
    assert app.openapi()["paths"], "the in-process schema must still be buildable"


def test_the_permission_dependency_is_memoized_per_permission() -> None:
    """One dependency object per permission, so an override can address it by identity."""
    assert require_permission(Permission.READ) is require_permission(Permission.READ)
    assert require_permission(Permission.READ) is not require_permission(Permission.WRITE)


def test_declared_permission_reads_the_permission_off_a_real_route(tmp_path: Path) -> None:
    """The inventory reports the permission each route actually declares."""
    app = _app(tmp_path)

    by_operation = {
        operation: declared_permission(route)
        for route in iter_api_routes(app)
        for operation in route_operations(route)
    }

    assert by_operation[("POST", "/runs")] is Permission.WRITE
    assert by_operation[("GET", "/runs")] is Permission.READ
    assert by_operation[("POST", "/runs/{run_id}/approve")] is Permission.APPROVE
    assert by_operation[("GET", "/healthz")] is None


def test_the_enumerator_walks_included_routers_rather_than_the_flat_table(tmp_path: Path) -> None:
    """The naive walk this enumerator replaces would see one route; state the gap outright."""
    app = _app(tmp_path)

    flat = [route for route in app.routes if isinstance(route, APIRoute)]

    assert len(flat) == 1
    assert len(list(iter_api_routes(app))) > len(flat)


def test_an_unguarded_route_inside_a_prefixed_mount_names_its_effective_path() -> None:
    """A mounted route is checked under the prefix the server actually dispatches."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mounted = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @mounted.get("/leak")
    def leak() -> dict[str, str]:
        """Serve a value to whoever asks."""
        return {"leaked": "yes"}

    app.mount("/v1", mounted)

    with pytest.raises(RuntimeError, match="GET /v1/leak"):
        verify_route_permissions(app)


def test_the_served_route_inventory_preserves_a_guarded_mount_prefix() -> None:
    """The behavioral inventory exposes the same effective path used by the verifier."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mounted = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mounted.add_api_route(
        "/read",
        lambda: {"ok": True},
        methods=["GET"],
        dependencies=[Depends(require_permission(Permission.READ))],
    )
    app.mount("/v1", mounted)

    served = {
        operation
        for entry in iter_served_routes(app)
        for operation in route_operations(entry.route, path=entry.path)
    }

    assert ("GET", "/v1/read") in served


def test_a_mounted_health_path_is_not_the_fixed_liveness_exception() -> None:
    """Only the application's own exact liveness handler may be public."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mounted = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @mounted.get("/healthz")
    def mounted_healthz() -> dict[str, str]:
        """Expose a lookalike health endpoint from a mounted application."""
        return {"status": "ok"}

    app.mount("/v1", mounted)

    with pytest.raises(RuntimeError, match="GET /v1/healthz"):
        verify_route_permissions(app)


def test_a_lookalike_health_route_without_the_composition_marker_is_refused() -> None:
    """A path and name alone cannot opt a newly added handler into public liveness."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def lookalike_healthz() -> dict[str, str]:
        """Serve a lookalike response from an unrelated app."""
        return {"status": "ok"}

    with pytest.raises(RuntimeError, match="GET /healthz"):
        verify_route_permissions(app)


def test_a_websocket_route_is_classified_and_refused_without_http_permission() -> None:
    """A WebSocket cannot disappear merely because it has no HTTP methods attribute."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def socket_handler(websocket: object) -> None:
        """Handle a test WebSocket."""
        del websocket

    app.add_api_websocket_route("/socket", socket_handler)

    with pytest.raises(RuntimeError, match="WEBSOCKET /socket"):
        verify_route_permissions(app)


def test_an_opaque_custom_asgi_mount_is_classified_and_refused() -> None:
    """A mount with no introspectable child routes is still a served authority boundary."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def custom_asgi(scope: object, receive: object, send: object) -> None:
        """Handle a test ASGI scope."""
        del scope, receive, send

    app.mount("/custom", custom_asgi)

    with pytest.raises(RuntimeError, match="ASGI /custom"):
        verify_route_permissions(app)
