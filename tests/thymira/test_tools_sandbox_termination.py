"""How one sandbox execution ended, recorded as evidence rather than inferred from an exit code.

Every node here drives a real child process. The runtime's claim -- "this finished cleanly" --
is checked against facts a different observer produced: the parent's own clock, the OS wait
status, and (through ``ToolManager``) the persisted ``tool.completed`` payload replayed from a
separately constructed event log.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.thymira.fixtures_tools import development_policy, review_gated_context
from thymira.events import JsonlEventLog, verify_log
from thymira.policies import PolicyEngine, RiskProfile
from thymira.schemas import EventType, SandboxEnforcement, SandboxMode, new_id
from thymira.tools import ToolManager
from thymira.tools.builtins import configured_builtins_registry
from thymira.tools.sandbox import LocalSubprocessSandbox
from thymira.tools.sandbox.process_capture import ProcessObservation, run_bounded_process

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace


def test_local_sandbox_records_termination_evidence_for_a_clean_child(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    run = LocalSubprocessSandbox().run(
        ["python", "-c", "print('done')"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=30,
    )

    assert run.exit_code == 0
    termination = run.termination
    assert termination is not None
    assert termination.outcome == "completed"
    assert termination.control_channel == "os_wait"
    assert termination.control_channel_validated is True
    assert termination.timed_out is False
    assert termination.deadline_exceeded is False
    assert termination.child_exit_code == 0
    assert termination.deadline_s == pytest.approx(30.0)
    assert termination.duration_s is not None
    assert 0.0 <= termination.duration_s < 30.0
    assert termination.reap == "not_required"
    assert termination.reap_deadline_s is None
    assert termination.reap_duration_s is None
    assert termination.signalled_after_reap is False


def test_run_bounded_process_reports_a_timeout_when_the_exit_was_confirmed_too_late(
    tmp_path: Path,
) -> None:
    """The deadline is a measured fact, not an inference from the child's exit code.

    The loop can leave normally when the child closes both pipes and exits in the same
    iteration, with the deadline already behind it; nothing used to re-read the clock after
    that. The injected clock jumps forward only once the wait has confirmed the exit -- the one
    deterministic way to reach that race -- so the child genuinely exits zero and the deadline
    genuinely passed.
    """
    observation = ProcessObservation()

    def clock() -> float:
        return time.monotonic() + (600.0 if observation.child_exit_code is not None else 0.0)

    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded_process(
            [sys.executable, "-c", "print('clean')"],
            cwd=tmp_path,
            timeout_s=30,
            observation=observation,
            clock=clock,
        )

    assert observation.child_exit_code == 0, "the child really did exit zero"
    assert observation.deadline_exceeded is True
    assert observation.timed_out is True


def test_local_sandbox_never_reports_a_clean_success_for_a_child_that_outlived_its_deadline(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    run = LocalSubprocessSandbox().run(
        ["python", "-c", "import time; time.sleep(30)"],
        workspace=workspace,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=0.4,
    )

    assert run.exit_code == 124
    termination = run.termination
    assert termination is not None
    assert termination.outcome == "timed_out"
    assert termination.timed_out is True
    assert termination.deadline_exceeded is True
    assert termination.deadline_s == pytest.approx(0.4)
    assert termination.duration_s is not None
    assert termination.duration_s >= 0.4
    assert termination.reap in {"quiesced", "unavailable"}
    reap_deadline = termination.reap_deadline_s
    reap_duration = termination.reap_duration_s
    assert reap_deadline == pytest.approx(1.0)
    assert reap_duration is not None
    assert reap_deadline is not None
    assert reap_duration <= reap_deadline + 0.25


def test_tool_completed_records_the_termination_evidence(tmp_path: Path) -> None:
    """The evidence is durable before the caller can report success (G.3)."""
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    context = replace(
        review_gated_context(
            tmp_path,
            run_id=run_id,
            log=JsonlEventLog(events_path, run_id),
            engine=PolicyEngine(development_policy()),
        ),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    registry = configured_builtins_registry(
        source={
            "THYMIRA_SANDBOX_BACKEND": "local",
            "THYMIRA_SANDBOX_MODE": "danger_full_access",
        }
    )
    manager = ToolManager(registry)

    execution = manager.execute(
        context,
        "run_python",
        {"code": "print('hello')", "description": "Record termination evidence."},
    )

    assert execution.result.sandbox_termination is not None
    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed = JsonlEventLog(events_path, run_id).events()
    completed = next(
        event
        for event in replayed
        if event.type is EventType.TOOL_COMPLETED and event.payload.get("tool") == "run_python"
    )
    recorded = completed.payload["sandbox_termination"]
    assert recorded is not None
    assert recorded["control_channel"] == "os_wait"
    assert recorded["timed_out"] is False
    assert recorded["deadline_exceeded"] is False
    assert isinstance(recorded["duration_s"], float)
    assert completed.payload["sandbox_enforcement"] == SandboxEnforcement.PARTIAL.value


__all__: list[str] = []
