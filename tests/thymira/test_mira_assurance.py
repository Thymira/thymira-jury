"""Unit tests for the MIRA assurance bundle (ASSUR-01).

The bundle assembles a report, a policy decision, a deduplicated evidence index and a
load-bearing disclaimer, and pins the terminal event hash and the policy content hash for replay.
"""

from __future__ import annotations

import pytest

from thymira.mira import ASSURANCE_DISCLAIMER, AssuranceBundle, build_assurance
from thymira.mira.checks import AuditReport, AuditStatus, ControlResult, ControlStatus
from thymira.schemas import (
    AuditFinding,
    Decision,
    Evidence,
    Framework,
    PolicyDecision,
    Severity,
    new_id,
)

_TERMINAL_HASH = "c" * 64
_POLICY_SHA = "d" * 64


def _finding(run_id: str, control_id: str, evidence: tuple[Evidence, ...]) -> AuditFinding:
    return AuditFinding(
        id=new_id("finding"),
        run_id=run_id,
        control_id=control_id,
        framework=Framework.INTERNAL,
        title=f"Finding for {control_id}",
        finding="observed",
        severity=Severity.MEDIUM,
        confidence=1.0,
        evidence=evidence,
    )


def _control(control_id: str, title: str) -> ControlResult:
    return ControlResult(
        control_id=control_id,
        title=title,
        status=ControlStatus.PASSED,
        severity=Severity.LOW,
        detail="checked",
    )


def _report(
    run_id: str,
    *,
    controls: tuple[ControlResult, ...],
    findings: tuple[AuditFinding, ...] = (),
    status: AuditStatus = "passed",
    terminal_hash: str | None = _TERMINAL_HASH,
) -> AuditReport:
    return AuditReport(
        run_id=run_id,
        status=status,
        controls=controls,
        findings=findings,
        terminal_hash=terminal_hash,
    )


def _decision(run_id: str, *, policy_sha256: str = _POLICY_SHA) -> PolicyDecision:
    return PolicyDecision(
        id=new_id("decision"),
        run_id=run_id,
        subject_kind="findings",
        subject_id="findings",
        decision=Decision.WARNING,
        rule_id="default",
        reason="one warning finding",
        policy_name="credit-risk@1.0",
        policy_sha256=policy_sha256,
    )


def test_build_assurance_deduplicates_finding_evidence() -> None:
    run_id = new_id("run")
    shared = Evidence(kind="event", ref="seq:7", sha256="a" * 64)
    first = Evidence(kind="artifact", ref="art_1", sha256="b" * 64)
    third = Evidence(kind="experiment", ref="exp_1")
    findings = (
        _finding(run_id, "CHAIN-001", (first, shared)),
        _finding(run_id, "LEAKAGE-002", (shared, third)),
    )
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),), findings=findings)

    bundle = build_assurance(run_id, report, _decision(run_id))

    # Each distinct reference appears once, in first-seen order across the findings.
    assert bundle.evidence_index == (first, shared, third)


def test_build_assurance_carries_the_disclaimer() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))

    bundle = build_assurance(run_id, report, _decision(run_id))

    assert bundle.disclaimer == ASSURANCE_DISCLAIMER
    assert "does not assert that a human, legal or business review took place" in bundle.disclaimer
    assert "never a legal, regulatory or professional certification" in bundle.disclaimer
    assert bundle.disclaimer in bundle.to_markdown()


def test_build_assurance_policy_sha256_equals_the_decision() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))
    decision = _decision(run_id)

    bundle = build_assurance(run_id, report, decision)

    assert bundle.policy_sha256 == decision.policy_sha256


def test_build_assurance_pins_the_terminal_hash_for_replay() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))

    bundle = build_assurance(run_id, report, _decision(run_id))

    assert bundle.terminal_hash == report.terminal_hash == _TERMINAL_HASH


def test_to_markdown_renders_every_control_row() -> None:
    run_id = new_id("run")
    controls = (
        _control("CHAIN-001", "Chain intact"),
        _control("PROV-002", "Provenance captured"),
        _control("LEAKAGE-003", "No leakage"),
    )
    report = _report(run_id, controls=controls)

    markdown = build_assurance(run_id, report, _decision(run_id)).to_markdown()

    for control in controls:
        assert control.control_id in markdown
        assert control.title in markdown


def test_to_markdown_lists_the_evidence_index_and_policy_hash() -> None:
    run_id = new_id("run")
    evidence = Evidence(kind="event", ref="seq:7", sha256="a" * 64)
    findings = (_finding(run_id, "CHAIN-001", (evidence,)),)
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),), findings=findings)

    markdown = build_assurance(run_id, report, _decision(run_id)).to_markdown()

    assert "seq:7" in markdown
    assert _POLICY_SHA in markdown


def test_to_markdown_reports_when_no_evidence_is_referenced() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))

    markdown = build_assurance(run_id, report, _decision(run_id)).to_markdown()

    assert "No evidence is referenced by the findings." in markdown


def test_assurance_bundle_cannot_drop_the_disclaimer() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))
    with pytest.raises(ValueError, match="no-human-review clause"):
        AssuranceBundle(
            run_id=run_id,
            report=report,
            decision=_decision(run_id),
            terminal_hash=_TERMINAL_HASH,
            disclaimer="controls verified.",
        )


def test_build_assurance_requires_a_terminal_hash() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),), terminal_hash=None)
    with pytest.raises(ValueError, match="terminal event hash"):
        build_assurance(run_id, report, _decision(run_id))


def test_assurance_bundle_rejects_a_terminal_hash_that_lies() -> None:
    run_id = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))
    with pytest.raises(ValueError, match="terminal_hash"):
        AssuranceBundle(
            run_id=run_id,
            report=report,
            decision=_decision(run_id),
            terminal_hash="e" * 64,
        )


def test_assurance_bundle_rejects_a_run_id_mismatch() -> None:
    run_id = new_id("run")
    other = new_id("run")
    report = _report(run_id, controls=(_control("CHAIN-001", "Chain intact"),))
    with pytest.raises(ValueError, match="run_id"):
        build_assurance(other, report, _decision(other))
