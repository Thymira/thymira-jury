"""Render a workspace Markdown deliverable, with its embedded images, to a PDF Artifact.

Closes a real gap found live (2026-09-11): a Run's declared deliverable can be a `.md` report,
but nothing in the tool surface could turn one into the PDF a downstream reviewer actually wants,
short of a human doing it by hand outside the Run entirely. `export_pdf` reads a Markdown file
this Run already wrote, embeds each image it references (as a base64 data URI, so the PDF is a
single self-contained file), and always registers the result as a `kind="report"` Artifact --
unlike `write_file`, where registration is opt-in, a PDF export with no registered Artifact would
defeat the tool's entire purpose.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import markdown as _markdown_lib
from pydantic import BaseModel, Field
from pypdf import PdfReader
from xhtml2pdf import pisa

from thymira.events import (
    PDF_EXPORT_EXECUTION_VERSION,
    hash_pdf_export_execution,
    sha256_bytes,
)
from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.state import ArtifactWrite
from thymira.tools.artifact_media import infer_artifact_media_type
from thymira.tools.artifact_validation import (
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    validate_artifact_bytes,
)
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.files import contained_path
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import ToolValue

if TYPE_CHECKING:
    from collections.abc import Buffer

_MAX_SOURCE_BYTES = 1024 * 1024
"""Matches ``write_file``'s own ceiling for a text file this tool reads back."""

_MAX_PDF_BYTES = MAX_PDF_BYTES
"""Matches the artifact ecosystem's general binary-file ceiling."""

_MAX_EMBEDDED_IMAGE_BYTES = MAX_IMAGE_BYTES
_MAX_EMBEDDED_TOTAL_BYTES = 12 * 1024 * 1024
_MAX_RENDERED_HTML_BYTES = 18 * 1024 * 1024
_MAX_SOURCE_LINES = 5_000
_MAX_BODY_TAGS = 10_000
_EXPORT_TOOL_NAME = "export_pdf"

_IMG_TAG = re.compile(r'(<img[^>]*\ssrc=")([^"]+)("[^>]*>)')
_DATA_IMAGE_URI = re.compile(
    r"data:image/(?:png|jpeg);base64,[A-Za-z0-9+/]*={0,2}",
    flags=re.IGNORECASE,
)
_LIST_TAG = re.compile(r"</?(?:ol|ul|li)\b[^>]*>", flags=re.IGNORECASE)
_IMAGE_INTRO_PARAGRAPH = re.compile(
    r"<p>(?P<content>(?:(?!</p>).)*)</p>(?P<gap>\s*)"
    r"(?=<p>\s*<img\b[^>]*?/?>\s*</p>)",
    flags=re.IGNORECASE | re.DOTALL,
)

_CSS = """
@page { size: A4; margin: 1.8cm 1.6cm 2cm 1.6cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; line-height: 1.45;
    color: #1a1a1a; }
h1 { font-size: 19pt; color: #12233d; border-bottom: 2pt solid #12233d; padding-bottom: 6pt;
    margin-bottom: 8pt; }
h2 { font-size: 14pt; color: #12233d; border-bottom: 0.75pt solid #a9b6c9; padding-bottom: 3pt;
    margin-top: 20pt; margin-bottom: 8pt; }
h3 { font-size: 11.5pt; color: #1c3a63; margin-top: 14pt; margin-bottom: 6pt; }
h2, h3, p.keep-with-next { -pdf-keep-with-next: true; }
p { margin: 6pt 0; text-align: justify; }
strong { color: #0d1b2f; }
code { font-family: Courier, monospace; font-size: 8.7pt; background-color: #eef1f6;
    padding: 1pt 3pt; }
table { width: 17.8cm; border-collapse: collapse; margin: 8pt 0 12pt 0; font-size: 8.3pt; }
th { background-color: #12233d; color: #ffffff; padding: 4pt 5pt; text-align: left;
    border: 0.5pt solid #12233d; }
td { padding: 3.5pt 5pt; border: 0.5pt solid #c7cedb; }
tr.odd td { background-color: #f4f6fa; }
img { width: 100%; margin: 8pt 0 4pt 0; }
blockquote { background-color: #fff6e6; border-left: 3pt solid #c99a2e; padding: 6pt 10pt;
    margin: 10pt 0; font-size: 9pt; }
hr { border: none; border-top: 0.5pt solid #c7cedb; margin: 14pt 0; }
ol, div.unordered-list { margin: 6pt 0; padding-left: 16pt; }
li, div.unordered-list-item { margin: 3pt 0; }
"""


