"""Workspace search tools: glob and grep (harness basics 1)."""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import regex as bounded_regex
from pydantic import BaseModel, Field

from thymira.events import sha256_text
from thymira.policies import ToolCapability
from thymira.tools.builtins.discovery import discovery_persistence_error, persist_discovery
from thymira.tools.builtins.file_contracts import FileToolContract
from thymira.tools.builtins.files import contained_path
from thymira.tools.builtins.output_bounds import (
    MAX_FOOTER_BYTES,
    MAX_LINE_CHARS,
    MAX_OUTPUT_BYTES,
    bound_lines,
)
from thymira.tools.discovery_witness import (
    MAX_DISCOVERY_MATCHES,
    MAX_DISCOVERY_SCAN_BYTES,
    MAX_DISCOVERY_SECONDS,
    MAX_DISCOVERY_SOURCE_FILES,
)
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import DiscoveryValue

GLOB_CAP = 100
GREP_CAP = 250
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
_REGEX_TIMEOUT_SECONDS = 0.05
MAX_GREP_SCAN_BYTES = MAX_DISCOVERY_SCAN_BYTES
MAX_GREP_SECONDS = MAX_DISCOVERY_SECONDS
MAX_GREP_MATCHES = MAX_DISCOVERY_MATCHES
MAX_GREP_SOURCE_FILES = MAX_DISCOVERY_SOURCE_FILES
_READ_CHUNK_BYTES = 64 * 1024
_EXCLUDED_DIR_NAMES = frozenset({".git", ".thymira"})


class GlobArguments(BaseModel):
    """Arguments for glob."""

    pattern: str = Field(
        min_length=1,
        description=(
            'Glob pattern to match file paths against (e.g. "**/*.py", "src/**/*.csv"). A '
            'pattern with no "/" matches the basename at any depth.'
        ),
    )
    path: str = Field(default=".", description="Directory to search in, workspace-relative.")


class GrepArguments(BaseModel):
    """Arguments for grep."""

    pattern: str = Field(min_length=1, description="Python regular expression to search for.")
    path: str = Field(default=".", description="File or directory to search, workspace-relative.")
    include: str | None = Field(
        default=None,
        description='One glob filter for which files to search (e.g. "*.py"). Not a list.',
    )


def _in_excluded_dir(parts: tuple[str, ...]) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES for part in parts)


def _workspace_files(
    root: Path,
    directory: Path,
    *,
    deadline: float | None = None,
    max_files: int | None = None,
) -> list[Path]:
    """Enumerate workspace files while keeping the bounded caller on its deadline."""
    files: list[Path] = []
    for candidate in directory.rglob("*"):
        if deadline is not None:
            _check_grep_deadline(deadline)
        if (
            candidate.is_file()
            and candidate.resolve(strict=False).is_relative_to(root)
            and not _in_excluded_dir(candidate.relative_to(root).parts)
        ):
            files.append(candidate)
            if max_files is not None and len(files) > max_files:
                raise ToolExecutionError(f"grep source exceeds {max_files} files")
    if deadline is not None:
        _check_grep_deadline(deadline)
    return files


def _check_grep_deadline(deadline: float) -> None:
    """Refuse after the producer's own monotonic execution budget expires."""
    if time.monotonic() >= deadline:
        raise ToolExecutionError(f"grep exceeded its {MAX_GREP_SECONDS:g}-second time budget")


