"""export_pdf: render a workspace Markdown deliverable (and its images) into a PDF Artifact."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from pypdf import PdfReader

import thymira.tools.builtins.export_pdf as export_pdf_module
from thymira.events import PDF_EXPORT_EXECUTION_VERSION, hash_pdf_export_execution
from thymira.schemas import ArtifactKind, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import ToolInvocation
from thymira.tools.builtins.export_pdf import ExportPdf, PdfExportValue
from thymira.tools.models import ToolExecutionError

if TYPE_CHECKING:
    from pathlib import Path

# A verified, CRC-correct 1x1 red PNG (69 bytes) -- generated once from raw PNG chunks rather
# than hand-typed, so a single mistyped byte can never make this fixture silently invalid.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)


def _invocation(tmp_path: Path) -> ToolInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_id = new_id("run")
    return ToolInvocation(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=workspace,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
    )


def _arguments(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_path": "report.md",
        "path": "report.pdf",
        "description": "Compile the report",
    }
    base.update(overrides)
    return base


def test_export_pdf_renders_markdown_and_embeds_a_workspace_image(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "plots").mkdir()
    (invocation.workspace / "plots" / "chart.png").write_bytes(_TINY_PNG)
    (invocation.workspace / "report.md").write_text(
        "# Findings\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n![Chart](plots/chart.png)\n",
        encoding="utf-8",
    )

    result = ExportPdf().execute(invocation, _arguments())

    assert result.success
    assert isinstance(result.value, PdfExportValue)
    assert result.value.page_count >= 1
    output = invocation.workspace / "report.pdf"
    assert output.is_file()
    reader = PdfReader(str(output))
    assert "Findings" in (reader.pages[0].extract_text() or "")


def test_export_pdf_renders_unordered_list_markers_as_ascii_hyphens(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "# Notes\n\n"
        "- Source: registered dataset.\n"
        "    - Nested detail.\n"
        "- Scope: descriptive analysis only.\n\n"
        "1. First ordered step.\n"
        "2. Second ordered step.\n",
        encoding="utf-8",
    )

    ExportPdf().execute(invocation, _arguments())

    reader = PdfReader(str(invocation.workspace / "report.pdf"))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "- Source: registered dataset." in text
    assert "- Nested detail." in text
    assert "- Scope: descriptive analysis only." in text
    assert "1. First ordered step." in text
    assert "2. Second ordered step." in text
    assert "\ufffd" not in text
    base_fonts = {
        str(font.get_object().get("/BaseFont"))
        for page in reader.pages
        for font in page.get("/Resources", {}).get("/Font", {}).values()
    }
    assert "/Symbol" not in base_fonts


def test_export_pdf_keeps_plot_heading_with_its_image(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    plots = invocation.workspace / "plots"
    plots.mkdir()
    section_names = [f"Variable {index}" for index in range(7)]
    markdown_sections: list[str] = []
    for index, section_name in enumerate(section_names):
        image_name = f"plot-{index}.png"
        Image.new("RGB", (600, 400), (index * 30, 80, 160)).save(plots / image_name)
        markdown_sections.append(
            f"## {section_name}\n\nDistribution for {section_name}.\n\n"
            f"![{section_name}](plots/{image_name})"
        )
    (invocation.workspace / "report.md").write_text(
        "# Report\n\nA compact report with seven plots.\n\n"
        + "\n\n".join(markdown_sections)
        + "\n\n## Notes\n\n- Source: registered dataset.\n- Scope: descriptive only.\n",
        encoding="utf-8",
    )

    ExportPdf().execute(invocation, _arguments())

    reader = PdfReader(str(invocation.workspace / "report.pdf"))
    for page in reader.pages:
        text = page.extract_text() or ""
        headings = sum(section_name in text for section_name in section_names)
        resources = page.get("/Resources", {})
        xobjects = resources.get("/XObject", {})
        images = sum(
            resource.get_object().get("/Subtype") == "/Image" for resource in xobjects.values()
        )
        assert headings <= images, f"orphaned plot heading on page containing {text!r}"


def test_export_pdf_always_registers_a_pdf_report_artifact(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    source_bytes = b"# Title\n\nBody text.\n"
    (invocation.workspace / "report.md").write_bytes(source_bytes)

    result = ExportPdf().execute(invocation, _arguments())

    assert isinstance(result.value, PdfExportValue)
    artifact_id = result.value.artifact_id
    assert artifact_id is not None
    active = {candidate.id: candidate for candidate in invocation.artifact_store.list_active()}
    assert len(result.artifact_ids) == 2
    assert set(result.artifact_ids) == set(active)
    pdf_artifact = active[artifact_id]
    (source_id,) = set(result.artifact_ids).difference({artifact_id})
    source_artifact = active[source_id]
    assert source_artifact.name == "report.source.md"
    assert result.value.input_source_path == "report.md"
    assert result.value.source_path == source_artifact.name
    assert source_artifact.kind is ArtifactKind.REPORT
    assert source_artifact.media_type == "text/markdown"
    assert invocation.artifact_store.load_bytes(source_artifact.name) == source_bytes
    assert pdf_artifact.name == "report.pdf"
    assert pdf_artifact.kind is ArtifactKind.REPORT
    assert pdf_artifact.media_type == "application/pdf"
    assert pdf_artifact.input_artifact_ids == (source_artifact.id,)
    assert result.value.format_version == PDF_EXPORT_EXECUTION_VERSION
    assert pdf_artifact.execution_key == hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool="export_pdf",
        source_name=source_artifact.name,
        source_sha256=source_artifact.sha256,
        output_name=pdf_artifact.name,
        output_sha256=pdf_artifact.sha256,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", pdf_artifact.execution_key)


def test_pdf_export_execution_key_binds_both_digests_independently() -> None:
    baseline = hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool="export_pdf",
        source_name="report.source.md",
        source_sha256="1" * 64,
        output_name="report.pdf",
        output_sha256="2" * 64,
    )
    changed_source = hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool="export_pdf",
        source_name="report.source.md",
        source_sha256="3" * 64,
        output_name="report.pdf",
        output_sha256="2" * 64,
    )
    changed_pdf = hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool="export_pdf",
        source_name="report.source.md",
        source_sha256="1" * 64,
        output_name="report.pdf",
        output_sha256="4" * 64,
    )

    assert changed_source != baseline
    assert changed_pdf != baseline


def test_export_pdf_execution_key_changes_with_its_source_and_output_pair(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text("# Stable\n", encoding="utf-8")

    first = ExportPdf().execute(invocation, _arguments())
    assert isinstance(first.value, PdfExportValue)
    first_pdf = next(
        artifact
        for artifact in invocation.artifact_store.list_active()
        if artifact.id == first.value.artifact_id
    )
    (invocation.workspace / "report.md").write_text("# Changed\n", encoding="utf-8")
    second = ExportPdf().execute(invocation, _arguments())
    assert isinstance(second.value, PdfExportValue)
    second_pdf = next(
        artifact
        for artifact in invocation.artifact_store.list_active()
        if artifact.id == second.value.artifact_id
    )

    assert first_pdf.execution_key is not None
    assert second_pdf.execution_key != first_pdf.execution_key


def test_export_pdf_publishes_source_and_pdf_as_one_atomic_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text("# Atomic\n", encoding="utf-8")
    store = invocation.artifact_store
    assert isinstance(store, LocalArtifactStore)
    original = store._write_staged_bytes
    writes = 0

    def fail_on_second(target: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected PDF batch publication failure")
        original(target, data)

    monkeypatch.setattr(store, "_write_staged_bytes", fail_on_second)

    with pytest.raises(OSError, match="injected PDF batch"):
        ExportPdf().execute(invocation, _arguments())

    assert store.list_active() == []


def test_export_pdf_accepts_markdown_table_alignment_styles(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "| left | right |\n| :--- | ---: |\n| a | 1 |\n",
        encoding="utf-8",
    )

    result = ExportPdf().execute(invocation, _arguments())

    assert result.success


def test_export_pdf_normalizes_windows_style_source_and_output_paths(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    reports = invocation.workspace / "reports"
    reports.mkdir()
    (reports / "chart.png").write_bytes(_TINY_PNG)
    (reports / "report.md").write_text("![Chart](chart.png)\n", encoding="utf-8")

    result = ExportPdf().execute(
        invocation,
        _arguments(source_path=r"reports\report.md", path=r"reports\report.pdf"),
    )

    assert result.success
    assert isinstance(result.value, PdfExportValue)
    assert result.value.path == "reports/report.pdf"
    assert (reports / "report.pdf").is_file()


def test_export_pdf_resolves_a_parent_image_path_that_stays_in_the_workspace(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    reports = invocation.workspace / "reports"
    reports.mkdir()
    plots = invocation.workspace / "plots"
    plots.mkdir()
    (plots / "chart.png").write_bytes(_TINY_PNG)
    (reports / "report.md").write_text("![Chart](../plots/chart.png)\n", encoding="utf-8")

    result = ExportPdf().execute(
        invocation,
        _arguments(source_path="reports/report.md", path="reports/report.pdf"),
    )

    assert result.success


def test_export_pdf_refuses_an_image_path_that_escapes_the_workspace(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    reports = invocation.workspace / "reports"
    reports.mkdir()
    (reports / "report.md").write_text("![Secret](../../secret.png)\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="escapes the workspace"):
        ExportPdf().execute(
            invocation,
            _arguments(source_path="reports/report.md", path="reports/report.pdf"),
        )


def test_export_pdf_refuses_a_missing_source(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)

    with pytest.raises(ToolExecutionError, match="cannot read"):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_refuses_a_missing_embedded_image(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "# Findings\n\n![Chart](plots/missing.png)\n", encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError, match=re.escape("plots/missing.png")):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_refuses_a_remote_image_url(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "# Findings\n\n![Chart](https://example.com/chart.png)\n", encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError, match="non-local image source"):
        ExportPdf().execute(invocation, _arguments())


@pytest.mark.parametrize(
    "malicious_html",
    [
        "<IMG SRC='https://example.com/chart.png'>",
        '<img src="//example.com/chart.png">',
        '<img src="file:///etc/passwd">',
        "<style>body { background: url(https://example.com/tracker.png); }</style>",
        "<link rel='stylesheet' href='https://example.com/style.css'>",
    ],
)
def test_export_pdf_rejects_raw_html_before_the_renderer_can_fetch_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malicious_html: str,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(malicious_html, encoding="utf-8")

    def _unexpected_renderer(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("renderer must not receive untrusted raw HTML")

    monkeypatch.setattr(export_pdf_module.pisa, "CreatePDF", _unexpected_renderer)

    with pytest.raises(ToolExecutionError, match="unsafe HTML"):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_keeps_html_examples_inside_markdown_code_inert(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    fence = "`" * 3
    (invocation.workspace / "report.md").write_text(
        f"# Examples\n\n`<model>`\n\n{fence}html\n<img src='https://example.com/x'>\n{fence}\n",
        encoding="utf-8",
    )

    result = ExportPdf().execute(invocation, _arguments())

    assert result.success
    assert isinstance(result.value, PdfExportValue)


def test_export_pdf_rejects_an_oversized_image_before_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "huge.png").write_bytes(
        _TINY_PNG + b"0" * export_pdf_module._MAX_EMBEDDED_IMAGE_BYTES
    )
    (invocation.workspace / "report.md").write_text("![Huge](huge.png)\n", encoding="utf-8")

    def _unexpected_renderer(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("renderer must not receive oversized image data")

    monkeypatch.setattr(export_pdf_module.pisa, "CreatePDF", _unexpected_renderer)

    with pytest.raises(ToolExecutionError, match="exceeds"):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_refuses_malformed_png_before_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\nnot a PNG")
    (invocation.workspace / "report.md").write_text("![Bad](bad.png)\n", encoding="utf-8")

    def _unexpected_renderer(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("renderer must not receive malformed image data")

    monkeypatch.setattr(export_pdf_module.pisa, "CreatePDF", _unexpected_renderer)

    with pytest.raises(ToolExecutionError, match="valid PNG"):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_renderer_callback_refuses_non_data_resources() -> None:
    with pytest.raises(ToolExecutionError, match="external resource"):
        export_pdf_module._resource_link_callback("file:///etc/passwd")


def _register_json(
    invocation: ToolInvocation,
    name: str,
    data: object,
    *,
    kind: ArtifactKind = ArtifactKind.METRICS,
) -> None:
    """Register a JSON evidence artifact the way a real Run's tool call would."""
    invocation.artifact_store.save_json(name, data, produced_by=invocation.agent_id, kind=kind)


