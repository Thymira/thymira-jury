"""Unit tests for the shipped model-risk audit agent (AUD-RISK).

The agent is declared purely as an ``AuditAgentSpec`` YAML consumed by the MIRA-02 runner, so these
tests drive the packaged declaration through :func:`run_audit_agent` with a ``ScriptedProvider`` and
assert the behaviour the roadmap fixes: experiment evidence lacking subgroup metrics yields a
``MODEL_RISK`` "insufficient validation evidence" finding whose Evidence has ``kind="experiment"``
and a confidence in ``[0, 1]``, while a fully validated Run yields no model-risk finding.
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


def _risk_spec() -> AuditAgentSpec:
    """Return the shipped post-hoc risk reviewer.

    Selected by name, not by framework: a framework classifies an agent, it does not identify
    one. ``AUD-MODEL-RISK`` also declares MODEL_RISK — the roadmap gives both agents that
    framework on purpose, this one reviewing bias and model limitations while the other reviews
    model-risk management. ``load_specs`` is what guarantees the name is unique.
    """
    risk = [spec for spec in load_default_specs() if spec.name == "risk"]
    assert len(risk) == 1, "exactly one shipped audit agent named 'risk' is expected"
    return risk[0]


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


def _insufficient_validation_input(log: InMemoryEventLog) -> tuple[AuditInput, str]:
    """Record an experiment reporting only headline metrics and return the input and its id."""
    experiment_id = new_id("experiment")
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {
            "experiment_id": experiment_id,
            "metrics": {"roc_auc": 0.81, "accuracy": 0.78},
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput.from_run(tuple(log.events()), _report(log.run_id))
    return audit_input, experiment_id


def _validation_response(experiment_id: str) -> dict[str, object]:
    """Return one scripted HIGH model-risk finding grounded on the experiment evidence."""
    return {
        "findings": [
            {
                "control_id": "RISK-VALIDATION",
                "title": "Insufficient validation evidence",
                "finding": (
                    "The experiment reports only headline roc_auc and accuracy with no held-out "
                    "validation, baseline, calibration, or per-subgroup metrics, so the model's "
                    "fitness cannot be judged: the validation evidence is insufficient."
                ),
                "severity": "HIGH",
                "confidence": 0.84,
                "evidence": [{"kind": "experiment", "ref": experiment_id}],
                "recommendation": "Record held-out and per-subgroup metrics against a baseline.",
            }
        ]
    }


def test_load_default_specs_ships_the_risk_agent() -> None:
    spec = _risk_spec()

    assert spec.name == "risk"
    assert spec.task_kinds == ("audit_judgement",)
    assert spec.tool_allowlist == ("search_regulation",)
    assert spec.max_turns > 0
    assert spec.system_prompt.strip()


def test_risk_prompt_forbids_asserting_authorization() -> None:
    prompt = _risk_spec().system_prompt

    assert "evidence, not decisions" in prompt
    assert "Policy Engine" in prompt


def test_risk_agent_flags_insufficient_validation_evidence() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, experiment_id = _insufficient_validation_input(log)
    provider = ScriptedProvider([_validation_response(experiment_id)])

    output = run_audit_agent(_risk_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "risk"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.framework is Framework.MODEL_RISK
    assert "insufficient" in finding.finding.lower()
    assert finding.severity in {Severity.HIGH, Severity.CRITICAL}
    assert len(finding.evidence) >= 1
    assert finding.evidence[0].kind == "experiment"
    assert finding.evidence[0].ref == experiment_id
    assert 0.0 <= finding.confidence <= 1.0


def test_risk_agent_sees_the_experiment_evidence_it_must_ground_on() -> None:
    log = InMemoryEventLog(new_id("run"))
    audit_input, experiment_id = _insufficient_validation_input(log)
    provider = ScriptedProvider([_validation_response(experiment_id)])

    run_audit_agent(_risk_spec(), audit_input, _context(log, provider=provider))

    assert experiment_id in provider.calls[0]["prompt"]


def test_fully_validated_run_yields_no_model_risk_finding() -> None:
    log = InMemoryEventLog(new_id("run"))
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        Actor.system(),
        {
            "experiment_id": new_id("experiment"),
            "metrics": {
                "roc_auc": 0.81,
                "baseline_roc_auc": 0.62,
                "roc_auc_by_group": {"female": 0.80, "male": 0.81},
                "calibration_brier": 0.14,
            },
        },
        surface=EventSurface.MODEL_VISIBLE,
    )
    audit_input = AuditInput.from_run(tuple(log.events()), _report(log.run_id))
    provider = ScriptedProvider([{"findings": []}])

    output = run_audit_agent(_risk_spec(), audit_input, _context(log, provider=provider))

    assert output.agent_name == "risk"
    assert output.findings == ()
