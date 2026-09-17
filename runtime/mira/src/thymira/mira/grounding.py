"""Resolution of an agent-proposed evidence reference against the Run's own authoritative log.

A finding's evidence is the only thing that makes it checkable by a human, and it is the substance
of the assurance bundle's evidence index and traceability index (ASSUR-02). A model that authors a
finding also authors its ``evidence`` tuple, so a reference is a *claim* until code resolves it:
``LLM proposes, code authorizes`` applies to the citation exactly as it applies to the finding.

This module is that resolution. :class:`RunEvidenceIndex` folds the Run's events into what the log
can actually back -- which event sequences exist and with which hash, which artifacts were
announced and with which digest, which experiments and tool calls were recorded -- and
:func:`ground_finding` strips from a finding every reference the index cannot back, reporting what
it dropped so the drop is itself recorded evidence.

A stripped finding is *kept*, not suppressed. Dropping the finding would let a resolution failure
silently delete an observation that may well be true, and the Policy Engine already has a rule for
a serious finding that arrives without evidence. What must never happen is the opposite: a bundle
that verifies while citing an artifact the Run never produced.

``external`` references point outside the Run by construction, but a completed
``search_regulation`` event records each returned chunk's source, location and digest in its
canonical result envelope. The index accepts an external citation only when both its
``source_id/location`` reference and digest exactly match one of those validated chunks. Callers
that execute one audit task can restrict those chunks to that task's selected search event; merely
having consulted the knowledge base elsewhere in the Run cannot back a model-authored citation.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from thymira.mira.finding_safety import safe_finding
from thymira.schemas import EventType, Framework

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import AuditFinding, Event, Evidence

_ARTIFACT_ID_KEYS = ("artifact_id", "id")
"""Where an ``artifact.created`` payload may carry the artifact's own id, tried in order."""

_TOOL_CALL_ID_KEYS = ("tool_call_id", "id")
"""Where a tool lifecycle payload may carry the call id, tried in order."""

_EXPERIMENT_ID_KEY = "experiment_id"
"""Where an experiment lifecycle payload carries its id directly."""

_REGULATION_SEARCH_TOOL = "search_regulation"
"""The one MIRA tool that retrieves citable knowledge-base text (``thymira.mira.tools``).

The producer stays unnamed in the dependency graph; grounding reads only its typed canonical
result contract and never executes the tool.
"""

_COMPLETED = "COMPLETED"
"""``ToolCallStatus.COMPLETED`` as it is written onto a ``tool.completed`` payload."""

_SHA256_HEX_LENGTH = 64
"""Exact lowercase hexadecimal length of a recorded SHA-256 digest."""

_REGULATION_VALUE_FIELDS = frozenset({"text", "result_count", "matches"})
"""Exact fields admitted by the persisted ``RegulationSearchValue`` contract."""

_REGULATION_MATCH_FIELDS = frozenset(
    {
        "source_id",
        "version",
        "framework",
        "location",
        "fragment",
        "sha256",
        "score",
        "backend",
    }
)
"""Exact fields admitted by one persisted regulation match."""


@dataclass(frozen=True, slots=True)
class RegulationEvidenceMatch:
    """Validated citation fields projected from one canonical regulation match."""

    source_id: str
    framework: Framework
    location: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RegulationSearchEvidence:
    """Validated citation projection of one canonical regulation search value."""

    matches: tuple[RegulationEvidenceMatch, ...]