_ALLOWED_HTML_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_RESOURCE_ATTRIBUTES = {"background", "data", "poster", "srcset", "xlink:href"}


class _HtmlSafetyValidator(HTMLParser):
    """Find renderer-visible HTML that could read a local or remote resource."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.problem: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def handle_decl(self, decl: str) -> None:
        self.problem = self.problem or f"declaration <!{decl}>"

    def handle_pi(self, data: str) -> None:
        self.problem = self.problem or f"processing instruction <?{data}>"

    def _inspect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag not in _ALLOWED_HTML_TAGS:
            self.problem = self.problem or f"unsupported tag <{tag}>"
            return
        normalized_attrs = {name.casefold(): value for name, value in attrs}
        style = normalized_attrs.get("style")
        if style is not None and not (
            normalized_tag in {"td", "th"}
            and re.fullmatch(r"text-align:\s*(?:left|right|center);?", style.casefold())
        ):
            self.problem = self.problem or f"style attribute on <{tag}>"
            return
        resource_attributes = _RESOURCE_ATTRIBUTES.intersection(normalized_attrs)
        if resource_attributes:
            attribute = min(resource_attributes)
            self.problem = self.problem or f"resource attribute {attribute!r} on <{tag}>"
            return
        if normalized_tag != "img" or "src" not in normalized_attrs:
            if "src" in normalized_attrs:
                self.problem = self.problem or f"resource attribute 'src' on <{tag}>"
            return
        src = normalized_attrs["src"] or ""
        if not _is_workspace_relative_resource(src):
            self.problem = self.problem or f"non-local image source {src!r}"


def _reject_unsafe_html(html: str) -> None:
    """Refuse renderer-visible resource access while retaining inert code examples."""
    validator = _HtmlSafetyValidator()
    validator.feed(html)
    validator.close()
    if validator.problem is not None:
        raise ToolExecutionError(
            f"export_pdf refuses unsafe HTML before rendering ({validator.problem})"
        )


def _is_workspace_relative_resource(src: str) -> bool:
    """Return whether an image source is an unqualified local relative path."""
    parsed = urlsplit(src)
    return bool(
        parsed.path
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and not src.startswith(("//", "\\\\"))
    )


def _resource_link_callback(uri: str, _base_path: str | None = None) -> str:
    """Allow only data images generated by ``_embed_images`` at pisa's I/O boundary."""
    if _DATA_IMAGE_URI.fullmatch(uri):
        return uri
    raise ToolExecutionError(f"export_pdf renderer refused an external resource: {uri!r}")


class _BoundedPdfBuffer(BytesIO):
    """Stop renderer output growth before its in-memory buffer exceeds the PDF ceiling."""

    def write(self, data: Buffer, /) -> int:
        if self.tell() + memoryview(data).nbytes > _MAX_PDF_BYTES:
            raise ToolExecutionError(f"rendered PDF exceeds {_MAX_PDF_BYTES} bytes")
        return super().write(data)


