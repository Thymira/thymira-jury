"""Confined deterministic experiment training tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictFloat, field_validator

from thymira.policies import ToolCapability
from thymira.schemas import (
    ArtifactKind,
    EventType,
    Experiment,
    ExperimentStatus,
    SandboxMode,
    new_id,
)
from thymira.state import ArtifactWrite
from thymira.tools.builtins.argument_contracts import DESCRIPTION_FIELD, Description
from thymira.tools.builtins.experiment_repro import DEFAULT_TEST_SIZE, load_reproducibility_evidence
from thymira.tools.builtins.subprocess_command import (
    assert_no_link_components,
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
from thymira.tools.datasets import DatasetSchema, schema_artifact_name
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.model_sidecars import (
    MAX_MODEL_ARTIFACT_BYTES,
    MAX_SIDECAR_BYTES,
    read_bounded_file,
    read_bounded_json,
)
from thymira.tools.models import ToolEvent, ToolExecutionError, ToolInvocation, ToolResult
from thymira.tools.results import ExperimentValue
from thymira.tools.sandbox import LocalSubprocessSandbox, Sandbox, StagedInput


class RunExperimentArguments(BaseModel):
    """Arguments for the deterministic baseline experiment."""

    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(min_length=1)
    target_column: str = Field(min_length=1)
    description: Description = DESCRIPTION_FIELD
    experiment_name: str = Field(default="thymira-experiment", min_length=1)
    model_artifact_name: str | None = Field(default=None, min_length=1)
    metrics_artifact_name: str | None = Field(default=None, min_length=1)
    seed: int = Field(default=7, ge=0)
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    code: str | None = Field(default=None, max_length=100_000)

    @field_validator("model_artifact_name", "metrics_artifact_name")
    @classmethod
    def _relative_artifact_name(cls, value: str | None) -> str | None:
        """Keep optional custom artifact destinations inside the artifact store namespace."""
        if value is None:
            return None
        normalized = value.replace("\\", "/")
        if (
            PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(normalized).is_absolute()
            or PureWindowsPath(normalized).drive
            or ".." in PurePosixPath(normalized).parts
            or normalized == ".thymira"
            or normalized.startswith(".thymira/")
        ):
            raise ValueError("artifact name must be workspace-relative")
        return normalized


class _ExperimentMetrics(RootModel[dict[str, StrictFloat]]):
    """Strict JSON metrics returned by a training child."""

    model_config = ConfigDict(strict=True, allow_inf_nan=False)


@dataclass(frozen=True, slots=True)
class RunExperiment:
    """Train a baseline classifier through the injected Python sandbox."""

    sandbox: Sandbox = field(default_factory=LocalSubprocessSandbox)
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE
    name: str = "run_experiment"
    description: str = (
        "Train and evaluate a deterministic baseline classifier on a registered dataset, track "
        "it in the local experiment tracker and save the model and its metrics as artifacts. "
        "Use model_artifact_name and metrics_artifact_name when a plan requires exact output "
        "paths. "
        "Report only the metrics the result carries; never a number you have not seen in the "
        "output."
    )
    arguments_model: type[BaseModel] = RunExperimentArguments
    result_model: type[BaseModel] = ExperimentValue
    _capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(
            id="run_experiment",
            # model_training is what CR-001 matches: training on sensitive attributes needs a
            # human whatever the side-effect rules say.
            risk_tags=("code_execution", "model_training"),
            data_access=("dataset", "experiment"),
            side_effects=("workspace_write",),
            external_effects=(),
        )
    )

    @property
    def capability(self) -> ToolCapability:
        """Return the capability that records this instance's requested sandbox mode."""
        return capability_for_sandbox_mode(self._capability, self.mode)

    def execute(  # noqa: PLR0915  # ordered experiment lifecycle remains explicit
        self, invocation: ToolInvocation, arguments: dict[str, Any]
    ) -> ToolResult:
        """Train, track and persist the model and metrics artifacts."""
        prepared = _training_preflight(self.capability, Path(invocation.workspace))
        if isinstance(prepared, ToolResult):
            return prepared
        mode, workspace, work_dir = prepared
        schema = DatasetSchema.model_validate(
            invocation.artifact_store.load_json(schema_artifact_name(arguments["dataset"]))
        )
        if not schema.artifact_name.endswith(".csv"):
            raise ValueError("run_experiment currently requires a registered CSV dataset")
        dataset_path = work_dir / f"{arguments['dataset']}.csv"
        if dataset_path.exists():
            dataset_path = work_dir / f"{new_id('tool')}-{arguments['dataset']}.csv"
        dataset_bytes = invocation.artifact_store.load_bytes(schema.artifact_name)
        model_path = work_dir / "model.joblib"
        metrics_path = work_dir / "metrics.json"
        evidence_path = work_dir / f"{new_id('tool')}-reproducibility.json"
        script_path = work_dir / f"{new_id('tool')}.py"
        _validate_training_paths(
            workspace, work_dir, dataset_path, model_path, metrics_path, evidence_path, script_path
        )
        numeric_columns = [
            name
            for name, dtype in zip(schema.columns, schema.dtypes, strict=True)
            if name != arguments["target_column"] and _is_numeric_dtype(dtype)
        ]
        boolean_columns = [
            name
            for name, dtype in zip(schema.columns, schema.dtypes, strict=True)
            if name != arguments["target_column"] and dtype == "Boolean"
        ]
        started = ToolEvent(
            EventType.EXPERIMENT_STARTED,
            {"experiment_name": arguments["experiment_name"], "dataset": arguments["dataset"]},
        )
        is_default_training = arguments.get("code") is None
        code = (
            _custom_training_code(
                arguments["code"],
                workspace_relative_path(workspace, dataset_path),
                workspace_relative_path(workspace, model_path),
                workspace_relative_path(workspace, metrics_path),
                target_column=arguments["target_column"],
                seed=arguments["seed"],
            )
            if arguments.get("code") is not None
            else _training_code(
                workspace_relative_path(workspace, dataset_path),
                workspace_relative_path(workspace, model_path),
                workspace_relative_path(workspace, metrics_path),
                target_column=arguments["target_column"],
                seed=arguments["seed"],
                numeric_columns=numeric_columns,
                boolean_columns=boolean_columns,
                evidence_path=workspace_relative_path(workspace, evidence_path),
            )
        )
        command = python_workspace_command(workspace, script_path)
        script_bytes = code.encode("utf-8")
        training_script_sha256 = sha256(script_bytes).hexdigest()
        staged_inputs = (
            StagedInput(dataset_path.relative_to(workspace).as_posix(), dataset_bytes),
            StagedInput(script_path.relative_to(workspace).as_posix(), script_bytes),
        )
        run = self.sandbox.run(
            command,
            workspace=workspace,
            mode=mode,
            timeout_s=arguments["timeout_s"],
            staged_inputs=staged_inputs,
        )
        if run.spec is None:
            persist_for_uninstrumented_backend(workspace, staged_inputs)
        training = with_sandbox_evidence(
            ToolResult(
                success=run.exit_code == 0,
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                error=(
                    None if run.exit_code == 0 else (run.stderr or "experiment execution failed")
                ),
            ),
            run,
            timeout_s=arguments["timeout_s"],
        )
        if not training.success:
            return _failed_training_result(training, started, training.error)

        try:
            model_bytes, metrics = _read_training_outputs(model_path, metrics_path)
        except (OSError, ToolExecutionError, ValueError) as exc:
            error = _experiment_protocol_error(exc)
            return _failed_training_result(training, started, error)
        reproducibility = None
        if is_default_training:
            try:
                evidence = load_reproducibility_evidence(evidence_path)
            except ToolExecutionError as exc:
                return _failed_training_result(training, started, str(exc))
            if evidence.seed != arguments["seed"]:
                return _failed_training_result(
                    training,
                    started,
                    "default experiment reproducibility evidence seed does not match "
                    "the invocation",
                )
            reproducibility = evidence.to_json_dict()
            reproducibility["training_script_sha256"] = training_script_sha256
        validated_model_path, validated_metrics_path = _write_validated_training_outputs(
            work_dir, model_bytes, metrics
        )
        model_artifact_name = (
            arguments.get("model_artifact_name") or f"models/{arguments['experiment_name']}.joblib"
        )
        metrics_artifact_name = (
            arguments.get("metrics_artifact_name") or f"metrics/{arguments['experiment_name']}.json"
        )
        metrics_bytes = json.dumps(metrics, sort_keys=True).encode("utf-8")
        tracker = MlflowTracker(workspace)
        tracker_run_id = tracker.start_run(arguments["experiment_name"])
        tracker.log_param(tracker_run_id, "seed", arguments["seed"])
        tracker.log_param(tracker_run_id, "dataset", arguments["dataset"])
        for key, value in metrics.items():
            tracker.log_metric(tracker_run_id, key, float(value))
        tracker.log_artifact_bytes(tracker_run_id, validated_model_path.name, model_bytes)
        tracker.log_artifact_bytes(
            tracker_run_id,
            validated_metrics_path.name,
            metrics_bytes,
        )
        tracker.end_run(tracker_run_id)
        model_artifact, metrics_artifact = invocation.artifact_store.save_artifact_batch(
            (
                ArtifactWrite(
                    name=model_artifact_name,
                    data=model_bytes,
                    kind=ArtifactKind.MODEL,
                    media_type="application/octet-stream",
                ),
                ArtifactWrite(
                    name=metrics_artifact_name,
                    data=metrics_bytes,
                    kind=ArtifactKind.METRICS,
                    media_type="application/json",
                ),
            ),
            produced_by=invocation.agent_id,
        )
        dataset_artifact = invocation.artifact_store.get(schema.artifact_name)
        experiment = Experiment(
            id=new_id("experiment"),
            run_id=invocation.run_id,
            name=arguments["experiment_name"],
            status=ExperimentStatus.COMPLETED,
            parameters={
                "dataset": arguments["dataset"],
                "target_column": arguments["target_column"],
            },
            metrics={key: float(value) for key, value in metrics.items()},
            seed=arguments["seed"],
            dataset_artifact_id=dataset_artifact.id if dataset_artifact is not None else None,
            model_artifact_id=model_artifact.id,
            artifact_ids=(model_artifact.id, metrics_artifact.id),
            tracker_run_id=tracker_run_id,
        )
        events = (
            started,
            ToolEvent(
                EventType.MODEL_TRAINED,
                {
                    **(reproducibility or {}),
                    "model_artifact_id": model_artifact.id,
                    "tracker_run_id": tracker_run_id,
                },
            ),
            ToolEvent(
                EventType.EXPERIMENT_COMPLETED,
                {"experiment": experiment.model_dump(mode="json")},
            ),
        )
        text = json.dumps(
            {
                "experiment_id": experiment.id,
                "metrics": metrics,
                "model_artifact_id": model_artifact.id,
                "tracker_run_id": tracker_run_id,
            },
            sort_keys=True,
        )
        return replace(
            training,
            stdout=text,
            value=ExperimentValue(
                text=text,
                experiment_id=experiment.id,
                metrics={key: float(value) for key, value in metrics.items()},
                model_artifact_id=model_artifact.id,
                tracker_run_id=tracker_run_id,
                artifact_ids=(model_artifact.id, metrics_artifact.id),
            ),
            artifact_ids=(model_artifact.id, metrics_artifact.id),
            events=events,
        )


