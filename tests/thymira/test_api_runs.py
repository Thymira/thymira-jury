"""Integration tests for the Run API boundary.

The FastAPI application and local persistence are real. The dispatcher is a small recording
fake because the real THY/MIRA graph factory is configured by a later composition task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import DEFAULT_ACTOR_ID, TEST_CREDENTIAL, authenticated_client
from thymira import observability
from thymira.api import (
    BearerTokenRegistry,
    build_default_deps,
    create_app,
    principal_for_role,
)
from thymira.core import (
    ExecutionDispatchError,
    MiraControlPlane,
    RunController,
    RunTransitionKind,
    default_activity_profile,
)
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.policies import PolicyEngine, RiskProfile, load_default_policy
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    AuditFinding,
    Decision,
    EventType,
    Framework,
    Id,
    RunCondition,
    RunOutcome,
    RunStage,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from thymira.schemas import Event


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the API composition's model-route snapshot to one code-owned test route."""
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "test-model")
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_WRITER_TOKEN = "token-writer"  # noqa: S105 - test-only bearer token, not a secret.
_APPROVER_TOKEN = "token-approver"  # noqa: S105 - test-only bearer token, not a secret.
_EXECUTION_REVIEW_RULE_ID = "API-EXECUTION-REVIEW"
_EXECUTION_REVIEW_POLICY = (
    "name: api-execution-review\n"
    "version: '1.0'\n"
    "action_rules:\n"
    f"  - id: {_EXECUTION_REVIEW_RULE_ID}\n"
    "    action_types: [execution.start]\n"
    "    decision: REQUIRE_HUMAN_REVIEW\n"
    "    reason: A human must approve execution.\n"
)


class _RecordingDispatcher:
    """Record submitted Runs without invoking a model or graph."""

    def __init__(self) -> None:
        self.submitted: list[Id] = []
        self.resumed: list[Id] = []

    def submit(self, run_id: Id) -> None:
        """Record one dispatch request."""
        self.submitted.append(run_id)

    def resume(self, run_id: Id) -> None:
        """Record one resume request."""
        self.resumed.append(run_id)


class _FailingDispatcher(_RecordingDispatcher):
    """Fail submissions after the API has persisted a Run."""

    def submit(self, run_id: Id) -> None:
        """Raise the dispatcher error handled by the background task."""
        super().submit(run_id)
        raise ExecutionDispatchError(f"dispatch failed for {run_id}")


class _LinkingClient:
    """A stand-in Langfuse client that answers the two calls the trace route makes."""

    def create_trace_id(self, *, seed: str | None = None) -> str:
        """Derive a trace id the way the real client does."""
        return (seed or "x").ljust(32, "0")[:32]

    def get_trace_url(self, *, trace_id: str) -> str:
        """Name where that trace can be read."""
        return f"https://langfuse.test/traces/{trace_id}"


def _client(
    tmp_path: Path, *, configured: bool = True, execution_review: bool = False
) -> tuple[TestClient, Id, _RecordingDispatcher]:
    """Build an API client backed by one optional temporary project workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_id: Id
    if configured:
        config_dir = workspace / ".thymira"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "project:\n  name: credit-risk\n  domain: credit_risk\n",
            encoding="utf-8",
            newline="\n",
        )
        if execution_review:
            (config_dir / "policies.yaml").write_text(
                _EXECUTION_REVIEW_POLICY,
                encoding="utf-8",
                newline="\n",
            )
    dispatcher = _RecordingDispatcher()
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace if configured else None,
        dispatcher=dispatcher,
        principal_resolver=TEST_CREDENTIAL,
    )
    if execution_review:
        assert deps.project_resolution is not None
        assert (deps.project_resolution.workspace / "policies.yaml").is_file()
    if deps.project_resolution is None:
        project_id = new_id("project")
    else:
        project_id = deps.project_resolution.project_id
    return authenticated_client(create_app(deps)), project_id, dispatcher


def test_post_runs_resolves_configured_project_and_creates_session(tmp_path: Path) -> None:
    """A prompt-only request is associated with the API's configured project."""
    client, project_id, dispatcher = _client(tmp_path)

    response = client.post("/runs", json={"prompt": "Analyze the dataset"})

    assert response.status_code == 201
    run = response.json()
    assert run["id"].startswith("run_")
    assert run["project_id"] == project_id
    assert run["session_id"].startswith("session_")
    assert run["status"] == "CREATED"
    assert dispatcher.submitted == [run["id"]]


