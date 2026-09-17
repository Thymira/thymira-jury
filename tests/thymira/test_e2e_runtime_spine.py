"""Runtime-spine integration tests through the CLI, API and local composition graph.

The real boundary is the Typer CLI, ``ApiClient``, FastAPI ``TestClient``, the runtime
composition graph, the Policy Engine and local JSON/JSONL persistence. THY and MIRA are small
scripted subgraph doubles so the suite remains deterministic and never calls a model or service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import TEST_CREDENTIAL, TEST_TOKEN, authenticated_client
from thymira.api import RuntimeDeps, build_default_deps, create_app
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient
from thymira.core import (
    RunController,
    RuntimeState,
    StateCheckpointer,
    SubgraphDeps,
    UsageLedger,
    build_runtime_graph,
)
from thymira.core.phases import Phase
from thymira.events import verify_events
from thymira.mira.checks import AuditReport
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import Actor, AuditFinding, EventType, Framework, Run, Severity

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_runner = CliRunner()


class _ScriptedExecutionError(RuntimeError):
    """Represent a deterministic execution failure in the test graph."""


class _ScriptedThy:
    """Minimal THY subgraph that either advances or fails predictably."""

    name = "scripted-thy"

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "scripted-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Advance the shared state or raise the configured execution failure."""
        del deps
        if self._fail:
            raise _ScriptedExecutionError("scripted THY execution failed")
        return state.model_copy(update={"phase": Phase.PREPARATION})


class _ScriptedMira:
    """Minimal MIRA subgraph that emits an audit fact and an optional finding."""

    name = "scripted-mira"

    def __init__(self, finding: AuditFinding | None = None) -> None:
        self._finding = finding

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "scripted-mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Record audit completion and expose the scripted finding to the Gate."""
        events = tuple(deps.event_log.events())
        findings = (self._finding,) if self._finding is not None else ()
        report = AuditReport(
            run_id=state.run_id,
            status="passed_with_warnings" if findings else "passed",
            controls=(),
            findings=findings,
            terminal_hash=events[-1].hash,
            manifest_sha256=artifact_manifest_sha256(deps.artifact_store),
            policy_sha256=deps.gate.engine.policy_sha256,
        )
        revision = sum(event.type is EventType.AUDIT_COMPLETED for event in events) + 1
        deps.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "status": report.status,
                "audit_revision": revision,
                "audit_report": report.model_dump(mode="json"),
            },
            subject_id=state.run_id,
            producer="tests.thymira",
            producer_version="0.1",
        )
        return state.model_copy(update={"findings": findings})


@dataclass(frozen=True, slots=True)
class _Harness:
    """Own one isolated API and CLI client pair for a test case."""

    api: TestClient
    cli: ApiClient
    deps: RuntimeDeps


@pytest.fixture
def harness(tmp_path: Path) -> Generator[_Harness, None, None]:
    """Build a real in-process API backed by isolated local persistence."""
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: runtime-spine\n  domain: test\n",
        encoding="utf-8",
        newline="\n",
    )

    dependency_ref: list[RuntimeDeps] = []

    def graph_factory(run: Run):
        deps = dependency_ref[0]
        event_log = deps.event_store.open(run.id)
        finding: AuditFinding | None = None
        if "warning" in run.prompt:
            finding = AuditFinding(
                id=f"finding_{run.id.removeprefix('run_')}",
                run_id=run.id,
                control_id="TEST-WARNING",
                framework=Framework.EU_AI_ACT,
                title="Scripted medium risk",
                finding="The scripted test produced a medium-severity finding.",
                severity=Severity.MEDIUM,
                confidence=0.8,
            )
        elif "review" in run.prompt:
            finding = AuditFinding(
                id=f"finding_{run.id.removeprefix('run_')}",
                run_id=run.id,
                control_id="TEST-REVIEW",
                framework=Framework.EU_AI_ACT,
                title="Scripted human review",
                finding="The scripted test requires a human decision.",
                severity=Severity.HIGH,
                confidence=0.95,
            )
        subgraph_deps = SubgraphDeps(
            event_log=event_log,
            gate=deps.gate_factory(run.id),
            artifact_store=deps.artifact_store_factory(run.id),
            tool_manager=deps.tool_manager,
            record_repository=deps.record_repository,
            usage_ledger=UsageLedger(),
        )
        return build_runtime_graph(
            _ScriptedThy(fail="fail" in run.prompt),
            _ScriptedMira(finding),
            checkpointer=StateCheckpointer(deps.checkpoint_repository),
            deps=subgraph_deps,
            controller=RunController(deps.run_store),
        )

    deps = build_default_deps(
        tmp_path / "runtime",
        workspace=workspace,
        graph_factory=graph_factory,
        graph_definition_hash="1" * 64,
        principal_resolver=TEST_CREDENTIAL,
    )
    dependency_ref.append(deps)
    api = authenticated_client(create_app(deps))

    def transport(request: httpx.Request) -> httpx.Response:
        """Adapt synchronous ApiClient requests to the in-process FastAPI client."""
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query.decode()}"
        response = api.request(
            request.method,
            target,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    cli = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(transport))
    try:
        yield _Harness(api=api, cli=cli, deps=deps)
    finally:
        cli.close()
        api.close()


