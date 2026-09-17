"""Real Docker proof of the local-driver tmpfs quota boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.thymira.docker_support import require_quota_sandbox_image
from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import JsonlEventLog, verify_log
from thymira.mira.checks import AuditContext, ControlStatus, audit_run
from thymira.schemas import EventType, SandboxEnforcement, SandboxMode, ToolCallStatus, new_id
from thymira.tools import ToolManager
from thymira.tools.builtins import configured_builtins_registry
from thymira.tools.sandbox import ContainerSandbox

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.integration
@pytest.mark.slow
def test_real_tmpfs_quota_returns_bounded_writeback_after_enospc(tmp_path: Path) -> None:
    """The worker reaches ENOSPC while the host receives only a bounded validated tree."""
    image = require_quota_sandbox_image()
    sandbox = ContainerSandbox(
        image=image,
        memory="1g",
        cpus="1.0",
        pids_limit=128,
        workspace_quota_bytes=1024 * 1024,
    )
    code = "\n".join(
        [
            "from pathlib import Path",
            "p = Path('/workspace/bounded.bin')",
            "p.write_bytes(b'a' * 65536)",
            "try:",
            "    Path('/workspace/overrun.bin').write_bytes(b'b' * 2097152)",
            "except OSError as exc:",
            "    print(type(exc).__name__)",
        ]
    )
    result = sandbox.run(
        [
            "python",
            "-c",
            code,
        ],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=30,
    )

    assert result.exit_code == 0
    assert result.enforcement is SandboxEnforcement.FULL
    assert result.tool_succeeded
    assert (tmp_path / "bounded.bin").stat().st_size == 65536
    assert (
        not (tmp_path / "overrun.bin").exists()
        or (tmp_path / "overrun.bin").stat().st_size <= 1024 * 1024
    )
    assert result.quota_evidence is not None
    assert result.quota_evidence.observed_filesystem == "tmpfs"
    assert result.quota_evidence.observed_capacity_bytes is not None
    assert result.quota_evidence.observed_capacity_bytes <= 1024 * 1024
    assert result.quota_evidence.worker_mount_inspected
    assert result.quota_evidence.publication == "committed"


@pytest.mark.integration
@pytest.mark.slow
def test_real_tmpfs_quota_can_write_inside_directory_from_previous_call(tmp_path: Path) -> None:
    """A staged directory remains writable to the next capability-dropped worker."""
    image = require_quota_sandbox_image()
    sandbox = ContainerSandbox(
        image=image,
        workspace_quota_bytes=1024 * 1024,
    )
    first = sandbox.run(
        [
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "Path('/workspace/results').mkdir(); "
                "Path('/workspace/results/first.txt').write_text('first')"
            ),
        ],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=30,
    )
    second = sandbox.run(
        [
            "python",
            "-c",
            "from pathlib import Path; Path('/workspace/results/second.txt').write_text('second')",
        ],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=30,
    )

    assert first.tool_succeeded, first.stderr
    assert second.tool_succeeded, second.stderr
    assert (tmp_path / "results" / "second.txt").read_text(encoding="utf-8") == "second"


@pytest.mark.integration
@pytest.mark.slow
def test_real_tmpfs_quota_honors_read_only_worker_mount(tmp_path: Path) -> None:
    """The quota backend keeps a requested read-only worker mount read-only."""
    image = require_quota_sandbox_image()
    result = ContainerSandbox(
        image=image,
        workspace_quota_bytes=1024 * 1024,
    ).run(
        ["python", "-c", "from pathlib import Path; Path('/workspace/blocked').write_text('x')"],
        workspace=tmp_path,
        mode=SandboxMode.READ_ONLY,
        timeout_s=30,
    )

    assert result.exit_code != 0
    assert result.enforcement is SandboxEnforcement.FULL
    assert not result.tool_succeeded
    assert not (tmp_path / "blocked").exists()
    assert result.spec is not None
    assert result.spec.workspace_mount.endswith(":ro")
    assert result.quota_evidence is not None
    assert result.quota_evidence.worker_mount_inspected
    assert result.quota_evidence.publication == "committed"


@pytest.mark.integration
@pytest.mark.slow
def test_real_tmpfs_quota_honors_explicit_danger_network_mode(tmp_path: Path) -> None:
    """The explicit danger mode keeps its requested network and writable volume facts."""
    image = require_quota_sandbox_image()
    result = ContainerSandbox(
        image=image,
        workspace_quota_bytes=1024 * 1024,
    ).run(
        ["python", "-c", "from pathlib import Path; Path('/workspace/danger').write_text('x')"],
        workspace=tmp_path,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=30,
    )

    assert result.exit_code == 0, result.stderr
    assert result.tool_succeeded
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert (tmp_path / "danger").read_text(encoding="utf-8") == "x"
    assert result.spec is not None
    assert result.spec.network == "bridge"
    assert result.spec.workspace_mount.endswith(":rw")
    assert result.quota_evidence is not None
    assert result.quota_evidence.worker_mount_inspected
    assert result.quota_evidence.publication == "committed"


@pytest.mark.integration
@pytest.mark.slow
def test_production_run_python_quota_reopens_and_mira_replays_evidence(tmp_path: Path) -> None:
    """The configured Tool Manager publishes quota evidence for a fresh MIRA replay."""
    image = require_quota_sandbox_image()
    run_id = new_id("run")
    events_path = tmp_path / "events.jsonl"
    context = human_approved_context(
        tmp_path,
        run_id=run_id,
        log=JsonlEventLog(events_path, run_id),
    )
    # The runtime receives an already allocated project workspace; keep the empty workspace
    # explicit here so quota refusal can be checked without relying on input staging to create it.
    context.workspace.mkdir(parents=True, exist_ok=True)
    registry = configured_builtins_registry(
        source={
            "THYMIRA_SANDBOX_BACKEND": "container",
            "THYMIRA_SANDBOX_IMAGE": image,
            "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES": str(1024 * 1024),
        }
    )
    execution = ToolManager(registry).execute(
        context,
        "run_python",
        {
            "code": (
                "from pathlib import Path\n"
                "Path('/workspace/manager.bin').write_bytes(b'x' * 65536)\n"
                "try:\n"
                "    Path('/workspace/too-large.bin').write_bytes(b'y' * 2097152)\n"
                "except OSError as exc:\n"
                "    print(type(exc).__name__)\n"
            ),
            "description": "Exercise the production quota consumer.",
        },
    )
    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.stderr
    assert execution.call.sandbox_enforcement is SandboxEnforcement.FULL
    observed = execution.result.stdout.strip()
    assert "OSError" in observed or "ENOSPC" in observed
    assert (context.workspace / "manager.bin").stat().st_size == 65536

    verification = verify_log(events_path)
    assert verification.valid, verification.error
    replayed = JsonlEventLog(events_path, run_id).events()
    completed = next(
        event
        for event in replayed
        if event.type is EventType.TOOL_COMPLETED and event.payload.get("tool") == "run_python"
    )
    evidence = completed.payload["sandbox_quota_evidence"]
    assert evidence["publication"] == "committed"
    assert evidence["observed_filesystem"] == "tmpfs"
    report = audit_run(AuditContext(run_id, replayed))
    a19 = next(control for control in report.controls if control.control_id == "A19")
    assert a19.status is ControlStatus.PASSED, a19.detail
    a32 = next(control for control in report.controls if control.control_id == "A32")
    assert a32.status is ControlStatus.PASSED, a32.detail
    a30 = next(control for control in report.controls if control.control_id == "A30")
    assert a30.status is ControlStatus.PASSED, a30.detail