def test_post_runs_records_a_failed_run_when_background_dispatch_fails(tmp_path: Path) -> None:
    """The creation response survives a later dispatcher failure with evidence of the failure."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    dispatcher = _FailingDispatcher()
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=dispatcher,
        principal_resolver=TEST_CREDENTIAL,
    )
    client = authenticated_client(create_app(deps))

    response = client.post("/runs", json={"prompt": "Analyze the dataset"})

    assert response.status_code == 201
    run = response.json()
    assert dispatcher.submitted == [run["id"]]
    assert deps.run_service.get_run(run["id"]).status.value == "FAILED"


def test_post_runs_rejects_project_not_bound_to_api(tmp_path: Path) -> None:
    """A caller cannot redirect a single-project API to another project."""
    client, project_id, _ = _client(tmp_path)

    response = client.post(
        "/runs",
        json={"prompt": "Analyze", "project_id": new_id("project")},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "project_not_found"
    assert response.json()["details"] == {}
    assert project_id != response.json().get("project_id")


def test_post_runs_rejects_session_from_another_project(tmp_path: Path) -> None:
    """An explicit session must belong to the configured project."""
    client, _, _ = _client(tmp_path)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    foreign_session = deps.session_service.create(
        project_id=new_id("project"),
        client="test",
    )

    response = client.post(
        "/runs",
        json={"prompt": "Analyze", "session_id": foreign_session.id},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "session_not_found"


def test_get_and_list_runs_use_project_scope_and_cursor(tmp_path: Path) -> None:
    """Created Runs can be inspected and paginated through the API."""
    client, project_id, _ = _client(tmp_path)
    first = client.post("/runs", json={"prompt": "First"}).json()
    second = client.post("/runs", json={"prompt": "Second"}).json()

    inspected = client.get(f"/runs/{first['id']}")
    page = client.get("/runs", params={"project_id": project_id, "limit": 1})

    assert inspected.status_code == 200
    assert inspected.json()["id"] == first["id"]
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1
    assert page.json()["next_cursor"] is not None

    next_page = client.get(
        "/runs",
        params={"limit": 1, "cursor": page.json()["next_cursor"]},
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1
    assert {page.json()["items"][0]["id"], next_page.json()["items"][0]["id"]} == {
        first["id"],
        second["id"],
    }


def test_get_runs_returns_problem_json_for_unknown_or_malformed_ids(tmp_path: Path) -> None:
    """Unknown Runs return 404 and non-Run identifiers return 422."""
    client, _, _ = _client(tmp_path)

    unknown = client.get(f"/runs/{new_id('run')}")
    malformed = client.get(f"/runs/{new_id('project')}")

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "run_not_found"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_run_id"


def test_post_runs_requires_a_configured_project(tmp_path: Path) -> None:
    """The API fails closed instead of inventing a project when none is configured."""
    client, _, dispatcher = _client(tmp_path, configured=False)

    response = client.post("/runs", json={"prompt": "Analyze"})

    assert response.status_code == 503
    assert response.json()["code"] == "project_not_configured"
    assert dispatcher.submitted == []


def _bearer(token: str) -> dict[str, str]:
    """Build the Authorization header carrying a bearer token."""
    return {"Authorization": f"Bearer {token}"}


def _authenticated_client(tmp_path: Path) -> TestClient:
    """Build an API client whose composition root resolves a real bearer-token principal."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    registry = BearerTokenRegistry(
        {
            _WRITER_TOKEN: principal_for_role("mario", "data-scientist"),
            _APPROVER_TOKEN: principal_for_role("val", "risk-officer"),
        }
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=registry,
    )
    return authenticated_client(create_app(deps))


def _human_approval_event(client: TestClient, run_id: Id) -> Event:
    """Return the single ``human.approval`` event recorded on one Run's hash-chained log."""
    app = cast("FastAPI", client.app)
    approvals = [
        event
        for event in app.state.runtime_deps.event_store.read(run_id)
        if event.type is EventType.HUMAN_APPROVAL
    ]
    assert len(approvals) == 1
    return approvals[0]


