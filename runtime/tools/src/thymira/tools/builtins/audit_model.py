"""Deterministic model-risk evidence tool."""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from sklearn.metrics import accuracy_score, brier_score_loss, confusion_matrix, roc_auc_score

from thymira.policies import ToolCapability
from thymira.schemas import ArtifactKind, SandboxMode, new_id
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.subprocess_command import (
    ensure_workspace_root,
    link_shaped_refusal,
    persist_for_uninstrumented_backend,
    python_module_command,
    read_only_staging_refusal,
    with_sandbox_evidence,
    workspace_relative_path,
)
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.datasets import load_dataset
from thymira.tools.model_predictions import (
    ModelPredictions,
    PredictionRequest,
    read_model_predictions,
)
from thymira.tools.model_sidecars import load_bounded_artifact
from thymira.tools.models import ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import ModelAuditValue
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, SandboxRun, StagedInput

if TYPE_CHECKING:
    import polars as pl

_BINARY_CLASS_COUNT = 2


class AuditModelArguments(BaseModel):
    """Arguments for model audit evidence."""

    model_config = ConfigDict(extra="forbid")

    model_artifact: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    target_column: str = Field(min_length=1)
    protected_column: str = Field(min_length=1)
    reference_dataset: str | None = Field(default=None, min_length=1)


@dataclass(frozen=True, slots=True)
class AuditModel:
    """Compute deterministic overall and subgroup performance."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    name: str = "audit_model"
    description: str = "Produce deterministic performance and subgroup evidence for a model."
    arguments_model: type[BaseModel] = AuditModelArguments
    result_model: type[BaseModel] = ModelAuditValue
    _capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="audit_model",
            risk_tags=("code_execution",),
            data_access=("dataset", "model"),
            side_effects=("workspace_write",),
            external_effects=(),
        )
    )

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Audit a model artifact against a registered labelled dataset.

        Deserialization and prediction happen only in the injected sandbox. The child returns the
        validated feature order, class labels, predictions, and optional positive probabilities;
        this host process computes metrics and report evidence from that sidecar only.

        A row whose protected-column value is missing is excluded from every subgroup rather
        than silently folded into one (see :func:`_subgroups`); the payload's
        ``subgroups_excluded_missing_protected`` is how many rows that was, so the exclusion is
        evidence rather than a silent drop.
        """
        mode = requested_sandbox_mode(self.capability)
        if refusal := read_only_staging_refusal(mode):
            return refusal
        frame = load_dataset(invocation.artifact_store, arguments["dataset"], max_rows=1_000_000)
        protected_column = arguments["protected_column"]
        if protected_column not in frame.columns:
            raise ValueError(f"dataset is missing protected column: {protected_column}")
        target = [str(value) for value in frame.get_column(arguments["target_column"]).to_list()]
        reference = (
            load_dataset(
                invocation.artifact_store,
                arguments["reference_dataset"],
                max_rows=1_000_000,
            )
            if arguments.get("reference_dataset") is not None
            else None
        )
        workspace = Path(invocation.workspace).resolve()
        work_dir = workspace / ".thymira"
        call_id = new_id("tool")
        dataset_path = work_dir / f"{call_id}-dataset.parquet"
        model_path = work_dir / f"{call_id}-model.joblib"
        request_path = work_dir / f"{call_id}-request.json"
        response_path = work_dir / f"{call_id}-response.json"
        dataset_relative = workspace_relative_path(workspace, dataset_path)
        model_relative = workspace_relative_path(workspace, model_path)
        request_relative = workspace_relative_path(workspace, request_path)
        response_relative = workspace_relative_path(workspace, response_path)
        request = PredictionRequest(
            model_path=model_relative,
            dataset_path=dataset_relative,
            target_column=arguments["target_column"],
        )
        if refusal := link_shaped_refusal(
            mode, workspace, work_dir, dataset_path, model_path, request_path, response_path
        ):
            return refusal
        ensure_workspace_root(workspace)
        dataset_buffer = io.BytesIO()
        frame.write_parquet(dataset_buffer)
        dataset_bytes = dataset_buffer.getvalue()
        model_bytes = load_bounded_artifact(
            invocation.artifact_store,
            arguments["model_artifact"],
            label="model artifact",
        )
        request_bytes = request.model_dump_json().encode("utf-8")
        staged_inputs = (
            StagedInput(dataset_path.relative_to(workspace).as_posix(), dataset_bytes),
            StagedInput(model_path.relative_to(workspace).as_posix(), model_bytes),
            StagedInput(request_path.relative_to(workspace).as_posix(), request_bytes),
        )
        run = self.sandbox.run(
            python_module_command(
                "thymira.tools.model_prediction_worker", request_relative, response_relative
            ),
            workspace=workspace,
            mode=mode,
            timeout_s=120.0,
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
                    error=run.stderr or "model prediction failed",
                ),
                run,
                timeout_s=120.0,
            )
        try:
            workspace_relative_path(workspace, response_path)
            predictions = read_model_predictions(
                response_path, columns=frame.columns, targets=target
            )
        except (OSError, ToolExecutionError, ValueError) as exc:
            return _post_sandbox_failure(run, f"model prediction produced no valid sidecar: {exc}")
        try:
            payload = _audit_payload(frame, arguments, target, predictions, reference)
            artifact = invocation.artifact_store.save_json(
                f"audits/{Path(arguments['model_artifact']).name}.json",
                payload,
                produced_by=invocation.agent_id,
                kind=ArtifactKind.REPORT,
                media_type="application/json",
            )
        except (OSError, ToolExecutionError, ValueError) as exc:
            return _post_sandbox_failure(run, f"model audit postprocessing failed: {exc}")
        text = json.dumps(payload, sort_keys=True)
        return with_sandbox_evidence(
            ToolResult(
                success=True,
                stdout=text,
                value=ModelAuditValue(
                    text=text,
                    dataset=arguments["dataset"],
                    reference_dataset=arguments.get("reference_dataset"),
                    subgroup_count=len(payload.get("subgroups", {})),
                    artifact_id=artifact.id,
                ),
                exit_code=run.exit_code,
                artifact_ids=(artifact.id,),
            ),
            run,
            timeout_s=120.0,
        )


