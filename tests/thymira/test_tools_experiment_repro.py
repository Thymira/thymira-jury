"""Reproducibility evidence for the real default experiment training child."""

from __future__ import annotations

import importlib.metadata
import json
import platform
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import joblib
import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.events import InMemoryEventLog
from thymira.mira.checks.models import ControlStatus
from thymira.mira.checks.repro import check_reproducibility_metadata
from thymira.policies import (
    Gate,
    PolicyEngine,
    RiskProfile,
    auto_approve,
)
from thymira.schemas import EventType, SandboxEnforcement, SandboxMode, ToolCallStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    LocalSubprocessSandbox,
    Tool,
    ToolContext,
    ToolExecution,
    ToolExecutionError,
    ToolManager,
    ToolRegistry,
    register_dataset,
)
from thymira.tools.builtins import RunExperiment
from thymira.tools.builtins.experiment_repro import load_reproducibility_evidence
from thymira.tools.sandbox import SandboxRun

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _MissingEvidenceSandbox:
    """Write the legacy output files but deliberately omit the fresh evidence sidecar."""

    def run(self, argv: list[str], **kwargs: Any) -> SandboxRun:
        """Simulate an otherwise successful child that cannot substantiate its provenance."""
        workspace = Path(kwargs["workspace"])
        work_dir = workspace / ".thymira"
        (work_dir / "model.joblib").write_bytes(b"not loaded by the parent")
        (work_dir / "metrics.json").write_text('{"accuracy": 0.5}', encoding="utf-8")
        return SandboxRun(
            stdout="",
            stderr="",
            exit_code=0,
            mode=kwargs["mode"],
            enforcement=SandboxEnforcement.PARTIAL,
        )


@dataclass(frozen=True, slots=True)
class _StagingObserverSandbox:
    """Run the real child and retain the runtime input the argv actually named.

    A backend stages the runtime-owned inputs itself and removes them again when the child ends,
    so the executed script is no longer on disk by the time the parent's evidence is read. The
    sandbox boundary is where it can still be observed, and observing it there keeps the check
    independent of the producer: the bytes compared are the ones the child was pointed at, not a
    variable the tool also derived its recorded digest from.
    """

    sandbox: LocalSubprocessSandbox = field(default_factory=LocalSubprocessSandbox)
    executed: dict[str, bytes] = field(default_factory=dict)

    def run(self, argv: list[str], **kwargs: Any) -> SandboxRun:
        """Retain the staged input whose workspace path this argv executes."""
        for item in kwargs.get("staged_inputs") or ():
            if item.relative_path == argv[-1]:
                self.executed[item.relative_path] = item.content
        return self.sandbox.run(argv, **kwargs)


@dataclass(frozen=True, slots=True)
class _ObservedPoolSandbox:
    """Run the real child, then inject observations that require an A23 failure."""

    pool_count: int | None
    sandbox: LocalSubprocessSandbox = field(default_factory=LocalSubprocessSandbox)

    def run(self, argv: list[str], **kwargs: Any) -> SandboxRun:
        """Adjust only the fresh child sidecar after its real model and metrics are written."""
        work_dir = Path(kwargs["workspace"]) / ".thymira"
        previous = set(work_dir.glob("*-reproducibility.json"))
        result = self.sandbox.run(argv, **kwargs)
        if result.exit_code != 0:
            return result
        sidecars = set(work_dir.glob("*-reproducibility.json")).difference(previous)
        sidecar = next(iter(sidecars))
        evidence = json.loads(sidecar.read_text(encoding="utf-8"))
        evidence["observed_threadpools"] = (
            []
            if self.pool_count is None
            else [
                {
                    "user_api": "blas",
                    "internal_api": "openblas",
                    "prefix": "libopenblas",
                    "version": "0.3",
                    "num_threads": self.pool_count,
                }
            ]
        )
        evidence["single_thread"] = False
        sidecar.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
        return result


