"""Execute a workspace Jupyter notebook through the sandbox and register the executed copy.

A notebook is code, so this tool is gated exactly like ``run_python`` (``code_execution``,
``workspace_write``, the Run's sandbox mode) and a human reviews each execution under
``GOV-008``. What it adds over ``run_python`` is the deliverable: the notebook the Coding agent
wrote is executed cell by cell in a fresh kernel, and the executed copy -- every cell's outputs,
plots included, inline in the ``.ipynb`` JSON -- is registered
as a ``kind="code"`` Artifact with lineage to the source ``write_file`` registered, so a reviewer
can open the result and MIRA can verify which source produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind, SandboxMode, new_id
from thymira.state import ArtifactWrite
from thymira.tools.artifact_validation import validate_artifact_bytes
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.files import contained_path
from thymira.tools.builtins.subprocess_command import (
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
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import ProcessToolValue
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, StagedInput

_MAX_SOURCE_BYTES = 1024 * 1024
"""A notebook the agent wrote with ``write_file``, so the same ceiling as that tool's text."""

_MAX_EXECUTED_BYTES = 16 * 1024 * 1024
"""Executed notebooks carry their plots inline as base64; bounded like the PDF export's HTML."""

_NOTEBOOK_MEDIA_TYPE = "application/x-ipynb+json"

_DEFAULT_TIMEOUT_S = 120.0
_MAX_TIMEOUT_S = 600.0

# The driver runs inside the sandbox, in the workspace: the kernel is started from the same
# interpreter the driver runs on (no kernelspec lookup on the host), one cell error stops the
# execution with a non-zero exit, and the executed notebook is written only on success.
_DRIVER = """\
import os
import sys

# The sandbox hands the child only PATH and SYSTEMROOT: no home directory, and jupyter refuses
# to start without one. Everything jupyter and the kernel write goes under the workspace.
home = os.path.join(os.getcwd(), ".thymira", "jupyter-home")
os.makedirs(home, exist_ok=True)
for name in (
    "HOME",
    "USERPROFILE",
    "JUPYTER_DATA_DIR",
    "JUPYTER_RUNTIME_DIR",
    "JUPYTER_CONFIG_DIR",
    "IPYTHONDIR",
    "MPLCONFIGDIR",
):
    os.environ[name] = home

import nbformat
from jupyter_client import KernelManager
from nbclient import NotebookClient

source, output, timeout = {source!r}, {output!r}, {timeout!r}
notebook = nbformat.read(source, as_version=4)
manager = KernelManager(
    kernel_cmd=[sys.executable, "-m", "ipykernel_launcher", "-f", "{{connection_file}}"]
)
client = NotebookClient(
    notebook,
    km=manager,
    timeout=timeout,
    allow_errors=False,
    resources={{"metadata": {{"path": "."}}}},
)
client.execute()
nbformat.write(notebook, output)
"""


class RunNotebookValue(ProcessToolValue):
    """The executed notebook, its registered artifacts and the sandbox's process facts."""

    source_path: str = Field(min_length=1)
    path: str = Field(min_length=1)
    cells_executed: int = Field(ge=0)
    artifact_id: str | None = Field(default=None, min_length=1)


class RunNotebookArguments(BaseModel):
    """Validated arguments for run_notebook."""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1, description="Workspace-relative .ipynb file to execute.")
    path: str | None = Field(
        default=None,
        description="Workspace-relative path for the executed copy; defaults to "
        "<source>.executed.ipynb beside the source.",
    )
    description: Description = DESCRIPTION_FIELD
    timeout_s: float = Field(default=_DEFAULT_TIMEOUT_S, gt=0, le=_MAX_TIMEOUT_S)


def _notebook_name(root: Path, requested: object) -> tuple[str, Path]:
    """Return one contained ``.ipynb`` path and its workspace-relative name."""
    name = str(requested).replace("\\", "/")
    if not name.casefold().endswith(".ipynb"):
        raise ToolExecutionError("run_notebook paths must end in .ipynb")
    path = contained_path(root, name)
    return path.relative_to(root).as_posix(), path


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise ToolExecutionError(f"{label} exceeds {limit} bytes")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ToolExecutionError(f"cannot read {label}: {exc}") from exc
    if len(data) > limit:
        raise ToolExecutionError(f"{label} exceeds {limit} bytes")
    return data