def _park_for_approval(
    client: TestClient,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[Id, _RecordingDispatcher]:
    """Prepare one Run in the same waiting state produced by the composition graph."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    created = client.post("/runs", json={"prompt": "Review the model"}, headers=headers).json()
    run_id = created["id"]
    controller = RunController(deps.run_store)
    controller.advance(run_id, RunTransitionKind.START)
    controller.advance(run_id, RunTransitionKind.BEGIN_EXECUTION)
    controller.advance(run_id, RunTransitionKind.BEGIN_AUDIT)
    finding = AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="GOV-API",
        framework=Framework.EU_AI_ACT,
        title="Human review required",
        finding="The API approval path needs a human decision.",
        severity=Severity.HIGH,
        confidence=0.95,
    )
    gate = deps.gate_factory(run_id)
    event_log = deps.event_store.open(run_id)
    event_log.append(
        EventType.AUDIT_FINDING,
        Actor.system(),
        {"finding": finding.to_json_dict()},
        subject_id=finding.id,
        producer="test.mira",
    )
    events = deps.run_store.events(run_id)
    report = AuditReport(
        run_id=run_id,
        status="passed_with_warnings",
        controls=(),
        findings=(finding,),
        terminal_hash=events[-1].hash,
        manifest_sha256=artifact_manifest_sha256(deps.artifact_store_factory(run_id)),
        policy_sha256=deps.gate_factory(run_id).engine.policy_sha256,
    )
    event_log.append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "status": report.status,
            "audit_revision": 1,
            "audit_report": report.model_dump(mode="json"),
        },
        subject_id=run_id,
        producer="test.mira",
    )
    decision = gate.review_findings((finding,))
    controller.park_for_review(run_id, decision)
    return run_id, deps.dispatcher


def _park_for_execution_start_approval(client: TestClient) -> tuple[Id, _RecordingDispatcher]:
    """Park a Run on a real project-declared ``execution.start`` review."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    created = client.post("/runs", json={"prompt": "Review execution start"}).json()
    run_id = created["id"]
    controller = RunController(deps.run_store)
    controller.advance(run_id, RunTransitionKind.START)
    gate = deps.gate_factory(run_id)
    assert any(
        rule.id == _EXECUTION_REVIEW_RULE_ID and rule.decision is Decision.REQUIRE_HUMAN_REVIEW
        for rule in gate.engine.policy.action_rules
    )
    decision = gate.authorize_execution(
        RiskProfile(risk_level="limited", activity_category="analysis", confidence=1.0), ()
    )
    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    controller.park_for_review(run_id, decision, resume_from="execution_start")
    return run_id, deps.dispatcher


def _pause_for_resume(client: TestClient, run_id: Id) -> None:
    """Reach the resumable condition through MIRA's bounded public control path."""
    app = cast("FastAPI", client.app)
    store = app.state.runtime_deps.run_store
    RunController(store).advance(run_id, RunTransitionKind.START)
    MiraControlPlane(store, PolicyEngine(load_default_policy())).handle(
        ActionIntent(
            id=new_id("intent"),
            run_id=run_id,
            requester=Actor(kind=ActorKind.AGENT, id="mira", authenticated=True),
            action_kind=ActionKind.PAUSE_RUN,
            subject_kind="run",
            subject_id=run_id,
            purpose="Pause the Run for the API resume contract.",
            idempotency_key=new_id("intent"),
        )
    )


def test_resume_route_delegates_to_the_injected_dispatcher(tmp_path: Path) -> None:
    """The resume endpoint keeps execution behind the dispatcher seam."""
    client, _, dispatcher = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "Resume me"}).json()
    _pause_for_resume(client, run["id"])

    response = client.post(f"/runs/{run['id']}/resume")

    assert response.status_code == 202
    assert response.json()["id"] == run["id"]
    assert dispatcher.resumed == [run["id"]]


def test_resume_route_refuses_a_run_that_is_already_active(tmp_path: Path) -> None:
    """A caller cannot race the dispatcher already executing a Run."""
    client, _, dispatcher = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "Already active"}).json()

    response = client.post(f"/runs/{run['id']}/resume")

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_resumable"
    assert dispatcher.resumed == []


def test_risk_interview_routes_expose_a_question_and_persist_the_human_answer(
    tmp_path: Path,
) -> None:
    """The API surfaces one pending field and resumes its Run after an event-backed answer."""
    client, _, dispatcher = _client(tmp_path)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    created = client.post("/runs", json={"prompt": "Prioritise applications"}).json()
    run_id = created["id"]
    run = deps.run_service.get_run(run_id)
    RunController(deps.run_store).advance(run_id, RunTransitionKind.START)
    deps.risk_interview.begin(run, default_activity_profile(run))

    question = client.get(f"/runs/{run_id}/risk-interview")

    assert question.status_code == 200
    assert question.json()["pending_question"]["field"] == "purpose"
    assert question.json()["profile"]["version"] == 1

    answer = client.post(
        f"/runs/{run_id}/risk-interview",
        json={"answer": "Credit applicants."},
    )

    assert answer.status_code == 202
    assert answer.json()["profile"]["version"] == 2
    assert answer.json()["pending_question"] is None
    assert dispatcher.resumed == [run_id]
    recorded = deps.run_store.events(run_id)
    answer_event = next(
        event for event in recorded if event.type is EventType.ACTIVITY_PROFILE_ANSWERED
    )
    assert answer_event.actor.id == DEFAULT_ACTOR_ID
    assert answer_event.actor.authenticated is True
    assert answer_event.payload["field"] == "purpose"


