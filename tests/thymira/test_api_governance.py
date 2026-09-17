"""Integration tests for the governance API routes (API-GOV).

Real: the FastAPI application (``create_app``) and the local runtime dependency graph
(``build_default_deps``) over ``tmp_path``. Faked: the execution dispatcher, so nothing here
invokes a model or graph. These tests pin the governance surface -- the audit read, the pending
approvals fold, and the service-backed approve/reject -- and the invariant that a service-recorded
human answer is evidence, not a Run transition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import DEFAULT_ACTOR_ID, TEST_CREDENTIAL, authenticated_client
from thymira.api import (
    BearerTokenRegistry,
    build_default_deps,
    create_app,
    principal_for_role,
)
from thymira.api.schemas import ApprovalListResponse, ApprovalResolution, RunAuditResponse
from thymira.core import RunController, RunTransitionKind
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import (
    Actor,
    ActorKind,
    AuditFinding,
    EventType,
    Framework,
    Id,
    RunStatus,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from thymira.schemas import Event


pytestmark = pytest.mark.integration

_WRITER_TOKEN = "token-writer"  # noqa: S105 - test-only bearer token, not a secret.
_APPROVER_TOKEN = "token-approver"  # noqa: S105 - test-only bearer token, not a secret.


class _RecordingDispatcher:
    """Record dispatches without invoking a model or graph."""

    def __init__(self) -> None:
        self.submitted: list[Id] = []
        self.resumed: list[Id] = []

    def submit(self, run_id: Id) -> None:
        """Record one create dispatch."""
        self.submitted.append(run_id)

    def resume(self, run_id: Id) -> None:
        """Record one resume dispatch."""
        self.resumed.append(run_id)


def _configured_workspace(tmp_path: Path) -> Path:
    """Write one temporary configured project workspace and return its root."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _client(tmp_path: Path) -> TestClient:
    """Build an API client backed by one temporary configured project workspace."""
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=_configured_workspace(tmp_path),
        dispatcher=_RecordingDispatcher(),
        principal_resolver=TEST_CREDENTIAL,
    )
    return authenticated_client(create_app(deps))


def _bearer(token: str) -> dict[str, str]:
    """Build the Authorization header carrying a bearer token."""
    return {"Authorization": f"Bearer {token}"}


def _authenticated_client(tmp_path: Path) -> TestClient:
    """Build an API client whose composition root resolves a real bearer-token principal."""
    registry = BearerTokenRegistry(
        {
            _WRITER_TOKEN: principal_for_role("mario", "data-scientist"),
            _APPROVER_TOKEN: principal_for_role("val", "risk-officer"),
        }
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=_configured_workspace(tmp_path),
        dispatcher=_RecordingDispatcher(),
        principal_resolver=registry,
    )
    return authenticated_client(create_app(deps))


def _high_finding(run_id: Id) -> AuditFinding:
    """Build the high-severity finding that drives a decision to human review."""
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id="GOV-API",
        framework=Framework.EU_AI_ACT,
        title="Human review required",
        finding="The API approval path needs a human decision.",
        severity=Severity.HIGH,
        confidence=0.95,
    )


def _park_for_approval(
    client: TestClient,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[Id, Id]:
    """Create one Run, drive it to waiting-for-approval, and record its pending decision.

    Returns the Run id and the pending policy-decision id folded into the approvals surface.
    """
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    created = client.post("/runs", json={"prompt": "Review the model"}, headers=headers)
    run_id = created.json()["id"]
    controller = RunController(deps.run_store)
    controller.advance(run_id, RunTransitionKind.START)
    controller.advance(run_id, RunTransitionKind.BEGIN_EXECUTION)
    controller.advance(run_id, RunTransitionKind.BEGIN_AUDIT)
    gate = deps.gate_factory(run_id)
    decision = gate.review_findings((_high_finding(run_id),))
    controller.park_for_review(run_id, decision)
    return run_id, decision.id


def _human_approval_event(client: TestClient, run_id: Id, decision_id: Id) -> Event:
    """Return the ``human.approval`` event the ApprovalService recorded for one decision."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    return next(
        event
        for event in deps.event_store.read(run_id)
        if event.type is EventType.HUMAN_APPROVAL
        and event.payload.get("decision_id") == decision_id
    )


def _completed_run_with_decision(client: TestClient) -> Id:
    """Create one Run, record a clean findings decision, and drive it to COMPLETED."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    run_id = client.post("/runs", json={"prompt": "Audit me"}).json()["id"]
    controller = RunController(deps.run_store)
    controller.advance(run_id, RunTransitionKind.START)
    controller.advance(run_id, RunTransitionKind.BEGIN_EXECUTION)
    controller.advance(run_id, RunTransitionKind.BEGIN_AUDIT)
    events = deps.run_store.events(run_id)
    report = AuditReport(
        run_id=run_id,
        status="passed",
        controls=(),
        terminal_hash=events[-1].hash,
        manifest_sha256=artifact_manifest_sha256(deps.artifact_store_factory(run_id)),
    )
    deps.event_store.open(run_id).append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"status": report.status, "audit_report": report.model_dump(mode="json")},
        subject_id=run_id,
        producer="thymira.mira",
    )
    deps.gate_factory(run_id).review_findings(())
    controller.advance(run_id, RunTransitionKind.BEGIN_REPORTING)
    controller.advance(run_id, RunTransitionKind.COMPLETE)
    return run_id