class PdfExportValue(ToolValue):
    """A compiled PDF and its primary registered artifact."""

    path: str = Field(min_length=1)
    input_source_path: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    format_version: int = Field(ge=1)
    bytes_written: int = Field(ge=0)
    page_count: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class ExportPdfArguments(BaseModel):
    """Arguments for export_pdf."""

    source_path: str = Field(
        min_length=1, description="Workspace-relative Markdown file to render."
    )
    path: str = Field(min_length=1, description="Workspace-relative path for the rendered PDF.")
    description: Description = DESCRIPTION_FIELD
    evidence_paths: tuple[str, ...] = Field(
        default=(),
        max_length=16,
        description=(
            'Names of already-registered JSON artifacts of kind="metrics" whose numbers back '
            "every number cited in the Markdown source; a JSON registered under any other kind "
            "is refused, because MIRA's audit counts only metrics artifacts. Read from the "
            "artifact store, not "
            "the workspace file -- if you edited the file since its last registration, "
            "re-register it (run_python.output_artifacts or mlflow_log_artifact) before citing "
            "it, or the edit will not count as evidence here or in MIRA's later audit. Required "
            "whenever the Markdown source cites a decimal or percentage number: left empty, "
            "export_pdf refuses to render such a source instead of producing a PDF the audit "
            "will later block; given, it refuses to render if the source cites a number none of "
            "them record."
        ),
    )


def _export_pdf_capability() -> ToolCapability:
    return ToolCapability(
        id="export_pdf",
        data_access=("workspace",),
        side_effects=("artifact_write",),
        external_effects=(),
    )


_CITED_NUMBER_RE = re.compile(r"-?\d+\.\d+%?|-?\d+%")
"""A decimal or a percentage — the two shapes every unsupported citation MIRA's A18 has found
live were. A bare integer (a count, a hyperparameter, "3-4 charts") never matches, so this check
stays conservative: it catches the observed failure class without refusing ordinary prose."""

_CITATION_TOLERANCE_FLOOR = 0.05
_CITATION_RELATIVE_TOLERANCE = 0.01


def _cited_numbers(markdown_text: str) -> list[tuple[str, float]]:
    """Return each decimal/percentage citation outside a heading line, with its numeric value."""
    cited: list[tuple[str, float]] = []
    for line in markdown_text.splitlines():
        if line.lstrip().startswith("#"):
            continue  # a heading's own numbering (### 3.1 ...) cites nothing
        for match in _CITED_NUMBER_RE.finditer(line):
            token = match.group(0)
            try:
                cited.append((token, float(token.removesuffix("%"))))
            except ValueError:  # pragma: no cover — the pattern only matches parseable floats
                continue
    return cited