@pytest.mark.parametrize(
    ("endpoint", "expected_statuses"),
    [("approve", {"AUDITING", "COMPLETED"}), ("reject", {"BLOCKED"})],
)
def test_human_decision_routes_resolve_a_waiting_run(
    tmp_path: Path,
    endpoint: str,
    expected_statuses: set[str],
) -> None:
    """Approve resumes a waiting Run and reject blocks it, with Gate evidence recorded."""
    client, _, dispatcher = _client(tmp_path)
    run_id, _ = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/{endpoint}",
        json={"actor": DEFAULT_ACTOR_ID, "note": "Reviewed the evidence."},
    )

    assert response.status_code == 202
    assert response.json()["status"] in expected_statuses
    if endpoint == "approve":
        assert dispatcher.resumed == [run_id]
    else:
        assert dispatcher.resumed == []


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_execution_start_decision_routes_apply_a_declared_human_answer(
    tmp_path: Path, endpoint: str
) -> None:
    """Execution-start reviews use the API's declared human actor and exact resume target."""
    client, _, _ = _client(tmp_path, execution_review=True)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    run_id, dispatcher = _park_for_execution_start_approval(client)

    response = client.post(
        f"/runs/{run_id}/{endpoint}",
        json={"actor": "chief-risk-officer@bank.example", "note": "Reviewed execution."},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "actor_mismatch"
    response = client.post(
        f"/runs/{run_id}/{endpoint}",
        json={"actor": DEFAULT_ACTOR_ID, "note": "Reviewed execution."},
    )

    assert response.status_code == 202
    approval = _human_approval_event(client, run_id)
    assert approval.actor.kind is ActorKind.HUMAN
    assert approval.actor.id == DEFAULT_ACTOR_ID
    assert approval.actor.authenticated is True
    state = RunController(deps.run_store).current_state(run_id)
    if endpoint == "approve":
        assert state.stage is RunStage.PLANNING
        assert state.condition is RunCondition.ACTIVE
        resume = next(
            event
            for event in reversed(deps.run_store.events(run_id))
            if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "resume"
        )
        assert resume.payload["resume_from"] == "execution_start"
        assert dispatcher.resumed == [run_id]
    else:
        assert response.json()["status"] == "BLOCKED"
        assert state.condition is RunCondition.TERMINAL
        assert state.outcome is RunOutcome.BLOCKED
        assert dispatcher.resumed == []


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_a_body_actor_naming_someone_else_is_refused(tmp_path: Path, endpoint: str) -> None:
    """A body ``actor`` naming anyone but the authenticated principal is a 403, and records nothing.

    ``Actor.authenticated`` is the audit layer's only signal that an identity was verified, and
    ``human.approval`` is hash-chained evidence that cannot be corrected later, so a caller who
    states an identity they do not hold is refused rather than quietly overruled.
    """
    client, _, _ = _client(tmp_path)
    run_id, _ = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/{endpoint}",
        json={"actor": "chief-risk-officer@bank.example", "note": "Reviewed the evidence."},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "actor_mismatch"
    app = cast("FastAPI", client.app)
    events = app.state.runtime_deps.event_store.read(run_id)
    assert not [event for event in events if event.type is EventType.HUMAN_APPROVAL]


def test_an_actor_header_no_longer_names_anyone(tmp_path: Path) -> None:
    """The ``X-Thymira-Actor`` header is gone: it names nobody and changes no recorded identity.

    It was the other door onto the same evidence -- any caller could set it, and the recorded id
    was whatever it said. The authenticated principal is now the only source of identity, so the
    header is inert rather than merely flagged unverified.
    """
    client, _, _ = _client(tmp_path)
    run_id, _ = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/approve",
        json={"note": "Reviewed the evidence."},
        headers={"X-Thymira-Actor": "chief-risk-officer@bank.example"},
    )

    assert response.status_code == 202
    approval = _human_approval_event(client, run_id)
    assert approval.actor.kind is ActorKind.HUMAN
    assert approval.actor.id == DEFAULT_ACTOR_ID
    assert approval.actor.id != "chief-risk-officer@bank.example"
    assert approval.actor.authenticated is True


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_a_human_decision_with_no_body_actor_records_the_authenticated_human(
    tmp_path: Path,
    endpoint: str,
) -> None:
    """Naming nobody is now correct, and the answer is still never ``Actor.system()`` (C6).

    The route used to refuse this with 401 ``actor_required`` because an approval recorded as the
    system is indistinguishable from an automated one. The call cannot be anonymous any more.
    """
    client, _, _ = _client(tmp_path)
    run_id, _ = _park_for_approval(client)

    response = client.post(f"/runs/{run_id}/{endpoint}", json={})

    assert response.status_code == 202
    approval = _human_approval_event(client, run_id)
    assert approval.actor.kind is ActorKind.HUMAN
    assert approval.actor.id == DEFAULT_ACTOR_ID
    assert approval.actor.authenticated is True


