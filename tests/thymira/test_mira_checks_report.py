"""Audit control A18: report facts match the source artifacts (`CTRL-REPORT`).

A18 reads the analytical report artifact and checks that every quantitative claim resolves to a
value the run recorded and that every cited artifact is present in the manifest; it is
NOT_APPLICABLE when the run produced no report artifact.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, cast

import pytest
from pypdf import PdfWriter

from tests.thymira.fixtures_tools import development_policy
from thymira.events import PDF_EXPORT_EXECUTION_VERSION, InMemoryEventLog
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import Gate, PolicyEngine, RiskProfile
from thymira.schemas import (
    Actor,
    ArtifactKind,
    EventType,
    Experiment,
    ExperimentStatus,
    new_id,
)

from thymira.state import LocalArtifactStore  # isort: skip
from thymira.tools import Tool, ToolContext, ToolInvocation, ToolManager, ToolRegistry
from thymira.tools.builtins.export_pdf import ExportPdf

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.mira.checks import AuditReport

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)


def _run(
    tmp_path: Path, *, metrics: dict[str, float], report_text: str | None
) -> tuple[str, InMemoryEventLog, LocalArtifactStore]:
    """A closed run with a recorded metrics artifact/experiment and an optional report."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {"run_environment": {"python": "3.13"}})
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_json(
        "metrics/baseline.json", metrics, produced_by=new_id("tool"), kind=ArtifactKind.METRICS
    )
    experiment = Experiment(
        id=new_id("experiment"),
        run_id=run_id,
        name="baseline",
        status=ExperimentStatus.COMPLETED,
        metrics=metrics,
    )
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        system,
        {"experiment": experiment.model_dump(mode="json")},
    )
    if report_text is not None:
        store.save_text(
            "report.md", report_text, produced_by=new_id("tool"), kind=ArtifactKind.REPORT
        )
    log.append(EventType.RUN_COMPLETED, system, {})
    return run_id, log, store


def _audit(run_id: str, log: InMemoryEventLog, store: LocalArtifactStore) -> AuditReport:
    return audit_run(AuditContext(run_id, log.events(), store))


def _control(report: AuditReport, control_id: str):
    return next(c for c in report.controls if c.control_id == control_id)


def _findings(report: AuditReport, control_id: str):
    return [f for f in report.findings if f.control_id == control_id]


def _blank_pdf() -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def _record_export_pdf_completion(
    log: InMemoryEventLog,
    *,
    source_id: str,
    source_name: str,
    pdf_id: str,
    pdf_name: str,
    pdf_size: int,
    input_source_name: str = "report.md",
) -> None:
    """Record the manager-owned success envelope that binds one exported PDF pair."""
    tool_call_id = new_id("tool")
    log.append(
        EventType.TOOL_COMPLETED,
        Actor(kind="tool", id="export_pdf", authenticated=True),
        {
            "tool_call_id": tool_call_id,
            "task_id": new_id("task"),
            "tool": "export_pdf",
            "status": "COMPLETED",
            "exit_code": None,
            "artifact_ids": [source_id, pdf_id],
            "error": None,
            "value": {
                "kind": "success",
                "value": {
                    "text": "exported",
                    "path": pdf_name,
                    "source_path": source_name,
                    "input_source_path": input_source_name,
                    "format_version": PDF_EXPORT_EXECUTION_VERSION,
                    "bytes_written": pdf_size,
                    "page_count": 1,
                    "artifact_id": pdf_id,
                },
            },
        },
        subject_id=tool_call_id,
    )


def _execute_export_pdf_with_completion(
    log: InMemoryEventLog,
    invocation: ToolInvocation,
    arguments: dict[str, object],
) -> None:
    """Execute the producer and record the Tool Manager envelope A18 independently verifies."""
    result = ExportPdf().execute(invocation, arguments)
    assert result.success
    assert len(result.artifact_ids) == 2
    source_id, pdf_id = result.artifact_ids
    active = {artifact.id: artifact for artifact in invocation.artifact_store.list_active()}
    source = active[source_id]
    pdf = active[pdf_id]
    _record_export_pdf_completion(
        log,
        source_id=source.id,
        source_name=source.name,
        pdf_id=pdf.id,
        pdf_name=pdf.name,
        pdf_size=pdf.size_bytes,
        input_source_name=str(arguments["source_path"]),
    )


