"""End-to-end governance test: PASS / WARNING / REQUIRE_HUMAN_REVIEW+approve / BLOCK (QA-E2E-GOV).

This is the product's reason to exist, exercised end to end: a MIRA finding reaches the
deterministic Policy Engine, the engine (never a model) returns ``PASS | WARNING |
REQUIRE_HUMAN_REVIEW | BLOCK``, and only a checkable human ``Approval`` -- never a model's say-so --
unblocks a Run parked for review.

The real boundary is the Typer CLI, ``ApiClient``, the FastAPI ``TestClient``, the runtime
composition graph (``build_runtime_graph``: THY -> MIRA -> Gate.review_findings -> terminal), MIRA's
real audit-agent runner driven by a ``ScriptedProvider`` (no model or network is ever called), the
real Policy Engine over the packaged base governance policy, the ``RunController`` and the local
JSON/JSONL persistence stack. Each case reconstructs its decision from the hash-chained event log
and asserts ``verify_events(...).valid``: the evidence, not internal state, is the proof.

Two things about the shape are load-bearing, and documented so they are not mistaken for shortcuts:

1. MIRA audits this composition *in flight*: the helper excludes A2/A7/A8 with
   ``AuditMode.IN_FLIGHT`` because Core has not reached its findings Gate or Run closeout yet. A
   final audit still evaluates those controls against terminal events in ``test_mira_checks.py``
   and ``test_mira_flow.py``. The four governance decisions are pinned by giving the Gate a
   controlled finding set, produced by the *real* MIRA audit agent (``run_audit_agent`` via
   ``build_audit_agent_runner``) whose model is a ``ScriptedProvider``. The agent runs inside the
   composed Run, so its ``agent.started`` / ``model.selected`` / ``agent.completed`` lifecycle lands
   in the Run's own hash-chained log.

2. Why THY is a double. A real ThyGraph would ask the shared Gate to authorise its ``plan.proposed``
   action, which the base policy itself escalates to REQUIRE_HUMAN_REVIEW -- a second, unrelated
   review that would obscure the single findings decision each case pins. The THY double emits
   nothing and preserves the Run identity, so the only run-level ``policy.decision`` a case records
   is the Gate's findings review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from tests.thymira.api_support import (
    DEFAULT_ACTOR_ID,
    TEST_CREDENTIAL,
    TEST_TOKEN,
    authenticated_client,
)
from thymira.agents.llm.scripted import ScriptedProvider
from thymira.api import RuntimeDeps, build_default_deps, create_app
from thymira.cli.__main__ import app
from thymira.cli.client import ApiClient
from thymira.core import (
    RunController,
    RuntimeState,
    StateCheckpointer,
    SubgraphDeps,
    UsageLedger,
    build_audit_agent_runner,
    build_runtime_graph,
)
from thymira.events import verify_events
from thymira.mira import AuditAgentSpec
from thymira.mira.checks import AuditContext, AuditMode, audit_run
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import Actor, EventType, Framework, ModelRoutePolicy, Run, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from fastapi.testclient import TestClient
    from langgraph.graph.state import CompiledStateGraph

    from thymira.schemas import Event

pytestmark = pytest.mark.integration

_runner = CliRunner()

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct audit-agent provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


class _ScriptedThy:
    """A THY double that advances the composition without exercising the plan gate.

    A real ThyGraph would ask the shared Gate to authorise ``plan.proposed``, which the base policy
    escalates to REQUIRE_HUMAN_REVIEW -- a second review that would obscure the one findings
    decision under test. This double emits nothing and preserves the Run identity.
    """

    name = "scripted-thy"

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "governance-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Return the shared state unchanged; the composition drives the lifecycle."""
        del deps
        return state


@dataclass(frozen=True, slots=True)
class _MiraCase:
    """A governance scenario: the audit-agent spec and the single structured response it returns."""

    spec: AuditAgentSpec
    response: dict[str, object]


