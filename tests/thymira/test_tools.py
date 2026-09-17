"""Unit tests for the P3 Tool Registry and Tool Manager lifecycle."""

from __future__ import annotations

import asyncio
import csv
import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import joblib
import numpy as np
import polars as pl
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, Field, ValidationError, field_validator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.tree import DecisionTreeClassifier

from tests.thymira.fixtures_tools import development_policy
from thymira.events import InMemoryEventLog
from thymira.mira.checks import AuditContext, audit_run
from thymira.policies import (
    ActionRule,
    Approver,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    auto_reject,
    load_policy_stack,
)
from thymira.schemas import (
    Actor,
    ActorKind,
    Approval,
    ArtifactKind,
    Decision,
    EventType,
    ExecutionConstraints,
    Experiment,
    PolicyDecision,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    ToolCallStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import (
    BudgetRefusal,
    DatasetSchema,
    LegacyToolValue,
    LocalSubprocessSandbox,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
    input_schema,
    load_dataset,
    register_dataset,
    spill_result,
    tool_guidance,
    tool_intent_sha256,
)
from thymira.tools.builtins import (
    AnalyzeDataset,
    AuditModel,
    CompareModels,
    GitStatus,
    GitWorktreeCreate,
    GitWorktreeList,
    GitWorktreeRemove,
    InspectModel,
    ListFiles,
    ProfileDataset,
    QueryMlflow,
    QuerySql,
    ReadFile,
    RunExperiment,
    RunPython,
    RunStatistics,
    WriteFile,
)
from thymira.tools.builtins import run_experiment as run_experiment_module
from thymira.tools.builtins.audit_model import _drift
from thymira.tools.builtins.audit_model import _finite as audit_finite
from thymira.tools.builtins.data_analysis import _finite as analysis_finite
from thymira.tools.builtins.data_analysis import _histogram
from thymira.tools.builtins.files import WriteFileArguments
from thymira.tools.builtins.mlflow_tools import mlflow_tools
from thymira.tools.builtins.query_sql import _read_only_query
from thymira.tools.builtins.run_experiment import RunExperimentArguments
from thymira.tools.builtins.run_python import RunPythonArguments, builtins_registry
from thymira.tools.builtins.run_statistics import _require_columns, _run_test
from thymira.tools.mcp import McpToolServer
from thymira.tools.mcp.server import _mcp_input_schema
from thymira.tools.mlflow import MlflowTracker
from thymira.tools.model_prediction_worker import _decode_predictions
from thymira.tools.sandbox import container as container_module
from thymira.tools.sandbox.base import ResolvedExecutionSpec, SandboxRun

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp.types import TextContent

    from thymira.schemas import Artifact


@dataclass(frozen=True, slots=True)
class _FakeTool(Tool):
    name: str
    capability: ToolCapability
    description: str = "A fake tool for tests."
    arguments_model: type[BaseModel] | None = None
    result_model = LegacyToolValue
    result: ToolResult = field(
        default_factory=lambda: ToolResult(
            success=True, stdout="ok", value=LegacyToolValue(text="ok")
        )
    )
    raises: bool = False
    writes_artifact: bool = False
    seen_arguments: dict[str, Any] = field(default_factory=dict)
    seen_invocation: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Return the configured result and record what the manager handed over."""
        self.seen_invocation.append(invocation)
        self.seen_arguments.update(arguments)
        if self.raises:
            raise ToolExecutionError("expected failure")
        if self.writes_artifact:
            invocation.artifact_store.save_json(
                "metrics.json",
                {"accuracy": 1.0},
                produced_by=invocation.agent_id,
            )
        return self.result


class _EchoArguments(BaseModel):
    # `description` is the narration every real tool's arguments model declares (RunPython,
    # WriteFile, RunExperiment): the model's account of why it is calling, not part of the
    # effect. It is declared here so the manager's validated arguments carry it, the way a real
    # call does, and `tool_intent_sha256` is exercised on a call that really has one.
    value: str = Field(min_length=1)
    description: str = ""


class _ManyRequiredArguments(BaseModel):
    first: str
    second: str
    third: str
    fourth: str
    fifth: str


_LONG_ARGUMENT_ALIAS = "field_" + ("x" * 600)


class _LongLocationArguments(BaseModel):
    value: str = Field(alias=_LONG_ARGUMENT_ALIAS)


@dataclass(frozen=True, slots=True)
class _FixedSandbox:
    result: SandboxRun

    def run(self, *args: Any, **kwargs: Any) -> SandboxRun:
        return self.result


@dataclass(frozen=True, slots=True)
class _CapturingSandbox:
    result: SandboxRun
    argv: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str], **_kwargs: Any) -> SandboxRun:
        self.argv.append(argv)
        return self.result


@dataclass(frozen=True, slots=True)
class _RaisingSandbox:
    error: Exception

    def run(self, *args: Any, **kwargs: Any) -> SandboxRun:
        raise self.error


@dataclass(frozen=True, slots=True)
class _SuccessfulTrainingSandbox:
    """Write the two validated child outputs without running real training."""

    def run(self, *args: Any, **kwargs: Any) -> SandboxRun:
        workspace = Path(kwargs["workspace"])
        work_dir = workspace / ".thymira"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "model.joblib").write_bytes(b"validated-model")
        (work_dir / "metrics.json").write_text('{"accuracy": 1.0}', encoding="utf-8", newline="\n")
        return SandboxRun(
            stdout='{"accuracy": 1.0}',
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )


class _FailingEndTracker:
    """Complete every tracker operation except the final durable close."""

    def __init__(self, _workspace: Path) -> None:
        pass

    def start_run(self, _experiment_name: str, *, tags: dict[str, str] | None = None) -> str:
        del tags
        return "tracker-run"

    def log_param(self, _run_id: str, _key: str, _value: Any) -> None:
        pass

    def log_metric(self, _run_id: str, _key: str, _value: float) -> None:
        pass

    def log_artifact_bytes(self, _run_id: str, _name: str, _data: bytes) -> None:
        pass

    def end_run(self, _run_id: str, *, status: str = "FINISHED") -> None:
        del status
        raise OSError("injected tracker finalization failure")


def _context(
    tmp_path: Path,
    *,
    capability: ToolCapability,
    approver: Approver | None = auto_approve,
    risk_confidence: float = 1.0,
    development: bool = False,
) -> ToolContext:
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(
            PolicyEngine(development_policy() if development else load_policy_stack()),
            log,
            approver=approver,
        ),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=risk_confidence
        ),
    )


def test_registry_rejects_duplicate_tool_names() -> None:
    capability = ToolCapability(id="echo", external_effects=())
    registry = ToolRegistry((_FakeTool("echo", capability),))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_FakeTool("echo", capability))


def test_manager_refuses_known_credentials_before_intent_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual caller cannot make the chain and the executed intent disagree."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    context = _context(tmp_path, capability=capability)
    monkeypatch.setenv("OPENAI_API_KEY", "known-provider-value")

    execution = manager.execute(context, "echo", {"value": "known-provider-value"})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.arguments == {"value": "[REDACTED:CREDENTIAL]"}
    assert execution.result.error == "tool arguments contain a credential-shaped value"
    assert tool.seen_arguments == {}
    assert all(
        "known-provider-value" not in event.model_dump_json()
        for event in context.event_log.events()
    )
    assert all(event.type is EventType.TOOL_COMPLETED for event in context.event_log.events())


@pytest.mark.parametrize(
    "recorded_id",
    [
        "approval_fixed",
        "bad",
        "tool_00000000000000000000000000000000",
        None,
        "approval_00000000000000000000000000000000\n",
    ],
)
def test_manager_rebuilds_an_approval_with_an_unusable_evidence_id(
    tmp_path: Path, recorded_id
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(
        ToolRegistry((_FakeTool("echo", capability, arguments_model=_EchoArguments),))
    )
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": first.pending_approval.id, "approved": True, "id": recorded_id},
    )

    retried = manager.execute(context, "echo", {"value": "x"})

    assert retried.call.status is ToolCallStatus.COMPLETED
    assert retried.call.policy_decision_id == first.pending_approval.id


@pytest.mark.parametrize("record_invalid_decision", [False, True])
def test_manager_unresolvable_approval_decision_requests_a_fresh_review(
    tmp_path: Path, record_invalid_decision: bool
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(
        ToolRegistry((_FakeTool("echo", capability, arguments_model=_EchoArguments),))
    )
    decision_id = new_id("decision")
    if record_invalid_decision:
        context.event_log.append(EventType.POLICY_DECISION, Actor.system(), {"id": decision_id})
    context.event_log.append(
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor.system(),
        {
            "decision_id": decision_id,
            "tool_intent_sha256": tool_intent_sha256("echo", {"value": "x", "description": ""}),
        },
    )
    _human_answer(context, decision_id, approved=True)

    execution = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is not None
    assert execution.pending_approval.id != decision_id
    report = audit_run(AuditContext(context.run_id, context.event_log.events()))
    assert (
        next(control for control in report.controls if control.control_id == "A3").status.value
        == "NOT_APPLICABLE"
    )
    assert (
        next(control for control in report.controls if control.control_id == "A6").status.value
        == "PASSED"
    )


@pytest.mark.parametrize("refusal", [BudgetRefusal("no budget"), "no budget"])
def test_manager_budget_refusal_without_a_decision_does_not_claim_authority(
    tmp_path: Path, refusal
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = replace(_context(tmp_path, capability=capability), budget_guard=lambda: refusal)
    manager = ToolManager(ToolRegistry((_FakeTool("echo", capability),)))

    execution = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.call.policy_decision_id is not None
    denied = context.event_log.events()[-1]
    assert denied.type is EventType.TOOL_DENIED
    assert denied.payload["decision_id"] is None


def test_tool_descriptor_exposes_a_non_empty_input_schema() -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)

    schema = input_schema(tool)

    assert tool.description
    assert schema["type"] == "object"
    assert "value" in schema["properties"]
    assert schema["required"] == ["value"]


def test_manager_records_invalid_arguments_without_running_or_authorizing_the_tool(
    tmp_path: Path,
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {"value": ""})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert execution.call.error == (
        "invalid arguments for tool 'echo': value: String should have at least 1 character"
    )
    assert tool.seen_arguments == {}
    assert [event.type for event in context.event_log.events()] == [EventType.TOOL_COMPLETED]


def test_manager_reports_a_missing_description_without_echoing_file_content(
    tmp_path: Path,
) -> None:
    tool = cast("Tool", WriteFile())
    context = _context(tmp_path, capability=tool.capability)
    sensitive_content = "private-model-output-that-must-not-appear"

    execution = ToolManager(ToolRegistry((tool,))).execute(
        context,
        "write_file",
        {"path": "report.py", "content": sensitive_content},
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.error == (
        "invalid arguments for tool 'write_file': description: Field required"
    )
    assert sensitive_content not in execution.call.error
    assert [event.type for event in context.event_log.events()] == [EventType.TOOL_COMPLETED]


def test_manager_bounds_the_number_and_length_of_argument_validation_errors(
    tmp_path: Path,
) -> None:
    capability = ToolCapability(id="many", external_effects=())
    tool = _FakeTool("many", capability, arguments_model=_ManyRequiredArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "many", {})

    assert execution.call.status is ToolCallStatus.FAILED
    error = execution.call.error or ""
    detail = error.partition(": ")[2]
    assert "first: Field required" in detail
    assert "third: Field required" in detail
    assert "fourth: Field required" not in detail
    assert "2 more errors" in detail
    assert len(detail) <= 512


def test_manager_bounds_a_long_validation_location_without_losing_the_message(
    tmp_path: Path,
) -> None:
    capability = ToolCapability(id="long", external_effects=())
    tool = _FakeTool("long", capability, arguments_model=_LongLocationArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "long", {})

    error = execution.call.error or ""
    detail = error.partition(": ")[2]
    assert len(detail) <= 512
    assert _LONG_ARGUMENT_ALIAS not in detail
    assert "Field required" in detail


def test_manager_scrubs_a_known_credential_from_a_validation_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plugin-schema-secret-value"  # noqa: S105  # credential redaction fixture
    monkeypatch.setenv("THIRD_PARTY_TOOL_TOKEN", secret)

    class _CredentialAliasArguments(BaseModel):
        value: str = Field(alias=secret)

    capability = ToolCapability(id="plugin", external_effects=())
    tool = _FakeTool("plugin", capability, arguments_model=_CredentialAliasArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "plugin", {})

    error = execution.call.error or ""
    assert secret not in error
    assert "[REDACTED:CREDENTIAL]: Field required" in error


def test_manager_closes_a_plugin_validator_type_error_with_a_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "plugin-validator-secret-value"  # noqa: S105  # redaction fixture
    monkeypatch.setenv("PLUGIN_VALIDATOR_TOKEN", secret)

    class _BrokenPluginArguments(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _broken_validator(cls, value: str) -> str:
            del cls, value
            raise TypeError(f"plugin validator exposed {secret}")

    capability = ToolCapability(id="plugin-validator", external_effects=())
    tool = _FakeTool(
        "plugin-validator",
        capability,
        arguments_model=_BrokenPluginArguments,
    )
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(
        context,
        "plugin-validator",
        {"value": "trigger"},
    )

    error = execution.call.error or ""
    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert "validator raised TypeError" in error
    assert secret not in error
    assert "[REDACTED:CREDENTIAL]" in error
    assert tool.seen_arguments == {}
    events = context.event_log.events()
    assert [event.type for event in events] == [EventType.TOOL_COMPLETED]
    assert events[0].payload["error"] == error


def test_manager_validates_and_executes_valid_arguments(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {"value": "hello"})

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.success is True
    assert tool.seen_arguments == {"value": "hello", "description": ""}


def test_manager_executes_allowed_tool_and_records_lifecycle(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    manager = ToolManager(ToolRegistry((_FakeTool("echo", capability),)))

    execution = manager.execute(context, "echo", {"credential": "secret@example.com"})

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.stdout == "ok"
    assert [event.type for event in context.event_log.events()] == [
        EventType.POLICY_DECISION,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    # The canonical chain keeps the model-visible value so the ticket and request are
    # reconstructible; API and trace copies redact it later.
    assert execution.call.arguments["credential"] == "secret@example.com"
    started = next(e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED)
    assert started.payload["arguments"]["credential"] == "secret@example.com"
    assert context.event_log.verify().valid


def test_a_tool_receives_the_real_arguments_not_the_redacted_ones(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("echo", capability)
    manager = ToolManager(ToolRegistry((tool,)))

    manager.execute(context, "echo", {"credential": "secret@example.com"})

    # Handing the tool the marker would make it authenticate with the literal string
    # "[REDACTED:EMAIL]" and would corrupt any innocent value that merely looks like a credential.
    assert tool.seen_arguments == {"credential": "secret@example.com"}


def test_manager_denies_external_effect_before_execution(tmp_path: Path) -> None:
    capability = ToolCapability(id="publish", external_effects=("network",))
    context = _context(tmp_path, capability=capability)
    manager = ToolManager(ToolRegistry((_FakeTool("publish", capability),)))

    execution = manager.execute(context, "publish")

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.result.success is False
    assert execution.result.error is not None
    assert context.event_log.events()[-1].type is EventType.TOOL_DENIED


ARTIFACT_WRITERS = {
    "analyze_dataset",
    "compare_models",
    "export_pdf",
    "mlflow_end_run",
    "mlflow_log_artifact",
    "mlflow_log_metric",
    "mlflow_log_param",
    "mlflow_start_run",
    "profile_dataset",
    "query_sql",
    "run_statistics",
}
"""Built-ins that persist only their own bounded report inside the workspace (GOV-009)."""

WORKSPACE_WRITERS = {
    "audit_model",
    "edit_file",
    "git_commit",
    "git_worktree_create",
    "git_worktree_remove",
    "inspect_model",
    "run_experiment",
    "run_notebook",
    "run_python",
    "write_file",
}
"""Built-ins that write arbitrary files or run code, still reviewed under GOV-008."""


def test_builtin_capabilities_declare_their_kind_of_write() -> None:
    """Every built-in that persists state advertises which local side effect it has."""
    tools = {tool.name: tool for tool in builtins_registry()}

    assert tools.keys() >= ARTIFACT_WRITERS | WORKSPACE_WRITERS
    assert all(
        tools[name].capability.side_effects == ("artifact_write",) for name in ARTIFACT_WRITERS
    )
    assert all(
        "workspace_write" in tools[name].capability.side_effects for name in WORKSPACE_WRITERS
    )


@pytest.mark.parametrize(
    ("constraints", "capability", "available_evidence", "expected"),
    [
        (
            ExecutionConstraints(allowed_tools=("other",)),
            ToolCapability(id="echo"),
            (),
            "allow-list",
        ),
        (
            ExecutionConstraints(prohibited_tools=("echo",)),
            ToolCapability(id="echo"),
            (),
            "prohibited",
        ),
        (
            ExecutionConstraints(local_execution_only=True),
            ToolCapability(id="echo", external_effects=("network",)),
            (),
            "local-only",
        ),
        (
            ExecutionConstraints(required_evidence=("dataset-registered",)),
            ToolCapability(id="echo"),
            (),
            "evidence",
        ),
    ],
)
def test_manager_enforces_every_execution_constraint_before_a_tool_runs(
    tmp_path: Path,
    constraints: ExecutionConstraints,
    capability: ToolCapability,
    available_evidence: tuple[str, ...],
    expected: str,
) -> None:
    """Every hard constraint refuses before the Gate is asked, and no human can lift it.

    ``requires_human_review`` is deliberately *not* one of these rows any more. It used to be:
    the manager answered it with a blanket ``HUMAN_REVIEW_REFUSAL`` for every call, which made
    the run-level review a human had already approved lock the Run out of every tool forever.
    It is now a per-call review inherited by the capability decision, so it is pinned by
    ``test_manager_asks_a_human_for_each_call_when_the_run_requires_execution_review`` and its
    neighbours below instead of by a refusal string.
    """
    policy = Policy(
        name="constraints",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=Decision.PASS,
                reason="test execution",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="CONSTRAIN",
                decision=Decision.PASS,
                reason="test constraints",
                execution_constraints=constraints,
            ),
        ),
    )
    base = _context(tmp_path, capability=capability)
    gate = Gate(PolicyEngine(policy), base.event_log, approver=auto_approve)
    decision = gate.authorize_execution(base.risk_profile, (capability,))
    context = replace(
        base,
        gate=gate,
        execution_constraints=decision.execution_constraints,
        execution_decision=decision,
        available_evidence=frozenset(available_evidence),
    )
    tool = _FakeTool("echo", capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo")

    assert execution.call.status is ToolCallStatus.DENIED
    assert expected in (execution.result.error or "")
    assert tool.seen_invocation == []


_START_RULE = ActionRule(
    id="START",
    action_types=("execution.start",),
    decision=Decision.PASS,
    reason="test execution",
)


def _run_constrained_context(
    tmp_path: Path,
    *,
    policy: Policy,
    capabilities: tuple[ToolCapability, ...],
) -> tuple[ToolContext, PolicyDecision]:
    """A deferred-Gate context whose run-level decision really records the run-wide constraints.

    The risk profile is confident, so nothing here is escalated by the fail-safe uncertainty
    path: whatever review a call ends up under came from the execution constraints, not from a
    thin classification.
    """
    base = _context(tmp_path, capability=capabilities[0], approver=None)
    gate = Gate(PolicyEngine(policy), base.event_log)
    execution = gate.authorize_execution(base.risk_profile, capabilities)
    context = replace(
        base,
        gate=gate,
        execution_constraints=execution.execution_constraints,
        execution_decision=execution,
    )
    return context, execution


_REVIEW_CONSTRAINT_POLICY = Policy(
    name="constraint-review",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(
            id="CONSTRAIN",
            decision=Decision.PASS,
            reason="test constraints",
            execution_constraints=ExecutionConstraints(requires_human_review=True),
        ),
    ),
)


def test_manager_asks_a_human_for_each_call_when_the_run_requires_execution_review(
    tmp_path: Path,
) -> None:
    """A run-wide review requirement gates each call separately instead of refusing them all.

    The shipped ``credit_risk`` rule CR-001 carries ``requires_human_review``, so under the old
    blanket refusal no tool could ever run -- not even after a human approved the execution-start
    review. The requirement is now inherited by the capability decision, which turns every call
    into an ordinary ticketed review: denied pending with its own identity on the log, run once
    by one human answer, and pending again under a fresh decision for the next identical call.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context, execution = _run_constrained_context(
        tmp_path, policy=_REVIEW_CONSTRAINT_POLICY, capabilities=(capability,)
    )
    assert execution.decision is Decision.REQUIRE_HUMAN_REVIEW
    manager = ToolManager(ToolRegistry((tool,)))

    first = manager.execute(context, "echo", {"value": "x", "description": "first phrasing"})

    assert first.call.status is ToolCallStatus.DENIED
    assert first.pending_approval is not None
    assert first.pending_approval.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert first.pending_approval.id != execution.id
    assert first.pending_approval.execution_constraints.requires_human_review is True
    ticket = tool_intent_sha256("echo", {"value": "x"})
    requested = [
        event
        for event in context.event_log.events()
        if event.type is EventType.HUMAN_APPROVAL_REQUESTED
    ][-1]
    assert requested.payload["tool"] == "echo"
    assert requested.payload["arguments"] == {"value": "x", "description": "first phrasing"}
    assert requested.payload["tool_intent_sha256"] == ticket
    denied = context.event_log.events()[-1]
    assert denied.type is EventType.TOOL_DENIED
    assert denied.payload["tool_intent_sha256"] == ticket
    assert denied.payload["decision_id"] == first.pending_approval.id
    assert tool.seen_invocation == []

    _human_answer(context, first.pending_approval.id, approved=True)
    retry = replace(context, agent_id=new_id("agent"))
    second = manager.execute(retry, "echo", {"value": "x", "description": "second phrasing"})
    third = manager.execute(retry, "echo", {"value": "x", "description": "again"})

    assert second.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen_invocation) == 1
    started = next(e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED)
    assert started.payload["decision_id"] == first.pending_approval.id
    assert third.call.status is ToolCallStatus.DENIED
    assert third.pending_approval is not None
    assert third.pending_approval.id not in {first.pending_approval.id, execution.id}


