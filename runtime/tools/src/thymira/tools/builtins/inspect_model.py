"""Model inspection evidence tool -- reports a pipeline through its final estimator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind, SandboxMode, new_id
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.subprocess_command import (
    ensure_workspace_root,
    link_shaped_refusal,
    persist_for_uninstrumented_backend,
    python_workspace_command,
    read_only_staging_refusal,
    with_sandbox_evidence,
    workspace_relative_path,
)
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.model_sidecars import (
    MAX_SIDECAR_BYTES,
    load_bounded_artifact,
    read_bounded_json,
)
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import ModelInspectionValue
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, StagedInput

_INSPECT_TIMEOUT_S = 60.0


class InspectModelArguments(BaseModel):
    """Arguments for model inspection."""

    model_config = ConfigDict(extra="forbid")

    model_artifact: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD


class _InspectionPayload(BaseModel):
    """Strict schema for the inspection child protocol."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    class_name: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    pipeline_steps: tuple[tuple[str, str], ...] | None
    input_signature: "_InputSignature"
    output_signature: "_OutputSignature"
    feature_importances: tuple[float, ...] | None = None
    coefficients: tuple[tuple[float, ...], ...] | None = None


class _InputSignature(BaseModel):
    """Input-shape facts returned by the inspection child."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    n_features_in: StrictInt | None
    feature_names_in: tuple[str, ...] | None
    encoded_feature_names: tuple[str, ...] | None


class _OutputSignature(BaseModel):
    """Output-shape facts returned by the inspection child."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    classes: tuple[JsonValue, ...] | None
    n_outputs: StrictInt | None


_InspectionPayload.model_rebuild()


@dataclass(frozen=True, slots=True)
class InspectModel:
    """Inspect a serialized estimator without changing it.

    The estimator is loaded inside the injected :class:`Sandbox`: deserializing a MODEL artifact
    is arbitrary code execution (``joblib``/``pickle`` runs whatever the file encodes), so the
    load never happens in the runtime process. The confined script writes the inspection payload
    to a workspace file, which the tool reads back and records as an ``Artifact(kind=REPORT)``.
    Because it runs code, the result reports the sandbox's confinement facts, never ``None``.

    When the artifact is a scikit-learn ``Pipeline`` -- the default `run_experiment` baseline
    persists one -- the class name, parameters, feature importances and coefficients describe its
    final estimator, not the pipeline wrapper: a caller asking what kind of model this is means the
    classifier, never the preprocessing that feeds it. The pipeline's own steps and the feature
    names its encoding step produces are reported alongside, so that evidence is not lost either.
    """

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    name: str = "inspect_model"
    description: str = (
        "Report estimator type, parameters and feature weights; a pipeline is reported through "
        "its final estimator, alongside its steps and encoded feature names."
    )
    arguments_model: type[BaseModel] = InspectModelArguments
    result_model: type[BaseModel] = ModelInspectionValue
    _capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="inspect_model",
            risk_tags=("code_execution",),
            data_access=("model",),
            side_effects=("workspace_write",),
            external_effects=(),
        )
    )

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Load a model artifact under the sandbox and summarize it."""
        mode = requested_sandbox_mode(self.capability)
        if refusal := read_only_staging_refusal(mode):
            return refusal
        workspace = Path(invocation.workspace).resolve()
        work_dir = workspace / ".thymira"
        model_path = work_dir / "inspect_model.joblib"
        report_path = work_dir / "inspection.json"
        script_path = work_dir / f"{new_id('tool')}.py"
        if refusal := link_shaped_refusal(
            mode, workspace, work_dir, model_path, report_path, script_path
        ):
            return refusal
        ensure_workspace_root(workspace)
        model_bytes = load_bounded_artifact(
            invocation.artifact_store,
            arguments["model_artifact"],
            label="model artifact",
        )
        report_path.unlink(missing_ok=True)
        command = python_workspace_command(workspace, script_path)
        script_bytes = _inspection_code(
            workspace_relative_path(workspace, model_path),
            workspace_relative_path(workspace, report_path),
        ).encode("utf-8")
        staged_inputs = (
            StagedInput(model_path.relative_to(workspace).as_posix(), model_bytes),
            StagedInput(script_path.relative_to(workspace).as_posix(), script_bytes),
        )
        run = self.sandbox.run(
            command,
            workspace=workspace,
            mode=mode,
            timeout_s=_INSPECT_TIMEOUT_S,
            staged_inputs=staged_inputs,
        )
        if run.spec is None:
            persist_for_uninstrumented_backend(workspace, staged_inputs)
        if run.exit_code != 0:
            return with_sandbox_evidence(
                ToolResult(
                    success=False,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                    error=run.stderr or "model inspection failed",
                ),
                run,
                timeout_s=_INSPECT_TIMEOUT_S,
            )
        try:
            payload = read_bounded_json(
                report_path,
                _InspectionPayload,
                max_bytes=MAX_SIDECAR_BYTES,
                label="model inspection report",
            )
            artifact = invocation.artifact_store.save_json(
                f"inspection/{Path(arguments['model_artifact']).name}.json",
                payload.model_dump(mode="json"),
                produced_by=invocation.agent_id,
                kind=ArtifactKind.REPORT,
                media_type="application/json",
            )
        except (OSError, ToolExecutionError, ValueError) as exc:
            return with_sandbox_evidence(
                ToolResult(
                    success=False,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                    error=str(exc),
                ),
                run,
            )
        text = json.dumps(payload.model_dump(mode="json"), sort_keys=True)
        pipeline_steps = payload.pipeline_steps or ()
        return with_sandbox_evidence(
            ToolResult(
                success=True,
                stdout=text,
                value=ModelInspectionValue(
                    text=text,
                    class_name=payload.class_name,
                    pipeline_steps=tuple(tuple(step) for step in pipeline_steps),
                    artifact_id=artifact.id,
                ),
                exit_code=run.exit_code,
                artifact_ids=(artifact.id,),
            ),
            run,
            timeout_s=_INSPECT_TIMEOUT_S,
        )


def _inspection_code(model_path: str, report_path: str) -> str:
    """Build the confined inspection script that loads and summarizes the estimator."""
    return f"""