@dataclass(frozen=True, slots=True)
class RunEvidenceIndex:
    """What one Run's authoritative event log can back an evidence reference with.

    Artifact digests are stored as ``None`` when the log announced no digest. External references
    are indexed only as exact reference-and-digest pairs from canonical regulation results. A
    reference that asserts a digest the log cannot confirm is not backed: an unconfirmable digest
    is precisely the claim this index exists to refuse.
    """

    event_hashes: Mapping[str, str | None]
    artifact_digests: Mapping[str, str | None]
    experiment_ids: frozenset[str]
    tool_call_ids: frozenset[str]
    external_digests: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_events(
        cls,
        events: Sequence[Event],
        *,
        regulation_events: Sequence[Event] | None = None,
    ) -> Self:
        """Fold a Run's events into the references its log can back.

        Args:
            events: Complete authoritative Run history for Run-owned evidence.
            regulation_events: Optional resolved search events allowed to back external citations.
                ``None`` preserves the whole-Run index used outside a scoped audit-agent call.
        """
        event_hashes = {f"seq:{event.seq}": event.hash for event in events}
        artifact_digests: dict[str, str | None] = {}
        experiment_ids: set[str] = set()
        tool_call_ids: set[str] = set()
        external_digests: dict[str, set[str]] = {}
        for event in events:
            if event.type is EventType.ARTIFACT_CREATED:
                _index_artifact(event, artifact_digests)
            elif event.type in (EventType.EXPERIMENT_STARTED, EventType.EXPERIMENT_COMPLETED):
                experiment_ids.update(_experiment_ids(event))
            elif event.type in (
                EventType.TOOL_STARTED,
                EventType.TOOL_COMPLETED,
                EventType.TOOL_DENIED,
            ):
                tool_call_ids.update(_identifiers(event, _TOOL_CALL_ID_KEYS))
        for event in events if regulation_events is None else regulation_events:
            _index_regulation_matches(event, external_digests)
        return cls(
            event_hashes=event_hashes,
            artifact_digests=artifact_digests,
            experiment_ids=frozenset(experiment_ids),
            tool_call_ids=frozenset(tool_call_ids),
            external_digests={
                reference: frozenset(digests) for reference, digests in external_digests.items()
            },
        )

    def backs(self, evidence: Evidence) -> bool:
        """Whether the Run's log can back this exact reference, digest included."""
        if evidence.kind == "event":
            return _digest_agrees(self.event_hashes, evidence)
        if evidence.kind == "artifact":
            return _digest_agrees(self.artifact_digests, evidence)
        if evidence.kind == "experiment":
            return evidence.ref in self.experiment_ids and evidence.sha256 is None
        if evidence.kind == "tool_call":
            return evidence.ref in self.tool_call_ids and evidence.sha256 is None
        if evidence.kind == "external":
            return evidence.sha256 is not None and evidence.sha256 in self.external_digests.get(
                evidence.ref, frozenset()
            )
        return False


@dataclass(frozen=True, slots=True)
class GroundedFinding:
    """One finding reduced to the evidence its Run can back, and what that cost it."""

    finding: AuditFinding
    unbacked: tuple[Evidence, ...]

    @property
    def changed(self) -> bool:
        """Whether any reference was dropped from the proposed finding."""
        return bool(self.unbacked)


def ground_finding(finding: AuditFinding, index: RunEvidenceIndex) -> GroundedFinding:
    """Strip every reference the Run cannot back, keeping the finding itself.

    Args:
        finding: The candidate finding, whose evidence a model may have authored.
        index: What the Run's authoritative log can back.

    Returns:
        The finding carrying only backed references, plus the references that were dropped.
    """
    safe = safe_finding(finding)
    verdicts = [(evidence, index.backs(evidence)) for evidence in safe.evidence]
    backed = tuple(evidence for evidence, ok in verdicts if ok)
    unbacked = tuple(evidence for evidence, ok in verdicts if not ok)
    if not unbacked:
        return GroundedFinding(finding=safe, unbacked=())
    return GroundedFinding(finding=safe.model_copy(update={"evidence": backed}), unbacked=unbacked)


def ground_findings(
    findings: Sequence[AuditFinding], index: RunEvidenceIndex
) -> tuple[tuple[AuditFinding, ...], tuple[Evidence, ...]]:
    """Ground every finding, returning them and every reference dropped across them all."""
    grounded = tuple(ground_finding(finding, index) for finding in findings)
    dropped = tuple(evidence for result in grounded for evidence in result.unbacked)
    return tuple(result.finding for result in grounded), dropped


def unbacked_summary(unbacked: Sequence[Evidence]) -> list[dict[str, str]]:
    """Render dropped references as a compact, payload-safe record of what was refused."""
    return [{"kind": evidence.kind, "ref": evidence.ref} for evidence in unbacked]


def _is_completed_search(event: Event) -> bool:
    """Whether one tool event is a completed regulation search recorded by the Tool Manager."""
    exit_code = event.payload.get("exit_code")
    return (
        event.type is EventType.TOOL_COMPLETED
        and event.payload.get("tool") == _REGULATION_SEARCH_TOOL
        and event.payload.get("status") == _COMPLETED
        and (exit_code is None or exit_code == 0)
    )


def regulation_search_result(
    event: Event,
) -> tuple[RegulationSearchEvidence | None, str | None]:
    """Return one canonical completed search result and a safe failure reason."""
    if not _is_completed_search(event):
        return None, "regulation search did not complete successfully"
    return _parse_regulation_search_envelope(event.payload.get("value"))