_TRAIN_CAPABILITY = ToolCapability(id="train", risk_tags=("model_training",), external_effects=())
_PROFILE_CAPABILITY = ToolCapability(id="profile", external_effects=())
_INHERITED_REVIEW_POLICY = Policy(
    name="inherited-review",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(
            id="TRAIN",
            risk_tags=("model_training",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Training needs a human.",
            execution_constraints=ExecutionConstraints(requires_human_review=True),
        ),
        CapabilityRule(
            id="LOCAL",
            external_effects=(),
            decision=Decision.PASS,
            reason="Local tools are allowed.",
        ),
    ),
)


def test_manager_escalates_a_passing_call_under_the_runs_inherited_review_requirement(
    tmp_path: Path,
) -> None:
    """A tool whose own rule PASSes is still reviewed while the Run's constraints demand it.

    The requirement comes from another capability's rule (``TRAIN``), so it exists only in the
    run-wide constraints the execution decision recorded. Without them the identical call passes
    and runs, which is what makes this inheritance and not the tool's own rule.
    """
    tool = _FakeTool("profile", _PROFILE_CAPABILITY, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    context, _ = _run_constrained_context(
        tmp_path,
        policy=_INHERITED_REVIEW_POLICY,
        capabilities=(_TRAIN_CAPABILITY, _PROFILE_CAPABILITY),
    )
    assert context.execution_constraints.requires_human_review is True

    inherited = manager.execute(context, "profile", {"value": "x"})

    assert inherited.call.status is ToolCallStatus.DENIED
    assert inherited.pending_approval is not None
    assert inherited.pending_approval.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert inherited.pending_approval.rule_id == "LOCAL"
    assert inherited.pending_approval.reason == (
        "Local tools are allowed. Execution constraints require human review."
    )
    assert inherited.pending_approval.execution_constraints.requires_human_review is True
    assert tool.seen_invocation == []

    unconstrained = ToolManager(
        ToolRegistry((_FakeTool("profile", _PROFILE_CAPABILITY, arguments_model=_EchoArguments),))
    )
    plain = _context(tmp_path, capability=_PROFILE_CAPABILITY, approver=None)
    plain = replace(plain, gate=Gate(PolicyEngine(_INHERITED_REVIEW_POLICY), plain.event_log))

    unconstrained_call = unconstrained.execute(plain, "profile", {"value": "x"})

    assert unconstrained_call.call.status is ToolCallStatus.COMPLETED


def test_manager_never_treats_the_execution_start_approval_as_a_tool_ticket(
    tmp_path: Path,
) -> None:
    """Approving the start of THY authorises no tool call: the start request carries no ticket.

    ``Gate.authorize_execution`` writes a ``human.approval_requested`` naming no
    ``tool_intent_sha256``, so ``_ticket_bindings`` binds nothing to it and the human's yes can
    never be spent by a call. The call still mints and waits on its own review.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context, execution = _run_constrained_context(
        tmp_path, policy=_REVIEW_CONSTRAINT_POLICY, capabilities=(capability,)
    )
    _human_answer(context, execution.id, approved=True)

    denied = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert denied.pending_approval is not None
    assert denied.pending_approval.id != execution.id
    recorded = context.event_log.events()[-1]
    assert recorded.type is EventType.TOOL_DENIED
    assert recorded.payload["decision_id"] == denied.pending_approval.id
    assert tool.seen_invocation == []


_STALE_ANSWER_POLICY = Policy(
    name="stale-answer",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(
            id="TRAIN",
            risk_tags=("model_training",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Training needs a human.",
            execution_constraints=ExecutionConstraints(requires_human_review=True),
        ),
        CapabilityRule(
            id="LOCAL",
            external_effects=(),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Local tools need a human.",
        ),
    ),
)


def test_manager_refuses_an_answer_given_under_weaker_constraints_than_now_apply(
    tmp_path: Path,
) -> None:
    """An approval recorded before the Run inherited a review requirement is not a ticket.

    The decision the human answered records the constraints that were in force when it was
    minted. Once the Run's own constraints tighten, that answer describes an authorisation the
    engine would no longer give, so the retry mints a fresh review instead of spending it.
    """
    tool = _FakeTool("profile", _PROFILE_CAPABILITY, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    base = _context(tmp_path, capability=_PROFILE_CAPABILITY, approver=None)
    gate = Gate(PolicyEngine(_STALE_ANSWER_POLICY), base.event_log)
    unconstrained = replace(base, gate=gate)
    first = manager.execute(unconstrained, "profile", {"value": "x"})
    assert first.pending_approval is not None
    assert first.pending_approval.execution_constraints == ExecutionConstraints()
    _human_answer(unconstrained, first.pending_approval.id, approved=True)
    execution = gate.authorize_execution(
        base.risk_profile, (_TRAIN_CAPABILITY, _PROFILE_CAPABILITY)
    )
    constrained = replace(
        unconstrained,
        execution_constraints=execution.execution_constraints,
        execution_decision=execution,
    )

    retry = manager.execute(constrained, "profile", {"value": "x"})

    assert retry.call.status is ToolCallStatus.DENIED
    assert retry.pending_approval is not None
    assert retry.pending_approval.id != first.pending_approval.id
    assert retry.pending_approval.execution_constraints.requires_human_review is True
    assert tool.seen_invocation == []

    # The refreshed review is a whole answer of its own: it buys one execution, and one only.
    _human_answer(constrained, retry.pending_approval.id, approved=True)
    allowed = manager.execute(constrained, "profile", {"value": "x"})
    again = manager.execute(constrained, "profile", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert allowed.call.policy_decision_id == retry.pending_approval.id
    assert len(tool.seen_invocation) == 1
    assert again.call.status is ToolCallStatus.DENIED
    assert again.pending_approval is not None
    assert again.pending_approval.id not in {first.pending_approval.id, retry.pending_approval.id}


@pytest.mark.parametrize("spend_the_weaker_answer_first", [False, True])
def test_manager_never_lets_a_stale_approval_pay_for_a_second_execution(
    tmp_path: Path, spend_the_weaker_answer_first: bool
) -> None:
    """An unspent approval given under weaker constraints buys nothing once they tighten.

    Both answers are bound to the same ticket, so pooling them by count alone credited the call
    twice for one human yes under the constraints that now apply: the effect ran again on a
    stale credit. Credits are held to the constraints in force, and every execution spends the
    strongest credit standing at that point in the log, so the weaker answer can only ever pay
    for an execution recorded while it was still the current one.
    """
    tool = _FakeTool("profile", _PROFILE_CAPABILITY, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    base = _context(tmp_path, capability=_PROFILE_CAPABILITY, approver=None)
    gate = Gate(PolicyEngine(_STALE_ANSWER_POLICY), base.event_log)
    unconstrained = replace(base, gate=gate)
    weaker = manager.execute(unconstrained, "profile", {"value": "x"})
    assert weaker.pending_approval is not None
    _human_answer(unconstrained, weaker.pending_approval.id, approved=True)
    if spend_the_weaker_answer_first:
        assert (
            manager.execute(unconstrained, "profile", {"value": "x"}).call.status
            is ToolCallStatus.COMPLETED
        )
    execution = gate.authorize_execution(
        base.risk_profile, (_TRAIN_CAPABILITY, _PROFILE_CAPABILITY)
    )
    constrained = replace(
        unconstrained,
        execution_constraints=execution.execution_constraints,
        execution_decision=execution,
    )
    stronger = manager.execute(constrained, "profile", {"value": "x"})
    assert stronger.pending_approval is not None
    _human_answer(constrained, stronger.pending_approval.id, approved=True)

    allowed = manager.execute(constrained, "profile", {"value": "x"})
    repeated = manager.execute(constrained, "profile", {"value": "x"})

    assert allowed.call.status is ToolCallStatus.COMPLETED
    assert repeated.call.status is ToolCallStatus.DENIED
    assert repeated.pending_approval is not None
    assert len(tool.seen_invocation) == 1 + int(spend_the_weaker_answer_first)


_SCOPE_CAPABILITY = ToolCapability(id="scope", risk_tags=("scope_source",), external_effects=())
_PROBE_CAPABILITY = ToolCapability(id="echo", risk_tags=("probe",), external_effects=())
_ALLOW_LIST_REUSE_POLICY = Policy(
    name="allowlist-reuse",
    version="1.0",
    action_rules=(_START_RULE,),
    capability_rules=(
        CapabilityRule(
            id="SCOPE",
            risk_tags=("scope_source",),
            decision=Decision.PASS,
            reason="Declare the allowed probe.",
            execution_constraints=ExecutionConstraints(allowed_tools=("echo",)),
        ),
        CapabilityRule(
            id="NARROW",
            risk_tags=("probe",),
            risk_factors=("narrowed",),
            decision=Decision.PASS,
            reason="Only the other tool is allowed now.",
            execution_constraints=ExecutionConstraints(allowed_tools=("other",)),
        ),
        CapabilityRule(
            id="REVIEW",
            risk_tags=("probe",),
            decision=Decision.REQUIRE_HUMAN_REVIEW,
            reason="Review the exact call.",
        ),
    ),
)


def test_manager_refuses_a_cached_approval_once_the_engine_would_block_the_call(
    tmp_path: Path,
) -> None:
    """An approval recorded under no constraints cannot outlive a conflict the engine now refuses.

    The earlier review recorded empty constraints; the Run then authorised
    `allowed_tools=("echo",)` and a new risk factor activated a rule allowing only another tool.
    Merging two disjoint allow-lists collapses to the empty tuple, which reads back as "nothing is
    narrowed" -- so a manager comparing merges of its own would have found the old empty-constraint
    approval current and run the call the engine refuses outright. The eligibility test goes
    through the engine's own non-recording evaluation instead, and a BLOCK there makes every
    stored answer unusable, so the call takes the Gate path and the refusal is recorded.
    """
    tool = _FakeTool("echo", _PROBE_CAPABILITY, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    base = _context(tmp_path, capability=_PROBE_CAPABILITY, approver=None)
    engine = PolicyEngine(_ALLOW_LIST_REUSE_POLICY)
    gate = Gate(engine, base.event_log)
    wide = replace(base, gate=gate)
    reviewed = manager.execute(wide, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    assert reviewed.pending_approval.execution_constraints == ExecutionConstraints()
    _human_answer(wide, reviewed.pending_approval.id, approved=True)
    execution = gate.authorize_execution(base.risk_profile, (_PROBE_CAPABILITY, _SCOPE_CAPABILITY))
    assert execution.execution_constraints.allowed_tools == ("echo",)
    narrowed = replace(
        wide,
        risk_profile=base.risk_profile.model_copy(update={"risk_factors": ("narrowed",)}),
        execution_decision=execution,
        execution_constraints=execution.execution_constraints,
    )
    # The policy never changed, so the stale answer is not caught by the policy hash.
    assert reviewed.pending_approval.policy_sha256 == engine.policy_sha256

    denied = manager.execute(narrowed, "echo", {"value": "x"})

    assert denied.call.status is ToolCallStatus.DENIED
    assert denied.pending_approval is None
    assert tool.seen_invocation == []
    assert not any(e.type is EventType.TOOL_STARTED for e in narrowed.event_log.events())
    blocking = [
        e
        for e in narrowed.event_log.events()
        if e.type is EventType.POLICY_DECISION and e.payload["decision"] == Decision.BLOCK.value
    ]
    assert len(blocking) == 1
    assert denied.call.policy_decision_id == blocking[0].payload["id"]
    assert narrowed.event_log.events()[-1].payload["decision_id"] == blocking[0].payload["id"]


@pytest.mark.parametrize("claimed", ["stale", "unknown"], ids=["stale-decision", "unknown-id"])
def test_manager_counts_a_recorded_execution_that_names_no_current_decision(
    tmp_path: Path, claimed: str
) -> None:
    """A start nobody can attribute still spends the ticket's strongest standing credit.

    A `tool.started` claiming a decision the constraints have invalidated, or one the log never
    recorded, is an execution of this exact effect all the same. Leaving it unpaid would let a
    writer record the effect and keep the human's fresh answer available for a second run of it.
    """
    tool = _FakeTool("profile", _PROFILE_CAPABILITY, arguments_model=_EchoArguments)
    manager = ToolManager(ToolRegistry((tool,)))
    base = _context(tmp_path, capability=_PROFILE_CAPABILITY, approver=None)
    gate = Gate(PolicyEngine(_STALE_ANSWER_POLICY), base.event_log)
    unconstrained = replace(base, gate=gate)
    weaker = manager.execute(unconstrained, "profile", {"value": "x"})
    assert weaker.pending_approval is not None
    _human_answer(unconstrained, weaker.pending_approval.id, approved=True)
    execution = gate.authorize_execution(
        base.risk_profile, (_TRAIN_CAPABILITY, _PROFILE_CAPABILITY)
    )
    constrained = replace(
        unconstrained,
        execution_constraints=execution.execution_constraints,
        execution_decision=execution,
    )
    stronger = manager.execute(constrained, "profile", {"value": "x"})
    assert stronger.pending_approval is not None
    _human_answer(constrained, stronger.pending_approval.id, approved=True)
    call_id = new_id("tool")
    constrained.event_log.append(
        EventType.TOOL_STARTED,
        Actor(kind=ActorKind.TOOL, id="profile", authenticated=True),
        {
            "tool_call_id": call_id,
            "tool": "profile",
            "arguments": {"value": "x", "description": ""},
            "tool_intent_sha256": tool_intent_sha256("profile", {"value": "x"}),
            "decision_id": (
                weaker.pending_approval.id if claimed == "stale" else new_id("decision")
            ),
        },
        subject_id=call_id,
    )

    retry = manager.execute(constrained, "profile", {"value": "x"})

    assert retry.call.status is ToolCallStatus.DENIED
    assert retry.pending_approval is not None
    assert tool.seen_invocation == []


def test_manager_enforces_the_authorised_tool_call_limit(tmp_path: Path) -> None:
    constraints = ExecutionConstraints(max_tool_calls=1)
    capability = ToolCapability(id="echo")
    policy = Policy(
        name="limit",
        version="1.0",
        action_rules=(
            ActionRule(
                id="START",
                action_types=("execution.start",),
                decision=Decision.PASS,
                reason="test execution",
            ),
        ),
        capability_rules=(
            CapabilityRule(
                id="LIMIT",
                decision=Decision.PASS,
                reason="one call",
                execution_constraints=constraints,
            ),
        ),
    )
    base = _context(tmp_path, capability=capability)
    gate = Gate(PolicyEngine(policy), base.event_log)
    decision = gate.authorize_execution(base.risk_profile, (capability,))
    context = replace(
        base,
        gate=gate,
        execution_constraints=decision.execution_constraints,
        execution_decision=decision,
    )
    manager = ToolManager(ToolRegistry((_FakeTool("echo", capability),)))

    assert manager.execute(context, "echo").call.status is ToolCallStatus.COMPLETED
    limited = manager.execute(context, "echo")

    assert limited.call.status is ToolCallStatus.DENIED
    assert "maximum number" in (limited.result.error or "")


def test_a_review_the_approver_declined_denies_the_call_and_never_runs_the_tool(
    tmp_path: Path,
) -> None:
    # The one branch where `approved` decides anything. A local tool with no external effects is
    # a PASS under GOV-005, which the engine then escalates to REQUIRE_HUMAN_REVIEW because the
    # risk profile sits under `minimum_risk_confidence` (0.75) — the fail-safe path. From there,
    # `approved is True` is the whole distance between "a human approved this" and "a model asked
    # nicely", and every other test in this module builds the Gate with `auto_approve`.
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability)
    context = _context(tmp_path, capability=capability, approver=auto_reject, risk_confidence=0.1)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo")

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.result.success is False
    assert context.event_log.events()[-1].type is EventType.TOOL_DENIED
    # The load-bearing assertion: denied has to mean it did not run, not merely that the record
    # was labelled denied afterwards.
    assert tool.seen_invocation == []


def test_a_synchronous_approval_still_never_runs_a_review_gated_tool(tmp_path: Path) -> None:
    """An *automatic* same-call approval is evidence, not an authorization for a review-gated tool.

    Contract 0.3 separates a ``PolicyDecision`` from the ``Approval`` that resolves it, and only a
    human can supply the second. ``auto_approve`` records a ``human.approval`` under the
    automation actor, so the answer is on the log and the call is still denied. A synchronous
    *human* approver's yes is a different matter -- it authorizes this very call, through the same
    ``allows_execution`` check; that is
    ``test_a_synchronous_human_yes_authorizes_the_same_call_once`` below.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability)
    context = _context(tmp_path, capability=capability, approver=auto_approve, risk_confidence=0.1)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo")

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.result.success is False
    assert tool.seen_invocation == []
    approval_event = next(
        event for event in context.event_log.events() if event.type is EventType.HUMAN_APPROVAL
    )
    assert approval_event.payload["approved"] is True
    assert context.event_log.events()[-1].type is EventType.TOOL_DENIED