import json
from pathlib import Path

import joblib


def _jsonable(values):
    return {{
        key: value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        for key, value in values.items()
    }}


def _jsonable_array(values):
    if values is None:
        return None
    return [
        value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        for value in values
    ]


def _encoded_feature_names(encoder):
    if encoder is None:
        return None
    try:
        names = encoder.get_feature_names_out()
    except (AttributeError, ValueError):
        return None
    return _jsonable_array(names)


model = joblib.load(Path({json.dumps(str(model_path))}))
steps = getattr(model, "steps", None)
estimator = steps[-1][1] if steps else model
encoder = model[:-1] if steps and len(steps) > 1 else None
payload = {{
    "class_name": type(estimator).__name__,
    "parameters": _jsonable(estimator.get_params(deep=False)),
    "pipeline_steps": (
        [[name, type(step).__name__] for name, step in steps] if steps else None
    ),
    "input_signature": {{
        "n_features_in": getattr(model, "n_features_in_", None),
        "feature_names_in": _jsonable_array(getattr(model, "feature_names_in_", None)),
        "encoded_feature_names": _encoded_feature_names(encoder),
    }},
    "output_signature": {{
        "classes": _jsonable_array(getattr(model, "classes_", None)),
        "n_outputs": getattr(model, "n_outputs_", None),
    }},
}}
if hasattr(estimator, "feature_importances_"):
    payload["feature_importances"] = [float(value) for value in estimator.feature_importances_]
if hasattr(estimator, "coef_"):
    payload["coefficients"] = [[float(value) for value in row] for row in estimator.coef_]
Path({json.dumps(str(report_path))}).write_text(
    json.dumps(payload, sort_keys=True), encoding="utf-8"
)
"""


__all__ = ["InspectModel", "InspectModelArguments"]
