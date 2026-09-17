"""The reference project under examples/ stays loadable by the runtime it documents."""

from __future__ import annotations

from pathlib import Path

import yaml

from thymira.policies import PolicyEngine, load_policy, load_policy_stack
from thymira.schemas import (
    AuditFinding,
    Decision,
    Evidence,
    Framework,
    ProjectConfig,
    Severity,
    new_id,
)

PROJECT = Path(__file__).resolve().parents[2] / "examples" / "credit-risk" / ".thymira"


def _finding(severity: Severity, *, confidence: float, with_evidence: bool) -> AuditFinding:
    return AuditFinding(
        id=new_id("finding"),
        run_id=new_id("run"),
        control_id="A3",
        framework=Framework.CREDIT_RISK,
        title="example",
        finding="example finding",
        severity=severity,
        confidence=confidence,
        evidence=(Evidence(kind="event", ref="seq:1"),) if with_evidence else (),
        recommendation="look",
    )


def test_project_config_is_a_valid_project_config() -> None:
    data = yaml.safe_load((PROJECT / "config.yaml").read_text(encoding="utf-8"))
    config = ProjectConfig.model_validate(data)
    assert config.project.domain == "credit_risk"
    assert Framework.CREDIT_RISK in config.governance.frameworks
    assert [(dataset.name, dataset.path, dataset.target) for dataset in config.datasets] == [
        ("german_credit", "data/applications.csv", "is_high_risk")
    ]


def test_project_policy_loads_and_layers_on_the_defaults() -> None:
    project = load_policy(PROJECT / "policies.yaml")
    assert len(project.finding_rules) == 3
    engine = PolicyEngine(load_policy_stack("credit_risk").merged_with(project))

    run_id = new_id("run")
    no_evidence = engine.decide_findings(
        run_id=run_id, findings=[_finding(Severity.HIGH, confidence=0.5, with_evidence=False)]
    )
    assert no_evidence.decision is Decision.REQUIRE_HUMAN_REVIEW

    low = engine.decide_findings(
        run_id=run_id, findings=[_finding(Severity.LOW, confidence=0.5, with_evidence=True)]
    )
    assert low.decision in {Decision.PASS, Decision.WARNING}


def test_project_policy_lets_thys_own_plan_proceed() -> None:
    """CRX-001 overrides the base stack's ``plan.proposed`` -> REQUIRE_HUMAN_REVIEW default.

    Without the overlay, a real (non-scripted) THY halts every run before touching the dataset:
    the MVP's synchronous InlineDispatcher has no mid-execution pause/resume for THY's own plan
    gate (unlike MIRA's post-hoc findings review, which the two assertions below leave untouched).
    """
    project = load_policy(PROJECT / "policies.yaml")
    base_engine = PolicyEngine(load_policy_stack("credit_risk"))
    project_engine = PolicyEngine(load_policy_stack("credit_risk").merged_with(project))
    run_id = new_id("run")

    unrouted = base_engine.decide_action(
        run_id=run_id, subject_kind="run", subject_id=run_id, action_type="plan.proposed"
    )
    assert unrouted.decision is Decision.REQUIRE_HUMAN_REVIEW

    routed = project_engine.decide_action(
        run_id=run_id, subject_kind="run", subject_id=run_id, action_type="plan.proposed"
    )
    assert routed.decision is Decision.PASS
    assert routed.rule_id == "CRX-001"