def _human_approver(*, approved: bool) -> tuple[Approver, Actor]:
    """A synchronous approver standing for a real human at a console, and who they are."""
    reviewer = Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True)
    return (lambda _request: approved), reviewer


def test_a_synchronous_human_yes_authorizes_the_same_call_once(tmp_path: Path) -> None:
    """Bug-hunt C5: the Gate's synchronous human answer is the ticket for this very call.

    ``Gate._record`` consults an in-process **human** approver inside ``check_capability`` and
    writes ``human.approval`` as evidence before returning. The manager re-reads the log once
    through the same ``_human_answer`` fold a retry uses, so a synchronous yes authorizes this
    call through the same ``allows_execution`` check instead of denying the call the human just
    approved.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    approver, reviewer = _human_approver(approved=True)
    base = _context(tmp_path, capability=capability, approver=approver, risk_confidence=0.1)
    context = replace(
        base, gate=Gate(base.gate.engine, base.event_log, approver=approver, human=reviewer)
    )
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert len(tool.seen_invocation) == 1
    events = context.event_log.events()
    decisions = [e for e in events if e.type is EventType.POLICY_DECISION]
    approvals = [e for e in events if e.type is EventType.HUMAN_APPROVAL]
    started = next(e for e in events if e.type is EventType.TOOL_STARTED)
    assert len(decisions) == 1
    assert len(approvals) == 1
    assert approvals[0].payload["automatic"] is False
    assert started.payload["decision_id"] == decisions[0].payload["id"]
    assert execution.call.policy_decision_id == decisions[0].payload["id"]
    # The second identical call is a new decision, answered yes again by the same human: it runs
    # too -- each execution has its own decision and its own answer on the log.
    again = manager.execute(context, "echo", {"value": "x"})
    assert again.call.status is ToolCallStatus.COMPLETED
    assert len([e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]) == 2
    report = audit_run(
        AuditContext(
            context.run_id, context.event_log.events(), None, context.gate.engine.policy_sha256
        )
    )
    assert (
        next(control for control in report.controls if control.control_id == "A3").status.value
        == "PASSED"
    )
    assert next(
        control for control in report.controls if control.control_id == "A6"
    ).status.value in {"PASSED", "NOT_APPLICABLE"}


def test_a_synchronous_human_no_is_a_final_rejection(tmp_path: Path) -> None:
    """Bug-hunt C5: a synchronous human's no is the rejection, final for this Run.

    Unlike the yes case, the manager never has to ask the Gate a second time: the top-of-``execute``
    ticket fold already finds the recorded rejection and denies the retry without minting a fresh
    decision.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    approver, reviewer = _human_approver(approved=False)
    base = _context(tmp_path, capability=capability, approver=approver, risk_confidence=0.1)
    context = replace(
        base, gate=Gate(base.gate.engine, base.event_log, approver=approver, human=reviewer)
    )
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "echo", {"value": "x"})
    again = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is None
    assert execution.result.error == "tool call rejected by a human"
    assert again.call.status is ToolCallStatus.DENIED
    assert again.result.error == "tool call rejected by a human"
    assert len([e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]) == 1
    assert tool.seen_invocation == []
    report = audit_run(
        AuditContext(
            context.run_id, context.event_log.events(), None, context.gate.engine.policy_sha256
        )
    )
    assert (
        next(control for control in report.controls if control.control_id == "A3").status.value
        == "NOT_APPLICABLE"
    )
    assert (
        next(control for control in report.controls if control.control_id == "A6").status.value
        == "PASSED"
    )


def test_a_non_bool_synchronous_answer_never_authorizes_the_call(tmp_path: Path) -> None:
    """Ruling R16: a non-bool answer is no answer, so the manager still denies the call pending.

    ``Gate._record`` no longer writes any ``human.approval`` for an approver returning
    ``"yes"`` -- a truthy non-bool that ``bool()`` used to coerce into an authorisation. With no
    answer on the log, the manager's re-read (Task 7 / bug-hunt C5) finds nothing and the call is
    denied, pending a real human's answer, exactly as an unanswered review always was.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    reviewer = Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True)
    base = _context(tmp_path, capability=capability, approver=auto_approve, risk_confidence=0.1)
    non_bool_approver = cast("Approver", lambda _r: "yes")
    context = replace(
        base,
        gate=Gate(base.gate.engine, base.event_log, approver=non_bool_approver, human=reviewer),
    )
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is not None
    assert len(tool.seen_invocation) == 0
    assert not any(event.type is EventType.HUMAN_APPROVAL for event in context.event_log.events())
    report = audit_run(
        AuditContext(
            context.run_id, context.event_log.events(), None, context.gate.engine.policy_sha256
        )
    )
    assert (
        next(control for control in report.controls if control.control_id == "A3").status.value
        == "NOT_APPLICABLE"
    )
    assert (
        next(control for control in report.controls if control.control_id == "A6").status.value
        == "PASSED"
    )


def _human_answer(context: ToolContext, decision_id: str, *, approved: bool) -> None:
    """Record what `Gate.resolve_pending_approval` writes when a real human answers."""
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": decision_id, "approved": approved, "automatic": False},
        subject_id=decision_id,
    )


def test_a_review_required_call_is_denied_pending_and_leaves_its_identity_on_the_log(
    tmp_path: Path,
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)

    execution = ToolManager(ToolRegistry((tool,))).execute(
        context, "echo", {"value": "x", "description": "first phrasing"}
    )

    assert execution.call.status is ToolCallStatus.DENIED
    assert execution.pending_approval is not None
    assert execution.pending_approval.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert execution.call.completed_at is not None
    ticket = tool_intent_sha256("echo", {"value": "x"})
    requested = next(
        e for e in context.event_log.events() if e.type is EventType.HUMAN_APPROVAL_REQUESTED
    )
    assert requested.payload["tool"] == "echo"
    assert requested.payload["arguments"] == {"value": "x", "description": "first phrasing"}
    assert requested.payload["tool_intent_sha256"] == ticket
    denied = context.event_log.events()[-1]
    assert denied.type is EventType.TOOL_DENIED
    assert denied.payload["tool_intent_sha256"] == ticket
    assert denied.payload["decision_id"] == execution.pending_approval.id
    assert tool.seen_invocation == []


def test_a_human_approval_of_the_exact_call_authorizes_it_once(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x", "description": "first phrasing"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    # A different narration and a different agent: the same call.
    retry_context = replace(context, agent_id=new_id("agent"))
    second = manager.execute(
        retry_context, "echo", {"value": "x", "description": "second phrasing"}
    )
    third = manager.execute(retry_context, "echo", {"value": "x", "description": "again"})

    assert second.call.status is ToolCallStatus.COMPLETED
    assert second.call.policy_decision_id == first.pending_approval.id
    assert len(tool.seen_invocation) == 1
    started = next(e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED)
    assert started.payload["tool_intent_sha256"] == tool_intent_sha256("echo", {"value": "x"})
    assert started.payload["decision_id"] == first.pending_approval.id
    # Exactly one decision for the approved execution: the ticket never re-asks the Gate.
    decisions = [e for e in context.event_log.events() if e.type is EventType.POLICY_DECISION]
    assert [d.payload["id"] for d in decisions][:1] == [first.pending_approval.id]
    # The answer is spent: the third identical call needs a new decision and a new human.
    assert third.call.status is ToolCallStatus.DENIED
    assert third.pending_approval is not None
    assert third.pending_approval.id != first.pending_approval.id
    assert len(decisions) == 2


def test_the_three_events_of_one_ticketed_call_record_the_same_arguments(tmp_path: Path) -> None:
    """The request, the denial and the start describe one call, so they carry one payload.

    All three are keyed to the same ``tool_intent_sha256``, which is computed from the *validated*
    arguments, so a start recording the caller's raw request would describe a call whose ticket
    cannot be recomputed from its own payload.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    second = manager.execute(context, "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.COMPLETED
    events = context.event_log.events()
    requested = next(e for e in events if e.type is EventType.HUMAN_APPROVAL_REQUESTED)
    denied = next(e for e in events if e.type is EventType.TOOL_DENIED)
    started = next(e for e in events if e.type is EventType.TOOL_STARTED)
    assert (
        requested.payload["arguments"]
        == denied.payload["arguments"]
        == started.payload["arguments"]
        == {"value": "x", "description": ""}
    )
    assert (
        started.payload["tool_intent_sha256"]
        == requested.payload["tool_intent_sha256"]
        == tool_intent_sha256("echo", started.payload["arguments"])
    )


def test_tool_started_records_a_default_the_arguments_model_filled(tmp_path: Path) -> None:
    """A default the arguments model supplies is part of what ran, so the start records it."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {"value": "x"})

    assert execution.call.status is ToolCallStatus.COMPLETED
    started = next(e for e in context.event_log.events() if e.type is EventType.TOOL_STARTED)
    assert started.payload["arguments"] == {"value": "x", "description": ""}
    # The recorded ToolCall keeps the request as the caller made it, unchanged.
    assert execution.call.arguments == {"value": "x"}


def test_an_automatic_answer_never_authorizes_a_retry(tmp_path: Path) -> None:
    """`auto_approve` records `automatic: true`; a ticket is built from a human's answer only."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=auto_approve, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))

    first = manager.execute(context, "echo", {"value": "x"})
    second = manager.execute(context, "echo", {"value": "x"})

    assert first.call.status is ToolCallStatus.DENIED
    assert second.call.status is ToolCallStatus.DENIED
    # The automatic answer already exists, so neither denial is waiting for anyone.
    assert first.pending_approval is None
    assert second.pending_approval is None
    assert tool.seen_invocation == []


def test_a_human_rejection_is_final_for_the_call_and_asks_no_one_again(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=False)
    decisions_before = sum(
        1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION
    )

    second = manager.execute(context, "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.pending_approval is None
    assert second.result.error == "tool call rejected by a human"
    assert second.call.policy_decision_id == first.pending_approval.id
    assert (
        sum(1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION)
        == decisions_before
    )
    assert context.event_log.events()[-1].type is EventType.TOOL_DENIED
    assert tool.seen_invocation == []


def test_a_rejection_is_final_even_when_a_later_request_is_approved(tmp_path: Path) -> None:
    """A rejection wins wherever it sits among the answers, never only when it is the last.

    The model asked for the identical call twice before anyone answered, so one ticket has two
    pending decisions. A reviewer who refuses the first and approves the second has not undone
    the refusal: an answer that arrives later does not overturn a human who already said no.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    second = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    assert second.pending_approval is not None
    assert second.pending_approval.id != first.pending_approval.id
    _human_answer(context, first.pending_approval.id, approved=False)
    _human_answer(context, second.pending_approval.id, approved=True)
    decisions_before = sum(
        1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION
    )

    third = manager.execute(context, "echo", {"value": "x"})

    assert third.call.status is ToolCallStatus.DENIED
    assert third.pending_approval is None
    assert third.result.error == "tool call rejected by a human"
    assert third.call.policy_decision_id == first.pending_approval.id
    assert (
        sum(1 for e in context.event_log.events() if e.type is EventType.POLICY_DECISION)
        == decisions_before
    )
    assert tool.seen_invocation == []


@pytest.mark.parametrize(
    "forge_first", [False, True], ids=["after-the-answer", "before-the-answer"]
)
def test_a_second_approval_request_cannot_re_point_an_answer_at_another_call(
    tmp_path: Path, forge_first: bool
) -> None:
    """One decision binds the one call it was first asked about, whoever asks again.

    The Gate writes exactly one ticketed `human.approval_requested` per decision. Reading a second
    one as a rebinding let any writer point a pending answer at a different call: a human is asked
    about `echo`, and the "yes" buys a `run_python` that nobody was ever shown. The answer stays
    bound to the call it was given for, so the substituted call takes the ordinary Gate path.
    """
    echo_capability = ToolCapability(id="echo", external_effects=())
    echo = _FakeTool("echo", echo_capability, arguments_model=_EchoArguments)
    substitute = _FakeTool(
        "run_python",
        ToolCapability(id="run_python", external_effects=()),
        arguments_model=_EchoArguments,
    )
    context = _context(tmp_path, capability=echo_capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((echo, substitute)))
    reviewed = manager.execute(context, "echo", {"value": "x"})
    assert reviewed.pending_approval is not None
    forgery = {
        "decision_id": reviewed.pending_approval.id,
        "tool": "run_python",
        "tool_intent_sha256": tool_intent_sha256("run_python", {"value": "y", "description": ""}),
    }
    forge = lambda: context.event_log.append(  # noqa: E731  # two orders, one call site
        EventType.HUMAN_APPROVAL_REQUESTED,
        Actor(kind=ActorKind.AGENT, id="thy", authenticated=False),
        forgery,
        subject_id=new_id("tool"),
    )
    if forge_first:
        forge()
    _human_answer(context, reviewed.pending_approval.id, approved=True)
    if not forge_first:
        forge()

    substituted = manager.execute(context, "run_python", {"value": "y"})

    assert substituted.call.status is ToolCallStatus.DENIED
    assert substituted.pending_approval is not None
    assert substituted.pending_approval.id != reviewed.pending_approval.id
    assert substitute.seen_invocation == []
    # The answer the human really gave is untouched: it still buys its own call, exactly once.
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.COMPLETED
    assert manager.execute(context, "echo", {"value": "x"}).call.status is ToolCallStatus.DENIED
    assert len(echo.seen_invocation) == 1


def test_an_approval_recorded_by_a_system_actor_is_not_a_human_answer(tmp_path: Path) -> None:
    """The actor is the only tell: Contract 0.3's `Approval` payload carries no `automatic` key.

    `Gate.request_approval` appends a serialised `Approval` whose actor is `_AUTOMATION` when the
    approver is automatic, so a payload that merely omits `automatic` proves nothing. Only an
    answer a human actor recorded can become a ticket.
    """
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor.system(),
        Approval(
            id=new_id("approval"),
            run_id=context.run_id,
            policy_decision_id=first.pending_approval.id,
            authorization_context_sha256="a" * 64,
            approved=True,
            approved_by=Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        ).to_json_dict(),
        subject_id=first.pending_approval.id,
    )

    second = manager.execute(context, "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.pending_approval is not None
    assert second.call.policy_decision_id != first.pending_approval.id
    assert tool.seen_invocation == []


def test_an_automatic_human_approval_does_not_authorise_a_ticket(tmp_path: Path) -> None:
    """A human envelope with a malformed automatic marker remains unresolved to the manager."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    context.event_log.append(
        EventType.HUMAN_APPROVAL,
        Actor(kind=ActorKind.HUMAN, id="reviewer", authenticated=True),
        {"decision_id": first.pending_approval.id, "approved": True, "automatic": True},
        subject_id=first.pending_approval.id,
    )

    second = manager.execute(context, "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.pending_approval is not None
    assert tool.seen_invocation == []


def test_an_approval_under_another_policy_is_not_a_ticket(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    context = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)
    other_policy = load_policy_stack("credit_risk")
    other_gate = Gate(PolicyEngine(other_policy), context.event_log)

    second = manager.execute(replace(context, gate=other_gate), "echo", {"value": "x"})

    assert second.call.status is ToolCallStatus.DENIED
    assert second.call.policy_decision_id != first.pending_approval.id
    assert tool.seen_invocation == []


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ({"value": "x", "description": "a"}, {"value": "x", "description": "b"}, True),
        ({"value": "x"}, {"value": "y"}, False),
    ],
)
def test_the_intent_digest_ignores_the_description_and_nothing_else(
    left: dict[str, str], right: dict[str, str], same: bool
) -> None:
    assert (tool_intent_sha256("echo", left) == tool_intent_sha256("echo", right)) is same
    assert tool_intent_sha256("echo", left) != tool_intent_sha256("other", left)


