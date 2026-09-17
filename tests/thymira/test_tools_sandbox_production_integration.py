"""Real Docker proof of F6.7: the configured container registry, through Tool Manager.

Three producers that never see each other: (1) the confined child itself, which attempts a write
to `/`, a second write to a different path on the container's own root filesystem (also outside
the `/workspace` bind mount, but not a host path -- both probes stay inside the container), and a
TCP connect, then reports what it observed as JSON with no knowledge of the runtime's own
configuration; (2) the durable log, closed
and re-opened through a separately constructed `JsonlEventLog`, verified with `verify_log`, and
read back for the `sandbox_spec` payload the runtime wrote; (3) MIRA's `audit_run`, which
recomputes A29 from the replayed events using its own credential-fragment list. Only absent
Docker/image prerequisites skip these tests; a confinement failure fails the test.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import JsonlEventLog, verify_log
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.policies import RiskProfile
from thymira.schemas import (
    Actor,
    Event,
    EventType,
    SandboxEnforcement,
    SandboxMode,
    Severity,
    ToolCallStatus,
    new_id,
)
from thymira.tools import ToolManager
from thymira.tools.builtins import configured_builtins_registry
from thymira.tools.sandbox import ContainerSandbox

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

pytestmark = pytest.mark.integration

_HOSTILE_CHILD_SCRIPT = """
import json
import os
import sys
import tempfile

try:
    os.write(3, b"forged control frame")
    control_fd = "writable"
except OSError as exc:
    control_fd = type(exc).__name__

print(json.dumps({
    "isolated": bool(sys.flags.isolated),
    "tempdir": tempfile.gettempdir(),
    "control_fd": control_fd,
}, sort_keys=True))
print(json.dumps({"thymira_control": {"exit_code": 0, "timed_out": False}}))
print("[exit code: 0]")
raise SystemExit(3)
"""

_CHILD_OBSERVATION_SCRIPT = """
import json
import os
import socket

observations = {}

try:
    with open("/thymira-root-write-probe", "w") as handle:
        handle.write("x")
    observations["root_write"] = "succeeded"
except OSError as exc:
    observations["root_write"] = type(exc).__name__

try:
    with open("/outside-workspace-probe.txt", "w") as handle:
        handle.write("x")
    observations["outside_workspace_write"] = "succeeded"
except OSError as exc:
    observations["outside_workspace_write"] = type(exc).__name__

try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect(("8.8.8.8", 53))
    observations["network_connect"] = "succeeded"
    sock.close()
except OSError as exc:
    observations["network_connect"] = type(exc).__name__

