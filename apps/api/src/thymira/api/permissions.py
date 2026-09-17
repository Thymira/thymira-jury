"""Route permissions and bearer-token principal resolution for the API (RA-API-10).

RA-API-10 fills the ``resolve_actor`` seam opened by RA-API-08: a configured
:class:`PrincipalResolver` turns a bearer token into an authenticated :class:`Principal` with a
role, and each route declares the :class:`Permission` it needs. The application composition
requires a resolver, so there is no anonymous or loopback escape when this boundary is enabled.
Only a resolved ``Principal`` is recorded as an authenticated identity. A ``Principal`` never
authorizes a domain action itself; it only decides whether a request may reach the deterministic
runtime at all (baseline sections 19, 22: least privilege, zero trust).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from thymira.schemas import Actor, ActorKind

if TYPE_CHECKING:
    from collections.abc import Mapping


class Permission(StrEnum):
    """A capability a route requires of the acting principal (baseline section 22)."""

    READ = "read"
    WRITE = "write"
    APPROVE = "approve"


# Baseline section 22 permission separation, mapped to the human principals that reach the API.
# THY and MIRA are agents inside the runtime, never HTTP principals; the roles here are the people
# who drive and govern a run.
ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = {
    "admin": frozenset(Permission),
    "data-scientist": frozenset({Permission.READ, Permission.WRITE}),
    "risk-officer": frozenset({Permission.READ, Permission.APPROVE}),
    "viewer": frozenset({Permission.READ}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller: an id, a role and the permissions the role grants."""

    id: str
    role: str
    permissions: frozenset[Permission]
    kind: ActorKind = ActorKind.HUMAN

    def actor(self) -> Actor:
        """Return the authenticated Actor recorded on this caller's runtime work."""
        return Actor(kind=self.kind, id=self.id, role=self.role, authenticated=True)

    def allows(self, permission: Permission) -> bool:
        """Whether this principal may perform an action that needs ``permission``."""
        return permission in self.permissions


def principal_for_role(
    actor_id: str,
    role: str,
    *,
    kind: ActorKind = ActorKind.HUMAN,
) -> Principal:
    """Build a :class:`Principal` for a known role.

    Args:
        actor_id: The authenticated identity recorded on the principal's events.
        role: One of :data:`ROLE_PERMISSIONS`.
        kind: The actor kind; humans by default, a service account otherwise.

    Raises:
        ValueError: ``role`` is not a known role.
    """
    try:
        permissions = ROLE_PERMISSIONS[role]
    except KeyError as exc:
        msg = f"unknown role: {role!r}"
        raise ValueError(msg) from exc
    return Principal(id=actor_id, role=role, permissions=permissions, kind=kind)


@runtime_checkable
class PrincipalResolver(Protocol):
    """Resolve a bearer token to an authenticated principal (the ``resolve_actor`` seam).

    FINAL swaps a JWT/OIDC verifier behind this same interface; the routes and the composition root
    do not change.
    """

    def resolve(self, token: str) -> Principal | None:
        """Return the Principal a bearer token authenticates, or ``None`` if unrecognized."""
        ...


class BearerTokenRegistry:
    """A :class:`PrincipalResolver` over a fixed token table (the MVP FINAL resolver)."""

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        self._tokens = dict(tokens)

    def resolve(self, token: str) -> Principal | None:
        """Return the Principal bound to ``token``, or ``None`` for an unknown token."""
        return self._tokens.get(token)

    def __repr__(self) -> str:
        """Describe the registry by size only.

        The generated ``repr`` would print every plaintext token, and this object is a field of
        :class:`~thymira.api.deps.RuntimeDeps`, a dataclass whose own generated ``repr`` reaches
        any log line or traceback that formats the dependency graph.
        """
        return f"BearerTokenRegistry({len(self._tokens)} tokens)"


__all__ = [
    "ROLE_PERMISSIONS",
    "BearerTokenRegistry",
    "Permission",
    "Principal",
    "PrincipalResolver",
    "principal_for_role",
]
