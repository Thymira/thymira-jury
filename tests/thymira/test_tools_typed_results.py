"""Independent evidence tests for typed tool dispatch and result projections."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, Field

from thymira.events import InMemoryEventLog
from thymira.policies import (
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import (
    EventType,
    SandboxEnforcement,
    SandboxMode,
    ThymiraModel,
    ToolCallStatus,
    new_id,
)
from thymira.state import LocalArtifactStore
from thymira.tools import (
    ProcessToolValue,
    QueryRowsValue,
    Sandbox,
    Tool,
    ToolContext,
    ToolFailureValue,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
    ToolResultCode,
    canonical_result,
    reconstruct_value,
    result_schema,
)
from thymira.tools.builtins import builtins_registry
from thymira.tools.builtins.run_python import RunPython
from thymira.tools.mcp import McpToolServer
from thymira.tools.projections import project_recorded_tool_text, project_tool_value
from thymira.tools.sandbox import SandboxRun

if TYPE_CHECKING:
    from pathlib import Path


class _EchoValue(ThymiraModel):
    """A deliberately narrow value used by the hostile producer tests."""

    text: str = Field(min_length=1)
    count: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class _EchoTool:
    """A typed producer whose legacy stdout can be forged independently."""

    name: str = "echo"
    description: str = "Return one typed value."
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="echo", external_effects=())
    )
    arguments_model: None = None
    result_model: type[ThymiraModel] = _EchoValue

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a value whose text is authoritative over a forged scalar field."""
        value = _EchoValue(text="canonical", count=2)
        return ToolResult(success=True, stdout="forged", value=value)


@dataclass(frozen=True, slots=True)
class _MissingValueTool(_EchoTool):
    """A producer that omits its required typed value."""

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a malformed successful envelope."""
        return ToolResult(success=True, stdout="missing")


@dataclass(frozen=True, slots=True)
class _WrongValueTool(_EchoTool):
    """A producer that returns a value outside its declared schema."""

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a count with the wrong type and let the manager reject it."""
        return ToolResult(
            success=True,
            value={"text": "wrong", "count": "not-an-integer"},  # ty: ignore[invalid-argument-type]  # hostile producer
        )


@dataclass(frozen=True, slots=True)
class _NonResultTool(_EchoTool):
    """A producer that violates the in-process envelope type altogether."""

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a non-result object and let the manager close the lifecycle safely."""
        return cast("ToolResult", {"stdout": "bypass"})


@dataclass(frozen=True, slots=True)
class _UntypedTool:
    """A dispatch candidate with no result schema."""

    name: str = "untyped"
    description: str = "No output schema."
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="untyped", external_effects=())
    )
    arguments_model: None = None

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """This body must never be reachable through a live registry."""
        raise AssertionError("untyped producer executed")


@dataclass(frozen=True, slots=True)
class _GenericSchemaTool(_UntypedTool):
    """A dynamic registration that tries to use the unconstrained Pydantic base model."""

    name: str = "generic"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="generic", external_effects=())
    )
    result_model = BaseModel


class _LooseValue(BaseModel):
    """A mutable, permissive model a caller must not register as a contract."""

    text: str


class _AnyValue(ThymiraModel):
    """A frozen model whose payload is unconstrained and cannot be replayed safely."""

    text: str
    payload: Any


class _ObjectValue(ThymiraModel):
    """A frozen model whose payload advertises the open JSON ``object`` type."""

    text: str
    payload: object


class _MappingValue(ThymiraModel):
    """A frozen model whose mapping omits both key and value constraints."""

    text: str
    payload: dict


class _BareListValue(ThymiraModel):
    """A result model whose list element type is omitted."""

    text: str
    payload: list


class _BareTupleValue(ThymiraModel):
    """A result model whose tuple element type is omitted."""

    text: str
    payload: tuple


class _BareSetValue(ThymiraModel):
    """A result model whose set element type is omitted."""

    text: str
    payload: set


class _NestedLooseValue(BaseModel):
    """A nested mutable model that would silently drop its own extra fields."""

    text: str


class _NestedValue(ThymiraModel):
    """A result model containing a nested model with a permissive contract."""

    text: str
    nested: _NestedLooseValue


@dataclass(frozen=True, slots=True)
class _LooseSchemaTool(_UntypedTool):
    """A dynamic registration that tries to use BaseModel's default extra handling."""

    name: str = "loose"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="loose", external_effects=())
    )
    result_model = _LooseValue


