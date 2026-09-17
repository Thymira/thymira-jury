"""Docker proof for the configured production model-tool surface.

The three model tools are exercised through ``configured_builtins_registry`` and ``ToolManager``
in one real run: training writes a model, inspection loads it, and the audit worker loads it again
to produce predictions. After each call, a separately opened JSONL log is chain-verified and MIRA
recomputes A29 and A30 from that replay. The training child also probes credential-shaped host
environment names, while a second model artifact attempts to write outside the mounted workspace.
Neither payload is deserialized by the host process.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import joblib
import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import JsonlEventLog, verify_log
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import RiskProfile
from thymira.schemas import ArtifactKind, SandboxEnforcement, ToolCallStatus, new_id
from thymira.tools import ToolContext, ToolManager, register_dataset
from thymira.tools.builtins import configured_builtins_registry

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_TRAINING_CODE = """
import json
import os

import joblib
from sklearn.dummy import DummyClassifier

model = DummyClassifier(strategy="most_frequent").fit(
    [[0, 0], [0, 1], [1, 0], [1, 1]], ["no", "no", "yes", "yes"]
)
joblib.dump(model, model_path)
metrics_path.write_text(json.dumps({"accuracy": 0.5}), encoding="utf-8")
model_path.with_name("sandbox-env-probe.json").write_text(
    json.dumps(
        {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "<missing>"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "<missing>"),
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
"""


class _HostEscapePayload:
    """Pickle payload that can only attempt a write from the child deserializer."""

    def __reduce__(self) -> tuple[object, tuple[str, str]]:
        """Ask the child to open a path outside its only host bind mount."""
        return open, ("/workspace/../thymira-model-host-escape", "w")


def _context(tmp_path: Path) -> tuple[ToolContext, Path]:
    """Build an explicitly human-approved context with a durable event log."""
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    context = replace(
        human_approved_context(tmp_path, run_id=run_id, log=JsonlEventLog(events_path, run_id)),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    context.workspace.mkdir(parents=True)
    dataset = context.workspace / "training.csv"
    dataset.write_text(
        "x,group,target\n0,0,no\n0,1,no\n1,0,yes\n1,1,yes\n2,0,yes\n2,1,yes\n3,0,yes\n3,1,yes\n",
        encoding="utf-8",
        newline="\n",
    )
    register_dataset(context.artifact_store, dataset, "training", produced_by=context.agent_id)
    return context, events_path


def _assert_replayed_evidence(
    context: ToolContext,
    events_path: Path,
    tool_name: str,
) -> None:
    """Re-open and independently grade the latest configured model-tool execution."""
    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed_log = JsonlEventLog(events_path, context.run_id)
    replayed = replayed_log.events()
    event = next(
        event
        for event in reversed(replayed)
        if event.type.value == "tool.completed" and event.payload.get("tool") == tool_name
    )
    assert event.payload["sandbox_enforcement"] == SandboxEnforcement.PARTIAL.value
    spec = event.payload["sandbox_spec"]
    assert isinstance(spec, dict)
    assert spec["backend"] == "container"
    assert spec["network"] == "none"
    assert spec["workspace_mount"].endswith(":/workspace:rw")
    assert spec["memory"] == "1g"
    assert spec["cpus"] == "1.0"
    assert spec["pids_limit"] == 128
    assert not any(
        name.upper() in {"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"}
        for name in spec["environment_names"]
    )
    termination = event.payload["sandbox_termination"]
    assert isinstance(termination, dict)
    assert termination["control_channel"] == "container_state"
    assert termination["control_channel_validated"] is True
    assert termination["outcome"] == "completed"
    assert termination["reap"] == "quiesced"
    assert event.payload["sandbox_cleanup_confirmed"] is True

    report = audit_run(AuditContext(context.run_id, replayed, context.artifact_store))
    controls = {control.control_id: control for control in report.controls}
    assert controls["A29"].status is ControlStatus.PASSED, controls["A29"].detail
    assert controls["A30"].status is ControlStatus.PASSED, controls["A30"].detail


def test_configured_container_model_tools_reopen_evidence_and_confine_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Train, inspect and predict through production composition with independent evidence."""
    require_sandbox_image()
    monkeypatch.setenv("OPENAI_API_KEY", "model-surface-openai-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "model-surface-aws-secret")
    context, events_path = _context(tmp_path)
    manager = ToolManager(
        configured_builtins_registry(source={"THYMIRA_SANDBOX_BACKEND": "container"})
    )

    training = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "configured-container-surface",
            "description": "Train a model in the configured container backend.",
            "code": _TRAINING_CODE,
        },
    )
    assert training.call.status is ToolCallStatus.COMPLETED, training.result.error
    assert training.result.success
    _assert_replayed_evidence(context, events_path, "run_experiment")

    environment_probe = json.loads(
        (context.workspace / ".thymira" / "sandbox-env-probe.json").read_text(encoding="utf-8")
    )
    assert all(value == "<missing>" for value in environment_probe.values())
    model_name = "models/configured-container-surface.joblib"
    assert context.artifact_store.get(model_name) is not None

    inspection = manager.execute(
        context,
        "inspect_model",
        {"model_artifact": model_name, "description": "Inspect the trained container model"},
    )
    assert inspection.call.status is ToolCallStatus.COMPLETED, inspection.result.error
    assert inspection.result.success
    assert json.loads(inspection.result.stdout)["class_name"] == "DummyClassifier"
    _assert_replayed_evidence(context, events_path, "inspect_model")

    audited = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": model_name,
            "dataset": "training",
            "target_column": "target",
            "protected_column": "group",
            "description": "Audit the trained container model for fairness",
        },
    )
    assert audited.call.status is ToolCallStatus.COMPLETED, audited.result.error
    assert audited.result.success
    payload = json.loads(audited.result.stdout)
    assert sum(sum(row) for row in payload["confusion_matrix"]) == 8
    assert sum(group["count"] for group in payload["subgroups"].values()) == 8
    _assert_replayed_evidence(context, events_path, "audit_model")


def test_configured_container_model_loader_cannot_escape_workspace(
    tmp_path: Path,
) -> None:
    """A hostile serialized model stays in the container and leaves truthful failure evidence."""
    require_sandbox_image()
    context, events_path = _context(tmp_path)
    payload_path = tmp_path / "hostile-model.joblib"
    joblib.dump(_HostEscapePayload(), payload_path)
    context.artifact_store.save_bytes(
        "models/hostile-container-model.joblib",
        payload_path.read_bytes(),
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    manager = ToolManager(
        configured_builtins_registry(source={"THYMIRA_SANDBOX_BACKEND": "container"})
    )

    execution = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/hostile-container-model.joblib",
            "dataset": "training",
            "target_column": "target",
            "protected_column": "group",
            "description": "Audit the hostile model for fairness",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert not execution.result.success
    assert execution.result.exit_code != 0
    failure = f"{execution.result.stderr}\n{execution.result.error}"
    assert "thymira-model-host-escape" in failure
    assert not (context.workspace.parent / "thymira-model-host-escape").exists()
    _assert_replayed_evidence(context, events_path, "audit_model")
