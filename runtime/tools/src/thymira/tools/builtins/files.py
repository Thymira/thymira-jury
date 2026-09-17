"""Workspace-contained file tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import BaseModel, Field

from thymira.events import sha256_bytes, sha256_text
from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind
from thymira.tools.artifact_media import (
    infer_artifact_media_type,
    infer_text_artifact_media_type,
)
from thymira.tools.artifact_validation import validate_report_kind
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.discovery import discovery_persistence_error, persist_discovery
from thymira.tools.builtins.file_contracts import FileToolContract
from thymira.tools.builtins.freshness import FreshnessPolicy, ReadLedger
from thymira.tools.builtins.output_bounds import (
    MAX_FOOTER_BYTES,
    MAX_LINE_CHARS,
    MAX_OUTPUT_BYTES,
    MAX_READ_BYTES,
    bound_lines,
)
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.refusals import (
    ambiguous_match_refusal,
    no_match_refusal,
    read_required_refusal,
    stale_version_refusal,
)
from thymira.tools.results import (
    FileEditValue,
    FileListingValue,
    FileReadValue,
    FileWriteValue,
)

_MAX_WRITE_BYTES = 1024 * 1024
_READ_DEFAULT_LIMIT = 2000
LIST_FILES_CAP = 100
_SCRATCH_DIR_NAME = ".thymira"


def _reject_non_finite_json(token: str) -> None:
    """Reject the non-standard numeric constants Python's JSON decoder otherwise accepts."""
    raise ValueError(f"non-finite JSON token is not valid: {token}")


def contained_path(workspace: Path, requested: str) -> Path:
    """Resolve a user path while rejecting traversal and symlink escapes."""
    candidate = Path(requested)
    windows = PureWindowsPath(requested)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive:
        raise ToolExecutionError("path must be workspace-relative")
    if ".." in candidate.parts:
        raise ToolExecutionError("path traversal is not allowed")
    root = workspace.resolve()
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolExecutionError("path escapes workspace") from exc
    return resolved


_contained = contained_path


