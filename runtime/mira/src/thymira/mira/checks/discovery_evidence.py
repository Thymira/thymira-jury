"""Recompute discovery evidence (glob/grep/list_files) independently from persisted facts.

A bounded discovery rendering claims a total, a truncation flag and a prefix of what it found;
:mod:`thymira.tools.builtins.discovery` persists the complete list a caller cannot see as a
manifest-hashed artifact. This module is the check that a rendering never claims more than the
persisted list can substantiate. It imports nothing from :mod:`thymira.tools` -- it re-declares
:data:`DISCOVERY_SCHEMA` itself, the way :mod:`thymira.mira.checks.tool_intent_evidence` and
:mod:`thymira.mira.checks.approval_scope_evidence` re-implement the facts they audit rather than
trust the producer's own constant.

What makes the recomputation independent is that the facts it relates were written by three
different parties at three different times: the *tool* wrote the discovery artifact's bytes, the
*manager* observed the source before and after execution and persisted a separate witness, and the
*store* computed the sha256 values copied onto ``artifact.created``. The manager also wrote
``result_sha256``/``artifact_ids`` onto ``tool.completed`` after the tool had already returned.
Nothing here reads ``thymira.tools.builtins.discovery`` or the manager witness generator to know
what "correct" means; it validates both durable artifacts and their event bindings independently.

A second, unrelated fold in the same module recomputes the four freshness refusals
(``FS_READ_REQUIRED``/``FS_STALE_VERSION``) a ``write_file``/``edit_file`` failure records,
straight from :mod:`thymira.schemas` -- the shared contract layer, not the tools runtime -- the
same way :mod:`thymira.tools.refusals` re-exports it for the manager. A recorded refusal sentence
that does not recompute from the request's own path is not evidence, only a claim.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import regex as bounded_regex

from thymira.events import canonical_json, sha256_bytes, sha256_text
from thymira.mira.checks.models import ControlStatus
from thymira.schemas import (
    EventType,
    ToolCallStatus,
    read_required_refusal,
    stale_version_refusal,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from thymira.schemas import Event
    from thymira.state import ArtifactStore

DISCOVERY_SCHEMA = "thymira.discovery/1"
"""Re-declared from ``thymira.tools.builtins.discovery.DISCOVERY_SCHEMA``: MIRA never imports
the tools runtime, so an artifact claiming a different schema is simply not one of ours."""

_DISCOVERY_TOOLS = frozenset({"glob", "grep", "list_files"})
DISCOVERY_WITNESS_SCHEMA = "thymira.discovery.source/1"
_SHA256_HEX_LENGTH = 64
_DISCOVERY_WITNESS_STATUSES = frozenset({"readable", "too_large", "undecodable", "unreadable"})
_ORDERS = frozenset({"path", "path_then_line"})
_FRESHNESS_TOOLS = frozenset({"write_file", "edit_file"})
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
_MAX_DISCOVERY_SOURCE_FILES = 10_000
_MAX_DISCOVERY_SCAN_BYTES = 256 * 1024 * 1024
_MAX_DISCOVERY_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_DISCOVERY_MATCHES = 100_000
_MAX_DISCOVERY_ARTIFACT_BYTES = 32 * 1024 * 1024
_MAX_DISCOVERY_SECONDS = 10.0
_REGEX_TIMEOUT_SECONDS = 0.05


def _sha256(value: Any) -> str | None:
    """Return a lowercase hexadecimal SHA-256 value, or ``None`` for malformed input."""
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None


def _text(value: Any) -> str | None:
    """A non-empty string from an untrusted payload value, or ``None`` for anything else."""
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: Any) -> int | None:
    """A genuine non-negative ``int`` from an untrusted value; ``bool`` is not an ``int`` here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sort_key_path(item: Any) -> tuple[int, str]:
    return (0, item) if isinstance(item, str) else (1, "")


def _sort_key_path_then_line(item: Any) -> tuple[int, str, int]:
    if isinstance(item, dict) and isinstance(item.get("path"), str):
        line = item.get("line")
        return (
            0,
            item["path"],
            line if isinstance(line, int) and not isinstance(line, bool) else 0,
        )
    return (1, "", 0)


def _canonical(item: Any) -> str:
    """A stable string identity for a match entry, used only to detect duplicates."""
    return json.dumps(item, sort_keys=True, default=str)


