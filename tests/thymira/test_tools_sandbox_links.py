"""A link-shaped path inside the workspace cannot make a sandboxed write land outside it.

Windows symlink creation needs a privilege this environment does not grant, so the host-side
nodes use a **junction** (``mklink /J``), which any user may create and which the Win32 API
resolves exactly as a reparse point. On POSIX the same nodes use a symlink. The container node
needs no host privilege at all: the confined child creates the link itself and writes through it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import tool_invocation
from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.builtins import RunPython
from thymira.tools.builtins.subprocess_command import workspace_relative_path
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox

if TYPE_CHECKING:
    from thymira.tools.models import ToolResult

pytestmark = pytest.mark.integration


def _link_directory(link: Path, target: Path) -> None:
    """Create a directory link, skipping when the platform refuses to make one."""
    if os.name == "nt":
        result = subprocess.run(  # explicit argv, fixture setup only
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode != 0:
            pytest.skip(f"this host cannot create a junction: {result.stderr.strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover  # only on a host that forbids symlinks
        pytest.skip(f"this host cannot create a symlink: {exc}")


def test_run_python_refuses_a_link_shaped_staging_directory(tmp_path: Path) -> None:
    """``.thymira`` planted as a link to an outside directory is a fail-closed refusal."""
    outside = tmp_path / "outside"
    outside.mkdir()
    invocation = tool_invocation(tmp_path)
    workspace = Path(invocation.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    _link_directory(workspace / ".thymira", outside)

    result: ToolResult = RunPython(
        sandbox=LocalSubprocessSandbox(), mode=SandboxMode.DANGER_FULL_ACCESS
    ).execute(invocation, {"code": "print('escaped')", "description": "Escape through a link."})

    assert result.success is False
    assert result.exit_code == 125
    assert result.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert result.error is not None
    assert "link" in result.error
    assert list(outside.iterdir()) == [], "a write landed outside the workspace"


def test_workspace_relative_path_refuses_a_link_component(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _link_directory(workspace / "artifacts", outside)

    with pytest.raises(ValueError, match="link"):
        workspace_relative_path(workspace, workspace / "artifacts" / "model.joblib")


def test_a_plain_workspace_path_is_still_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "reports").mkdir(parents=True)

    assert (
        workspace_relative_path(workspace, workspace / "reports" / "metrics.json")
        == "reports/metrics.json"
    )


def test_a_container_child_cannot_write_outside_the_workspace_through_its_own_link(
    tmp_path: Path,
) -> None:
    """The child plants the link itself, so no host privilege is involved."""
    require_sandbox_image()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = (
        "import json, os\n"
        "os.symlink('/etc', '/workspace/escape')\n"
        "try:\n"
        "    open('/workspace/escape/thymira-probe', 'w').write('x')\n"
        "    outcome = 'succeeded'\n"
        "except OSError as exc:\n"
        "    outcome = type(exc).__name__\n"
        "print(json.dumps({'write_through_link': outcome}))\n"
    )

    run = ContainerSandbox().run(
        ["python", "-I", "-u", "-B", "-c", code],
        workspace=workspace,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=30,
    )

    assert run.exit_code == 0, run.stderr
    assert '"write_through_link": "succeeded"' not in run.stdout
    assert not (workspace / "thymira-probe").exists()


__all__: list[str] = []