def test_get_audit_on_a_completed_run_returns_report_and_decision(tmp_path: Path) -> None:
    """GET /runs/{id}/audit returns the deterministic report paired with the run decision."""
    client = _client(tmp_path)
    run_id = _completed_run_with_decision(client)

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 200
    audit = RunAuditResponse.model_validate(response.json())
    assert audit.report.run_id == run_id
    assert audit.decision is not None
    assert audit.decision.subject_kind == "findings"


def test_get_approvals_lists_the_pending_request(tmp_path: Path) -> None:
    """GET /runs/{id}/approvals folds the log into the run's unresolved human-review request."""
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    response = client.get(f"/runs/{run_id}/approvals")

    assert response.status_code == 200
    listing = ApprovalListResponse.model_validate(response.json())
    assert listing.run_id == run_id
    assert [item.decision_id for item in listing.items] == [decision_id]
    assert listing.items[0].summary == "audit findings"


def test_get_approvals_is_empty_when_nothing_is_pending(tmp_path: Path) -> None:
    """A run with no unresolved request folds to an empty approvals listing."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "No approval"}).json()["id"]

    response = client.get(f"/runs/{run_id}/approvals")

    assert response.status_code == 200
    assert ApprovalListResponse.model_validate(response.json()).items == ()


def test_approve_resolves_the_pending_decision_and_returns_approved_true(tmp_path: Path) -> None:
    """POST approve records the human answer as evidence and returns the resolved decision."""
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/approve",
        json={"reason": "Evidence reviewed."},
    )

    assert response.status_code == 200
    resolution = ApprovalResolution.model_validate(response.json())
    assert resolution.approved is True
    assert resolution.decision_id == decision_id
    assert resolution.decision.id == decision_id

    approval = _human_approval_event(client, run_id, decision_id)
    assert approval.payload["approved"] is True
    assert approval.payload["rationale"] == "Evidence reviewed."
    assert approval.actor.id == DEFAULT_ACTOR_ID
    assert approval.actor.authenticated is True
    # Recording the human answer is evidence, not authorization: the Run does not transition.
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    assert deps.run_service.get_run(run_id).status == RunStatus.WAITING_FOR_APPROVAL


def test_reject_resolves_the_pending_decision_and_returns_approved_false(tmp_path: Path) -> None:
    """POST reject records approved False and leaves the Run's lifecycle untouched."""
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/reject",
        json={},
    )

    assert response.status_code == 200
    resolution = ApprovalResolution.model_validate(response.json())
    assert resolution.approved is False
    assert resolution.decision_id == decision_id

    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    assert deps.run_service.get_run(run_id).status == RunStatus.WAITING_FOR_APPROVAL