class _AuditAgentMira:
    """A MIRA subgraph that runs the real audit-agent runner inside the composed Run.

    ``invoke`` emits ``audit.started``, runs one shipped MIRA audit agent through the unchanged
    :func:`run_audit_agent` (via :func:`build_audit_agent_runner`) with a ``ScriptedProvider`` in
    place of a model, records one ``audit.finding`` per returned finding, and returns the state with
    those findings and persists one report snapshot. It writes no ``policy.decision`` because this
    test double leaves the run-level decision to the composition; the production ``MiraSubgraph``
    obtains the same audit fact from MIRA's canonical flow before Core's ``review`` Gate.
    """

    name = "scripted-mira"

    def __init__(self, case: _MiraCase) -> None:
        self._case = case

    def graph_version(self) -> str:
        """Return the stable test graph version."""
        return "governance-mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Run the scripted audit agent over the Run and expose its findings to the Gate."""
        deps.event_log.append(
            EventType.AUDIT_STARTED,
            Actor.system(),
            {"audited_at": utc_now().isoformat()},
            subject_id=state.run_id,
            producer="thymira.mira",
        )
        report = audit_run(
            AuditContext(
                state.run_id,
                tuple(deps.event_log.events()),
                audit_mode=AuditMode.IN_FLIGHT,
            )
        )
        control_ids = {control.control_id for control in report.controls}
        assert {"A2", "A7", "A8"}.isdisjoint(control_ids)
        events = tuple(deps.event_log.events())
        runner = build_audit_agent_runner(
            deps.event_log,
            Actor.system(),
            provider=ScriptedProvider([self._case.response]),
            route_policy=TEST_ROUTE_POLICY,
        )
        findings = tuple(runner(self._case.spec, state.run_id, events, report).findings)
        for finding in findings:
            deps.event_log.append(
                EventType.AUDIT_FINDING,
                Actor.system(),
                {"finding": finding.model_dump(mode="json")},
                subject_id=finding.id,
                producer="thymira.mira",
            )
        report = report.model_copy(
            update={
                "findings": findings,
                "manifest_sha256": artifact_manifest_sha256(deps.artifact_store),
                "terminal_hash": deps.event_log.events()[-1].hash,
                "policy_sha256": deps.gate.engine.policy_sha256,
            }
        )
        audit_revision = (
            sum(event.type is EventType.AUDIT_COMPLETED for event in deps.event_log.events()) + 1
        )
        deps.event_log.append(
            EventType.AUDIT_COMPLETED,
            Actor.system(),
            {
                "status": report.status,
                "audit_revision": audit_revision,
                "audit_report": report.model_dump(mode="json"),
            },
            subject_id=state.run_id,
            producer="thymira.mira",
        )
        return state.model_copy(update={"findings": findings})


def _spec(framework: Framework) -> AuditAgentSpec:
    """Build a one-turn, tool-free MIRA audit-agent declaration for one framework."""
    return AuditAgentSpec(
        name=f"{framework.value.lower()}-e2e-agent",
        framework=framework,
        task_kinds=("audit_judgement",),
        max_turns=1,
        system_prompt="Return candidate findings for the governance end-to-end test only.",
    )


def _finding_response(
    *, control_id: str, title: str, finding: str, severity: str, confidence: float
) -> dict[str, object]:
    """Render one ``_AuditAgentResponse``-shaped scripted response carrying a single finding."""
    return {
        "findings": [
            {
                "control_id": control_id,
                "title": title,
                "finding": finding,
                "severity": severity,
                "confidence": confidence,
                "evidence": [],
            }
        ]
    }


