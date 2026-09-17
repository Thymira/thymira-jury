"""Unit coverage for hostile model-tool sidecar and artifact boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import InMemoryEventLog
from thymira.schemas import ArtifactKind, SandboxEnforcement, SandboxMode, ToolCallStatus, new_id
from thymira.tools import ToolContext, ToolManager, ToolRegistry, register_dataset
from thymira.tools.builtins import InspectModel, RunExperiment
from thymira.tools.model_sidecars import read_bounded_file
from thymira.tools.models import ToolExecutionError
from thymira.tools.sandbox import SandboxRun

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping
    from pathlib import Path

    from thymira.tools.models import Tool
    from thymira.tools.sandbox import Sandbox, StagedInput


@dataclass(frozen=True, slots=True)
class _InspectionSidecarSandbox:
    """Return a successful child result after writing the requested inspection sidecar."""

    report: bytes

    def run(
        self,
        _argv: list[str],
        *,
        workspace: Path,
        mode: SandboxMode,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
    ) -> SandboxRun:
        del _argv, timeout_s, env, staged_inputs
        path = workspace / ".thymira" / "inspection.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.report)
        return SandboxRun(
            stdout="",
            stderr="",
            exit_code=0,
            mode=mode,
            enforcement=SandboxEnforcement.PARTIAL,
        )


@dataclass(frozen=True, slots=True)
class _LinkInspectionSidecarSandbox:
    """Replace the child report with a link to prove the host refuses it."""

    target: Path

    def run(
        self,
        _argv: list[str],
        *,
        workspace: Path,
        mode: SandboxMode,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
    ) -> SandboxRun:
        del _argv, timeout_s, env, staged_inputs
        path = workspace / ".thymira" / "inspection.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(self.target)
        return SandboxRun(
            stdout="",
            stderr="",
            exit_code=0,
            mode=mode,
            enforcement=SandboxEnforcement.PARTIAL,
        )


def _context(tmp_path: Path) -> ToolContext:
    """Build an explicitly approved context for the sidecar boundary tests."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = human_approved_context(tmp_path, run_id=run_id, log=log)
    context.workspace.mkdir(parents=True)
    return context


def _valid_inspection_report() -> dict[str, Any]:
    """Return the complete typed payload emitted by the inspection child."""
    return {
        "class_name": "LogisticRegression",
        "parameters": {"C": 1.0},
        "pipeline_steps": [["classify", "LogisticRegression"]],
        "input_signature": {
            "n_features_in": 1,
            "feature_names_in": ["value"],
            "encoded_feature_names": ["value"],
        },
        "output_signature": {"classes": ["no", "yes"], "n_outputs": 1},
        "coefficients": [[0.25]],
    }


def _execute_inspection(context: ToolContext, report: bytes, *, sandbox: Any = None) -> Any:
    """Execute inspection through the real manager with a deterministic child boundary."""
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        b"model",
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
    )
    inspection = InspectModel(sandbox=cast("Sandbox", sandbox or _InspectionSidecarSandbox(report)))
    return ToolManager(ToolRegistry((cast("Tool", inspection),))).execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/untrusted.joblib",
            "description": "Inspect the untrusted model artifact",
        },
    )


def test_inspect_model_rejects_an_oversized_sidecar_and_keeps_sandbox_evidence(
    tmp_path: Path,
) -> None:
    """A report larger than the protocol bound cannot become a published artifact."""
    context = _context(tmp_path)
    result = _execute_inspection(context, json.dumps({"blob": "x" * (9 * 1024 * 1024)}).encode())

    assert result.call.status is ToolCallStatus.FAILED
    assert result.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert result.result.error is not None
    assert "exceed" in result.result.error
    assert result.result.artifact_ids == ()


def test_inspect_model_rejects_an_untyped_sidecar(tmp_path: Path) -> None:
    """A child report with unknown keys cannot become a report artifact."""
    result = _execute_inspection(
        _context(tmp_path), json.dumps({**_valid_inspection_report(), "unexpected": True}).encode()
    )

    assert result.call.status is ToolCallStatus.FAILED
    assert result.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert result.result.error == "model inspection report is missing or malformed"
    assert result.result.artifact_ids == ()


def test_inspect_model_rejects_a_linked_sidecar(tmp_path: Path) -> None:
    """A child report must be a regular file owned by the workspace protocol."""
    target = tmp_path / "outside.json"
    target.write_bytes(json.dumps(_valid_inspection_report()).encode())
    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(target)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    result = _execute_inspection(
        _context(tmp_path), b"", sandbox=_LinkInspectionSidecarSandbox(target)
    )

    assert result.call.status is ToolCallStatus.FAILED
    assert result.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert result.result.error is not None
    assert "regular file" in result.result.error or "link" in result.result.error
    assert result.result.artifact_ids == ()