def test_approve_with_nothing_pending_returns_409(tmp_path: Path) -> None:
    """Approving a run with no pending decision is a 409, not an authorization."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Nothing pending"}).json()["id"]

    response = client.post(
        f"/runs/{run_id}/approvals/{new_id('decision')}/approve",
        json={},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "approval_not_pending"


def test_a_pending_decision_can_be_resolved_only_once(tmp_path: Path) -> None:
    """Once the human answer is recorded the request leaves the pending set; a repeat is a 409."""
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    first = client.post(f"/runs/{run_id}/approvals/{decision_id}/approve", json={})
    second = client.post(f"/runs/{run_id}/approvals/{decision_id}/approve", json={})

    assert first.status_code == 200
    assert second.status_code == 409
    assert client.get(f"/runs/{run_id}/approvals").json()["items"] == []


def test_approvals_routes_enforce_run_scope(tmp_path: Path) -> None:
    """The governance routes reject an unknown Run (404) and a non-Run identifier (422)."""
    client = _client(tmp_path)

    unknown = client.get(f"/runs/{new_id('run')}/approvals")
    malformed = client.get(f"/runs/{new_id('project')}/approvals")
    unknown_approve = client.post(
        f"/runs/{new_id('run')}/approvals/{new_id('decision')}/approve", json={}
    )

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "run_not_found"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_run_id"
    assert unknown_approve.status_code == 404
    assert unknown_approve.json()["code"] == "run_not_found"


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_a_governance_body_actor_naming_someone_else_is_refused(
    tmp_path: Path,
    endpoint: str,
) -> None:
    """A body ``actor`` no longer names anyone; asserting a different identity is a 403.

    This is the third door onto the same ``human.approval`` evidence, after the run-lifecycle body
    actor and the deleted ``X-Thymira-Actor`` header. ``Actor.authenticated`` is the audit layer's
    only signal that an identity was verified, and the ApprovalService writes onto the
    hash-chained, append-only log, so a claim recorded here can never be corrected -- which is why
    a caller who states the wrong identity is refused rather than quietly overruled.
    """
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/{endpoint}",
        json={"actor": "ceo@example.com", "reason": "I approve myself."},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "actor_mismatch"
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    assert not any(
        event.type is EventType.HUMAN_APPROVAL for event in deps.event_store.read(run_id)
    )


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_a_governance_decision_with_no_body_actor_records_the_authenticated_human(
    tmp_path: Path,
    endpoint: str,
) -> None:
    """Naming nobody is now correct: the credential already named who is acting (bug-hunt C6).

    The route used to refuse a fully anonymous call with 401 ``actor_required``, because
    ``Actor.system()`` recorded for a human's decision is unreviewable. The call cannot be
    anonymous any more, so that refusal has nothing left to catch and the answer is recorded
    under a verified human.
    """
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)

    response = client.post(f"/runs/{run_id}/approvals/{decision_id}/{endpoint}", json={})

    assert response.status_code == 200
    approval = _human_approval_event(client, run_id, decision_id)
    assert approval.actor.kind is ActorKind.HUMAN
    assert approval.actor.id == DEFAULT_ACTOR_ID
    assert approval.actor.authenticated is True


def test_governance_approve_records_the_multi_principal_resolver_identity(
    tmp_path: Path,
) -> None:
    """A multi-principal deployment records the exact principal whose token was presented.

    ``BearerTokenRegistry`` remains the supported multi-identity resolver behind the same
    protocol, and the approver's own role reaches the evidence -- the seam FINAL's JWT/OIDC
    verifier slots into.
    """
    client = _authenticated_client(tmp_path)
    run_id, decision_id = _park_for_approval(client, headers=_bearer(_WRITER_TOKEN))

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/approve",
        json={"actor": "val", "reason": "Evidence reviewed."},
        headers=_bearer(_APPROVER_TOKEN),
    )

    assert response.status_code == 200
    approval = _human_approval_event(client, run_id, decision_id)
    assert approval.actor.id == "val"
    assert approval.actor.role == "risk-officer"
    assert approval.actor.authenticated is True


def test_a_governance_body_actor_cannot_displace_a_resolved_principal(tmp_path: Path) -> None:
    """Even with a real multi-principal resolver, the body cannot name a different approver."""
    client = _authenticated_client(tmp_path)
    run_id, decision_id = _park_for_approval(client, headers=_bearer(_WRITER_TOKEN))

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/approve",
        json={"actor": "ceo@example.com", "reason": "I approve myself."},
        headers=_bearer(_APPROVER_TOKEN),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "actor_mismatch"


def test_approving_a_decision_after_the_run_was_cancelled_is_refused(tmp_path: Path) -> None:
    """The stale-resolve hole is closed at the HTTP boundary, not only in the fold.

    Nothing checked the Run's condition before recording a ``human.approval``, so an answer could
    still be written for a Run that had left the state which raised the request. Cancelling
    closes the request, so the decision-scoped route refuses it and appends nothing.
    """
    client = _client(tmp_path)
    run_id, decision_id = _park_for_approval(client)
    assert client.post(f"/runs/{run_id}/cancel").status_code == 202

    response = client.post(
        f"/runs/{run_id}/approvals/{decision_id}/approve",
        json={"reason": "Too late."},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "approval_not_pending"
    events = cast("FastAPI", client.app).state.runtime_deps.event_store.read(run_id)
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in events)
