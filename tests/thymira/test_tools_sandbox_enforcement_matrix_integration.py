"""Actual sandbox-mode enforcement through Tool Manager and a fresh MIRA reader.

The real boundary is the configured ``ContainerSandbox`` or ``LocalSubprocessSandbox`` running a
child; the human approval is supplied by the existing test identity, while model calls and remote
services are absent. Each successful container case has the child probe its filesystem and network
view, then re-opens the persisted JSONL chain and lets MIRA recompute authorization and execution
evidence independently. Refused local confinement and a missing container image are checked as
tool results, with no unconfined fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import JsonlEventLog, verify_log
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import CapabilityRule, Policy, PolicyEngine, ToolCapability
from thymira.schemas import (
    Decision,
    EventType,
    SandboxEnforcement,
    SandboxMode,
    ToolCallStatus,
    new_id,
)
from thymira.tools import (
    ProcessToolValue,
    Tool,
    ToolContext,
    ToolInvocation,
    ToolManager,
    ToolRegistry,
    ToolResult,
)
from thymira.tools.builtins.subprocess_command import with_sandbox_evidence
from thymira.tools.builtins.subprocess_mode import (
    capability_for_sandbox_mode,
    requested_sandbox_mode,
)
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox, Sandbox

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.schemas import Event

pytestmark = pytest.mark.integration

_CHILD_PROBE = """
import json
import socket
from pathlib import Path

observations = {}
for key, path in {
    "workspace_write": Path("/workspace/matrix-probe.txt"),
    "root_write": Path("/matrix-root-probe.txt"),
}.items():
    try:
        path.write_text("probe", encoding="utf-8")
        observations[key] = "succeeded"
    except OSError as exc:
        observations[key] = type(exc).__name__

try:
    with socket.create_connection(("1.1.1.1", 53), timeout=2):
        observations["network_connect"] = "succeeded"
except OSError as exc:
    observations["network_connect"] = type(exc).__name__

print(json.dumps(observations, sort_keys=True))
"""


class _ProbeArguments(BaseModel):
    """Arguments for the real backend probe tool."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _SandboxProbeTool:
    """Route one real sandbox child through the Tool Manager boundary."""

    sandbox: Sandbox
    capability: ToolCapability
    name: str = "sandbox_probe"
    description: str = "Probe the configured sandbox boundary."
    arguments_model: type[BaseModel] | None = _ProbeArguments
    result_model: type[BaseModel] = ProcessToolValue

    def execute(self, invocation: ToolInvocation, arguments: dict[str, Any]) -> ToolResult:
        """Run the child under the capability's declared mode and preserve backend facts."""
        mode = requested_sandbox_mode(self.capability)
        run = self.sandbox.run(
            ["python", "-I", "-u", "-B", "-c", arguments["code"]],
            workspace=invocation.workspace,
            mode=mode,
            timeout_s=10,
        )
        return with_sandbox_evidence(
            ToolResult(
                success=run.exit_code == 0,
                value=ProcessToolValue(
                    text=run.stdout,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                ),
                stdout=run.stdout,
                stderr=run.stderr,
                exit_code=run.exit_code,
                error=None if run.exit_code == 0 else (run.stderr or "sandbox probe failed"),
            ),
            run,
            timeout_s=10,
        )


def _review_policy() -> Policy:
    """Require an explicit human answer while permitting this matrix's declared effects."""
    return Policy(
        name="sandbox-enforcement-matrix",
        version="1.0",
        capability_rules=(
            CapabilityRule(
                id="matrix-human-review",
                decision=Decision.REQUIRE_HUMAN_REVIEW,
                reason="Sandbox matrix calls require an explicit human approval.",
            ),
        ),
        capability_default_decision=Decision.REQUIRE_HUMAN_REVIEW,
        capability_default_reason="Sandbox matrix calls require approval.",
    )


def _context(tmp_path: Path) -> tuple[ToolContext, Path]:
    """Build a human-approved context backed by a durable event log."""
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = human_approved_context(
        tmp_path,
        run_id=run_id,
        log=JsonlEventLog(events_path, run_id),
        engine=PolicyEngine(_review_policy()),
    )
    return replace(context, workspace=workspace), events_path


def _tool(mode: SandboxMode, sandbox: Sandbox) -> Tool:
    """Build a probe capability whose approval is bound to ``mode``."""
    capability = capability_for_sandbox_mode(
        ToolCapability(
            id="sandbox_probe",
            risk_tags=("code_execution",),
            side_effects=("workspace_write",),
        ),
        mode,
    )
    return cast("Tool", _SandboxProbeTool(sandbox=sandbox, capability=capability))


def _replayed(context: ToolContext, events_path: Path) -> list[Event]:
    """Read the chain through a separate event-log instance and verify its bytes."""
    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed = JsonlEventLog(events_path, context.run_id).events()
    assert len(replayed) == verification.event_count
    return replayed


def _controls(context: ToolContext, events: list[Event]) -> dict[str, Any]:
    """Run MIRA over the fresh replay and return controls by id."""
    report = audit_run(AuditContext(context.run_id, events))
    return {control.control_id: control for control in report.controls}