def test_an_escalated_call_counts_twice_against_the_tool_call_limit(tmp_path: Path) -> None:
    """The denied attempt and the approved execution are both attempts (decision 6)."""
    capability = ToolCapability(id="echo", external_effects=())
    tool = _FakeTool("echo", capability, arguments_model=_EchoArguments)
    base = _context(tmp_path, capability=capability, approver=None, risk_confidence=0.1)
    constraints = ExecutionConstraints(max_tool_calls=2)
    decision = base.gate.authorize_execution(base.risk_profile, (capability,))
    context = replace(
        base,
        execution_constraints=constraints,
        execution_decision=decision.model_copy(update={"execution_constraints": constraints}),
    )
    manager = ToolManager(ToolRegistry((tool,)))
    first = manager.execute(context, "echo", {"value": "x"})
    assert first.pending_approval is not None
    _human_answer(context, first.pending_approval.id, approved=True)

    second = manager.execute(context, "echo", {"value": "x"})
    third = manager.execute(context, "echo", {"value": "y"})

    assert second.call.status is ToolCallStatus.COMPLETED
    assert third.call.status is ToolCallStatus.DENIED
    assert "maximum number" in (third.result.error or "")


def test_manager_records_expected_execution_failure(tmp_path: Path) -> None:
    capability = ToolCapability(id="fails", external_effects=())
    context = _context(tmp_path, capability=capability)
    manager = ToolManager(ToolRegistry((_FakeTool("fails", capability, raises=True),)))

    execution = manager.execute(context, "fails")

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.error == "expected failure"
    assert execution.result.success is False
    assert context.event_log.events()[-1].type is EventType.TOOL_COMPLETED


def test_the_manager_records_the_confinement_the_sandbox_reported(tmp_path: Path) -> None:
    """Enforcement is a reported fact: the manager copies it, it never infers or upgrades it."""
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool(
        "echo",
        capability,
        result=ToolResult(
            success=True,
            stdout="ok",
            sandbox_mode=SandboxMode.WORKSPACE_WRITE,
            sandbox_enforcement=SandboxEnforcement.PARTIAL,
        ),
    )
    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {})

    # An unconfined or weakly confined run must say so: claiming FULL would put a false statement
    # in the evidence chain and silence the MIRA control meant to catch it.
    assert execution.call.sandbox_mode is SandboxMode.WORKSPACE_WRITE
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    completed = next(e for e in context.event_log.events() if e.type is EventType.TOOL_COMPLETED)
    assert completed.payload["sandbox_enforcement"] == SandboxEnforcement.PARTIAL


def test_tool_completed_records_the_resolved_specification_and_cleanup_result(
    tmp_path: Path,
) -> None:
    """G.3: the resolved spec and cleanup result are canonical event keys, not in-memory state."""
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    spec = ResolvedExecutionSpec(
        backend="container",
        image="thymira:dev",
        workspace_mount="/host:/workspace:rw",
        network="none",
        memory="1g",
        cpus="1.0",
        pids_limit=128,
        environment_names=("SAFE_FLAG",),
        excluded_environment_names=("OPENAI_API_KEY",),
        unenforced=("rlimits", "workspace_quota"),
    )
    tool = _FakeTool(
        "echo",
        capability,
        result=ToolResult(
            success=True,
            stdout="ok",
            sandbox_mode=SandboxMode.WORKSPACE_WRITE,
            sandbox_enforcement=SandboxEnforcement.PARTIAL,
            sandbox_spec=spec,
            sandbox_cleanup_confirmed=False,
        ),
    )
    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo", {})

    assert execution.result.sandbox_spec == spec
    assert execution.result.sandbox_cleanup_confirmed is False
    completed = next(e for e in context.event_log.events() if e.type is EventType.TOOL_COMPLETED)
    assert completed.payload["sandbox_spec"] == spec.as_payload()
    assert completed.payload["sandbox_cleanup_confirmed"] is False
    assert "OPENAI_API_KEY" not in completed.payload["sandbox_spec"]["environment_names"]


def test_unrecordable_sandbox_spec_is_rejected_and_named(tmp_path: Path) -> None:
    """A malformed spec is rejected and named rather than crashing between started/completed."""
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool(
        "echo",
        capability,
        result=ToolResult(
            success=True,
            stdout="ok",
            sandbox_spec=cast("Any", "not-a-spec"),
            sandbox_cleanup_confirmed=cast("Any", "not-a-bool"),
        ),
    )
    ToolManager(ToolRegistry((tool,))).execute(context, "echo", {})

    completed = next(e for e in context.event_log.events() if e.type is EventType.TOOL_COMPLETED)
    assert completed.payload["sandbox_spec"] is None
    assert completed.payload["sandbox_cleanup_confirmed"] is None
    assert "sandbox_spec" in (completed.payload["error"] or "")
    assert "sandbox_cleanup_confirmed" in (completed.payload["error"] or "")


def test_a_tool_that_runs_no_code_reports_no_confinement(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    manager = ToolManager(ToolRegistry((_FakeTool("echo", capability),)))

    execution = manager.execute(context, "echo", {})

    assert execution.call.sandbox_mode is None
    assert execution.call.sandbox_enforcement is None


def test_a_tool_is_never_handed_the_event_log_or_the_gate(tmp_path: Path) -> None:
    """A tool holding a writable log could forge the approval it was denied.

    `event_log.append(HUMAN_APPROVAL, ...)` would chain, verify() would call it valid, and the
    tool would have manufactured the human approval a REQUIRE_HUMAN_REVIEW decision waits for.
    A tool reports; the manager authorises and records.
    """
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("echo", capability)
    ToolManager(ToolRegistry((tool,))).execute(context, "echo", {})

    invocation = tool.seen_invocation[0]
    assert not hasattr(invocation, "event_log")
    assert not hasattr(invocation, "gate")
    # What a tool legitimately needs is still there.
    assert invocation.workspace == context.workspace
    assert invocation.artifact_store is context.artifact_store
    assert invocation.run_id == context.run_id


def test_spill_preserves_full_output_in_a_log_artifact(tmp_path: Path) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)

    result = spill_result(
        ToolResult(success=True, stdout="0123456789" * 40),
        context,
        max_inline_bytes=128,
        produced_by=context.agent_id,
    )

    assert "[output spilled to artifact" in result.stdout
    assert len(result.stdout.encode()) < len("0123456789" * 40)
    assert result.artifact_ids
    artifact = context.artifact_store.get(f"tool-results/{context.agent_id}/stdout.log")
    assert artifact is not None
    assert artifact.kind is ArtifactKind.LOG
    assert context.artifact_store.load_text(artifact.name) == "0123456789" * 40
    assert result.result_sha256 is not None


def test_spill_failure_keeps_the_inline_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = ToolCapability(id="echo", external_effects=())
    context = _context(tmp_path, capability=capability)

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("artifact store unavailable")

    monkeypatch.setattr(context.artifact_store, "save_text", fail_save)
    result = spill_result(
        ToolResult(success=False, stdout="0123456789" * 40, error="failed"),
        context,
        max_inline_bytes=128,
        produced_by=context.agent_id,
    )

    assert result.stdout == "0123456789" * 40
    assert result.artifact_ids == ()
    assert result.result_sha256 is None


def test_local_sandbox_runs_argv_without_turning_success_into_failure(tmp_path: Path) -> None:
    sandbox = LocalSubprocessSandbox()

    result = sandbox.run(
        [sys.executable, "-c", "print('ok')"],
        workspace=tmp_path,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=3,
    )

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.enforcement is SandboxEnforcement.PARTIAL


def test_local_sandbox_reports_timeout(tmp_path: Path) -> None:
    sandbox = LocalSubprocessSandbox()

    result = sandbox.run(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        workspace=tmp_path,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=0.05,
    )

    assert result.exit_code != 0
    assert "sandbox timeout" in result.stderr


def test_dataset_registry_captures_schema_and_bounds_load(tmp_path: Path) -> None:
    source = tmp_path / "customers.csv"
    source.write_text("name,score\nAda,10\nGrace,20\n", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    artifact, schema = register_dataset(store, source, "customers", produced_by=new_id("tool"))

    assert isinstance(schema, DatasetSchema)
    assert artifact.kind is ArtifactKind.DATASET
    assert schema.columns == ("name", "score")
    assert schema.row_count == 2
    assert schema.byte_size == source.stat().st_size
    assert store.load_json("datasets/customers.schema.json")["row_count"] == 2
    loaded = load_dataset(store, "customers", max_rows=1)
    assert loaded.height == 1
    assert loaded.columns == ["name", "score"]


def test_run_python_tool_executes_and_records_partial_confinement(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; Path('created.txt').write_text('ok'); print('hello')"
            ),
            "description": "Write a file and print a greeting",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.stdout == "hello\n"
    assert execution.result.exit_code == 0
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert (context.workspace / "created.txt").read_text(encoding="utf-8") == "ok"
    assert context.event_log.verify().valid


def test_run_python_publishes_declared_binary_outputs(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; "
                "Path('models').mkdir(); "
                "Path('models/model.joblib').write_bytes(b'\\x00\\x01binary')"
            ),
            "description": "Publish a binary model output",
            "output_artifacts": [
                {
                    "path": "models/model.joblib",
                    "kind": "model",
                }
            ],
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.artifact_ids
    artifact = context.artifact_store.get("models/model.joblib")
    assert artifact is not None
    assert artifact.kind is ArtifactKind.MODEL
    assert context.artifact_store.load_bytes("models/model.joblib") == b"\x00\x01binary"


def test_run_python_refuses_to_publish_plain_text_as_a_pdf(tmp_path: Path) -> None:
    """A filename and model-supplied MIME cannot turn arbitrary bytes into a PDF Artifact."""
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": "from pathlib import Path; Path('fake.pdf').write_text('not a PDF')",
            "description": "Attempt to publish a fake PDF",
            "output_artifacts": [
                {
                    "path": "fake.pdf",
                    "kind": "report",
                    "media_type": "application/pdf",
                }
            ],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "valid PDF" in (execution.result.error or "")
    assert context.artifact_store.get("fake.pdf") is None


def test_run_python_refuses_to_publish_invalid_png_bytes(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": "from pathlib import Path; Path('fake.png').write_bytes(b'not an image')",
            "description": "Attempt to publish a fake PNG",
            "output_artifacts": [{"path": "fake.png", "kind": "other"}],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "valid PNG" in (execution.result.error or "")
    assert context.artifact_store.get("fake.png") is None


def test_run_python_refuses_an_oversized_pdf_before_publishing(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": "from pathlib import Path; Path('huge.pdf').write_bytes(b'0' * 15728641)",
            "description": "Attempt to publish an oversized PDF",
            "output_artifacts": [{"path": "huge.pdf", "kind": "report"}],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "exceeds" in (execution.result.error or "")
    assert context.artifact_store.get("huge.pdf") is None


def test_run_python_prevalidates_every_output_name_before_publishing(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; from PIL import Image; "
                "Path('.history').mkdir(); "
                "Image.new('RGB', (1, 1)).save('good.png'); "
                "Image.new('RGB', (1, 1)).save('.history/bad.png')"
            ),
            "description": "Attempt a mixed valid and reserved output batch",
            "output_artifacts": [
                {"path": "good.png", "kind": "other"},
                {"path": ".history/bad.png", "kind": "other"},
            ],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "history" in (execution.result.error or "")
    assert context.artifact_store.list_active() == []


def test_run_python_publish_failure_leaves_no_partial_artifacts_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed heterogeneous output batch never publishes its successful prefix."""
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    store = context.artifact_store
    assert isinstance(store, LocalArtifactStore)
    original = store._write_staged_bytes
    writes = 0

    def fail_on_second(target: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected second-artifact publication failure")
        original(target, data)

    monkeypatch.setattr(store, "_write_staged_bytes", fail_on_second)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; "
                "Path('report.md').write_text('# Report'); "
                "Path('metrics.json').write_text('{\"accuracy\": 1.0}')"
            ),
            "description": "Publish one report and one metrics artifact",
            "output_artifacts": [
                {"path": "report.md", "kind": "report"},
                {"path": "metrics.json", "kind": "metrics"},
            ],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "injected second-artifact" in (execution.result.error or "")
    assert store.list_active() == []
    assert not any(event.type is EventType.ARTIFACT_CREATED for event in context.event_log.events())
    completed = context.event_log.events()[-1]
    assert completed.type is EventType.TOOL_COMPLETED
    assert completed.payload["artifact_ids"] == []


def test_run_python_post_replace_error_keeps_committed_publication_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable manifest match cannot be reported as a failed tool call."""
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    store = context.artifact_store
    assert isinstance(store, LocalArtifactStore)
    original = store._write_manifest

    def publish_then_fail(manifest: Mapping[str, Artifact] | None = None) -> None:
        original(manifest)
        raise OSError("injected post-replace durability signal")

    monkeypatch.setattr(store, "_write_manifest", publish_then_fail)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path; "
                "Path('report.md').write_text('# Report'); "
                "Path('metrics.json').write_text('{\"accuracy\": 1.0}')"
            ),
            "description": "Publish one report and one metrics artifact",
            "output_artifacts": [
                {"path": "report.md", "kind": "report"},
                {"path": "metrics.json", "kind": "metrics"},
            ],
        },
    )

    active = store.list_active()
    reopened = LocalArtifactStore(tmp_path / "artifacts", context.run_id)
    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.success
    assert len(active) == 2
    assert {artifact.id for artifact in reopened.list_active()} == {
        artifact.id for artifact in active
    }
    assert reopened.verify() == []
    created = [
        event for event in context.event_log.events() if event.type is EventType.ARTIFACT_CREATED
    ]
    assert {event.subject_id for event in created} == {artifact.id for artifact in active}
    completed = context.event_log.events()[-1]
    assert completed.type is EventType.TOOL_COMPLETED
    assert set(completed.payload["artifact_ids"]) == {artifact.id for artifact in active}


def test_run_python_fails_when_declared_output_is_missing(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {
            "code": "print('finished')",
            "description": "Declare a missing output",
            "output_artifacts": [{"path": "reports/missing.md", "kind": "report"}],
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.error == "declared output artifact does not exist: reports/missing.md"
    assert not context.artifact_store.exists("reports/missing.md")


def test_run_python_passes_a_workspace_relative_script_to_the_sandbox(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability)
    sandbox = _CapturingSandbox(
        SandboxRun(
            stdout="",
            stderr="",
            exit_code=0,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )
    )

    result = RunPython(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        context.for_tool(),
        {"code": "print('portable')", "description": "Exercise portable command paths."},
    )

    assert result.success
    assert sandbox.argv[0][:4] == ["python", "-I", "-u", "-B"]
    assert sandbox.argv[0][4].startswith(".thymira/")
    assert not Path(sandbox.argv[0][4]).is_absolute()


def test_run_python_refuses_read_only_before_staging_the_script(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability)

    result = RunPython(mode=SandboxMode.READ_ONLY).execute(
        context.for_tool(),
        {"code": "print('unreachable')", "description": "Refuse read-only staging."},
    )

    assert not result.success
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not context.workspace.exists()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            RunExperiment(mode=SandboxMode.READ_ONLY),
            {"dataset": "unreachable", "target_column": "target"},
        ),
        (InspectModel(mode=SandboxMode.READ_ONLY), {"model_artifact": "unreachable"}),
    ],
)
def test_staged_subprocess_tools_refuse_read_only_before_creating_workspace(
    tmp_path: Path, tool: Tool, arguments: dict[str, str]
) -> None:
    context = _context(tmp_path, capability=ToolCapability(id=tool.name, external_effects=()))

    result = tool.execute(context.for_tool(), arguments)

    assert not result.success
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert result.sandbox_mode is SandboxMode.READ_ONLY
    assert not context.workspace.exists()


def test_run_experiment_stages_only_workspace_relative_paths_for_the_sandbox(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path, capability=ToolCapability(id="run_experiment", external_effects=())
    )
    context.workspace.mkdir(parents=True)
    source = context.workspace / "training.csv"
    source.write_text("value,target\n0,no\n1,yes\n2,yes\n3,no\n", encoding="utf-8")
    register_dataset(context.artifact_store, source, "training", produced_by=context.agent_id)
    sandbox = _CapturingSandbox(
        SandboxRun(
            stdout="",
            stderr="stopped after staging",
            exit_code=1,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )
    )

    result = RunExperiment(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        context.for_tool(),
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "portable",
            "seed": 7,
            "timeout_s": 30.0,
        },
    )

    assert not result.success
    assert sandbox.argv[0][:4] == ["python", "-I", "-u", "-B"]
    assert sandbox.argv[0][4].startswith(".thymira/")
    script = next((context.workspace / ".thymira").glob("*.py")).read_text(encoding="utf-8")
    assert str(context.workspace) not in script
    assert ".thymira/training.csv" in script
    assert ".thymira/model.joblib" in script
    assert ".thymira/metrics.json" in script


