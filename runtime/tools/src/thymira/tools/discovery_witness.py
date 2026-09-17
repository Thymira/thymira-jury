"""Independent source witnesses for manager-owned filesystem discovery evidence.

The discovery builtins persist the result they produce.  That proves that the result is intact,
ordered and durable, but it cannot prove that a producer did not leave a real match out.  The
Tool Manager captures this source witness separately, before the tool runs, and compares a second
capture after it returns.  MIRA later relates the two independently written artifacts without
walking the workspace (which may have changed since execution).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, cast

import regex as bounded_regex

from thymira.events import canonical_json, sha256_bytes
from thymira.schemas import is_credential_environment_name

if TYPE_CHECKING:
    from collections.abc import Iterator

DISCOVERY_WITNESS_SCHEMA = "thymira.discovery.source/1"
DISCOVERY_TOOLS = frozenset({"glob", "grep", "list_files"})
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_SOURCE_FILES = 10_000
MAX_DISCOVERY_SCAN_BYTES = 256 * 1024 * 1024
MAX_DISCOVERY_CONTENT_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY_MATCHES = 100_000
MAX_DISCOVERY_WITNESS_BYTES = 32 * 1024 * 1024
MAX_DISCOVERY_SECONDS = 10.0
_REGEX_TIMEOUT_SECONDS = 0.05
_READ_CHUNK_BYTES = 64 * 1024
_EXCLUDED_DIR_NAMES = frozenset({".git", ".thymira"})


class DiscoveryWitnessLimitError(ValueError):
    """Raised when an independent discovery witness would exceed a resource budget."""


class DiscoveryWitnessCredentialError(ValueError):
    """Raised when a source snapshot would duplicate a known credential into evidence."""


@dataclass(frozen=True, slots=True)
class DiscoverySourceWitness:
    """The manager's independent observation of one discovery query."""

    tool: str
    query: dict[str, Any]
    query_sha256: str
    source_sha256: str
    source_files: tuple[dict[str, Any], ...]
    matches: tuple[Any, ...]
    skipped: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        """Return the durable, self-describing witness payload."""
        return {
            "schema": DISCOVERY_WITNESS_SCHEMA,
            "tool": self.tool,
            "query": self.query,
            "query_sha256": self.query_sha256,
            "source_sha256": self.source_sha256,
            "source": {"files": list(self.source_files)},
            "matches": list(self.matches),
            "skipped": list(self.skipped),
        }

    def serialized_payload(self, *, deadline: float | None = None) -> bytes:
        """Return canonical witness bytes with an incremental size and time bound.

        The complete payload is intentionally not materialized before applying the artifact
        limit.  Source facts and projections are emitted one item at a time, so a refused
        witness retains at most the configured bound plus one bounded item in memory.
        """
        deadline = _with_deadline(deadline)
        encoded = bytearray()

        def append(chunk: bytes) -> None:
            _check_deadline(deadline)
            if len(encoded) + len(chunk) > MAX_DISCOVERY_WITNESS_BYTES:
                raise DiscoveryWitnessLimitError(
                    f"discovery witness exceeds {MAX_DISCOVERY_WITNESS_BYTES} serialized bytes"
                )
            encoded.extend(chunk)
            _check_deadline(deadline)

        def append_value(value: Any) -> None:
            _check_deadline(deadline)
            if _json_upper_bound(value) > MAX_DISCOVERY_WITNESS_BYTES:
                raise DiscoveryWitnessLimitError(
                    f"discovery witness exceeds {MAX_DISCOVERY_WITNESS_BYTES} serialized bytes"
                )
            append(canonical_json(value).encode("utf-8"))

        def append_list(values: tuple[Any, ...]) -> None:
            append(b"[")
            for index, value in enumerate(values):
                if index:
                    append(b",")
                append_value(value)
            append(b"]")

        append(b"{")
        fields: tuple[tuple[str, Any], ...] = (
            ("matches", self.matches),
            ("query", self.query),
            ("query_sha256", self.query_sha256),
            ("schema", DISCOVERY_WITNESS_SCHEMA),
            ("skipped", self.skipped),
            ("source", self.source_files),
            ("source_sha256", self.source_sha256),
            ("tool", self.tool),
        )
        for index, (name, value) in enumerate(fields):
            if index:
                append(b",")
            append_value(name)
            append(b":")
            if name == "source":
                append(b"{")
                append_value("files")
                append(b":")
                append_list(cast("tuple[Any, ...]", value))
                append(b"}")
            elif name in {"matches", "skipped"}:
                append_list(cast("tuple[Any, ...]", value))
            else:
                append_value(value)
        append(b"}")
        return bytes(encoded)