def _decode_universal_newlines(raw: bytes, path_label: str) -> str:
    """Decode UTF-8 bytes and normalize CRLF/CR to LF, matching ``Path.read_text``'s default.

    Reading with :func:`Path.read_bytes` (needed so the freshness digest is over the exact bytes
    on disk) bypasses Python's text-mode universal-newline translation; this restores it so a
    file written with CRLF line endings (Windows' own default) still renders and matches
    ``old_string`` the same way ``path.read_text(encoding="utf-8")`` always did.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(f"{path_label!r} is not valid UTF-8: {exc}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _reject_scratch_directory(relative: str) -> None:
    """Refuse a write/edit target inside the run_python scratch directory.

    ``.thymira/`` is run_python's own scratch space (staged scripts, sandbox bookkeeping); the
    file tools have no business writing there, and letting them would make freshness/discovery
    evidence about a directory this slice deliberately keeps out of glob/grep results (see
    ``_workspace_files`` in ``search.py``) inconsistent with what a caller could ever verify.
    """
    if relative == _SCRATCH_DIR_NAME or relative.split("/", 1)[0] == _SCRATCH_DIR_NAME:
        raise ToolExecutionError(
            f"{relative!r} is inside {_SCRATCH_DIR_NAME}/, the run_python scratch directory; "
            "write_file and edit_file may not target it"
        )


class ReadFileArguments(BaseModel):
    """Arguments for read_file."""

    path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1, description="1-based first line to return.")
    limit: int = Field(
        default=_READ_DEFAULT_LIMIT,
        ge=1,
        le=20_000,
        description="Maximum number of lines to return. Defaults to 2000.",
    )


class WriteFileArguments(BaseModel):
    """Arguments for write_file."""

    path: str = Field(min_length=1)
    content: str
    description: Description = DESCRIPTION_FIELD
    kind: ArtifactKind | None = Field(
        default=None,
        description="When set, also register the written file as an Artifact of this kind.",
    )


class EditFileArguments(BaseModel):
    """Arguments for edit_file."""

    path: str = Field(min_length=1)
    old_string: str = Field(
        min_length=1, description="Literal text to replace. Must match exactly."
    )
    new_string: str = Field(
        description="Literal replacement text. Use an empty string to delete the match."
    )
    replace_all: bool = Field(
        default=False,
        description=(
            "Replace all matches. Defaults to false; then old_string must appear exactly once."
        ),
    )
    description: Description = DESCRIPTION_FIELD


class ListFilesArguments(BaseModel):
    """Arguments for list_files."""

    path: str = "."


def _read_capability() -> ToolCapability:
    return ToolCapability(
        id="read_file",
        data_access=("workspace",),
        external_effects=(),
    )


def _write_capability() -> ToolCapability:
    return ToolCapability(
        id="write_file",
        data_access=("workspace",),
        side_effects=("workspace_write",),
        external_effects=(),
    )


def _list_capability() -> ToolCapability:
    return ToolCapability(
        id="list_files",
        data_access=("workspace",),
        external_effects=(),
    )


def _read_contract() -> FileToolContract:
    return FileToolContract(
        line_numbering="none",
        paging=True,
        reports_total=True,
        bounded_output=True,
        discovery_artifact=False,
        mutates_workspace=False,
        requires_prior_read=False,
    )


@dataclass(frozen=True, slots=True)
class ReadFile:
    """Read a text file inside the invocation workspace."""

    name: str = "read_file"
    description: str = (
        "Read a UTF-8 text file from the workspace and return its content, with no added line "
        "numbers. Use offset and limit to page through a large file; a partial or bounded "
        'window ends with a bracket such as "[showing lines A-B of N]", naming the total line '
        "count and any character/byte truncation. Use profile_dataset, not read_file, to "
        "understand a registered dataset."
    )
    arguments_model: type[BaseModel] = ReadFileArguments
    result_model: type[BaseModel] = FileReadValue
    capability: ToolCapability = field(default_factory=_read_capability)
    ledger: ReadLedger = field(default_factory=ReadLedger)
    contract: FileToolContract = field(default_factory=_read_contract)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Read the selected, bounded window of the file, recording every cut as a fact."""
        root = Path(invocation.workspace).resolve()
        path = _contained(root, arguments["path"])
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {arguments['path']!r}: {exc}") from exc
        content = _decode_universal_newlines(raw, arguments["path"])
        relative = path.relative_to(root).as_posix()
        self.ledger.record(
            run_id=invocation.run_id,
            workspace=str(root),
            relative_path=relative,
            sha256=sha256_bytes(raw),
        )
        offset = int(arguments.get("offset", 1))
        limit = int(arguments.get("limit", _READ_DEFAULT_LIMIT))
        lines = content.splitlines(keepends=True)
        total = len(lines)
        window = lines[offset - 1 : offset - 1 + limit]
        if not window:
            text = f"[no lines at offset {offset}; the file has {total} lines]"
            return ToolResult(
                success=True,
                stdout=text,
                value=FileReadValue(
                    text=text,
                    path=arguments["path"],
                    content="",
                    offset=offset,
                    lines_returned=0,
                    total_lines=total,
                    bytes_returned=0,
                    truncated=True,
                ),
            )
        stripped = [line.removesuffix("\n") for line in window]
        if (
            offset == 1
            and len(window) == total
            and len(content.encode("utf-8")) <= MAX_READ_BYTES
            and all(len(line) <= MAX_LINE_CHARS for line in stripped)
        ):
            # The exact fast path includes the file's final newline, so check its bytes rather
            # than relying on ``bound_lines``' join cost, which has no trailing separator.
            return ToolResult(
                success=True,
                stdout=content,
                value=FileReadValue(
                    text=content,
                    path=arguments["path"],
                    content=content,
                    offset=offset,
                    lines_returned=total,
                    total_lines=total,
                    bytes_returned=len(content.encode("utf-8")),
                    truncated=False,
                ),
            )
        rendered = bound_lines(stripped, max_bytes=MAX_READ_BYTES - MAX_FOOTER_BYTES - 1)
        shown_to = offset - 1 + rendered.rendered_count
        body = rendered.text
        if body and not body.endswith("\n"):
            body += "\n"
        clauses = [f"showing lines {offset}-{shown_to} of {total}"]
        if rendered.lines_truncated:
            noun = "line" if rendered.lines_truncated == 1 else "lines"
            clauses.append(
                f"{rendered.lines_truncated} {noun} truncated at {MAX_LINE_CHARS} characters"
            )
        if rendered.byte_bounded:
            clauses.append(f"output bounded at {MAX_READ_BYTES} bytes")
        text = f"{body}[{'; '.join(clauses)}]"
        return ToolResult(
            success=True,
            stdout=text,
            value=FileReadValue(
                text=text,
                path=arguments["path"],
                content=body,
                offset=offset,
                lines_returned=rendered.rendered_count,
                total_lines=total,
                bytes_returned=len(body.encode("utf-8")),
                truncated=True,
            ),
        )