def test_export_pdf_refuses_evidence_that_is_not_a_registered_metrics_artifact(
    tmp_path: Path,
) -> None:
    """MIRA's A18 counts only kind="metrics" JSON; export_pdf must not accept anything looser."""
    invocation = _invocation(tmp_path)
    _register_json(invocation, "facts.json", {"share": 69.9}, kind=ArtifactKind.OTHER)
    (invocation.workspace / "report.md").write_text("Share is 69.9%.\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match=r"facts\.json.*kind=.metrics."):
        ExportPdf().execute(invocation, _arguments(evidence_paths=("facts.json",)))


def test_export_pdf_accepts_a_citation_the_evidence_file_records(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_json(
        invocation, "metrics.json", {"auc": 0.7844, "class_balance": {"good": 69.9, "bad": 30.1}}
    )
    (invocation.workspace / "report.md").write_text(
        "# Report\n\nAUC is 0.7844. Good-risk share is 69.9%.\n", encoding="utf-8"
    )

    result = ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))

    assert result.success


def test_export_pdf_refuses_a_citation_no_evidence_file_records(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_json(invocation, "metrics.json", {"auc": 0.7844})
    (invocation.workspace / "report.md").write_text(
        "# Report\n\nAUC is 0.7844. Good-risk share is 69.9%.\n", encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError, match=r"69\.9%"):
        ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))


