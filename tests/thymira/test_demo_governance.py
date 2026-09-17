"""The credit-risk governance demo path, end to end (QA-DEMO-GOV).

This is the reference demo the ``examples/credit-risk`` project documents, exercised through the
exact surface a human uses: the Typer CLI (``thymira run`` / ``thymira audit`` / ``thymira
approve`` / ``thymira status``), the ``ApiClient``, the FastAPI ``TestClient``, the runtime
composition graph (``build_runtime_graph``: THY -> MIRA -> ``Gate.review_findings`` -> terminal),
MIRA's real audit-agent runner driven by a ``ScriptedProvider`` (no model or network is ever
called), the real Policy Engine over the packaged base governance policy, the ``RunController`` and
the local JSON/JSONL persistence stack. The demo reconstructs its decision from the hash-chained
event log and asserts ``verify_events(...).valid``: the evidence, not internal state, is the proof.

Scope, stated so the demo is not mistaken for more than it is (QA-E2E-GOV seam: "the demo path is
the MVP path with more findings"):

* This reuses the QA-E2E-GOV harness shape deliberately rather than running the shipped seven-agent
  fan-out and the deterministic controls live. That live path cannot *pin* a governance outcome:
  the deterministic control A2 ("run closed") always fails HIGH over a Run audited in flight, so
  every composed Run would park on REQUIRE_HUMAN_REVIEW regardless of the credit-risk finding, and a
  deterministic leakage control could instead BLOCK. The four governance decisions are covered
  deterministically in ``test_e2e_governance.py``; here the demo pins the one credit-risk review
  outcome by giving MIRA a single controlled credit-risk finding, produced by the *real* audit-agent
  runner (``build_audit_agent_runner``) whose model is a ``ScriptedProvider``.
* Why THY is a double. A real ThyGraph would ask the shared Gate to authorise its ``plan.proposed``
  action, which the base policy escalates to REQUIRE_HUMAN_REVIEW -- a second, unrelated review that
  would obscure the single findings decision the demo pins. The THY double emits nothing and
  preserves the Run identity.
* Honesty. The demo produces evidence and a deterministic policy decision, then a *recorded* human
  approval -- never a certification. The assurance bundle it assembles carries the load-bearing
  disclaimer verbatim, and the documented outcome is pinned here: what the README states, this test
  asserts against the real CLI output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
from thymira.mira import AuditAgentSpec, build_assurance
from thymira.mira.checks import AuditContext, AuditReport, audit_run
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import (
    Actor,
    AuditFinding,
    EventType,
    Framework,
    ModelRoutePolicy,
    PolicyDecision,
    Run,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastapi.testclient import TestClient
    from langgraph.graph.state import CompiledStateGraph

    from thymira.mira.assurance import AssuranceBundle
    from thymira.schemas import Event
    from thymira.state import ArtifactStore

pytestmark = pytest.mark.integration

_runner = CliRunner()

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct audit-agent provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


# The example project the demo audits: its .thymira/config.yaml makes the composed Run genuinely the
# credit-risk project (domain credit_risk, frameworks EU_AI_ACT + CREDIT_RISK).
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "credit-risk"
_EXAMPLE_CONFIG = _EXAMPLE / ".thymira" / "config.yaml"
_EXAMPLE_README = _EXAMPLE / "README.md"

# The one credit-risk finding the demo scripts. A confident, high-severity CREDIT_RISK finding is
# REQUIRE_HUMAN_REVIEW under the base governance policy (GOV-103), the demo's intended outcome.
_CREDIT_RISK_FINDING: dict[str, object] = {
    "findings": [
        {
            "control_id": "CREDIT-SUBGROUP-DISPARITY",
            "title": "Approval rates diverge across a protected attribute",
            "finding": (
                "Selection rates differ materially across the groups of a protected attribute; "
                "a human must assess whether the disparity is justified before the model is used."
            ),
            "severity": "HIGH",
            "confidence": 0.95,
            "evidence": [{"kind": "event", "ref": "seq:1"}],
            "recommendation": "Produce a subgroup breakdown and review the disparity with a human.",
        }
    ]
}


class _ScriptedThy:
    """A THY double that advances the composition without exercising the plan gate."""

    name = "scripted-thy"

    def graph_version(self) -> str:
        """Return the stable demo graph version."""
        return "demo-thy-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Return the shared state unchanged; the composition drives the lifecycle."""
        del deps
        return state