def capture_discovery_source(
    workspace: Path, tool: str, arguments: dict[str, Any]
) -> DiscoverySourceWitness:
    """Observe one discovery query independently of its registered producer.

    The returned match projection is calculated from a fresh ``os.walk``-style traversal and
    fresh file reads.  It is intentionally not imported by ``builtins.search`` or ``builtins``
    file tools.  The manager compares two captures around execution; MIRA verifies the captured
    bytes and independently projects the query over them later, so an audit does not rely on a
    stale live workspace.
    """
    deadline = _with_deadline(None)
    _check_deadline(deadline)
    root = Path(workspace).resolve()
    query = _query(tool, arguments)
    _check_deadline(deadline)
    target = _contained(root, query["path"])
    files = _source_files(root, target, deadline=deadline)
    source_facts_list: list[dict[str, Any]] = []
    scan_bytes = 0
    content_bytes = 0
    for path in files:
        fact = _file_fact(
            root,
            path,
            tool,
            include=query.get("include"),
            scan_remaining=MAX_DISCOVERY_SCAN_BYTES - scan_bytes,
            content_remaining=MAX_DISCOVERY_CONTENT_BYTES - content_bytes,
            deadline=deadline,
        )
        source_facts_list.append(fact)
        size = fact.get("size_bytes")
        if isinstance(size, int) and size >= 0 and fact.get("sha256") is not None:
            scan_bytes += size
            if scan_bytes > MAX_DISCOVERY_SCAN_BYTES:
                raise DiscoveryWitnessLimitError(
                    f"discovery source scan exceeds {MAX_DISCOVERY_SCAN_BYTES} bytes"
                )
            if tool == "grep" and "content" in fact:
                content_bytes += len(fact["content"].encode("utf-8"))
                if content_bytes > MAX_DISCOVERY_CONTENT_BYTES:
                    raise DiscoveryWitnessLimitError(
                        f"discovery grep content exceeds {MAX_DISCOVERY_CONTENT_BYTES} bytes"
                    )
        _check_deadline(deadline)
    source_facts = tuple(source_facts_list)
    source_sha256 = _source_sha256(source_facts, deadline=deadline)
    readable = {fact["path"]: fact for fact in source_facts}
    if tool == "glob":
        pattern = query["pattern"]
        matches = tuple(
            path for path in sorted(readable) if _glob_matches(pattern, path, deadline=deadline)
        )
        if len(matches) > MAX_DISCOVERY_MATCHES:
            raise DiscoveryWitnessLimitError(
                f"discovery result exceeds {MAX_DISCOVERY_MATCHES} matches"
            )
        skipped: tuple[str, ...] = ()
    elif tool == "list_files":
        matches = tuple(sorted(readable))
        if len(matches) > MAX_DISCOVERY_MATCHES:
            raise DiscoveryWitnessLimitError(
                f"discovery result exceeds {MAX_DISCOVERY_MATCHES} matches"
            )
        skipped = ()
    elif tool == "grep":
        matches, skipped = _grep_matches(query, source_facts, deadline=deadline)
    else:
        raise ValueError(f"discovery source witness does not support {tool!r}")
    witness = DiscoverySourceWitness(
        tool=tool,
        query=query,
        query_sha256=_query_sha256(query, deadline=deadline),
        source_sha256=source_sha256,
        source_files=source_facts,
        matches=matches,
        skipped=skipped,
    )
    witness.serialized_payload(deadline=deadline)
    return witness