@dataclass(frozen=True, slots=True)
class _AnySchemaTool(_UntypedTool):
    """A dynamic registration that tries to use an ``Any`` output field."""

    name: str = "any-value"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="any-value", external_effects=())
    )
    result_model = _AnyValue


@dataclass(frozen=True, slots=True)
class _ObjectSchemaTool(_UntypedTool):
    """A dynamic registration that tries to use an ``object`` output field."""

    name: str = "object-value"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="object-value", external_effects=())
    )
    result_model = _ObjectValue


@dataclass(frozen=True, slots=True)
class _MappingSchemaTool(_UntypedTool):
    """A dynamic registration that tries to use an unconstrained mapping."""

    name: str = "mapping-value"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="mapping-value", external_effects=())
    )
    result_model = _MappingValue


@dataclass(frozen=True, slots=True)
class _NestedSchemaTool(_UntypedTool):
    """A dynamic registration that tries to hide permissive fields in a nested model."""

    name: str = "nested-value"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="nested-value", external_effects=())
    )
    result_model = _NestedValue


@dataclass(frozen=True, slots=True)
class _BareListSchemaTool(_UntypedTool):
    """A dynamic registration with an unparameterized list field."""

    name: str = "bare-list"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="bare-list", external_effects=())
    )
    result_model = _BareListValue


@dataclass(frozen=True, slots=True)
class _BareTupleSchemaTool(_UntypedTool):
    """A dynamic registration with an unparameterized tuple field."""

    name: str = "bare-tuple"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="bare-tuple", external_effects=())
    )
    result_model = _BareTupleValue


@dataclass(frozen=True, slots=True)
class _BareSetSchemaTool(_UntypedTool):
    """A dynamic registration with an unparameterized set field."""

    name: str = "bare-set"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="bare-set", external_effects=())
    )
    result_model = _BareSetValue


@dataclass(frozen=True, slots=True)
class _ExtraValueTool(_EchoTool):
    """A producer that returns a source field absent from its declared contract."""

    name: str = "extra"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="extra", external_effects=())
    )

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a value with an unexpected source field."""
        return ToolResult(
            success=True,
            value={"text": "canonical", "count": 2, "unexpected": "must reject"},  # ty: ignore[invalid-argument-type]  # hostile producer
        )


@dataclass(frozen=True, slots=True)
class _StrayStderrTool(_EchoTool):
    """A producer whose auxiliary stderr is absent from its typed value."""

    name: str = "stray-stderr"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="stray-stderr", external_effects=())
    )

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return canonical text plus an unmodelled stderr field."""
        return ToolResult(
            success=True,
            value=_EchoValue(text="canonical", count=2),
            stderr="stray stderr",
        )


@dataclass(frozen=True, slots=True)
class _FailedTool(_EchoTool):
    """A producer that returns an ordinary typed failure."""

    name: str = "failed"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="failed", external_effects=())
    )

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a failed envelope without a successful value."""
        return ToolResult(success=False, error="producer failed")


@dataclass(frozen=True, slots=True)
class _FailedProcessTool(_EchoTool):
    """A process-backed producer whose child failed after writing real output."""

    name: str = "failed-process"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="failed-process", external_effects=())
    )
    result_model: type[ThymiraModel] = ProcessToolValue

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a failed envelope carrying the process facts the runtime observed."""
        return ToolResult(
            success=False,
            stdout='{"isolated": true}',
            stderr="Traceback: the child refused the frame",
            exit_code=3,
            error="python execution failed",
        )


