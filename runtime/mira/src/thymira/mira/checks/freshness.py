"""The passive audit-freshness control A25 (CTRL-AUDIT-FRESHNESS).

An :class:`AuditReport` is a snapshot: it concludes about the run as it stood at one point in the
hash-chained event history, pinned by ``terminal_hash``. This control locates that snapshot in a
supplied history and asks whether anything happened afterwards that the audit could not have seen.
It FAILS when later *material execution* or activity-context evidence exists, when a referenced
artifact was later invalidated, when the artifact manifest changed, or when the persisted graph,
policy, or activity-profile snapshot differs from the current one. It is ``NOT_APPLICABLE`` when
the report carries no terminal hash or the supplied history cannot contain that snapshot.

It is deliberately passive: the result is MIRA evidence only. It neither reopens a Run, schedules an
audit, writes an event, nor calls the Gate -- a future invalidation or re-audit scheduler consumes
this same evidence without changing the control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.mira.checks.models import ControlResult, ControlStatus
from thymira.mira.evidence import artifact_manifest_sha256
from thymira.schemas import EventType, Evidence, Severity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.mira.checks.models import AuditReport
    from thymira.schemas import Event
    from thymira.state import ArtifactStore

_CONTROL_ID = "A25"
_TITLE = "Audit snapshot freshness"

_MATERIAL_EXECUTION = frozenset(
    {
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_DENIED,
        EventType.MODEL_SELECTED,
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
        EventType.MODEL_TRAINED,
        EventType.EXPERIMENT_STARTED,
        EventType.EXPERIMENT_COMPLETED,
        EventType.ARTIFACT_CREATED,
    }
)
"""Event types that mean the run did more analytical work after the audited snapshot."""

_MATERIAL_CONTEXT = frozenset(
    {
        EventType.ACTIVITY_PROFILE_RECORDED,
        EventType.ACTIVITY_PROFILE_QUESTIONED,
        EventType.ACTIVITY_PROFILE_ANSWERED,
        EventType.RISK_ASSESSMENT_RECORDED,
        EventType.RISK_CLASSIFIED,
    }
)
"""Event types that change the activity or risk context used by the audit."""

_ARTIFACT_NAME_KEYS = ("name", "artifact", "artifact_name")
"""Where an ``artifact.invalidated`` payload may name the affected artifact."""

_MAX_LISTED = 5
"""How many later events to attach as evidence before truncating."""

_SHA256 = frozenset("0123456789abcdef")
"""The lowercase hex alphabet a valid digest is drawn from."""

_SHA256_LENGTH = 64
"""The character length of a sha256 hex digest."""


def assess_audit_freshness(
    report: AuditReport,
    events: Sequence[Event],
    *,
    store: ArtifactStore | None = None,
    current_graph_definition_hash: str | None = None,
    current_policy_sha256: str | None = None,
    current_activity_profile_id: str | None = None,
    current_activity_profile_version: int | None = None,
) -> ControlResult:
    """Assess whether an audit snapshot is still current against its current dependencies.

    The optional current values make version and context invalidation explicit while preserving
    A25's evidence-only contract. Callers that need to enforce the result must pass it through
    Core's Gate; this function never writes an event or changes a Run.
    """
    if report.terminal_hash is None:
        return _result(ControlStatus.NOT_APPLICABLE, "the report records no terminal hash", ())
    index = _snapshot_index(events, report.terminal_hash)
    if index is None:
        return _result(
            ControlStatus.NOT_APPLICABLE,
            "the supplied history does not contain the audited snapshot",
            (),
        )
    later = events[index + 1 :]
    referenced = _referenced_artifacts(events[: index + 1])
    reasons: list[str] = []
    evidence: list[Evidence] = []

    _append_snapshot_mismatches(
        report,
        reasons,
        current_graph_definition_hash=current_graph_definition_hash,
        current_policy_sha256=current_policy_sha256,
        current_activity_profile_id=current_activity_profile_id,
        current_activity_profile_version=current_activity_profile_version,
    )

    material = [
        event
        for event in later
        if event.type in _MATERIAL_EXECUTION or event.type in _MATERIAL_CONTEXT
    ]
    if material:
        reasons.append(f"{len(material)} later material or context event(s) after the snapshot")
        evidence.extend(_event_evidence(event) for event in material[:_MAX_LISTED])

    invalidations = _referenced_invalidations(later, referenced)
    if invalidations:
        names = sorted({name for _, name in invalidations})
        reasons.append(f"artifact(s) referenced by the snapshot later invalidated: {names}")
        for event, name in invalidations:
            evidence.append(_event_evidence(event))
            evidence.append(_artifact_evidence(name, referenced.get(name), store))

    if store is not None:
        try:
            artifact_problems = store.verify()
        except (OSError, ValueError) as exc:
            reasons.append(f"artifact manifest could not be verified: {exc}")
        else:
            if artifact_problems:
                reasons.append(
                    "artifact manifest verification failed: "
                    + "; ".join(artifact_problems[:_MAX_LISTED])
                )
            if report.manifest_sha256 is not None:
                current_manifest = artifact_manifest_sha256(store)
                if current_manifest != report.manifest_sha256:
                    reasons.append("the current artifact manifest differs from the audit snapshot")

    if reasons:
        return _result(ControlStatus.FAILED, "; ".join(reasons), tuple(evidence))
    return _result(ControlStatus.PASSED, "no later material evidence invalidates the snapshot", ())


def _snapshot_index(events: Sequence[Event], terminal_hash: str) -> int | None:
    """Position of the event whose hash pins the snapshot, or ``None`` when it is not present."""
    for position, event in enumerate(events):
        if event.hash == terminal_hash:
            return position
    return None


def _append_snapshot_mismatches(
    report: AuditReport,
    reasons: list[str],
    *,
    current_graph_definition_hash: str | None,
    current_policy_sha256: str | None,
    current_activity_profile_id: str | None,
    current_activity_profile_version: int | None,
) -> None:
    """Add explicit dependency changes that invalidate a persisted audit snapshot."""
    if (
        report.graph_definition_hash is not None
        and current_graph_definition_hash is not None
        and report.graph_definition_hash != current_graph_definition_hash
    ):
        reasons.append("the audit flow version differs from the current flow version")
    if (
        report.policy_sha256 is not None
        and current_policy_sha256 is not None
        and report.policy_sha256 != current_policy_sha256
    ):
        reasons.append("the policy hash differs from the audit snapshot")
    if (
        report.activity_profile_id is not None
        and current_activity_profile_id is not None
        and report.activity_profile_id != current_activity_profile_id
    ):
        reasons.append("the activity profile differs from the audit snapshot")
    if (
        report.activity_profile_version is not None
        and current_activity_profile_version is not None
        and report.activity_profile_version != current_activity_profile_version
    ):
        reasons.append("the activity profile version differs from the audit snapshot")


def _referenced_artifacts(snapshot_events: Sequence[Event]) -> dict[str, str | None]:
    """Map each artifact the snapshot announced to the digest recorded on its creation event."""
    referenced: dict[str, str | None] = {}
    for event in snapshot_events:
        if event.type is EventType.ARTIFACT_CREATED:
            name = event.payload.get("name")
            if isinstance(name, str):
                referenced[name] = _clean_sha(event.payload.get("sha256"))
    return referenced


def _referenced_invalidations(
    later: Sequence[Event], referenced: dict[str, str | None]
) -> list[tuple[Event, str]]:
    """Later ``artifact.invalidated`` events that name an artifact the snapshot referenced."""
    hits: list[tuple[Event, str]] = []
    for event in later:
        if event.type is not EventType.ARTIFACT_INVALIDATED:
            continue
        name = _invalidated_name(event)
        if name is not None and name in referenced:
            hits.append((event, name))
    return hits


def _invalidated_name(event: Event) -> str | None:
    """Read the invalidated artifact's name from an ``artifact.invalidated`` payload."""
    for key in _ARTIFACT_NAME_KEYS:
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _event_evidence(event: Event) -> Evidence:
    """A verifiable pointer to one later event, pinned by its chain hash."""
    return Evidence(kind="event", ref=f"seq:{event.seq}", sha256=_clean_sha(event.hash))


def _artifact_evidence(name: str, created_sha: str | None, store: ArtifactStore | None) -> Evidence:
    """A pointer to a referenced artifact, digest resolved from the store when one is supplied."""
    resolved = created_sha
    if store is not None:
        artifact = store.get(name)
        if artifact is not None:
            resolved = artifact.sha256
    return Evidence(kind="artifact", ref=name, sha256=_clean_sha(resolved))


def _clean_sha(value: object) -> str | None:
    """Return ``value`` when it is a 64-char lowercase hex digest, else ``None``."""
    if isinstance(value, str) and len(value) == _SHA256_LENGTH and set(value) <= _SHA256:
        return value
    return None


def _result(status: ControlStatus, detail: str, evidence: tuple[Evidence, ...]) -> ControlResult:
    """Wrap a freshness verdict as MIRA evidence; a stale snapshot is a HIGH-severity finding."""
    return ControlResult(
        control_id=_CONTROL_ID,
        title=_TITLE,
        status=status,
        severity=Severity.HIGH,
        detail=detail,
        evidence=evidence,
    )


__all__ = ["assess_audit_freshness"]