def _read_grep_text(path: Path, *, deadline: float, scan_remaining: int) -> tuple[str, int]:
    """Read one UTF-8 input with per-file, aggregate and deadline bounds.

    One byte beyond each remaining budget is used only as an EOF sentinel.  It is never retained,
    so a file that grows after its initial ``stat`` fails closed without an unbounded read.
    """
    if scan_remaining <= 0:
        raise ToolExecutionError(f"grep scan exceeds {MAX_GREP_SCAN_BYTES} bytes")
    data = bytearray()
    total = 0
    read_limit = min(_MAX_GREP_FILE_BYTES, scan_remaining)
    with path.open("rb") as handle:
        while True:
            _check_grep_deadline(deadline)
            remaining = read_limit + 1 - total
            if remaining <= 0:
                raise ToolExecutionError("grep input exceeded its bounded read budget")
            chunk = handle.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            next_total = total + len(chunk)
            if next_total > read_limit:
                raise ToolExecutionError("grep input exceeded its bounded read budget")
            data.extend(chunk)
            total = next_total
    _check_grep_deadline(deadline)
    text = bytes(data).decode("utf-8")
    _check_grep_deadline(deadline)
    return text, total


def _discovery_contract() -> FileToolContract:
    return FileToolContract(
        line_numbering="none",
        paging=False,
        reports_total=True,
        bounded_output=True,
        discovery_artifact=True,
        mutates_workspace=False,
        requires_prior_read=False,
    )


@dataclass(frozen=True, slots=True)
class Glob:
    """Find workspace files by path pattern."""

    name: str = "glob"
    description: str = (
        "Find files in the workspace whose paths match a glob pattern, ordered by path "
        "(never by modification time). Returns file paths only, never directories; a pattern "
        'with no "/" matches the basename at any depth. At most 100 paths are shown; the '
        "UTF-8 rendering is bounded at 32 KiB including its footer. The result names the true "
        "total and persists the complete ordered list as a discovery artifact referenced in "
        "artifact_ids."
    )
    arguments_model: type[BaseModel] = GlobArguments
    result_model: type[BaseModel] = DiscoveryValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="glob", data_access=("workspace",), external_effects=()
        )
    )
    contract: FileToolContract = field(default_factory=_discovery_contract)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return matches ordered by path, capped at ``GLOB_CAP``; persist the complete list."""
        root = Path(invocation.workspace).resolve()
        directory = contained_path(root, arguments.get("path", "."))
        if not directory.is_dir():
            raise ToolExecutionError("path is not a directory")
        compiled = _compile_glob(arguments["pattern"])
        matches = sorted(
            candidate.relative_to(root).as_posix()
            for candidate in _workspace_files(root, directory)
            if _glob_matches(compiled, candidate.relative_to(root).as_posix())
        )
        shown = matches[:GLOB_CAP]
        rendered = bound_lines(shown, max_bytes=MAX_OUTPUT_BYTES - MAX_FOOTER_BYTES - 1)
        locations = shown[: rendered.rendered_count]
        persisted = persist_discovery(
            invocation,
            tool="glob",
            query={"pattern": arguments["pattern"], "path": arguments.get("path", ".")},
            order="path",
            matches=matches,
            rendered_text=rendered.text,
            rendered_locations=locations,
            skipped=(),
        )
        clauses = [
            f"glob: {rendered.rendered_count} of {len(matches)} paths shown, ordered by path"
        ]
        if rendered.lines_truncated:
            noun = "path" if rendered.lines_truncated == 1 else "paths"
            clauses.append(
                f"{rendered.lines_truncated} {noun} truncated at {MAX_LINE_CHARS} characters"
            )
        artifact_ids: tuple[str, ...] = ()
        error: str | None = None
        if persisted.artifact_id is not None:
            clauses.append(f"complete list in artifact {persisted.artifact_id}")
            artifact_ids = (persisted.artifact_id,)
        else:
            error = discovery_persistence_error(persisted.failure)
            clauses.append(f"discovery list not persisted ({error})")
        body = f"{rendered.text}\n" if rendered.text else ""
        stdout = f"{body}[{'; '.join(clauses)}]"
        return ToolResult(
            success=error is None,
            stdout=stdout,
            artifact_ids=artifact_ids,
            result_sha256=sha256_text(rendered.text),
            error=error,
            value=DiscoveryValue(
                text=stdout,
                matches=tuple(locations),
                total_count=len(matches),
                returned_count=rendered.rendered_count,
                omitted_count=max(0, len(matches) - rendered.rendered_count),
                complete_list_artifact_id=persisted.artifact_id,
            ),
        )


def _compile_glob(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Translate a glob pattern into an anchored regex.

    Returns the compiled regex together with whether a pattern lacking "/" should be matched
    against the basename only (the documented "no slash" rule) rather than the full path.
    Unlike ``fnmatch``, ``*`` and ``?`` never cross a "/" here, so ``src/*.py`` cannot
    over-match a nested file the way ``fnmatch.fnmatch`` would.
    """
    return re.compile(f"^{_translate_glob(pattern)}$"), "/" not in pattern