def _is_numeric_dtype(dtype: str) -> bool:
    """Return whether a polars dtype string names a column the audit hands the model as numbers.

    Booleans count as numeric because polars hands the audit ``True``/``False``, which
    ``np.asarray(..., dtype=float)`` casts to ``1.0``/``0.0`` -- the same values this training
    script encodes them as (see ``_training_code``'s ``cell`` helper).
    """
    return dtype == "Boolean" or dtype.startswith(("Int", "UInt", "Float", "Decimal"))


def _prepare_workspace(workspace: Path) -> tuple[Path, Path]:
    """Resolve ``workspace``, create its root, and return its private staging path.

    The staging path itself stays unallocated on the host: the backend writes and charges the
    ``StagedInput`` bytes, so a quota refusal leaves an empty workspace.
    """
    workspace = workspace.resolve()
    ensure_workspace_root(workspace)
    work_dir = workspace / ".thymira"
    return workspace, work_dir


def _training_preflight(
    capability: ToolCapability, workspace: Path
) -> tuple[SandboxMode, Path, Path] | ToolResult:
    """Prepare a training workspace after refusing link-shaped staging roots."""
    mode = requested_sandbox_mode(capability)
    if refusal := read_only_staging_refusal(mode):
        return refusal
    resolved = workspace.resolve()
    work_dir = resolved / ".thymira"
    if refusal := link_shaped_refusal(mode, resolved, work_dir):
        return refusal
    prepared = _prepare_workspace(resolved)
    return mode, *prepared


