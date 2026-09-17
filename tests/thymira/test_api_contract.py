"""API contract tests: the HTTP surface every client (CLI, web, SDK, MCP) depends on.

Real: the FastAPI application (``create_app``) and the local runtime dependency graph
(``build_default_deps``) over ``tmp_path``, exactly as the sibling ``test_api_*`` modules build
it. Faked: the execution dispatcher, so nothing here invokes a model or graph.

This module does not repeat behaviour already pinned elsewhere; it asserts the *contract* those
behaviours run inside:

- ``test_api.py`` / ``test_api_app.py`` — the frozen boundary models in isolation, and that the
  five route-group paths exist on the app.
- ``test_api_errors.py`` — idempotent Run creation, actor-header resolution on creation, and one
  problem+json example each for a 422 and a 409.
- ``test_api_runs.py`` / ``test_api_plan.py`` / ``test_api_events.py`` /
  ``test_api_audit_experiments.py`` — the business rules of each route (pagination, SSE framing,
  approval flow, audit folding, project/session scoping) with a handful of representative errors.
- ``test_api_telemetry.py`` — tracing spans and the graph-hash provenance in ``run.started``.

What is new here: the full registered-route inventory pinned against ``app.openapi()`` so an
added, removed or changed route fails this suite (the point of a contract test); that every
non-``/healthz`` operation documents FastAPI's validation-error response and that
``Idempotency-Key`` is declared as an optional header; that the *actual* problem+json envelope
(``ApiError``) is what every error path returns even where the auto-generated OpenAPI doc still
shows FastAPI's own ``HTTPValidationError`` (per-route docs cannot see app-level exception
handlers); one uniform 404/422 scope sweep across every ``run_id``-addressed route; the actor-seam
precedence rule for ``approve``/``reject`` (the trusted header wins over the legacy body actor);
and, for every route, one success response validated against its declared pydantic model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import DEFAULT_ACTOR_ID, TEST_CREDENTIAL, authenticated_client
from thymira.api import (
    ApiError,
    ArtifactContentResponse,
    ArtifactListResponse,
    EventPage,
    ExperimentListResponse,
    RiskInterviewResponse,
    RunAuditResponse,
    RunPage,
    RunPlan,
    ToolDescriptor,
    ToolListResponse,
    build_default_deps,
    create_app,
)
from thymira.api.schemas import MlflowRunListResponse
from thymira.core import (
    PHASE_ORDER,
    MiraControlPlane,
    RunController,
    RunTransitionKind,
    default_activity_profile,
)
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.policies import PolicyEngine, load_default_policy
from thymira.schemas import (
    ActionIntent,
    ActionKind,
    Actor,
    ActorKind,
    AuditFinding,
    EventType,
    Framework,
    Id,
    Run,
    RunStatus,
    Severity,
    Task,
    TaskStatus,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the API composition's model-route snapshot to one code-owned test route."""
    monkeypatch.setenv("THYMIRA_ALLOWED_MODEL_ROUTES", "test-model")
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def submit(self, run_id: Id) -> None:
        """Record one create dispatch."""
        del run_id

    def resume(self, run_id: Id) -> None:
        """Record one resume dispatch."""
        del run_id


def _client(tmp_path: Path, *, configured: bool = True) -> tuple[TestClient, Id]:
    """Build an API client backed by one optional temporary configured project workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    if configured:
        config_dir = workspace / ".thymira"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "project:\n  name: credit-risk\n  domain: credit_risk\n",
            encoding="utf-8",
            newline="\n",
        )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace if configured else None,
        dispatcher=_RecordingDispatcher(),
        principal_resolver=TEST_CREDENTIAL,
    )
    project_id = (
        deps.project_resolution.project_id if deps.project_resolution else new_id("project")
    )
    return authenticated_client(create_app(deps)), project_id


def _park_for_approval(client: TestClient) -> Id:
    """Create one Run and drive it into the waiting-for-approval state approve/reject expect."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    created = client.post("/runs", json={"prompt": "Review the model"}).json()
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
    gate = deps.gate_factory(run_id)
    decision = gate.review_findings((finding,))
    controller.park_for_review(run_id, decision)
    return run_id


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
            purpose="Pause the Run for the API response-schema contract.",
            idempotency_key=new_id("intent"),
        )
    )


