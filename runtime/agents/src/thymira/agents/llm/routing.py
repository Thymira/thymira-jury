"""Model routing: the model proposes a kind of task, the router chooses the model.

Three tiers — ``FRONTIER`` for reasoning that shapes the run, ``STANDARD`` for day-to-day
work, ``FAST`` for mechanical steps (choosing a tool, summarising, extracting, classifying).
Each orchestrator (THY, MIRA) can run on its own frontier model; their sub-agents run on the
cheaper tiers. Nothing here names a vendor or a model: every id comes from the environment.

An agent never picks a model id. It declares the *kind of task* (and may request a tier); the
router applies the tier table and the per-role floors and returns a :class:`ModelChoice` that
the caller records as a ``model.selected`` event — the choice is evidence, not a whim.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from thymira.agents.llm.litellm_provider import MODEL_ENV_VAR, LiteLLMProvider
from thymira.agents.route_policy import enforce_model_route
from thymira.schemas import ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from thymira.schemas import ModelRoutePolicy


class ModelTier(StrEnum):
    """Capability tiers, cheapest first; comparison follows ``RANK``."""

    FAST = "FAST"
    STANDARD = "STANDARD"
    FRONTIER = "FRONTIER"


RANK: dict[ModelTier, int] = {ModelTier.FAST: 0, ModelTier.STANDARD: 1, ModelTier.FRONTIER: 2}


class Role(StrEnum):
    """Who is asking: one of the two orchestrators or a sub-agent."""

    THY = "thy"
    MIRA = "mira"
    AGENT = "agent"


TaskKind = Literal[
    "plan",
    "decide",
    "synthesize",
    "audit_judgement",
    "analyze",
    "code",
    "review",
    "select_tool",
    "summarize",
    "extract",
    "classify",
    "format",
]

DEFAULT_TIER_BY_TASK: dict[str, ModelTier] = {
    "plan": ModelTier.FRONTIER,
    "decide": ModelTier.FRONTIER,
    "synthesize": ModelTier.FRONTIER,
    "audit_judgement": ModelTier.FRONTIER,
    "analyze": ModelTier.STANDARD,
    "code": ModelTier.STANDARD,
    "review": ModelTier.STANDARD,
    "select_tool": ModelTier.FAST,
    "summarize": ModelTier.FAST,
    "extract": ModelTier.FAST,
    "classify": ModelTier.FAST,
    "format": ModelTier.FAST,
}
"""Which tier a kind of task deserves by default; overridable per request, subject to floors."""

TIER_ENV_VARS: dict[ModelTier, str] = {
    ModelTier.FRONTIER: "THYMIRA_MODEL_FRONTIER",
    ModelTier.STANDARD: "THYMIRA_MODEL_STANDARD",
    ModelTier.FAST: "THYMIRA_MODEL_FAST",
}
ROLE_MODEL_ENV_VARS: dict[Role, str] = {
    Role.THY: "THYMIRA_THY_MODEL",
    Role.MIRA: "THYMIRA_MIRA_MODEL",
    Role.AGENT: "THYMIRA_AGENT_MODEL",
}
ORCHESTRATOR_MODEL_ENV_VAR = "THYMIRA_ORCHESTRATOR_MODEL"
UNCONFIGURED_MODEL = "<unconfigured>"
"""The choice recorded when no model is configured for the applied tier.

It names no provider route, so it is a configuration state rather than something a session
route snapshot can allow or deny.
"""
# A floor is a governance minimum: these variables may only raise one (see `floor_for`).
ROLE_FLOOR_ENV_VARS: dict[Role, str] = {
    Role.THY: "THYMIRA_THY_MIN_TIER",
    Role.MIRA: "THYMIRA_MIRA_MIN_TIER",
    Role.AGENT: "THYMIRA_AGENT_MIN_TIER",
}
DEFAULT_FLOORS: dict[Role, ModelTier] = {
    # MIRA's judgement must never be cheaper than the work it audits.
    Role.MIRA: ModelTier.STANDARD,
    Role.THY: ModelTier.FAST,
    Role.AGENT: ModelTier.FAST,
}


class ModelChoice(ThymiraModel):
    """The router's decision for one call — recorded as a ``model.selected`` event."""

    role: Role
    task: str
    tier_requested: ModelTier
    tier_applied: ModelTier
    model: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    def event_payload(self) -> dict[str, str]:
        """The redaction-safe payload for the ``model.selected`` event."""
        return {
            "role": self.role.value,
            "task": self.task,
            "tier_requested": self.tier_requested.value,
            "tier_applied": self.tier_applied.value,
            "model": self.model,
            "reason": self.reason,
        }