def _query(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool == "glob":
        return {"pattern": arguments["pattern"], "path": arguments.get("path", ".")}
    if tool == "grep":
        return {
            "pattern": arguments["pattern"],
            "path": arguments.get("path", "."),
            "include": arguments.get("include"),
        }
    if tool == "list_files":
        return {"path": arguments.get("path", ".")}
    raise ValueError(f"discovery source witness does not support {tool!r}")


def _with_deadline(deadline: float | None) -> float:
    """Return a caller deadline, or start one bounded discovery phase."""
    return deadline if deadline is not None else time.monotonic() + MAX_DISCOVERY_SECONDS


def _check_deadline(deadline: float) -> None:
    """Refuse once a discovery phase has consumed its monotonic time budget."""
    if time.monotonic() >= deadline:
        raise DiscoveryWitnessLimitError(
            f"discovery source scan exceeded {MAX_DISCOVERY_SECONDS:g}-second time budget"
        )


def _json_upper_bound(value: Any) -> int:
    """Return a conservative JSON byte bound without serializing ``value``."""
    if value is None or isinstance(value, bool):
        return 5
    if isinstance(value, str):
        # ensure_ascii=False still escapes control characters; six bytes per character is
        # conservative for those escapes and avoids encoding a giant value just to reject it.
        return 6 * len(value) + 2
    if isinstance(value, (int, float)):
        return len(str(value)) + 1
    if isinstance(value, (list, tuple)):
        return 2 + sum(_json_upper_bound(item) + 1 for item in value)
    if isinstance(value, dict):
        return 2 + sum(
            _json_upper_bound(str(key)) + _json_upper_bound(item) + 2 for key, item in value.items()
        )
    return MAX_DISCOVERY_WITNESS_BYTES + 1


def _source_sha256(source_facts: tuple[dict[str, Any], ...], *, deadline: float) -> str:
    """Hash the source snapshot incrementally using the canonical files representation."""
    digest = hashlib.sha256()
    digest.update(b'{"files":[')
    for index, fact in enumerate(source_facts):
        _check_deadline(deadline)
        if index:
            digest.update(b",")
        digest.update(canonical_json(fact).encode("utf-8"))
        _check_deadline(deadline)
    digest.update(b"]}")
    _check_deadline(deadline)
    return digest.hexdigest()


def _query_sha256(query: dict[str, Any], *, deadline: float) -> str:
    """Hash a query after giving the phase one final deadline check."""
    _check_deadline(deadline)
    encoded = canonical_json(query).encode("utf-8")
    _check_deadline(deadline)
    return sha256_bytes(encoded)


def _contained(root: Path, requested: str) -> Path:
    candidate = Path(requested)
    windows = PureWindowsPath(requested)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("path must be workspace-relative")
    if ".." in candidate.parts:
        raise ValueError("path traversal is not allowed")
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace") from exc
    return resolved


def _source_files(root: Path, target: Path, *, deadline: float) -> list[Path]:
    _check_deadline(deadline)
    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        candidates = []
        for directory, dirnames, filenames in _walk(target, deadline=deadline):
            dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIR_NAMES]
            if len(candidates) + len(filenames) > MAX_DISCOVERY_SOURCE_FILES:
                raise DiscoveryWitnessLimitError(
                    f"discovery source exceeds {MAX_DISCOVERY_SOURCE_FILES} files"
                )
            candidates.extend(directory / name for name in filenames)
            _check_deadline(deadline)
    else:
        raise ValueError("path does not exist")
    # ``os.walk`` has already classified ``filenames`` as non-directories.  Avoid a second
    # ``Path.is_file`` probe here: it suppresses an ``OSError`` and would silently omit an
    # unreadable entry from the supposedly complete witness.  ``_file_fact`` keeps that entry
    # with an explicit ``unreadable`` status instead.
    contained: list[Path] = []
    for path in candidates:
        _check_deadline(deadline)
        if not path.resolve(strict=False).is_relative_to(root):
            continue
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        contained.append(path)
    _check_deadline(deadline)
    return sorted(contained, key=lambda path: path.relative_to(root).as_posix())