def _post_sandbox_failure(run: SandboxRun, error: str) -> ToolResult:
    """Return a failed result without erasing a child execution's sandbox evidence."""
    return with_sandbox_evidence(
        ToolResult(
            success=False,
            stdout=run.stdout,
            stderr=run.stderr,
            exit_code=run.exit_code,
            error=error,
        ),
        run,
        timeout_s=120.0,
    )


def _audit_payload(
    frame: pl.DataFrame,
    arguments: dict[str, Any],
    target: list[str],
    predictions: ModelPredictions,
    reference: pl.DataFrame | None,
) -> dict[str, Any]:
    """Compute host-side evidence from a validated prediction envelope."""
    features = list(predictions.feature_names)
    class_labels = list(predictions.class_labels)
    predicted = list(predictions.predictions)
    payload: dict[str, Any] = {
        "accuracy": float(accuracy_score(target, predicted)),
        "confusion_matrix": confusion_matrix(target, predicted).tolist(),
        "subgroups": _subgroups(frame, arguments, target, predicted),
        "subgroups_excluded_missing_protected": int(
            frame.get_column(arguments["protected_column"]).null_count()
        ),
    }
    probabilities = predictions.positive_probabilities
    if len(class_labels) == _BINARY_CLASS_COUNT and probabilities is not None:
        positive_label = class_labels[1]
        target_codes = [1 if value == positive_label else 0 for value in target]
        if len(set(target_codes)) == _BINARY_CLASS_COUNT:
            payload["auc"] = float(roc_auc_score(target_codes, probabilities))
            payload["calibration"] = {
                "brier_score": float(brier_score_loss(target_codes, probabilities)),
                "mean_predicted_probability": float(sum(probabilities) / len(target)),
                "positive_rate": float(sum(target_codes) / len(target_codes)),
            }
    if reference is not None:
        payload["drift"] = _drift(frame, reference, features, arguments["reference_dataset"])
    return payload


def _subgroups(
    frame: pl.DataFrame,
    arguments: dict[str, Any],
    target: list[str],
    predictions: list[str],
) -> dict[str, dict[str, float]]:
    """Calculate protected-group accuracy by slicing the full child prediction vector."""
    result: dict[str, dict[str, float]] = {}
    group = arguments["protected_column"]
    values = frame.get_column(group).to_list()
    for value in sorted({str(item) for item in values if item is not None}):
        indices = [
            index for index, item in enumerate(values) if item is not None and str(item) == value
        ]
        expected = [target[index] for index in indices]
        predicted = [predictions[index] for index in indices]
        result[value] = {
            "accuracy": float(accuracy_score(expected, predicted)),
            "count": float(len(expected)),
        }
    return result


def _drift(
    frame: pl.DataFrame,
    reference: pl.DataFrame,
    features: list[str],
    reference_name: str,
) -> dict[str, Any]:
    """Report deterministic numeric mean deltas against a reference dataset."""
    missing = sorted(set(features) - set(reference.columns))
    if missing:
        raise ValueError(f"reference dataset is missing columns: {', '.join(missing)}")
    numeric: dict[str, dict[str, float | None]] = {}
    for name in features:
        current = frame.get_column(name)
        baseline = reference.get_column(name)
        if not current.dtype.is_numeric() or not baseline.dtype.is_numeric():
            continue
        current_mean = current.mean()
        baseline_mean = baseline.mean()
        numeric[name] = {
            "current_mean": _finite(current_mean),
            "reference_mean": _finite(baseline_mean),
            "mean_delta": _finite(
                None
                if current_mean is None or baseline_mean is None
                else float(str(current_mean)) - float(str(baseline_mean))
            ),
        }
    return {"reference_dataset": reference_name, "numeric_features": numeric}


def _finite(value: Any) -> float | None:
    """Convert numeric values to JSON-safe floats."""
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


__all__ = ["AuditModel", "AuditModelArguments"]