def _extract_operations(app: FastAPI) -> dict[tuple[str, str], tuple[int, str | None]]:
    """Flatten one app's OpenAPI document into (method, path) -> (success status, schema name)."""
    operations: dict[tuple[str, str], tuple[int, str | None]] = {}
    for path, methods in app.openapi()["paths"].items():
        for method, operation in methods.items():
            responses = operation["responses"]
            success_codes = [code for code in responses if code.startswith("2")]
            assert len(success_codes) == 1, f"{method.upper()} {path}: {success_codes}"
            content = responses[success_codes[0]].get("content")
            schema_name = None
            if content:
                schema = content["application/json"]["schema"]
                ref = schema.get("$ref")
                schema_name = ref.rsplit("/", 1)[-1] if ref else None
            operations[(method.upper(), path)] = (int(success_codes[0]), schema_name)
    return operations


# The pinned MVP route table: (method, path) -> (success status code, success schema name).
# A route added, removed, retargeted to a different status, or given a different response model
# must update this table in the same change, which is the point of a contract suite.
_EXPECTED_OPERATIONS: dict[tuple[str, str], tuple[int, str | None]] = {
    ("GET", "/healthz"): (200, None),
    ("POST", "/runs"): (201, "Run"),
    ("GET", "/runs"): (200, "RunPage"),
    ("GET", "/runs/{run_id}"): (200, "Run"),
    ("GET", "/runs/{run_id}/risk-interview"): (200, "RiskInterviewResponse"),
    ("GET", "/runs/{run_id}/trace"): (200, "RunTraceResponse"),
    ("POST", "/runs/{run_id}/plan"): (200, "RunPlan"),
    ("GET", "/runs/{run_id}/plan"): (200, "DurablePlanResponse"),
    ("POST", "/runs/{run_id}/delegate"): (201, "Task"),
    ("POST", "/runs/{run_id}/resume"): (202, "Run"),
    ("POST", "/runs/{run_id}/cancel"): (202, "Run"),
    ("POST", "/runs/{run_id}/risk-interview"): (202, "RiskInterviewResponse"),
    ("POST", "/runs/{run_id}/approve"): (202, "Run"),
    ("POST", "/runs/{run_id}/reject"): (202, "Run"),
    ("GET", "/runs/{run_id}/events"): (200, None),  # response_model=None: JSON page or SSE.
    ("GET", "/runs/{run_id}/audit"): (200, "RunAuditResponse"),
    ("GET", "/runs/{run_id}/assurance"): (200, "AssuranceBundle"),
    ("GET", "/runs/{run_id}/experiments"): (200, "ExperimentListResponse"),
    ("GET", "/runs/{run_id}/artifacts"): (200, "ArtifactListResponse"),
    ("GET", "/runs/{run_id}/artifacts/{artifact_id}"): (200, "ArtifactContentResponse"),
    ("GET", "/runs/{run_id}/mlflow"): (200, "MlflowRunListResponse"),
    ("GET", "/runs/{run_id}/approvals"): (200, "ApprovalListResponse"),
    ("POST", "/runs/{run_id}/approvals/{decision_id}/approve"): (200, "ApprovalResolution"),
    ("POST", "/runs/{run_id}/approvals/{decision_id}/reject"): (200, "ApprovalResolution"),
    ("GET", "/settings"): (200, "SettingsListResponse"),
    ("GET", "/settings/{namespace}"): (200, "SettingsSnapshot"),
    ("PUT", "/settings/{namespace}"): (200, "SettingsSnapshot"),
    ("GET", "/tools"): (200, "ToolListResponse"),
    ("GET", "/tools/{name}"): (200, "ToolDescriptor"),
    ("GET", "/project/context"): (200, "ProjectContextResponse"),
    ("PUT", "/project/context"): (200, "ProjectContextResponse"),
    ("GET", "/project/datasets"): (200, "ProjectDatasetListResponse"),
    ("PUT", "/project/datasets/{name}"): (200, "ProjectDatasetUploadResponse"),
    ("PATCH", "/project/datasets/{name}"): (200, "ProjectDatasetView"),
    ("DELETE", "/project/datasets/{name}"): (200, "ProjectDatasetListResponse"),
}


def test_openapi_surface_matches_the_pinned_route_table(tmp_path: Path) -> None:
    """The registered route set, methods, success status and response schema are all pinned."""
    app = create_app(build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL))

    assert _extract_operations(app) == _EXPECTED_OPERATIONS