def _run_from_cli(harness: _Harness, prompt: str) -> str:
    """Create one Run through the real CLI and return its displayed identifier."""
    result = _runner.invoke(app, ["run", prompt], obj=harness.cli)
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.strip().startswith("ID"))
    return line.split()[-1]


def _status_from_cli(harness: _Harness, run_id: str) -> str:
    """Read one Run through the real CLI and return its displayed status."""
    result = _runner.invoke(app, ["status", run_id], obj=harness.cli)
    assert result.exit_code == 0, result.output
    line = next(line for line in result.output.splitlines() if line.strip().startswith("Status"))
    return line.split()[-1]


def _assert_read_surfaces_agree(harness: _Harness, run_id: str, expected_status: str) -> None:
    """Check status, audit and experiment surfaces against the persisted event evidence."""
    run = harness.cli.get_run(run_id)
    audit = harness.api.get(f"/runs/{run_id}/audit")
    experiments = harness.api.get(f"/runs/{run_id}/experiments")
    events = harness.deps.run_store.events(run_id)

    assert run.status == expected_status
    if expected_status == "FAILED":
        assert audit.status_code == 404
        assert audit.json()["code"] == "audit_not_found"
    else:
        assert audit.status_code == 200
        assert audit.json()["report"]["run_id"] == run_id
        decisions = [
            event.payload
            for event in events
            if event.type is EventType.POLICY_DECISION
            and event.payload.get("subject_kind") == "findings"
        ]
        audit_decision = audit.json()["decision"]
        if decisions:
            assert audit_decision["decision"] == decisions[-1]["decision"]
        else:
            assert audit_decision is None
    assert experiments.status_code == 200
    assert experiments.json() == {"run_id": run_id, "items": []}
    assert verify_events(events).valid


def test_happy_path_completes_through_cli_api_and_runtime(harness: _Harness) -> None:
    """A clean scripted run reaches COMPLETED and all read surfaces agree."""
    run_id = _run_from_cli(harness, "happy path")

    assert _status_from_cli(harness, run_id) == "COMPLETED"
    _assert_read_surfaces_agree(harness, run_id, "COMPLETED")
    assert any(
        event.type is EventType.RUN_COMPLETED for event in harness.deps.run_store.events(run_id)
    )


def test_failed_execution_is_persisted_as_failed(harness: _Harness) -> None:
    """A graph execution error becomes a terminal FAILED Run with verifiable evidence."""
    run_id = _run_from_cli(harness, "fail path")

    assert _status_from_cli(harness, run_id) == "FAILED"
    _assert_read_surfaces_agree(harness, run_id, "FAILED")
    events = harness.deps.run_store.events(run_id)
    failed = next(event for event in events if event.type is EventType.RUN_FAILED)
    assert failed.payload["error"].startswith("run " + run_id + ": inline execution failed: ")


def test_warning_decision_completes_and_is_exposed_by_audit(harness: _Harness) -> None:
    """A medium scripted finding becomes WARNING through the real Policy Engine."""
    run_id = _run_from_cli(harness, "warning path")

    assert _status_from_cli(harness, run_id) == "COMPLETED"
    _assert_read_surfaces_agree(harness, run_id, "COMPLETED")
    decisions = [
        event.payload
        for event in harness.deps.run_store.events(run_id)
        if event.type is EventType.POLICY_DECISION
    ]
    assert decisions[-1]["decision"] == "WARNING"


def test_human_review_can_be_approved_through_cli(harness: _Harness) -> None:
    """A high-confidence finding waits for a human and completes after CLI approval."""
    run_id = _run_from_cli(harness, "review path")

    assert _status_from_cli(harness, run_id) == "WAITING_FOR_APPROVAL"
    approval = _runner.invoke(
        app,
        ["approve", run_id, "--note", "Evidence reviewed."],
        obj=harness.cli,
    )
    assert approval.exit_code == 0, approval.output
    assert "Run approved" in approval.output
    assert _status_from_cli(harness, run_id) == "COMPLETED"
    _assert_read_surfaces_agree(harness, run_id, "COMPLETED")

    events = harness.deps.run_store.events(run_id)
    assert any(
        event.type is EventType.HUMAN_APPROVAL and event.payload.get("approved") is True
        for event in events
    )
    decision = next(event for event in events if event.type is EventType.POLICY_DECISION)
    assert decision.payload["decision"] == "REQUIRE_HUMAN_REVIEW"