def test_inspect_model_stages_only_workspace_relative_paths_for_the_sandbox(tmp_path: Path) -> None:
    context = _context(tmp_path, capability=ToolCapability(id="inspect_model", external_effects=()))
    sandbox = _CapturingSandbox(
        SandboxRun(
            stdout="",
            stderr="stopped after staging",
            exit_code=1,
            mode=SandboxMode.DANGER_FULL_ACCESS,
            enforcement=SandboxEnforcement.PARTIAL,
        )
    )
    context.artifact_store.save_bytes(
        "models/untrusted.joblib",
        b"not loaded because the capturing sandbox exits first",
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )

    result = InspectModel(sandbox=sandbox, mode=SandboxMode.DANGER_FULL_ACCESS).execute(
        context.for_tool(),
        {"model_artifact": "models/untrusted.joblib"},
    )

    assert not result.success
    assert sandbox.argv[0][:4] == ["python", "-I", "-u", "-B"]
    assert sandbox.argv[0][4].startswith(".thymira/")
    script = next((context.workspace / ".thymira").glob("*.py")).read_text(encoding="utf-8")
    assert str(context.workspace) not in script
    assert ".thymira/inspect_model.joblib" in script
    assert ".thymira/inspection.json" in script


def test_file_tools_round_trip_and_reject_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = ToolCapability(id="write_file", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry(
            (cast("Tool", WriteFile()), cast("Tool", ReadFile()), cast("Tool", ListFiles()))
        )
    )

    written = manager.execute(
        context,
        "write_file",
        {
            "path": "data/value.txt",
            "content": "hello",
            "description": "Write the greeting value file",
        },
    )
    read = manager.execute(context, "read_file", {"path": "data/value.txt"})
    listed = manager.execute(context, "list_files", {"path": "."})
    rejected = manager.execute(context, "read_file", {"path": "../outside.txt"})

    assert written.call.status is ToolCallStatus.COMPLETED
    assert read.result.stdout == "hello"
    assert "data/value.txt" in listed.result.stdout
    assert rejected.call.status is ToolCallStatus.FAILED
    assert "traversal" in (rejected.result.error or "")


def test_write_file_infers_media_type_for_a_registered_markdown_report(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="write_file", external_effects=()),
        development=True,
    )
    execution = ToolManager(ToolRegistry((cast("Tool", WriteFile()),))).execute(
        context,
        "write_file",
        {
            "path": "reports/final.md",
            "content": "# Final report\n",
            "description": "Write the final Markdown report",
            "kind": "report",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    artifact = context.artifact_store.get("reports/final.md")
    assert artifact is not None
    assert artifact.media_type == "text/markdown"


def test_write_file_refuses_to_register_text_as_a_binary_pdf_artifact(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="write_file", external_effects=()),
        development=True,
    )
    execution = ToolManager(ToolRegistry((cast("Tool", WriteFile()),))).execute(
        context,
        "write_file",
        {
            "path": "german_credit_eda_report.pdf",
            "content": "This is plain text, not a PDF.",
            "description": "Attempt to register a fake PDF",
            "kind": "report",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "cannot register a binary artifact" in (execution.call.error or "")
    assert not (context.workspace / "german_credit_eda_report.pdf").exists()
    assert context.artifact_store.get("german_credit_eda_report.pdf") is None


def test_write_file_refuses_invalid_json_before_writing_or_registering(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="write_file", external_effects=()),
        development=True,
    )
    execution = ToolManager(ToolRegistry((cast("Tool", WriteFile()),))).execute(
        context,
        "write_file",
        {
            "path": "reports/metrics.json",
            "content": '{"accuracy": NaN}',
            "description": "Attempt to register invalid JSON metrics",
            "kind": "metrics",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert "cannot register invalid JSON" in (execution.call.error or "")
    assert not (context.workspace / "reports" / "metrics.json").exists()
    assert context.artifact_store.get("reports/metrics.json") is None


def test_read_file_returns_a_failed_result_for_a_missing_file(tmp_path: Path) -> None:
    """A missing file is a tool failure (``ToolExecutionError``), not an uncaught ``OSError``.

    ``ToolManager.execute`` lets an unexpected programming error propagate and only turns a
    ``ToolExecutionError`` into a failed call -- before this, ``ReadFile.execute`` called
    ``Path.read_text`` uncaught, so a missing file crashed the whole Run rather than reporting a
    result the delegated agent could act on.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = ToolCapability(id="read_file", external_effects=())
    context = _context(tmp_path, capability=capability)
    manager = ToolManager(ToolRegistry((cast("Tool", ReadFile()),)))

    execution = manager.execute(context, "read_file", {"path": "missing.txt"})

    assert execution.call.status is ToolCallStatus.FAILED
    assert "missing.txt" in (execution.result.error or "")


def test_git_tools_fail_as_results_outside_a_repository(tmp_path: Path) -> None:
    capability = ToolCapability(id="git_status", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", GitStatus(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(context, "git_status")

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.error


def test_manager_emits_artifact_created_between_tool_events(tmp_path: Path) -> None:
    capability = ToolCapability(id="writer", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("writer", capability, writes_artifact=True)
    manager = ToolManager(ToolRegistry((tool,)))

    execution = manager.execute(context, "writer")

    assert execution.call.status is ToolCallStatus.COMPLETED
    events = context.event_log.events()
    types = [event.type for event in events]
    assert types.index(EventType.ARTIFACT_CREATED) < types.index(EventType.TOOL_COMPLETED)
    created = next(event for event in events if event.type is EventType.ARTIFACT_CREATED)
    artifact = context.artifact_store.get("metrics.json")
    assert artifact is not None
    assert created.payload["name"] == artifact.name
    assert created.payload["sha256"] == artifact.sha256
    assert created.payload["artifact_id"] == artifact.id
    assert artifact.id in execution.call.artifact_ids


def test_mlflow_tools_persist_params_metrics_and_artifacts(tmp_path: Path) -> None:
    capability = ToolCapability(id="mlflow_start_run", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "metrics.json").write_text("{}", encoding="utf-8")
    manager = ToolManager(ToolRegistry(mlflow_tools()))

    started = manager.execute(
        context,
        "mlflow_start_run",
        {"experiment_name": "smoke", "description": "Start the smoke tracker run"},
    )
    run_id = json.loads(started.result.stdout)["tracker_run_id"]
    manager.execute(
        context,
        "mlflow_log_param",
        {
            "run_id": run_id,
            "key": "seed",
            "value": "7",
            "description": "Log the training seed parameter",
        },
    )
    manager.execute(
        context,
        "mlflow_log_metric",
        {
            "run_id": run_id,
            "key": "accuracy",
            "value": 0.9,
            "description": "Log the validation accuracy metric",
        },
    )
    manager.execute(
        context,
        "mlflow_log_artifact",
        {
            "run_id": run_id,
            "path": "metrics.json",
            "description": "Log the metrics artifact",
        },
    )
    rejected = manager.execute(
        context,
        "mlflow_log_artifact",
        {
            "run_id": run_id,
            "path": "../outside.json",
            "description": "Attempt to log an outside artifact",
        },
    )
    ended = manager.execute(
        context, "mlflow_end_run", {"run_id": run_id, "description": "End the smoke tracker run"}
    )

    run = MlflowTracker(context.workspace).query_runs(run_id=run_id)[0]
    assert ended.call.status is ToolCallStatus.COMPLETED
    assert rejected.call.status is ToolCallStatus.FAILED
    assert run.params == {"seed": "7"}
    assert run.metrics == {"accuracy": 0.9}
    assert run.artifacts == (f"artifacts/{run_id}/metrics.json",)


def test_dataset_analysis_sql_and_statistics_tools(tmp_path: Path) -> None:
    capability = ToolCapability(id="analyze_dataset", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "scores.csv"
    source.write_text(
        "value,grp\n1,A\n2,A\n10,B\n12,B\n",
        encoding="utf-8",
    )
    register_dataset(
        context.artifact_store,
        source,
        "scores",
        produced_by=context.agent_id,
    )
    large_source = context.workspace / "large.csv"
    large_source.write_text(
        "value,grp\n" + "\n".join(f"{index},A" for index in range(8_000)) + "\n",
        encoding="utf-8",
    )
    register_dataset(
        context.artifact_store,
        large_source,
        "large",
        produced_by=context.agent_id,
    )
    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", AnalyzeDataset()),
                cast("Tool", ProfileDataset()),
                cast("Tool", QuerySql()),
                cast("Tool", RunStatistics()),
            )
        )
    )

    analysis = manager.execute(
        context,
        "analyze_dataset",
        {"dataset": "scores", "description": "Analyze the scores dataset"},
    )
    profile = manager.execute(
        context,
        "profile_dataset",
        {"dataset": "scores", "description": "Profile the scores dataset"},
    )
    queried = manager.execute(
        context,
        "query_sql",
        {
            "dataset": "scores",
            "query": "SELECT grp, AVG(value) AS mean FROM dataset GROUP BY grp ORDER BY grp",
            "description": "Summarize scores by group",
        },
    )
    statistics = manager.execute(
        context,
        "run_statistics",
        {
            "dataset": "scores",
            "value_column": "value",
            "group_column": "grp",
            "description": "Run grouped score statistics",
        },
    )
    anova = manager.execute(
        context,
        "run_statistics",
        {
            "dataset": "scores",
            "value_column": "value",
            "group_column": "grp",
            "test": "anova",
            "description": "Compare grouped score means",
        },
    )
    correlation = manager.execute(
        context,
        "run_statistics",
        {
            "dataset": "scores",
            "value_column": "value",
            "second_value_column": "value",
            "test": "correlation",
            "description": "Measure paired score correlation",
        },
    )
    normality = manager.execute(
        context,
        "run_statistics",
        {
            "dataset": "scores",
            "value_column": "value",
            "test": "normality",
            "description": "Test score normality",
        },
    )
    rejected_query = manager.execute(
        context,
        "query_sql",
        {
            "dataset": "scores",
            "query": "WITH changed AS (DELETE FROM dataset) SELECT * FROM changed",
            "description": "Attempt a mutating SQL query",
        },
    )
    large_query = manager.execute(
        context,
        "query_sql",
        {
            "dataset": "large",
            "query": "SELECT * FROM dataset",
            "description": "Read the large dataset rows",
        },
    )

    assert analysis.call.status is ToolCallStatus.COMPLETED
    assert json.loads(analysis.result.stdout)["shape"]["rows"] == 4
    assert profile.call.status is ToolCallStatus.COMPLETED
    profile_payload = json.loads(profile.result.stdout)
    assert "correlation" in profile_payload
    assert profile_payload["columns"]["value"]["histogram"]["counts"]
    assert queried.call.status is ToolCallStatus.COMPLETED
    assert len(json.loads(queried.result.stdout)["rows"]) == 2
    assert statistics.call.status is ToolCallStatus.COMPLETED
    assert "p_value" in json.loads(statistics.result.stdout)
    assert anova.call.status is ToolCallStatus.COMPLETED
    assert json.loads(anova.result.stdout)["test"] == "anova"
    assert correlation.call.status is ToolCallStatus.COMPLETED
    assert normality.call.status is ToolCallStatus.COMPLETED
    assert rejected_query.call.status is ToolCallStatus.FAILED
    assert large_query.call.status is ToolCallStatus.COMPLETED
    large_artifacts = [
        artifact
        for artifact in context.artifact_store.list_active()
        if artifact.name == "query/large.csv"
    ]
    assert len(large_artifacts) == 1


def test_analysis_histogram_handles_empty_and_constant_numeric_series() -> None:
    empty = _histogram(pl.Series("value", [None], dtype=pl.Float64))
    constant = _histogram(pl.Series("value", [3.0, 3.0]))

    assert empty == {"edges": [], "counts": []}
    assert constant == {"edges": [3.0, 3.0], "counts": [2]}


def test_analysis_finite_converts_non_finite_values_to_null() -> None:
    assert analysis_finite(float("inf")) is None
    assert analysis_finite(None) is None


def test_profile_dataset_reports_correlation_for_two_numeric_columns(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="profile_dataset", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "paired.csv"
    source.write_text("left,right,label\n1,2,A\n2,4,B\n3,6,A\n", encoding="utf-8")
    register_dataset(context.artifact_store, source, "paired", produced_by=context.agent_id)

    execution = ToolManager(ToolRegistry((cast("Tool", ProfileDataset()),))).execute(
        context,
        "profile_dataset",
        {"dataset": "paired", "description": "Profile the paired dataset"},
    )
    payload = json.loads(execution.result.stdout)

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert payload["correlation"]["left"][1] == pytest.approx(1.0)


def test_statistics_supports_chi_square_for_categorical_columns() -> None:
    frame = pl.DataFrame(
        {
            "value": ["yes", "yes", "no", "no"],
            "group": ["A", "B", "A", "B"],
        }
    )

    result = _run_test(
        frame,
        {"value_column": "value", "group_column": "group"},
        "chi_square",
    )

    assert result["row_levels"] == ["A", "B"]
    assert result["column_levels"] == ["no", "yes"]


def test_statistics_correlation_ignores_rows_with_null_values() -> None:
    frame = pl.DataFrame(
        {
            "value": [1.0, None, 3.0, 4.0],
            "second": [2.0, 4.0, 6.0, 8.0],
        }
    )

    result = _run_test(
        frame,
        {"value_column": "value", "second_value_column": "second"},
        "correlation",
    )

    assert result["statistic"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("test_name", "arguments", "message"),
    [
        ("welch_t", {"value_column": "value"}, "welch_t requires group_column"),
        (
            "welch_t",
            {"value_column": "value", "group_column": "group"},
            "welch_t requires exactly two groups",
        ),
        (
            "welch_t",
            {"value_column": "value", "group_column": "group"},
            "welch_t requires at least two values per group",
        ),
        ("chi_square", {"value_column": "value"}, "chi_square requires group_column"),
        (
            "correlation",
            {"value_column": "value"},
            "correlation requires second_value_column",
        ),
        (
            "normality",
            {"value_column": "value"},
            "normality requires at least three values",
        ),
        ("unsupported", {"value_column": "value"}, "unsupported statistical test"),
    ],
)
def test_statistics_rejects_invalid_test_requirements(
    test_name: str,
    arguments: dict[str, Any],
    message: str,
) -> None:
    if test_name == "welch_t" and message.endswith("two groups"):
        frame = pl.DataFrame({"value": [1.0, 2.0, 3.0], "group": ["A", "A", "A"]})
    elif test_name == "welch_t" and message.endswith("per group"):
        frame = pl.DataFrame({"value": [1.0, 2.0, 3.0], "group": ["A", "B", "B"]})
    elif test_name == "normality":
        frame = pl.DataFrame({"value": [1.0, 2.0]})
    else:
        frame = pl.DataFrame({"value": [1.0, 2.0, 3.0], "group": ["A", "B", "A"]})

    with pytest.raises(ValueError, match=message):
        _run_test(frame, arguments, test_name)


def test_statistics_rejects_missing_requested_columns() -> None:
    frame = pl.DataFrame({"value": [1.0]})

    with pytest.raises(ValueError, match="unknown dataset columns: missing"):
        _require_columns(frame, {"value_column": "missing"})


def test_query_sql_accepts_one_trailing_semicolon() -> None:
    assert _read_only_query(" SELECT * FROM dataset; ") == "SELECT * FROM dataset"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "UPDATE dataset SET value = 1",
        "SELECT * FROM dataset; SELECT * FROM dataset",
        "WITH changed AS (DELETE FROM dataset) SELECT * FROM changed",
    ],
)
def test_query_sql_rejects_non_read_only_statements(query: str) -> None:
    with pytest.raises(ToolExecutionError, match="query_sql"):
        _read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM read_csv('outside.csv')",
        "SELECT * FROM 'outside.csv'",
        "SELECT * FROM other_table",
        "SELECT * FROM main.dataset",
        "SELECT * FROM memory.main.dataset",
        "SELECT * FROM dataset, other_table",
        "SELECT * FROM dataset JOIN other_table ON true",
        "SELECT * FROM (SELECT * FROM other_table) AS nested",
        "SELECT EXISTS(FROM read_csv('outside.csv'))",
        "WITH rows AS (SELECT * FROM read_parquet('outside.parquet')) SELECT * FROM rows",
        "SELECT * FROM /* source hidden by a comment */ read_json('outside.json')",
    ],
)
def test_query_sql_rejects_relations_outside_the_registered_dataset(query: str) -> None:
    with pytest.raises(ToolExecutionError, match="registered dataset"):
        _read_only_query(query)


def test_query_sql_accepts_ctes_joins_and_subqueries_derived_from_dataset() -> None:
    query = """
        WITH filtered AS (
            SELECT grp, value
            FROM dataset
            WHERE value > 0
        ), totals AS (
            SELECT grp, sum(value) AS total
            FROM filtered
            GROUP BY grp
        )
        SELECT source.grp, source.value, totals.total
        FROM (SELECT grp, value FROM filtered) AS source
        JOIN totals ON totals.grp = source.grp
    """

    assert _read_only_query(query) == query.strip()


def test_query_sql_does_not_treat_scalar_function_from_as_a_relation() -> None:
    query = "SELECT extract(year FROM current_date) AS year FROM dataset"

    assert _read_only_query(query) == query


def test_query_sql_accepts_quoted_dataset_and_cte_names() -> None:
    query = 'WITH "Filtered Rows" AS (SELECT * FROM "dataset") SELECT * FROM "Filtered Rows"'

    assert _read_only_query(query) == query


_QUERY_SQL_CANARY = "SUPERSECRET-abc123"


def _query_sql_context(tmp_path: Path) -> ToolContext:
    """Build a context with one registered dataset and a secret file outside the workspace."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="query_sql", data_access=("dataset",), external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "scores.csv"
    source.write_text("value,grp\n1,A\n2,B\n", encoding="utf-8")
    register_dataset(context.artifact_store, source, "scores", produced_by=context.agent_id)
    outside = tmp_path / "vault"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "secret.csv").write_text(
        f"name,secret\nadmin,{_QUERY_SQL_CANARY}\n", encoding="utf-8"
    )
    (outside / "secret.txt").write_text(_QUERY_SQL_CANARY, encoding="utf-8")
    return context


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_csv('{csv}')",
        "SELECT * FROM read_csv_auto('{csv}')",
        "SELECT * FROM '{csv}'",
        "SELECT content FROM read_text('{txt}')",
        "SELECT * FROM glob('{directory}/*')",
    ],
)
def test_query_sql_cannot_reach_files_outside_the_registered_dataset(
    tmp_path: Path,
    template: str,
) -> None:
    context = _query_sql_context(tmp_path)
    outside = tmp_path / "vault"
    query = template.format(
        csv=(outside / "secret.csv").as_posix(),
        txt=(outside / "secret.txt").as_posix(),
        directory=outside.as_posix(),
    )
    manager = ToolManager(ToolRegistry((cast("Tool", QuerySql()),)))

    execution = manager.execute(
        context,
        "query_sql",
        {"dataset": "scores", "query": query, "description": "Probe SQL path restrictions"},
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert _QUERY_SQL_CANARY not in (execution.result.stdout or "")
    assert _QUERY_SQL_CANARY not in (execution.result.error or "")
    assert _QUERY_SQL_CANARY not in (execution.call.error or "")


def test_query_sql_still_answers_an_ordinary_query_over_the_registered_dataset(
    tmp_path: Path,
) -> None:
    context = _query_sql_context(tmp_path)
    manager = ToolManager(ToolRegistry((cast("Tool", QuerySql()),)))

    execution = manager.execute(
        context,
        "query_sql",
        {
            "dataset": "scores",
            "query": "SELECT grp, value FROM dataset WHERE value > 1 ORDER BY grp",
            "description": "Read filtered scores from SQL",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert json.loads(execution.result.stdout) == {
        "columns": ["grp", "value"],
        "rows": [{"grp": "B", "value": 2.0}],
        "row_count": 1,
    }


def test_query_sql_reports_the_engine_diagnostic_when_the_model_query_fails(
    tmp_path: Path,
) -> None:
    """DuckDB's own text is what lets an agent repair its query, so it reaches the caller."""
    context = _query_sql_context(tmp_path)
    manager = ToolManager(ToolRegistry((cast("Tool", QuerySql()),)))

    execution = manager.execute(
        context,
        "query_sql",
        {
            "dataset": "scores",
            "query": "SELECT vlaue FROM dataset",
            "description": "Exercise an invalid SQL query",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    # The failure is a recorded call, not an escaping error: `tool.started` keeps its pair.
    assert [
        event.type
        for event in context.event_log.events()
        if event.payload.get("tool_call_id") == execution.call.id
    ] == [EventType.TOOL_STARTED, EventType.TOOL_COMPLETED]
    # `build_agent_tools` returns `result.error` to the model verbatim, and the candidate binding
    # is the actionable half: it names the column that was meant, so the typo is fixable in place.
    reported = execution.result.error or ""
    assert reported.startswith("query_sql could not run the query: ")
    assert 'Referenced column "vlaue" not found' in reported
    assert 'Candidate bindings: "value"' in reported
    assert execution.call.error == reported


def test_audit_helper_decodes_predictions_by_value_never_by_position() -> None:
    """A prediction is matched against the class space, not used as an index into it.

    This test used to assert ``_decode_predictions(["yes", 1], ["no", "yes"]) == ["yes", "yes"]``
    -- that is, it pinned the positional guess that was the mechanism of the mis-labelling defect:
    the prediction ``1`` was read as *index 1* and relabelled ``"yes"``. A prediction is always one
    of the estimator's own classes, so a value outside them means the two lists are not describing
    the same model, and that is now an error rather than a silent relabelling.
    """
    # A label the dataset and the model spell differently is still one label.
    assert _decode_predictions(["yes", "1.0"], ["yes", "1"]) == ["yes", "1"]
    with pytest.raises(ValueError, match="not one of the classes it declares"):
        _decode_predictions(["yes", 1], ["no", "yes"])


def test_audit_helper_omits_non_numeric_drift_and_serializes_null_means() -> None:
    result = _drift(
        pl.DataFrame({"numeric": [float("nan")], "label": ["A"]}),
        pl.DataFrame({"numeric": [float("nan")], "label": ["B"]}),
        ["numeric", "label"],
        "reference",
    )

    assert result["numeric_features"]["numeric"] == {
        "current_mean": None,
        "reference_mean": None,
        "mean_delta": None,
    }
    assert "label" not in result["numeric_features"]
    assert audit_finite(None) is None


def test_audit_helper_rejects_drift_reference_missing_features() -> None:
    with pytest.raises(ValueError, match="reference dataset is missing columns: x2"):
        _drift(
            pl.DataFrame({"x1": [1.0], "x2": [2.0]}),
            pl.DataFrame({"x1": [1.0]}),
            ["x1", "x2"],
            "reference",
        )


def _assert_experiment_output_matches_record(stdout: str, experiment: Experiment) -> None:
    """The model-visible result identifies the same experiment, model, and tracker as the log."""
    assert experiment.model_artifact_id is not None
    assert experiment.tracker_run_id
    assert json.loads(stdout) == {
        "experiment_id": experiment.id,
        "model_artifact_id": experiment.model_artifact_id,
        "tracker_run_id": experiment.tracker_run_id,
        "metrics": experiment.metrics,
    }


def test_run_experiment_publish_failure_leaves_no_partial_artifacts_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed model-and-metrics publication never exposes its successful prefix."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "training.csv"
    source.write_text("feature,target\n0,no\n1,yes\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "training", produced_by=context.agent_id)
    store = context.artifact_store
    assert isinstance(store, LocalArtifactStore)
    active_before = {artifact.id for artifact in store.list_active()}
    original = store._write_staged_bytes
    writes = 0

    def fail_on_second(target: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected second-experiment-artifact failure")
        original(target, data)

    monkeypatch.setattr(store, "_write_staged_bytes", fail_on_second)
    manager = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(
                        sandbox=_SuccessfulTrainingSandbox(),
                        mode=SandboxMode.DANGER_FULL_ACCESS,
                    ),
                ),
            )
        )
    )

    execution = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "atomic",
            "model_artifact_name": "models/atomic.joblib",
            "metrics_artifact_name": "metrics/atomic.json",
            "description": "Publish an atomic model and metrics pair",
            "code": "# Outputs are supplied by the injected sandbox.",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "injected second-experiment-artifact" in (execution.result.error or "")
    assert {artifact.id for artifact in store.list_active()} == active_before
    assert store.get("models/atomic.joblib") is None
    assert store.get("metrics/atomic.json") is None
    failed_events = context.event_log.events()
    assert not any(event.type is EventType.ARTIFACT_CREATED for event in failed_events)
    assert {
        EventType.EXPERIMENT_STARTED,
        EventType.MODEL_TRAINED,
        EventType.EXPERIMENT_COMPLETED,
    }.isdisjoint(event.type for event in failed_events)
    completed = failed_events[-1]
    assert completed.type is EventType.TOOL_COMPLETED
    assert completed.payload["artifact_ids"] == []


def test_run_experiment_tracker_failure_precedes_artifact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracker failure cannot leave model or metrics artifacts visible to the Run."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "training.csv"
    source.write_text("feature,target\n0,no\n1,yes\n", encoding="utf-8", newline="\n")
    register_dataset(context.artifact_store, source, "training", produced_by=context.agent_id)
    store = context.artifact_store
    active_before = {artifact.id for artifact in store.list_active()}
    monkeypatch.setattr(run_experiment_module, "MlflowTracker", _FailingEndTracker)
    manager = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(
                        sandbox=_SuccessfulTrainingSandbox(),
                        mode=SandboxMode.DANGER_FULL_ACCESS,
                    ),
                ),
            )
        )
    )

    execution = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "tracking-failure",
            "model_artifact_name": "models/tracking-failure.joblib",
            "metrics_artifact_name": "metrics/tracking-failure.json",
            "description": "Fail while finalizing experiment tracking",
            "code": "# Outputs are supplied by the injected sandbox.",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert "injected tracker finalization failure" in (execution.result.error or "")
    assert {artifact.id for artifact in store.list_active()} == active_before
    assert store.get("models/tracking-failure.joblib") is None
    assert store.get("metrics/tracking-failure.json") is None
    failed_events = context.event_log.events()
    assert not any(event.type is EventType.ARTIFACT_CREATED for event in failed_events)
    assert {
        EventType.EXPERIMENT_STARTED,
        EventType.MODEL_TRAINED,
        EventType.EXPERIMENT_COMPLETED,
    }.isdisjoint(event.type for event in failed_events)
    completed = failed_events[-1]
    assert completed.type is EventType.TOOL_COMPLETED
    assert completed.payload["artifact_ids"] == []