@pytest.fixture
def experiment_context(tmp_path: Path) -> ToolContext:
    """Build a registered binary dataset and an authorised tool context."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    context.workspace.mkdir()
    dataset = context.workspace / "training.csv"
    dataset.write_text(
        "x1,x2,target\n0,0,no\n0,1,no\n1,0,yes\n1,1,yes\n2,0,yes\n2,1,yes\n3,0,yes\n3,1,yes\n",
        encoding="utf-8",
    )
    register_dataset(context.artifact_store, dataset, "training", produced_by=context.agent_id)
    return context


def _run_default(context: ToolContext, *, seed: int = 7, sandbox: Any = None) -> ToolExecution:
    """Execute the public tool boundary with the fixed baseline arguments."""
    experiment = (
        RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)
        if sandbox is None
        else RunExperiment(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)
    )
    return ToolManager(ToolRegistry((cast("Tool", experiment),))).execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": f"baseline-{seed}",
            "seed": seed,
            "description": "Train the baseline.",
        },
    )


def _trained_payload(context: ToolContext) -> dict[str, Any]:
    """Return the only model-trained payload recorded for the current call."""
    return next(
        event.payload
        for event in context.event_log.events()
        if event.type is EventType.MODEL_TRAINED
    )


def test_run_experiment_default_records_child_evidence_matching_the_saved_classifier(
    experiment_context: ToolContext,
) -> None:
    """The child event matches its saved classifier without production deserialization."""
    observer = _StagingObserverSandbox()
    execution = _run_default(experiment_context, sandbox=observer)

    assert execution.call.status is ToolCallStatus.COMPLETED
    payload = _trained_payload(experiment_context)
    model = joblib.load(experiment_context.workspace / ".thymira" / "model.joblib")
    assert payload["seed"] == 7
    assert payload["estimator_args"] == model.named_steps["classify"].get_params(deep=False)
    assert payload["split"] == {
        "test_size": 0.25,
        "random_state": 7,
        "shuffle": True,
        "stratify": None,
    }
    assert payload["library_versions"] == {
        "python": platform.python_version(),
        "numpy": importlib.metadata.version("numpy"),
        "scipy": importlib.metadata.version("scipy"),
        "scikit-learn": importlib.metadata.version("scikit-learn"),
        "joblib": importlib.metadata.version("joblib"),
        "threadpoolctl": importlib.metadata.version("threadpoolctl"),
    }
    assert payload["single_thread"] is True
    assert payload["observed_threadpools"]
    assert all(pool["num_threads"] == 1 for pool in payload["observed_threadpools"])
    (executed_script,) = observer.executed.values()
    assert payload["training_script_sha256"] == sha256(executed_script).hexdigest()


def test_german_credit_default_training_stays_within_the_metrics_oracle(
    tmp_path: Path,
) -> None:
    """The shipped German-credit baseline keeps its recorded accuracy and A23 evidence."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(development_policy()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    context.workspace.mkdir()
    dataset = Path(__file__).resolve().parents[2] / "data" / "german_credit.csv"
    register_dataset(context.artifact_store, dataset, "german_credit", produced_by=context.agent_id)

    execution = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    ).execute(
        context,
        "run_experiment",
        {
            "dataset": "german_credit",
            "target_column": "is_high_risk",
            "experiment_name": "german-credit-regression",
            "description": "Train the German-credit baseline.",
        },
    )

    expected = json.loads(
        (Path(__file__).parent / "acceptance" / "metrics.expected.json").read_text(encoding="utf-8")
    )
    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.error
    actual = json.loads(execution.result.stdout)["metrics"]["accuracy"]
    status, _ = check_reproducibility_metadata(context.event_log.events())
    assert abs(actual - expected["accuracy"]) <= 0.02
    assert status is ControlStatus.PASSED
    assert _trained_payload(context)["single_thread"] is True


def test_run_experiment_default_repeats_metrics_for_the_same_actual_seed(
    experiment_context: ToolContext,
) -> None:
    """A fixed default seed yields the same metrics and recorded effective seed."""
    first = _run_default(experiment_context, seed=19)
    first_payload = _trained_payload(experiment_context)
    second = _run_default(experiment_context, seed=19)
    trained = [
        event.payload
        for event in experiment_context.event_log.events()
        if event.type is EventType.MODEL_TRAINED
    ]

    assert first.call.status is ToolCallStatus.COMPLETED
    assert second.call.status is ToolCallStatus.COMPLETED
    assert json.loads(first.result.stdout)["metrics"] == json.loads(second.result.stdout)["metrics"]
    assert first_payload["seed"] == 19
    assert trained[-1]["seed"] == 19