def _write_contract() -> FileToolContract:
    return FileToolContract(
        line_numbering="none",
        paging=False,
        reports_total=False,
        bounded_output=False,
        discovery_artifact=False,
        mutates_workspace=True,
        requires_prior_read=True,
    )


@dataclass(frozen=True, slots=True)
class WriteFile:
    """Write a bounded UTF-8 text file inside the invocation workspace.

    A plain write is workspace-only, same as before. Passing `kind` additionally registers the
    file as an `Artifact` (`ArtifactStore.save_batch`) so it gets a content hash in the manifest
    and a `tool.completed`/`artifact.created` pair the `ToolManager`'s before/after diff (TOOL-04)
    can pick up -- the only way a tool without its own artifact-producing logic (e.g. a plot
    written by an agent through `run_python` + `write_file`, THY-21) can produce a real artifact.

    ``freshness`` defaults to *off* on a directly constructed instance -- most of this
    repository's tests build tools this way, with no shared :class:`ReadLedger` and no
    ``read_file`` call to satisfy -- so their behavior is unchanged. The production registry
    (:func:`~thymira.tools.builtins.run_python.builtins_registry`) constructs its own
    ``WriteFile`` with the shared ledger and freshness turned on explicitly (F3.3).
    """

    name: str = "write_file"
    description: str = (
        "Create or fully replace a UTF-8 text file inside the workspace (mutates the "
        "workspace). Overwriting an existing file requires a prior read_file of its current "
        "content in this run -- an unread or stale target is refused with FS_READ_REQUIRED or "
        "FS_STALE_VERSION, naming the fix; a brand-new path needs no prior read. To change an "
        "existing file, read it and provide its complete replacement content. Pass kind to "
        "register the file as an Artifact."
    )
    arguments_model: type[BaseModel] = WriteFileArguments
    result_model: type[BaseModel] = FileWriteValue
    capability: ToolCapability = field(default_factory=_write_capability)
    ledger: ReadLedger = field(default_factory=ReadLedger)
    freshness: FreshnessPolicy = field(
        default_factory=lambda: FreshnessPolicy(require_read_before_edit=False)
    )
    contract: FileToolContract = field(default_factory=_write_contract)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Write the selected file after containment, freshness and size checks."""
        root = Path(invocation.workspace).resolve()
        path = _contained(root, arguments["path"])
        relative = path.relative_to(root).as_posix()
        _reject_scratch_directory(relative)
        content = arguments["content"]
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_WRITE_BYTES:
            raise ToolExecutionError(f"write exceeds {_MAX_WRITE_BYTES} bytes")
        kind = arguments.get("kind")
        artifact_media_type = None
        if kind is not None:
            try:
                validate_report_kind(arguments["path"], kind)
            except ValueError as exc:
                raise ToolExecutionError(str(exc)) from exc
            artifact_media_type = infer_text_artifact_media_type(arguments["path"])
            if (
                artifact_media_type is None
                and infer_artifact_media_type(arguments["path"]) is not None
            ):
                raise ToolExecutionError(
                    f"write_file cannot register a binary artifact at {arguments['path']!r}; "
                    "use a tool that produces binary bytes"
                )
            if artifact_media_type == "application/json":
                try:
                    json.loads(content, parse_constant=_reject_non_finite_json)
                except ValueError as exc:
                    raise ToolExecutionError(
                        f"write_file cannot register invalid JSON at {arguments['path']!r}"
                    ) from exc
        existed_before = path.exists()
        current_sha256 = sha256_bytes(path.read_bytes()) if existed_before else None
        reason = self.freshness.stale_reason(
            self.ledger,
            run_id=invocation.run_id,
            workspace=str(root),
            relative_path=relative,
            existed_before=existed_before,
            current_sha256=current_sha256,
        )
        if reason == "unread":
            raise ToolExecutionError(read_required_refusal(arguments["path"]))
        if reason == "stale":
            raise ToolExecutionError(stale_version_refusal(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        self.ledger.record(
            run_id=invocation.run_id,
            workspace=str(root),
            relative_path=relative,
            sha256=sha256_bytes(encoded),
        )
        artifact_ids: tuple[str, ...] = ()
        if kind is not None:
            artifact = invocation.artifact_store.save_batch(
                {arguments["path"]: encoded},
                produced_by=invocation.agent_id,
                kind=kind,
                media_type=artifact_media_type,
            )[0]
            artifact_ids = (artifact.id,)
        text = json.dumps({"path": arguments["path"], "bytes": len(encoded)}, sort_keys=True)
        return ToolResult(
            success=True,
            stdout=text,
            value=FileWriteValue(
                text=text,
                path=arguments["path"],
                bytes_written=len(encoded),
                artifact_id=artifact_ids[0] if artifact_ids else None,
            ),
            artifact_ids=artifact_ids,
        )


def _edit_capability() -> ToolCapability:
    return ToolCapability(
        id="edit_file",
        data_access=("workspace",),
        side_effects=("workspace_write",),
        external_effects=(),
        reversibility="reversible",
    )


def _edit_contract() -> FileToolContract:
    return FileToolContract(
        line_numbering="none",
        paging=False,
        reports_total=False,
        bounded_output=False,
        discovery_artifact=False,
        mutates_workspace=True,
        requires_prior_read=True,
    )


@dataclass(frozen=True, slots=True)
class EditFile:
    """Replace literal text inside an existing workspace file.

    ``freshness`` defaults to *off* on a directly constructed instance, for the same reason as
    :class:`WriteFile`; the production registry turns it on explicitly.
    """

    name: str = "edit_file"
    description: str = (
        "Edit an existing UTF-8 text file inside the workspace by replacing literal text "
        "(mutates the workspace). Requires a prior read_file of the file's current content in "
        "this run -- an unread or stale target is refused with FS_READ_REQUIRED or "
        "FS_STALE_VERSION. old_string must match exactly and, unless replace_all is true, "
        "appear exactly once; a zero-match or ambiguous-match refusal names the fix."
    )
    arguments_model: type[BaseModel] = EditFileArguments
    result_model: type[BaseModel] = FileEditValue
    capability: ToolCapability = field(default_factory=_edit_capability)
    ledger: ReadLedger = field(default_factory=ReadLedger)
    freshness: FreshnessPolicy = field(
        default_factory=lambda: FreshnessPolicy(require_read_before_edit=False)
    )
    contract: FileToolContract = field(default_factory=_edit_contract)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Apply the literal replacement after containment, freshness and uniqueness checks."""
        root = Path(invocation.workspace).resolve()
        path = _contained(root, arguments["path"])
        relative = path.relative_to(root).as_posix()
        _reject_scratch_directory(relative)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {arguments['path']!r}: {exc}") from exc
        current_sha256 = sha256_bytes(raw)
        reason = self.freshness.stale_reason(
            self.ledger,
            run_id=invocation.run_id,
            workspace=str(root),
            relative_path=relative,
            existed_before=True,
            current_sha256=current_sha256,
        )
        if reason == "unread":
            raise ToolExecutionError(read_required_refusal(arguments["path"]))
        if reason == "stale":
            raise ToolExecutionError(stale_version_refusal(arguments["path"]))
        content = _decode_universal_newlines(raw, arguments["path"])
        old, new = arguments["old_string"], arguments["new_string"]
        count = content.count(old)
        if count == 0:
            raise ToolExecutionError(no_match_refusal(arguments["path"]))
        replace_all = arguments.get("replace_all", False)
        if count > 1 and not replace_all:
            raise ToolExecutionError(ambiguous_match_refusal(arguments["path"], count))
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_WRITE_BYTES:
            raise ToolExecutionError(f"edit exceeds {_MAX_WRITE_BYTES} bytes")
        path.write_bytes(encoded)
        self.ledger.record(
            run_id=invocation.run_id,
            workspace=str(root),
            relative_path=relative,
            sha256=sha256_bytes(encoded),
        )
        replaced = count if replace_all else 1
        noun = "replacement" if replaced == 1 else "replacements"
        text = f"{replaced} {noun} in {arguments['path']}"
        return ToolResult(
            success=True,
            stdout=text,
            value=FileEditValue(
                text=text,
                path=arguments["path"],
                replacements=replaced,
            ),
        )