def test_run_experiment_persists_model_record_and_events(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "training.csv"
    source.write_text(
        "x1,x2,target\n0,0,no\n0,1,no\n1,0,yes\n1,1,yes\n2,0,yes\n2,1,yes\n3,0,yes\n3,1,yes\n",
        encoding="utf-8",
    )
    register_dataset(
        context.artifact_store,
        source,
        "training",
        produced_by=context.agent_id,
    )
    reference = context.workspace / "reference.csv"
    reference.write_text(
        "x1,x2,target\n0,0,no\n1,1,no\n2,0,yes\n3,1,yes\n",
        encoding="utf-8",
    )
    register_dataset(
        context.artifact_store,
        reference,
        "reference",
        produced_by=context.agent_id,
    )
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "seed": 7,
            "description": "Train the logistic-regression baseline",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    events = [event.type for event in context.event_log.events()]
    assert events.index(EventType.EXPERIMENT_STARTED) < events.index(EventType.MODEL_TRAINED)
    assert events.index(EventType.MODEL_TRAINED) < events.index(EventType.EXPERIMENT_COMPLETED)
    completed = next(
        event
        for event in context.event_log.events()
        if event.type is EventType.EXPERIMENT_COMPLETED
    )
    experiment = Experiment.model_validate(completed.payload["experiment"])
    _assert_experiment_output_matches_record(execution.result.stdout, experiment)
    assert all(
        artifact is not None and artifact.uri.startswith(".batches/")
        for artifact in (
            context.artifact_store.get("models/thymira-experiment.joblib"),
            context.artifact_store.get("metrics/thymira-experiment.json"),
        )
    )
    assert context.event_log.verify().valid
    inspector = ToolManager(
        ToolRegistry(
            (
                cast("Tool", InspectModel(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", QueryMlflow()),
            )
        )
    )
    inspected = inspector.execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/thymira-experiment.joblib",
            "description": "Inspect the trained experiment model",
        },
    )
    audited = inspector.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/thymira-experiment.joblib",
            "dataset": "training",
            "target_column": "target",
            "protected_column": "x1",
            "reference_dataset": "reference",
            "description": "Audit the trained experiment model",
        },
    )
    queried = inspector.execute(context, "query_mlflow")
    assert inspected.call.status is ToolCallStatus.COMPLETED
    inspected_payload = json.loads(inspected.result.stdout)
    # The default training script persists a Pipeline (column encoding + classifier), but
    # inspect_model reports it through its final estimator, so class name and coefficients are
    # the wrapped LogisticRegression's, not the pipeline wrapper's.
    assert inspected_payload["class_name"] == "LogisticRegression"
    assert inspected_payload["input_signature"]["n_features_in"] == 2
    # The model is fitted on the labels the dataset carries, so its class space names them.
    assert inspected_payload["output_signature"]["classes"] == ["no", "yes"]
    assert inspected_payload["coefficients"]
    assert inspected_payload["pipeline_steps"] == [
        ["encode", "ColumnTransformer"],
        ["classify", "LogisticRegression"],
    ]
    assert audited.call.status is ToolCallStatus.COMPLETED
    audited_payload = json.loads(audited.result.stdout)
    assert "subgroups" in audited_payload
    assert audited_payload["calibration"]["positive_rate"] == 0.75
    assert audited_payload["drift"]["reference_dataset"] == "reference"
    assert audited_payload["drift"]["numeric_features"]["x1"]["mean_delta"] == 0.0
    assert queried.call.status is ToolCallStatus.COMPLETED
    assert experiment.tracker_run_id is not None
    assert experiment.tracker_run_id in queried.result.stdout

    custom = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "custom",
            "description": "Train a dummy most-frequent classifier",
            "code": """
import json
import joblib
from sklearn.dummy import DummyClassifier

model = DummyClassifier(strategy="most_frequent")
model.fit([[0], [1]], [0, 1])
joblib.dump(model, model_path)
metrics_path.write_text(json.dumps({"accuracy": 0.5}), encoding="utf-8")
""",
        },
    )
    assert custom.call.status is ToolCallStatus.COMPLETED

    (context.workspace / ".thymira" / "model.joblib").unlink()
    (context.workspace / ".thymira" / "metrics.json").unlink()
    invalid = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "experiment_name": "invalid-custom",
            "description": "Run invalid training code on purpose",
            "code": "pass",
        },
    )
    assert invalid.call.status is ToolCallStatus.FAILED
    assert invalid.result.error == (
        "experiment code must create model.joblib and valid metrics.json"
    )

    failed_manager = ToolManager(
        ToolRegistry(
            (
                cast(
                    "Tool",
                    RunExperiment(
                        sandbox=_FixedSandbox(
                            SandboxRun(
                                stdout="",
                                stderr="training failed",
                                exit_code=2,
                                mode=SandboxMode.WORKSPACE_WRITE,
                                enforcement=SandboxEnforcement.PARTIAL,
                            )
                        )
                    ),
                ),
            )
        )
    )
    failed = failed_manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "description": "Train the baseline on a fixed sandbox",
        },
    )
    assert failed.call.status is ToolCallStatus.FAILED
    assert failed.result.error == "training failed"


def test_local_tracker_publishes_validated_artifact_bytes(tmp_path: Path) -> None:
    """Tracker publication does not depend on a temporary staging path remaining visible."""
    tracker = MlflowTracker(tmp_path)
    run_id = tracker.start_run("bytes")

    tracker.log_artifact_bytes(run_id, "validated-model.joblib", b"model")

    record = tracker.query_runs(run_id=run_id)[0]
    assert record.artifacts == (f"artifacts/{run_id}/validated-model.joblib",)