def _translate_glob(pattern: str) -> str:
    """Translate glob syntax to a regex body: ``*``/``?`` stop at "/", ``**`` does not."""
    i, length = 0, len(pattern)
    parts: list[str] = []
    while i < length:
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
            end = _bracket_end(pattern, i)
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


def _bracket_end(pattern: str, start: int) -> int | None:
    """Return the index of the "]" closing the bracket expression opened at ``start``."""
    j = start + 1
    if j < len(pattern) and pattern[j] in "!^":
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    while j < len(pattern) and pattern[j] != "]":
        j += 1
    return j if j < len(pattern) else None


def _glob_matches(compiled: tuple[re.Pattern[str], bool], relative_posix: str) -> bool:
    regex, basename_only = compiled
    target = relative_posix.rsplit("/", 1)[-1] if basename_only else relative_posix
    return regex.match(target) is not None


@dataclass(frozen=True, slots=True)
class Grep:
    """Search workspace file contents with a regular expression."""

    name: str = "grep"
    description: str = (
        "Search file contents in the workspace with a Python regular expression, ordered by "
        'path then line number. Returns matching lines as "path:line: text", at most 250 '
        "shown; UTF-8 output is bounded at 32 KiB including its footer. The result names the "
        "true total, the files it had to skip (too large or undecodable), and persists the "
        "complete ordered match list as a discovery artifact referenced in artifact_ids. Use "
        "read_file on a matched file for surrounding context."
    )
    arguments_model: type[BaseModel] = GrepArguments
    result_model: type[BaseModel] = DiscoveryValue
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="grep", data_access=("workspace",), external_effects=()
        )
    )
    contract: FileToolContract = field(default_factory=_discovery_contract)

    def execute(  # noqa: PLR0912, PLR0915  # scan and durable evidence stay together
        self, invocation: ToolInvocation, arguments: dict[str, Any]
    ) -> ToolResult:
        """Return matches ordered by path then line, capped at ``GREP_CAP``; persist the rest."""
        deadline = time.monotonic() + MAX_GREP_SECONDS
        _check_grep_deadline(deadline)
        root = Path(invocation.workspace).resolve()
        target = contained_path(root, arguments.get("path", "."))
        try:
            regex = bounded_regex.compile(arguments["pattern"])
        except bounded_regex.error as exc:
            raise ToolExecutionError(f"invalid regular expression: {exc}") from exc
        _check_grep_deadline(deadline)
        include = arguments.get("include")
        files = (
            [target]
            if target.is_file()
            else _workspace_files(root, target, deadline=deadline, max_files=MAX_GREP_SOURCE_FILES)
        )
        files.sort(key=lambda candidate: candidate.relative_to(root).as_posix())
        matches: list[dict[str, Any]] = []
        formatted: list[str] = []
        skipped: list[str] = []
        files_with_matches: set[str] = set()
        scan_bytes = 0
        for candidate in files:
            _check_grep_deadline(deadline)
            relative = candidate.relative_to(root).as_posix()
            if include and not fnmatch.fnmatch(relative.rsplit("/", 1)[-1], include):
                continue
            size = candidate.stat().st_size
            if size > _MAX_GREP_FILE_BYTES:
                skipped.append(relative)
                continue
            if size > MAX_GREP_SCAN_BYTES - scan_bytes:
                raise ToolExecutionError(f"grep scan exceeds {MAX_GREP_SCAN_BYTES} bytes")
            try:
                text, read_size = _read_grep_text(
                    candidate,
                    deadline=deadline,
                    scan_remaining=MAX_GREP_SCAN_BYTES - scan_bytes,
                )
            except (OSError, UnicodeDecodeError):
                skipped.append(relative)
                continue
            scan_bytes += read_size
            _check_grep_deadline(deadline)
            lines = text.splitlines()
            _check_grep_deadline(deadline)
            for number, line in enumerate(lines, start=1):
                _check_grep_deadline(deadline)
                try:
                    remaining = max(deadline - time.monotonic(), 0.000001)
                    found = regex.search(line, timeout=min(_REGEX_TIMEOUT_SECONDS, remaining))
                except TimeoutError as exc:
                    raise ToolExecutionError("regular expression exceeded its time budget") from exc
                if found:
                    if len(matches) >= MAX_GREP_MATCHES:
                        raise ToolExecutionError(f"grep result exceeds {MAX_GREP_MATCHES} matches")
                    matches.append({"path": relative, "line": number})
                    if len(formatted) < GREP_CAP:
                        formatted.append(f"{relative}:{number}: {line}")
                    files_with_matches.add(relative)
        _check_grep_deadline(deadline)
        shown_matches = matches[:GREP_CAP]
        shown_formatted = formatted[:GREP_CAP]
        _check_grep_deadline(deadline)
        rendered = bound_lines(shown_formatted, max_bytes=MAX_OUTPUT_BYTES - MAX_FOOTER_BYTES - 1)
        _check_grep_deadline(deadline)
        locations = shown_matches[: rendered.rendered_count]
        persisted = persist_discovery(
            invocation,
            tool="grep",
            query={
                "pattern": arguments["pattern"],
                "path": arguments.get("path", "."),
                "include": include,
            },
            order="path_then_line",
            matches=matches,
            rendered_text=rendered.text,
            rendered_locations=locations,
            skipped=skipped,
        )
        _check_grep_deadline(deadline)
        clauses = [
            (
                f"grep: {rendered.rendered_count} of {len(matches)} matches shown in "
                f"{len(files_with_matches)} files, ordered by path then line"
            )
        ]
        if rendered.lines_truncated:
            noun = "line" if rendered.lines_truncated == 1 else "lines"
            clauses.append(
                f"{rendered.lines_truncated} {noun} truncated at {MAX_LINE_CHARS} characters"
            )
        if skipped:
            noun = "file" if len(skipped) == 1 else "files"
            clauses.append(f"{len(skipped)} {noun} skipped (too large or undecodable)")
        artifact_ids: tuple[str, ...] = ()
        error: str | None = None
        if persisted.artifact_id is not None:
            clauses.append(f"complete list in artifact {persisted.artifact_id}")
            artifact_ids = (persisted.artifact_id,)
        else:
            error = discovery_persistence_error(persisted.failure)
            clauses.append(f"discovery list not persisted ({error})")
        body = f"{rendered.text}\n" if rendered.text else ""
        stdout = f"{body}[{'; '.join(clauses)}]"
        _check_grep_deadline(deadline)
        return ToolResult(
            success=error is None,
            stdout=stdout,
            artifact_ids=artifact_ids,
            result_sha256=sha256_text(rendered.text),
            error=error,
            value=DiscoveryValue(
                text=stdout,
                matches=tuple(shown_formatted[: rendered.rendered_count]),
                total_count=len(matches),
                returned_count=rendered.rendered_count,
                omitted_count=max(0, len(matches) - rendered.rendered_count),
                complete_list_artifact_id=persisted.artifact_id,
            ),
        )


__all__ = [
    "GLOB_CAP",
    "GREP_CAP",
    "MAX_GREP_MATCHES",
    "MAX_GREP_SCAN_BYTES",
    "MAX_GREP_SECONDS",
    "MAX_GREP_SOURCE_FILES",
    "Glob",
    "GlobArguments",
    "Grep",
    "GrepArguments",
]