@pytest.mark.parametrize(
    ("mode", "expected_workspace_write", "expected_network", "expected_mount"),
    [
        (SandboxMode.READ_ONLY, False, False, ":ro"),
        (SandboxMode.WORKSPACE_WRITE, True, False, ":rw"),
        (SandboxMode.DANGER_FULL_ACCESS, True, None, ":rw"),
    ],
)
def test_tool_manager_approval_cannot_widen_actual_container_mode(
    tmp_path: Path,
    mode: SandboxMode,
    expected_workspace_write: bool,
    expected_network: bool | None,
    expected_mount: str,
) -> None:
    """An approved capability preserves the requested mode and the child's observed boundary."""
    require_sandbox_image()
    context, events_path = _context(tmp_path)
    manager = ToolManager(ToolRegistry((_tool(mode, ContainerSandbox(image="thymira:dev")),)))

    execution = manager.execute(context, "sandbox_probe", {"code": _CHILD_PROBE})

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.stderr
    assert execution.call.sandbox_mode is mode
    assert execution.result.sandbox_mode is mode
    assert execution.result.sandbox_enforcement is SandboxEnforcement.PARTIAL
    observed = json.loads(execution.result.stdout)
    assert (observed["workspace_write"] == "succeeded") is expected_workspace_write
    assert observed["root_write"] != "succeeded"
    assert isinstance(observed["network_connect"], str)
    if expected_network is False:
        assert observed["network_connect"] != "succeeded"

    events = _replayed(context, events_path)
    started = next(event for event in events if event.type is EventType.TOOL_STARTED)
    completed = next(event for event in events if event.type is EventType.TOOL_COMPLETED)
    approval = next(event for event in events if event.type is EventType.HUMAN_APPROVAL)
    assert approval.payload["approved"] is True
    assert "sandbox_enforcement" not in approval.payload
    assert started.payload["sandbox_mode"] == mode.value
    assert completed.payload["requested_sandbox_mode"] == mode.value
    assert completed.payload["sandbox_mode"] == mode.value
    assert completed.payload["sandbox_enforcement"] == SandboxEnforcement.PARTIAL.value
    spec = completed.payload["sandbox_spec"]
    assert isinstance(spec, dict)
    assert spec["backend"] == "container"
    assert spec["workspace_mount"].endswith(expected_mount)
    assert spec["network"] == ("bridge" if mode is SandboxMode.DANGER_FULL_ACCESS else "none")
    termination = completed.payload["sandbox_termination"]
    assert isinstance(termination, dict)
    assert termination["probe"]["network"] == spec["network"]

    controls = _controls(context, events)
    assert controls["A3"].status is ControlStatus.PASSED, controls["A3"].detail
    assert controls["A29"].status is ControlStatus.PASSED, controls["A29"].detail
    assert controls["A30"].status is ControlStatus.PASSED, controls["A30"].detail
    assert controls["A19"].status is ControlStatus.FAILED


@pytest.mark.parametrize("mode", [SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE])
def test_tool_manager_refuses_unsupported_local_confinement_without_running_a_child(
    tmp_path: Path, mode: SandboxMode
) -> None:
    """A local backend returns an honest unusable result for a confined approved request."""
    context, events_path = _context(tmp_path)
    manager = ToolManager(ToolRegistry((_tool(mode, LocalSubprocessSandbox()),)))

    execution = manager.execute(
        context,
        "sandbox_probe",
        {"code": "from pathlib import Path; Path('must-not-run').touch()"},
    )

    assert not execution.result.success
    assert execution.result.exit_code == 125
    assert execution.result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not (context.workspace / "must-not-run").exists()
    events = _replayed(context, events_path)
    completed = next(event for event in events if event.type is EventType.TOOL_COMPLETED)
    assert completed.payload["requested_sandbox_mode"] == mode.value
    assert completed.payload["sandbox_enforcement"] == SandboxEnforcement.UNUSABLE.value
    spec = completed.payload["sandbox_spec"]
    assert spec["backend"] == "local_subprocess"
    assert "filesystem" in spec["unenforced"]
    controls = _controls(context, events)
    assert controls["A3"].status is ControlStatus.PASSED, controls["A3"].detail
    assert controls["A19"].status is ControlStatus.FAILED
    assert controls["A29"].status is ControlStatus.PASSED, controls["A29"].detail
    assert controls["A30"].status is ControlStatus.NOT_APPLICABLE


def test_tool_manager_refuses_an_unavailable_container_without_host_fallback(
    tmp_path: Path,
) -> None:
    """A missing image is a tool refusal with no child execution or host fallback."""
    require_sandbox_image()
    context, events_path = _context(tmp_path)
    manager = ToolManager(
        ToolRegistry(
            (
                _tool(
                    SandboxMode.WORKSPACE_WRITE,
                    ContainerSandbox(image="thymira-enforcement-matrix-missing:never-pull"),
                ),
            )
        )
    )

    execution = manager.execute(
        context,
        "sandbox_probe",
        {"code": "from pathlib import Path; Path('must-not-run').touch()"},
    )

    assert not execution.result.success
    assert execution.result.exit_code == 125
    assert execution.result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert not (context.workspace / "must-not-run").exists()
    events = _replayed(context, events_path)
    completed = next(event for event in events if event.type is EventType.TOOL_COMPLETED)
    assert completed.payload["requested_sandbox_mode"] == SandboxMode.WORKSPACE_WRITE.value
    assert completed.payload["sandbox_enforcement"] == SandboxEnforcement.UNUSABLE.value
    assert completed.payload["sandbox_spec"]["backend"] == "container"
    assert completed.payload["sandbox_termination"]["outcome"] == "unavailable"
    controls = _controls(context, events)
    assert controls["A3"].status is ControlStatus.PASSED, controls["A3"].detail
    assert controls["A19"].status is ControlStatus.FAILED
    assert controls["A29"].status is ControlStatus.PASSED, controls["A29"].detail
    assert controls["A30"].status is ControlStatus.NOT_APPLICABLE
