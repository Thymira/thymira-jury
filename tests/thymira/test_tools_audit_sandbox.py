"""Unit coverage for sandboxed model predictions during audit evidence generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import joblib
import pytest
from pydantic import ValidationError

from tests.thymira.fixtures_tools import development_policy
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, load_policy_stack
from thymira.schemas import ArtifactKind, SandboxEnforcement, SandboxMode, ToolCallStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import Tool, ToolContext, ToolManager, ToolRegistry
from thymira.tools.builtins import AuditModel
from thymira.tools.builtins.audit_model import AuditModelArguments
from thymira.tools.datasets import register_dataset
from thymira.tools.sandbox.base import SandboxRun, StagedInput
from thymira.tools.sandbox.staged_inputs import stage_inputs_on_host


@dataclass(frozen=True, slots=True)
class _CapturingSandbox:
    """Return a fixed child result after retaining the portable command."""

    result: SandboxRun
    argv: list[list[str]] = field(default_factory=list)
    modes: list[SandboxMode] = field(default_factory=list)

    def run(self, argv: list[str], *, mode: SandboxMode, **_kwargs: Any) -> SandboxRun:
        self.argv.append(argv)
        self.modes.append(mode)
        return self.result


@dataclass(frozen=True, slots=True)
class _WritingSandbox:
    """Write one worker sidecar before returning an observed child result.

    Stages the runtime inputs on the host exactly as the bind-mounting backends do, so the
    private staging directory the worker writes its response into exists for the same reason it
    does in production rather than because this double created it.
    """

    result: SandboxRun
    response: dict[str, object]

    def run(
        self,
        argv: list[str],
        *,
        workspace: Path,
        staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
        **_kwargs: Any,
    ) -> SandboxRun:
        with stage_inputs_on_host(workspace, staged_inputs):
            (workspace / argv[-1]).write_text(
                json.dumps(self.response), encoding="utf-8", newline="\n"
            )
        return self.result


class _HostTouch:
    """A pickle payload whose deserialization touches the supplied sentinel."""

    def __init__(self, sentinel: Path) -> None:
        self.sentinel = sentinel

    def __reduce__(self) -> tuple[object, tuple[Path]]:
        return Path.touch, (self.sentinel,)


def _context(tmp_path: Path, *, development: bool = False) -> ToolContext:
    run_id = new_id("run")
    event_log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=event_log,
        gate=Gate(
            PolicyEngine(development_policy() if development else load_policy_stack()), event_log
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_audit_model_does_not_deserialize_an_artifact_in_the_host_process(tmp_path: Path) -> None:
    # The test exercises deserialization isolation after explicit policy authorization.
    context = _context(tmp_path, development=True)
    context.workspace.mkdir()
    source = context.workspace / "tiny.csv"
    source.write_text("value,target,group\n0,no,a\n1,yes,b\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "tiny", produced_by=context.agent_id)
    sentinel = tmp_path / "host-deserialization-sentinel"
    artifact_path = tmp_path / "untrusted.joblib"
    joblib.dump(_HostTouch(sentinel), artifact_path)
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        artifact_path.read_bytes(),
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    manager = ToolManager(ToolRegistry((cast("Tool", AuditModel()),)))

    execution = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/untrusted.joblib",
            "dataset": "tiny",
            "target_column": "target",
            "protected_column": "group",
            "description": "Audit the untrusted model",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert not sentinel.exists(), "untrusted model bytes executed in the host process"


def test_audit_model_records_partial_sandbox_evidence_when_the_sidecar_is_missing(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, development=True)
    context.workspace.mkdir()
    source = context.workspace / "tiny.csv"
    source.write_text("value,target,group\n0,no,a\n1,yes,b\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "tiny", produced_by=context.agent_id)
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        b"the worker does not run in this unit test",
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    sandbox = _CapturingSandbox(
        SandboxRun(
            stdout="child output",
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )
    )
    manager = ToolManager(
        ToolRegistry(
            (cast("Tool", AuditModel(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)),)
        )
    )

    execution = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/untrusted.joblib",
            "dataset": "tiny",
            "target_column": "target",
            "protected_column": "group",
            "description": "Audit the sandbox sidecar model",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert execution.result.error is not None
    assert "sidecar" in execution.result.error
    assert not execution.result.artifact_ids
    assert sandbox.modes == [SandboxMode.DANGER_FULL_ACCESS]
    assert sandbox.argv[0][:6] == [
        "python",
        "-I",
        "-u",
        "-B",
        "-m",
        "thymira.tools.model_prediction_worker",
    ]
    assert not Path(sandbox.argv[0][6]).is_absolute()
    assert not Path(sandbox.argv[0][7]).is_absolute()


def test_audit_model_keeps_partial_evidence_when_the_worker_sidecar_is_invalid(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, development=True)
    context.workspace.mkdir()
    source = context.workspace / "tiny.csv"
    source.write_text("value,target,group\n0,no,a\n1,yes,b\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "tiny", produced_by=context.agent_id)
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        b"the worker does not run in this unit test",
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    sandbox = _WritingSandbox(
        SandboxRun(
            stdout="child output",
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        ),
        {"format_version": 2},
    )
    manager = ToolManager(
        ToolRegistry(
            (cast("Tool", AuditModel(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)),)
        )
    )

    execution = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/untrusted.joblib",
            "dataset": "tiny",
            "target_column": "target",
            "protected_column": "group",
            "description": "Audit the malformed sidecar model",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.sandbox_mode is SandboxMode.DANGER_FULL_ACCESS
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert not execution.result.artifact_ids
    assert context.artifact_store.get("audits/untrusted.joblib.json") is None


def test_audit_model_keeps_partial_evidence_when_post_child_drift_fails(tmp_path: Path) -> None:
    context = _context(tmp_path, development=True)
    context.workspace.mkdir()
    source = context.workspace / "tiny.csv"
    source.write_text("value,target,group\n0,no,a\n1,yes,b\n", encoding="utf-8", newline="\n")
    reference = context.workspace / "reference.csv"
    reference.write_text("value,target\n0,no\n1,yes\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "tiny", produced_by=context.agent_id)
    register_dataset(context.artifact_store, reference, "reference", produced_by=context.agent_id)
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        b"the worker does not run in this unit test",
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    sandbox = _WritingSandbox(
        SandboxRun(
            stdout="child output",
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        ),
        {
            "format_version": 1,
            "feature_names": ["value", "group"],
            "class_labels": ["no", "yes"],
            "predictions": ["no", "yes"],
            "positive_probabilities": [0.1, 0.9],
        },
    )
    manager = ToolManager(
        ToolRegistry(
            (cast("Tool", AuditModel(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS)),)
        )
    )

    execution = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/untrusted.joblib",
            "dataset": "tiny",
            "target_column": "target",
            "protected_column": "group",
            "reference_dataset": "reference",
            "description": "Audit the drifting sidecar model",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.sandbox_mode is SandboxMode.DANGER_FULL_ACCESS
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert execution.result.error is not None
    assert "postprocessing" in execution.result.error
    assert not execution.result.artifact_ids
    assert context.artifact_store.get("audits/untrusted.joblib.json") is None


def test_audit_model_refuses_read_only_before_staging_or_reading_artifacts(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = AuditModel(mode=SandboxMode.READ_ONLY).execute(
        context.for_tool(),
        {
            "model_artifact": "models/unreachable.joblib",
            "dataset": "unreachable",
            "target_column": "target",
            "protected_column": "group",
            "description": "Attempt a confined model audit",
        },
    )

    assert not result.success
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not context.workspace.exists()


def test_audit_model_arguments_reject_a_model_selected_sandbox_mode() -> None:
    with pytest.raises(ValidationError, match="mode"):
        AuditModelArguments.model_validate(
            {
                "model_artifact": "models/model.joblib",
                "dataset": "data",
                "target_column": "target",
                "protected_column": "group",
                "mode": "danger_full_access",
            }
        )
