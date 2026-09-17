"""Persistent runtime catalog evidence and its independent fresh-consumer verifier."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from thymira.events import canonical_json, verify_events
from thymira.schemas import EventType, ProviderRequest, ThymiraModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event


class RuntimeSkillFile(ThymiraModel):
    """A source file digest recorded in a runtime catalog manifest."""

    ref: str = Field(min_length=1)
    role: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    specificity: int = Field(ge=0)
    source_layer: int = Field(default=0, ge=0)


class RuntimeSkillManifestEntry(ThymiraModel):
    """One effective catalog entry recorded for a fresh consumer."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source: str = Field(min_length=1)
    declaration_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    body: RuntimeSkillFile | None = None
    references: tuple[RuntimeSkillFile, ...] = ()
    specificity: int = Field(ge=0)
    source_layer: int = Field(default=0, ge=0)


class RuntimeSkillChange(ThymiraModel):
    """A replacement or explicit removal applied while building a catalog."""

    name: str = Field(min_length=1)
    action: str = Field(pattern=r"^(added|replaced|removed)$")
    source: str = Field(min_length=1)
    source_layer: int = Field(default=0, ge=0)


class RuntimeSkillSelectionFile(ThymiraModel):
    """A selected file projection, including whether it was bounded or omitted."""

    skill: str = Field(min_length=1)
    file: RuntimeSkillFile
    included: bool
    truncated: bool = False
    omitted_bytes: int = Field(default=0, ge=0)


class RuntimeSkillManifest(ThymiraModel):
    """Persisted runtime catalog evidence that a fresh consumer can verify independently."""

    format_version: int = Field(default=1, ge=1)
    orchestrator: str = Field(min_length=1)
    catalog_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    layers: tuple[str, ...] = ()
    entries: tuple[RuntimeSkillManifestEntry, ...] = ()
    changes: tuple[RuntimeSkillChange, ...] = ()
    selected: tuple[RuntimeSkillSelectionFile, ...] = ()
    selection_phase: Literal["catalog", "selected"] = "catalog"
    selection_id: str | None = Field(
        default=None, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"
    )
    request_id: str | None = Field(default=None, min_length=1)
    dispatch_id: str | None = Field(default=None, min_length=1)
    selected_frame_nonce: str | None = Field(
        default=None, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"
    )
    selected_projection_sha256: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    selected_projection_size_bytes: int | None = Field(default=None, ge=0)


def verify_runtime_skill_manifest(
    manifest: RuntimeSkillManifest,
    roots: tuple[Path, ...],
    *,
    provider_projection: str | None = None,
    authoritative_events: Sequence[Event] | None = None,
) -> tuple[str, ...]:
    """Verify source bytes and selection evidence for a catalog or selected manifest."""
    resolved_roots = tuple(Path(root).resolve() for root in roots)
    errors = []
    catalog_shape = {
        "orchestrator": manifest.orchestrator,
        "entries": [entry.model_dump(mode="json") for entry in manifest.entries],
        "changes": [change.model_dump(mode="json") for change in manifest.changes],
    }
    if _digest(canonical_json(catalog_shape).encode("utf-8")) != manifest.catalog_sha256:
        errors.append("catalog digest mismatch")
    errors.extend(_verify_manifest_entries(manifest.entries, resolved_roots))
    errors.extend(_verify_selected_files(manifest.selected, manifest.entries, resolved_roots))
    errors.extend(_verify_selection_state(manifest, authoritative_events))
    if authoritative_events is not None:
        chain = verify_events(authoritative_events)
        if not chain.valid:
            errors.append(f"authoritative event chain invalid: {chain.error}")
        else:
            errors.extend(_verify_authoritative_selection(manifest, authoritative_events))
    if manifest.selected_projection_sha256 is not None and provider_projection is not None:
        projection_bytes = provider_projection.encode()
        if len(projection_bytes) != manifest.selected_projection_size_bytes:
            errors.append("selected provider projection size mismatch")
        if _digest(projection_bytes) != manifest.selected_projection_sha256:
            errors.append("selected provider projection digest mismatch")
    return tuple(dict.fromkeys(errors))


