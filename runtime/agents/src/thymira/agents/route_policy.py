"""Code-owned enforcement for immutable session model-route snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.schemas import Actor, EventType, ModelRoutePolicy

if TYPE_CHECKING:
    from thymira.agents.llm.routing import ModelChoice
    from thymira.events import EventLog


class ModelRouteDeniedError(RuntimeError):
    """Raised before a provider call when a selected route is outside the session snapshot."""


class ModelRoutePolicyUnavailableError(ModelRouteDeniedError):
    """Raised before a provider is built when no bound route snapshot is available."""


def model_route_policy_payload(policy: ModelRoutePolicy) -> dict[str, object]:
    """Return the stable, redaction-safe payload for a policy snapshot event."""
    return {
        "version": policy.version,
        "allowed_routes": list(policy.allowed_routes),
        "authority": policy.authority,
        "sha256": policy.sha256,
    }


def record_model_route_policy_snapshot(
    event_log: EventLog,
    policy: ModelRoutePolicy,
    *,
    actor: Actor | None = None,
    subject_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Append one allowlist snapshot event, separate from ``model.selected`` evidence.

    The local runtime has one authoritative hash chain per Run. A session's snapshot is therefore
    published into that chain before dispatch, with the session as the event subject and the Run
    as an explicit relation in the payload. This preserves one evidence log while making the
    session provenance addressable during replay.
    """
    payload = model_route_policy_payload(policy)
    if subject_id is not None:
        payload["session_id"] = subject_id
    if run_id is not None:
        payload["run_id"] = run_id
    event_log.append(
        EventType.MODEL_ROUTE_POLICY_SNAPSHOTTED,
        actor or Actor.system(),
        payload,
        subject_id=subject_id,
        producer="thymira.agents",
    )


def enforce_model_route(
    choice: ModelChoice,
    policy: ModelRoutePolicy | None,
    *,
    event_log: EventLog | None = None,
    actor: Actor | None = None,
    subject_id: str | None = None,
) -> None:
    """Enforce a session route snapshot immediately before provider construction/invocation.

    ``choose`` remains the only model selector. An absent policy is an authorization failure;
    composition roots must bind an explicit snapshot, including the empty default-deny snapshot.
    A denial is durable evidence when a log is supplied and is deterministic across replay.
    """
    if policy is None:
        if event_log is not None:
            event_log.append(
                EventType.MODEL_ROUTE_DENIED,
                actor or Actor.system(),
                {
                    "route": choice.model,
                    "role": choice.role.value,
                    "task": choice.task,
                    "reason": "route_policy_unavailable",
                },
                subject_id=subject_id,
                producer="thymira.agents",
            )
        raise ModelRoutePolicyUnavailableError(
            f"model route {choice.model!r} cannot be used without a bound route policy"
        )
    if policy.allows(choice.model):
        return
    payload = {
        "route": choice.model,
        "role": choice.role.value,
        "task": choice.task,
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
        "authority": policy.authority,
        "reason": "route_not_allowed",
    }
    if event_log is not None:
        event_log.append(
            EventType.MODEL_ROUTE_DENIED,
            actor or Actor.system(),
            payload,
            subject_id=subject_id,
            producer="thymira.agents",
        )
    raise ModelRouteDeniedError(
        f"model route {choice.model!r} is not allowed by session policy "
        f"{policy.sha256} (version {policy.version})"
    )


__all__ = [
    "ModelRouteDeniedError",
    "ModelRoutePolicyUnavailableError",
    "enforce_model_route",
    "model_route_policy_payload",
    "record_model_route_policy_snapshot",
]