def _resorted(matches: list[Any], order: str) -> list[Any] | None:
    """Return ``matches`` re-sorted under the declared order, or ``None`` for an unknown order."""
    if order == "path":
        return sorted(matches, key=_sort_key_path)
    if order == "path_then_line":
        return sorted(matches, key=_sort_key_path_then_line)
    return None


@dataclass(frozen=True, slots=True)
class _ArtifactFact:
    name: str
    sha256: str


def _artifacts_by_id(events: Sequence[Event]) -> dict[str, _ArtifactFact]:
    """Fold ``artifact.created`` events into an ``artifact_id -> (name, sha256)`` map."""
    facts: dict[str, _ArtifactFact] = {}
    for event in events:
        if event.type is not EventType.ARTIFACT_CREATED:
            continue
        artifact_id = _text(event.payload.get("artifact_id"))
        name = _text(event.payload.get("name"))
        sha256 = _text(event.payload.get("sha256"))
        if artifact_id is not None and name is not None and sha256 is not None:
            facts[artifact_id] = _ArtifactFact(name=name, sha256=sha256)
    return facts


def _started_by_call_id(events: Sequence[Event]) -> dict[str, Event]:
    """Fold ``tool.started`` events into a ``tool_call_id -> event`` map, first one wins."""
    started: dict[str, Event] = {}
    for event in events:
        if event.type is not EventType.TOOL_STARTED:
            continue
        call_id = _text(event.payload.get("tool_call_id"))
        if call_id is not None and call_id not in started:
            started[call_id] = event
    return started


@dataclass(frozen=True, slots=True)
class _Problem:
    detail: str
    seq: int


def _discovery_artifact(
    completed: Event, artifacts: dict[str, _ArtifactFact]
) -> _ArtifactFact | None:
    """Find the discovery artifact among a completed call's declared ``artifact_ids``."""
    ids = completed.payload.get("artifact_ids")
    if not isinstance(ids, list):
        return None
    witness_id = completed.payload.get("discovery_witness_artifact_id")
    for artifact_id in ids:
        if artifact_id == witness_id:
            continue
        fact = artifacts.get(artifact_id) if isinstance(artifact_id, str) else None
        if fact is not None:
            return fact
    return None


def _problem(completed: Event, tool: str, detail: str) -> _Problem:
    return _Problem(f"{tool} at seq {completed.seq}: {detail}", completed.seq)


def _load_bounded(  # noqa: PLR0911  # each store boundary failure remains explicit
    store: ArtifactStore, fact: _ArtifactFact, max_bytes: int
) -> tuple[bytes | None, str | None]:
    """Read a manifest artifact through its bounded reader, before parsing any JSON."""
    try:
        metadata = store.get(fact.name)
    except Exception as exc:  # noqa: BLE001  # an untrusted store must become an audit finding
        return None, f"could not inspect artifact {fact.name!r}: {exc}"
    if metadata is None:
        return None, f"artifact {fact.name!r} is missing from the store manifest"
    if metadata.size_bytes > max_bytes:
        return None, f"artifact {fact.name!r} exceeds the {max_bytes}-byte audit bound"
    loader = getattr(store, "load_bytes_bounded", None)
    if not callable(loader):
        return None, "artifact store has no bounded byte reader"
    try:
        raw = loader(fact.name, max_bytes)
    except Exception as exc:  # noqa: BLE001  # an untrusted store must become an audit finding
        return None, f"could not read artifact {fact.name!r}: {exc}"
    if not isinstance(raw, bytes):
        return None, f"artifact {fact.name!r} bounded reader returned non-bytes"
    if len(raw) > max_bytes:
        return None, f"artifact {fact.name!r} exceeds the {max_bytes}-byte audit bound"
    return raw, None