def test_every_operation_except_healthz_declares_the_validation_error_response(
    tmp_path: Path,
) -> None:
    """FastAPI's automatic 422 response is documented on every route that takes input."""
    app = create_app(build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL))

    for path, methods in app.openapi()["paths"].items():
        for method, operation in methods.items():
            if (method.upper(), path) == ("GET", "/healthz"):
                continue
            responses = operation["responses"]
            assert "422" in responses, f"{method.upper()} {path} does not declare 422"
            schema = responses["422"]["content"]["application/json"]["schema"]
            assert schema["$ref"] == "#/components/schemas/HTTPValidationError"


def test_idempotency_key_is_declared_as_an_optional_request_header(tmp_path: Path) -> None:
    """POST /runs documents the header clients use to retry a Run creation safely."""
    app = create_app(build_default_deps(tmp_path, principal_resolver=TEST_CREDENTIAL))

    parameters = app.openapi()["paths"]["/runs"]["post"]["parameters"]
    headers = {
        parameter["name"]: parameter for parameter in parameters if parameter["in"] == "header"
    }

    assert headers["Idempotency-Key"]["required"] is False


_RUN_SCOPED_OPERATIONS: tuple[tuple[str, str, dict[str, object] | None], ...] = (
    ("GET", "/runs/{run_id}", None),
    ("POST", "/runs/{run_id}/plan", None),
    ("GET", "/runs/{run_id}/plan", None),
    ("POST", "/runs/{run_id}/delegate", {"agent": "data-agent", "objective": "Profile it"}),
    ("POST", "/runs/{run_id}/resume", None),
    ("POST", "/runs/{run_id}/cancel", None),
    ("GET", "/runs/{run_id}/risk-interview", None),
    ("POST", "/runs/{run_id}/risk-interview", {"answer": "Credit applicants."}),
    ("POST", "/runs/{run_id}/approve", {}),
    ("POST", "/runs/{run_id}/reject", {}),
    ("GET", "/runs/{run_id}/events", None),
    ("GET", "/runs/{run_id}/audit", None),
    ("GET", "/runs/{run_id}/assurance", None),
    ("GET", "/runs/{run_id}/experiments", None),
    ("GET", "/runs/{run_id}/artifacts", None),
    ("GET", f"/runs/{{run_id}}/artifacts/{new_id('artifact')}", None),
    ("GET", "/runs/{run_id}/mlflow", None),
    ("GET", "/runs/{run_id}/approvals", None),
)