class _CreditRiskMira:
    """A MIRA subgraph that runs the real audit-agent runner over one credit-risk finding.

    ``invoke`` emits ``audit.started``, runs one MIRA credit-risk audit agent through the unchanged
    :func:`build_audit_agent_runner` with a ``ScriptedProvider`` in place of a model, records one
    ``audit.finding`` per returned finding, and returns the state carrying those findings. It writes
    no ``policy.decision`` and no ``audit.completed`` because this test double leaves both
    run-level decisions to the composition; the production ``MiraSubgraph`` now obtains the same
    audit fact from MIRA's canonical flow before Core's ``review`` Gate.
    """

    name = "scripted-mira"

    def __init__(self, spec: AuditAgentSpec, response: dict[str, object]) -> None:
        self._spec = spec
        self._response = response

    def graph_version(self) -> str:
        """Return the stable demo graph version."""
        return "demo-mira-v1"

    def invoke(self, state: RuntimeState, *, deps: SubgraphDeps) -> RuntimeState:
        """Run the scripted credit-risk audit agent and expose its findings to the Gate."""
        deps.event_log.append(
            EventType.AUDIT_STARTED,
            Actor.system(),
            {"audited_at": utc_now().isoformat()},
            subject_id=state.run_id,
            producer="thymira.mira",
        )
        events = tuple(deps.event_log.events())
        report = AuditReport(
            run_id=state.run_id,
            status="passed",
            controls=(),
            terminal_hash=events[-1].hash,
            manifest_sha256=artifact_manifest_sha256(deps.artifact_store),
            policy_sha256=deps.gate.engine.policy_sha256,
        )
        runner = build_audit_agent_runner(
            deps.event_log,
            Actor.system(),
            provider=ScriptedProvider([self._response]),
            route_policy=TEST_ROUTE_POLICY,
        )
        findings = tuple(runner(self._spec, state.run_id, events, report).findings)
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
                "terminal_hash": deps.event_log.events()[-1].hash,
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


def _credit_risk_spec() -> AuditAgentSpec:
    """Build a one-turn, tool-free MIRA credit-risk audit-agent declaration."""
    return AuditAgentSpec(
        name="credit-risk-demo-agent",
        framework=Framework.CREDIT_RISK,
        task_kinds=("audit_judgement",),
        max_turns=1,
        system_prompt="Return the scripted credit-risk finding for the governance demo only.",
    )


@dataclass(frozen=True, slots=True)
class _Harness:
    """Own one isolated API and CLI client pair for the demo."""

    api: TestClient
    cli: ApiClient
    deps: RuntimeDeps