# The four scenarios. The scripted severity/confidence is what the base policy maps to a decision:
# no findings -> PASS; MEDIUM -> WARNING (GOV-104); a HIGH, high-confidence regulatory finding ->
# REQUIRE_HUMAN_REVIEW (GOV-102/103); CRITICAL -> BLOCK (GOV-101, the highest-precedence rule).
_PASS = _MiraCase(spec=_spec(Framework.METHODOLOGY), response={"findings": []})
_WARNING = _MiraCase(
    spec=_spec(Framework.METHODOLOGY),
    response=_finding_response(
        control_id="MIRA-METH-SPLIT",
        title="Validation split not documented",
        finding="The train/validation split was not recorded; the result needs a caveat.",
        severity="MEDIUM",
        confidence=0.8,
    ),
)
_REVIEW = _MiraCase(
    spec=_spec(Framework.CREDIT_RISK),
    response=_finding_response(
        control_id="CR-DISPARATE-IMPACT",
        title="Potential disparate impact on a protected group",
        finding="Approval rates differ across a protected attribute; a human must assess it.",
        severity="HIGH",
        confidence=0.95,
    ),
)
_BLOCK = _MiraCase(
    spec=_spec(Framework.EU_AI_ACT),
    response=_finding_response(
        control_id="CR-LEAKAGE",
        title="Target leakage detected",
        finding="A feature encodes the label; the reported metrics cannot be trusted.",
        severity="CRITICAL",
        confidence=0.99,
    ),
)


@dataclass(frozen=True, slots=True)
class _Harness:
    """Own one isolated API and CLI client pair for a governance test case."""

    api: TestClient
    cli: ApiClient
    deps: RuntimeDeps


