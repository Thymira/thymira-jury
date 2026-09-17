"""The ML agent: AgentSpec + MLResult output schema, helper delegation (THY-19)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.fixtures_tools import development_policy
from thymira.agents import (
    RUNTIME_CONTEXT_FORM,
    AgentContext,
    DelegationDepthExceededError,
    DepthGuard,
    LLMToolCall,
    ScriptedProvider,
    build_agent_tools,
)
from thymira.events import InMemoryEventLog
from thymira.policies import Gate, PolicyEngine, RiskProfile, auto_approve
from thymira.schemas import EventType, ModelRoutePolicy, SandboxMode, Task, TaskStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.thy import (
    ML_AGENT_SPEC,
    ML_TUNING_HELPER_SPEC,
    MLResult,
    ml_agent_catalog,
    run_ml_agent,
)
from thymira.tools import Tool, ToolContext, ToolRegistry
from thymira.tools.builtins import RunPython
from thymira.tools.builtins.mlflow_tools import (
    MlflowEndRun,
    MlflowLogArtifact,
    MlflowLogMetric,
    MlflowLogParam,
    MlflowStartRun,
)

if TYPE_CHECKING:
    from pathlib import Path

TEST_ROUTE_POLICY = ModelRoutePolicy.from_routes(("test-model",), authority="code-owned-tests")


@pytest.fixture(autouse=True)
def _configured_test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the direct AgentContext provider seam to an explicit code-owned test route."""
    for variable in ("THYMIRA_MODEL_FAST", "THYMIRA_MODEL_STANDARD", "THYMIRA_MODEL_FRONTIER"):
        monkeypatch.setenv(variable, "test-model")


def _tool_context(tmp_path: Path, *, run_id: str, log: InMemoryEventLog) -> ToolContext:
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


def _full_registry() -> ToolRegistry:
    """A registry holding every tool `ML_AGENT_SPEC.tool_allowlist` names.

    `build_agent_tools` resolves every allowlisted name up front, even one a given test's script
    never actually calls -- see THY-16's `EXPERIMENT_AGENT_SPEC` tests for the same requirement.
    """
    return ToolRegistry(
        (
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", MlflowStartRun()),
            cast("Tool", MlflowLogParam()),
            cast("Tool", MlflowLogMetric()),
            cast("Tool", MlflowLogArtifact()),
            cast("Tool", MlflowEndRun()),
        )
    )


def test_ml_agent_catalog_registers_both_specs_and_their_output_schemas() -> None:
    catalog = ml_agent_catalog()

    assert catalog.get("ml-agent") is ML_AGENT_SPEC
    assert catalog.get("ml-tuning-helper") is ML_TUNING_HELPER_SPEC
    assert catalog.output_schema("ml-agent") is MLResult
    assert catalog.system_prompt("ml-agent")
    assert catalog.system_prompt("ml-tuning-helper")


def test_ml_agent_only_exposes_its_allowlisted_tools(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = _tool_context(tmp_path, run_id=run_id, log=log)
    registry = ToolRegistry(
        (
            cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),
            cast("Tool", MlflowStartRun()),
            cast("Tool", MlflowLogParam()),
            cast("Tool", MlflowLogMetric()),
            cast("Tool", MlflowLogArtifact()),
            cast("Tool", MlflowEndRun()),
        )
    )

    tools = build_agent_tools(ML_AGENT_SPEC, registry, context)

    assert {tool.name for tool in tools} == {
        "run_python",
        "mlflow_start_run",
        "mlflow_log_param",
        "mlflow_log_metric",
        "mlflow_log_artifact",
        "mlflow_end_run",
    }


def test_ml_tuning_helper_max_depth_permits_exactly_one_helper_level() -> None:
    """orchestrator(0) -> ml-agent(1) -> helper(2): a third level must be refused."""
    DepthGuard().check(2, ML_TUNING_HELPER_SPEC)  # the one level THY-19 actually uses.

    with pytest.raises(DelegationDepthExceededError, match="max_depth"):
        DepthGuard().check(3, ML_TUNING_HELPER_SPEC)