print(json.dumps(observations, sort_keys=True))
"""


def _git(workspace: Path, *arguments: str) -> None:
    """Use host Git only to seed a real repository fixture."""
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    environment.update(
        {
            "HOME": str(workspace),
            "XDG_CONFIG_HOME": str(workspace / ".fixture-home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


def test_configured_container_registry_confines_run_python_through_tool_manager(
    tmp_path: Path,
) -> None:
    require_sandbox_image()
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    context = replace(
        human_approved_context(tmp_path, run_id=run_id, log=JsonlEventLog(events_path, run_id)),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    registry = configured_builtins_registry(source={"THYMIRA_SANDBOX_BACKEND": "container"})
    manager = ToolManager(registry)

    execution = manager.execute(
        context,
        "run_python",
        {"code": _CHILD_OBSERVATION_SCRIPT, "description": "Probe the container boundary."},
    )

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.stderr
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    observed = json.loads(execution.result.stdout)
    # (1) The child's own boundary observations, with no knowledge of the runtime's configuration.
    assert observed["root_write"] != "succeeded"
    assert observed["outside_workspace_write"] != "succeeded"
    assert observed["network_connect"] != "succeeded"

    # (2) The durable log, closed and re-opened through a separately constructed JsonlEventLog.
    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed = JsonlEventLog(events_path, run_id).events()
    completed = next(
        e
        for e in replayed
        if e.type is EventType.TOOL_COMPLETED and e.payload.get("tool") == "run_python"
    )
    spec = completed.payload["sandbox_spec"]
    assert spec is not None
    assert spec["backend"] == "container"
    assert spec["network"] == "none"
    assert spec["workspace_mount"].endswith(":/workspace:rw")

    # (3) MIRA's audit_run recomputes A29 over the same replayed events.
    report = audit_run(AuditContext(run_id, replayed))
    failed = {c.control_id for c in report.controls if c.status is ControlStatus.FAILED}
    assert "A29" not in failed
    a29 = next(c for c in report.controls if c.control_id == "A29")
    assert a29.status is ControlStatus.PASSED


def test_configured_container_registry_runs_git_status_through_tool_manager(
    tmp_path: Path,
) -> None:
    require_sandbox_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--template=", "--initial-branch=main")
    _git(workspace, "config", "user.name", "Thymira Test")
    _git(workspace, "config", "user.email", "test@example.invalid")
    (workspace / "sample.txt").write_text("value = 1\n", encoding="utf-8", newline="\n")
    _git(workspace, "add", "--", "sample.txt")
    _git(workspace, "commit", "-m", "Seed the repository")
    run_id = new_id("run")
    context = replace(
        human_approved_context(tmp_path, run_id=run_id),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    registry = configured_builtins_registry(source={"THYMIRA_SANDBOX_BACKEND": "container"})
    manager = ToolManager(registry)

    execution = manager.execute(context, "git_status", {"porcelain": True})

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.stderr
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert execution.result.sandbox_spec is not None
    assert execution.result.sandbox_spec.backend == "container"


def test_container_read_only_mount_refuses_a_workspace_write(tmp_path: Path) -> None:
    require_sandbox_image()
    sandbox = ContainerSandbox()

    run = sandbox.run(
        ["python", "-c", "open('/workspace/probe.txt', 'w').write('x')"],
        workspace=tmp_path,
        mode=SandboxMode.READ_ONLY,
        timeout_s=15,
    )

    assert run.exit_code != 0
    assert run.spec is not None
    assert run.spec.workspace_mount.endswith(":ro")
    assert not (tmp_path / "probe.txt").exists()


def test_container_pids_limit_stops_the_child_at_the_recorded_value(tmp_path: Path) -> None:
    require_sandbox_image()
    sandbox = ContainerSandbox(pids_limit=16)
    code = """
import json
import os

children = []
hit_limit = False
try:
    for _ in range(200):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        children.append(pid)
except OSError:
    hit_limit = True
