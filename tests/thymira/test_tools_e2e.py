"""End-to-end tools-execution QA gates (TOOL-27 and TOOL-28b).

Boundary (python-testing-integration): real ``thymira`` members wired together — the
``ToolManager`` + ``Gate`` + ``PolicyEngine``, ``LocalSubprocessSandbox`` in explicit
``DANGER_FULL_ACCESS`` development mode and (for TOOL-28b)
``ContainerSandbox``, ``LocalArtifactStore`` and the local ``MlflowTracker`` file store under
``tmp_path``, and MIRA's deterministic ``audit_run`` over the recorded events plus the store.
Nothing is faked: the audit is recomputed from evidence, never read off a success flag. No model
provider is involved (the tools execute Python in a subprocess, not an LLM).

``TOOL-27`` is the roadmap week-1 demo captured as a test: register a dataset, train a tiny
classifier through ``run_python`` (producing ``model.pkl`` + ``metrics.json`` and a spilled stdout
artifact), train and track it through ``run_experiment`` (exercising the Sandbox, artifact-event,
tracker and Experiment seams), then preserve the truthful A19 HIGH finding for explicit
unconfined execution alongside the independent A23 finding for custom training.

The container case uses the actual Python, default experiment, inspection and model-audit tools
with the shipped policy. It proves portable execution and records PARTIAL enforcement: A19 remains a
finding, while the model's default training evidence still passes A23.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import development_policy, human_approved_context
from thymira.events import verify_events
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import (
    Policy,
    PolicyEngine,
    RiskProfile,
    load_policy_stack,
)
from thymira.schemas import (
    Actor,
    EventType,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    ToolCallStatus,
    new_id,
)
from thymira.tools import (
    Tool,
    ToolContext,
    ToolManager,
    ToolRegistry,
    register_dataset,
)
from thymira.tools.builtins import AuditModel, InspectModel, RunExperiment, RunPython
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.sandbox import ContainerSandbox

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_DATASET = "german_credit"
_TARGET = "is_high_risk"
_EXPERIMENT = "credit-baseline"
_SPILL_CHARS = 100_000
"""Bytes of stdout the training snippet prints — above the manager's 64 KiB inline cap, so it
spills into a log artifact instead of the event payload."""

# Training that runs *through the run_python tool*: it reads the dataset from the workspace,
# label-encodes every feature, fits a tiny tree, writes ``model.pkl`` + ``metrics.json``, and
# finally prints an oversized blob so the manager spills stdout to an artifact.
_RUN_PYTHON_TRAINING = f"""
import csv
import json
import joblib
from sklearn.preprocessing import LabelEncoder
from sklearn.tree import DecisionTreeClassifier