def test_inspect_model_publishes_a_valid_typed_sidecar(tmp_path: Path) -> None:
    """A complete typed report remains publishable after protocol validation."""
    context = _context(tmp_path)
    result = _execute_inspection(context, json.dumps(_valid_inspection_report()).encode())

    assert result.call.status is ToolCallStatus.COMPLETED
    assert result.result.artifact_ids
    report = context.artifact_store.load_json("inspection/untrusted.joblib.json")
    assert report["class_name"] == "LogisticRegression"
    assert report["output_signature"]["classes"] == ["no", "yes"]


def test_bounded_sidecar_rejects_a_replaced_file_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar replacement between lstat and fstat cannot be trusted by the host."""
    source = tmp_path / "sidecar.json"
    replacement = tmp_path / "replacement.json"
    source.write_bytes(b"{}")
    replacement.write_bytes(b"replacement")

    def replaced_fstat(descriptor: int) -> os.stat_result:
        del descriptor
        return replacement.stat()

    monkeypatch.setattr("thymira.tools.model_sidecars.os.fstat", replaced_fstat)
    with pytest.raises(ToolExecutionError, match="changed while being read"):
        read_bounded_file(source, max_bytes=1024, label="test sidecar")


def test_run_experiment_rejects_an_oversized_model_sidecar(tmp_path: Path) -> None:
    """A training child cannot publish a model larger than the configured model bound."""
    context = _context(tmp_path)
    dataset = context.workspace / "training.csv"
    dataset.write_text("value,target\n0,no\n1,yes\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, dataset, "training", produced_by=context.agent_id)

    @dataclass(frozen=True, slots=True)
    class _TrainingSandbox:
        def run(
            self,
            _argv: list[str],
            *,
            workspace: Path,
            mode: SandboxMode,
            timeout_s: float,
            env: Mapping[str, str] | None = None,
            staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
        ) -> SandboxRun:
            del _argv, timeout_s, env, staged_inputs
            work_dir = workspace / ".thymira"
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "model.joblib").write_bytes(b"x" * (257 * 1024 * 1024))
            (work_dir / "metrics.json").write_text('{"accuracy": 0.5}', encoding="utf-8")
            return SandboxRun(
                stdout="",
                stderr="",
                exit_code=0,
                mode=mode,
                enforcement=SandboxEnforcement.PARTIAL,
            )

    result = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(sandbox=cast("Sandbox", _TrainingSandbox())),
                ),
            )
        )
    ).execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "oversized",
            "description": "Reject an oversized model.",
            "code": "pass",
        },
    )

    assert result.call.status is ToolCallStatus.FAILED
    assert result.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert result.result.error is not None
    assert "exceed" in result.result.error
    assert context.artifact_store.get("models/oversized.joblib") is None


def test_run_experiment_rejects_untyped_metrics_and_keeps_sandbox_evidence(tmp_path: Path) -> None:
    """A training child cannot publish metrics with the wrong JSON types."""
    context = _context(tmp_path)
    dataset = context.workspace / "training.csv"
    dataset.write_text("value,target\n0,no\n1,yes\n", encoding="utf-8", newline="")
    register_dataset(context.artifact_store, dataset, "training", produced_by=context.agent_id)

    @dataclass(frozen=True, slots=True)
    class _MalformedMetricsSandbox:
        def run(
            self,
            _argv: list[str],
            *,
            workspace: Path,
            mode: SandboxMode,
            timeout_s: float,
            env: Mapping[str, str] | None = None,
            staged_inputs: tuple[StagedInput, ...] | list[StagedInput] | None = None,
        ) -> SandboxRun:
            del _argv, timeout_s, env, staged_inputs
            work_dir = workspace / ".thymira"
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "model.joblib").write_bytes(b"model")
            (work_dir / "metrics.json").write_text('{"accuracy": "0.5"}', encoding="utf-8")
            return SandboxRun(
                stdout="",
                stderr="",
                exit_code=0,
                mode=mode,
                enforcement=SandboxEnforcement.PARTIAL,
            )

    result = ToolManager(
        ToolRegistry(
            (cast("Tool", RunExperiment(sandbox=cast("Sandbox", _MalformedMetricsSandbox()))),)
        )
    ).execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "malformed-metrics",
            "description": "Reject malformed metrics.",
            "code": "pass",
        },
    )

    assert result.call.status is ToolCallStatus.FAILED
    assert result.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert result.result.error == "experiment metrics sidecar is missing or malformed"
    assert context.artifact_store.get("metrics/malformed-metrics.json") is None