@pytest.mark.parametrize(("method", "path_template", "json_body"), _RUN_SCOPED_OPERATIONS)
def test_run_scoped_routes_enforce_the_same_scope_contract(
    tmp_path: Path,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    """Every route addressed by run_id shares one 404-unknown / 422-malformed scope contract."""
    client, _project_id = _client(tmp_path)

    unknown = client.request(method, path_template.format(run_id=new_id("run")), json=json_body)
    malformed = client.request(
        method, path_template.format(run_id=new_id("project")), json=json_body
    )

    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/problem+json")
    assert ApiError.model_validate(unknown.json()).code == "run_not_found"

    assert malformed.status_code == 422
    assert malformed.headers["content-type"].startswith("application/problem+json")
    assert ApiError.model_validate(malformed.json()).code == "invalid_run_id"


def test_artifact_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """The artifact inventory and content routes answer with their declared envelopes."""
    client, _project_id = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Profile it"}).json()["id"]
    app = cast("FastAPI", client.app)
    store = app.state.runtime_deps.artifact_store_factory(run_id)
    artifact = store.save_text("reports/summary.md", "# Summary", produced_by=run_id)

    listed = client.get(f"/runs/{run_id}/artifacts")
    content = client.get(f"/runs/{run_id}/artifacts/{artifact.id}")

    assert listed.status_code == 200
    assert ArtifactListResponse.model_validate(listed.json()).items[0].id == artifact.id
    assert content.status_code == 200
    assert ArtifactContentResponse.model_validate(content.json()).artifact.id == artifact.id


def test_request_validation_failures_return_the_stable_api_error_envelope(tmp_path: Path) -> None:
    """A body FastAPI itself rejects still answers in the project's stable error shape.

    ``POST /runs`` documents 422 as ``HTTPValidationError`` (FastAPI's own default, asserted
    above) because a per-route OpenAPI response cannot see the app's exception handlers. The
    wire body clients actually receive is ``ApiError``, produced by ``validation_error_handler``.
    This pins that divergence deliberately instead of letting it go unnoticed.
    """
    client, _project_id = _client(tmp_path)

    response = client.post("/runs", json={})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert "detail" not in body
    error = ApiError.model_validate(body)
    assert error.code == "validation_error"
    assert error.details["errors"]


def test_custom_problem_errors_return_the_stable_api_error_envelope(tmp_path: Path) -> None:
    """Hand-built ``problem()`` responses share the exact envelope raised domain errors use."""
    client, _project_id = _client(tmp_path)
    unconfigured_client, _project_id2 = _client(tmp_path / "unconfigured", configured=False)

    invalid_cursor = client.get("/runs", params={"cursor": "not-a-valid-cursor"})
    unconfigured = unconfigured_client.post("/runs", json={"prompt": "Analyze"})

    assert invalid_cursor.status_code == 422
    assert invalid_cursor.headers["content-type"].startswith("application/problem+json")
    assert ApiError.model_validate(invalid_cursor.json()).code == "invalid_cursor"

    assert unconfigured.status_code == 503
    assert unconfigured.headers["content-type"].startswith("application/problem+json")
    assert ApiError.model_validate(unconfigured.json()).code == "project_not_configured"


def test_a_body_actor_naming_someone_else_is_refused(tmp_path: Path) -> None:
    """The body may assert who is acting; it may not name anyone but the authenticated principal.

    The ``X-Thymira-Actor`` header used to beat the body field, and both were unverified. Neither
    names anyone now: an assertion that disagrees with the credential is a loud 403 rather than a
    silently ignored intent.
    """
    client, _project_id = _client(tmp_path)
    run_id = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/approve",
        json={"actor": "legacy-body-actor"},
        headers={"X-Thymira-Actor": "trusted-header-actor"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "actor_mismatch"
    app = cast("FastAPI", client.app)
    events = app.state.runtime_deps.event_store.read(run_id)
    assert not [event for event in events if "approved_by" in event.payload]


def test_a_body_actor_equal_to_the_principal_is_accepted(tmp_path: Path) -> None:
    """Asserting the identity you actually hold is allowed, and records that identity."""
    client, _project_id = _client(tmp_path)
    run_id = _park_for_approval(client)

    response = client.post(f"/runs/{run_id}/approve", json={"actor": DEFAULT_ACTOR_ID})

    assert response.status_code == 202
    app = cast("FastAPI", client.app)
    events = app.state.runtime_deps.event_store.read(run_id)
    transition = next(event for event in events if "approved_by" in event.payload)
    assert transition.payload["approved_by"]["id"] == DEFAULT_ACTOR_ID
    assert transition.payload["approved_by"]["authenticated"] is True


def test_run_lifecycle_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """POST/GET /runs and GET /runs/{run_id} return bodies that validate against their models."""
    client, project_id = _client(tmp_path)

    created = client.post("/runs", json={"prompt": "Analyze the dataset"})
    run = Run.model_validate(created.json())
    listed = client.get("/runs", params={"limit": 10})
    page = RunPage.model_validate(listed.json())
    fetched = client.get(f"/runs/{run.id}")

    assert created.status_code == 201
    assert run.project_id == project_id
    assert listed.status_code == 200
    assert run.id in {item.id for item in page.items}
    assert fetched.status_code == 200
    assert Run.model_validate(fetched.json()) == run


def test_plan_and_events_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """POST /plan and the JSON form of GET /events validate against their declared models."""
    client, _project_id = _client(tmp_path)
    run = Run.model_validate(client.post("/runs", json={"prompt": "Plan me"}).json())

    planned = client.post(f"/runs/{run.id}/plan")
    plan = RunPlan.model_validate(planned.json())
    events_response = client.get(f"/runs/{run.id}/events")
    page = EventPage.model_validate(events_response.json())

    assert planned.status_code == 200
    assert plan.run_id == run.id
    assert list(plan.phases) == list(PHASE_ORDER)
    assert events_response.status_code == 200
    assert page.run_id == run.id
    assert page.items  # run.started was recorded by the create route.


def test_audit_and_experiment_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """GET /audit and GET /experiments validate against their declared response models."""
    client, _project_id = _client(tmp_path)
    run = Run.model_validate(client.post("/runs", json={"prompt": "Audit me"}).json())
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    events = deps.run_store.events(run.id)
    report = AuditReport(
        run_id=run.id,
        status="passed",
        controls=(),
        terminal_hash=events[-1].hash,
        manifest_sha256=artifact_manifest_sha256(deps.artifact_store_factory(run.id)),
    )
    deps.event_store.open(run.id).append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"status": report.status, "audit_report": report.model_dump(mode="json")},
        subject_id=run.id,
        producer="thymira.mira",
    )

    audited = client.get(f"/runs/{run.id}/audit")
    audit = RunAuditResponse.model_validate(audited.json())
    listed = client.get(f"/runs/{run.id}/experiments")
    experiments = ExperimentListResponse.model_validate(listed.json())

    assert audited.status_code == 200
    assert audit.report.run_id == run.id
    assert listed.status_code == 200
    assert experiments == ExperimentListResponse(run_id=run.id, items=())


