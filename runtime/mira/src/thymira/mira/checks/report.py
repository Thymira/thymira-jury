"""Recomputation of audit control A18: report facts match the source artifacts.

A18 independently recomputes versioned dataset profiles from their registered schema and dataset
bytes. Ordinary analytical reports retain the inherited meaning at
``docs/legacy/trazabilidad.md:118``: every quantitative claim must resolve to an Experiment or a
recorded metrics artifact, and every cited artifact must be present in the manifest.

PDF REPORT artifacts are audited through exactly one active Markdown REPORT lineage input and a
canonical producer execution key. Unreadable report text is a failure, never an empty report.
Structured JSON and CSV REPORT artifacts must have consistent media metadata and readable bounded
content, but their machine fields are not prose claims; versioned profile JSON retains its
independent source recomputation.

A quantitative claim is a signed decimal, percentage or percentage-point literal; bare integers
(counts, years) are not treated as claims. Markdown section numbers are document structure rather
than data claims. A cited artifact is a path-shaped token (it contains a separator) whose extension
is a known artifact extension. An ordinary report without experiment or metrics evidence is
``NOT_APPLICABLE``; a profile candidate is always checked.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from thymira.events import PDF_EXPORT_EXECUTION_VERSION, hash_pdf_export_execution
from thymira.mira.checks.models import ControlStatus
from thymira.mira.checks.profile import verify_profile_report
from thymira.schemas import ActorKind, ArtifactKind, EventType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Artifact, Event
    from thymira.state import ArtifactStore

_CLAIM_RE = re.compile(
    r"(?<![\w.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:e[+-]?\d+)?)(%|pp)?",
    re.IGNORECASE,
)
"""A signed decimal, grouped decimal or scientific literal outside an identifier."""

_REFERENCE_RE = re.compile(r"(?<![\w./-])(?:\.\.?/)*[A-Za-z0-9_][\w./-]*\.[A-Za-z0-9]+")
"""A path- or file-shaped token ending in an extension."""

_ARTIFACT_EXTENSIONS = frozenset(
    {
        "json",
        "csv",
        "joblib",
        "pkl",
        "npy",
        "npz",
        "parquet",
        "png",
        "jpg",
        "jpeg",
        "svg",
        "md",
        "txt",
        "html",
        "pdf",
        "yaml",
        "yml",
    }
)
"""Extensions that mark a cited token as an artifact reference rather than prose."""

_TOLERANCE = 1e-9
"""Absolute tolerance for a claim matching a recorded value exactly."""

_MAX_DECIMALS = 6
"""Cap on the rounding precision used to resolve a claim against a recorded value."""

_MAX_ABS_EXPONENT = 308
"""Largest scientific exponent A18 evaluates without overflowing a finite binary64 value."""

_MAX_MARKDOWN_REPORT_BYTES = 16 * 1024 * 1024
_MAX_PDF_REPORT_BYTES = 15 * 1024 * 1024
_MARKDOWN_MEDIA_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_NARRATIVE_MEDIA_BY_SUFFIX = {
    ".md": _MARKDOWN_MEDIA_TYPES,
    ".markdown": _MARKDOWN_MEDIA_TYPES,
    ".txt": frozenset({"text/plain"}),
}
_STRUCTURED_MEDIA_BY_SUFFIX = {
    ".csv": frozenset({"text/csv"}),
    ".json": frozenset({"application/json"}),
}
_PDF_EXPORT_TOOL = "export_pdf"
_PDF_EXPORT_VALUE_FIELDS = frozenset(
    {
        "artifact_id",
        "bytes_written",
        "format_version",
        "input_source_path",
        "page_count",
        "path",
        "source_path",
        "text",
    }
)


class ReportEvidenceError(ValueError):
    """A report or its declared audit source cannot be verified."""


def check_report_fidelity(
    store: ArtifactStore | None, events: Sequence[Event]
) -> tuple[ControlStatus, str]:
    """Recompute that every report claim resolves and every cited artifact exists."""
    if store is None:
        return ControlStatus.NOT_APPLICABLE, "no artifact store"
    reports = [artifact for artifact in store.list_active() if artifact.kind is ArtifactKind.REPORT]
    if not reports:
        return ControlStatus.NOT_APPLICABLE, "no report artifact"
    ordinary_reports, linked_text, profile_details, problems = _classify_reports(
        store, reports, events
    )
    source_events = [event for event in events if event.type is EventType.EXPERIMENT_COMPLETED]
    source_artifacts = [
        artifact for artifact in store.list_active() if artifact.kind is ArtifactKind.METRICS
    ]
    has_recorded_evidence = bool(source_events or source_artifacts)
    recorded = _recorded_numbers(store, events) if has_recorded_evidence else set()
    for report in ordinary_reports:
        text = linked_text.get(report.id)
        if text is None:
            text = _load_text(store, report.name)
        if text is None:
            problems.append(f"report {report.name!r} is not readable UTF-8 text")
            continue
        if has_recorded_evidence:
            problems.extend(_reference_problems(store, report, text))
            problems.extend(_claim_problems(text, recorded))
    if problems:
        return ControlStatus.FAILED, "; ".join(problems)
    if not ordinary_reports and not profile_details:
        return (
            ControlStatus.NOT_APPLICABLE,
            "no narrative Markdown/plain report or dataset profile",
        )
    if ordinary_reports and not has_recorded_evidence:
        verified = f"; verified profiles: {'; '.join(profile_details)}" if profile_details else ""
        return (
            ControlStatus.NOT_APPLICABLE,
            f"ordinary reports have no experiment.completed event or METRICS artifact{verified}",
        )
    detail = (
        "; ".join(profile_details)
        if profile_details
        else f"{len(ordinary_reports)} report(s): every quantitative claim resolves"
    )
    return ControlStatus.PASSED, detail


def _classify_reports(
    store: ArtifactStore, reports: list[Artifact], events: Sequence[Event]
) -> tuple[list[Artifact], dict[str, str], list[str], list[str]]:
    """Classify profiles and resolve each PDF to one auditable Markdown source."""
    ordinary_candidates: list[Artifact] = []
    pdf_reports: list[Artifact] = []
    profile_details: list[str] = []
    problems: list[str] = []
    for report in reports:
        try:
            family = _report_family(report)
        except ReportEvidenceError as exc:
            problems.append(str(exc))
            continue
        if family == "pdf":
            pdf_reports.append(report)
            continue
        if family == "narrative":
            ordinary_candidates.append(report)
            continue
        if family == "json":
            verification = verify_profile_report(store, report)
            if verification.candidate:
                if verification.passed:
                    profile_details.append(verification.detail)
                else:
                    problems.append(verification.detail)
                continue
        try:
            _verify_structured_report(store, report, family)
        except ReportEvidenceError as exc:
            problems.append(str(exc))

    active_by_id = {artifact.id: artifact for artifact in store.list_active()}
    linked_sources: dict[str, Artifact] = {}
    linked_text: dict[str, str] = {}
    for pdf_report in pdf_reports:
        try:
            source, text = _pdf_markdown_source(store, pdf_report, active_by_id, events)
        except ReportEvidenceError as exc:
            problems.append(str(exc))
        else:
            linked_sources[source.id] = source
            linked_text[source.id] = text

    ordinary_by_id = {
        report.id: report for report in ordinary_candidates if report.id not in linked_sources
    }
    ordinary_by_id.update(linked_sources)
    return list(ordinary_by_id.values()), linked_text, profile_details, problems


def _report_family(report: Artifact) -> str:
    """Classify one REPORT from a consistent filename extension and declared MIME type."""
    suffix = PurePosixPath(report.name).suffix.casefold()
    media_type = _base_media_type(report)
    if report.name.startswith("profile/") and suffix != ".json":
        raise ReportEvidenceError(
            f"reserved profile REPORT {report.name!r} must use a .json extension "
            "and application/json media type"
        )
    if suffix == ".pdf" or media_type == "application/pdf":
        if suffix != ".pdf" or media_type != "application/pdf":
            raise ReportEvidenceError(
                f"REPORT {report.name!r} has inconsistent extension {suffix!r} "
                f"and media type {media_type or '<unspecified>'!r}"
            )
        return "pdf"
    narrative_media = _NARRATIVE_MEDIA_BY_SUFFIX.get(suffix)
    if narrative_media is not None:
        if media_type and media_type not in narrative_media:
            raise ReportEvidenceError(
                f"REPORT {report.name!r} has inconsistent extension {suffix!r} "
                f"and media type {media_type!r}"
            )
        return "narrative"
    structured_media = _STRUCTURED_MEDIA_BY_SUFFIX.get(suffix)
    if structured_media is not None:
        if media_type and media_type not in structured_media:
            raise ReportEvidenceError(
                f"REPORT {report.name!r} has inconsistent extension {suffix!r} "
                f"and media type {media_type!r}"
            )
        return suffix.removeprefix(".")
    raise ReportEvidenceError(
        f"REPORT {report.name!r} has unsupported extension {suffix or '<none>'!r} "
        f"and media type {media_type or '<unspecified>'!r}"
    )


def _verify_structured_report(store: ArtifactStore, report: Artifact, family: str) -> None:
    """Verify bounded UTF-8 structured bytes without treating their values as prose claims."""
    subject = f"structured {family.upper()} REPORT {report.name!r}"
    text = _verified_utf8_text(store, report, subject=subject)
    if family != "json":
        return
    try:
        json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ReportEvidenceError(f"{subject} is not valid JSON: {exc}") from exc


def _pdf_markdown_source(
    store: ArtifactStore,
    pdf_report: Artifact,
    active_by_id: dict[str, Artifact],
    events: Sequence[Event],
) -> tuple[Artifact, str]:
    """Resolve and verify the sole auditable Markdown source of one PDF report."""
    prefix = f"PDF report {pdf_report.name!r}"
    if _base_media_type(pdf_report) != "application/pdf":
        raise ReportEvidenceError(f"{prefix} does not declare media type 'application/pdf'")
    if len(pdf_report.input_artifact_ids) != 1:
        raise ReportEvidenceError(
            f"{prefix} must have exactly one active REPORT Markdown source; "
            f"found {len(pdf_report.input_artifact_ids)} lineage input(s)"
        )
    source = active_by_id.get(pdf_report.input_artifact_ids[0])
    if source is None:
        raise ReportEvidenceError(f"{prefix} does not resolve to an active REPORT Markdown source")
    if source.run_id != pdf_report.run_id:
        raise ReportEvidenceError(f"{prefix} has a Markdown source from another Run")
    if source.kind is not ArtifactKind.REPORT:
        raise ReportEvidenceError(f"{prefix} source {source.name!r} is not a REPORT artifact")
    if _base_media_type(source) not in _MARKDOWN_MEDIA_TYPES or not source.name.casefold().endswith(
        (".md", ".markdown")
    ):
        raise ReportEvidenceError(f"{prefix} source {source.name!r} is not compatible Markdown")
    if pdf_report.execution_key is None:
        raise ReportEvidenceError(
            f"{prefix} has no canonical execution key for its Markdown source"
        )

    try:
        text = _verified_markdown_text(store, source)
    except ReportEvidenceError as exc:
        raise ReportEvidenceError(f"{prefix} {exc}") from exc
    pdf_sha256 = _verified_artifact_sha256(
        store,
        pdf_report,
        subject=prefix,
        limit=_MAX_PDF_REPORT_BYTES,
    )
    expected_key = hash_pdf_export_execution(
        version=PDF_EXPORT_EXECUTION_VERSION,
        tool=_PDF_EXPORT_TOOL,
        source_name=source.name,
        source_sha256=source.sha256,
        output_name=pdf_report.name,
        output_sha256=pdf_sha256,
    )
    if pdf_report.execution_key != expected_key:
        raise ReportEvidenceError(
            f"{prefix} execution key does not bind its verified Markdown/PDF pair"
        )
    if not _has_completed_pdf_export(events, source, pdf_report):
        raise ReportEvidenceError(
            f"{prefix} has no completed export_pdf event for its exact artifact pair"
        )
    return source, text


def _has_completed_pdf_export(
    events: Sequence[Event], source: Artifact, pdf_report: Artifact
) -> bool:
    """Return whether the Tool Manager recorded this exact export pair as successful."""
    for event in events:
        if (
            event.type is not EventType.TOOL_COMPLETED
            or event.actor.kind is not ActorKind.TOOL
            or event.actor.id != _PDF_EXPORT_TOOL
            or not event.actor.authenticated
            or event.payload.get("tool") != _PDF_EXPORT_TOOL
            or event.payload.get("status") != "COMPLETED"
            or event.payload.get("error") is not None
            or event.payload.get("artifact_ids") != [source.id, pdf_report.id]
        ):
            continue
        exit_code = event.payload.get("exit_code")
        if exit_code is not None and (isinstance(exit_code, bool) or exit_code != 0):
            continue
        tool_call_id = event.payload.get("tool_call_id")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or event.subject_id != tool_call_id
        ):
            continue
        envelope = event.payload.get("value")
        if not isinstance(envelope, dict) or set(envelope) != {"kind", "value"}:
            continue
        value = envelope.get("value")
        if envelope.get("kind") != "success" or not isinstance(value, dict):
            continue
        if set(value) != _PDF_EXPORT_VALUE_FIELDS:
            continue
        page_count = value.get("page_count")
        format_version = value.get("format_version")
        bytes_written = value.get("bytes_written")
        if (
            value.get("artifact_id") != pdf_report.id
            or value.get("path") != pdf_report.name
            or value.get("source_path") != source.name
            or not isinstance(value.get("input_source_path"), str)
            or not value["input_source_path"]
            or not isinstance(value.get("text"), str)
            or format_version != PDF_EXPORT_EXECUTION_VERSION
            or isinstance(format_version, bool)
            or bytes_written != pdf_report.size_bytes
            or isinstance(bytes_written, bool)
            or not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count < 1
        ):
            continue
        return True
    return False


def _base_media_type(artifact: Artifact) -> str:
    """Return an artifact's normalized MIME base type."""
    return (artifact.media_type or "").partition(";")[0].strip().casefold()