MODEL_OVERRIDES_NAMESPACE = "models"
"""The durable settings namespace a live override is read from and written to.

Owned here, not in ``apps/api``, so the one place that knows what these keys mean also names
where they live.
"""

_model_overrides: dict[str, str] = {}
"""An in-process cache of live, settings-backed overrides, consulted before the environment.

Deliberately not a callback into the settings store: `_env` runs on every routed model call, and
the store's own `get()` takes a cross-process file lock on every read (by design, so a concurrent
writer is never missed) -- paying that on every routing decision would trade a config knob for
real latency under concurrency. The composition root calls :func:`set_model_overrides` once at
startup and again, in the same process, the instant a write to :data:`MODEL_OVERRIDES_NAMESPACE`
succeeds, so this cache is never more than one write behind within one process. A second process
does not see another process's live override without restarting -- an accepted limit for a
single-operator local tool, not a distributed one.
"""


def set_model_overrides(overrides: Mapping[str, str]) -> None:
    """Replace the live model-choice override cache the composition root maintains.

    An override only changes what :func:`_env` returns for a matching ``THYMIRA_*`` variable name;
    it never bypasses :func:`thymira.agents.route_policy.enforce_model_route`, which still runs on
    whatever model this resolves to, exactly as it does for an environment-configured one. An
    unrecognised key is silently inert -- nothing here ever looks it up -- so this accepts whatever
    JSON mapping the durable settings store holds without needing its own schema.
    """
    # Mutate the existing dict in place rather than rebinding the module name: `_env` (elsewhere
    # in this module) holds no reference of its own, but `global` here would still invite a
    # reader to wonder whether some other holder of the old dict is now stale. There is none.
    _model_overrides.clear()
    _model_overrides.update({str(key): str(value) for key, value in overrides.items()})


def _env(name: str) -> str | None:
    override = _model_overrides.get(name, "").strip()
    if override:
        return override
    value = os.getenv(name, "").strip()
    return value or None


def requested_floor(role: Role) -> ModelTier | None:
    """The floor an operator asked for in ``THYMIRA_<ROLE>_MIN_TIER``, or ``None`` if unset.

    Raises:
        ValueError: The variable is set to something that is not a :class:`ModelTier`.
    """
    raw = _env(ROLE_FLOOR_ENV_VARS[role])
    return ModelTier(raw.upper()) if raw else None


def floor_for(role: Role) -> ModelTier:
    """The lowest tier a role may run on: the stricter of the env floor and the role default.

    ``THYMIRA_<ROLE>_MIN_TIER`` may only *raise* a floor. A value below
    :data:`DEFAULT_FLOORS` is refused and the default stands — MIRA's judgement must never be
    cheaper than the work it audits, and configuration may harden a governance floor, never
    soften one. :func:`choose` records a refused override in the ``model.selected`` reason.
    """
    default = DEFAULT_FLOORS[role]
    requested = requested_floor(role)
    if requested is None or RANK[requested] <= RANK[default]:
        return default
    return requested


def tier_for(task: str, *, requested: ModelTier | None = None) -> ModelTier:
    """The tier the table assigns to ``task`` unless the caller requested one."""
    if requested is not None:
        return requested
    try:
        return DEFAULT_TIER_BY_TASK[task]
    except KeyError as exc:
        msg = f"unknown task kind {task!r}; known: {sorted(DEFAULT_TIER_BY_TASK)}"
        raise ValueError(msg) from exc


def model_for(role: Role, tier: ModelTier) -> str | None:
    """Resolve the model id for ``(role, tier)`` from the environment, most specific first.

    - FRONTIER for an orchestrator: ``THYMIRA_THY_MODEL`` / ``THYMIRA_MIRA_MODEL`` →
      ``THYMIRA_ORCHESTRATOR_MODEL`` → ``THYMIRA_MODEL_FRONTIER`` → ``THYMIRA_MODEL``;
    - FRONTIER for a sub-agent: ``THYMIRA_MODEL_FRONTIER`` → ``THYMIRA_ORCHESTRATOR_MODEL`` →
      ``THYMIRA_MODEL``;
    - STANDARD: ``THYMIRA_MODEL_STANDARD`` → ``THYMIRA_AGENT_MODEL`` → ``THYMIRA_MODEL``;
    - FAST: ``THYMIRA_MODEL_FAST`` → ``THYMIRA_MODEL_STANDARD`` → ``THYMIRA_AGENT_MODEL`` →
      ``THYMIRA_MODEL``.
    """
    if tier is ModelTier.FRONTIER:
        chain = (
            [ROLE_MODEL_ENV_VARS[role], ORCHESTRATOR_MODEL_ENV_VAR, TIER_ENV_VARS[tier]]
            if role is not Role.AGENT
            else [TIER_ENV_VARS[tier], ORCHESTRATOR_MODEL_ENV_VAR]
        )
    elif tier is ModelTier.STANDARD:
        chain = [TIER_ENV_VARS[tier], ROLE_MODEL_ENV_VARS[Role.AGENT]]
    else:
        chain = [
            TIER_ENV_VARS[tier],
            TIER_ENV_VARS[ModelTier.STANDARD],
            ROLE_MODEL_ENV_VARS[Role.AGENT],
        ]
    for variable in (*chain, MODEL_ENV_VAR):
        value = _env(variable)
        if value:
            return value
    return None