@pytest.fixture
def make_harness(tmp_path: Path) -> Generator[Callable[[_MiraCase], _Harness]]:
    """Yield a builder for an in-process API whose composition runs one scripted MIRA agent."""
    created: list[_Harness] = []

    def _build(case: _MiraCase) -> _Harness:
        root = tmp_path / f"harness{len(created)}"
        workspace = root / "workspace"
        (workspace / ".thymira").mkdir(parents=True)
        (workspace / ".thymira" / "config.yaml").write_text(
            "project:\n  name: governance-e2e\n  domain: credit_risk\n",
            encoding="utf-8",
            newline="\n",
        )

        dependency_ref: list[RuntimeDeps] = []

        def graph_factory(run: Run) -> CompiledStateGraph:
            deps = dependency_ref[0]
            subgraph_deps = SubgraphDeps(
                event_log=deps.event_store.open(run.id),
                gate=deps.gate_factory(run.id),
                artifact_store=deps.artifact_store_factory(run.id),
                tool_manager=deps.tool_manager,
                record_repository=deps.record_repository,
                usage_ledger=UsageLedger(),
            )
            return build_runtime_graph(
                _ScriptedThy(),
                _AuditAgentMira(case),
                checkpointer=StateCheckpointer(deps.checkpoint_repository),
                deps=subgraph_deps,
                controller=RunController(deps.run_store),
            )

        deps = build_default_deps(
            root / "runtime",
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

        cli = ApiClient(
            "http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(transport)
        )
        harness = _Harness(api=api, cli=cli, deps=deps)
        created.append(harness)
        return harness

    yield _build
    for harness in created:
        harness.cli.close()
        harness.api.close()


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


def _findings_decisions(events: list[Event]) -> list[str]:
    """Reconstruct, from the hash-chained log alone, every run-level findings decision in order."""
    return [
        str(event.payload.get("decision"))
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]


def _mira_agent_started(events: list[Event]) -> bool:
    """Return whether MIRA's scripted audit agent ran in-Run (its start is stamped by MIRA)."""
    return any(
        event.type is EventType.AGENT_STARTED and event.producer == "thymira.mira"
        for event in events
    )


def _audit_decision_via_cli(harness: _Harness, run_id: str) -> str | None:
    """Read the run-level findings decision the ``thymira audit`` read surface exposes."""
    result = _runner.invoke(app, ["audit", run_id, "--json"], obj=harness.cli)
    assert result.exit_code == 0, result.output
    response = harness.api.get(f"/runs/{run_id}/audit")
    assert response.status_code == 200
    decision = response.json()["decision"]
    return None if decision is None else str(decision["decision"])


def test_clean_run_passes_and_records_a_pass_decision(
    make_harness: Callable[[_MiraCase], _Harness],
) -> None:
    """A finding-free MIRA audit yields PASS and completes with a verifiable log."""
    harness = make_harness(_PASS)
    run_id = _run_from_cli(harness, "governance pass scenario")

    assert _status_from_cli(harness, run_id) == "COMPLETED"
    events = harness.deps.run_store.events(run_id)
    assert _findings_decisions(events) == ["PASS"]
    # The real MIRA audit agent ran in-Run over the ScriptedProvider, and found nothing to raise.
    assert _mira_agent_started(events)
    assert not any(event.type is EventType.AUDIT_FINDING for event in events)
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert _audit_decision_via_cli(harness, run_id) == "PASS"
    assert verify_events(events).valid


def test_medium_finding_warns_and_completes(
    make_harness: Callable[[_MiraCase], _Harness],
) -> None:
    """A single MEDIUM finding is a WARNING and still completes the Run."""
    harness = make_harness(_WARNING)
    run_id = _run_from_cli(harness, "governance warning scenario")

    assert _status_from_cli(harness, run_id) == "COMPLETED"
    events = harness.deps.run_store.events(run_id)
    assert _findings_decisions(events) == ["WARNING"]
    assert _mira_agent_started(events)
    assert sum(1 for event in events if event.type is EventType.AUDIT_FINDING) == 1
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert _audit_decision_via_cli(harness, run_id) == "WARNING"
    assert verify_events(events).valid


def test_high_confidence_finding_requires_review_then_approval_completes(
    make_harness: Callable[[_MiraCase], _Harness],
) -> None:
    """A HIGH-confidence credit-risk finding parks the Run until a checkable approval resumes it."""
    harness = make_harness(_REVIEW)
    run_id = _run_from_cli(harness, "governance review scenario")

    # The Run is parked: the decision is recorded and a human answer is requested but not given.
    assert _status_from_cli(harness, run_id) == "WAITING_FOR_APPROVAL"
    parked = harness.deps.run_store.events(run_id)
    assert _findings_decisions(parked) == ["REQUIRE_HUMAN_REVIEW"]
    assert any(event.type is EventType.HUMAN_APPROVAL_REQUESTED for event in parked)
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in parked)
    assert verify_events(parked).valid

    approval = _runner.invoke(
        app,
        ["approve", run_id, "--note", "Evidence reviewed."],
        obj=harness.cli,
    )
    assert approval.exit_code == 0, approval.output
    assert "Run approved" in approval.output

    assert _status_from_cli(harness, run_id) == "COMPLETED"
    events = harness.deps.run_store.events(run_id)
    # Exactly one findings decision: the approval re-evaluates the Gate's routing, not the audit.
    assert _findings_decisions(events) == ["REQUIRE_HUMAN_REVIEW"]
    approvals = [
        event
        for event in events
        if event.type is EventType.HUMAN_APPROVAL and event.payload.get("approved") is True
    ]
    assert len(approvals) == 1
    # The Approval is checkable: a real human actor answered this exact decision.
    assert approvals[0].actor.id == DEFAULT_ACTOR_ID
    assert approvals[0].actor.authenticated is True
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert verify_events(events).valid


def test_critical_finding_blocks_and_nothing_runs_after_the_block(
    make_harness: Callable[[_MiraCase], _Harness],
) -> None:
    """A CRITICAL finding BLOCKs the Run, emits audit.block, and nothing executes after it."""
    harness = make_harness(_BLOCK)
    run_id = _run_from_cli(harness, "governance block scenario")

    assert _status_from_cli(harness, run_id) == "BLOCKED"
    events = harness.deps.run_store.events(run_id)
    assert _findings_decisions(events) == ["BLOCK"]

    block_index = next(
        index for index, event in enumerate(events) if event.type is EventType.AUDIT_BLOCK
    )
    after_block = events[block_index + 1 :]
    # After the audit block only the policy decision and the terminal transition are recorded --
    # no tool ever starts and no further work runs once the Run is blocked.
    assert {event.type for event in after_block} <= {
        EventType.POLICY_DECISION,
        EventType.RUN_TRANSITIONED,
    }
    assert not any(event.type is EventType.TOOL_STARTED for event in events)
    assert not any(event.type is EventType.RUN_COMPLETED for event in events)
    assert verify_events(events).valid