def _parse_regulation_search_envelope(
    envelope: object,
) -> tuple[RegulationSearchEvidence | None, str | None]:
    """Validate the exact persisted success envelope without importing its tool producer."""
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"kind", "value"}
        or envelope.get("kind") != "success"
    ):
        return None, "canonical regulation result envelope is invalid"
    value = envelope.get("value")
    if (
        not isinstance(value, Mapping)
        or "result_count" not in value
        or not set(value).issubset(_REGULATION_VALUE_FIELDS)
    ):
        error = (
            "canonical regulation result value is not an object"
            if not isinstance(value, Mapping)
            else "regulation result matches are incomplete or malformed"
        )
        return None, error
    text = value.get("text", "")
    result_count = value.get("result_count")
    raw_matches = value.get("matches", [])
    if (
        not isinstance(text, str)
        or not isinstance(result_count, int)
        or isinstance(result_count, bool)
        or result_count < 0
        or not isinstance(raw_matches, list)
    ):
        return None, "regulation result matches are incomplete or malformed"
    matches: list[RegulationEvidenceMatch] = []
    for raw_match in raw_matches:
        match = _regulation_match(raw_match)
        if match is None:
            return None, "regulation result matches are incomplete or malformed"
        matches.append(match)
    if result_count != len(matches):
        return None, "regulation result_count does not match the complete match set"
    return RegulationSearchEvidence(matches=tuple(matches)), None


def _regulation_match(payload: object) -> RegulationEvidenceMatch | None:
    """Validate one exact persisted match and retain only its citation authority fields."""
    if not isinstance(payload, Mapping) or set(payload) != _REGULATION_MATCH_FIELDS:
        return None
    source_id = payload.get("source_id")
    version = payload.get("version")
    raw_framework = payload.get("framework")
    location = payload.get("location")
    fragment = payload.get("fragment")
    sha256 = payload.get("sha256")
    score = payload.get("score")
    backend = payload.get("backend")
    if (
        not isinstance(source_id, str)
        or not source_id
        or not isinstance(version, str)
        or not version
        or not isinstance(raw_framework, str)
        or not isinstance(location, str)
        or not location
        or not isinstance(fragment, str)
        or not fragment
        or not isinstance(sha256, str)
        or len(sha256) != _SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in sha256)
        or hashlib.sha256(fragment.encode("utf-8")).hexdigest() != sha256
        or not isinstance(score, (int, float))
        or isinstance(score, bool)
        or (isinstance(score, float) and not math.isfinite(score))
        or score < 0
        or not isinstance(backend, str)
        or not backend
    ):
        return None
    try:
        framework = Framework(raw_framework)
    except ValueError:
        return None
    return RegulationEvidenceMatch(
        source_id=source_id,
        framework=framework,
        location=location,
        sha256=sha256,
    )


def _index_regulation_matches(event: Event, digests: dict[str, set[str]]) -> None:
    """Index each exact external reference and digest from a canonical completed search."""
    result, _ = regulation_search_result(event)
    if result is None:
        return
    for match in result.matches:
        reference = f"{match.source_id}/{match.location}"
        digests.setdefault(reference, set()).add(match.sha256)


def _index_artifact(event: Event, digests: dict[str, str | None]) -> None:
    """Record every reference an ``artifact.created`` event announces, under its digest."""
    declared = event.payload.get("sha256")
    digest = declared if isinstance(declared, str) and declared else None
    name = event.payload.get("name")
    if isinstance(name, str) and name:
        digests.setdefault(name, digest)
    for identifier in _identifiers(event, _ARTIFACT_ID_KEYS):
        digests.setdefault(identifier, digest)


def _experiment_ids(event: Event) -> tuple[str, ...]:
    """Return the experiment ids one lifecycle event records, directly or nested."""
    identifiers = list(_identifiers(event, (_EXPERIMENT_ID_KEY,)))
    experiment = event.payload.get("experiment")
    if isinstance(experiment, dict):
        nested = experiment.get("id")
        if isinstance(nested, str) and nested:
            identifiers.append(nested)
    return tuple(identifiers)


def _identifiers(event: Event, keys: Sequence[str]) -> tuple[str, ...]:
    """Return the non-empty string identifiers an event carries, payload keys and subject alike."""
    found = [value for key in keys if isinstance(value := event.payload.get(key), str) and value]
    subject = event.subject_id
    if isinstance(subject, str) and subject:
        found.append(subject)
    return tuple(found)


def _digest_agrees(known: Mapping[str, str | None], evidence: Evidence) -> bool:
    """Whether a reference exists and its asserted digest is one the log confirms."""
    if evidence.ref not in known:
        return False
    if evidence.sha256 is None:
        return True
    return known[evidence.ref] == evidence.sha256


__all__ = [
    "GroundedFinding",
    "RegulationEvidenceMatch",
    "RegulationSearchEvidence",
    "RunEvidenceIndex",
    "ground_finding",
    "ground_findings",
    "regulation_search_result",
    "unbacked_summary",
]
