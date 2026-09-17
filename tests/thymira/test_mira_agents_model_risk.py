"""Unit tests for the shipped Model Risk audit agent (AUD-MODEL-RISK).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent` with a ``ScriptedProvider`` and
assert the behaviour the roadmap fixes: a Run whose best model has no trivial-baseline comparison in
evidence yields a ``MODEL_RISK`` "unbenchmarked model" finding with Evidence ``kind="experiment"``,
and a benchmarked Run yields none.

The shipped roster now carries two ``MODEL_RISK`` agents -- the MVP ``risk`` reviewer and this
model-risk-management ``model_risk`` reviewer -- so this test selects the agent by name.
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


def _model_risk_spec() -> AuditAgentSpec:
    """Return the one shipped audit agent named ``model_risk``."""
    specs = [spec for spec in load_default_specs() if spec.name == "model_risk"]
    assert len(specs) == 1, "exactly one shipped model_risk audit agent is expected"
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


def _unbenchmarked_input(log: InMemoryEventLog) -> tuple[AuditInput, str]:
    """Record an experiment whose best model has no baseline comparison, and return its id."""
    experiment_id = new_id("experiment")
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {
            "experiment_id": experiment_id,
            "metrics": {"roc_auc": 0.83, "accuracy": 0.79},
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput.from_run(tuple(log.events()), _report(log.run_id))
    return audit_input, experiment_id


def _unbenchmarked_finding(experiment_id: str) -> dict[str, object]:
    """Return one scripted MODEL_RISK 'unbenchmarked model' finding grounded on the experiment."""
    return {
        "findings": [
            {
                "control_id": "MODEL-RISK-BENCHMARK",
                "title": "Unbenchmarked model: no trivial-baseline comparison",
                "finding": (
                    "The best model reports roc_auc and accuracy with no trivial-baseline "
                    "comparison in the experiment evidence, so its metrics cannot be interpreted: "
                    "the model is unbenchmarked."
                ),
                "severity": "HIGH",
                "confidence": 0.82,
                "evidence": [{"kind": "experiment", "ref": experiment_id}],
                "recommendation": "Train a trivial baseline on the same folds and compare.",
            }
        ]
    }


def test_load_default_specs_ships_the_model_risk_agent() -> None:
    spec = _model_risk_spec()

    assert spec.framework is Framework.MODEL_RISK
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.tool_allowlist == ("search_regulation",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_model_risk_prompt_forbids_asserting_authorization() -> None:
    prompt = _model_risk_spec().system_prompt

    assert "evidence, not decisions" in prompt
    assert "Policy Engine" in prompt


def test_model_risk_flags_an_unbenchmarked_model() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, experiment_id = _unbenchmarked_input(log)
    provider = ScriptedProvider([_unbenchmarked_finding(experiment_id)])

    output = run_audit_agent(_model_risk_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "model_risk"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.MODEL_RISK
    assert "unbenchmarked" in finding.finding.lower()
    assert len(finding.evidence) >= 1
    assert finding.evidence[0].kind == "experiment"
    assert finding.evidence[0].ref == experiment_id
    assert 0.0 <= finding.confidence <= 1.0


def test_model_risk_sees_the_experiment_evidence_it_must_ground_on() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, experiment_id = _unbenchmarked_input(log)
    provider = ScriptedProvider([_unbenchmarked_finding(experiment_id)])

    run_audit_agent(_model_risk_spec(), audit_input, _context(log, provider=provider))

    assert experiment_id in provider.calls[0]["prompt"]


def test_benchmarked_run_yields_no_model_risk_finding() -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {
            "experiment_id": new_id("experiment"),
            "metrics": {
                "roc_auc": 0.83,
                "baseline_roc_auc": 0.61,
                "calibration_brier": 0.13,
            },
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput.from_run(tuple(log.events()), _report(log.run_id))
    provider = ScriptedProvider([{"findings": []}])

    output = run_audit_agent(_model_risk_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "model_risk"
    assert output.findings == ()
