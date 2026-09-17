"""Integration tests for the persisted, read-only MIRA audit surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import build_default_deps, create_app
from thymira.mira import (
    ASSURANCE_BUNDLE_EXPORT,
    AssuranceBundle,
    assemble_run_assurance,
    canonical_graph_definition_hash,
    checks,
    verify_assurance_bundle,
)
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import Actor, EventType, Id

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from thymira.api.deps import RuntimeDeps
    from thymira.core import ExecutionDispatcher

pytestmark = pytest.mark.integration


class _NoopDispatcher:
    """Keep API fixtures from invoking the production graph."""

    def submit(self, _run_id: Id) -> None:
        """Accept one create dispatch without executing a graph."""

    def resume(self, _run_id: Id) -> None:
        """Accept one resume dispatch without executing a graph."""


def _client(tmp_path: Path) -> TestClient:
    """Build an API client with one configured local project."""
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".thymira"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "project:\n  name: credit-risk\n  domain: credit_risk\n",
        encoding="utf-8",
        newline="\n",
    )
    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        dispatcher=cast("ExecutionDispatcher", _NoopDispatcher()),
        principal_resolver=TEST_CREDENTIAL,
    )
    return authenticated_client(create_app(deps))


def _persist_audit(client: TestClient, run_id: Id) -> AuditReport:
    """Write one valid audit.completed snapshot through the production event boundary."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
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
    return report


def test_get_audit_returns_the_persisted_snapshot_without_running_mira(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns the exact event snapshot and does not execute a second audit."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Audit me"}).json()["id"]
    report = _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    before = deps.run_store.events(run_id)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        """Fail if the read route tries to recalculate MIRA."""
        raise AssertionError("GET /audit must not execute MIRA")

    monkeypatch.setattr(checks, "audit_run", fail_audit)
    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 200
    assert response.json()["report"] == report.model_dump(mode="json")
    assert deps.run_store.events(run_id) == before


def test_get_audit_reports_absence_before_audit_completed(tmp_path: Path) -> None:
    """A Run without an audit snapshot has an explicit not-found response."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Not audited yet"}).json()["id"]

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 404
    assert response.json()["code"] == "audit_not_found"


def test_get_audit_rejects_a_report_from_another_graph_version(tmp_path: Path) -> None:
    """A valid old snapshot is not served as if it were current."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Old graph"}).json()["id"]
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    store = deps.artifact_store_factory(run_id)
    events = deps.run_store.events(run_id)
    report = AuditReport(
        run_id=run_id,
        status="passed",
        controls=(),
        terminal_hash=events[0].hash,
        manifest_sha256=artifact_manifest_sha256(store),
    )
    deps.run_store.append(
        run_id,
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "status": report.status,
            "audit_report": report.model_dump(mode="json"),
            "graph_definition_hash": "old-graph",
        },
        expected_version=len(events),
    )
    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 409
    assert response.json()["code"] == "audit_stale"
    assert canonical_graph_definition_hash() != "old-graph"


def test_get_audit_reports_stale_evidence_after_new_tool_output(tmp_path: Path) -> None:
    """Execution evidence appended after completion invalidates the served snapshot."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Stale audit"}).json()["id"]
    _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    app.state.runtime_deps.event_store.open(run_id).append(
        EventType.TOOL_COMPLETED,
        Actor.system(),
        {"tool": "profile_dataset", "status": "COMPLETED"},
    )

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 409
    assert response.json()["code"] == "audit_stale"


def test_get_audit_rejects_a_malformed_persisted_report(tmp_path: Path) -> None:
    """A malformed audit.completed payload is an integrity error, not a fresh audit trigger."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Malformed audit"}).json()["id"]
    app = cast("FastAPI", client.app)
    app.state.runtime_deps.event_store.open(run_id).append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {"audit_report": {"run_id": run_id}},
        subject_id=run_id,
        producer="thymira.mira",
    )

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 409
    assert response.json()["code"] == "audit_integrity_error"


def _write_bundle(deps: RuntimeDeps, run_id: Id) -> AssuranceBundle:
    """Persist the export exactly as the composition writes it when a Run completes."""
    store = deps.run_store
    bundle = assemble_run_assurance(
        run_id,
        events=store.events(run_id),
        run=store.get(run_id),
        store=deps.artifact_store_factory(run_id),
    )
    assert bundle is not None
    store.write_export(run_id, ASSURANCE_BUNDLE_EXPORT, bundle.to_json_dict())
    return bundle


def test_get_assurance_reads_the_bundle_the_runtime_produced_at_completion(
    tmp_path: Path,
) -> None:
    """The assurance surface reads and re-verifies the export; it never builds or writes one."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Bundle me"}).json()["id"]
    _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    deps.gate_factory(run_id).review_findings(())
    _write_bundle(deps, run_id)
    before = deps.run_store.events(run_id)

    response = client.get(f"/runs/{run_id}/assurance")

    assert response.status_code == 200
    returned = AssuranceBundle.model_validate_json(response.content)
    assert returned.run_id == run_id
    # The persisted export is a redacted projection: its altered representation has no digest
    # claiming identity with the full in-memory bundle and carries no embedded raw event copy.
    assert returned.bundle_sha256 is None
    assert returned.verified_events == ()
    persisted = deps.run_store.read_export(run_id, ASSURANCE_BUNDLE_EXPORT)
    assert isinstance(persisted, dict)
    assert "verified_events" not in persisted
    assert verify_assurance_bundle(
        returned,
        events=before,
        store=deps.artifact_store_factory(run_id),
    ).valid
    assert deps.run_store.events(run_id) == before


def test_get_assurance_reports_a_run_whose_bundle_was_never_produced(tmp_path: Path) -> None:
    """Reading is not a generation trigger: an absent bundle is reported, never rebuilt."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "No bundle"}).json()["id"]
    _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    deps.gate_factory(run_id).review_findings(())

    response = client.get(f"/runs/{run_id}/assurance")

    assert response.status_code == 404
    assert response.json()["code"] == "assurance_not_found"
    assert deps.run_store.read_export(run_id, ASSURANCE_BUNDLE_EXPORT) is None


def test_get_assurance_rejects_a_tampered_persisted_bundle(tmp_path: Path) -> None:
    """A bundle whose export no longer replays against the Run is an integrity error."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Tampered bundle"}).json()["id"]
    _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    deps.gate_factory(run_id).review_findings(())
    bundle = _write_bundle(deps, run_id)
    payload = bundle.to_json_dict()
    payload["disclaimer"] = "Certified."
    deps.run_store.write_export(run_id, ASSURANCE_BUNDLE_EXPORT, payload)

    response = client.get(f"/runs/{run_id}/assurance")

    assert response.status_code == 409
    assert response.json()["code"] == "assurance_integrity_error"


def test_get_assurance_rejects_an_unreadable_persisted_bundle(tmp_path: Path) -> None:
    """An export that is no longer a bundle at all fails closed instead of being rebuilt."""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"prompt": "Broken bundle"}).json()["id"]
    _persist_audit(client, run_id)
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    deps.run_store.write_export(run_id, ASSURANCE_BUNDLE_EXPORT, {"run_id": run_id})

    response = client.get(f"/runs/{run_id}/assurance")

    assert response.status_code == 409
    assert response.json()["code"] == "assurance_integrity_error"
