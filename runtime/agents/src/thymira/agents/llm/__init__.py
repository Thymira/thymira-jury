"""The LLM provider contract, its implementations and the model router for Thymira agents.

LiteLLM is the only production implementation; the model identifier selects the real
provider, so there is no per-provider branch. Models are configuration, never names in the
code (ADR-0003), and they are *routed by code* (ADR-0004): an agent declares the kind of task,
:mod:`thymira.agents.llm.routing` applies the tier table and the per-role floors, and the
resulting :class:`ModelChoice` is recorded as a ``model.selected`` event.

- ``provider_for(role, task)`` — the primary API: a provider plus the choice to record;
- ``get_provider(role=...)`` — the coarse two-tier shortcut (orchestrator / agent);
- :class:`ScriptedProvider` — a deterministic double for tests.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Collection

from thymira.agents.llm.base import (
    BaseModelT,
    LLMCallError,
    LLMConfigurationError,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
)
from thymira.agents.llm.litellm_provider import (
    MODEL_ENV_VAR,
    REASONING_EFFORT_ENV_VAR,
    LiteLLMProvider,
)
from thymira.agents.llm.proxy import resolve_proxy_endpoint
from thymira.agents.llm.routing import (
    DEFAULT_TIER_BY_TASK,
    ModelChoice,
    ModelTier,
    Role,
    TaskKind,
    choose,
    provider_for,
)
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.route_policy import enforce_model_route

if TYPE_CHECKING:
    from thymira.schemas import ModelRoutePolicy

ModelRole = Literal["orchestrator", "agent"]
"""Coarse tier: an orchestrator (THY, MIRA) or one of their sub-agents."""

ROLE_ENV_VARS: dict[str, str] = {
    "orchestrator": "THYMIRA_ORCHESTRATOR_MODEL",
    "agent": "THYMIRA_AGENT_MODEL",
}


def resolve_model(role: ModelRole, *, model: str | None = None) -> str | None:
    """Pick the model id for a coarse ``role``: explicit, the role's variable, the fallback.

    Returns ``None`` when nothing is configured so the caller can raise a precise error.
    """
    if model:
        return model
    for variable in (ROLE_ENV_VARS[role], MODEL_ENV_VAR):
        value = os.getenv(variable, "").strip()
        if value:
            return value
    return None


def get_provider(
    *,
    model: str | None = None,
    role: ModelRole = "agent",
    proxy_endpoint: str | None = None,
    declared_safe_endpoints: Collection[str] = (),
    route_policy: ModelRoutePolicy | None = None,
) -> LLMProvider:
    """Return the production ``LLMProvider`` for a coarse role (see ``provider_for`` for tiers).

    Args:
        model: An explicit model identifier; overrides the environment.
        role: ``"orchestrator"`` (THY/MIRA, frontier tier) or ``"agent"`` (sub-agents).
        proxy_endpoint: An explicit endpoint for the LiteLLM gateway. It is refused unless it is
            an exact member of ``declared_safe_endpoints``.
        declared_safe_endpoints: Runtime-owned endpoint allowlist for ``proxy_endpoint``.
        route_policy: Immutable Run-bound allowlist checked before provider creation.

    Raises:
        LLMConfigurationError: If no model is configured for the role or LiteLLM is missing.
    """
    resolved = resolve_model(role, model=model)
    if resolved is None:
        msg = (
            f"No model configured for role {role!r}. Set {ROLE_ENV_VARS[role]} (or the "
            f"{MODEL_ENV_VAR} fallback) or pass model=..."
        )
        raise LLMConfigurationError(msg)
    routing_role = Role.THY if role == "orchestrator" else Role.AGENT
    choice = choose(routing_role, "classify", model=resolved)
    enforce_model_route(choice, route_policy)
    return LiteLLMProvider(
        model=resolved,
        proxy_endpoint=proxy_endpoint,
        declared_safe_endpoints=declared_safe_endpoints,
    )


__all__ = [
    "DEFAULT_TIER_BY_TASK",
    "MODEL_ENV_VAR",
    "REASONING_EFFORT_ENV_VAR",
    "ROLE_ENV_VARS",
    "BaseModelT",
    "LLMCallError",
    "LLMConfigurationError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMStructuredOutputError",
    "LLMTimeoutError",
    "LLMToolCall",
    "LLMToolDefinition",
    "LiteLLMProvider",
    "ModelChoice",
    "ModelRole",
    "ModelTier",
    "Role",
    "ScriptedProvider",
    "TaskKind",
    "choose",
    "get_provider",
    "resolve_model",
    "resolve_proxy_endpoint",
]