def test_approve_records_the_multi_principal_resolver_identity(tmp_path: Path) -> None:
    """A multi-principal deployment records the exact principal whose token was presented."""
    client = _authenticated_client(tmp_path)
    run_id, _ = _park_for_approval(client, headers=_bearer(_WRITER_TOKEN))

    response = client.post(
        f"/runs/{run_id}/approve",
        json={"actor": "val", "note": "Reviewed the evidence."},
        headers=_bearer(_APPROVER_TOKEN),
    )

    assert response.status_code == 202
    approval = _human_approval_event(client, run_id)
    assert approval.actor.id == "val"
    assert approval.actor.role == "risk-officer"
    assert approval.actor.authenticated is True


def test_approve_route_rejects_a_run_without_pending_approval(tmp_path: Path) -> None:
    """A human cannot approve a Run that is not waiting for a policy decision."""
    client, _, _ = _client(tmp_path)
    run = client.post("/runs", json={"prompt": "No approval"}).json()

    response = client.post(
        f"/runs/{run['id']}/approve",
        json={"actor": DEFAULT_ACTOR_ID},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "approval_not_pending"


def test_the_trace_route_reports_no_trace_when_the_runtime_is_not_tracing(tmp_path: Path) -> None:
    """The route answers rather than 404s: "there is no trace" is a real answer about a real Run."""
    client, _, _ = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Analyze"}).json()["id"]

    response = client.get(f"/runs/{run_id}/trace")

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "trace_url": None}


def test_the_trace_route_links_to_the_run_s_own_trace(tmp_path: Path) -> None:
    """A client cannot build this URL: it names the Langfuse project, which only the keys know."""
    client, _, _ = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Analyze"}).json()["id"]
    observability.configure(client=_LinkingClient())
    try:
        payload = client.get(f"/runs/{run_id}/trace").json()
    finally:
        observability.reset()

    assert payload["trace_url"] == f"https://langfuse.test/traces/{run_id.ljust(32, '0')[:32]}"


def test_the_trace_route_scopes_to_the_project_like_every_other_run_route(tmp_path: Path) -> None:
    """An unknown id must not become a link to a trace the caller has no business reading."""
    client, _, _ = _client(tmp_path)

    assert client.get(f"/runs/{new_id('run')}/trace").status_code == 404
    assert client.get(f"/runs/{new_id('project')}/trace").status_code == 422


def test_cancel_run_records_a_cancelled_transition_and_closes_the_pending_approval(
    tmp_path: Path,
) -> None:
    """The caller-abort surface exists and is reachable: F5.1's production call site.

    ``RunTransitionKind.CANCEL`` had no caller anywhere in the runtime. The route drives it
    through ``RunController`` like every other persistent transition, and the review the Run was
    parked on leaves the pending set -- so nobody can answer for a Run that has ended.
    """
    client, _, _ = _client(tmp_path)
    run_id, _ = _park_for_approval(client)
    assert len(client.get(f"/runs/{run_id}/approvals").json()["items"]) == 1

    response = client.post(f"/runs/{run_id}/cancel", json={"reason": "the caller went away"})

    assert response.status_code == 202
    assert response.json()["id"] == run_id
    events = cast("FastAPI", client.app).state.runtime_deps.event_store.read(run_id)
    cancelled = [
        event
        for event in events
        if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "cancel"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "the caller went away"
    assert client.get(f"/runs/{run_id}/approvals").json()["items"] == []


def test_cancel_run_is_idempotent_on_a_terminal_run(tmp_path: Path) -> None:
    """Abort is safe to retry: a disconnecting client's second call corrupts nothing."""
    client, _, _ = _client(tmp_path)
    run_id, _ = _park_for_approval(client)
    first = client.post(f"/runs/{run_id}/cancel")

    second = client.post(f"/runs/{run_id}/cancel")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["status"] == first.json()["status"]
    events = cast("FastAPI", client.app).state.runtime_deps.event_store.read(run_id)
    assert (
        sum(
            1
            for event in events
            if event.type is EventType.RUN_TRANSITIONED and event.payload.get("command") == "cancel"
        )
        == 1
    )