def _validate_training_paths(workspace: Path, *paths: Path) -> None:
    """Reject link-shaped training inputs and outputs before a child can write through them."""
    for path in paths:
        assert_no_link_components(workspace, path)


def _experiment_protocol_error(exc: BaseException) -> str:
    """Keep the established missing-output message while exposing protocol failures."""
    detail = str(exc)
    if detail in {
        "experiment model artifact is missing or invalid",
        "experiment metrics sidecar is missing or invalid",
    }:
        return "experiment code must create model.joblib and valid metrics.json"
    return detail or "experiment code must create model.joblib and valid metrics.json"


def _failed_training_result(
    training: ToolResult, started: ToolEvent, error: str | None
) -> ToolResult:
    """Close a training failure with the same event shape for child and protocol failures."""
    failed_event = ToolEvent(
        EventType.EXPERIMENT_COMPLETED,
        {"status": ExperimentStatus.FAILED, "error": error},
    )
    return replace(training, success=False, error=error, events=(started, failed_event))


def _read_training_outputs(model_path: Path, metrics_path: Path) -> tuple[bytes, dict[str, float]]:
    """Read the model and strictly typed metrics returned by a training child."""
    model_bytes = read_bounded_file(
        model_path, max_bytes=MAX_MODEL_ARTIFACT_BYTES, label="experiment model artifact"
    )
    metrics_payload = read_bounded_json(
        metrics_path,
        _ExperimentMetrics,
        max_bytes=MAX_SIDECAR_BYTES,
        label="experiment metrics sidecar",
    )
    return model_bytes, metrics_payload.root