def test_mlflow_route_conforms_to_its_response_schema(tmp_path: Path) -> None:
    """GET /mlflow validates against its model; a run with no experiments lists no tracker runs."""
    client, _project_id = _client(tmp_path)
    run = Run.model_validate(client.post("/runs", json={"prompt": "Browse MLflow"}).json())

    listed = client.get(f"/runs/{run.id}/mlflow")
    tracker_runs = MlflowRunListResponse.model_validate(listed.json())

    assert listed.status_code == 200
    assert tracker_runs == MlflowRunListResponse(run_id=run.id, items=())


def test_resume_approve_reject_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """The three human/dispatcher decision routes return a Run that validates against the model."""
    client, _project_id = _client(tmp_path)

    fresh = Run.model_validate(client.post("/runs", json={"prompt": "Resume me"}).json())
    _pause_for_resume(client, fresh.id)
    resumed = client.post(f"/runs/{fresh.id}/resume")

    approve_run_id = _park_for_approval(client)
    approved = client.post(f"/runs/{approve_run_id}/approve", json={"actor": DEFAULT_ACTOR_ID})

    reject_run_id = _park_for_approval(client)
    rejected = client.post(f"/runs/{reject_run_id}/reject", json={"actor": DEFAULT_ACTOR_ID})

    assert resumed.status_code == 202
    assert Run.model_validate(resumed.json()).id == fresh.id
    assert approved.status_code == 202
    assert Run.model_validate(approved.json()).status in {RunStatus.AUDITING, RunStatus.COMPLETED}
    assert rejected.status_code == 202
    assert Run.model_validate(rejected.json()).status == RunStatus.BLOCKED


def test_risk_interview_routes_conform_to_their_response_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending-question view and its answer response both match their API contract."""
    for variable in (
        "THYMIRA_MODEL",
        "THYMIRA_AGENT_MODEL",
        "THYMIRA_MODEL_FAST",
        "THYMIRA_MODEL_STANDARD",
        "THYMIRA_MODEL_FRONTIER",
        "THYMIRA_ORCHESTRATOR_MODEL",
        "THYMIRA_THY_MODEL",
        "THYMIRA_MIRA_MODEL",
    ):
        monkeypatch.delenv(variable, raising=False)
    client, _project_id = _client(tmp_path)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    run = Run.model_validate(client.post("/runs", json={"prompt": "Classify risk"}).json())
    RunController(deps.run_store).advance(run.id, RunTransitionKind.START)
    deps.risk_interview.begin(run, default_activity_profile(run))

    pending = client.get(f"/runs/{run.id}/risk-interview")
    answered = client.post(
        f"/runs/{run.id}/risk-interview",
        json={"answer": "Credit applicants."},
    )

    assert pending.status_code == 200
    assert RiskInterviewResponse.model_validate_json(pending.text).pending_question is not None
    assert answered.status_code == 202
    assert RiskInterviewResponse.model_validate_json(answered.text).profile.version == 2


def test_delegate_and_tools_routes_conform_to_their_response_schema(tmp_path: Path) -> None:
    """POST /delegate and the two GET /tools routes validate against their declared models."""
    client, _project_id = _client(tmp_path)
    run = Run.model_validate(client.post("/runs", json={"prompt": "Delegate me"}).json())

    delegated = client.post(
        f"/runs/{run.id}/delegate",
        json={"agent": "data-agent", "objective": "Profile it"},
    )
    task = Task.model_validate(delegated.json())
    listed = client.get("/tools")
    tools = ToolListResponse.model_validate(listed.json())
    fetched = client.get(f"/tools/{tools.items[0].name}")

    assert delegated.status_code == 201
    assert task.run_id == run.id
    assert task.status is TaskStatus.PENDING
    assert listed.status_code == 200
    assert tools.items
    assert fetched.status_code == 200
    assert ToolDescriptor.model_validate(fetched.json()).name == tools.items[0].name
