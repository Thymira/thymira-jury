"""Unit tests for the shipped Compliance audit agent (AUD-COMPLIANCE).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent` with a ``ScriptedProvider``.
The scenario the roadmap fixes: for a project declaring EU_AI_ACT, a Run lacking the Article 12
event-logging evidence yields a compliance finding naming the requirement (REQ) and the mapped
control_id, and a fully covered Run yields none. The compliance agent narrates requirements
coverage; it complements, never repeats, the deterministic requirements-coverage control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.agents import ScriptedProvider
from thymira.events import InMemoryEventLog
from thymira.mira.agents.loader import load_default_specs
from thymira.mira.agents.runner import AuditAgentContext, run_audit_agent
from thymira.mira.audit_io import AuditInput
from thymira.mira.checks import AuditReport
from thymira.schemas import Actor, EventSurface, EventType, Framework, ModelRoutePolicy, new_id

if TYPE_CHECKING:
    from thymira.mira.agents.spec import AuditAgentSpec

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AuditAgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


# The Article 12 record-keeping requirement and the deterministic controls the KB-02 mapping
# (docs/governance/requirements_controls.json) assigns to it. The finding must name both.
_ART12_REQUIREMENT = "EU-AI-ACT-ART-12"
_ART12_CONTROLS = {"A1", "A2"}


def _compliance_spec() -> AuditAgentSpec:
    """Return the one shipped audit agent named ``compliance``."""
    specs = [spec for spec in load_default_specs() if spec.name == "compliance"]
    assert len(specs) == 1, "exactly one shipped compliance audit agent is expected"
    return specs[0]


def _report(run_id: str) -> AuditReport:
    """Return a minimal deterministic report with no findings for one Run."""
    return AuditReport(run_id=run_id, status="passed", controls=(), findings=())


def _context(log: InMemoryEventLog, *, provider: ScriptedProvider) -> AuditAgentContext:
    """Build one run-only execution context with fresh MIRA identities."""
    return AuditAgentContext(
        route_policy=TEST_ROUTE_POLICY,
        event_log=log,
        actor=Actor.system(),
        agent_id=new_id("agent"),
        task_id=new_id("task"),
        provider=provider,
    )


def _eu_ai_act_input(log: InMemoryEventLog) -> tuple[AuditInput, int]:
    """Record an EU_AI_ACT Run whose event-logging evidence is incomplete."""
    event = log.append(
        EventType.RUN_STARTED,
        Actor.system(),
        {"text": "Run started for automated credit scoring under EU AI Act obligations."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id,
        events=tuple(log.events()),
        report=_report(log.run_id),
        frameworks=(Framework.EU_AI_ACT,),
    )
    return audit_input, event.seq


def _uncovered_finding(seq: int) -> dict[str, object]:
    """Return one scripted compliance finding naming the REQ and the mapped control_id."""
    return {
        "findings": [
            {
                "control_id": "A1",
                "title": f"Requirement {_ART12_REQUIREMENT} is uncovered",
                "finding": (
                    f"The Run declares EU_AI_ACT but produced no event-logging evidence for "
                    f"requirement {_ART12_REQUIREMENT}: mapped control A1 folded no verified, "
                    f"closed event chain (event seq:{seq})."
                ),
                "severity": "HIGH",
                "confidence": 0.86,
                "evidence": [{"kind": "event", "ref": f"seq:{seq}"}],
                "recommendation": (
                    "Record and verify the append-only event chain and close the Run so control "
                    "A1 can fold the Article 12 evidence."
                ),
            }
        ]
    }


def test_load_default_specs_ships_the_compliance_agent() -> None:
    spec = _compliance_spec()

    assert spec.framework is Framework.INTERNAL
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_compliance_prompt_forbids_asserting_authorization() -> None:
    prompt = _compliance_spec().system_prompt

    assert "evidence, not decisions" in prompt
    assert "never an authorization" in prompt
    assert "Policy Engine" in prompt


def test_compliance_flags_an_uncovered_eu_ai_act_requirement() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _eu_ai_act_input(log)
    provider = ScriptedProvider([_uncovered_finding(seq)])

    output = run_audit_agent(_compliance_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "compliance"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.INTERNAL
    assert finding.control_id in {f"model:{control_id}" for control_id in _ART12_CONTROLS}
    assert _ART12_REQUIREMENT in f"{finding.title} {finding.finding}"
    assert len(finding.evidence) >= 1
    assert 0.0 <= finding.confidence <= 1.0


def test_compliance_sees_the_declared_framework_it_must_review() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _eu_ai_act_input(log)
    provider = ScriptedProvider([_uncovered_finding(seq)])

    run_audit_agent(_compliance_spec(), audit_input, _context(log, provider=provider))

    assert "EU_AI_ACT" in provider.calls[0]["prompt"]


def test_compliance_fully_covered_run_yields_no_finding() -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.RUN_COMPLETED,
        Actor.system(),
        {"text": "Run completed; the verified event chain covers Article 12 record-keeping."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id,
        events=tuple(log.events()),
        report=_report(log.run_id),
        frameworks=(Framework.EU_AI_ACT,),
    )
    provider = ScriptedProvider([{"findings": []}])

    output = run_audit_agent(_compliance_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "compliance"
    assert output.findings == ()
