"""Lifecycle guarantees of the Tool Manager: no started event is left without its completed.

These cover findings #40-1/N4 (an unexpected exception from a tool must not escape between
``tool.started`` and ``tool.completed``) and #14-5 (an unknown tool name is a recorded failed
call, never an unguarded ``KeyError`` that leaves zero events).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from thymira.core import UsageLedger
from thymira.events import InMemoryEventLog
from thymira.policies import (
    BudgetRule,
    CapabilityRule,
    Gate,
    Policy,
    PolicyEngine,
    RiskProfile,
    ToolCapability,
    auto_approve,
    load_policy_stack,
)
from thymira.schemas import Decision, EventType, ToolCallStatus, new_id
from thymira.state import LocalArtifactStore
from thymira.tools import (
    BudgetRefusal,
    LegacyToolValue,
    Tool,
    ToolContext,
    ToolExecution,
    ToolExecutionError,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
    reconstruct_value,
)
from thymira.tools.models import ToolEvent

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _FakeTool(Tool):
    """A tool whose behaviour each test fixes: return a result, or raise a chosen exception."""

    name: str
    capability: ToolCapability
    description: str = "A fake tool for lifecycle tests."
    arguments_model: None = None
    result_model = LegacyToolValue
    result: ToolResult = field(
        default_factory=lambda: ToolResult(
            success=True, stdout="ok", value=LegacyToolValue(text="ok")
        )
    )
    raise_exc: BaseException | None = None
    save_artifact_first: bool = False
    seen_invocation: list[ToolInvocation] = field(default_factory=list)

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Record the invocation, optionally write an artifact, then act as configured."""
        self.seen_invocation.append(invocation)
        if self.save_artifact_first:
            invocation.artifact_store.save_json(
                "partial.json", {"partial": True}, produced_by=invocation.agent_id
            )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def _context(tmp_path: Path, *, capability: ToolCapability) -> ToolContext:
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


def test_manager_checks_the_shared_budget_before_executing_a_tool(tmp_path: Path) -> None:
    """A hard tool-call ceiling emits a Gate decision before the tool implementation runs."""
    capability = ToolCapability(id="echo", external_effects=())
    base = _context(tmp_path, capability=capability)
    ledger = UsageLedger()
    ledger.charge_tool()
    policy = Policy(
        name="tool-budget",
        version="1.0",
        capability_rules=(
            CapabilityRule(
                id="ALLOW-ECHO",
                decision=Decision.PASS,
                reason="allow the local echo tool",
                external_effects=(),
            ),
        ),
        budget_rules=(
            BudgetRule(
                id="HARD-CALLS",
                decision=Decision.BLOCK,
                reason="tool-call budget exhausted",
                max_tool_calls=1,
            ),
        ),
    )
    gate = Gate(PolicyEngine(policy), base.event_log)

    def budget_guard() -> BudgetRefusal | None:
        """Refuse under the budget decision, the way `_tool_budget_guard` in Core does."""
        current = ledger.snapshot()
        proposed = {**current, "tool_calls": int(current["tool_calls"] or 0) + 1}
        decision = gate.check_budget(proposed, summary="tool budget", cost_so_far=current)
        if decision.decision is Decision.BLOCK:
            return BudgetRefusal(reason=decision.reason, decision_id=decision.id)
        ledger.charge_tool()
        return None

    tool = _FakeTool("echo", capability)
    context = replace(base, gate=gate, budget_guard=budget_guard)
    execution = ToolManager(ToolRegistry((tool,))).execute(context, "echo")

    assert execution.call.status is ToolCallStatus.DENIED
    assert tool.seen_invocation == []
    assert [event.type for event in context.event_log.events()] == [
        EventType.POLICY_DECISION,
        EventType.POLICY_DECISION,
        EventType.TOOL_DENIED,
    ]
    budget_decision = context.event_log.events()[1]
    assert budget_decision.payload["decision"] == Decision.BLOCK.value
    # The denial names what refused the call, not the capability decision that allowed it.
    denial = context.event_log.events()[2]
    assert denial.payload["decision_id"] == budget_decision.payload["id"]
    capability_decision = context.event_log.events()[0]
    assert execution.call.policy_decision_id == capability_decision.payload["id"]


