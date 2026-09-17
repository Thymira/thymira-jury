"""Requirements-coverage control A26 (CTRL-COVERAGE).

A26 reads the real requirements->controls mapping (``docs/governance/requirements_controls.json``,
the KB-02 schema) and fails when a declared framework's mapped requirement has a control that
produced no evidence in the run. The mapping is loaded from the repository and injected through the
``AuditContext`` so the assertions are deterministic against the shipped mapping rather than a
hardcoded list.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run, coverage
from thymira.mira.kb.ingest import load_requirements
from thymira.policies import Gate, PolicyEngine, auto_approve, load_policy_stack
from thymira.schemas import Actor, ArtifactKind, EventType, Framework, new_id

from thymira.state import LocalArtifactStore  # isort: skip

if TYPE_CHECKING:
    import pytest

    from thymira.mira.checks import AuditReport
    from thymira.mira.kb import RequirementsControls

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAPPING_PATH = _REPO_ROOT / "docs" / "governance" / "requirements_controls.json"


def _mapping() -> RequirementsControls:
    return load_requirements(_MAPPING_PATH)


def _control(report: AuditReport, control_id: str):
    return next(c for c in report.controls if c.control_id == control_id)


def _findings(report: AuditReport, control_id: str):
    return [f for f in report.findings if f.control_id == control_id]


def _covered_run(tmp_path: Path) -> tuple[InMemoryEventLog, LocalArtifactStore]:
    """A run whose evidence satisfies every deterministic control the EU AI Act requirements map to.

    Art. 11 -> A5/A10 (artifact integrity), art. 12 -> A1/A2 (chain + closed run), art. 14 -> A3/A7
    (authorization + resolved review). ``run_python`` defaults to REQUIRE_HUMAN_REVIEW under the
    credit-risk stack; ``auto_approve`` records the request and the answer, so A3 and A7 both hold.
    """
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=auto_approve)
    system = Actor.system()
    tool_id = new_id("tool")
    log.append(
        EventType.RUN_STARTED, system, {"prompt": "x", "run_environment": {"python": "3.13"}}
    )
    gate.check_action(subject_kind="tool_call", subject_id=tool_id, action_type="run_python")
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    artifact = store.save_json(
        "metrics.json", {"auc": 0.8}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": artifact.name, "sha256": artifact.sha256},
        subject_id=artifact.id,
    )
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=tool_id)
    log.append(EventType.RUN_COMPLETED, system, {"status": "COMPLETED"})
    return log, store


def _run_missing_logging_evidence(tmp_path: Path) -> tuple[InMemoryEventLog, LocalArtifactStore]:
    """The covered run with no run lifecycle: A2 has no evidence, so art. 12 is uncovered."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    gate = Gate(engine, log, approver=auto_approve)
    system = Actor.system()
    tool_id = new_id("tool")
    gate.check_action(subject_kind="tool_call", subject_id=tool_id, action_type="run_python")
    log.append(EventType.TOOL_STARTED, system, {"tool": "run_python"}, subject_id=tool_id)
    artifact = store.save_json(
        "metrics.json", {"auc": 0.8}, produced_by=tool_id, kind=ArtifactKind.METRICS
    )
    log.append(
        EventType.ARTIFACT_CREATED,
        system,
        {"name": artifact.name, "sha256": artifact.sha256},
        subject_id=artifact.id,
    )
    log.append(EventType.TOOL_COMPLETED, system, {"exit_code": 0}, subject_id=tool_id)
    return log, store


def test_a26_fails_naming_the_uncovered_art12_requirement(tmp_path: Path) -> None:
    log, store = _run_missing_logging_evidence(tmp_path)

    report = audit_run(
        AuditContext(
            log.run_id,
            log.events(),
            store,
            frameworks=(Framework.EU_AI_ACT,),
            requirements_controls=_mapping(),
        )
    )

    a26 = _control(report, "A26")
    assert a26.status is ControlStatus.FAILED
    assert "EU-AI-ACT-ART-12" in a26.detail  # the run-closed logging requirement is uncovered
    assert "A2" in a26.detail
    # the artifact-documentation and human-oversight requirements remain covered.
    assert "EU-AI-ACT-ART-11" not in a26.detail
    assert "EU-AI-ACT-ART-14" not in a26.detail
    assert "A26" in {f.control_id for f in report.findings}


def test_a26_passes_a_fully_covered_eu_ai_act_run(tmp_path: Path) -> None:
    log, store = _covered_run(tmp_path)

    report = audit_run(
        AuditContext(
            log.run_id,
            log.events(),
            store,
            frameworks=(Framework.EU_AI_ACT,),
            requirements_controls=_mapping(),
        )
    )

    assert _control(report, "A26").status is ControlStatus.PASSED
    assert _findings(report, "A26") == []


def test_a26_loads_the_default_mapping_when_none_is_injected(tmp_path: Path) -> None:
    # With no mapping supplied the control finds docs/governance/requirements_controls.json itself.
    log, store = _covered_run(tmp_path)

    report = audit_run(
        AuditContext(log.run_id, log.events(), store, frameworks=(Framework.EU_AI_ACT,))
    )

    assert _control(report, "A26").status is ControlStatus.PASSED


def test_a26_is_not_applicable_when_no_frameworks_are_declared(tmp_path: Path) -> None:
    log, store = _covered_run(tmp_path)

    report = audit_run(AuditContext(log.run_id, log.events(), store))

    assert _control(report, "A26").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A26") == []


# ------------------------------------- A26 when the mapping itself cannot be resolved


def test_a26_is_not_applicable_when_no_mapping_can_be_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the requirements mapping there is nothing to assert, so A26 asserts nothing."""
    monkeypatch.setattr(coverage, "_default_mapping_path", lambda: None)
    log, store = _covered_run(tmp_path)

    report = audit_run(
        AuditContext(log.run_id, log.events(), store, frameworks=(Framework.EU_AI_ACT,))
    )

    a26 = _control(report, "A26")
    assert a26.status is ControlStatus.NOT_APPLICABLE
    assert "no requirements-controls mapping available" in a26.detail
    assert _findings(report, "A26") == []


def test_loading_the_default_mapping_returns_nothing_when_it_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed mapping is not evidence: the loader fails closed to ``None``."""
    broken = tmp_path / "requirements_controls.json"
    broken.write_text("{", encoding="utf-8", newline="\n")
    monkeypatch.setattr(coverage, "_default_mapping_path", lambda: broken)

    assert coverage.load_default_requirements_controls() is None


def test_loading_the_default_mapping_returns_nothing_when_it_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository layout without the mapping is reported as absent, never guessed."""
    monkeypatch.setattr(coverage, "_default_mapping_path", lambda: None)

    assert coverage.load_default_requirements_controls() is None


def test_a26_skips_control_ids_that_are_not_deterministic_controls(tmp_path: Path) -> None:
    """A mapped control MIRA does not evaluate here (a pack control) is not assessable."""
    log, store = _covered_run(tmp_path)
    ctx = AuditContext(log.run_id, log.events(), store, frameworks=(Framework.EU_AI_ACT,))

    status, detail = coverage.check_requirements_coverage(ctx, {}, mapping=_mapping())

    assert status is ControlStatus.NOT_APPLICABLE
    assert "no deterministic control covers the declared frameworks" in detail
