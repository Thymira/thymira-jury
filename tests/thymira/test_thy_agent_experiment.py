"""The Experiment agent: AgentSpec + ExperimentResult output schema (THY-16)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.agents import (
    AgentContext,
    AgentRunner,
    LLMToolCall,
    ScriptedProvider,
    build_agent_tools,
)
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve
from thymira.schemas import ModelRoutePolicy, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import (
    EXPERIMENT_AGENT_SPEC,
    ExperimentResult,
    ThyAgentKind,
    experiment_agent_catalog,
)
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import (
    AuditModel,
    CompareModels,
    ExportPdf,
    GitCommit,
    InspectModel,
    QueryMlflow,
    RunExperiment,
    RunPython,
    WriteFile,
)
from thymira.tools.builtins.mlflow_tools import (
    MlflowEndRun,
    MlflowLogArtifact,
    MlflowLogMetric,
    MlflowLogParam,
    MlflowStartRun,
)
from thymira.tools.mlflow import MlflowTracker

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


_TRACKER_RUN_ID = "run_" + "a" * 32


def _tool_context(tmp_path: Path, *, run_id: str) -> ToolContext:
    log = InMemoryEventLog(run_id)
    return ToolContext(
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


def test_experiment_agent_spec_name_matches_thy_agent_kind_experiment() -> None:
    """Execute resolves a delegated task via `catalog.get(agent_task.agent.value)`."""
    assert EXPERIMENT_AGENT_SPEC.name == ThyAgentKind.EXPERIMENT.value


def test_experiment_agent_catalog_registers_the_spec_and_its_output_schema() -> None:
    catalog = experiment_agent_catalog()

    assert catalog.get("experiment") is EXPERIMENT_AGENT_SPEC
    assert catalog.output_schema("experiment") is ExperimentResult
    assert catalog.system_prompt("experiment")


def test_experiment_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    registry = ToolRegistry(
        (
            cast("Tool", MlflowStartRun()),
            cast("Tool", MlflowLogParam()),
            cast("Tool", MlflowLogMetric()),
            cast("Tool", MlflowLogArtifact()),
            cast("Tool", MlflowEndRun()),
            cast("Tool", RunPython()),
            cast("Tool", RunExperiment()),
            cast("Tool", GitCommit()),
            cast("Tool", AuditModel()),
            cast("Tool", CompareModels()),
            cast("Tool", InspectModel()),
            cast("Tool", QueryMlflow()),
            cast("Tool", WriteFile()),
            cast("Tool", ExportPdf()),
        )
    )

    tools = build_agent_tools(EXPERIMENT_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {
        "mlflow_start_run",
        "mlflow_log_param",
        "mlflow_log_metric",
        "mlflow_log_artifact",
        "mlflow_end_run",
        "run_python",
        "run_experiment",
        "audit_model",
        "compare_models",
        "inspect_model",
        "query_mlflow",
        "write_file",
        "export_pdf",
    }


def test_experiment_agent_runs_one_experiment_through_real_mlflow_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # mlflow_start_run mints its tracker run id via thymira.tools.mlflow.local.new_id; a
    # ScriptedProvider's later calls (log_param, log_metric, ...) are fixed ahead of time and
    # cannot read that id back out of a prior tool result, so it is pinned here instead.
    monkeypatch.setattr("thymira.tools.mlflow.local.new_id", lambda kind: f"{kind}_{'a' * 32}")
    run_id = new_id("run")
    context = _tool_context(tmp_path, run_id=run_id)
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "metrics.json").write_text('{"accuracy": 0.91}', encoding="utf-8")
    registry = ToolRegistry(
        (
            cast("Tool", MlflowStartRun()),
            cast("Tool", MlflowLogParam()),
            cast("Tool", MlflowLogMetric()),
            cast("Tool", MlflowLogArtifact()),
            cast("Tool", MlflowEndRun()),
            cast("Tool", RunPython()),
            cast("Tool", RunExperiment()),
            cast("Tool", AuditModel()),
            cast("Tool", CompareModels()),
            cast("Tool", InspectModel()),
            cast("Tool", QueryMlflow()),
            cast("Tool", WriteFile()),
            cast("Tool", ExportPdf()),
        )
    )
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-1",
                name="mlflow_start_run",
                arguments={
                    "experiment_name": "baseline",
                    "description": "Start the baseline tracker run",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="mlflow_log_param",
                arguments={
                    "run_id": _TRACKER_RUN_ID,
                    "key": "seed",
                    "value": "7",
                    "description": "Log the experiment seed parameter",
                },
            ),
            LLMToolCall(
                id="call-3",
                name="mlflow_log_metric",
                arguments={
                    "run_id": _TRACKER_RUN_ID,
                    "key": "accuracy",
                    "value": 0.91,
                    "description": "Log the experiment accuracy metric",
                },
            ),
            LLMToolCall(
                id="call-4",
                name="mlflow_log_artifact",
                arguments={
                    "run_id": _TRACKER_RUN_ID,
                    "path": "metrics.json",
                    "description": "Log the experiment metrics artifact",
                },
            ),
            LLMToolCall(
                id="call-5",
                name="mlflow_end_run",
                arguments={
                    "run_id": _TRACKER_RUN_ID,
                    "description": "End the baseline tracker run",
                },
            ),
            LLMToolCall(
                id="call-6",
                name="final_result",
                arguments={
                    "parameters": {"seed": "7"},
                    "metrics": {"accuracy": 0.91},
                    "seed": 7,
                    "model_artifact_id": None,
                    "tracker_run_id": _TRACKER_RUN_ID,
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="Run one experiment."
    )

    result = AgentRunner().run(
        EXPERIMENT_AGENT_SPEC,
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=experiment_agent_catalog(),
            event_log=context.event_log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    assert result.output == ExperimentResult(
        parameters={"seed": "7"},
        metrics={"accuracy": 0.91},
        seed=7,
        model_artifact_id=None,
        tracker_run_id=_TRACKER_RUN_ID,
    )
    tracker_run = MlflowTracker(context.workspace).query_runs(run_id=_TRACKER_RUN_ID)[0]
    assert tracker_run.status == "FINISHED"
    assert tracker_run.params == {"seed": "7"}
    assert tracker_run.metrics == {"accuracy": 0.91}
    assert tracker_run.artifacts == (f"artifacts/{_TRACKER_RUN_ID}/metrics.json",)