def _verified_markdown_text(store: ArtifactStore, source: Artifact) -> str:
    """Load bounded UTF-8 Markdown only when its manifest identity still matches its bytes."""
    return _verified_utf8_text(store, source, subject=f"Markdown source {source.name!r}")


def _verified_utf8_text(store: ArtifactStore, artifact: Artifact, *, subject: str) -> str:
    """Load bounded UTF-8 text only when its bytes still match the manifest identity."""
    raw = _verified_artifact_bytes(
        store,
        artifact,
        subject=subject,
        limit=_MAX_MARKDOWN_REPORT_BYTES,
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportEvidenceError(f"{subject} is not valid UTF-8") from exc


def _verified_artifact_sha256(
    store: ArtifactStore, artifact: Artifact, *, subject: str, limit: int
) -> str:
    """Return the independently recomputed digest of one bounded manifest artifact."""
    raw = _verified_artifact_bytes(store, artifact, subject=subject, limit=limit)
    return hashlib.sha256(raw).hexdigest()


def _verified_artifact_bytes(
    store: ArtifactStore, artifact: Artifact, *, subject: str, limit: int
) -> bytes:
    """Load bounded bytes only when their size and digest match the active manifest record."""
    try:
        raw = store.load_bytes_bounded(artifact.name, limit)
    except (KeyError, OSError, ValueError) as exc:
        raise ReportEvidenceError(f"{subject} could not be read: {exc}") from exc
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ReportEvidenceError(f"{subject} does not match its manifest digest")
    return raw


def _reference_problems(store: ArtifactStore, report: Artifact, text: str) -> list[str]:
    """Report every cited artifact path that is absent from the manifest or dataset registry."""
    problems: list[str] = []
    reported: set[str] = set()
    registered_sources = _registered_source_paths(store)
    for match in _REFERENCE_RE.finditer(text):
        reference = match.group(0)
        if "/" not in reference:
            continue
        extension = reference.rsplit(".", 1)[-1].casefold()
        if extension not in _ARTIFACT_EXTENSIONS:
            continue
        if reference in reported:
            continue
        locations = _reference_locations(report.name, reference)
        if locations is None:
            reported.add(reference)
            problems.append(f"report cites artifact {reference!r} outside the artifact namespace")
            continue
        if report.name in locations:
            continue
        if not any(
            store.exists(location) or location in registered_sources for location in locations
        ):
            reported.add(reference)
            problems.append(f"report cites artifact {reference!r} absent from the manifest")
    return problems


def _reference_locations(report_name: str, reference: str) -> tuple[str, ...] | None:
    """Return safe root- and report-relative identities for one cited artifact.

    Existing reports conventionally cite manifest names from the artifact root. Markdown also
    permits paths relative to the document directory, so A18 accepts either interpretation when
    it resolves inside the artifact namespace. Parent traversal beyond that root fails closed.
    """
    reference_path = PurePosixPath(reference)
    base_parts = list(PurePosixPath(report_name).parent.parts)
    resolved_parts = base_parts.copy()
    for part in reference_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved_parts:
                return None
            resolved_parts.pop()
            continue
        resolved_parts.append(part)
    resolved = PurePosixPath(*resolved_parts).as_posix()
    literal = reference_path.as_posix()
    candidates = [literal] if ".." not in reference_path.parts else []
    if resolved not in candidates:
        candidates.append(resolved)
    return tuple(candidates)


def _registered_source_paths(store: ArtifactStore) -> set[str]:
    """Return workspace paths bound to registered dataset artifacts.

    Dataset registration keeps its logical artifact name separate from the project's physical
    workspace path. Reports may cite either identity; this reader deliberately consumes the
    schema JSON instead of importing the producer's dataset implementation, preserving MIRA's
    independence from THY and the tools layer.
    """
    paths: set[str] = set()
    for artifact in store.list_active():
        if artifact.kind is not ArtifactKind.OTHER or not artifact.name.endswith(".schema.json"):
            continue
        payload = _load_json(store, artifact.name)
        if isinstance(payload, dict):
            source_path = payload.get("source_path")
            if isinstance(source_path, str) and source_path:
                paths.add(source_path.replace("\\", "/"))
    return paths


def _claim_problems(text: str, recorded: set[float]) -> list[str]:
    """Report every decimal or percentage claim that no recorded value supports."""
    problems: list[str] = []
    reported: set[str] = set()
    for match in _CLAIM_RE.finditer(text):
        digits, unit = match.group(1), match.group(2)
        if "." not in digits and "e" not in digits.casefold() and not unit:
            continue
        if _is_section_number(text, match):
            continue
        token = match.group(0)
        if token in reported:
            continue
        if not _claim_resolves(digits, scaled=unit is not None, recorded=recorded):
            reported.add(token)
            problems.append(f"report cites {token!r}, which no recorded evidence supports")
    return problems


def _is_section_number(text: str, match: re.Match[str]) -> bool:
    """Whether a matched token numbers or references a Markdown section, not a claim.

    `### 3.1 Bivariate Analysis` numbers its own section; no recorded evidence will ever "support"
    that numbering, so treating it as a quantitative claim manufactures a false positive on every
    numbered heading a report writes -- reproduced against a real report's own `### 1.3 ...` and
    `### 2.1 ...` headings.
    """
    line_start = text.rfind("\n", 0, match.start()) + 1
    prefix = text[line_start : match.start()]
    if re.fullmatch(r"#{1,6}[ \t]+", prefix) is not None:
        return True
    return re.search(r"\bsection[ \t]+\Z", prefix, re.IGNORECASE) is not None


def _claim_resolves(digits: str, *, scaled: bool, recorded: set[float]) -> bool:
    """Whether a claim resolves under either numeric convention the recorded evidence may use.

    A percentage claim's own source value may be recorded as a 0-1 proportion (the printed
    literal divided by 100, e.g. a `default_rate` field) or already in percent-native form (the
    literal as printed, e.g. a `..._pct` field) -- both are legitimate, and the report gives no
    reliable signal for which one its own source used, so a claim resolves against either.
    Reproduced against a real report: `support_pct: 27.45` in the recorded evidence never matched
    a cited `27.45%` when only the divide-by-100 reading was tried.
    """
    normalized = digits.replace(",", "")
    parsed = _finite_claim(normalized)
    if parsed is None:
        return False
    raw_value, exponent = parsed
    mantissa = normalized.casefold().partition("e")[0]
    raw_decimals = len(mantissa.partition(".")[2])
    raw_tolerance = 0.5 * 10.0 ** (exponent - min(raw_decimals, _MAX_DECIMALS))
    fraction_value = raw_value / 100
    return _resolves(raw_value, raw_tolerance, recorded) or (
        scaled and _resolves(fraction_value, raw_tolerance / 100, recorded)
    )


def _finite_claim(normalized: str) -> tuple[float, int] | None:
    """Return one finite claim and bounded exponent, or ``None`` when it is unsafe."""
    _mantissa, exponent_marker, exponent_text = normalized.casefold().partition("e")
    exponent = _bounded_exponent(exponent_text) if exponent_marker else 0
    if exponent is None:
        return None
    try:
        value = float(normalized)
    except (OverflowError, ValueError):
        return None
    return (value, exponent) if math.isfinite(value) else None


def _bounded_exponent(text: str) -> int | None:
    """Parse a scientific exponent without constructing an attacker-sized integer or power."""
    unsigned = text.lstrip("+-").lstrip("0") or "0"
    if len(unsigned) > len(str(_MAX_ABS_EXPONENT)):
        return None
    exponent = int(text)
    return exponent if -_MAX_ABS_EXPONENT <= exponent <= _MAX_ABS_EXPONENT else None


def _resolves(value: float, rounding_half_step: float, recorded: set[float]) -> bool:
    """Whether a claim matches a recorded value exactly or at the claim's own precision."""
    for candidate in recorded:
        if abs(candidate - value) <= _TOLERANCE:
            return True
        if abs(candidate - value) <= rounding_half_step + _TOLERANCE:
            return True
    return False


def _recorded_numbers(store: ArtifactStore, events: Sequence[Event]) -> set[float]:
    """Every numeric value a run recorded: metrics, parameters, splits and metric files."""
    numbers: set[float] = set()
    for event in events:
        if event.type is EventType.MODEL_TRAINED:
            _collect_numbers(event.payload.get("split"), numbers)
        if event.type is EventType.EXPERIMENT_COMPLETED:
            experiment = event.payload.get("experiment")
            if isinstance(experiment, dict):
                _collect_numbers(experiment.get("metrics"), numbers)
                _collect_numbers(experiment.get("parameters"), numbers)
    for artifact in store.list_active():
        if artifact.kind is ArtifactKind.METRICS and artifact.name.endswith(".json"):
            _collect_numbers(_load_json(store, artifact.name), numbers)
    return numbers


def _collect_numbers(value: Any, into: set[float]) -> None:
    """Recursively gather every numeric leaf (excluding booleans) into ``into``."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        numeric = float(value)
        if math.isfinite(numeric):
            into.add(numeric)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_numbers(item, into)


def _load_json(store: ArtifactStore, name: str) -> Any:
    """Load an artifact as JSON, returning ``None`` when it is missing or not JSON."""
    try:
        value = store.load_json(name)
    except (OSError, ValueError):
        return None
    else:
        return value


def _load_text(store: ArtifactStore, name: str) -> str | None:
    """Load an artifact as UTF-8 text, returning ``None`` when it cannot be read."""
    try:
        text = store.load_text(name)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    else:
        return text


__all__ = ["check_report_fidelity"]
