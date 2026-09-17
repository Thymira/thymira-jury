"""Immutable operator snapshots for the model routes a session may use.

The router still owns model selection. This contract only describes the routes an operator has
permitted for a session, so a later configuration change cannot widen an already-created session.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from thymira.schemas.base import ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ALLOWED_MODEL_ROUTES_ENV_VAR = "THYMIRA_ALLOWED_MODEL_ROUTES"
"""Comma-separated exact model route ids supplied by the operator."""


def _canonical_snapshot(version: int, routes: Sequence[str], authority: str) -> str:
    """Return the canonical bytes used to identify one route-policy snapshot."""
    return json.dumps(
        {"authority": authority, "routes": list(routes), "version": version},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _snapshot_hash(version: int, routes: Sequence[str], authority: str) -> str:
    """Hash a normalized route-policy snapshot."""
    return hashlib.sha256(_canonical_snapshot(version, routes, authority).encode()).hexdigest()


def _normalize_routes(routes: Sequence[str]) -> tuple[str, ...]:
    """Normalize and validate exact route ids, refusing wildcard policy syntax."""
    normalized = tuple(sorted({route.strip() for route in routes}))
    if any(not route for route in normalized):
        raise ValueError("model route allowlist entries must not be empty")
    if "<unconfigured>" in normalized:
        raise ValueError("model route allowlist cannot authorize an unconfigured route")
    if any(any(marker in route for marker in ("*", "?")) for route in normalized):
        raise ValueError("model route allowlist does not support wildcard entries")
    return normalized


class ModelRoutePolicy(ThymiraModel):
    """A frozen, hash-pinned allowlist of exact provider/model route ids.

    An empty allowlist is valid and means fail closed: a session with that snapshot cannot make a
    model request until an operator creates a new explicit policy. The policy carries no model
    selection authority; ``choose`` remains the sole selector.
    """

    version: int = Field(ge=1)
    allowed_routes: tuple[str, ...] = ()
    authority: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def unavailable(cls) -> ModelRoutePolicy:
        """Return the explicit empty snapshot used when no trusted policy is available."""
        return cls.from_routes((), authority="unavailable")

    @model_validator(mode="after")
    def _valid_snapshot(self) -> ModelRoutePolicy:
        """Reject malformed, non-canonical or tampered snapshots."""
        normalized = _normalize_routes(self.allowed_routes)
        if normalized != self.allowed_routes:
            raise ValueError("model route allowlist must be sorted and unique")
        expected = _snapshot_hash(self.version, self.allowed_routes, self.authority)
        if self.sha256 != expected:
            raise ValueError("model route policy sha256 does not match its contents")
        return self

    @classmethod
    def from_routes(
        cls,
        routes: Sequence[str],
        *,
        version: int = 1,
        authority: str = "operator",
    ) -> ModelRoutePolicy:
        """Create a hash-pinned snapshot from explicit operator routes."""
        normalized = _normalize_routes(routes)
        return cls(
            version=version,
            allowed_routes=normalized,
            authority=authority,
            sha256=_snapshot_hash(version, normalized, authority),
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        version: int = 1,
        authority: str = "operator",
    ) -> ModelRoutePolicy:
        """Load exact routes from the operator environment.

        An unset variable yields an empty fail-closed snapshot. Composition roots attach this
        value to every Session so an absent operator setting can never become an implicit permit.
        """
        source = os.environ if environ is None else environ
        raw = source.get(ALLOWED_MODEL_ROUTES_ENV_VAR, "")
        routes = tuple(item.strip() for item in raw.split(",")) if raw.strip() else ()
        return cls.from_routes(routes, version=version, authority=authority)

    @property
    def routes(self) -> tuple[str, ...]:
        """Alias used by callers that refer to the allowlist as ``routes``."""
        return self.allowed_routes

    @property
    def policy_hash(self) -> str:
        """Return the immutable content hash."""
        return self.sha256

    @property
    def permitted_routes(self) -> tuple[str, ...]:
        """Return the operator-permitted routes using the session vocabulary."""
        return self.allowed_routes

    @property
    def allowed_model_routes(self) -> tuple[str, ...]:
        """Return the allowlist using the explicit model-route vocabulary."""
        return self.allowed_routes

    @classmethod
    def from_operator_config(
        cls,
        allowed_routes: Sequence[str],
        *,
        version: int = 1,
        authority: str = "operator",
    ) -> ModelRoutePolicy:
        """Create a snapshot from an explicit operator configuration value."""
        return cls.from_routes(allowed_routes, version=version, authority=authority)

    def allows(self, route: str) -> bool:
        """Return whether one exact route id is permitted by this snapshot."""
        return route in self.allowed_routes

    def narrowed_to(
        self,
        routes: Sequence[str],
        *,
        authority: str | None = None,
    ) -> ModelRoutePolicy:
        """Create a later snapshot that can only narrow this policy."""
        narrowed = _normalize_routes(routes)
        if not set(narrowed).issubset(self.allowed_routes):
            raise ValueError("a route-policy update may not widen the existing allowlist")
        return ModelRoutePolicy.from_routes(
            narrowed,
            version=self.version + 1,
            authority=authority or self.authority,
        )

    def narrowed_by(self, current: ModelRoutePolicy) -> ModelRoutePolicy:
        """Apply a later policy as a restriction without widening this snapshot.

        A current policy may revoke routes from a bound Run, but it can never add a route that was
        absent when the Run was published. Returning ``self`` when no restriction is needed keeps
        the durable identity hash stable across an unchanged restart.
        """
        permitted = tuple(route for route in self.allowed_routes if current.allows(route))
        if permitted == self.allowed_routes:
            return self
        return self.narrowed_to(permitted, authority=current.authority)


__all__ = ["ALLOWED_MODEL_ROUTES_ENV_VAR", "ModelRoutePolicy"]
