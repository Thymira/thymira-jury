"""Unit tests for the shipped Methodology audit agent (AUD-METHODOLOGY).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent` with a ``ScriptedProvider`` and
assert the two behaviours the roadmap fixes: a target-correlated feature yields a ``METHODOLOGY``
finding of severity >= HIGH carrying at least one Evidence ref, and a clean Run yields none.
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
from thymira.schemas import (
    Actor,
    EventSurface,
    EventType,
    Framework,
    ModelRoutePolicy,
    Severity,
    new_id,
)

if TYPE_CHECKING:
    from thymira.mira.agents.spec import AuditAgentSpec


TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AuditAgentContext provider seams to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _methodology_spec() -> AuditAgentSpec:
    """Return the one shipped audit agent declared for the METHODOLOGY framework."""
    methodology = [spec for spec in load_default_specs() if spec.framework is Framework.METHODOLOGY]
    assert len(methodology) == 1, "exactly one shipped METHODOLOGY audit agent is expected"
    return methodology[0]


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


def _leakage_input(log: InMemoryEventLog) -> tuple[AuditInput, int]:
    """Record a model-visible event exposing a target-correlated feature and return the input."""
    event = log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "Feature 'is_default_flag' correlates 0.98 with the target 'default'."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    return audit_input, event.seq


def _leakage_response(seq: int) -> dict[str, object]:
    """Return one scripted HIGH methodology finding grounded on the leakage event."""
    return {
        "findings": [
            {
                "control_id": "METHOD-LEAKAGE",
                "title": "Target leakage: feature nearly identical to the label",
                "finding": (
                    "Feature 'is_default_flag' correlates 0.98 with the target, above the 0.95 "
                    "leakage threshold."
                ),
                "severity": "HIGH",
                "confidence": 0.92,
                "evidence": [{"kind": "event", "ref": f"seq:{seq}"}],
                "recommendation": "Drop the leaking feature and re-train before evaluation.",
            }
        ]
    }


def test_load_default_specs_ships_the_methodology_agent() -> None:
    spec = _methodology_spec()

    assert spec.name == "methodology"
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.tool_allowlist == ("search_regulation",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_methodology_prompt_forbids_asserting_authorization() -> None:
    prompt = _methodology_spec().system_prompt

    assert "evidence, not decisions" in prompt
    assert "Policy Engine" in prompt


def test_methodology_agent_flags_a_target_correlated_feature() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _leakage_input(log)
    provider = ScriptedProvider([_leakage_response(seq)])

    output = run_audit_agent(_methodology_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "methodology"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.METHODOLOGY
    assert finding.severity in {Severity.HIGH, Severity.CRITICAL}
    assert len(finding.evidence) >= 1
    assert finding.evidence[0].ref == f"seq:{seq}"
    assert 0.0 <= finding.confidence <= 1.0


def test_methodology_agent_sees_the_leakage_evidence_it_must_ground_on() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, seq = _leakage_input(log)
    provider = ScriptedProvider([_leakage_response(seq)])

    run_audit_agent(_methodology_spec(), audit_input, _context(log, provider=provider))

    assert "is_default_flag" in provider.calls[0]["prompt"]


def test_clean_run_yields_no_methodology_finding() -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"text": "Stratified split, fold-local preprocessing, and a trivial baseline recorded."},
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput(
        run_id=log.run_id, events=tuple(log.events()), report=_report(log.run_id)
    )
    provider = ScriptedProvider([{"findings": []}])

    output = run_audit_agent(_methodology_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "methodology"
    assert output.findings == ()