def _verify_selection_state(
    manifest: RuntimeSkillManifest, authoritative_events: Sequence[Event] | None
) -> tuple[str, ...]:
    """Verify the explicit catalog-only/selected state contract before event binding."""
    selected_state = (
        bool(manifest.selected)
        or manifest.selection_id is not None
        or manifest.request_id is not None
        or manifest.dispatch_id is not None
        or manifest.selected_frame_nonce is not None
        or manifest.selected_projection_sha256 is not None
        or manifest.selected_projection_size_bytes is not None
    )
    errors: list[str] = []
    if manifest.selection_phase == "catalog" and selected_state:
        errors.append("catalog manifest contains selected runtime skill state")
    if manifest.selection_phase == "selected":
        if not selected_state:
            errors.append("selected manifest has no runtime skill selection state")
        if manifest.selection_id is None or manifest.dispatch_id is None:
            errors.append("selected manifest is missing selection or dispatch identity")
        if manifest.selected_projection_sha256 is None:
            errors.append("selected manifest is missing provider projection evidence")
        if manifest.selected_projection_size_bytes is None:
            errors.append("selected manifest is missing provider projection size")
        if manifest.selected_frame_nonce is None:
            errors.append("selected manifest is missing provider projection nonce")
        if authoritative_events is None:
            errors.append(
                "selected manifest requires authoritative runtime skill selection evidence"
            )
    return tuple(errors)


def runtime_skill_manifest_sha256(manifest: RuntimeSkillManifest) -> str:
    """Return the digest of one complete manifest, including its current selection state."""
    return _digest(canonical_json(manifest.model_dump(mode="json")).encode("utf-8"))


def _verify_authoritative_selection(
    manifest: RuntimeSkillManifest, events: Sequence[Event]
) -> tuple[str, ...]:
    """Require selection and at least one recorded provider request to agree.

    The export is derived state. A matching export and ``model.selected`` event alone cannot prove
    that a provider consumed it, and a later catalog-only export must not downgrade a completed
    selection. The request-ledger event is the authoritative association for that decision.
    """
    selection_events = tuple(
        event
        for event in events
        if event.type is EventType.MODEL_SELECTED
        and event.payload.get("runtime_skill_manifest_sha256") is not None
        and event.payload.get("runtime_skill_orchestrator") == manifest.orchestrator
    )
    errors: list[str] = []
    if manifest.selection_phase == "catalog":
        if selection_events:
            errors.append(
                "catalog-only manifest conflicts with authoritative runtime skill selection"
            )
    else:
        evidence = tuple(
            event
            for event in selection_events
            if event.payload.get("runtime_skill_selection_id") == manifest.selection_id
            and event.payload.get("runtime_skill_dispatch_id") == manifest.dispatch_id
        )
        expected = {
            "runtime_skill_manifest_sha256": runtime_skill_manifest_sha256(manifest),
            "runtime_skill_orchestrator": manifest.orchestrator,
            "runtime_skill_selection_id": manifest.selection_id,
            "runtime_skill_dispatch_id": manifest.dispatch_id,
            "runtime_skill_projection_sha256": manifest.selected_projection_sha256,
            "runtime_skill_projection_size_bytes": manifest.selected_projection_size_bytes,
            "runtime_skill_frame_nonce": manifest.selected_frame_nonce,
        }
        if not evidence:
            errors.append("manifest has no authoritative runtime skill selection")
        else:
            latest = evidence[-1]
            if not all(latest.payload.get(key) == value for key, value in expected.items()):
                errors.append("manifest does not match authoritative runtime skill selection")
            elif latest.hash is None:
                errors.append("runtime skill selection event has no chain hash")
            else:
                requests = tuple(
                    event
                    for event in events
                    if event.type is EventType.MODEL_REQUEST_RECORDED
                    and isinstance(event.payload.get("runtime_skill"), dict)
                    and event.payload["runtime_skill"].get("orchestrator") == manifest.orchestrator
                    and event.payload["runtime_skill"].get("selection_id") == manifest.selection_id
                    and event.payload["runtime_skill"].get("manifest_sha256")
                    == expected["runtime_skill_manifest_sha256"]
                    and event.payload["runtime_skill"].get("dispatch_id") == manifest.dispatch_id
                    and event.payload["runtime_skill"].get("projection_sha256")
                    == expected["runtime_skill_projection_sha256"]
                    and event.payload["runtime_skill"].get("projection_size_bytes")
                    == expected["runtime_skill_projection_size_bytes"]
                    and event.payload["runtime_skill"].get("selection_event_id")
                    == str(latest.event_id)
                    and event.payload["runtime_skill"].get("selection_event_hash") == latest.hash
                )
                if not requests:
                    errors.append("manifest has no authoritative provider request association")
                elif not any(
                    _request_contains_projection(event.payload, manifest) for event in requests
                ):
                    errors.append(
                        "manifest does not match actual provider runtime skill projection"
                    )
    return tuple(errors)