def _floor_override_note(role: Role) -> str:
    """How ``THYMIRA_<ROLE>_MIN_TIER`` was honoured, for the ``model.selected`` reason.

    Empty when no override is configured. A *refused* override — one below the role default —
    is named loudly: an operator attempt to soften a governance floor must leave more evidence
    than an accepted one, not less.
    """
    requested = requested_floor(role)
    if requested is None:
        return ""
    variable = ROLE_FLOOR_ENV_VARS[role]
    default = DEFAULT_FLOORS[role]
    if RANK[requested] > RANK[default]:
        return f"; {variable}={requested} raised the {role} floor (default {default})"
    if RANK[requested] == RANK[default]:
        return f"; {variable}={requested} matches the {role} floor"
    return f"; {variable}={requested} REFUSED: below the mandatory {role} floor {default}"


def choose(
    role: Role,
    task: str,
    *,
    requested_tier: ModelTier | None = None,
    model: str | None = None,
) -> ModelChoice:
    """Decide the model for one call: table → requested tier → role floor → environment.

    Args:
        role: Who is calling (``thy``, ``mira`` or a sub-agent).
        task: The kind of work (a key of :data:`DEFAULT_TIER_BY_TASK`).
        requested_tier: A tier the caller proposes (cheaper for a mechanical step, or
            higher when the stakes warrant it); floors still apply.
        model: An explicit model id; bypasses the environment but still records the tiers.

    Raises:
        ValueError: Unknown task kind.
        LLMConfigurationError: No model configured for the applied tier (raised by the
            provider when built; ``model`` is ``None`` here is reported as ``reason``).
    """
    tier = tier_for(task, requested=requested_tier)
    floor = floor_for(role)
    applied = tier if RANK[tier] >= RANK[floor] else floor
    if applied is not tier:
        reason = f"{task}: requested {tier}, raised to the {role} floor {floor}"
    elif requested_tier is not None and requested_tier is not DEFAULT_TIER_BY_TASK.get(task):
        reason = f"{task}: caller requested {tier} (table default {DEFAULT_TIER_BY_TASK[task]})"
    else:
        reason = f"{task}: tier table"
    reason += _floor_override_note(role)
    resolved = model or model_for(role, applied)
    return ModelChoice(
        role=role,
        task=task,
        tier_requested=tier,
        tier_applied=applied,
        model=resolved or UNCONFIGURED_MODEL,
        reason=reason if resolved else f"{reason}; no model configured for {applied}",
    )


def provider_for(
    role: Role,
    task: str,
    *,
    requested_tier: ModelTier | None = None,
    model: str | None = None,
    proxy_endpoint: str | None = None,
    declared_safe_endpoints: Collection[str] = (),
    route_policy: ModelRoutePolicy | None = None,
) -> tuple[LiteLLMProvider, ModelChoice]:
    """The production provider for one call plus the choice to record as evidence.

    An explicit proxy endpoint is resolved by :class:`LiteLLMProvider` against the same
    runtime-owned allowlist used by the other provider factories. ``route_policy`` is checked
    before constructing the production provider. The returned choice remains evidence; it never
    grants model authorization.
    """
    choice = choose(role, task, requested_tier=requested_tier, model=model)
    enforce_model_route(choice, route_policy)
    resolved = None if choice.model == UNCONFIGURED_MODEL else choice.model
    return (
        LiteLLMProvider(
            model=resolved,
            proxy_endpoint=proxy_endpoint,
            declared_safe_endpoints=declared_safe_endpoints,
        ),
        choice,
    )


__all__ = [
    "DEFAULT_FLOORS",
    "DEFAULT_TIER_BY_TASK",
    "MODEL_OVERRIDES_NAMESPACE",
    "ORCHESTRATOR_MODEL_ENV_VAR",
    "RANK",
    "ROLE_FLOOR_ENV_VARS",
    "ROLE_MODEL_ENV_VARS",
    "TIER_ENV_VARS",
    "UNCONFIGURED_MODEL",
    "ModelChoice",
    "ModelTier",
    "Role",
    "TaskKind",
    "choose",
    "floor_for",
    "model_for",
    "provider_for",
    "requested_floor",
    "set_model_overrides",
    "tier_for",
]