def _list_contract() -> FileToolContract:
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
class ListFiles:
    """List regular files under a workspace-contained directory, ordered by path."""

    name: str = "list_files"
    description: str = (
        "List regular files below a workspace directory, ordered by path, at most 100 shown. "
        "The UTF-8 JSON result is bounded at 32 KiB; it reports the total file count and "
        "persists the complete ordered list as a discovery artifact referenced in artifact_ids, "
        "even when the shown list is capped."
    )
    arguments_model: type[BaseModel] = ListFilesArguments
    result_model: type[BaseModel] = FileListingValue
    capability: ToolCapability = field(default_factory=_list_capability)
    contract: FileToolContract = field(default_factory=_list_contract)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return the bounded, path-ordered file list and persist the complete one."""
        root = Path(invocation.workspace).resolve()
        directory = _contained(root, arguments["path"])
        if not directory.is_dir():
            raise ToolExecutionError("path is not a directory")
        matches = sorted(
            candidate.relative_to(root).as_posix()
            for candidate in directory.rglob("*")
            if candidate.is_file()
            and candidate.resolve(strict=False).is_relative_to(root)
            and _SCRATCH_DIR_NAME not in candidate.relative_to(root).parts
            and ".git" not in candidate.relative_to(root).parts
        )
        # ``list_files`` returns JSON rather than a line-oriented footer. Keep a genuine prefix
        # of complete path values (never a character-truncated path) and budget the serialized
        # envelope as well as the paths themselves. The discovery artifact is persisted after
        # this selection, so its rendered locations and the caller-facing JSON describe exactly
        # the same prefix.
        shown = matches[:LIST_FILES_CAP]
        budget = MAX_OUTPUT_BYTES - MAX_FOOTER_BYTES
        locations: list[str] = []
        rendered = bound_lines([], max_bytes=budget)
        line_bounded = False
        byte_bounded = False
        for candidate in shown:
            if len(candidate) > MAX_LINE_CHARS:
                line_bounded = True
                break
            candidate_locations = [*locations, candidate]
            candidate_rendered = bound_lines(candidate_locations, max_bytes=budget)
            if candidate_rendered.rendered_count != len(candidate_locations):
                byte_bounded = True
                break
            # Budget the actual JSON envelope too. Use both worst-case metadata branches while
            # selecting the prefix: a successful call adds a 41-character artifact id, while a
            # failed store adds a bounded diagnostic, and either must fit under the final limit.
            candidate_payload = {
                "files": candidate_locations,
                "shown": len(candidate_locations),
                "total": len(matches),
                "truncated": len(candidate_locations) < len(matches),
                "order": "path",
                "output_bounded": True,
                "discovery_artifact_id": "artifact_" + ("0" * 32),
                "discovery_persistence_error": ("FS_DISCOVERY_PERSISTENCE: " + ("x" * 256)),
            }
            if len(json.dumps(candidate_payload, sort_keys=True).encode("utf-8")) > budget:
                byte_bounded = True
                break
            locations = candidate_locations
            rendered = candidate_rendered
        persisted = persist_discovery(
            invocation,
            tool="list_files",
            query={"path": arguments["path"]},
            order="path",
            matches=matches,
            rendered_text=rendered.text,
            rendered_locations=locations,
            skipped=(),
        )
        payload: dict[str, Any] = {
            "files": locations,
            "shown": len(locations),
            "total": len(matches),
            "truncated": len(locations) < len(matches),
            "order": "path",
        }
        if rendered.lines_truncated or line_bounded:
            payload["lines_truncated"] = rendered.lines_truncated + int(line_bounded)
        if rendered.byte_bounded or byte_bounded:
            payload["output_bounded"] = True
        artifact_ids: tuple[str, ...] = ()
        error: str | None = None
        if persisted.artifact_id is not None:
            payload["discovery_artifact_id"] = persisted.artifact_id
            artifact_ids = (persisted.artifact_id,)
        else:
            error = discovery_persistence_error(persisted.failure)
            payload["discovery_persistence_error"] = error
        stdout = json.dumps(payload, sort_keys=True)
        return ToolResult(
            success=error is None,
            stdout=stdout,
            artifact_ids=artifact_ids,
            result_sha256=sha256_text(rendered.text),
            error=error,
            value=FileListingValue(
                text=stdout,
                paths=tuple(locations),
                total_count=len(matches),
                omitted_count=max(0, len(matches) - len(locations)),
            ),
        )


__all__ = [
    "DESCRIPTION_FIELD",
    "LIST_FILES_CAP",
    "EditFile",
    "EditFileArguments",
    "ListFiles",
    "ReadFile",
    "WriteFile",
]