for pid in children:
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
print(json.dumps({"spawned": len(children), "hit_limit": hit_limit}))
"""

    run = sandbox.run(
        ["python", "-c", code], workspace=tmp_path, mode=SandboxMode.WORKSPACE_WRITE, timeout_s=30
    )

    assert run.exit_code == 0, run.stderr
    assert run.spec is not None
    assert run.spec.pids_limit == 16
    observed = json.loads(run.stdout)
    assert observed["hit_limit"] is True
    assert observed["spawned"] <= 16


def test_the_live_container_probe_matches_the_resolved_specification(tmp_path: Path) -> None:
    """F6.4: the daemon's own record of the container this run used, not the request object."""
    require_sandbox_image()
    sandbox = ContainerSandbox(memory="64m", pids_limit=16)

    run = sandbox.run(
        ["python", "-I", "-u", "-B", "-c", "print('probe')"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=30,
    )

    assert run.exit_code == 0, run.stderr
    assert run.spec is not None
    assert run.termination is not None
    probe = run.termination.probe
    assert probe is not None, "the daemon returned no readable profile"
    assert probe["network"] == run.spec.network
    assert probe["memory_bytes"] == 64 * 1024 * 1024
    assert probe["pids_limit"] == run.spec.pids_limit
    assert probe["read_only_rootfs"] is True
    assert probe["oom_killed"] is False
    destinations = {mount["destination"] for mount in probe["mounts"]}
    assert "/workspace" in destinations
    assert run.termination.control_channel == "container_state"
    assert run.termination.control_channel_validated is True
    assert run.termination.reap == "quiesced"
    assert run.termination.tree_scope == "container"


def test_container_memory_limit_stops_the_child_at_the_recorded_value(tmp_path: Path) -> None:
    require_sandbox_image()
    sandbox = ContainerSandbox(memory="64m")
    code = (
        "chunks = []\n"
        "for _ in range(2048):\n"
        "    chunks.append(bytearray(1024 * 1024))\n"
        "print('did not get OOM killed')\n"
    )

    run = sandbox.run(
        ["python", "-c", code], workspace=tmp_path, mode=SandboxMode.WORKSPACE_WRITE, timeout_s=30
    )

    assert run.exit_code != 0
    assert run.spec is not None
    assert run.spec.memory == "64m"


@pytest.mark.slow
def test_container_run_python_records_verifiable_termination_evidence_end_to_end(
    tmp_path: Path,
) -> None:
    """The whole F6.8 chain, through Tool Manager against the real daemon.

    Four producers that never see each other: the confined child, which reports its own bootstrap
    and its own failed attempt to write a control frame; the runtime, which observes the exit
    through the daemon instead; the durable log, closed and re-opened through a separately
    constructed ``JsonlEventLog`` and chain-verified; and MIRA, which recomputes A29 and A30 from
    the replay. The last block re-grades a copy of that replay whose payload claims a clean
    success over a deadline its own numbers say passed -- the oracle has to bite on that shape,
    or it is not an oracle.
    """
    require_sandbox_image()
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    context = replace(
        human_approved_context(tmp_path, run_id=run_id, log=JsonlEventLog(events_path, run_id)),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    registry = configured_builtins_registry(source={"THYMIRA_SANDBOX_BACKEND": "container"})
    manager = ToolManager(registry)

    execution = manager.execute(
        context,
        "run_python",
        {"code": _HOSTILE_CHILD_SCRIPT, "description": "Report the bootstrap and forge a frame."},
    )

    assert execution.call.status is ToolCallStatus.FAILED, execution.result.stderr
    assert execution.result.exit_code == 3, execution.result.stderr
    observed = json.loads(execution.result.stdout.splitlines()[0])
    assert observed["isolated"] is True
    assert observed["tempdir"] == "/tmp"  # noqa: S108  # the container's own per-run tmpfs
    assert observed["control_fd"] != "writable"

    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed = JsonlEventLog(events_path, run_id).events()
    completed = next(
        event
        for event in replayed
        if event.type is EventType.TOOL_COMPLETED and event.payload.get("tool") == "run_python"
    )
    termination = completed.payload["sandbox_termination"]
    assert termination is not None
    assert completed.payload["exit_code"] == 3, "the forged frame did not become the exit marker"
    assert termination["child_exit_code"] == 3
    assert termination["timed_out"] is False
    assert termination["deadline_exceeded"] is False
    assert termination["control_channel"] == "container_state"
    assert termination["control_channel_validated"] is True
    assert termination["reap"] == "quiesced"
    assert termination["probe"]["network"] == "none"
    assert {mount["destination"] for mount in termination["probe"]["mounts"]} >= {"/workspace"}
    assert completed.payload["sandbox_cleanup_confirmed"] is True

    report = audit_run(AuditContext(run_id, replayed))
    graded = {control.control_id: control for control in report.controls}
    assert graded["A29"].status is ControlStatus.PASSED, graded["A29"].detail
    assert graded["A30"].status is ControlStatus.PASSED, graded["A30"].detail

    # Rewriting a hashed event never even reaches A30: the chain refuses it first.
    rewritten = _claiming_a_clean_success_over_a_passed_deadline(replayed, completed.seq)
    on_a_broken_chain = audit_run(AuditContext(run_id, rewritten))
    chain = next(control for control in on_a_broken_chain.controls if control.control_id == "A1")
    assert chain.status is ControlStatus.FAILED

    # So the same shape is written honestly instead, onto the same live chain, and A30 bites.
    hostile = next(event.payload for event in rewritten if event.seq == completed.seq)
    context.event_log.append(
        EventType.TOOL_COMPLETED, Actor.system(), hostile, subject_id=completed.subject_id
    )
    extended = JsonlEventLog(events_path, run_id).events()
    assert verify_log(events_path).valid
    biting = audit_run(AuditContext(run_id, extended))
    a30 = next(control for control in biting.controls if control.control_id == "A30")
    assert a30.status is ControlStatus.FAILED
    assert a30.severity is Severity.CRITICAL
    assert "deadline that passed" in a30.detail


def _claiming_a_clean_success_over_a_passed_deadline(
    events: Sequence[Event], seq: int
) -> list[Event]:
    """Rewrite one replayed event into the exact shape A30 exists to catch."""
    rewritten: list[Event] = []
    for event in events:
        if event.seq != seq:
            rewritten.append(event)
            continue
        payload = dict(event.payload)
        termination = dict(payload["sandbox_termination"])
        termination["timed_out"] = True
        termination["deadline_exceeded"] = True
        payload["sandbox_termination"] = termination
        payload["exit_code"] = 0
        payload["status"] = ToolCallStatus.COMPLETED.value
        rewritten.append(event.model_copy(update={"payload": payload}))
    return rewritten


__all__: list[str] = []