@dataclass(frozen=True, slots=True)
class _FailedOutputTool(_EchoTool):
    """A non-process producer that failed after recording its own output."""

    name: str = "failed-output"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="failed-output", external_effects=())
    )

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a failed envelope whose stdout and stderr are not producer claims."""
        return ToolResult(
            success=False,
            stdout="partial output",
            stderr="recorded stderr",
            error="producer failed",
        )


@dataclass(frozen=True, slots=True)
class _TimeoutTool(_EchoTool):
    """A producer that reports a bounded child timeout and active abort."""

    name: str = "timeout"
    capability: ToolCapability = field(
        default_factory=lambda: ToolCapability(id="timeout", external_effects=())
    )

    def execute(self, _invocation: ToolInvocation, _arguments: dict[str, Any]) -> ToolResult:
        """Return a canonical timeout result with its budget and abort fact."""
        return ToolResult(
            success=False,
            code=ToolResultCode.TOOL_TIMEOUT,
            timeout_s=0.5,
            aborted=True,
            exit_code=124,
            error="tool timed out",
        )


class _TimeoutSandbox:
    """A sandbox double returning the standard process timeout outcome."""

    def run(
        self,
        _argv: list[str],
        *,
        workspace: Path,
        mode: SandboxMode,
        timeout_s: float,
        env: Any = None,
        staged_inputs: Any = None,
    ) -> SandboxRun:
        """Return a timed out child without launching a process.

        ``staged_inputs`` is accepted and ignored: the backend contract stages runtime inputs, and
        no child runs here, so the double only has to satisfy the protocol its caller now uses.
        """
        return SandboxRun(
            stdout="partial",
            stderr=f"sandbox timeout after {timeout_s}s",
            exit_code=124,
            mode=mode,
            enforcement=SandboxEnforcement.PARTIAL,
            timed_out=True,
        )


def _context(tmp_path: Path) -> ToolContext:
    """Build an isolated context with the regular policy and event chain."""
    run_id = new_id("run")
    log = InMemoryEventLog(run_id)
    return ToolContext(
        run_id=run_id,
        agent_id=new_id("agent"),
        workspace=tmp_path / "workspace",
        event_log=log,
        gate=Gate(PolicyEngine(load_policy_stack()), log, approver=auto_approve),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts", run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )


def test_every_builtin_declares_a_concrete_output_schema() -> None:
    """The production registry exposes a non-empty schema for every concrete producer."""
    registry = builtins_registry()

    assert all(result_schema(tool).get("properties") for tool in registry)
    assert len(tuple(registry)) >= 20


def test_untyped_tool_cannot_enter_dispatch() -> None:
    """A dynamic registry cannot bypass the typed result boundary."""
    with pytest.raises(ValueError, match="typed result model"):
        ToolRegistry((cast("Tool", _UntypedTool()),))
    with pytest.raises(ValueError, match="ThymiraModel"):
        ToolRegistry((cast("Tool", _GenericSchemaTool()),))
    with pytest.raises(ValueError, match="ThymiraModel"):
        ToolRegistry((cast("Tool", _LooseSchemaTool()),))
    for tool in (
        _AnySchemaTool(),
        _ObjectSchemaTool(),
        _MappingSchemaTool(),
        _NestedSchemaTool(),
        _BareListSchemaTool(),
        _BareTupleSchemaTool(),
        _BareSetSchemaTool(),
    ):
        with pytest.raises(ValueError, match="Any, object, or open mappings"):
            ToolRegistry((cast("Tool", tool),))


def test_durable_value_is_the_projection_oracle(tmp_path: Path) -> None:
    """The manager and an independent reader agree on the same canonical typed value."""
    tool = _EchoTool()
    context = _context(tmp_path)
    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.COMPLETED
    assert execution.result.stdout == "canonical"
    assert execution.result.stderr == ""
    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    durable = completed.payload["value"]
    value = execution.result.value
    assert value is not None
    assert durable == canonical_result(value, success=True)
    rebuilt = reconstruct_value(tool, durable)
    assert project_tool_value(rebuilt) == durable["value"]
    assert project_recorded_tool_text(tool, durable) == "canonical"


def test_mcp_projects_auxiliary_fields_from_the_durable_typed_value(tmp_path: Path) -> None:
    """MCP does not expose producer fields that the canonical value cannot reconstruct."""
    tool = _StrayStderrTool()
    context = _context(tmp_path)
    registry = ToolRegistry((cast("Tool", tool),))
    response = McpToolServer(registry, ToolManager(registry), lambda: context).call(tool.name, {})

    assert response["value"] == {"kind": "success", "value": {"text": "canonical", "count": 2}}
    assert response["stdout"] == "canonical"
    assert response["stderr"] == ""


@pytest.mark.parametrize("tool_type", [_MissingValueTool, _WrongValueTool])
def test_malformed_typed_values_are_recorded_as_failed_results(
    tmp_path: Path, tool_type: type[_EchoTool]
) -> None:
    """Missing or schema-invalid producer values never become successful evidence."""
    tool = tool_type()
    context = _context(tmp_path)
    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert execution.result.value is not None
    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    assert completed.payload["status"] == ToolCallStatus.FAILED
    assert completed.payload["value"]["kind"] == "failure"
    assert completed.payload["value"]["value"]["error"]
    assert context.event_log.verify().valid


def test_unexpected_source_fields_are_recorded_as_failed_results(tmp_path: Path) -> None:
    """A strict result model rejects source fields that are absent from its contract."""
    tool = _ExtraValueTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    assert completed.payload["value"]["kind"] == "failure"
    assert "invalid typed result" in completed.payload["value"]["value"]["error"]
    assert reconstruct_value(tool, completed.payload["value"]).model_dump(mode="json")["error"]


def test_failed_result_reconstructs_from_its_discriminated_durable_envelope(
    tmp_path: Path,
) -> None:
    """An ordinary failed call replays through the failure schema without its producer."""
    tool = _FailedTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    durable = completed.payload["value"]
    rebuilt = reconstruct_value(tool, durable)
    assert execution.call.status is ToolCallStatus.FAILED
    assert durable["kind"] == "failure"
    assert rebuilt.model_dump(mode="json")["error"] == "producer failed"


def test_failed_process_result_keeps_the_recorded_process_facts(tmp_path: Path) -> None:
    """A failed process tool keeps its stdout, stderr and exit code beside the typed failure."""
    tool = _FailedProcessTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert isinstance(execution.result.value, ToolFailureValue)
    assert execution.result.stdout == '{"isolated": true}'
    assert execution.result.stderr == "Traceback: the child refused the frame"
    assert execution.result.exit_code == 3
    assert execution.result.error == "python execution failed"
    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    assert completed.payload["exit_code"] == 3
    assert completed.payload["value"]["kind"] == "failure"


def test_failed_non_process_result_keeps_the_output_it_recorded(tmp_path: Path) -> None:
    """A failed tool outside the process family keeps the stdout and stderr it recorded."""
    tool = _FailedOutputTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert isinstance(execution.result.value, ToolFailureValue)
    assert execution.result.stdout == "partial output"
    assert execution.result.stderr == "recorded stderr"
    assert execution.result.error == "producer failed"


def test_query_rows_rejects_non_json_scalars_and_wrong_row_width() -> None:
    """SQL values are closed JSON scalars and every row matches the declared columns."""
    with pytest.raises(ValueError, match="query rows must have the same width"):
        QueryRowsValue(columns=("value",), rows=((1, 2),), row_count=1)
    with pytest.raises(ValueError, match="JSON scalar"):
        QueryRowsValue(
            columns=("value",),
            rows=((object(),),),  # ty: ignore[invalid-argument-type]  # hostile producer
            row_count=1,
        )


def test_timeout_result_replays_its_budget_and_abort_semantics(tmp_path: Path) -> None:
    """A timeout is a typed failure distinct from an ordinary tool failure."""
    tool = _TimeoutTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    durable = completed.payload["value"]
    assert execution.call.status is ToolCallStatus.FAILED
    assert durable["kind"] == "failure"
    assert durable["value"]["code"] == ToolResultCode.TOOL_TIMEOUT
    assert durable["value"]["budget_seconds"] == 0.5
    assert durable["value"]["aborted"] is True
    assert completed.payload["result_code"] == ToolResultCode.TOOL_TIMEOUT
    assert completed.payload["timeout_s"] == 0.5
    assert completed.payload["aborted"] is True


def test_run_python_timeout_records_the_declared_budget(tmp_path: Path) -> None:
    """A sandbox timeout carries run_python's configured timeout into the typed failure."""
    tool = RunPython(sandbox=cast("Sandbox", _TimeoutSandbox()))
    context = _context(tmp_path)
    context = replace(
        context,
        gate=Gate(
            PolicyEngine(
                Policy(
                    name="timeout-test",
                    version="1",
                    capability_rules=(
                        CapabilityRule(
                            id="allow-timeout",
                            decision="PASS",
                            reason="allow timeout test",
                            side_effects=("workspace_write",),
                        ),
                    ),
                )
            ),
            context.event_log,
        ),
    )

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(
        context,
        tool.name,
        {"code": "print('never reached')", "timeout_s": 0.5, "description": "Run timeout"},
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.value is not None
    assert execution.result.value.model_dump(mode="json")["code"] == "TOOL_TIMEOUT"
    assert execution.result.value.model_dump(mode="json")["budget_seconds"] == 0.5
    assert execution.result.value.model_dump(mode="json")["aborted"] is True


def test_non_result_producer_cannot_escape_the_manager_boundary(tmp_path: Path) -> None:
    """A producer bypassing ``ToolResult`` still gets a typed failed completion."""
    tool = _NonResultTool()
    context = _context(tmp_path)

    execution = ToolManager(ToolRegistry((cast("Tool", tool),))).execute(context, tool.name, {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.value is not None
    assert execution.result.value.model_dump(mode="json")["error"]
    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    assert completed.payload["value"]["kind"] == "failure"
    assert completed.payload["value"]["value"]["error"]
    assert context.event_log.verify().valid
