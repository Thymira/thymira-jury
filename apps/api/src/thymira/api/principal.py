"""Resolution of the actor responsible for an API request, and route authorization.

Every route declares the :class:`~thymira.api.permissions.Permission` it needs; the dependency
authenticates the request's bearer token against the composition's configured
:class:`~thymira.api.permissions.PrincipalResolver`, authorizes that permission, and stashes the
resolved principal on the request. ``resolve_actor`` then records *that* principal and nothing
else (baseline sections 19, 22).

There used to be an unverified ``X-Thymira-Actor`` header naming who was acting. It is gone: a
header any caller can set was, in practice, an identity any caller could choose, and F13.3 refuses
to let a request's own contents grant it an identity. A request body may still *assert* an actor,
but the server verifies that assertion against the authenticated principal and refuses a mismatch.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 - FastAPI inspects dependency annotations at runtime.

from thymira.api.deps import get_runtime_deps
from thymira.api.errors import problem

if TYPE_CHECKING:
    from thymira.api.permissions import Permission, Principal, PrincipalResolver
    from thymira.schemas import Actor

AUTHORIZATION_HEADER = "Authorization"
_WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}


def _authenticated_principal(request: Request) -> Principal | None:
    """Return the principal stashed by an authorization dependency, if any."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal


def resolve_actor(request: Request) -> Actor:
    """Return the authenticated principal's Actor for this request.

    There is one source of identity, so every Actor this function returns carries
    ``authenticated=True`` and the principal's real role. Nothing a caller sends can produce an
    identity: no header, no body field.

    Raises:
        RuntimeError: The request reached a handler with no authentication dependency. Unreachable
            -- :func:`thymira.api.route_table.verify_route_permissions` refuses to build such an
            application -- and it fails closed rather than quietly recording ``Actor.system()``
            for what must be a person.
    """
    principal = resolve_principal(request)
    return principal.actor()


def resolve_principal(request: Request) -> Principal:
    """Return the principal authenticated by this request's route dependency."""
    principal = _authenticated_principal(request)
    if principal is None:
        msg = (
            "the authenticated principal was requested on a route with no authentication dependency"
        )
        raise RuntimeError(msg)
    return principal


def _authenticate(request: Request, resolver: PrincipalResolver) -> Principal:
    """Resolve the request's bearer token to a principal, raising 401 when it cannot."""
    header = request.headers.get(AUTHORIZATION_HEADER)
    if header is None:
        raise problem(
            401,
            "unauthenticated",
            "A bearer token is required.",
            headers=_WWW_AUTHENTICATE,
        )
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise problem(
            401,
            "unauthenticated",
            "A bearer token is required.",
            headers=_WWW_AUTHENTICATE,
        )
    principal = resolver.resolve(token.strip())
    if principal is None:
        raise problem(
            401,
            "invalid_token",
            "The bearer token is not recognized.",
            headers=_WWW_AUTHENTICATE,
        )
    return principal


def enforce_permission(request: Request, permission: Permission) -> None:
    """Authenticate and authorize the request.

    Raises 401 for a missing or unrecognized bearer token, 403 when the authenticated principal
    lacks ``permission``, and otherwise stashes the principal for ``resolve_actor`` to record.

    There is no permissive branch. ``RuntimeDeps.principal_resolver`` is a required field, so no
    composition can be built without one -- the escape hatch is absent rather than refused by
    default, which is the difference between a control and a switch someone flips in CI (F13.3).
    """
    resolver = get_runtime_deps(request).principal_resolver
    principal = _authenticate(request, resolver)
    if not principal.allows(permission):
        raise problem(
            403,
            "forbidden",
            f"The {principal.role!r} principal is not permitted to {permission.value} here.",
        )
    request.state.principal = principal


class RequirePermission:
    """The FastAPI dependency that authorizes one :class:`Permission`.

    A class rather than a closure so the permission it enforces is readable off a route
    (:func:`thymira.api.route_table.declared_permission`), which is what lets the start-up check
    and the behavioral sweep read the application's own table instead of a hand-written copy.
    """

    __slots__ = ("permission",)

    def __init__(self, permission: Permission) -> None:
        """Bind the dependency to the permission it authorizes."""
        self.permission = permission

    def __call__(self, request: Request) -> None:
        """Authenticate and authorize one request."""
        enforce_permission(request, self.permission)

    def __repr__(self) -> str:
        """Name the permission this dependency enforces."""
        return f"RequirePermission({self.permission.value!r})"


@cache
def require_permission(permission: Permission) -> RequirePermission:
    """Return the one dependency object that authorizes ``permission``.

    Memoized on purpose: FastAPI keys ``app.dependency_overrides`` on the dependency object, so
    one object per permission is what makes the authentication dependency addressable -- the
    blindness proof for the refusal sweep overrides exactly these three objects.
    """
    return RequirePermission(permission)


__all__ = [
    "AUTHORIZATION_HEADER",
    "RequirePermission",
    "enforce_permission",
    "require_permission",
    "resolve_actor",
    "resolve_principal",
]