def test_ml_agent_trains_a_baseline_without_asking_for_tuning_help(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = _tool_context(tmp_path, run_id=run_id, log=log)
    registry = _full_registry()
    provider = ScriptedProvider(
        [
            LLMToolCall(
                id="call-1",
                name="run_python",
                arguments={
                    "code": "print('trained')",
                    "description": "Train the candidate model family",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="final_result",
                arguments={
                    "chosen_family": "logistic_regression",
                    "hyperparameters": {"C": "1.0"},
                    "cv_strategy": "5-fold stratified",
                    "metrics": {"auc": 0.81},
                    "needs_tuning_help": False,
                    "tuning_objective": None,
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"), run_id=run_id, agent_id=new_id("agent"), objective="Train a baseline."
    )

    result = run_ml_agent(
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=ml_agent_catalog(),
            event_log=log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    output = cast("MLResult", result.output)
    assert output.chosen_family == "logistic_regression"
    assert not output.needs_tuning_help
    # The only `agent.message` events are the runner's per-step runtime-context snapshots; the
    # agent trained a baseline itself, so it never delegated (no `Delegator`-produced message).
    delegations = [
        e
        for e in log.events()
        if e.type == EventType.AGENT_MESSAGE and e.payload.get("form") != RUNTIME_CONTEXT_FORM
    ]
    assert delegations == []


def test_ml_agent_delegates_to_the_tuning_helper_when_it_asks_for_help(tmp_path: Path) -> None:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    context = _tool_context(tmp_path, run_id=run_id, log=log)
    registry = _full_registry()
    provider = ScriptedProvider(
        [
            # ml-agent's own turn (depth 1): trains a baseline, then asks for tuning help.
            LLMToolCall(
                id="call-1",
                name="run_python",
                arguments={
                    "code": "print('trained')",
                    "description": "Train the candidate model family",
                },
            ),
            LLMToolCall(
                id="call-2",
                name="final_result",
                arguments={
                    "chosen_family": "gradient_boosting",
                    "hyperparameters": {"n_estimators": "100"},
                    "cv_strategy": "5-fold stratified",
                    "metrics": {"auc": 0.77},
                    "needs_tuning_help": True,
                    "tuning_objective": "search n_estimators and max_depth to maximize auc",
                },
            ),
            # ml-tuning-helper's turn (depth 2): runs its own search.
            LLMToolCall(
                id="call-3",
                name="run_python",
                arguments={"code": "print('tuned')", "description": "Search hyperparameters"},
            ),
            LLMToolCall(
                id="call-4",
                name="final_result",
                arguments={
                    "hyperparameters": {"n_estimators": "300", "max_depth": "6"},
                    "best_score": 0.85,
                    "trials": 20,
                },
            ),
        ]
    )
    task = Task(
        id=new_id("task"),
        run_id=run_id,
        agent_id=new_id("agent"),
        objective="Train the best model you can.",
    )

    result = run_ml_agent(
        task,
        AgentContext(
            route_policy=TEST_ROUTE_POLICY,
            catalog=ml_agent_catalog(),
            event_log=log,
            provider=provider,
            tool_registry=registry,
            tool_context=context,
        ),
    )

    assert result.task_status is TaskStatus.COMPLETED
    output = cast("MLResult", result.output)
    assert output.needs_tuning_help
    assert output.chosen_family == "gradient_boosting"  # unmerged: the primary run's own result.

    # Filter out the runner's per-step runtime-context snapshots; the delegation outcome is the
    # single `Delegator`-produced `agent.message` (the one carrying `parent_agent`).
    messages = [
        e
        for e in log.events()
        if e.type == EventType.AGENT_MESSAGE and e.payload.get("form") != RUNTIME_CONTEXT_FORM
    ]
    assert len(messages) == 1
    payload = messages[0].payload
    assert payload["parent_agent"] == "ml-agent"
    assert payload["agent"] == "ml-tuning-helper"
    assert payload["depth"] == 2
    assert payload["status"] == TaskStatus.COMPLETED.value
    summary = json.loads(payload["summary"])
    assert summary["best_score"] == pytest.approx(0.85)
    assert summary["trials"] == 20