def _collect_evidence_numbers(value: object, into: set[float]) -> None:
    """Recursively collect every numeric leaf of a parsed JSON evidence document."""
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        into.add(float(value))
    elif isinstance(value, dict):
        for item in value.values():
            _collect_evidence_numbers(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_evidence_numbers(item, into)


def _load_evidence_numbers(
    invocation: ToolInvocation, evidence_paths: tuple[str, ...]
) -> set[float]:
    """Read every declared evidence file's *registered* content and collect its numbers.

    Reads through ``invocation.artifact_store.load_json`` -- the same hash-verified, registered
    snapshot MIRA's A18 later checks -- never the raw workspace filesystem. A file an agent edited
    after its last registration would pass a filesystem-based check while still being invisible to
    the audit, which trusts only what was actually published; reading the store here closes that
    gap instead of only narrowing it.
    """
    recorded: set[float] = set()
    for name in evidence_paths:
        registered = invocation.artifact_store.get(str(name).replace("\\", "/"))
        if registered is not None and registered.kind is not ArtifactKind.METRICS:
            # MIRA's A18 counts numbers only from kind="metrics" JSON artifacts. Anything
            # looser here would pass a report the audit then blocks for the same numbers.
            raise ToolExecutionError(
                f"export_pdf: evidence artifact {name!r} is registered as "
                f'kind="{registered.kind.value}", not kind="metrics"; re-register it as '
                'kind="metrics" (run_python.output_artifacts) before citing it.'
            )
        try:
            data = invocation.artifact_store.load_json(str(name).replace("\\", "/"))
        except OSError as exc:
            raise ToolExecutionError(
                f"export_pdf: cannot read registered evidence artifact {name!r}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ToolExecutionError(
                f"export_pdf: registered evidence artifact {name!r} is not valid JSON: {exc}"
            ) from exc
        _collect_evidence_numbers(data, recorded)
    return recorded


def _citation_resolves(value: float, recorded: set[float]) -> bool:
    """Whether a cited number matches a recorded one, read as itself, a ratio, or a percentage."""
    for candidate in (value, value / 100, value * 100):
        for known in recorded:
            tolerance = max(_CITATION_TOLERANCE_FLOOR, abs(known) * _CITATION_RELATIVE_TOLERANCE)
            if abs(candidate - known) <= tolerance:
                return True
    return False


def _reject_unsupported_citations(
    markdown_text: str, invocation: ToolInvocation, evidence_paths: tuple[str, ...]
) -> None:
    """Refuse to render a PDF whose source cites a number no *registered* evidence file records.

    Cheap, conservative and additive: a source with no decimal/percentage citation at all is
    skipped entirely, evidence_paths or not -- there is nothing here for MIRA's A18 to later
    block on. A source that *does* cite one, given no ``evidence_paths``, is refused up front
    instead of rendering unchecked: an agent that forgets the argument must not get a PDF that
    A18 blocks after the fact for exactly this Run. Never a substitute for MIRA's own A18
    report-fidelity control, which recomputes independently from the Run's recorded evidence
    after the fact -- this only saves the agent from spending the rest of a Run's turn budget on
    a report MIRA would go on to block, by refusing it immediately with the exact numbers to fix.
    Reads through the artifact store, not the workspace filesystem, so a workspace edit an agent
    forgot to re-register cannot pass this check either -- the two must see the same evidence
    MIRA will.
    """
    cited = _cited_numbers(markdown_text)
    if not cited:
        return
    if not evidence_paths:
        unsupported = sorted({token for token, _ in cited})
        raise ToolExecutionError(
            "export_pdf refuses to render: the source cites "
            f'{", ".join(unsupported)}; pass evidence_paths naming the kind="metrics" JSON '
            "artifacts that record: " + ", ".join(unsupported)
        )
    recorded = _load_evidence_numbers(invocation, evidence_paths)
    unsupported = sorted(
        {token for token, value in cited if not _citation_resolves(value, recorded)}
    )
    if unsupported:
        raise ToolExecutionError(
            "export_pdf refuses to render: the source cites "
            f"{', '.join(unsupported)}, which none of {list(evidence_paths)} records. "
            "Save every number the report cites into one of those files first, or list the file "
            "that already records it in evidence_paths."
        )


def _zebra_stripe(html: str) -> str:
    """Tag every other body row of each <tbody> with class="odd" for the CSS above to stripe."""
    sections = html.split("<tbody>")
    if len(sections) == 1:
        return html
    rebuilt = [sections[0]]
    for section in sections[1:]:
        body, _, rest = section.partition("</tbody>")
        rows = body.split("<tr>")
        striped = [rows[0]]
        for index, row in enumerate(rows[1:]):
            striped.append(f'<tr class="odd">{row}' if index % 2 == 0 else f"<tr>{row}")
        rebuilt.append("".join(striped) + "</tbody>" + rest)
    return "<tbody>".join(rebuilt)


def _render_unordered_lists_as_ascii(html: str) -> str:
    """Render unordered lists as indented blocks with visible ASCII hyphens."""
    rendered: list[str] = []
    list_stack: list[str] = []
    item_stack: list[str] = []
    cursor = 0
    for match in _LIST_TAG.finditer(html):
        rendered.append(html[cursor : match.start()])
        tag = match.group(0)
        normalized = tag.casefold()
        name_match = re.match(r"</?([a-z]+)", normalized)
        if name_match is None:
            rendered.append(tag)
            cursor = match.end()
            continue
        name = name_match.group(1)
        is_closing = normalized.startswith("</")
        if name in {"ol", "ul"}:
            rendered.append(_render_list_container(tag, name, is_closing, list_stack))
        else:
            rendered.append(_render_list_item(tag, is_closing, list_stack, item_stack))
        cursor = match.end()
    rendered.append(html[cursor:])
    return "".join(rendered)


def _render_list_container(tag: str, name: str, is_closing: bool, list_stack: list[str]) -> str:
    """Render one list-container tag and update its validated nesting stack."""
    if is_closing:
        if list_stack and list_stack[-1] == name:
            list_stack.pop()
            return "</div>" if name == "ul" else tag
        return tag
    list_stack.append(name)
    return '<div class="unordered-list">' if name == "ul" else tag


def _render_list_item(
    tag: str,
    is_closing: bool,
    list_stack: list[str],
    item_stack: list[str],
) -> str:
    """Render one list-item tag according to the type of its containing list."""
    if is_closing:
        owner = item_stack.pop() if item_stack else ""
        return "</div>" if owner == "ul" else tag
    owner = list_stack[-1] if list_stack else ""
    item_stack.append(owner)
    return '<div class="unordered-list-item">- ' if owner == "ul" else tag


def _keep_image_intro_with_figure(html: str) -> str:
    """Mark a paragraph immediately before an image so its heading and figure stay together."""

    def _mark(match: re.Match[str]) -> str:
        return f'<p class="keep-with-next">{match.group("content")}</p>{match.group("gap")}'

    return _IMAGE_INTRO_PARAGRAPH.sub(_mark, html)


def _embed_images(html: str, *, workspace: Path, source_dir: PurePosixPath) -> str:
    """Inline each workspace-relative <img src="..."> as a base64 data URI.

    Refuses a remote URL outright: this tool's capability declares no external effect, so it must
    never let a document's own markup turn one read into a network fetch.
    """
    embedded_bytes = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal embedded_bytes
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        if not _is_workspace_relative_resource(src):
            raise ToolExecutionError(
                f"export_pdf only embeds workspace-relative images; refusing remote src {src!r}"
            )
        parsed = urlsplit(src)
        relative = _resolve_workspace_resource(source_dir, parsed.path)
        image_path = contained_path(workspace, relative)
        try:
            image_size = image_path.stat().st_size
            if image_size > _MAX_EMBEDDED_IMAGE_BYTES:
                raise ToolExecutionError(
                    f"export_pdf: embedded image {src!r} exceeds {_MAX_EMBEDDED_IMAGE_BYTES} bytes"
                )
            with image_path.open("rb") as handle:
                data = handle.read(_MAX_EMBEDDED_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ToolExecutionError(
                f"export_pdf: embedded image {src!r} could not be read: {exc}"
            ) from exc
        encoded_size = ((len(data) + 2) // 3) * 4
        embedded_bytes += encoded_size
        if embedded_bytes > _MAX_EMBEDDED_TOTAL_BYTES:
            raise ToolExecutionError(
                "export_pdf: embedded images exceed the pre-render budget of "
                f"{_MAX_EMBEDDED_TOTAL_BYTES} bytes"
            )
        media_type = infer_artifact_media_type(image_path.name)
        if media_type not in {"image/png", "image/jpeg"}:
            raise ToolExecutionError(f"export_pdf: embedded image {src!r} must be a PNG or JPEG")
        try:
            validate_artifact_bytes(src, data, media_type)
        except ValueError as exc:
            raise ToolExecutionError(f"export_pdf: {exc}") from exc
        encoded = base64.b64encode(data).decode("ascii")
        return f"{prefix}data:{media_type};base64,{encoded}{suffix}"

    return _IMG_TAG.sub(_replace, html)


def _resolve_workspace_resource(source_dir: PurePosixPath, resource: str) -> str:
    """Resolve a document-relative resource lexically without permitting a root escape."""
    resolved_parts = list(source_dir.parts)
    for part in PurePosixPath(resource).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved_parts:
                raise ToolExecutionError(
                    f"export_pdf image source {resource!r} escapes the workspace"
                )
            resolved_parts.pop()
            continue
        resolved_parts.append(part)
    return PurePosixPath(*resolved_parts).as_posix()


def _source_artifact_name(output_name: str) -> str:
    """Return the stable audit-sidecar name for one PDF output name."""
    return PurePosixPath(output_name).with_suffix(".source.md").as_posix()


def _resolve_pdf_output(root: Path, requested: object) -> tuple[str, Path]:
    """Return one contained output path and its canonical workspace-relative artifact name."""
    requested_name = str(requested).replace("\\", "/")
    if not requested_name.casefold().endswith(".pdf"):
        raise ToolExecutionError("export_pdf output path must end in .pdf")
    output_path = contained_path(root, requested_name)
    return output_path.relative_to(root).as_posix(), output_path


def _execution_key(
    *, source_name: str, output_name: str, raw_source: bytes, encoded_pdf: bytes
) -> str:
    """Bind the persisted PDF bytes to the exact persisted Markdown bytes and output identity."""
    return hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool=_EXPORT_TOOL_NAME,
        source_name=source_name,
        source_sha256=sha256_bytes(raw_source),
        output_name=output_name,
        output_sha256=sha256_bytes(encoded_pdf),
    )


def _publish_pdf_artifacts(
    invocation: ToolInvocation,
    *,
    output_name: str,
    raw_source: bytes,
    encoded_pdf: bytes,
    execution_key: str,
) -> tuple[str, str]:
    """Atomically publish the auditable Markdown source and its derived PDF."""
    source_name = _source_artifact_name(output_name)
    source_artifact, pdf_artifact = invocation.artifact_store.save_artifact_batch(
        (
            ArtifactWrite(
                name=source_name,
                data=raw_source,
                kind=ArtifactKind.REPORT,
                media_type="text/markdown",
            ),
            ArtifactWrite(
                name=output_name,
                data=encoded_pdf,
                kind=ArtifactKind.REPORT,
                media_type="application/pdf",
                input_artifact_names=(source_name,),
                execution_key=execution_key,
            ),
        ),
        produced_by=invocation.agent_id,
    )
    return source_artifact.id, pdf_artifact.id


@dataclass(frozen=True, slots=True)
class ExportPdf:
    """Render a workspace Markdown file, and the images it embeds, into a PDF deliverable."""

    name: str = "export_pdf"
    description: str = (
        "Render a Markdown file already in the workspace -- headings, tables, and any image it "
        "embeds via a workspace-relative path (![alt](plots/x.png)) -- into a styled PDF, and "
        'always register the result as a kind="report" Artifact. Write the Markdown with '
        "write_file first; export_pdf reads it back, so use this once the report's text and its "
        "referenced plots already exist in the workspace. If the report cites any decimal or "
        "percentage figure, evidence_paths naming every JSON file that records it is required: "
        "export_pdf refuses immediately, before rendering, when such a citation is present and "
        "evidence_paths is empty, or when a cited number resolves to none of the named files -- "
        "the same check MIRA's audit would otherwise apply only after the whole Run finishes."
    )
    arguments_model: type[BaseModel] = ExportPdfArguments
    result_model: type[BaseModel] = PdfExportValue
    capability: ToolCapability = field(default_factory=_export_pdf_capability)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Convert the declared Markdown source, and its images, into a registered PDF artifact."""
        root = Path(invocation.workspace).resolve()
        source_name = str(arguments["source_path"]).replace("\\", "/")
        source_path = contained_path(root, source_name)
        try:
            if source_path.stat().st_size > _MAX_SOURCE_BYTES:
                raise ToolExecutionError(f"source exceeds {_MAX_SOURCE_BYTES} bytes")
            with source_path.open("rb") as handle:
                raw_source = handle.read(_MAX_SOURCE_BYTES + 1)
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {arguments['source_path']!r}: {exc}") from exc
        if len(raw_source) > _MAX_SOURCE_BYTES:
            raise ToolExecutionError(f"source exceeds {_MAX_SOURCE_BYTES} bytes")
        try:
            source_text = raw_source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                f"{arguments['source_path']!r} is not valid UTF-8: {exc}"
            ) from exc
        if source_text.count("\n") + 1 > _MAX_SOURCE_LINES:
            raise ToolExecutionError(f"source exceeds {_MAX_SOURCE_LINES} lines")
        _reject_unsupported_citations(
            source_text, invocation, arguments.get("evidence_paths") or ()
        )

        body_html = _markdown_lib.markdown(
            source_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"]
        )
        if body_html.count("<") > _MAX_BODY_TAGS:
            raise ToolExecutionError(f"rendered body exceeds {_MAX_BODY_TAGS} HTML tags")
        _reject_unsafe_html(body_html)
        source_dir = PurePosixPath(source_name).parent
        body_html = _keep_image_intro_with_figure(body_html)
        body_html = _embed_images(body_html, workspace=root, source_dir=source_dir)
        body_html = _zebra_stripe(body_html)
        body_html = _render_unordered_lists_as_ascii(body_html)
        full_html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8" />'
            f"<style>{_CSS}</style></head><body>{body_html}</body></html>"
        )
        if len(full_html.encode("utf-8")) > _MAX_RENDERED_HTML_BYTES:
            raise ToolExecutionError(
                f"export_pdf: rendered HTML exceeds {_MAX_RENDERED_HTML_BYTES} bytes"
            )

        output_name, output_path = _resolve_pdf_output(root, arguments["path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        buffer = _BoundedPdfBuffer()
        pdf_result = pisa.CreatePDF(
            full_html,
            dest=buffer,
            encoding="utf-8",
            link_callback=_resource_link_callback,
        )
        if pdf_result.err:
            raise ToolExecutionError(f"export_pdf: xhtml2pdf reported {pdf_result.err} error(s)")
        encoded = buffer.getvalue()
        if len(encoded) > _MAX_PDF_BYTES:
            raise ToolExecutionError(f"rendered PDF exceeds {_MAX_PDF_BYTES} bytes")
        try:
            validate_artifact_bytes(output_name, encoded, "application/pdf")
        except ValueError as exc:
            raise ToolExecutionError(f"export_pdf: {exc}") from exc
        output_path.write_bytes(encoded)
        page_count = len(PdfReader(BytesIO(encoded)).pages)

        execution_key = _execution_key(
            source_name=_source_artifact_name(output_name),
            output_name=output_name,
            raw_source=raw_source,
            encoded_pdf=encoded,
        )
        source_artifact_id, pdf_artifact_id = _publish_pdf_artifacts(
            invocation,
            output_name=output_name,
            raw_source=raw_source,
            encoded_pdf=encoded,
            execution_key=execution_key,
        )

        text = json.dumps(
            {"path": arguments["path"], "bytes": len(encoded), "pages": page_count},
            sort_keys=True,
        )
        return ToolResult(
            success=True,
            stdout=text,
            value=PdfExportValue(
                text=text,
                path=output_name,
                input_source_path=source_name,
                source_path=_source_artifact_name(output_name),
                format_version=PDF_EXPORT_EXECUTION_VERSION,
                bytes_written=len(encoded),
                page_count=page_count,
                artifact_id=pdf_artifact_id,
            ),
            artifact_ids=(source_artifact_id, pdf_artifact_id),
        )


__all__ = ["ExportPdf", "ExportPdfArguments", "PdfExportValue"]