def _write_validated_training_outputs(
    work_dir: Path, model_bytes: bytes, metrics: dict[str, float]
) -> tuple[Path, Path]:
    """Copy validated child outputs to host-owned paths for tracker publication."""
    model_path = work_dir / f"{new_id('tool')}-validated-model.joblib"
    metrics_path = work_dir / f"{new_id('tool')}-validated-metrics.json"
    model_path.write_bytes(model_bytes)
    metrics_path.write_text(json.dumps(metrics, sort_keys=True), encoding="utf-8", newline="\n")
    return model_path, metrics_path


def _training_code(
    dataset_path: str,
    model_path: str,
    metrics_path: str,
    *,
    target_column: str,
    seed: int,
    numeric_columns: list[str],
    boolean_columns: list[str],
    evidence_path: str,
) -> str:
    """Build a fixed-seed training script with JSON-encoded paths and names.

    Numeric-ness comes from the registered schema's polars dtypes -- the same typing
    `audit_model` sees when it reads the dataset back through polars -- never from parsing the
    CSV text: a blank cell in an otherwise numeric column would demote it to the one-hot branch,
    and at audit time polars still types that column numeric, so every category the encoder
    fitted becomes "unknown" and `handle_unknown="ignore"` zeroes the block silently. Blanks are
    imputed (median for numeric columns, the constant "missing" for categorical ones) inside the
    persisted `Pipeline`, so a blank cell can never turn a continuous column into categories.
    Booleans are cast to 1.0/0.0, the same values polars' `True`/`False` become. The numeric
    branch is standardized after imputation: an unscaled column like `credit_amount` (reaching
    the tens of thousands) otherwise stalls `lbfgs` at its iteration limit, leaving the
    persisted weights those of an unconverged fit and a `ConvergenceWarning` on every default
    run. Labels are fitted as they appear -- a `LabelEncoder` on the target would leave
    `model.classes_` a list of positional codes that no longer name anything, and every later
    reader would have to guess the mapping back.
    """
    return f"""
import csv
import importlib.metadata
import json
import platform
from pathlib import Path

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler
from threadpoolctl import threadpool_info, threadpool_limits

source = Path({json.dumps(str(dataset_path))})
with source.open(encoding="utf-8", newline="") as handle:
    rows = list(csv.DictReader(handle))
target = {json.dumps(target_column)}
numeric_names = set({json.dumps(list(numeric_columns))})
boolean_names = set({json.dumps(list(boolean_columns))})
features = [name for name in rows[0] if name != target]


def cell(name, value):
    # A blank cell is missing on both sides (polars hands the audit `None`); a boolean is the
    # 1.0/0.0 polars' `True`/`False` casts to.
    if value is None or value == "":
        return None
    if name in boolean_names:
        return 1.0 if value.strip().lower() in ("true", "1", "t", "yes") else 0.0
    return value


x_values = [[cell(name, row[name]) for name in features] for row in rows]
labels = [row[target] for row in rows]
numeric = [index for index, name in enumerate(features) if name in numeric_names]
categorical = [index for index, name in enumerate(features) if name not in numeric_names]
encoder = ColumnTransformer(
    [
        (
            "numeric",
            Pipeline(
                [
                    (
                        "cast",
                        FunctionTransformer(
                            np.asarray, kw_args={{"dtype": float}}, feature_names_out="one-to-one"
                        ),
                    ),
                    ("impute", SimpleImputer(strategy="median")),
                    ("scale", StandardScaler()),
                ]
            ),
            numeric,
        ),
        (
            "categorical",
            Pipeline(
                [
                    (
                        "impute",
                        SimpleImputer(
                            missing_values=None, strategy="constant", fill_value="missing"
                        ),
                    ),
                    ("onehot", OneHotEncoder(handle_unknown="ignore")),
                ]
            ),
            categorical,
        ),
    ]
)
model = Pipeline(
    [
        ("encode", encoder),
        ("classify", LogisticRegression(random_state={seed}, max_iter=1000)),
    ]
)
x_train, x_test, y_train, y_test = train_test_split(
    x_values, labels, test_size={DEFAULT_TEST_SIZE!r}, random_state={seed}
)
with threadpool_limits(limits=1):
    model.fit(x_train, y_train)
    predicted = model.predict(x_test)
    observed_threadpools = [
        {{
            "user_api": pool["user_api"],
            "internal_api": pool["internal_api"],
            "prefix": pool["prefix"],
            "version": pool.get("version"),
            "num_threads": pool["num_threads"],
        }}
        for pool in threadpool_info()
    ]
metrics = {{"accuracy": float(accuracy_score(y_test, predicted))}}
joblib.dump(model, {json.dumps(str(model_path))})
Path({json.dumps(str(metrics_path))}).write_text(
    json.dumps(metrics, sort_keys=True), encoding="utf-8"
)
classifier_args = model.named_steps["classify"].get_params(deep=False)
evidence = {{
    "seed": classifier_args["random_state"],
    "library_versions": {{
        "python": platform.python_version(),
        "numpy": importlib.metadata.version("numpy"),
        "scipy": importlib.metadata.version("scipy"),
        "scikit-learn": importlib.metadata.version("scikit-learn"),
        "joblib": importlib.metadata.version("joblib"),
        "threadpoolctl": importlib.metadata.version("threadpoolctl"),
    }},
    "estimator_args": classifier_args,
    "split": {{
        "test_size": {DEFAULT_TEST_SIZE!r},
        "random_state": {seed},
        "shuffle": True,
        "stratify": None,
    }},
    "single_thread": bool(observed_threadpools) and all(
        pool["num_threads"] == 1 for pool in observed_threadpools
    ),
    "observed_threadpools": observed_threadpools,
}}
Path({json.dumps(str(evidence_path))}).write_text(
    json.dumps(evidence, sort_keys=True), encoding="utf-8"
)
print(json.dumps(metrics, sort_keys=True))
    """


def _custom_training_code(
    user_code: str,
    dataset_path: str,
    model_path: str,
    metrics_path: str,
    *,
    target_column: str,
    seed: int,
) -> str:
    """Provide a stable file contract to caller-supplied training code.

    The code still executes only through the injected Sandbox. It receives paths and the fixed
    seed as variables and must write the model and metrics files consumed by the runtime.
    """
    prefix = f"""
from pathlib import Path

dataset_path = Path({json.dumps(str(dataset_path))})
model_path = Path({json.dumps(str(model_path))})
metrics_path = Path({json.dumps(str(metrics_path))})
target_column = {json.dumps(target_column)}
seed = {seed}
"""
    return prefix + "\n" + user_code + "\n"


__all__ = ["RunExperiment", "RunExperimentArguments"]
