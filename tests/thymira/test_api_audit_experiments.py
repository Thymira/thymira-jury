"""Integration tests for the RA-API-05 audit and experiment read surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL, authenticated_client
from thymira.api import build_default_deps, create_app
from thymira.core import ExecutionDispatcher, RunController, RunTransitionKind
from thymira.mira.checks import AuditReport, ControlResult, ControlStatus
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import (
    Actor,
    EventType,
    Experiment,
    PolicyDecision,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class _NoopDispatcher:
    """Keep API fixtures from invoking the unconfigured production graph."""

    def submit(self, _run_id: str) -> None:
        """Accept one create dispatch without executing a graph."""

    def resume(self, _run_id: str) -> None:
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


def _completed_run(client: TestClient) -> tuple[FastAPI, str]:
    """Create and complete a Run through the public state transition boundary."""
    app = cast("FastAPI", client.app)
    deps = app.state.runtime_deps
    run = client.post("/runs", json={"prompt": "Audit the model"}).json()
    controller = RunController(deps.run_store)
    for command in (
        RunTransitionKind.START,
        RunTransitionKind.BEGIN_EXECUTION,
        RunTransitionKind.BEGIN_AUDIT,
        RunTransitionKind.BEGIN_REPORTING,
        RunTransitionKind.COMPLETE,
    ):
        controller.advance(run["id"], command)
    return app, run["id"]


def _record_run_decision(app: FastAPI, run_id: str) -> PolicyDecision:
    """Record the run-level findings decision that the audit route must fold."""
    deps = app.state.runtime_deps
    decision = deps.gate_factory(run_id).review_findings(())
    assert decision.subject_kind == "findings"
    return decision


def _record_audit(app: FastAPI, run_id: str) -> AuditReport:
    """Persist the report snapshot that the audit route reads."""
    deps = app.state.runtime_deps
    events = deps.run_store.events(run_id)
    report = AuditReport(
        run_id=run_id,
        status="passed",
        controls=(
            ControlResult(
                control_id="TEST-API",
                title="Persisted test evidence",
                status=ControlStatus.PASSED,
                severity=Severity.LOW,
            ),
        ),
        terminal_hash=events[-1].hash,
        manifest_sha256=artifact_manifest_sha256(deps.artifact_store_factory(run_id)),
        summary={"passed": 1},
    )
    deps.event_store.open(run_id).append(
        EventType.AUDIT_COMPLETED,
        Actor.system(),
        {
            "status": report.status,
            "audit_report": report.model_dump(mode="json"),
            "verified_dropped": 0,
        },
        subject_id=run_id,
        producer="thymira.mira",
    )
    return report


def test_audit_returns_report_and_run_decision_without_writing_events(tmp_path: Path) -> None:
    """The audit endpoint reads deterministic evidence and leaves the log unchanged."""
    client = _client(tmp_path)
    app, run_id = _completed_run(client)
    persisted = _record_audit(app, run_id)
    decision = _record_run_decision(app, run_id)
    deps = app.state.runtime_deps
    before = deps.run_store.events(run_id)

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 200
    body = response.json()
    assert body["report"]["run_id"] == run_id
    assert body["report"] == persisted.model_dump(mode="json")
    assert body["decision"]["id"] == decision.id
    assert body["decision"]["decision"] == decision.decision.value
    assert deps.run_store.events(run_id) == before


def test_experiments_returns_records_in_insertion_order(tmp_path: Path) -> None:
    """The experiments route uses the repository's stable insertion order."""
    client = _client(tmp_path)
    app, run_id = _completed_run(client)
    repository = app.state.runtime_deps.record_repository
    first = Experiment(id=new_id("experiment"), run_id=run_id, name="baseline")
    second = Experiment(id=new_id("experiment"), run_id=run_id, name="tuned")
    repository.save(first)
    repository.save(second)

    response = client.get(f"/runs/{run_id}/experiments")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": run_id,
        "items": [first.model_dump(mode="json"), second.model_dump(mode="json")],
    }


def test_experiments_returns_an_empty_list_when_no_records_exist(tmp_path: Path) -> None:
    """A valid Run without experiments has an explicit empty result."""
    client = _client(tmp_path)
    _app, run_id = _completed_run(client)

    response = client.get(f"/runs/{run_id}/experiments")

    assert response.status_code == 200
    assert response.json() == {"run_id": run_id, "items": []}


@pytest.mark.parametrize("suffix", ["audit", "experiments"])
def test_read_surfaces_reject_unknown_or_foreign_runs(tmp_path: Path, suffix: str) -> None:
    """Both read surfaces enforce the same project scope as the existing Run routes."""
    client = _client(tmp_path)

    unknown = client.get(f"/runs/{new_id('run')}/{suffix}")
    malformed = client.get(f"/runs/{new_id('project')}/{suffix}")

    assert unknown.status_code == 404
    assert unknown.json()["code"] == "run_not_found"
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "invalid_run_id"


def test_audit_rejects_a_malformed_policy_decision_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt evidence is reported as an integrity problem, never silently discarded."""
    client = _client(tmp_path)
    app, run_id = _completed_run(client)
    deps = app.state.runtime_deps
    deps.gate_factory(run_id).review_findings(())
    event = next(event for event in deps.run_store.events(run_id) if event.payload.get("decision"))
    events = deps.run_store.events(run_id)
    tampered = [
        current.model_copy(update={"payload": {"decision": "not-a-decision"}})
        if current.seq == event.seq
        else current
        for current in events
    ]
    monkeypatch.setattr(deps.event_store, "read", lambda _run_id: tampered)

    response = client.get(f"/runs/{run_id}/audit")

    assert response.status_code == 409
    assert response.json()["code"] == "run_integrity_error"