@pytest.fixture
def harness(tmp_path: Path) -> Generator[_Harness]:
    """Yield an in-process API whose composition audits the credit-risk project."""
    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    # Copy the real example project context so the Run is genuinely the credit-risk project.
    (workspace / ".thymira" / "config.yaml").write_text(
        _EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
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
            _CreditRiskMira(_credit_risk_spec(), _CREDIT_RISK_FINDING),
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
            request.method, target, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    cli = ApiClient("http://api.test", token=TEST_TOKEN, transport=httpx.MockTransport(transport))
    built = _Harness(api=api, cli=cli, deps=deps)
    yield built
    built.cli.close()
    built.api.close()


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


def _audit_text_from_cli(harness: _Harness, run_id: str) -> str:
    """Return the human-readable ``thymira audit`` output the README documents."""
    result = _runner.invoke(app, ["audit", run_id], obj=harness.cli)
    assert result.exit_code == 0, result.output
    return result.output


def _findings_decisions(events: list[Event]) -> list[str]:
    """Reconstruct, from the hash-chained log alone, every run-level findings decision in order."""
    return [
        str(event.payload.get("decision"))
        for event in events
        if event.type is EventType.POLICY_DECISION
        and event.payload.get("subject_kind") == "findings"
    ]


def _findings_decision(events: list[Event], run_id: str) -> PolicyDecision:
    """Return the newest run-level findings decision reconstructed from the log."""
    decision: PolicyDecision | None = None
    for event in events:
        if event.type is not EventType.POLICY_DECISION:
            continue
        candidate = PolicyDecision.model_validate(event.payload)
        if candidate.run_id == run_id and candidate.subject_kind == "findings":
            decision = candidate
    assert decision is not None, "the run recorded no findings decision"
    return decision


def _credit_risk_findings(events: list[Event]) -> list[AuditFinding]:
    """Return every recorded MIRA audit finding, reconstructed from the log."""
    return [
        AuditFinding.model_validate(event.payload["finding"])
        for event in events
        if event.type is EventType.AUDIT_FINDING
    ]


def _assurance_bundle(events: list[Event], store: ArtifactStore, run_id: str) -> AssuranceBundle:
    """Assemble the replayable assurance bundle from the run's own report and decision.

    The report is the deterministic audit MIRA recomputes over the final log (the same report the
    ``thymira audit`` surface computes), extended with MIRA's recorded credit-risk finding so the
    bundle traces the finding that drove the decision. ``build_assurance`` pins the terminal event
    hash and the policy content hash, and carries the no-certification disclaimer.
    """
    report = audit_run(AuditContext(run_id=run_id, events=events, store=store))
    full_report = report.model_copy(
        update={"findings": (*report.findings, *_credit_risk_findings(events))}
    )
    return build_assurance(
        run_id, full_report, _findings_decision(events, run_id), review_events=tuple(events)
    )


def test_credit_risk_demo_audit_review_approve_completes(harness: _Harness) -> None:
    """The scripted credit-risk demo runs end to end: audit -> review -> approve -> completed."""
    run_id = _run_from_cli(harness, "Assess whether the baseline credit model is fit for lending.")

    # Audit -> require review: MIRA raised a credit-risk finding and the Run parked for a human.
    assert _status_from_cli(harness, run_id) == "WAITING_FOR_APPROVAL"
    parked = harness.deps.run_store.events(run_id)
    assert _findings_decisions(parked) == ["REQUIRE_HUMAN_REVIEW"]
    credit_findings = _credit_risk_findings(parked)
    assert len(credit_findings) == 1
    assert credit_findings[0].framework is Framework.CREDIT_RISK
    assert credit_findings[0].control_id == "model:CREDIT-SUBGROUP-DISPARITY"
    assert any(event.type is EventType.HUMAN_APPROVAL_REQUESTED for event in parked)
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in parked)
    assert verify_events(parked).valid

    # The ``thymira audit`` surface shows the credit-risk review decision and the next step, and the
    # README documents exactly these lines.
    audit_text = _audit_text_from_cli(harness, run_id)
    assert "Decision: REQUIRE_HUMAN_REVIEW" in audit_text
    assert f"Next: thymira approve {run_id}" in audit_text
    readme = _EXAMPLE_README.read_text(encoding="utf-8")
    assert "Decision: REQUIRE_HUMAN_REVIEW" in readme
    assert "Next: thymira approve" in readme

    # Approve -> completed: a checkable human answer, recorded as evidence, unblocks the Run.
    approval = _runner.invoke(
        app,
        ["approve", run_id, "--note", "Subgroup disparity reviewed."],
        obj=harness.cli,
    )
    assert approval.exit_code == 0, approval.output
    assert "Run approved" in approval.output
    assert "Run approved" in readme

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
    assert approvals[0].actor.id == DEFAULT_ACTOR_ID
    assert approvals[0].actor.authenticated is True
    assert any(event.type is EventType.RUN_COMPLETED for event in events)
    assert verify_events(events).valid

    # The run completes with an assurance bundle: evidence and a deterministic decision, never a
    # certification.
    bundle = _assurance_bundle(events, harness.deps.artifact_store_factory(run_id), run_id)
    assert bundle.decision.decision.value == "REQUIRE_HUMAN_REVIEW"
    assert bundle.terminal_hash is not None
    assert bundle.terminal_hash == bundle.report.terminal_hash
    assert "never a legal, regulatory or professional certification" in bundle.disclaimer
    demo_finding = next(
        finding
        for finding in bundle.report.findings
        if finding.control_id == "model:CREDIT-SUBGROUP-DISPARITY"
    )
    assert demo_finding.framework is Framework.CREDIT_RISK
    markdown = bundle.to_markdown()
    assert "REQUIRE_HUMAN_REVIEW" in markdown
    assert "never a legal, regulatory or professional certification" in markdown
