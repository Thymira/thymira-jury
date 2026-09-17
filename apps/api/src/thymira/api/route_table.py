"""The application's own route inventory, and the start-up check that every route is guarded.

F13.3 requires that *endpoint names never grant authority*. A declaration on each handler is not
enough on its own: a route added later without one would simply serve anonymously, and no test
over a hand-written list would notice. :func:`verify_route_permissions` runs at composition time
and refuses to build an application that would serve such a route, and :func:`iter_api_routes` is
the inventory the behavioral sweep is parametrized over -- the application's own table, not a
copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi.routing import APIRoute
from starlette.routing import Host, Mount, WebSocketRoute

from thymira.api.principal import RequirePermission

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from starlette.routing import BaseRoute

    from thymira.api.permissions import Permission

UNAUTHENTICATED_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/healthz")})
"""The one operation served without a credential.

Liveness, for a container runtime, a load balancer and ``scripts/real_e2e_smoke.py``'s own
``_wait_for_health``. It reads no state, names no Run and grants no authority; its entire response
is ``{"status": "ok"}``. Widening this set is a deliberate edit to this constant, reviewed here,
never a parameter a caller can pass.
"""


@dataclass(frozen=True, slots=True)
class ServedRoute:
    """One leaf route and the path at which the application serves it."""

    route: BaseRoute
    path: str


def _join_path(prefix: str, path: str | None) -> str:
    """Join a Starlette mount or router prefix to a child path."""
    if not path:
        return prefix or "/"
    if not prefix:
        return path
    if path == "/":
        return f"{prefix.rstrip('/')}/"
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _route_path(route: BaseRoute, prefix: str) -> str:
    """Return a route's effective path, including a host marker when it has no URL path."""
    path = getattr(route, "path", None)
    if path is None and isinstance(route, Host):
        path = f"<host:{route.host}>"
    return _join_path(prefix, path)


def _nested_routes(route: BaseRoute) -> list[BaseRoute] | None:
    """Return introspectable children for a router, mount or host application."""
    nested = getattr(route, "routes", None)
    if nested:
        return list(nested)
    app = getattr(route, "app", None)
    nested = getattr(app, "routes", None)
    if nested:
        return list(nested)
    return None


def _iter_served_routes(routes: list[BaseRoute], prefix: str = "") -> Iterator[ServedRoute]:
    """Walk every route shape down to leaves while preserving effective mount prefixes.

    FastAPI wraps an included router in an ``_IncludedRouter`` whose real routes hang off
    ``original_router``; a ``Mount`` exposes ``routes`` directly. A mount around an opaque ASGI app
    has no children, but it still handles requests and is yielded as an ``ASGI`` operation so it
    cannot disappear from the authorization inventory.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            include_prefix = getattr(context, "prefix", "")
            yield from _iter_served_routes(
                list(included.routes), _join_path(prefix, include_prefix)
            )
            continue

        nested = _nested_routes(route)
        if nested is not None:
            yield from _iter_served_routes(nested, _route_path(route, prefix))
            continue

        # Mount and Host can dispatch an opaque ASGI application even when there is no route list
        # to inspect. A route-like object with a path is also retained so a future Starlette route
        # type cannot silently create an unclassified endpoint.
        if isinstance(route, (Mount, Host)) or getattr(route, "path", None) is not None:
            yield ServedRoute(route, _route_path(route, prefix))


def _leaf_routes(routes: list[BaseRoute]) -> Iterator[BaseRoute]:
    """Walk a router tree down to route objects, retaining compatibility for internal callers."""
    for entry in _iter_served_routes(routes):
        yield entry.route


def iter_served_routes(app: FastAPI) -> Iterator[ServedRoute]:
    """Yield every route the application can serve, including opaque and WebSocket routes."""
    yield from _iter_served_routes(list(app.routes))


def iter_api_routes(app: FastAPI) -> Iterator[APIRoute]:
    """Yield every :class:`APIRoute` the application serves, through included routers."""
    for entry in iter_served_routes(app):
        if isinstance(entry.route, APIRoute):
            yield entry.route


def declared_permission(route: BaseRoute) -> Permission | None:
    """Return the :class:`Permission` a route declares, or ``None`` when it declares none."""
    if not isinstance(route, APIRoute):
        return None
    for dependency in route.dependencies:
        if isinstance(dependency.dependency, RequirePermission):
            return dependency.dependency.permission
    return None


def route_operations(route: BaseRoute, *, path: str | None = None) -> Iterator[tuple[str, str]]:
    """Yield the ``(method, path)`` pairs one served route answers.

    WebSockets and opaque ASGI mounts do not expose HTTP methods. They receive explicit operation
    labels so the startup verifier reports them instead of skipping them as an empty route.
    """
    effective_path = path if path is not None else getattr(route, "path", None)
    if effective_path is None:
        return
    if isinstance(route, WebSocketRoute):
        yield ("WEBSOCKET", effective_path)
        return
    methods = getattr(route, "methods", None)
    if not methods:
        yield ("ASGI", effective_path)
        return
    for method in methods:
        yield (str(method).upper(), effective_path)


def _is_fixed_liveness_route(app: FastAPI, entry: ServedRoute, operation: tuple[str, str]) -> bool:
    """Return whether an operation is the exact stateless liveness handler owned by this app."""
    endpoint = getattr(entry.route, "endpoint", None)
    expected = getattr(app.state, "public_liveness_endpoint", None)
    return (
        operation in UNAUTHENTICATED_ROUTES
        and isinstance(entry.route, APIRoute)
        and endpoint is expected
    )


def verify_route_permissions(app: FastAPI) -> None:
    """Refuse an application that would serve any route without a declared permission.

    Raises:
        RuntimeError: A route is neither in :data:`UNAUTHENTICATED_ROUTES` nor declaring a
            :class:`Permission`. The message names every offending method and path, because the
            fix is to add the declaration to that exact handler.
    """
    unguarded: list[str] = []
    for entry in iter_served_routes(app):
        guarded = declared_permission(entry.route) is not None
        for operation in route_operations(entry.route, path=entry.path):
            if guarded or _is_fixed_liveness_route(app, entry, operation):
                continue
            method, path = operation
            unguarded.append(f"{method} {path}")
    if unguarded:
        listed = ", ".join(sorted(unguarded))
        msg = (
            f"these routes declare no Permission and are not in UNAUTHENTICATED_ROUTES: {listed}; "
            "add dependencies=[Depends(require_permission(Permission.<...>))] to each handler"
        )
        raise RuntimeError(msg)


__all__ = [
    "UNAUTHENTICATED_ROUTES",
    "ServedRoute",
    "declared_permission",
    "iter_api_routes",
    "iter_served_routes",
    "route_operations",
    "verify_route_permissions",
]