def test_inspect_model_reports_tree_feature_importances_and_feature_names(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="inspect_model", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    model = DecisionTreeClassifier(random_state=7).fit(
        [[0, 1], [1, 0], [1, 1], [0, 0]], [0, 1, 1, 0]
    )
    model.feature_names_in_ = np.array(["left", "right"])
    model_path = context.workspace / "tree.joblib"
    joblib.dump(model, model_path)
    context.artifact_store.save_bytes(
        "models/tree.joblib",
        model_path.read_bytes(),
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )

    execution = ToolManager(
        ToolRegistry((cast("Tool", InspectModel(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    ).execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/tree.joblib",
            "description": "Inspect the decision tree model",
        },
    )
    payload = json.loads(execution.result.stdout)

    assert execution.call.status is ToolCallStatus.COMPLETED
    # TOOL-21 Done-when: the estimator class name, its hyperparameters and a feature-importance
    # vector of the expected length (one weight per input feature).
    assert payload["class_name"] == "DecisionTreeClassifier"
    assert payload["parameters"]["random_state"] == 7
    assert payload["parameters"]["criterion"] == "gini"
    assert len(payload["feature_importances"]) == 2
    assert payload["input_signature"]["feature_names_in"] == ["left", "right"]
    assert payload["output_signature"]["n_outputs"] == 1
    # The deliverable requires the load to run in the Sandbox; the local sandbox reports PARTIAL.
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL


def test_audit_model_reports_subgroup_and_auc_evidence_on_german_credit(tmp_path: Path) -> None:
    """Audit german_credit for subgroup performance, AUC, and deterministic re-runs.

    TOOL-22 Done-when: the evidence contains per-subgroup performance for a protected attribute,
    an overall AUC matching sklearn within tolerance, and byte-identical metrics on a re-run.
    """
    dataset = Path(__file__).resolve().parents[2] / "data" / "german_credit.csv"
    if not dataset.is_file():
        pytest.skip(f"demo dataset missing: {dataset}")
    context = _context(
        tmp_path, capability=ToolCapability(id="audit_model", external_effects=()), development=True
    )
    context.workspace.mkdir(parents=True, exist_ok=True)

    # Build a numeric view -- the numeric columns plus a binary protected attribute (is_female,
    # from personal_status_sex) -- so a plain estimator can train and audit.
    raw = pl.read_csv(dataset)
    numeric = [
        name for name, dtype in zip(raw.columns, raw.dtypes, strict=True) if dtype.is_numeric()
    ]
    frame = raw.select(
        *[pl.col(name) for name in numeric if name != "is_high_risk"],
        pl.col("personal_status_sex").str.starts_with("mujer").cast(pl.Int64).alias("is_female"),
        pl.col("is_high_risk"),
    ).drop_nulls()
    credit_csv = context.workspace / "credit.csv"
    frame.write_csv(credit_csv)
    register_dataset(context.artifact_store, credit_csv, "credit", produced_by=context.agent_id)

    loaded = load_dataset(context.artifact_store, "credit", max_rows=1_000_000)
    feature_columns = [name for name in loaded.columns if name != "is_high_risk"]
    x_values = loaded.select(feature_columns).to_numpy()
    y_values = loaded.get_column("is_high_risk").to_numpy()
    model = LogisticRegression(solver="liblinear", random_state=0).fit(x_values, y_values)
    serialized = context.workspace / "credit.joblib"
    joblib.dump(model, serialized)
    context.artifact_store.save_bytes(
        "models/credit.joblib",
        serialized.read_bytes(),
        produced_by=context.agent_id,
        kind=ArtifactKind.MODEL,
        media_type="application/octet-stream",
    )
    expected_auc = float(roc_auc_score(y_values, model.predict_proba(x_values)[:, 1]))

    manager = ToolManager(
        ToolRegistry((cast("Tool", AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )
    arguments = {
        "model_artifact": "models/credit.joblib",
        "dataset": "credit",
        "target_column": "is_high_risk",
        "protected_column": "is_female",
        "description": "Audit credit model subgroup evidence",
    }
    first = manager.execute(context, "audit_model", arguments)
    second = manager.execute(context, "audit_model", arguments)

    assert first.call.status is ToolCallStatus.COMPLETED
    payload = json.loads(first.result.stdout)
    # Per-subgroup performance for the protected attribute: both groups present, each with a count.
    assert set(payload["subgroups"]) == {"0", "1"}
    for group in payload["subgroups"].values():
        assert "accuracy" in group
        assert group["count"] > 0
    # An overall AUC matching sklearn within tolerance.
    assert payload["auc"] == pytest.approx(expected_auc, abs=1e-9)
    # The credit frame was built with `.drop_nulls()`, so no row is missing "is_female".
    assert payload["subgroups_excluded_missing_protected"] == 0
    # A REPORT artifact of evidence was written; the tool authorizes nothing.
    assert first.result.artifact_ids
    # Byte-identical metrics on a re-run: the evidence is deterministic.
    assert first.result.stdout == second.result.stdout


def test_run_experiment_rejects_registered_parquet_dataset(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "training.parquet"
    pl.DataFrame({"value": [1.0, 2.0], "target": [0, 1]}).write_parquet(source)
    register_dataset(context.artifact_store, source, "training", produced_by=context.agent_id)

    with pytest.raises(ValueError, match="requires a registered CSV dataset"):
        RunExperiment().execute(
            context.for_tool(),
            {"dataset": "training", "target_column": "target"},
        )


def test_mcp_server_exposes_descriptors_and_routes_through_manager(tmp_path: Path) -> None:
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )
    server = McpToolServer(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),)),
        manager,
        lambda: context,
    )

    descriptors = server.list_tools()
    response = server.call(
        "run_python", {"code": "print('mcp')", "description": "Print the mcp marker"}
    )
    assert descriptors[0].name == "run_python"
    assert descriptors[0].input_schema["type"] == "object"
    assert response["success"] is True
    assert response["stdout"] == "mcp\n"
    assert context.event_log.verify().valid

    async def round_trip() -> None:
        async with create_connected_server_and_client_session(server.protocol_server()) as client:
            await client.initialize()
            listed = await client.list_tools()
            called = await client.call_tool(
                "run_python",
                {"code": "print('protocol')", "description": "Print the protocol marker"},
            )
            assert any(tool.name == "run_python" for tool in listed.tools)
            assert "protocol" in cast("TextContent", called.content[0]).text

    asyncio.run(round_trip())


def test_mcp_schema_advertises_object_for_argumentless_tools() -> None:
    tool = _FakeTool(
        "no_args",
        ToolCapability(id="no_args", external_effects=()),
        arguments_model=None,
    )

    assert _mcp_input_schema(tool) == {"type": "object", "properties": {}}


@pytest.mark.slow
def test_worktrees_are_detached_and_isolate_writes(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="git_worktree_create", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=context.workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=context.workspace,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=context.workspace, check=True)
    (context.workspace / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=context.workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=context.workspace, check=True)

    worktrees = ToolManager(
        ToolRegistry(
            (
                cast("Tool", GitWorktreeCreate(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", GitWorktreeRemove(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )
    first = worktrees.execute(
        context,
        "git_worktree_create",
        {"name": "first", "description": "Create the first isolated worktree"},
    )
    second = worktrees.execute(
        context,
        "git_worktree_create",
        {"name": "second", "description": "Create the second isolated worktree"},
    )
    first_path = context.workspace / ".thymira" / "worktrees" / "first"
    second_path = context.workspace / ".thymira" / "worktrees" / "second"

    assert first.call.status is ToolCallStatus.COMPLETED
    assert second.call.status is ToolCallStatus.COMPLETED
    python = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )
    first_run = python.execute(
        replace(context, workspace=first_path),
        "run_python",
        {
            "code": "from pathlib import Path; Path('first.txt').write_text('one')",
            "description": "Write the first worktree marker file",
        },
    )
    second_run = python.execute(
        replace(context, workspace=second_path),
        "run_python",
        {
            "code": "from pathlib import Path; Path('second.txt').write_text('two')",
            "description": "Write the second worktree marker file",
        },
    )
    assert first_run.call.status is ToolCallStatus.COMPLETED
    assert second_run.call.status is ToolCallStatus.COMPLETED
    assert (first_path / "first.txt").is_file()
    assert not (first_path / "second.txt").exists()
    assert (second_path / "second.txt").is_file()
    assert not (second_path / "first.txt").exists()

    assert (
        worktrees.execute(
            context,
            "git_worktree_remove",
            {"name": "first", "description": "Remove the first isolated worktree"},
        ).call.status
        is ToolCallStatus.COMPLETED
    )
    assert (
        worktrees.execute(
            context,
            "git_worktree_remove",
            {"name": "second", "description": "Remove the second isolated worktree"},
        ).call.status
        is ToolCallStatus.COMPLETED
    )
    assert not first_path.exists()
    assert not second_path.exists()
    # ToolManager.execute wraps every call in a WorkspaceLock -- "the boundary for every
    # workspace effect" (thymira.tools.manager) -- a cross-platform advisory lock stored beside,
    # never inside, the workspace it guards, released rather than deleted on exit (deleting a
    # live lock file would race a concurrent acquirer; see
    # test_tools_workspace_tree.py::test_workspace_lock_is_a_sibling_path). Each run_python call
    # above therefore leaves one `.<name>.workspace.lock` file beside the worktree it ran
    # against, under `.thymira/worktrees/`; git_worktree_remove only removes the worktree
    # checkout itself, so those two sibling files are expected, already-tested residue, not a
    # leak. What this test claims -- that the worktrees are fully torn down and never mixed
    # writes -- is covered by the assertions above; the check below only has to show no *other*
    # untracked footprint survives removal.
    clean = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=context.workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    untracked = {line[3:] for line in clean.stdout.splitlines()}
    expected_lock_residue = {
        f".thymira/worktrees/.{name}.workspace.lock" for name in ("first", "second")
    }
    assert untracked == expected_lock_residue


def test_worktree_list_reports_created_detached_worktrees(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="git_worktree_create", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=context.workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=context.workspace,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=context.workspace, check=True)
    (context.workspace / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=context.workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=context.workspace, check=True)

    worktrees = ToolManager(
        ToolRegistry(
            (
                cast("Tool", GitWorktreeCreate(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", GitWorktreeList(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", GitWorktreeRemove(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )
    created = worktrees.execute(
        context,
        "git_worktree_create",
        {"name": "listed", "description": "Create the listed isolated worktree"},
    )
    listed = worktrees.execute(context, "git_worktree_list")

    assert created.call.status is ToolCallStatus.COMPLETED
    assert listed.call.status is ToolCallStatus.COMPLETED
    assert "worktree" in listed.result.stdout
    listed_path = str(context.workspace / ".thymira" / "worktrees" / "listed").replace("\\", "/")
    assert listed_path in listed.result.stdout

    removed = worktrees.execute(
        context,
        "git_worktree_remove",
        {"name": "listed", "description": "Remove the listed isolated worktree"},
    )
    assert removed.call.status is ToolCallStatus.COMPLETED


def test_worktree_tool_reports_sandbox_errors_as_failed_results(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="git_worktree_list", external_effects=()),
    )
    tool = GitWorktreeList(
        sandbox=_RaisingSandbox(ValueError("sandbox unavailable")),
        mode=SandboxMode.DANGER_FULL_ACCESS,
    )

    result = tool.execute(context.for_tool(), {})

    assert result.success is False
    assert result.error == "sandbox unavailable"


def test_container_sandbox_fails_closed_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_module.shutil, "which", lambda _name: None)

    result = container_module.ContainerSandbox().run(
        [sys.executable, "-c", "print('unreachable')"],
        workspace=tmp_path,
        mode=SandboxMode.READ_ONLY,
        timeout_s=1,
    )

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert "docker was not found" in result.stderr


@pytest.mark.parametrize(
    ("argv", "workspace", "timeout_s", "message"),
    [
        ([], "workspace", 1, "argv must not be empty"),
        (["python"], "workspace", 0, "timeout_s must be positive"),
        (["python"], "missing", 1, "workspace does not exist"),
    ],
)
def test_container_sandbox_rejects_invalid_inputs(
    tmp_path: Path,
    argv: list[str],
    workspace: str,
    timeout_s: float,
    message: str,
) -> None:
    selected_workspace = tmp_path / workspace if workspace == "missing" else tmp_path

    with pytest.raises(ValueError, match=message):
        container_module.ContainerSandbox().run(
            argv,
            workspace=selected_workspace,
            mode=SandboxMode.READ_ONLY,
            timeout_s=timeout_s,
        )


def test_local_sandbox_run_records_partial_enforcement_and_raises_the_a19_finding(
    tmp_path: Path,
) -> None:
    # Explicit unconfined development execution records partial enforcement. MIRA turns that
    # fact into exactly one HIGH A19 finding; test permission does not erase confinement risk.
    capability = ToolCapability(id="run_python", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunPython(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    execution = manager.execute(
        context,
        "run_python",
        {"code": "print('trained')", "description": "Print the trained marker"},
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    report = audit_run(
        AuditContext(context.run_id, context.event_log.events(), context.artifact_store)
    )
    findings = [finding for finding in report.findings if finding.control_id == "A19"]
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH


def test_query_mlflow_returns_logged_run_params_and_metrics(tmp_path: Path) -> None:
    """After run_experiment (TOOL-18) logs a run, query_mlflow returns its params and metrics."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "training.csv"
    source.write_text(
        "x1,x2,target\n0,0,no\n0,1,no\n1,0,yes\n1,1,yes\n2,0,yes\n2,1,yes\n3,0,yes\n3,1,yes\n",
        encoding="utf-8",
    )
    register_dataset(context.artifact_store, source, "training", produced_by=context.agent_id)
    trained = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    ).execute(
        context,
        "run_experiment",
        {
            "dataset": "training",
            "target_column": "target",
            "seed": 7,
            "description": "Train the logistic-regression baseline",
        },
    )
    logged_metrics = json.loads(trained.result.stdout)["metrics"]

    queried = ToolManager(ToolRegistry((cast("Tool", QueryMlflow()),))).execute(
        context, "query_mlflow"
    )

    assert queried.call.status is ToolCallStatus.COMPLETED
    runs = json.loads(queried.result.stdout)
    assert len(runs) == 1
    logged = runs[0]
    assert logged["params"]["seed"] == "7"
    assert logged["params"]["dataset"] == "training"
    assert logged["metrics"]["accuracy"] == pytest.approx(logged_metrics["accuracy"])
    assert logged["tags"] == {}


def test_compare_models_ranks_two_experiments_and_writes_report(tmp_path: Path) -> None:
    """Two tracker runs with different accuracy rank correctly and yield a REPORT artifact."""
    context = _context(
        tmp_path,
        capability=ToolCapability(id="compare_models", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    tracker = MlflowTracker(context.workspace)
    weaker = tracker.start_run("candidate-a")
    tracker.log_metric(weaker, "accuracy", 0.72)
    tracker.end_run(weaker)
    stronger = tracker.start_run("candidate-b")
    tracker.log_metric(stronger, "accuracy", 0.91)
    tracker.end_run(stronger)

    execution = ToolManager(ToolRegistry((cast("Tool", CompareModels()),))).execute(
        context,
        "compare_models",
        {
            "run_ids": [weaker, stronger],
            "metric": "accuracy",
            "description": "Compare candidate model accuracy",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED
    payload = json.loads(execution.result.stdout)
    assert payload["winner"] == stronger
    assert payload["ranking"] == [stronger, weaker]
    assert payload["winner_by_metric"]["accuracy"] == stronger
    report = context.artifact_store.get("comparisons/accuracy.json")
    assert report is not None
    assert report.kind is ArtifactKind.REPORT
    assert report.id in execution.result.artifact_ids


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        (RunPythonArguments, {"code": "print(1)"}),
        (WriteFileArguments, {"path": "a.txt", "content": "x"}),
        (RunExperimentArguments, {"dataset": "d", "target_column": "y"}),
    ],
)
def test_mutating_tool_arguments_require_a_description(
    model: type[BaseModel], arguments: dict[str, Any]
) -> None:
    """Every mutating tool's argument model rejects a call without a description."""
    with pytest.raises(ValidationError, match="description"):
        model.model_validate(arguments)


def test_run_python_schema_advertises_description_as_required() -> None:
    """The model-facing JSON schema lists description as required with its guidance text."""
    schema = RunPythonArguments.model_json_schema()

    assert "description" in schema["required"]
    assert "5-10 words" in schema["properties"]["description"]["description"]


def test_tool_guidance_returns_one_paragraph_per_named_tool_in_order() -> None:
    paragraphs = tool_guidance(builtins_registry(), ("run_python", "read_file"))

    assert len(paragraphs) == 2
    assert paragraphs[0].startswith("Check the [exit code: N] marker")
    assert paragraphs[1].startswith("Use the read_file tool")


def test_write_file_guidance_does_not_recommend_an_unavailable_tool() -> None:
    paragraphs = tool_guidance(builtins_registry(), ("write_file",))

    assert len(paragraphs) == 1
    assert "edit_file" not in paragraphs[0]


def test_write_file_descriptor_does_not_recommend_an_unavailable_tool() -> None:
    assert "edit_file" not in WriteFile().description


def test_tool_guidance_skips_tools_without_a_paragraph() -> None:
    paragraphs = tool_guidance(builtins_registry(), ("git_status",))

    assert paragraphs == ()


def test_tool_guidance_refuses_an_unknown_tool() -> None:
    with pytest.raises(KeyError):
        tool_guidance(builtins_registry(), ("no_such_tool",))


def test_register_dataset_names_the_ragged_lines_of_a_csv_it_refuses(tmp_path: Path) -> None:
    ragged = tmp_path / "ragged.csv"
    ragged.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n9\n", encoding="utf-8", newline="\n")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match=r"ragged\.csv: 2 row\(s\).*3 fields.*lines 3, 5") as info:
        register_dataset(store, ragged, "ragged", produced_by=new_id("agent"))

    assert "fix the file" in str(info.value)
    assert store.list_active() == []


def test_register_dataset_names_a_quoted_multiline_fields_ragged_line_correctly(
    tmp_path: Path,
) -> None:
    r"""A quoted field spanning physical lines must not shift the line numbers reported after it.

    The header is line 1; the quoted `"one\ntwo"` field occupies lines 2-3 as one record (3
    fields, matching the header); the ragged record ``4,5`` (2 fields) ends at physical line 4.
    """
    ragged = tmp_path / "ragged.csv"
    ragged.write_text('a,b,c\n"one\ntwo",2,3\n4,5\n', encoding="utf-8", newline="\n")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match=r"lines 4"):
        register_dataset(store, ragged, "ragged", produced_by=new_id("agent"))

    assert store.list_active() == []


def test_register_dataset_refuses_a_file_over_the_registration_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("thymira.tools.datasets.MAX_DATASET_BYTES", 10)
    oversized = tmp_path / "oversized.csv"
    oversized.write_text("a,b,c\n1,2,3\n4,5,6\n", encoding="utf-8", newline="\n")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match="exceeds"):
        register_dataset(store, oversized, "oversized", produced_by=new_id("agent"))

    assert store.list_active() == []


def test_the_shipped_demo_dataset_registers_as_shipped(tmp_path: Path) -> None:
    dataset = Path(__file__).resolve().parents[2] / "data" / "german_credit.csv"
    if not dataset.is_file():
        pytest.skip(f"demo dataset missing: {dataset}")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    _artifact, schema = register_dataset(
        store, dataset, "german_credit", produced_by=new_id("agent")
    )

    assert schema.row_count == 998
    assert len(schema.columns) == 21
    assert schema.columns[-1] == "is_high_risk"


def test_register_dataset_turns_any_polars_read_failure_into_a_value_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8", newline="\n")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match=r"empty\.csv: cannot be read as CSV"):
        register_dataset(store, empty, "empty", produced_by=new_id("agent"))

    assert store.list_active() == []


def test_register_dataset_turns_an_unreadable_parquet_into_a_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    store = LocalArtifactStore(tmp_path / "artifacts", new_id("run"))

    with pytest.raises(ValueError, match=r"bad\.parquet: cannot be read as Parquet"):
        register_dataset(store, bad, "bad", produced_by=new_id("agent"))

    assert store.list_active() == []


def _categorical_csv(path: Path, rows: int = 40) -> None:
    """Two categorical features, one numeric feature, one boolean feature, a string label.

    Rows 3 and 9 blank out the numeric column and row 5 blanks out a categorical column -- the
    cases that demoted a numeric column to one-hot categories and silently zeroed a fitted
    category. The `round` column is the boolean path: polars reads it as `Boolean`.
    """
    colours = ("red", "blue", "green")
    lines = ["colour,size,shape,round,label"]
    for index in range(rows):
        colour = "" if index == 5 else colours[index % 3]
        size = "" if index in (3, 9) else str(index % 7)
        shape = "round" if index % 2 == 0 else "square"
        is_round = "true" if index % 2 == 0 else "false"
        label = "yes" if (index % 3 == 0) != (index % 5 == 0) else "no"
        lines.append(f"{colour},{size},{shape},{is_round},{label}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _text_feature_rows(
    path: Path, features: list[str], boolean_columns: frozenset[str] = frozenset()
) -> list[list[object]]:
    """Read a CSV as the plain-text rows the default training script feeds the pipeline.

    A blank cell becomes `None` (missing on both sides of train and audit); a boolean column's
    `true`/`false` text becomes `1.0`/`0.0`, the value `np.asarray(..., dtype=float)` produces
    from the `True`/`False` polars hands the audit for a `Boolean` column. Mirrors
    `_training_code`'s own `cell` helper so this can feed the persisted pipeline directly and be
    compared against predictions made from polars-typed values.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    # Mirrors `cell()` in `run_experiment._training_code` and must change with it.
    def cell(name: str, value: str) -> object:
        if value == "":
            return None
        if name in boolean_columns:
            return 1.0 if value.strip().lower() in ("true", "1", "t", "yes") else 0.0
        return value

    return [[cell(name, row[name]) for name in features] for row in rows]


def test_run_experiment_default_training_handles_categorical_features(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "shapes.csv"
    _categorical_csv(source)
    register_dataset(context.artifact_store, source, "shapes", produced_by=context.agent_id)
    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", AuditModel(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "shapes",
            "target_column": "label",
            "experiment_name": "shapes-baseline",
            "description": "Train the default baseline on categorical features",
        },
    )

    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error
    reported = json.loads(trained.result.stdout)["metrics"]
    assert 0.0 <= reported["accuracy"] <= 1.0
    assert reported == context.artifact_store.load_json("metrics/shapes-baseline.json")

    audited = manager.execute(
        context,
        "audit_model",
        {
            "model_artifact": "models/shapes-baseline.joblib",
            "dataset": "shapes",
            "target_column": "label",
            "protected_column": "colour",
            "description": "Audit the categorical model",
        },
    )

    assert audited.call.status is ToolCallStatus.COMPLETED, audited.result.error
    payload = json.loads(audited.result.stdout)
    assert set(payload["subgroups"]) == {"blue", "green", "red"}
    assert "auc" in payload
    # Row 5's blanked `colour` is excluded from subgroup evidence, not silently folded in.
    assert payload["subgroups_excluded_missing_protected"] == 1

    # The persisted pipeline must predict the same whether it is fed the plain CSV text
    # run_experiment trained on or the polars-typed values audit_model feeds it -- including the
    # boolean `round` column, which polars types `Boolean` but the CSV spells `true`/`false`.
    model_path = tmp_path / "shapes-model.joblib"
    model_path.write_bytes(context.artifact_store.load_bytes("models/shapes-baseline.joblib"))
    model = joblib.load(model_path)
    features = ["colour", "size", "shape", "round"]
    text_rows = _text_feature_rows(source, features, boolean_columns=frozenset({"round"}))
    typed_rows = pl.read_csv(source).select(features).to_numpy().tolist()
    assert model.predict(text_rows).tolist() == model.predict(typed_rows).tolist()


def test_inspect_model_reports_a_pipeline_through_its_final_estimator(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "shapes.csv"
    _categorical_csv(source)
    register_dataset(context.artifact_store, source, "shapes", produced_by=context.agent_id)
    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", InspectModel(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "shapes",
            "target_column": "label",
            "experiment_name": "shapes-baseline",
            "description": "Train the default baseline on categorical features",
        },
    )
    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error

    inspected = manager.execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/shapes-baseline.joblib",
            "description": "Inspect the categorical model",
        },
    )

    assert inspected.call.status is ToolCallStatus.COMPLETED, inspected.result.error
    inspected_payload = json.loads(inspected.result.stdout)
    assert inspected_payload["class_name"] == "LogisticRegression"
    assert inspected_payload["pipeline_steps"][0] == ["encode", "ColumnTransformer"]
    assert len(inspected_payload["coefficients"][0]) == len(
        inspected_payload["input_signature"]["encoded_feature_names"]
    )


def test_run_experiment_default_training_keeps_the_raw_labels_on_the_model(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "numbers.csv"
    source.write_text(
        "x,y,label\n"
        + "\n".join(f"{i},{(i * 7) % 11},{int(i % 3 == 0)}" for i in range(40))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    register_dataset(context.artifact_store, source, "numbers", produced_by=context.agent_id)
    manager = ToolManager(
        ToolRegistry((cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),))
    )

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "numbers",
            "target_column": "label",
            "experiment_name": "numbers-baseline",
            "description": "Train the default baseline on numeric features",
        },
    )

    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error
    model_path = tmp_path / "model.joblib"
    model_path.write_bytes(context.artifact_store.load_bytes("models/numbers-baseline.joblib"))
    model = joblib.load(model_path)
    assert [str(label) for label in model.classes_] == ["0", "1"]
    assert len(model.predict([["1", "7"], ["2", "3"]])) == 2


def test_default_training_predicts_the_same_from_csv_text_and_polars_types(
    tmp_path: Path,
) -> None:
    """Pin the regression: blanks in german_credit's numeric columns must not turn them categorical.

    The pipeline must predict the same from the raw CSV text it trained on and from the
    polars-typed values `audit_model` feeds it -- and the encoding must stay small.
    """
    dataset = Path(__file__).resolve().parents[2] / "data" / "german_credit.csv"
    if not dataset.is_file():
        pytest.skip(f"demo dataset missing: {dataset}")
    context = _context(
        tmp_path,
        capability=ToolCapability(id="run_experiment", external_effects=()),
        development=True,
    )
    context.workspace.mkdir(parents=True, exist_ok=True)
    register_dataset(context.artifact_store, dataset, "german_credit", produced_by=context.agent_id)
    manager = ToolManager(
        ToolRegistry(
            (
                cast("Tool", RunExperiment(mode=SandboxMode.DANGER_FULL_ACCESS)),
                cast("Tool", InspectModel(mode=SandboxMode.DANGER_FULL_ACCESS)),
            )
        )
    )

    trained = manager.execute(
        context,
        "run_experiment",
        {
            "dataset": "german_credit",
            "target_column": "is_high_risk",
            "experiment_name": "german-credit-baseline",
            "description": "Train the default baseline on the shipped demo dataset",
        },
    )
    assert trained.call.status is ToolCallStatus.COMPLETED, trained.result.error
    # An unscaled `credit_amount` (tens of thousands) used to stall lbfgs at its iteration limit
    # and print a ConvergenceWarning on every default run; a clean stderr pins that it converges.
    assert trained.result.stderr == ""

    model_path = tmp_path / "german-credit-model.joblib"
    model_path.write_bytes(
        context.artifact_store.load_bytes("models/german-credit-baseline.joblib")
    )
    model = joblib.load(model_path)
    features = [name for name in pl.read_csv(dataset).columns if name != "is_high_risk"]
    text_rows = _text_feature_rows(dataset, features)
    typed_rows = pl.read_csv(dataset).select(features).to_numpy().tolist()
    assert model.predict(text_rows).tolist() == model.predict(typed_rows).tolist()

    inspected = manager.execute(
        context,
        "inspect_model",
        {
            "model_artifact": "models/german-credit-baseline.joblib",
            "description": "Inspect the shipped credit model",
        },
    )
    assert inspected.call.status is ToolCallStatus.COMPLETED, inspected.result.error
    inspected_payload = json.loads(inspected.result.stdout)
    assert len(inspected_payload["input_signature"]["encoded_feature_names"]) < 120


@pytest.mark.parametrize(
    ("tool", "tag"),
    [
        pytest.param(RunExperiment(), "model_training", id="run_experiment"),
        pytest.param(CompareModels(), "model_comparison", id="compare_models"),
    ],
)
def test_cr_001_reviews_the_training_and_comparison_builtins_on_sensitive_data(
    tool: Tool, tag: str
) -> None:
    """CR-001 matched only tags no built-in declared; training and comparison now declare them."""
    engine = PolicyEngine(load_policy_stack("credit_risk"))
    confident = RiskProfile(
        risk_level="medium", activity_category="model_development", confidence=1.0
    )
    sensitive = confident.model_copy(update={"risk_factors": ("sensitive_attributes",)})

    reviewed = engine.decide_capability(
        run_id=new_id("run"), subject_id="c1", capability=tool.capability, risk=sensitive
    )
    plain = engine.decide_capability(
        run_id=new_id("run"), subject_id="c2", capability=tool.capability, risk=confident
    )

    assert tag in tool.capability.risk_tags
    assert (reviewed.decision, reviewed.rule_id) == (Decision.REQUIRE_HUMAN_REVIEW, "CR-001")
    # The rule reviews the call itself; it no longer arms a run-wide review of every call.
    assert reviewed.execution_constraints.requires_human_review is False
    assert plain.rule_id != "CR-001"


def _files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


def _run_owned(tmp_path: Path, path: Path) -> bool:
    """Inside the Run's workspace or artifact store, or the Tool Manager's own workspace lock."""
    if path.name == ".workspace.workspace.lock" and path.parent == tmp_path:
        return True  # the manager's lock beside the workspace, not a tool output
    roots = (tmp_path / "workspace", tmp_path / "artifacts")
    return any(path.is_relative_to(root) for root in roots)


def _artifact_writer_context(tmp_path: Path) -> ToolContext:
    """A real base+credit-risk Gate, no approver, a confident profile: only the rules decide."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack("credit_risk")), log, approver=None),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="medium", activity_category="data_analysis", confidence=1.0
        ),
    )


def _scores_dataset(context: ToolContext) -> None:
    context.workspace.mkdir(parents=True, exist_ok=True)
    source = context.workspace / "scores.csv"
    source.write_text("value,grp\n1,A\n2,A\n10,B\n12,B\n", encoding="utf-8")
    register_dataset(context.artifact_store, source, "scores", produced_by=context.agent_id)


def _tracker_runs(context: ToolContext) -> list[str]:
    context.workspace.mkdir(parents=True, exist_ok=True)
    tracker = MlflowTracker(context.workspace)
    ids = []
    for name, accuracy in (("a", 0.7), ("b", 0.9)):
        run_id = tracker.start_run(name)
        tracker.log_metric(run_id, "accuracy", accuracy)
        tracker.end_run(run_id)
        ids.append(run_id)
    return ids


def test_mlflow_log_tools_accept_a_batch_in_one_call(tmp_path: Path) -> None:
    """One call logs several params or metrics, so an agent spends one turn, not one per key."""
    capability = ToolCapability(id="mlflow_start_run", external_effects=())
    context = _context(tmp_path, capability=capability, development=True)
    context.workspace.mkdir(parents=True, exist_ok=True)
    manager = ToolManager(ToolRegistry(mlflow_tools()))
    started = manager.execute(
        context, "mlflow_start_run", {"experiment_name": "smoke", "description": "start"}
    )
    run_id = json.loads(started.result.stdout)["tracker_run_id"]

    params = manager.execute(
        context,
        "mlflow_log_param",
        {"run_id": run_id, "params": {"seed": 7, "model": "xgb"}, "description": "batch"},
    )
    metrics = manager.execute(
        context,
        "mlflow_log_metric",
        {"run_id": run_id, "metrics": {"auc": 0.8, "f1": 0.5}, "description": "batch"},
    )
    neither = manager.execute(
        context, "mlflow_log_metric", {"run_id": run_id, "description": "nothing to log"}
    )
    both = manager.execute(
        context,
        "mlflow_log_metric",
        {"run_id": run_id, "key": "acc", "value": 0.9, "metrics": {"auc": 0.8}, "description": "x"},
    )

    run = MlflowTracker(context.workspace).query_runs(run_id=run_id)[0]
    assert params.call.status is ToolCallStatus.COMPLETED
    assert metrics.call.status is ToolCallStatus.COMPLETED
    assert neither.call.status is ToolCallStatus.FAILED
    assert both.call.status is ToolCallStatus.FAILED
    assert run.params == {"seed": "7", "model": "xgb"}
    assert run.metrics == {"auc": 0.8, "f1": 0.5}


def _mlflow_calls(context: ToolContext) -> list[tuple[str, dict[str, Any]]]:
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "metrics.json").write_text("{}", encoding="utf-8")
    run_id = MlflowTracker(context.workspace).start_run("seed")
    return [
        ("mlflow_start_run", {"experiment_name": "smoke", "description": "start"}),
        ("mlflow_log_param", {"run_id": run_id, "key": "seed", "value": "7", "description": "p"}),
        ("mlflow_log_metric", {"run_id": run_id, "key": "acc", "value": 0.9, "description": "m"}),
        ("mlflow_log_artifact", {"run_id": run_id, "path": "metrics.json", "description": "a"}),
        ("mlflow_end_run", {"run_id": run_id, "description": "end"}),
    ]


def _export_pdf_calls(context: ToolContext) -> list[tuple[str, dict[str, Any]]]:
    context.workspace.mkdir(parents=True, exist_ok=True)
    (context.workspace / "report.md").write_text("# Findings\n\nDone.\n", encoding="utf-8")
    return [
        (
            "export_pdf",
            {"source_path": "report.md", "path": "report.pdf", "description": "compile"},
        )
    ]


_DATASET = {"dataset": "scores", "description": "d"}


@pytest.mark.parametrize(
    ("prepare", "calls"),
    [
        pytest.param(_scores_dataset, [("profile_dataset", _DATASET)], id="profile_dataset"),
        pytest.param(_scores_dataset, [("analyze_dataset", _DATASET)], id="analyze_dataset"),
        pytest.param(
            _scores_dataset,
            [
                (
                    "query_sql",
                    {**_DATASET, "query": "SELECT grp, AVG(value) FROM dataset GROUP BY grp"},
                )
            ],
            id="query_sql",
        ),
        pytest.param(
            _scores_dataset,
            [("run_statistics", {**_DATASET, "value_column": "value", "group_column": "grp"})],
            id="run_statistics",
        ),
        pytest.param(None, _mlflow_calls, id="mlflow_tools"),
        pytest.param(None, _export_pdf_calls, id="export_pdf"),
    ],
)
def test_artifact_writers_pass_under_gov_009_and_stay_inside_the_workspace(
    tmp_path: Path, prepare: Any, calls: Any
) -> None:
    """Each reclassified tool runs with no human and leaves files only in the Run's own space."""
    context = _artifact_writer_context(tmp_path)
    if prepare is not None:
        prepare(context)
    planned = calls(context) if callable(calls) else calls
    before = _files_under(tmp_path)
    manager = ToolManager(builtins_registry())

    for name, arguments in planned:
        execution = manager.execute(context, name, arguments)
        assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.error
        assert execution.pending_approval is None

    decisions = [
        event.payload
        for event in context.event_log.events()
        if event.type is EventType.POLICY_DECISION and event.payload["subject_kind"] == "tool_call"
    ]
    assert {decision["rule_id"] for decision in decisions} == {"GOV-009"}
    assert all(decision["decision"] == "PASS" for decision in decisions)
    assert all(decision["execution_constraints"]["local_execution_only"] for decision in decisions)
    escaped = [path for path in _files_under(tmp_path) - before if not _run_owned(tmp_path, path)]
    assert escaped == []


def test_compare_models_passes_under_gov_009_and_stays_inside_the_workspace(
    tmp_path: Path,
) -> None:
    context = _artifact_writer_context(tmp_path)
    ids = _tracker_runs(context)
    before = _files_under(tmp_path)

    execution = ToolManager(builtins_registry()).execute(
        context,
        "compare_models",
        {"run_ids": ids, "metric": "accuracy", "description": "compare"},
    )

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.error
    assert execution.pending_approval is None
    decision = next(
        event.payload
        for event in context.event_log.events()
        if event.type is EventType.POLICY_DECISION and event.payload["subject_kind"] == "tool_call"
    )
    assert (decision["decision"], decision["rule_id"]) == ("PASS", "GOV-009")
    assert all(_run_owned(tmp_path, path) for path in _files_under(tmp_path) - before)


def test_code_running_writers_still_need_a_human_under_gov_008(tmp_path: Path) -> None:
    """Relaxing artifact writes never touched the tools that run code or write arbitrary files."""
    context = _artifact_writer_context(tmp_path)
    engine = context.gate.engine
    tools = {tool.name: tool for tool in builtins_registry()}

    for name in (
        "run_python",
        "run_notebook",
        "write_file",
        "run_experiment",
        "inspect_model",
        "audit_model",
    ):
        decision = engine.decide_capability(
            run_id=context.run_id,
            subject_id=name,
            capability=tools[name].capability,
            risk=context.risk_profile,
        )
        assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW, name
        assert decision.rule_id in {"GOV-008", "capability_default"}, (name, decision.rule_id)


def test_an_unnamed_side_effect_falls_to_the_reviewing_capability_default() -> None:
    """GOV-008 no longer matches everything; the default still asks a human for the rest."""
    decision = PolicyEngine(load_policy_stack()).decide_capability(
        run_id=new_id("run"),
        subject_id="c1",
        capability=ToolCapability(id="novel", side_effects=("registry_write",)),
        risk=RiskProfile(risk_level="medium", activity_category="x", confidence=1.0),
    )

    assert (decision.decision, decision.rule_id) == (
        Decision.REQUIRE_HUMAN_REVIEW,
        "capability_default",
    )


def test_an_artifact_writer_is_still_escalated_for_an_uncertain_classification(
    tmp_path: Path,
) -> None:
    decision = PolicyEngine(load_policy_stack()).decide_capability(
        run_id=new_id("run"),
        subject_id="c1",
        capability=ProfileDataset().capability,
        risk=RiskProfile(),
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert decision.rule_id == "GOV-009"
    assert "Fail-safe escalation" in decision.reason
