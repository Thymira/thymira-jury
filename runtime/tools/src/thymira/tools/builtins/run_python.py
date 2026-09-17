"""The policy-gated Python execution tool."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind, SandboxMode, new_id
from thymira.state import ArtifactWrite, canonical_artifact_name
from thymira.tools.artifact_validation import (
    MAX_GENERIC_ARTIFACT_BYTES,
    artifact_read_limit,
    resolve_artifact_media_type,
    validate_artifact_bytes,
    validate_report_kind,
)
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.audit_model import AuditModel
from thymira.tools.builtins.compare_models import CompareModels
from thymira.tools.builtins.data_analysis import AnalyzeDataset, ProfileDataset
from thymira.tools.builtins.export_pdf import ExportPdf
from thymira.tools.builtins.files import (
    EditFile,
    ListFiles,
    ReadFile,
    WriteFile,
    contained_path,
)
from thymira.tools.builtins.freshness import FreshnessPolicy, ReadLedger
from thymira.tools.builtins.git import GitCommit, GitDiff, GitLog, GitStatus
from thymira.tools.builtins.inspect_model import InspectModel
from thymira.tools.builtins.mlflow_tools import mlflow_tools
from thymira.tools.builtins.query_mlflow import QueryMlflow
from thymira.tools.builtins.query_sql import QuerySql
from thymira.tools.builtins.run_experiment import RunExperiment
from thymira.tools.builtins.run_notebook import RunNotebook
from thymira.tools.builtins.run_statistics import RunStatistics
from thymira.tools.builtins.search import Glob, Grep
from thymira.tools.builtins.subprocess_command import (
    assert_no_link_components,
    ensure_workspace_root,
    link_shaped_refusal,
    persist_for_uninstrumented_backend,
    python_workspace_command,
    read_only_staging_refusal,
    with_sandbox_evidence,
)
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.builtins.worktrees import GitWorktreeCreate, GitWorktreeList, GitWorktreeRemove
from thymira.tools.models import Tool, ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.registry import ToolRegistry
from thymira.tools.results import ProcessToolValue
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, StagedInput

_MAX_OUTPUT_ARTIFACTS = 64
_MAX_OUTPUT_ARTIFACT_TOTAL_BYTES = MAX_GENERIC_ARTIFACT_BYTES


class RunPythonOutputArtifact(BaseModel):
    """A raw file produced by ``run_python`` and published to the artifact store."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    kind: ArtifactKind = ArtifactKind.OTHER
    media_type: str | None = None