def _request_contains_projection(
    payload: dict[str, object], manifest: RuntimeSkillManifest
) -> bool:
    """Check the exact selected projection in one recorded provider request independently."""
    try:
        request = ProviderRequest.model_validate(payload)
    except ValueError:
        return False
    if _digest(canonical_json(request.gateway.to_json_dict()).encode("utf-8")) != (
        request.gateway_sha256
    ):
        return False
    if manifest.selected_frame_nonce is None:
        return False
    for message in request.gateway.messages:
        if message.role != "system":
            continue
        projection = _extract_projection(message.content, manifest.selected_frame_nonce)
        if projection is None:
            if manifest.selected:
                continue
            projection = ""
        projection_bytes = projection.encode("utf-8")
        if (
            len(projection_bytes) == manifest.selected_projection_size_bytes
            and _digest(projection_bytes) == manifest.selected_projection_sha256
        ):
            return True
    return False


def _extract_projection(system: str, nonce: str) -> str | None:
    """Extract nonce-bound runtime frames from a provider system message."""
    prefix = f"<<<THYMIRA_RUNTIME_SKILL:{nonce}:"
    pattern = re.compile(
        rf"{re.escape(prefix)}[^\n]*:START>>>\n.*?\n"
        rf"{re.escape(prefix)}[^\n]*:END>>>",
        flags=re.DOTALL,
    )
    frames = tuple(match.group(0) for match in pattern.finditer(system))
    return "\n\n".join(frames) if frames else None


def _verify_manifest_entries(
    entries: tuple[RuntimeSkillManifestEntry, ...], roots: tuple[Path, ...]
) -> tuple[str, ...]:
    """Verify declaration source identities and digests in a fresh consumer."""
    errors: list[str] = []
    for entry in entries:
        if entry.source_layer >= len(roots):
            errors.append(f"missing declaration layer: {entry.name}")
            continue
        try:
            candidate = _safe_manifest_path(roots[entry.source_layer], entry.source)
        except ValueError:
            errors.append(f"invalid declaration source: {entry.name}")
            continue
        if not candidate.is_file():
            errors.append(f"missing declaration: {entry.source}")
        elif _digest(candidate.read_bytes()) != entry.declaration_sha256:
            errors.append(f"declaration digest mismatch: {entry.name}")
    return tuple(errors)


def _verify_selected_files(
    selected_files: tuple[RuntimeSkillSelectionFile, ...],
    entries: tuple[RuntimeSkillManifestEntry, ...],
    roots: tuple[Path, ...],
) -> tuple[str, ...]:
    """Verify selected files bind to effective entries and their source digests."""
    errors: list[str] = []
    entries_by_name = {entry.name: entry for entry in entries}
    selected_by_skill: dict[str, list[RuntimeSkillFile]] = {}
    for selected in selected_files:
        entry = entries_by_name.get(selected.skill)
        if entry is None:
            errors.append(f"selected file belongs to unknown skill: {selected.skill}")
        else:
            expected = tuple(file for file in (entry.body, *entry.references) if file is not None)
            if not any(selected.file == file for file in expected):
                errors.append(
                    f"selected file is not declared by effective skill: "
                    f"{selected.skill}:{selected.file.ref}"
                )
            selected_by_skill.setdefault(selected.skill, []).append(selected.file)
        if selected.file.source_layer >= len(roots):
            errors.append(f"missing selected file layer: {selected.file.ref}")
            continue
        try:
            candidate = _safe_manifest_path(roots[selected.file.source_layer], selected.file.ref)
        except ValueError:
            errors.append(f"invalid selected file source: {selected.file.ref}")
            continue
        if not candidate.is_file():
            errors.append(f"missing selected file: {selected.file.ref}")
        elif _digest(candidate.read_bytes()) != selected.file.sha256:
            errors.append(f"selected file digest mismatch: {selected.file.ref}")
    for entry in entries:
        files = selected_by_skill.get(entry.name, ())
        expected = tuple(file for file in (entry.body, *entry.references) if file is not None)
        errors.extend(
            f"selected file missing from projection: {entry.name}:{expected_file.ref}"
            for expected_file in expected
            if not any(expected_file == file for file in files)
        )
    return tuple(errors)


def _safe_manifest_path(root: Path, reference: str) -> Path:
    """Resolve a persisted manifest reference without allowing a path escape."""
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"manifest source escapes its root: {reference!r}") from exc
    return candidate


def _digest(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of ``value``."""
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "RuntimeSkillChange",
    "RuntimeSkillFile",
    "RuntimeSkillManifest",
    "RuntimeSkillManifestEntry",
    "RuntimeSkillSelectionFile",
    "runtime_skill_manifest_sha256",
    "verify_runtime_skill_manifest",
]