def test_a18_passes_when_every_quoted_figure_resolves(tmp_path: Path) -> None:
    text = (
        "# Report\n\n"
        "The baseline model reached an accuracy of 0.83 on the held-out test set.\n"
        "Full metrics are in metrics/baseline.json.\n"
    )
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=text)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_resolves_signed_percentage_points_and_rounded_negative_values(
    tmp_path: Path,
) -> None:
    text = (
        "# Report\n\n"
        "The strongest effects were +19.21pp and -18.33pp, while correlation was -0.039.\n"
        "Full metrics are in metrics/baseline.json.\n"
    )
    run_id, log, store = _run(
        tmp_path,
        metrics={"positive_effect": 0.1921, "negative_effect": -0.1833, "correlation": -0.0392},
        report_text=text,
    )

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_ignores_markdown_section_numbers_and_cross_references(tmp_path: Path) -> None:
    text = (
        "# Report\n\n"
        "### 3.2 Continuous features\n\n"
        "Section 3.2 explains the accuracy of 0.83.\n"
        "Full metrics are in metrics/baseline.json.\n"
    )
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=text)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_accepts_percent_native_metrics(tmp_path: Path) -> None:
    text = "# Report\n\nThe segment represents 27.45% of the sample.\n"
    run_id, log, store = _run(tmp_path, metrics={"support_pct": 27.45}, report_text=text)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_resolves_a_claim_at_its_decimal_half_step(tmp_path: Path) -> None:
    text = "# Report\n\nThe rounded correlation is 0.218.\n"
    run_id, log, store = _run(tmp_path, metrics={"correlation": 0.2175}, report_text=text)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED


def test_a18_resolves_grouped_decimals_and_scientific_notation(tmp_path: Path) -> None:
    text = (
        "# Report\n\n"
        "The upper fence is 7,879.5 and the p-value is 4.19e-12.\n"
        "Full metrics are in metrics/baseline.json.\n"
    )
    run_id, log, store = _run(
        tmp_path,
        metrics={"upper_fence": 7879.5, "p_value": 4.191e-12},
        report_text=text,
    )

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


@pytest.mark.parametrize("claim", ["1.0e999999999", "1.0e-999999999"])
def test_a18_rejects_unbounded_scientific_exponents_without_evaluating_them(
    tmp_path: Path, claim: str
) -> None:
    """Model-authored exponents cannot make the deterministic final audit allocate huge powers."""
    run_id, log, store = _run(
        tmp_path,
        metrics={"baseline": 1.0},
        report_text=f"# Report\n\nThe unsupported result is {claim}.\n",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert claim in a18.detail


def test_a18_rejects_a_value_outside_the_claims_decimal_half_step(tmp_path: Path) -> None:
    text = "# Report\n\nThe rounded correlation is 0.218.\n"
    run_id, log, store = _run(tmp_path, metrics={"correlation": 0.21749}, report_text=text)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.FAILED


def test_a18_fails_a_report_quoting_an_accuracy_no_experiment_recorded(tmp_path: Path) -> None:
    text = (
        "# Report\n\n"
        "The baseline model reached an accuracy of 0.97 on the held-out test set.\n"
        "Full metrics are in metrics/baseline.json.\n"
    )
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=text)

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "0.97" in a18.detail
    assert "A18" in {f.control_id for f in report.findings}