def test_export_pdf_refuses_a_citation_backed_only_by_an_unregistered_workspace_edit(
    tmp_path: Path,
) -> None:
    """A workspace edit never re-registered is not evidence, exactly like MIRA's audit."""
    invocation = _invocation(tmp_path)
    _register_json(invocation, "metrics.json", {"auc": 0.5})
    # Edit the file on disk without re-registering it -- what the fixed live bug looked like.
    (invocation.workspace / "metrics.json").write_text(
        '{"auc": 0.5, "share": 69.9}', encoding="utf-8"
    )
    (invocation.workspace / "report.md").write_text("Share is 69.9%.\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match=r"69\.9%"):
        ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))


def test_export_pdf_refuses_a_citation_when_no_evidence_paths_are_given(
    tmp_path: Path,
) -> None:
    """A cited number with no evidence_paths at all must not render unchecked (A18 blocks it)."""
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "# Report\n\nAn entirely unverifiable 42.7% claim.\n", encoding="utf-8"
    )

    with pytest.raises(ToolExecutionError, match=r"evidence_paths"):
        ExportPdf().execute(invocation, _arguments())


def test_export_pdf_skips_the_citation_check_for_a_source_with_no_numeric_claims(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text(
        "# Report\n\nNo numeric claim here at all.\n", encoding="utf-8"
    )

    result = ExportPdf().execute(invocation, _arguments())

    assert result.success


def test_export_pdf_citation_check_ignores_heading_numbers(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_json(invocation, "metrics.json", {"auc": 0.5})
    (invocation.workspace / "report.md").write_text(
        "### 3.1 Bivariate Analysis\n\nAUC is 0.5.\n", encoding="utf-8"
    )

    result = ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))

    assert result.success