with open("{_DATASET}.csv", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
features = [name for name in rows[0] if name != "{_TARGET}"]
codes = {{name: LabelEncoder().fit_transform([row[name] for row in rows]) for name in features}}
x_values = [[int(codes[name][index]) for name in features] for index in range(len(rows))]
y_values = [int(row["{_TARGET}"]) for row in rows]
model = DecisionTreeClassifier(random_state=7, max_depth=4).fit(x_values, y_values)
accuracy = float(model.score(x_values, y_values))
joblib.dump(model, "model.pkl")
with open("metrics.json", "w", encoding="utf-8") as handle:
    json.dump({{"accuracy": accuracy, "rows": len(rows)}}, handle, sort_keys=True)
print("A" * {_SPILL_CHARS})
"""

# Training that runs *through the run_experiment tool*: run_experiment injects dataset_path,
# model_path, metrics_path, target_column and seed; the snippet only has to fit a model and write
# the two files the tool then registers as artifacts and logs to the MLflow file store.
_RUN_EXPERIMENT_TRAINING = """
import csv
import json
import joblib
from sklearn.preprocessing import LabelEncoder
from sklearn.tree import DecisionTreeClassifier

with dataset_path.open(encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
features = [name for name in rows[0] if name != target_column]
codes = {name: LabelEncoder().fit_transform([row[name] for row in rows]) for name in features}
x_values = [[int(codes[name][index]) for name in features] for index in range(len(rows))]
y_values = [int(row[target_column]) for row in rows]
model = DecisionTreeClassifier(random_state=seed, max_depth=4).fit(x_values, y_values)
accuracy = float(model.score(x_values, y_values))
joblib.dump(model, model_path)
metrics_path.write_text(json.dumps({"accuracy": accuracy}, sort_keys=True), encoding="utf-8")
print(json.dumps({"accuracy": accuracy}))
"""


def _dataset_path() -> Path:
    """Return the demo dataset shipped at the repository root."""
    return Path(__file__).resolve().parents[2] / "data" / f"{_DATASET}.csv"


def _tool_context(tmp_path: Path, *, policy: Policy | None = None) -> ToolContext:
    """Build a runtime-owned tool context whose confidence keeps local tools on the PASS path."""
    run_id = new_id("run")
    return replace(
        human_approved_context(
            tmp_path,
            run_id=run_id,
            engine=PolicyEngine(policy or development_policy()),
        ),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_development_execution_retains_a19_and_custom_training_findings(tmp_path: Path) -> None:
    dataset = _dataset_path()
    if not dataset.is_file():
        pytest.skip(f"demo dataset missing: {dataset}")

    context = _tool_context(tmp_path)
    log = context.event_log
    store = context.artifact_store
    context.workspace.mkdir(parents=True, exist_ok=True)
    workspace_dataset = context.workspace / f"{_DATASET}.csv"
    shutil.copyfile(dataset, workspace_dataset)

    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )

    # A real run records its environment (A17), then classifies risk before it runs any tool
    # (A15). register_dataset writes the dataset + schema artifacts the experiment tool consumes.
    log.append(
        EventType.RUN_STARTED,
        Actor.system(),
        {"prompt": "train a credit-risk baseline", "run_environment": {"demo": "tool-27"}},
    )
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"agent": "risk-classifier", "status": "classified", "text": "risk classified: limited"},
    )
    register_dataset(store, workspace_dataset, _DATASET, produced_by=context.agent_id)

    python_execution = manager.execute(
        context,
        "run_python",
        {
            "code": _RUN_PYTHON_TRAINING,
            "description": "Preprocess the credit-risk training data",
        },
    )
    experiment_execution = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": _DATASET,
            "target_column": _TARGET,
            "experiment_name": _EXPERIMENT,
            "seed": 7,
            "code": _RUN_EXPERIMENT_TRAINING,
            "description": "Train the credit-risk baseline model",
        },
    )
    log.append(EventType.RUN_COMPLETED, Actor.system(), {"status": "COMPLETED"})

    # Both tool calls completed and both recorded the partial confinement C-3 mandates locally.
    assert python_execution.call.status is ToolCallStatus.COMPLETED, python_execution.result.error
    assert experiment_execution.call.status is ToolCallStatus.COMPLETED, (
        experiment_execution.result.error
    )
    assert python_execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert experiment_execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL

    events = log.events()

    # The evidence itself verifies: the chain is intact and every manifest entry matches its file.
    assert verify_events(events).valid
    assert store.verify() == []

    report = audit_run(AuditContext(context.run_id, events, store))

    # Explicit danger mode is recorded as a HIGH A19 finding; custom code independently leaves
    # A23 failed. This test keeps both findings visible rather than hiding the changed severity.
    #   A19: unconfined execution was explicitly requested for this trusted development test.
    #   A23: this test supplies custom training code. Its execution provenance cannot be
    #        inferred from the injected seed or the default baseline's configuration, so the
    #        tool correctly makes no default provenance claim for this model.
    failed = [
        control.control_id for control in report.controls if control.status is ControlStatus.FAILED
    ]
    assert failed == ["A19", "A23"], report.to_markdown()
    assert report.status == "failed"
    assert next(control for control in report.controls if control.control_id == "A30").status is (
        ControlStatus.PASSED
    )
    a19 = [finding for finding in report.findings if finding.control_id == "A19"]
    assert len(a19) == 1
    assert a19[0].severity is Severity.HIGH

    # A10 verifies the announced artifacts against the manifest: the digest in each artifact.created
    # event equals the store's recorded digest for the same file.
    a10 = next(control for control in report.controls if control.control_id == "A10")
    assert a10.status is ControlStatus.PASSED
    created = [event for event in events if event.type is EventType.ARTIFACT_CREATED]
    assert len(created) >= 3
    for event in created:
        artifact = store.get(str(event.payload["name"]))
        assert artifact is not None
        assert artifact.sha256 == event.payload["sha256"]

    # The oversized stdout is retrievable in full from the artifact it spilled to.
    spill = store.get(f"tool-results/{python_execution.call.id}/stdout.log")
    assert spill is not None
    assert "[output spilled to artifact" in python_execution.result.stdout
    assert store.load_text(spill.name).rstrip("\r\n") == "A" * _SPILL_CHARS

    # run_python produced model.pkl + metrics.json in the workspace; run_experiment registered the
    # model and metrics as store artifacts and logged them to the local MLflow file store.
    assert (context.workspace / "model.pkl").is_file()
    metrics = json.loads((context.workspace / "metrics.json").read_text(encoding="utf-8"))
    assert "accuracy" in metrics
    assert store.get(f"models/{_EXPERIMENT}.joblib") is not None
    assert store.get(f"metrics/{_EXPERIMENT}.json") is not None
    tracker_runs = MlflowTracker(context.workspace).query_runs(experiment_name=_EXPERIMENT)
    assert tracker_runs
    assert tracker_runs[0].metrics.get("accuracy") is not None


def test_container_python_training_inspection_and_model_audit_preserve_evidence(
    tmp_path: Path,
) -> None:
    require_sandbox_image()
    context = _tool_context(tmp_path, policy=load_policy_stack())
    context.workspace.mkdir(parents=True)
    dataset = context.workspace / "tiny.csv"
    dataset.write_text(
        "amount,target\n" + "".join(f"{index},{index % 2}\n" for index in range(40)),
        encoding="utf-8",
    )
    log = context.event_log
    log.append(
        EventType.RUN_STARTED,
        Actor.system(),
        {"prompt": "train inside the workspace", "run_environment": {"demo": "container-tools"}},
    )
    log.append(
        EventType.AGENT_MESSAGE,
        Actor.system(),
        {"agent": "risk-classifier", "status": "classified", "text": "risk classified: limited"},
    )
    register_dataset(context.artifact_store, dataset, "tiny", produced_by=context.agent_id)
    sandbox = ContainerSandbox()
    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", RunPython(sandbox=sandbox)),
                cast("Tool", RunExperiment(sandbox=sandbox)),
                cast("Tool", InspectModel(sandbox=sandbox)),
                cast("Tool", AuditModel(sandbox=sandbox)),
            )
        )
    )

    python_execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; "
                "Path('portable.txt').write_text('container'); print('ready')"
            ),
            "description": "Write an execution marker inside the workspace",
        },
    )
    assert python_execution.call.status is ToolCallStatus.COMPLETED, python_execution.result.error
    assert (context.workspace / "portable.txt").read_text(encoding="utf-8") == "container"
    experiment = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "tiny",
            "target_column": "target",
            "experiment_name": "container-baseline",
            "description": "Train the default classifier inside the container",
        },
    )
    assert experiment.call.status is ToolCallStatus.COMPLETED, experiment.result.error
    inspection = manager.execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/container-baseline.joblib",
            "description": "Inspect the trained baseline model",
        },
    )
    assert inspection.call.status is ToolCallStatus.COMPLETED, inspection.result.error
    assert json.loads(inspection.result.stdout)["class_name"] == "LogisticRegression"
    audited = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/container-baseline.joblib",
            "dataset": "tiny",
            "target_column": "target",
            "protected_column": "amount",
            "description": "Audit the trained baseline model",
        },
    )
    assert audited.call.status is ToolCallStatus.COMPLETED, audited.result.error
    audit_payload = json.loads(audited.result.stdout)
    assert sum(sum(row) for row in audit_payload["confusion_matrix"]) == 40
    assert sum(group["count"] for group in audit_payload["subgroups"].values()) == 40
    assert audit_payload["subgroups_excluded_missing_protected"] == 0
    assert (
        context.artifact_store.load_json("audits/container-baseline.joblib.json") == audit_payload
    )
    log.append(EventType.RUN_COMPLETED, Actor.system(), {"status": "COMPLETED"})

    executions = (python_execution, experiment, inspection, audited)
    assert all(item.call.sandbox_mode is SandboxMode.WORKSPACE_WRITE for item in executions)
    assert all(item.call.sandbox_enforcement is SandboxEnforcement.PARTIAL for item in executions)
    completed = [event for event in log.events() if event.type is EventType.TOOL_COMPLETED]
    assert len(completed) == 4
    assert all(event.payload["sandbox_mode"] == SandboxMode.WORKSPACE_WRITE for event in completed)
    assert all(
        event.payload["requested_sandbox_mode"] == SandboxMode.WORKSPACE_WRITE
        for event in completed
    )
    for event in completed:
        termination = event.payload["sandbox_termination"]
        assert termination["reap"] == "quiesced"
        assert termination["reap_deadline_s"] == pytest.approx(5.0)
        assert isinstance(termination["reap_duration_s"], float)
    assert verify_events(log.events()).valid
    assert context.artifact_store.verify() == []
    report = audit_run(AuditContext(context.run_id, log.events(), context.artifact_store))
    controls = {control.control_id: control.status for control in report.controls}
    assert controls["A3"] is ControlStatus.PASSED, report.to_markdown()
    assert controls["A23"] is ControlStatus.PASSED, report.to_markdown()
    assert controls["A30"] is ControlStatus.PASSED, report.to_markdown()
    assert controls["A19"] is ControlStatus.FAILED
    a19 = [finding for finding in report.findings if finding.control_id == "A19"]
    assert a19
    assert all(finding.severity is Severity.MEDIUM for finding in a19)