def _code_cell_count(raw: bytes, label: str) -> int:
    """Validate the notebook is JSON with a cell list and count its code cells."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ToolExecutionError(f"{label} is not a valid notebook: {exc}") from exc
    cells = document.get("cells") if isinstance(document, dict) else None
    if not isinstance(cells, list):
        raise ToolExecutionError(f"{label} is not a valid notebook: no cells list")
    return sum(1 for cell in cells if isinstance(cell, dict) and cell.get("cell_type") == "code")


@dataclass(frozen=True, slots=True)
class RunNotebook:
    """Execute a workspace ``.ipynb`` in a fresh kernel and register the executed copy."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    name: str = "run_notebook"
    description: str = (
        "Execute a Jupyter notebook (.ipynb) already in the workspace, top to bottom, in a fresh "
        "kernel through the runtime-configured sandbox, and always register the executed copy "
        '(with every cell output inline) as a kind="code" Artifact. Write the '
        "notebook JSON with write_file first; run_notebook reads it back. A failing cell stops "
        'the execution and is reported as "[exit code: N]" with the traceback; fix the notebook '
        "and run it again rather than retrying unchanged."
    )
    arguments_model: type[BaseModel] = RunNotebookArguments
    result_model: type[BaseModel] = RunNotebookValue
    _capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="run_notebook",
            risk_tags=("code_execution",),
            data_access=("workspace",),
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
        """Stage the driver, execute the notebook in the sandbox and publish the executed copy."""
        mode = requested_sandbox_mode(self.capability)
        if refusal := read_only_staging_refusal(mode):
            return refusal
        workspace = Path(invocation.workspace).resolve()
        source_name, source_path = _notebook_name(workspace, arguments["source_path"])
        requested_output = (
            arguments.get("path") or source_name[: -len(".ipynb")] + ".executed.ipynb"
        )
        output_name, output_path = _notebook_name(workspace, requested_output)
        if output_name == source_name:
            raise ToolExecutionError("run_notebook must not overwrite its own source notebook")
        raw_source = _read_bounded(source_path, _MAX_SOURCE_BYTES, "source notebook")
        cells = _code_cell_count(raw_source, "source notebook")

        script_dir = workspace / ".thymira"
        if refusal := link_shaped_refusal(mode, workspace, script_dir, output_path.parent):
            return refusal
        ensure_workspace_root(workspace)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / f"{new_id('tool')}.py"
        timeout_s = float(arguments.get("timeout_s", _DEFAULT_TIMEOUT_S))
        staged = StagedInput(
            script_path.relative_to(workspace).as_posix(),
            _DRIVER.format(source=source_name, output=output_name, timeout=timeout_s).encode(
                "utf-8"
            ),
        )
        run = self.sandbox.run(
            python_workspace_command(workspace, script_path),
            workspace=workspace,
            mode=mode,
            timeout_s=timeout_s,
            staged_inputs=(staged,),
        )
        if run.spec is None:
            persist_for_uninstrumented_backend(workspace, (staged,))
        result = with_sandbox_evidence(
            ToolResult(
                success=run.exit_code == 0,
                value=RunNotebookValue(
                    text=run.stdout,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                    source_path=source_name,
                    path=output_name,
                    cells_executed=cells if run.exit_code == 0 else 0,
                ),
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                error=None if run.exit_code == 0 else (run.stderr or "notebook execution failed"),
            ),
            run,
            timeout_s=timeout_s,
        )
        if not result.success:
            return result
        try:
            executed = _read_bounded(output_path, _MAX_EXECUTED_BYTES, "executed notebook")
            _code_cell_count(executed, "executed notebook")
            validate_artifact_bytes(output_name, executed, _NOTEBOOK_MEDIA_TYPE)
            # The source was registered by the write_file that produced it, under the kind the
            # plan required; re-registering it here superseded that revision with a different
            # kind and made THY's required-artifact check miss it. Only the executed copy is
            # published, with lineage to the registered source when there is one. It is code
            # with its outputs, not a REPORT: MIRA's A18 verifies report media it can read
            # (Markdown, PDF, JSON, CSV) and rightly refuses a notebook as one.
            source_artifact = invocation.artifact_store.get(source_name)
            (executed_artifact,) = invocation.artifact_store.save_artifact_batch(
                (
                    ArtifactWrite(
                        name=output_name,
                        data=executed,
                        kind=ArtifactKind.CODE,
                        media_type=_NOTEBOOK_MEDIA_TYPE,
                        input_artifact_ids=(
                            (source_artifact.id,) if source_artifact is not None else ()
                        ),
                    ),
                ),
                produced_by=invocation.agent_id,
            )
        except (OSError, ToolExecutionError, ValueError) as exc:
            return replace(result, success=False, error=str(exc))
        # The model reads the text: an empty stdout (the driver prints nothing) read as a failure
        # and sent a real Run into a rewrite-and-rerun loop. Say plainly what happened.
        text = json.dumps(
            {
                "status": "executed",
                "source_path": source_name,
                "path": output_name,
                "cells_executed": cells,
                "artifact_id": executed_artifact.id,
            },
            sort_keys=True,
        )
        value = result.value
        if isinstance(value, RunNotebookValue):
            value = value.model_copy(update={"artifact_id": executed_artifact.id, "text": text})
        return replace(result, value=value, stdout=text, artifact_ids=(executed_artifact.id,))


__all__ = ["RunNotebook", "RunNotebookArguments", "RunNotebookValue"]
