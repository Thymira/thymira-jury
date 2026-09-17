"""F13.2 model-route snapshots and the pre-gateway enforcement seam."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent

from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.agents import (
    ModelRouteDeniedError,
    ModelRoutePolicyUnavailableError,
    record_model_route_policy_snapshot,
)
from thymira.agents.llm.routing import Role, provider_for
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.agents.model_binding import routed_model
from thymira.api import build_default_deps
from thymira.core import RunService, SessionService, resolve_run_model_route_policy
from thymira.core import runs as runs_module
from thymira.events import InMemoryEventLog
from thymira.schemas import Actor, EventType, ModelRoutePolicy, Run, new_id
from thymira.state import InMemorySessionRepository, LocalRunStore, LocalSessionRepository

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.core import ExecutionDispatcher


def test_policy_is_hash_pinned_immutable_and_narrowing_only() -> None:
    policy = ModelRoutePolicy.from_routes(("vendor-b/standard", "vendor-a/frontier"))

    assert policy.allowed_routes == ("vendor-a/frontier", "vendor-b/standard")
    assert policy.policy_hash == policy.sha256
    with pytest.raises((TypeError, ValidationError)):
        policy.allowed_routes = ("vendor-c/other",)  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="may not widen"):
        policy.narrowed_to(("vendor-c/other",))
    with pytest.raises(ValueError, match="wildcard"):
        ModelRoutePolicy.from_routes(("vendor-*/*",))


def test_route_snapshot_survives_session_creation_and_is_distinct_from_selection() -> None:
    policy = ModelRoutePolicy.from_routes(("vendor-b/standard",))
    repository = InMemorySessionRepository()
    session = SessionService(repository, model_route_policy=policy).create(
        project_id=new_id("project"), client="api"
    )
    log = InMemoryEventLog(new_id("run"))

    assert session.model_route_policy is not None
    record_model_route_policy_snapshot(log, session.model_route_policy, subject_id=session.id)

    assert session.model_route_policy == policy
    assert [event.type for event in log.events()] == [EventType.MODEL_ROUTE_POLICY_SNAPSHOTTED]
    assert log.events()[0].payload["sha256"] == policy.sha256
    assert log.events()[0].subject_id == session.id
    assert log.events()[0].payload["session_id"] == session.id


def test_changed_router_configuration_is_denied_before_scripted_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = "vendor-b/standard"
    changed = "vendor-c/other"
    policy = ModelRoutePolicy.from_routes((initial,))
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(["would be an unauthorized call"])

    # The session snapshot is frozen before the operator changes the live router configuration.
    model = routed_model(
        Role.AGENT,
        "code",
        log,
        provider=provider,
        route_policy=policy,
    )
    from thymira.agents.llm import routing

    monkeypatch.setattr(routing, "model_for", lambda role, tier: changed)
    with pytest.raises(ModelRouteDeniedError, match="vendor-c/other"):
        Agent(model=model).run_sync("make the call")

    assert provider.calls == []
    assert [event.type for event in log.events()] == [
        EventType.MODEL_SELECTED,
        EventType.MODEL_ROUTE_DENIED,
    ]
    denial = log.events()[-1]
    assert denial.payload == {
        "route": changed,
        "role": Role.AGENT.value,
        "task": "code",
        "policy_version": policy.version,
        "policy_sha256": policy.sha256,
        "authority": policy.authority,
        "reason": "route_not_allowed",
    }


def test_api_composition_derives_the_session_snapshot_from_explicit_operator_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "vendor-a/frontier,vendor-b/standard")
    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)

    session = deps.session_service.create(project_id=new_id("project"), client="api")

    assert session.model_route_policy is not None
    assert session.model_route_policy.allowed_routes == (
        "vendor-a/frontier",
        "vendor-b/standard",
    )


def test_unset_operator_routes_attach_explicit_default_deny_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THYMIRA_ALLOWED_MODEL_ROUTES", raising=False)

    deps = build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL)
    session = deps.session_service.create(project_id=new_id("project"), client="api")

    assert session.model_route_policy.allowed_routes == ()
    assert session.model_route_policy.authority == "operator"


def test_public_session_creation_cannot_replace_configured_policy() -> None:
    configured = ModelRoutePolicy.from_routes(("vendor-a/approved",))
    foreign = ModelRoutePolicy.from_routes(("vendor-b/unapproved",))
    service = SessionService(InMemorySessionRepository(), model_route_policy=configured)

    with pytest.raises(ValueError, match="configured snapshot or its explicit subset"):
        service.create(
            project_id=new_id("project"),
            client="api",
            model_route_policy=foreign,
        )


def test_run_creation_binds_policy_before_dispatch_and_worker_ignores_session_replacement(
    tmp_path: Path,
) -> None:
    initial = ModelRoutePolicy.from_routes(("vendor-a/approved", "vendor-b/standard"))
    revoked = ModelRoutePolicy.from_routes(("vendor-b/standard",))
    replacement = ModelRoutePolicy.from_routes(("vendor-c/unapproved",))
    session_repository = LocalSessionRepository(tmp_path / "sessions")
    sessions = SessionService(session_repository, model_route_policy=initial)
    session = sessions.create(project_id=new_id("project"), client="api")
    store = LocalRunStore(tmp_path / "runs")
    service = RunService(
        store,
        sessions,
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
    )

    run = service.create_run(
        session.id,
        "Inspect the dataset",
        actor=Actor.system(),
        workspace=tmp_path,
        dispatch=False,
    )
    session_repository.save(session.model_copy(update={"model_route_policy": replacement}))

    assert run.model_route_policy == initial
    assert service.get_run(run.id).model_route_policy == initial
    snapshot_event = next(
        event
        for event in store.events(run.id)
        if event.type is EventType.MODEL_ROUTE_POLICY_SNAPSHOTTED
    )
    assert snapshot_event.subject_id == session.id
    assert snapshot_event.payload["session_id"] == session.id
    assert snapshot_event.payload["run_id"] == run.id
    with pytest.raises(ValueError, match="outside this composition authority"):
        sessions.get_session(session.id)
    assert resolve_run_model_route_policy(run, revoked).allowed_routes == ("vendor-b/standard",)
    assert resolve_run_model_route_policy(run, replacement).allowed_routes == ()
    assert (
        resolve_run_model_route_policy(run, initial, session_present=False).authority
        == "unavailable"
    )


def test_missing_route_policy_fails_before_production_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "vendor-a/approved")

    def gateway_must_not_be_constructed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("route denial must precede production gateway construction")

    from thymira.agents import model_binding
    from thymira.agents.llm import routing

    monkeypatch.setattr(routing, "LiteLLMProvider", gateway_must_not_be_constructed)
    monkeypatch.setattr(model_binding, "LiteLLMProvider", gateway_must_not_be_constructed)
    log = InMemoryEventLog(new_id("run"))

    with pytest.raises(ModelRoutePolicyUnavailableError):
        provider_for(Role.AGENT, "code")

    model = routed_model(Role.AGENT, "code", log)
    with pytest.raises(ModelRoutePolicyUnavailableError):
        Agent(model=model).run_sync("must be refused")

    assert log.events()[-1].type is EventType.MODEL_ROUTE_DENIED
    assert log.events()[-1].payload["reason"] == "route_policy_unavailable"


def test_injected_provider_also_requires_route_policy_before_invocation() -> None:
    """An offline provider seam cannot turn a missing policy into an authorization."""
    log = InMemoryEventLog(new_id("run"))
    provider = ScriptedProvider(["must not run"])
    model = routed_model(Role.AGENT, "code", log, provider=provider)

    with pytest.raises(ModelRoutePolicyUnavailableError):
        Agent(model=model).run_sync("must be refused")

    assert provider.calls == []
    assert log.events()[-1].type is EventType.MODEL_ROUTE_DENIED


def test_missing_or_malformed_run_identity_cannot_be_loaded() -> None:
    run_data = Run(
        id=new_id("run"),
        project_id=new_id("project"),
        session_id=new_id("session"),
        prompt="identity validation",
    ).model_dump(mode="json")
    run_data.pop("model_route_policy")
    assert Run.model_validate(run_data).model_route_policy.allowed_routes == ()

    malformed = {
        **run_data,
        "model_route_policy": {
            "version": 1,
            "allowed_routes": ["vendor-a/approved"],
            "authority": "operator",
            "sha256": "0" * 64,
        },
    }
    with pytest.raises(ValidationError, match="sha256"):
        Run.model_validate(malformed)


def test_failed_policy_snapshot_persistence_prevents_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Dispatcher:
        def __init__(self) -> None:
            self.submitted = 0

        def submit(self, _run_id: str) -> None:
            self.submitted += 1

        def resume(self, _run_id: str) -> None:
            raise AssertionError("resume must not be reached")

    dispatcher = _Dispatcher()
    sessions = SessionService(
        LocalSessionRepository(tmp_path / "sessions"),
        model_route_policy=ModelRoutePolicy.from_routes(("vendor-a/approved",)),
    )
    session = sessions.create(project_id=new_id("project"), client="api")
    service = RunService(
        LocalRunStore(tmp_path / "runs"),
        sessions,
        dispatcher=cast("ExecutionDispatcher", dispatcher),
        policy_sha256="a" * 64,
        graph_definition_hash="b" * 64,
    )

    def fail_persistence(*_args: object, **_kwargs: object) -> None:
        raise OSError("route snapshot persistence failed")

    monkeypatch.setattr(runs_module, "record_model_route_policy_snapshot", fail_persistence)
    with pytest.raises(OSError, match="route snapshot persistence failed"):
        service.create_run(
            session.id,
            "must persist before dispatch",
            actor=Actor.system(),
            workspace=tmp_path,
        )
    assert dispatcher.submitted == 0


def test_manual_provider_factory_applies_the_same_exact_route_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THYMIRA_MODEL_STANDARD", "vendor-b/standard")
    policy = ModelRoutePolicy.from_routes(("vendor-a/frontier",))

    with pytest.raises(ModelRouteDeniedError, match="vendor-b/standard"):
        provider_for(Role.AGENT, "code", route_policy=policy)