def test_run_experiment_custom_code_never_inherits_default_reproducibility_claims(
    experiment_context: ToolContext,
) -> None:
    """Custom code remains an A23-warning case even after a default baseline run."""
    _run_default(experiment_context)
    execution = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    ).execute(
        experiment_context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "custom",
            "description": "Train a custom baseline.",
            "code": """
import json
import joblib
from sklearn.dummy import DummyClassifier

model = DummyClassifier(strategy="most_frequent").fit([[0], [1]], [0, 1])
joblib.dump(model, model_path)
metrics_path.write_text(json.dumps({"accuracy": 0.5}), encoding="utf-8")
(model_path.parent / "fake-reproducibility.json").write_text("{}", encoding="utf-8")
""",
        },
    )
    trained = [
        event.payload
        for event in experiment_context.event_log.events()
        if event.type is EventType.MODEL_TRAINED
    ]

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert set(trained[-1]) == {"model_artifact_id", "tracker_run_id"}


def test_run_experiment_default_fails_without_fresh_child_evidence(
    experiment_context: ToolContext,
) -> None:
    """A successful-looking child cannot reuse old files when its fresh sidecar is absent."""
    prior = _run_default(experiment_context)
    trained_before = [
        event.payload
        for event in experiment_context.event_log.events()
        if event.type is EventType.MODEL_TRAINED
    ]
    execution = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(
                        sandbox=_MissingEvidenceSandbox(), mode=SandboxMode.DANGER_FULL_ACCESS
                    ),
                ),
            )
        )
    ).execute(
        experiment_context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "description": "Train the baseline.",
        },
    )

    assert prior.call.status is ToolCallStatus.COMPLETED
    assert execution.call.status is ToolCallStatus.FAILED
    assert "reproducibility evidence" in (execution.result.error or "")
    trained_after = [
        event.payload
        for event in experiment_context.event_log.events()
        if event.type is EventType.MODEL_TRAINED
    ]
    assert trained_after == trained_before


def _valid_evidence() -> dict[str, Any]:
    """Return a complete sidecar payload for isolated malformed-evidence cases."""
    return {
        "seed": 1,
        "library_versions": {
            "python": "3.13",
            "numpy": "2.0",
            "scipy": "1.0",
            "scikit-learn": "1.5",
            "joblib": "1.4",
            "threadpoolctl": "3.6",
        },
        "estimator_args": {"random_state": 1},
        "split": {"test_size": 0.25, "random_state": 1, "shuffle": True, "stratify": None},
        "single_thread": True,
        "observed_threadpools": [
            {
                "user_api": "blas",
                "internal_api": "openblas",
                "prefix": "libopenblas",
                "version": "0.3",
                "num_threads": 1,
            }
        ],
    }


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("library_versions", {"python": "3.13"}),
        ("observed_threadpools", []),
        ("estimator_args", {"random_state": True}),
        (
            "split",
            {"test_size": 0.25, "random_state": True, "shuffle": True, "stratify": None},
        ),
    ],
)
def test_reproducibility_evidence_rejects_malformed_sidecars(
    tmp_path: Path, field: str, invalid_value: Any
) -> None:
    """Malformed sidecars cannot be promoted into model-trained event evidence."""
    sidecar = tmp_path / "evidence.json"
    payload = _valid_evidence()
    payload[field] = invalid_value
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ToolExecutionError, match="malformed reproducibility evidence"):
        load_reproducibility_evidence(sidecar)


@pytest.mark.parametrize("pool_count", [8, None])
def test_run_experiment_records_truthful_non_single_thread_evidence_for_a23_failure(
    experiment_context: ToolContext, pool_count: int | None
) -> None:
    """Parallel or unavailable observations complete training and leave A23 to reject them."""
    manager = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(
                        sandbox=_ObservedPoolSandbox(pool_count),
                        mode=SandboxMode.DANGER_FULL_ACCESS,
                    ),
                ),
            )
        )
    )
    execution = manager.execute(
        experiment_context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "description": "Train the baseline.",
        },
    )
    status, _ = check_reproducibility_metadata(experiment_context.event_log.events())

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert _trained_payload(experiment_context)["single_thread"] is False
    assert status is ControlStatus.FAILED