def test_manager_serializes_workspace_effects_across_calls(tmp_path: Path) -> None:
    """The manager's sibling lock prevents two workspace effects from overlapping."""
    capability = ToolCapability(id="slow", external_effects=())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_lock = threading.Lock()
    active = 0
    peak = 0

    @dataclass(frozen=True, slots=True)
    class _SlowTool(Tool):
        name: str
        capability: ToolCapability
        description: str = "A tool that holds the workspace briefly."
        arguments_model: None = None
        result_model = LegacyToolValue

        def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
            del invocation, arguments
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return ToolResult(success=True, stdout="ok", value=LegacyToolValue(text="ok"))

    manager = ToolManager(ToolRegistry((_SlowTool("slow", capability),)))
    contexts = [
        replace(_context(tmp_path / f"call-{index}", capability=capability), workspace=workspace)
        for index in range(2)
    ]
    start = threading.Barrier(2)
    executions: list[ToolExecution] = []

    def invoke(context: ToolContext) -> None:
        start.wait()
        executions.append(manager.execute(context, "slow"))

    threads = [threading.Thread(target=invoke, args=(context,)) for context in contexts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(executions) == 2
    assert all(execution.result.success for execution in executions)
    assert peak == 1


def test_an_unexpected_exception_from_a_tool_is_recorded_not_left_escaping(tmp_path: Path) -> None:
    """A bare ValueError out of a tool becomes a failed call, not an escape past the started event.

    MIRA's A9 control pairs ``tool.started`` with ``tool.completed``; an escaping exception would
    leave a started with no completed, and the append-only log can never be corrected afterwards.
    """
    capability = ToolCapability(id="boom", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("boom", capability, raise_exc=ValueError("kaboom"))

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "boom", {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    types = [event.type for event in context.event_log.events()]
    assert types == [
        EventType.POLICY_DECISION,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    assert context.event_log.verify().valid


def test_an_unexpected_exception_stays_distinguishable_from_an_expected_failure(
    tmp_path: Path,
) -> None:
    """The error text of an unexpected exception carries its type name; an expected one does not.

    A verifier auditing the log must be able to tell a tool that failed as designed
    (``ToolExecutionError``) from a tool that crashed on an unhandled error.
    """
    capability = ToolCapability(id="boom", external_effects=())

    unexpected_context = _context(tmp_path / "unexpected", capability=capability)
    unexpected = ToolManager(
        ToolRegistry((_FakeTool("boom", capability, raise_exc=ValueError("kaboom")),))
    ).execute(unexpected_context, "boom", {})

    expected_context = _context(tmp_path / "expected", capability=capability)
    expected = ToolManager(
        ToolRegistry((_FakeTool("boom", capability, raise_exc=ToolExecutionError("planned")),))
    ).execute(expected_context, "boom", {})

    assert unexpected.result.error is not None
    assert "ValueError" in unexpected.result.error
    assert expected.result.error == "planned"
    assert "ToolExecutionError" not in (expected.result.error or "")
    completed = next(
        event
        for event in unexpected_context.event_log.events()
        if event.type is EventType.TOOL_COMPLETED
    )
    assert "ValueError" in completed.payload["error"]


def test_the_unexpected_exception_path_still_diffs_artifacts_and_completes(tmp_path: Path) -> None:
    """Post-call bookkeeping is not skipped when a tool raises: an artifact it wrote is recorded.

    Skipping the artifact diff and the completed event on an exception is the same fail-open shape
    the lifecycle exists to close.
    """
    capability = ToolCapability(id="boom", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("boom", capability, raise_exc=RuntimeError("late"), save_artifact_first=True)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "boom", {})

    assert execution.call.status is ToolCallStatus.FAILED
    types = [event.type for event in context.event_log.events()]
    assert EventType.ARTIFACT_CREATED in types
    assert types[-1] is EventType.TOOL_COMPLETED
    # The artifact written before the tool raised is diffed and carried onto the completed call.
    assert len(execution.call.artifact_ids) == 1


@pytest.mark.parametrize("signal", [KeyboardInterrupt(), SystemExit()])
def test_process_control_signals_still_propagate(tmp_path: Path, signal: BaseException) -> None:
    """``KeyboardInterrupt`` and ``SystemExit`` are not caught; the guarantee stops at Exception.

    Swallowing these would trap an operator's Ctrl-C or an interpreter shutdown inside a fake
    ``ToolResult``. They derive from ``BaseException``, and the manager catches only ``Exception``.
    """
    capability = ToolCapability(id="boom", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("boom", capability, raise_exc=signal)

    with pytest.raises(type(signal)):
        ToolManager(ToolRegistry((tool,))).execute(context, "boom", {})


def test_an_unknown_tool_name_is_a_recorded_failed_call_not_an_exception(tmp_path: Path) -> None:
    """A model naming a tool that does not exist is ordinary traffic that must leave evidence.

    Finding #14-5: the previous ``registry.get`` raised an unguarded ``KeyError`` before any
    event was written, so an unknown tool produced no evidence at all.
    """
    capability = ToolCapability(id="present", external_effects=())
    present = _FakeTool("present", capability)
    context = _context(tmp_path, capability=capability)

    execution = ToolManager(ToolRegistry((present,))).execute(context, "absent", {})

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert execution.result.error is not None
    assert "absent" in execution.result.error
    # Nothing was authorized and nothing ran: exactly one completed event, no policy decision,
    # no started event, and the registered tool was never invoked.
    assert [event.type for event in context.event_log.events()] == [EventType.TOOL_COMPLETED]
    assert present.seen_invocation == []
    assert context.event_log.verify().valid


def test_a_tool_declared_event_the_log_refuses_still_completes_the_call(tmp_path: Path) -> None:
    """A tool cannot strand its own lifecycle through the events it declares.

    A declared event's type and payload come from the tool, so appending one is still inside the
    tool's reach: a payload the log cannot canonicalize raises after ``tool.started`` and before
    ``tool.completed`` -- the same gap catching the tool's own exceptions closes one line above it.
    The failure is recorded on the call instead of half-announcing what the tool did.
    """
    capability = ToolCapability(id="declares", external_effects=())
    context = _context(tmp_path, capability=capability)
    impossible = ToolResult(
        success=True,
        events=(ToolEvent(type=EventType.EXPERIMENT_STARTED, payload={"o": object()}),),
    )
    tool = _FakeTool("declares", capability, result=impossible)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "declares", {})

    types = [event.type for event in context.event_log.events()]
    assert types == [
        EventType.POLICY_DECISION,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    assert execution.call.status is ToolCallStatus.FAILED
    assert "recording a tool event" in (execution.result.error or "")
    completed = context.event_log.events()[-1]
    durable = completed.payload["value"]
    assert durable["kind"] == "failure"
    assert "recording a tool event" in durable["value"]["error"]
    assert reconstruct_value(tool, durable).model_dump(mode="json")["error"]
    assert context.event_log.verify().valid


def test_an_exception_whose_message_cannot_be_rendered_still_completes(tmp_path: Path) -> None:
    """Describing the failure must never be the thing that fails.

    The handler that guarantees a completed record built its error text with ``str(exc)``, which
    runs the exception's own ``__str__`` -- and a third-party tool, the reason this boundary
    catches broadly at all, can raise from there. The raise happened inside the guarantee.
    """

    class UnrenderableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("the message itself raises")

    capability = ToolCapability(id="unrenderable", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("unrenderable", capability, raise_exc=UnrenderableError())

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "unrenderable", {})

    types = [event.type for event in context.event_log.events()]
    assert types == [
        EventType.POLICY_DECISION,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]
    assert execution.call.status is ToolCallStatus.FAILED
    assert "UnrenderableError" in (execution.result.error or "")


def test_a_spill_failure_replaces_the_success_value_with_a_replayable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-start spill fault cannot leave a success-shaped canonical value under FAILED."""
    capability = ToolCapability(id="spill", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("spill", capability)

    def fail_spill(*_args: Any, **_kwargs: Any) -> ToolResult:
        """Force the manager's post-start spill branch."""
        raise OSError("disk full")

    monkeypatch.setattr("thymira.tools.manager.spill_result", fail_spill)
    execution = ToolManager(ToolRegistry((tool,))).execute(context, "spill", {})

    completed = next(
        event for event in context.event_log.events() if event.type is EventType.TOOL_COMPLETED
    )
    durable = completed.payload["value"]
    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.success is False
    assert durable["kind"] == "failure"
    assert "spilling the result" in durable["value"]["error"]
    assert reconstruct_value(tool, durable).model_dump(mode="json")["error"]


def test_an_artifact_listing_failure_replaces_the_success_value_with_a_replayable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-start artifact listing fault cannot retain the producer's success value."""
    capability = ToolCapability(id="artifact-list", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("artifact-list", capability)
    original_list_active = context.artifact_store.list_active
    calls = 0

    def fail_after_started() -> list[Any]:
        """Allow the pre-call snapshot and fail the post-call listing."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("manifest unavailable")
        return original_list_active()

    monkeypatch.setattr(context.artifact_store, "list_active", fail_after_started)
    execution = ToolManager(ToolRegistry((tool,))).execute(context, tool.name, {})

    durable = context.event_log.events()[-1].payload["value"]
    assert execution.call.status is ToolCallStatus.FAILED
    assert durable["kind"] == "failure"
    assert "recording produced artifacts" in durable["value"]["error"]
    assert reconstruct_value(tool, durable).model_dump(mode="json")["error"]


def test_an_artifact_event_failure_replaces_the_success_value_with_a_replayable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed artifact announcement cannot retain the producer's success value."""
    capability = ToolCapability(id="artifact-event", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("artifact-event", capability, save_artifact_first=True)
    original_append = context.event_log.append

    def fail_artifact_event(*args: Any, **kwargs: Any) -> Any:
        """Reject only the artifact-created event while preserving lifecycle events."""
        if args and args[0] is EventType.ARTIFACT_CREATED:
            raise OSError("event sink unavailable")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(context.event_log, "append", fail_artifact_event)
    execution = ToolManager(ToolRegistry((tool,))).execute(context, tool.name, {})

    durable = context.event_log.events()[-1].payload["value"]
    assert execution.call.status is ToolCallStatus.FAILED
    assert durable["kind"] == "failure"
    assert "recording produced artifacts" in durable["value"]["error"]
    assert reconstruct_value(tool, durable).model_dump(mode="json")["error"]


def test_final_serialization_failure_replaces_the_success_value_with_a_replayable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical serialization fault cannot leave a successful value under FAILED."""
    capability = ToolCapability(id="serialize", external_effects=())
    context = _context(tmp_path, capability=capability)
    tool = _FakeTool("serialize", capability)
    from thymira.tools import manager as manager_module

    original_canonical_result = manager_module.canonical_result

    def fail_success_serialization(value: Any, *, success: bool) -> dict[str, object]:
        """Fail only while serializing the producer's success value."""
        if success:
            raise TypeError("cannot serialize success")
        return original_canonical_result(value, success=success)

    monkeypatch.setattr(manager_module, "canonical_result", fail_success_serialization)
    execution = ToolManager(ToolRegistry((tool,))).execute(context, tool.name, {})

    durable = context.event_log.events()[-1].payload["value"]
    assert execution.call.status is ToolCallStatus.FAILED
    assert durable["kind"] == "failure"
    assert "cannot serialize success" in durable["value"]["error"]
    assert reconstruct_value(tool, durable).model_dump(mode="json")["error"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_code", float("nan")),
        ("exit_code", object()),
        ("code", object()),
        ("timeout_s", float("nan")),
        ("timeout_s", object()),
        ("aborted", object()),
        ("result_sha256", "not-a-sha"),
        ("sandbox_mode", object()),
        ("sandbox_enforcement", object()),
        ("artifact_ids", (object(),)),
        ("artifact_ids", object()),
    ],
)
def test_a_result_field_the_log_cannot_record_costs_the_field_not_the_record(
    tmp_path: Path, field: str, value: object
) -> None:
    """``ToolResult`` is a plain dataclass, so nothing checked these before they reached the log.

    ``model_copy(update=...)`` does not revalidate either, so a NaN exit code or an arbitrary
    object went straight into the completed event's payload and raised in the log's canonicalizer
    -- after ``tool.started`` and before ``tool.completed``, stranding the call on an append-only
    log where it can never be closed. A malformed field now fails the call and is named in the
    error.
    """
    capability = ToolCapability(id="poison", external_effects=())
    context = _context(tmp_path, capability=capability)
    # The types are deliberately wrong: that is the input a tool can actually produce, since
    # ToolResult is a plain dataclass that validates nothing.
    poisoned = ToolResult(success=True, stdout="ok", **{field: value})  # ty: ignore[invalid-argument-type]
    tool = _FakeTool("poison", capability, result=poisoned)

    execution = ToolManager(ToolRegistry((tool,))).execute(context, "poison", {})

    types = [event.type for event in context.event_log.events()]
    assert EventType.TOOL_COMPLETED in types
    assert context.event_log.verify().valid
    if field not in {"artifact_ids", "code", "timeout_s", "aborted"}:
        assert "unrecordable result fields rejected" in (execution.call.error or "")


def test_an_unknown_tool_name_is_bounded_before_it_reaches_the_log(tmp_path: Path) -> None:
    """The name of an unknown tool is model-supplied, and the log does not redact an actor id.

    Making an unknown tool a recorded failed call was right, but it also handed a caller-supplied
    string a route onto the append-only, hash-chained log -- as ``ToolCall.tool_name`` and as the
    event ``Actor.id``, neither of which bounds its length or its characters. An empty name was
    worse: it raised out of the ``ToolCall`` constructor before a single event existed, which is
    the no-evidence crash the change set out to remove.
    """
    context = _context(tmp_path, capability=ToolCapability(id="none", external_effects=()))
    manager = ToolManager(ToolRegistry(()))

    long_name = manager.execute(context, "x" * 5000, {})
    empty_name = manager.execute(context, "   ", {})

    assert long_name.call.status is ToolCallStatus.FAILED
    assert len(long_name.call.tool_name) < 100
    assert empty_name.call.status is ToolCallStatus.FAILED
    assert empty_name.call.tool_name == "<unnamed>"
    assert context.event_log.verify().valid