class RunPythonArguments(BaseModel):
    """Validated arguments for run_python."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    timeout_s: float = Field(default=30.0, gt=0, le=300)
    output_artifacts: tuple[RunPythonOutputArtifact, ...] = Field(
        default=(), max_length=_MAX_OUTPUT_ARTIFACTS
    )


@dataclass(frozen=True, slots=True)
class RunPython:
    """Execute a Python snippet inside the configured sandbox."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    name: str = "run_python"
    description: str = (
        "Execute Python code in a fresh interpreter through the runtime-configured sandbox with "
        "a bounded timeout. Nothing persists between calls: write files for anything the next "
        'call needs. Non-zero exits are reported as "[exit code: N]"; check that marker on '
        "every result and investigate a failure before moving on. Declare files created by the "
        "code in output_artifacts to publish their raw bytes, including binary .joblib and .png "
        "files; do not use read_file for binary outputs. Long output is truncated to its tail. "
        "A sandbox or policy refusal means execution is not permitted; do not retry another way."
    )
    arguments_model: type[BaseModel] = RunPythonArguments
    result_model: type[BaseModel] = ProcessToolValue
    _capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="run_python",
            risk_tags=("code_execution",),
            side_effects=("workspace_write",),
            external_effects=(),
            reversibility="reversible",
        )
    )

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Write a temporary script under the workspace and execute it."""
        mode = requested_sandbox_mode(self.capability)
        if refusal := read_only_staging_refusal(mode):
            return refusal
        workspace = Path(invocation.workspace).resolve()
        script_dir = workspace / ".thymira"
        # Checked before the directory is created and before anything is written into it: a
        # link planted here would otherwise carry the staged script out of the workspace.
        if refusal := link_shaped_refusal(mode, workspace, script_dir):
            return refusal
        ensure_workspace_root(workspace)
        script_path = script_dir / f"{new_id('tool')}.py"
        command = python_workspace_command(workspace, script_path)
        staged = StagedInput(
            script_path.relative_to(workspace).as_posix(),
            arguments["code"].encode("utf-8"),
        )
        run = self.sandbox.run(
            command,
            workspace=workspace,
            mode=mode,
            timeout_s=arguments.get("timeout_s", 30.0),
            staged_inputs=(staged,),
        )
        if run.spec is None:
            persist_for_uninstrumented_backend(workspace, (staged,))
        result = with_sandbox_evidence(
            ToolResult(
                success=run.exit_code == 0,
                value=ProcessToolValue(
                    text=run.stdout,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                ),
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                error=None if run.exit_code == 0 else (run.stderr or "python execution failed"),
            ),
            run,
            timeout_s=arguments.get("timeout_s", 30.0),
        )
        if not result.success or not arguments.get("output_artifacts"):
            return result
        try:
            artifact_ids = _publish_output_artifacts(invocation, arguments["output_artifacts"])
        except (OSError, ToolExecutionError, ValueError) as exc:
            return replace(result, success=False, error=str(exc))
        return replace(result, artifact_ids=artifact_ids)


def _publish_output_artifacts(  # noqa: PLR0912, PLR0915  # ordered validation is fail-closed
    invocation: ToolInvocation, output_artifacts: tuple[dict[str, Any], ...]
) -> tuple[str, ...]:
    """Read bounded declared outputs and independently validate typed contents before publishing."""
    if invocation.artifact_store is None:
        raise ToolExecutionError("output_artifacts require an artifact store")
    workspace = Path(invocation.workspace).resolve()
    attachments: list[ArtifactWrite] = []
    seen: set[str] = set()
    seen_folded: dict[str, str] = {}
    existing_names = tuple(invocation.artifact_store.manifest())
    total_bytes = 0
    for descriptor in output_artifacts:
        relative = str(descriptor["path"]).replace("\\", "/")
        candidate = Path(relative)
        if candidate.is_absolute() or PureWindowsPath(relative).is_absolute():
            raise ToolExecutionError("output artifact path must be workspace-relative")
        path = contained_path(workspace, relative)
        logical_name = path.relative_to(workspace).as_posix()
        try:
            canonical_name = canonical_artifact_name(logical_name)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        if canonical_name != logical_name:
            raise ToolExecutionError(
                f"output artifact path is not canonical: {logical_name!r} -> {canonical_name!r}"
            )
        if logical_name == ".thymira" or logical_name.startswith(".thymira/"):
            raise ToolExecutionError("output artifacts may not be stored in .thymira/")
        if logical_name in seen:
            raise ToolExecutionError(f"output artifact declared more than once: {logical_name}")
        seen.add(logical_name)
        folded_name = logical_name.casefold()
        collision = seen_folded.get(folded_name)
        if collision is not None and collision != logical_name:
            raise ToolExecutionError(
                f"output artifact {logical_name!r} collides with {collision!r}"
            )
        existing_collision = next(
            (
                existing
                for existing in existing_names
                if existing.casefold() == folded_name and existing != logical_name
            ),
            None,
        )
        if existing_collision is not None:
            raise ToolExecutionError(
                f"output artifact {logical_name!r} collides with {existing_collision!r}"
            )
        seen_folded[folded_name] = logical_name
        assert_no_link_components(workspace, path)
        if not path.is_file():
            raise ToolExecutionError(f"declared output artifact does not exist: {logical_name}")
        try:
            media_type = resolve_artifact_media_type(logical_name, descriptor.get("media_type"))
            validate_report_kind(logical_name, descriptor.get("kind", ArtifactKind.OTHER))
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        read_limit = artifact_read_limit(media_type)
        file_size = path.stat().st_size
        if file_size > read_limit:
            raise ToolExecutionError(
                f"declared output artifact {logical_name!r} exceeds {read_limit} bytes"
            )
        total_bytes += file_size
        if total_bytes > _MAX_OUTPUT_ARTIFACT_TOTAL_BYTES:
            raise ToolExecutionError(
                "declared output artifacts exceed the aggregate limit of "
                f"{_MAX_OUTPUT_ARTIFACT_TOTAL_BYTES} bytes"
            )
        with path.open("rb") as handle:
            data = handle.read(read_limit + 1)
        if len(data) > read_limit:
            raise ToolExecutionError(
                f"declared output artifact {logical_name!r} exceeds {read_limit} bytes"
            )
        try:
            validate_artifact_bytes(logical_name, data, media_type)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        attachments.append(
            ArtifactWrite(
                name=logical_name,
                data=data,
                kind=descriptor.get("kind", ArtifactKind.OTHER),
                media_type=media_type,
            )
        )
    artifacts = invocation.artifact_store.save_artifact_batch(
        attachments,
        produced_by=invocation.agent_id,
    )
    return tuple(artifact.id for artifact in artifacts)


def builtins_registry(
    *, sandbox: Sandbox | None = None, subprocess_mode: SandboxMode | None = None
) -> ToolRegistry:
    """Build the default local registry for the P3 built-in tools.

    ``read_file``, ``write_file`` and ``edit_file`` share one :class:`ReadLedger` -- constructed
    once here, per registry -- and ``write_file``/``edit_file`` are configured with the
    freshness policy turned *on* (F3.3): a write or edit to an existing file this run has not
    read since its last change is refused. Direct construction of these tools elsewhere (most of
    this repository's other tests) leaves the policy at its class-level default of *off*; this
    is the one production seam that turns it on.
    """
    backend = sandbox or LocalSubprocessSandbox()
    if subprocess_mode is not None and not isinstance(subprocess_mode, SandboxMode):
        raise TypeError("subprocess_mode must be a SandboxMode")
    read_mode = subprocess_mode or SandboxMode.READ_ONLY
    write_mode = subprocess_mode or SandboxMode.WORKSPACE_WRITE
    read_ledger = ReadLedger()
    freshness = FreshnessPolicy(require_read_before_edit=True)
    tools = tuple(
        cast("Tool", item)
        for item in (
            RunPython(sandbox=backend, mode=write_mode),
            ReadFile(ledger=read_ledger),
            WriteFile(ledger=read_ledger, freshness=freshness),
            EditFile(ledger=read_ledger, freshness=freshness),
            Glob(),
            Grep(),
            GitWorktreeCreate(sandbox=backend, mode=write_mode),
            GitWorktreeList(sandbox=backend, mode=read_mode),
            GitWorktreeRemove(sandbox=backend, mode=write_mode),
            ListFiles(),
            GitStatus(sandbox=backend, mode=read_mode),
            GitDiff(sandbox=backend, mode=read_mode),
            GitLog(sandbox=backend, mode=read_mode),
            GitCommit(sandbox=backend, mode=write_mode),
            AnalyzeDataset(),
            AuditModel(sandbox=backend, mode=write_mode),
            CompareModels(),
            ProfileDataset(),
            QuerySql(),
            RunStatistics(),
            QueryMlflow(),
            RunExperiment(sandbox=backend, mode=write_mode),
            InspectModel(sandbox=backend, mode=write_mode),
            ExportPdf(),
            RunNotebook(sandbox=backend, mode=write_mode),
            *mlflow_tools(),
        )
    )
    return ToolRegistry(tools)


__all__ = ["RunPython", "RunPythonArguments", "RunPythonOutputArtifact", "builtins_registry"]