def test_a18_does_not_scan_structured_json_report_values_as_narrative_claims(
    tmp_path: Path,
) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_json(
        "audits/model.pkl.json",
        {
            "demographic_parity_difference": 0.217,
            "equal_opportunity_difference": -0.091,
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A18") == []


def test_a18_does_not_scan_tabular_csv_values_as_narrative_claims(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        "query/applications.csv",
        "group,difference\nA,0.217\nB,-0.091\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/csv",
    )

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A18") == []


def test_a18_fails_a_malformed_non_profile_json_report(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        "audits/model.json",
        '{"accuracy":',
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/json",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "not valid JSON" in a18.detail


@pytest.mark.parametrize(
    ("name", "media_type"),
    [("audits/model.json", "application/json"), ("query/applications.csv", "text/csv")],
)
def test_a18_fails_non_utf8_structured_reports(tmp_path: Path, name: str, media_type: str) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_bytes(
        name,
        b"\xff",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type=media_type,
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "not valid UTF-8" in a18.detail


def test_a18_still_scans_plain_text_narrative_claims(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        "reports/model.txt",
        "The unsupported accuracy is 0.97.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/plain; charset=utf-8",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "0.97" in a18.detail


@pytest.mark.parametrize(
    ("name", "media_type", "content"),
    [
        ("reports/model.md", "application/json", "# Accuracy\n\n0.83\n"),
        ("audits/model.json", "text/plain", '{"accuracy":0.83}'),
        ("reports/model.txt", "text/markdown", "Accuracy: 0.83\n"),
        ("reports/model.pdf", "application/json", '{"accuracy":0.83}'),
    ],
)
def test_a18_fails_inconsistent_report_extension_and_media_type(
    tmp_path: Path, name: str, media_type: str, content: str
) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        name,
        content,
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type=media_type,
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "inconsistent" in a18.detail


def test_a18_fails_an_unsupported_report_format(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        "reports/model.html",
        "<p>Accuracy: 0.83</p>",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/html",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "unsupported extension" in a18.detail


def test_a18_fails_a_reserved_profile_with_a_narrative_format(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_text(
        "profile/sample.txt",
        '{"profile_version":1}',
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/plain",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "reserved profile REPORT" in a18.detail


def test_a18_fails_a_report_citing_an_artifact_absent_from_the_manifest(tmp_path: Path) -> None:
    text = (
        "# Report\n\n"
        "The baseline model reached an accuracy of 0.83.\n"
        "See the ROC curve in plots/roc_curve.png.\n"
    )
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=text)

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "roc_curve.png" in a18.detail


@pytest.mark.parametrize("reference", ["plots/roc_curve.png", "../plots/roc_curve.png"])
def test_a18_resolves_root_and_report_relative_artifact_references(
    tmp_path: Path, reference: str
) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_bytes(
        "plots/roc_curve.png",
        b"registered plot evidence",
        produced_by=new_id("tool"),
        kind=ArtifactKind.PLOT,
    )
    store.save_text(
        "reports/model.md",
        f"The accuracy is 0.83. See {reference}.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
    )

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_rejects_a_report_reference_that_escapes_the_artifact_root(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_bytes(
        "secrets/data.csv",
        b"registered but outside the report-relative namespace",
        produced_by=new_id("tool"),
        kind=ArtifactKind.OTHER,
    )
    store.save_text(
        "reports/model.md",
        "The accuracy is 0.83. See ../../secrets/data.csv.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "outside the artifact namespace" in a18.detail


def test_a18_accepts_registered_dataset_path_and_recorded_split_evidence(tmp_path: Path) -> None:
    """A report may cite the declared workspace path and the experiment's split size."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_json(
        "datasets/credit.schema.json",
        {
            "name": "credit",
            "artifact_name": "datasets/credit.csv",
            "source_path": "data/applications.csv",
        },
        produced_by=new_id("tool"),
        kind=ArtifactKind.OTHER,
    )
    store.save_text(
        "reports/model.md",
        "The model used data/applications.csv and a 25% test split.\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
    )
    store.save_json(
        "metrics/model.json",
        {"accuracy": 0.83},
        produced_by=new_id("tool"),
        kind=ArtifactKind.METRICS,
    )
    experiment = Experiment(
        id=new_id("experiment"),
        run_id=run_id,
        name="baseline",
        status=ExperimentStatus.COMPLETED,
        metrics={"accuracy": 0.83},
    )
    log.append(
        EventType.MODEL_TRAINED,
        system,
        {"split": {"test_size": 0.25, "random_state": 7}},
    )
    log.append(
        EventType.EXPERIMENT_COMPLETED,
        system,
        {"experiment": experiment.model_dump(mode="json")},
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_is_not_applicable_without_a_report_artifact(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A18") == []


def test_a18_is_not_applicable_when_report_has_no_source_evidence(tmp_path: Path) -> None:
    """A report has no fidelity claim to audit until a producer records metrics evidence."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_text(
        "report.md",
        "The model reached an accuracy of 0.97.\n",
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.NOT_APPLICABLE
    assert _findings(report, "A18") == []


def test_a18_audits_the_markdown_source_of_an_exported_pdf(tmp_path: Path) -> None:
    """A claim just within export_pdf's coarser pre-flight tolerance still fails A18's own.

    export_pdf's pre-flight tolerates up to +/-0.05 (or 1% of the recorded value) so an agent's
    typo-sized error is not what it exists to catch; 0.87 against a recorded 0.83 (diff 0.04)
    passes that and renders. A18 resolves a claim at its own printed precision (two decimals here:
    +/-0.005), so it still independently fails the same PDF -- proving A18 is not merely repeating
    the pre-flight check.
    """
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text(
        "# Report\n\nThe unsupported accuracy is 0.87.\n",
        encoding="utf-8",
    )
    _execute_export_pdf_with_completion(
        log,
        ToolInvocation(
            run_id=run_id,
            agent_id=new_id("agent"),
            workspace=workspace,
            artifact_store=store,
        ),
        {
            "source_path": "report.md",
            "path": "report.pdf",
            "description": "Compile the audited report",
            "evidence_paths": ("metrics/baseline.json",),
        },
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "0.87" in a18.detail


def test_a18_resolves_an_exported_pdfs_report_relative_plot_reference(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    workspace = tmp_path / "workspace"
    (workspace / "reports").mkdir(parents=True)
    (workspace / "plots").mkdir()
    (workspace / "plots" / "roc_curve.png").write_bytes(_TINY_PNG)
    (workspace / "reports" / "model.md").write_text(
        "# Report\n\nThe accuracy is 0.83.\n\n![ROC](../plots/roc_curve.png)\n",
        encoding="utf-8",
    )
    store.save_bytes(
        "plots/roc_curve.png",
        _TINY_PNG,
        produced_by=new_id("tool"),
        kind=ArtifactKind.PLOT,
        media_type="image/png",
    )
    _execute_export_pdf_with_completion(
        log,
        ToolInvocation(
            run_id=run_id,
            agent_id=new_id("agent"),
            workspace=workspace,
            artifact_store=store,
        ),
        {
            "source_path": "reports/model.md",
            "path": "reports/model.pdf",
            "description": "Compile the audited report",
            "evidence_paths": ("metrics/baseline.json",),
        },
    )

    report = _audit(run_id, log, store)

    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_accepts_the_real_tool_manager_export_pdf_completion(tmp_path: Path) -> None:
    """The producer's actual manager envelope must satisfy A18 without a hand-built event."""
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text(
        "# Report\n\nThe accuracy is 0.83.\n",
        encoding="utf-8",
    )
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        task_id=new_id("task"),
        workspace=workspace,
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log),
        artifact_store=store,
        risk_profile=RiskProfile(
            risk_level="limited",
            activity_category="analysis",
            confidence=1.0,
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", ExportPdf()),))).execute(
        context,
        "export_pdf",
        {
            "source_path": "report.md",
            "path": "./report.pdf",
            "description": "Compile the audited report through the Tool Manager",
            "evidence_paths": ("metrics/baseline.json",),
        },
    )

    assert execution.result.success
    completed = next(
        event
        for event in reversed(log.events())
        if event.type is EventType.TOOL_COMPLETED and event.payload.get("tool") == "export_pdf"
    )
    assert completed.payload["artifact_ids"] == list(execution.result.artifact_ids)
    value = completed.payload["value"]["value"]
    assert value["path"] == "report.pdf"
    assert value["input_source_path"] == "report.md"
    assert value["source_path"] == "report.source.md"
    report = _audit(run_id, log, store)
    assert _control(report, "A18").status is ControlStatus.PASSED
    assert _findings(report, "A18") == []


def test_a18_fails_a_pdf_without_a_markdown_lineage_source(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_bytes(
        "orphan.pdf",
        _blank_pdf(),
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "Markdown source" in a18.detail


def test_a18_fails_a_pdf_whose_markdown_source_is_not_utf8(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    source = store.save_bytes(
        "report.source.md",
        b"\xff",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/markdown",
    )
    store.save_bytes(
        "report.pdf",
        _blank_pdf(),
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    store.set_lineage(
        "report.pdf",
        input_artifact_ids=(source.id,),
        execution_key="0" * 64,
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "UTF-8" in a18.detail


def test_a18_fails_a_pdf_whose_lineage_input_is_not_markdown(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    source = store.save_text(
        "report.source.txt",
        "# Report\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/plain",
    )
    store.save_bytes(
        "report.pdf",
        _blank_pdf(),
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    store.set_lineage(
        "report.pdf",
        input_artifact_ids=(source.id,),
        execution_key="0" * 64,
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "compatible Markdown" in a18.detail


def test_a18_fails_a_pdf_with_a_noncanonical_execution_key(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    source = store.save_text(
        "report.source.md",
        "# Report\n",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/markdown",
    )
    store.save_bytes(
        "report.pdf",
        _blank_pdf(),
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    store.set_lineage(
        "report.pdf",
        input_artifact_ids=(source.id,),
        execution_key="not-a-canonical-sha256",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "execution key" in a18.detail


def test_a18_fails_a_forged_pdf_pair_with_a_shape_only_execution_key(tmp_path: Path) -> None:
    """A plausible event and lineage cannot make an arbitrary 64-hex key authentic."""
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    source = store.save_text(
        "report.source.md",
        "# Report\n\nThe accuracy is 0.83.\n",
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
        media_type="text/markdown",
    )
    pdf_bytes = _blank_pdf()
    pdf = store.save_bytes(
        "report.pdf",
        pdf_bytes,
        produced_by=new_id("agent"),
        kind=ArtifactKind.REPORT,
        media_type="application/pdf",
    )
    store.set_lineage(
        pdf.name,
        input_artifact_ids=(source.id,),
        execution_key="0" * 64,
    )
    _record_export_pdf_completion(
        log,
        source_id=source.id,
        source_name=source.name,
        pdf_id=pdf.id,
        pdf_name=pdf.name,
        pdf_size=len(pdf_bytes),
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "execution key does not bind" in a18.detail


def test_a18_fails_a_pdf_pair_without_a_completed_export_event(tmp_path: Path) -> None:
    """A content-bound pair still needs manager evidence that export_pdf produced it."""
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text(
        "# Report\n\nThe accuracy is 0.83.\n",
        encoding="utf-8",
    )
    ExportPdf().execute(
        ToolInvocation(
            run_id=run_id,
            agent_id=new_id("agent"),
            workspace=workspace,
            artifact_store=store,
        ),
        {
            "source_path": "report.md",
            "path": "report.pdf",
            "description": "Compile without Tool Manager evidence",
            "evidence_paths": ("metrics/baseline.json",),
        },
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "completed export_pdf event" in a18.detail


def test_a18_fails_an_unreadable_non_pdf_report_instead_of_skipping_it(tmp_path: Path) -> None:
    run_id, log, store = _run(tmp_path, metrics={"accuracy": 0.83}, report_text=None)
    store.save_bytes(
        "report.txt",
        b"\xff",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/plain",
    )

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "readable UTF-8" in a18.detail


def test_a18_fails_an_unreadable_non_pdf_report_without_metrics_evidence(
    tmp_path: Path,
) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    system = Actor.system()
    log.append(EventType.RUN_STARTED, system, {})
    store = LocalArtifactStore(tmp_path / "artifacts", run_id)
    store.save_bytes(
        "report.txt",
        b"\xff",
        produced_by=new_id("tool"),
        kind=ArtifactKind.REPORT,
        media_type="text/plain",
    )
    log.append(EventType.RUN_COMPLETED, system, {})

    report = _audit(run_id, log, store)

    a18 = _control(report, "A18")
    assert a18.status is ControlStatus.FAILED
    assert "readable UTF-8" in a18.detail