def test_export_pdf_citation_check_ignores_bare_integers(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_json(invocation, "metrics.json", {"auc": 0.5})
    (invocation.workspace / "report.md").write_text(
        "AUC is 0.5. The top 10 features across 3-4 charts.\n", encoding="utf-8"
    )

    result = ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))

    assert result.success


def test_export_pdf_citation_check_reads_multiple_evidence_files(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    _register_json(invocation, "metrics.json", {"auc": 0.7844})
    _register_json(invocation, "facts.json", {"share": 69.9})
    (invocation.workspace / "report.md").write_text(
        "AUC is 0.7844. Good-risk share is 69.9%.\n", encoding="utf-8"
    )

    result = ExportPdf().execute(
        invocation, _arguments(evidence_paths=("metrics.json", "facts.json"))
    )

    assert result.success


def test_export_pdf_refuses_an_evidence_artifact_that_is_not_valid_json(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    invocation.artifact_store.save_bytes(
        "metrics.json",
        b"not json",
        produced_by=invocation.agent_id,
        kind=ArtifactKind.METRICS,
        media_type="application/json",
    )
    (invocation.workspace / "report.md").write_text("AUC is 0.5.\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="not valid JSON"):
        ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))


def test_export_pdf_refuses_an_unregistered_evidence_name(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    (invocation.workspace / "report.md").write_text("AUC is 0.5.\n", encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="cannot read registered evidence artifact"):
        ExportPdf().execute(invocation, _arguments(evidence_paths=("metrics.json",)))