def _walk(directory: Path, *, deadline: float) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Yield sorted rows, refusing any enumeration error instead of dropping a subtree."""

    def refuse(error: OSError) -> None:
        """Make ``os.walk`` surface a permission or directory-read failure."""
        raise error

    _check_deadline(deadline)
    for raw_root, dirnames, filenames in os.walk(directory, topdown=True, onerror=refuse):
        _check_deadline(deadline)
        dirnames.sort()
        filenames.sort()
        yield Path(raw_root), dirnames, filenames
        _check_deadline(deadline)


def _file_fact(
    root: Path,
    path: Path,
    tool: str,
    *,
    include: str | None = None,
    scan_remaining: int = MAX_DISCOVERY_SCAN_BYTES,
    content_remaining: int = MAX_DISCOVERY_CONTENT_BYTES,
    deadline: float | None = None,
) -> dict[str, Any]:
    deadline = _with_deadline(deadline)
    _check_deadline(deadline)
    relative = path.relative_to(root).as_posix()
    try:
        stat = path.stat()
    except OSError:
        return {
            "path": relative,
            "size_bytes": None,
            "sha256": None,
            "status": "unreadable",
        }
    if scan_remaining < 0 or stat.st_size > scan_remaining:
        raise DiscoveryWitnessLimitError(
            f"discovery source scan exceeds {MAX_DISCOVERY_SCAN_BYTES} bytes"
        )
    capture_content = tool == "grep" and (
        include is None or fnmatch.fnmatch(relative.rsplit("/", 1)[-1], include)
    )
    status = "too_large" if tool == "grep" and stat.st_size > _MAX_GREP_FILE_BYTES else "readable"
    if (
        capture_content
        and status == "readable"
        and (content_remaining < 0 or stat.st_size > content_remaining)
    ):
        raise DiscoveryWitnessLimitError(
            f"discovery grep content exceeds {MAX_DISCOVERY_CONTENT_BYTES} bytes"
        )
    try:
        digest, raw, size = _stream_file(
            path,
            capture_content=capture_content and status == "readable",
            scan_remaining=scan_remaining,
            content_remaining=content_remaining,
            deadline=deadline,
        )
    except DiscoveryWitnessLimitError:
        raise
    except OSError:
        return {
            "path": relative,
            "size_bytes": stat.st_size,
            "sha256": None,
            "status": "unreadable",
        }
    if capture_content and status == "readable":
        try:
            content = raw.decode("utf-8") if raw is not None else ""
        except UnicodeDecodeError:
            status = "undecodable"
        else:
            if any(
                value and is_credential_environment_name(name) and value in content
                for name, value in os.environ.items()
            ):
                raise DiscoveryWitnessCredentialError(
                    "FS_DISCOVERY_CREDENTIAL_EXCLUDED: source content is not retained"
                )
    if tool == "grep" and size > _MAX_GREP_FILE_BYTES:
        status = "too_large"
    if size > scan_remaining:
        raise DiscoveryWitnessLimitError(
            f"discovery source scan exceeds {MAX_DISCOVERY_SCAN_BYTES} bytes"
        )
    fact: dict[str, Any] = {
        "path": relative,
        "size_bytes": size,
        "sha256": digest,
        "status": status,
    }
    if capture_content and status == "readable":
        # MIRA uses this execution-time content snapshot to recompute every regex match without
        # reading the later workspace.  It is carried by the witness artifact, never an event;
        # G1 still has to assign the artifact the private visibility required for sensitive data.
        fact["content"] = content
    return fact


def _stream_file(
    path: Path,
    *,
    capture_content: bool,
    scan_remaining: int = MAX_DISCOVERY_SCAN_BYTES,
    content_remaining: int = MAX_DISCOVERY_CONTENT_BYTES,
    deadline: float | None = None,
) -> tuple[str, bytes | None, int]:
    """Hash a file with bounded memory, retaining content only for eligible grep inputs."""
    deadline = _with_deadline(deadline)
    if scan_remaining < 0 or content_remaining < 0:
        raise DiscoveryWitnessLimitError("discovery source budget is exhausted")
    digest = hashlib.sha256()
    retained = bytearray() if capture_content else None
    total = 0
    with path.open("rb") as handle:
        while True:
            _check_deadline(deadline)
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > scan_remaining:
                raise DiscoveryWitnessLimitError(
                    f"discovery source scan exceeds {MAX_DISCOVERY_SCAN_BYTES} bytes"
                )
            digest.update(chunk)
            if retained is not None:
                if total > content_remaining:
                    raise DiscoveryWitnessLimitError(
                        f"discovery grep content exceeds {MAX_DISCOVERY_CONTENT_BYTES} bytes"
                    )
                if total <= _MAX_GREP_FILE_BYTES:
                    retained.extend(chunk)
                else:
                    # Continue hashing to retain a useful exclusion digest, but never retain a
                    # too-large grep input in the witness or in an unbounded temporary bytes value.
                    retained = None
    _check_deadline(deadline)
    return digest.hexdigest(), None if retained is None else bytes(retained), total


def _grep_matches(
    query: dict[str, Any],
    source_facts: tuple[dict[str, Any], ...],
    *,
    deadline: float,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    try:
        regex = bounded_regex.compile(query["pattern"])
    except bounded_regex.error as exc:
        raise DiscoveryWitnessLimitError(f"invalid regular expression: {exc}") from exc
    include = query["include"]
    matches: list[dict[str, Any]] = []
    skipped: list[str] = []
    for fact in source_facts:
        _check_deadline(deadline)
        relative = fact["path"]
        if include and not fnmatch.fnmatch(relative.rsplit("/", 1)[-1], include):
            continue
        if fact["status"] in {"too_large", "undecodable", "unreadable"}:
            skipped.append(relative)
            continue
        content = fact.get("content")
        if not isinstance(content, str):
            skipped.append(relative)
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            _check_deadline(deadline)
            try:
                found = regex.search(line, timeout=_REGEX_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise DiscoveryWitnessLimitError(
                    "discovery regular expression exceeded its time budget"
                ) from exc
            if found:
                matches.append({"path": relative, "line": number})
                if len(matches) > MAX_DISCOVERY_MATCHES:
                    raise DiscoveryWitnessLimitError(
                        f"discovery result exceeds {MAX_DISCOVERY_MATCHES} matches"
                    )
    return tuple(matches), tuple(skipped)


def _glob_matches(pattern: str, relative: str, *, deadline: float | None = None) -> bool:
    deadline = _with_deadline(deadline)
    _check_deadline(deadline)
    target = relative.rsplit("/", 1)[-1] if "/" not in pattern else relative
    matched = re.match(f"^{_translate_glob(pattern, deadline=deadline)}$", target) is not None
    _check_deadline(deadline)
    return matched


def _translate_glob(pattern: str, *, deadline: float | None = None) -> str:
    """Translate glob syntax independently, with ``*`` stopping at path separators."""
    i = 0
    parts: list[str] = []
    while i < len(pattern):
        if deadline is not None:
            _check_deadline(deadline)
        if pattern[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            end = _bracket_end(pattern, i, deadline=deadline)
            if end is None:
                parts.append(re.escape("["))
                i += 1
            else:
                inner = pattern[i + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                parts.append(f"[{inner}]")
                i = end + 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return "".join(parts)


def _bracket_end(pattern: str, start: int, *, deadline: float | None = None) -> int | None:
    """Return the closing bracket for one character class, including a literal first ``]``."""
    index = start + 1
    if index < len(pattern) and pattern[index] in "!^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern) and pattern[index] != "]":
        if deadline is not None:
            _check_deadline(deadline)
        index += 1
    return index if index < len(pattern) else None


__all__ = [
    "DISCOVERY_TOOLS",
    "DISCOVERY_WITNESS_SCHEMA",
    "MAX_DISCOVERY_CONTENT_BYTES",
    "MAX_DISCOVERY_MATCHES",
    "MAX_DISCOVERY_SCAN_BYTES",
    "MAX_DISCOVERY_SECONDS",
    "MAX_DISCOVERY_SOURCE_FILES",
    "MAX_DISCOVERY_WITNESS_BYTES",
    "DiscoverySourceWitness",
    "DiscoveryWitnessCredentialError",
    "DiscoveryWitnessLimitError",
    "capture_discovery_source",
]