def _load_discovery_payload(
    completed: Event, tool: str, artifacts: dict[str, _ArtifactFact], store: ArtifactStore
) -> tuple[dict[str, Any] | None, _Problem | None]:
    """Find, hash-verify and JSON-decode the artifact one completed call claims to have made.

    Returns the decoded payload, or ``None`` plus the one problem that stopped it -- an artifact
    that cannot be found, read, hash-verified or parsed is a single fact, not several.
    """
    fact = _discovery_artifact(completed, artifacts)
    if fact is None:
        return None, _problem(completed, tool, "no discovery artifact recorded")
    raw, load_error = _load_bounded(store, fact, _MAX_DISCOVERY_ARTIFACT_BYTES)
    if load_error is not None or raw is None:
        return None, _problem(completed, tool, load_error or "could not read artifact")
    if sha256_bytes(raw) != fact.sha256:
        return None, _problem(
            completed,
            tool,
            f"artifact {fact.name!r} bytes do not hash to its recorded artifact.created sha256",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _problem(completed, tool, f"discovery artifact is not valid JSON: {exc}")
    if not isinstance(payload, dict) or payload.get("schema") != DISCOVERY_SCHEMA:
        return None, _problem(
            completed, tool, f"discovery artifact carries no {DISCOVERY_SCHEMA!r} schema"
        )
    return payload, None


def _load_witness_payload(  # noqa: PLR0911  # malformed witness branches fail closed
    completed: Event,
    tool: str,
    artifacts: dict[str, _ArtifactFact],
    store: ArtifactStore,
) -> tuple[dict[str, Any] | None, _Problem | None]:
    """Load the manager-owned source witness named by one discovery completion."""
    witness_id = completed.payload.get("discovery_witness_artifact_id")
    if not isinstance(witness_id, str):
        return None, _problem(completed, tool, "no manager discovery source witness recorded")
    completed_ids = completed.payload.get("artifact_ids")
    if not isinstance(completed_ids, list) or witness_id not in completed_ids:
        return None, _problem(
            completed, tool, "discovery source witness is not an announced artifact"
        )
    fact = artifacts.get(witness_id)
    if fact is None:
        return None, _problem(completed, tool, "discovery source witness is missing")
    raw, load_error = _load_bounded(store, fact, _MAX_DISCOVERY_ARTIFACT_BYTES)
    if load_error is not None or raw is None:
        return None, _problem(completed, tool, load_error or "could not read source witness")
    if sha256_bytes(raw) != fact.sha256:
        return None, _problem(
            completed, tool, "source witness bytes do not hash to its artifact.created sha256"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, _problem(completed, tool, f"source witness is not valid JSON: {exc}")
    if not isinstance(payload, dict) or payload.get("schema") != DISCOVERY_WITNESS_SCHEMA:
        return None, _problem(completed, tool, "source witness carries an unknown schema")
    if payload.get("tool") != tool:
        return None, _problem(completed, tool, "source witness tool does not match the call")
    return payload, None


def _query_from_started(  # noqa: PLR0911  # malformed recorded arguments fail closed
    tool: str, arguments: Any
) -> dict[str, Any] | None:
    """Extract the exact manager-recorded discovery query from ``tool.started``."""
    if not isinstance(arguments, dict):
        return None
    path = arguments.get("path", ".")
    if not isinstance(path, str):
        return None
    if tool == "glob" and isinstance(arguments.get("pattern"), str):
        return {"pattern": arguments["pattern"], "path": path}
    if tool == "grep" and isinstance(arguments.get("pattern"), str):
        include = arguments.get("include")
        if include is not None and not isinstance(include, str):
            return None
        return {
            "pattern": arguments["pattern"],
            "path": path,
            "include": include,
        }
    if tool == "list_files":
        return {"path": path}
    return None


def _verify_source_snapshot(  # noqa: PLR0911, PLR0912, PLR0915  # malformed facts fail closed
    completed: Event, tool: str, payload: dict[str, Any]
) -> _Problem | None:
    """Verify the source snapshot is self-consistent and its projection is well formed."""
    deadline = time.monotonic() + _MAX_DISCOVERY_SECONDS
    source = payload.get("source")
    source_sha256 = _sha256(payload.get("source_sha256"))
    files = source.get("files") if isinstance(source, dict) else None
    query = payload.get("query")
    include = query.get("include") if isinstance(query, dict) else None
    if (
        source_sha256 is None
        or not isinstance(files, list)
        or len(files) > _MAX_DISCOVERY_SOURCE_FILES
    ):
        return _problem(completed, tool, "source witness snapshot is malformed")
    paths: list[str] = []
    total_source_bytes = 0
    total_content_bytes = 0
    for fact in files:
        if time.monotonic() >= deadline:
            return _problem(completed, tool, "source witness audit exceeded its time budget")
        if not isinstance(fact, dict):
            return _problem(completed, tool, "source witness file fact is malformed")
        path = _text(fact.get("path"))
        status = fact.get("status")
        size = fact.get("size_bytes")
        digest = fact.get("sha256")
        content = fact.get("content")
        path_parts = path.split("/") if isinstance(path, str) else ()
        content_required = tool == "grep" and (
            include is None
            or (
                isinstance(include, str)
                and isinstance(path, str)
                and fnmatch.fnmatch(path.rsplit("/", 1)[-1], include)
            )
        )
        if (
            path is None
            or path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path_parts)
            or status not in _DISCOVERY_WITNESS_STATUSES
            or (size is not None and _non_negative_int(size) is None)
            or (digest is not None and _sha256(digest) is None)
            or (status != "unreadable" and not isinstance(digest, str))
        ):
            return _problem(completed, tool, "source witness file fact is malformed")
        if status == "too_large" and (
            tool != "grep" or not isinstance(size, int) or size <= _MAX_GREP_FILE_BYTES
        ):
            return _problem(completed, tool, "source witness too_large exclusion is not justified")
        if isinstance(size, int):
            total_source_bytes += size
            if total_source_bytes > _MAX_DISCOVERY_SCAN_BYTES:
                return _problem(completed, tool, "source witness exceeds its scan byte budget")
        if content_required and status == "readable" and not isinstance(content, str):
            return _problem(completed, tool, "source witness has no required content snapshot")
        if tool != "grep" and "content" in fact:
            return _problem(completed, tool, "source witness file fact is malformed")
        if tool == "grep" and status != "readable" and "content" in fact:
            return _problem(completed, tool, "source witness file fact is malformed")
        if content_required and status == "readable":
            if not isinstance(content, str):
                return _problem(completed, tool, "source witness file fact is malformed")
            encoded = content.encode("utf-8")
            total_content_bytes += len(encoded)
            if total_content_bytes > _MAX_DISCOVERY_CONTENT_BYTES:
                return _problem(completed, tool, "source witness exceeds its content byte budget")
            if size != len(encoded) or digest != sha256_bytes(encoded):
                return _problem(
                    completed, tool, "source witness content does not match its file digest"
                )
        paths.append(path)
    if time.monotonic() >= deadline:
        return _problem(completed, tool, "source witness audit exceeded its time budget")
    if paths != sorted(set(paths)):
        return _problem(completed, tool, "source witness files are not unique and ordered")
    facts_by_path = {fact["path"]: fact for fact in files}
    expected_source_sha256 = sha256_text(canonical_json({"files": files}))
    if time.monotonic() >= deadline:
        return _problem(completed, tool, "source witness audit exceeded its time budget")
    if source_sha256 != expected_source_sha256:
        return _problem(completed, tool, "source witness snapshot digest is inconsistent")
    expected_matches = payload.get("matches")
    expected_skipped = payload.get("skipped")
    if (
        not isinstance(expected_matches, list)
        or not isinstance(expected_skipped, list)
        or len(expected_matches) > _MAX_DISCOVERY_MATCHES
        or len(expected_skipped) > _MAX_DISCOVERY_SOURCE_FILES
    ):
        return _problem(completed, tool, "source witness projection is malformed")
    order = "path_then_line" if tool == "grep" else "path"
    problem = _verify_match_list(completed, tool, expected_matches, order, len(expected_matches))
    if problem is not None:
        return _Problem(
            f"{problem.detail.replace('persisted match list', 'source witness match list')}",
            problem.seq,
        )
    if expected_skipped != sorted(set(expected_skipped)) or not all(
        isinstance(path, str) and path in facts_by_path for path in expected_skipped
    ):
        return _problem(completed, tool, "source witness skipped list is malformed")
    for match in expected_matches:
        path = match if tool != "grep" else match.get("path") if isinstance(match, dict) else None
        if not isinstance(path, str) or path not in facts_by_path:
            return _problem(completed, tool, "source witness match is outside its source snapshot")
        if tool == "grep" and facts_by_path[path].get("status") != "readable":
            return _problem(completed, tool, "source witness match uses a non-readable source file")
    return None


def _source_glob_matches(pattern: str, relative: str) -> bool:
    """Apply the documented glob semantics to a retained source path independently."""
    target = relative.rsplit("/", 1)[-1] if "/" not in pattern else relative
    return re.match(f"^{_translate_glob(pattern)}$", target) is not None


def _translate_glob(pattern: str) -> str:
    """Translate glob syntax without importing the producer's matcher."""
    index = 0
    parts: list[str] = []
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            parts.append("(?:.*/)?")
            index += 3
        elif pattern[index : index + 2] == "**":
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            end = _bracket_end(pattern, index)
            if end is None:
                parts.append(re.escape("["))
                index += 1
            else:
                inner = pattern[index + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                parts.append(f"[{inner}]")
                index = end + 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return "".join(parts)


def _bracket_end(pattern: str, start: int) -> int | None:
    """Find one glob character class's closing bracket."""
    index = start + 1
    if index < len(pattern) and pattern[index] in "!^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern) and pattern[index] != "]":
        index += 1
    return index if index < len(pattern) else None


def _source_projection(  # noqa: PLR0911, PLR0912  # each malformed branch fails closed
    completed: Event, tool: str, query: dict[str, Any], witness: dict[str, Any]
) -> tuple[list[Any], list[str]] | _Problem:
    """Recompute a discovery projection from the retained source snapshot and exact query."""
    deadline = time.monotonic() + _MAX_DISCOVERY_SECONDS
    source = witness.get("source")
    files = source.get("files") if isinstance(source, dict) else None
    if not isinstance(files, list):
        return _problem(completed, tool, "source witness snapshot is malformed")
    if tool == "glob":
        pattern = query.get("pattern")
        if not isinstance(pattern, str):
            return _problem(completed, tool, "source witness glob query is malformed")
        try:
            matches = [
                fact["path"]
                for fact in files
                if time.monotonic() < deadline
                if isinstance(fact, dict)
                and isinstance(fact.get("path"), str)
                and _source_glob_matches(pattern, fact["path"])
            ]
            if time.monotonic() >= deadline:
                return _problem(
                    completed, tool, "source witness projection exceeded its time budget"
                )
        except re.error as exc:
            return _problem(completed, tool, f"source witness glob query is invalid: {exc}")
        return matches, []
    if tool == "list_files":
        if time.monotonic() >= deadline:
            return _problem(completed, tool, "source witness projection exceeded its time budget")
        return (
            [
                fact["path"]
                for fact in files
                if isinstance(fact, dict) and isinstance(fact.get("path"), str)
            ],
            [],
        )
    if tool != "grep":
        return _problem(completed, tool, "source witness tool is unknown")
    pattern = query.get("pattern")
    include = query.get("include")
    if not isinstance(pattern, str) or (include is not None and not isinstance(include, str)):
        return _problem(completed, tool, "source witness grep query is malformed")
    try:
        regex = bounded_regex.compile(pattern)
    except bounded_regex.error as exc:
        return _problem(completed, tool, f"source witness grep query is invalid: {exc}")
    matches: list[dict[str, Any]] = []
    skipped: list[str] = []
    for fact in files:
        if time.monotonic() >= deadline:
            return _problem(completed, tool, "source witness projection exceeded its time budget")
        if not isinstance(fact, dict) or not isinstance(fact.get("path"), str):
            return _problem(completed, tool, "source witness file fact is malformed")
        relative = fact["path"]
        if include and not fnmatch.fnmatch(relative.rsplit("/", 1)[-1], include):
            continue
        if fact.get("status") in {"too_large", "undecodable", "unreadable"}:
            skipped.append(relative)
            continue
        content = fact.get("content")
        if not isinstance(content, str):
            return _problem(completed, tool, "source witness has no grep content snapshot")
        for number, line in enumerate(content.splitlines(), start=1):
            if time.monotonic() >= deadline:
                return _problem(
                    completed, tool, "source witness projection exceeded its time budget"
                )
            try:
                found = regex.search(line, timeout=_REGEX_TIMEOUT_SECONDS)
            except TimeoutError:
                return _problem(
                    completed,
                    tool,
                    "source witness regular expression exceeded its time budget",
                )
            if found:
                matches.append({"path": relative, "line": number})
                if len(matches) > _MAX_DISCOVERY_MATCHES:
                    return _problem(completed, tool, "source witness exceeds its match budget")
    return matches, skipped


def _verify_source_binding(  # noqa: PLR0911  # independent binding failures stay explicit
    completed: Event,
    started: Event | None,
    tool: str,
    discovery: dict[str, Any],
    witness: dict[str, Any],
) -> _Problem | None:
    """Bind the producer list to the manager's query and independent source projection."""
    if started is None:
        return _problem(completed, tool, "discovery call has no matching tool.started witness")
    query = witness.get("query")
    query_sha256 = _text(witness.get("query_sha256"))
    expected_query = _query_from_started(tool, started.payload.get("arguments"))
    if (
        not isinstance(query, dict)
        or expected_query is None
        or query != expected_query
        or query_sha256 != sha256_text(canonical_json(query))
    ):
        return _problem(completed, tool, "source witness query is not bound to tool.started")
    if query_sha256 != _text(started.payload.get("discovery_witness_query_sha256")):
        return _problem(
            completed, tool, "source witness query identity disagrees with tool.started"
        )
    if discovery.get("query") != query:
        return _problem(completed, tool, "producer discovery query differs from tool.started")
    source_sha256 = _text(witness.get("source_sha256"))
    if source_sha256 != _text(started.payload.get("discovery_witness_source_sha256")):
        return _problem(completed, tool, "source witness identity disagrees with tool.started")
    projection = _source_projection(completed, tool, query, witness)
    if isinstance(projection, _Problem):
        return projection
    projected_matches, projected_skipped = projection
    if witness.get("matches") != projected_matches:
        return _problem(
            completed,
            tool,
            "source witness matches do not recompute from its retained source snapshot",
        )
    if witness.get("skipped") != projected_skipped:
        return _problem(
            completed,
            tool,
            "source witness skipped files do not recompute from its retained source snapshot",
        )
    if witness.get("matches") != discovery.get("matches"):
        return _problem(
            completed,
            tool,
            "producer discovery list is incomplete or differs from the source witness",
        )
    if witness.get("skipped") != discovery.get("skipped"):
        return _problem(completed, tool, "producer skipped list differs from the source witness")
    return None


def _verify_match_list(
    completed: Event, tool: str, matches: list[Any], order: str, total: int
) -> _Problem | None:
    """Verify the persisted list itself: complete, unique, and ordered as declared."""
    if len(matches) > _MAX_DISCOVERY_MATCHES:
        return _problem(completed, tool, "persisted match list exceeds its match budget")
    if total != len(matches):
        return _problem(
            completed,
            tool,
            f"claimed total {total} disagrees with {len(matches)} persisted matches",
        )
    canonical = [_canonical(item) for item in matches]
    if len(set(canonical)) != len(canonical):
        return _problem(completed, tool, "persisted match list contains a duplicate entry")
    resorted = _resorted(matches, order)
    if resorted is None or resorted != matches:
        return _problem(completed, tool, f"persisted match list is not ordered by {order!r}")
    return None


def _verify_rendering(
    completed: Event, tool: str, matches: list[Any], total: int, rendered: dict[str, Any]
) -> _Problem | None:
    """Verify the rendered block is a true, fact-consistent prefix of the persisted list."""
    count = _non_negative_int(rendered.get("count"))
    truncated = rendered.get("truncated")
    locations = rendered.get("locations")
    rendered_sha256 = _text(rendered.get("sha256"))
    malformed = (
        count is None
        or not isinstance(truncated, bool)
        or not isinstance(locations, list)
        or rendered_sha256 is None
    )
    if malformed or count is None:
        return _problem(completed, tool, "discovery artifact's rendered block is malformed")
    if count > total:
        return _problem(completed, tool, f"rendered count {count} exceeds total {total}")
    if truncated != (count < total):
        return _problem(completed, tool, "rendered.truncated disagrees with count vs total")
    if locations != matches[:count]:
        return _problem(
            completed, tool, "rendered locations are not the true prefix of the persisted list"
        )
    if rendered_sha256 != _text(completed.payload.get("result_sha256")):
        return _problem(
            completed,
            tool,
            "rendered.sha256 disagrees with the recorded tool.completed result_sha256",
        )
    return None


def _check_one_discovery_call(  # noqa: PLR0911  # each evidence layer fails closed
    completed: Event,
    started: Event | None,
    artifacts: dict[str, _ArtifactFact],
    store: ArtifactStore,
) -> _Problem | None:
    """Recompute every relationship one discovery ``tool.completed`` event claims."""
    tool = _text(completed.payload.get("tool")) or "?"
    payload, problem = _load_discovery_payload(completed, tool, artifacts, store)
    if problem is not None or payload is None:
        return problem
    if payload.get("tool") != tool:
        return _problem(completed, tool, "discovery artifact tool does not match the call")
    matches = payload.get("matches")
    order = payload.get("order")
    total = _non_negative_int(payload.get("total"))
    rendered = payload.get("rendered")
    if (
        not isinstance(matches, list)
        or order not in _ORDERS
        or total is None
        or not isinstance(rendered, dict)
    ):
        return _problem(completed, tool, "discovery artifact is malformed")
    problem = _verify_match_list(completed, tool, matches, order, total)
    if problem is not None:
        return problem
    problem = _verify_rendering(completed, tool, matches, total, rendered)
    if problem is not None:
        return problem
    witness, problem = _load_witness_payload(completed, tool, artifacts, store)
    if problem is not None or witness is None:
        return problem
    problem = _verify_source_snapshot(completed, tool, witness)
    if problem is not None:
        return problem
    return _verify_source_binding(completed, started, tool, payload, witness)


def _check_one_freshness_refusal(completed: Event, started: dict[str, Event]) -> _Problem | None:
    """Recompute an FS_READ_REQUIRED/FS_STALE_VERSION refusal from its own request's path."""
    error = _text(completed.payload.get("error"))
    if error is None or not error.startswith(("FS_STALE_VERSION", "FS_READ_REQUIRED")):
        return None
    call_id = _text(completed.payload.get("tool_call_id"))
    request = started.get(call_id) if call_id is not None else None
    arguments = request.payload.get("arguments") if request is not None else None
    path = arguments.get("path") if isinstance(arguments, dict) else None
    if not isinstance(path, str):
        return _Problem(
            f"{completed.payload.get('tool', '?')} at seq {completed.seq}: FS_ refusal has no "
            "matching tool.started request to recompute it from",
            completed.seq,
        )
    expected = (
        stale_version_refusal(path)
        if error.startswith("FS_STALE_VERSION")
        else read_required_refusal(path)
    )
    if error != expected:
        return _Problem(
            f"{completed.payload.get('tool', '?')} at seq {completed.seq}: recorded refusal does "
            "not recompute from its own request",
            completed.seq,
        )
    return None


def check_discovery_evidence(
    events: Sequence[Event], store: ArtifactStore | None
) -> tuple[ControlStatus, str]:
    """Fold every discovery call and freshness refusal, recomputing what each one claims.

    Returns ``NOT_EVALUATED`` when no artifact store is available at all -- an honest "cannot
    check", never a silent pass. Returns ``NOT_APPLICABLE`` when the log holds no discovery call
    and no freshness refusal to check. Otherwise ``FAILED`` on the first-collected set of
    problems, or ``PASSED``.
    """
    discovery_completed = [
        event
        for event in events
        if event.type is EventType.TOOL_COMPLETED
        and _text(event.payload.get("tool")) in _DISCOVERY_TOOLS
        and event.payload.get("status") == ToolCallStatus.COMPLETED.value
    ]
    freshness_completed = [
        event
        for event in events
        if event.type is EventType.TOOL_COMPLETED
        and _text(event.payload.get("tool")) in _FRESHNESS_TOOLS
        and event.payload.get("status") == ToolCallStatus.FAILED.value
    ]
    if not discovery_completed and not freshness_completed:
        return ControlStatus.NOT_APPLICABLE, "no discovery call or freshness refusal recorded"
    if store is None:
        return (
            ControlStatus.NOT_EVALUATED,
            "no artifact store available to verify discovery evidence",
        )

    problems: list[_Problem] = []
    if discovery_completed:
        artifacts = _artifacts_by_id(events)
        started = _started_by_call_id(events)
        for completed in discovery_completed:
            call_id = _text(completed.payload.get("tool_call_id"))
            problem = _check_one_discovery_call(
                completed, started.get(call_id) if call_id is not None else None, artifacts, store
            )
            if problem is not None:
                problems.append(problem)
    if freshness_completed:
        started = _started_by_call_id(events)
        for completed in freshness_completed:
            problem = _check_one_freshness_refusal(completed, started)
            if problem is not None:
                problems.append(problem)

    if not problems:
        return ControlStatus.PASSED, (
            f"{len(discovery_completed)} discovery call(s) and {len(freshness_completed)} "
            "freshness refusal(s) all substantiated"
        )
    return ControlStatus.FAILED, "; ".join(problem.detail for problem in problems)


__all__ = ["DISCOVERY_SCHEMA", "check_discovery_evidence"]
