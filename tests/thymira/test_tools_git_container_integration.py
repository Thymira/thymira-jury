"""Real Git, Docker and Tool Manager evidence; no model or external Git service.

Repository fixtures and host sentinels live under tmp_path. Only absent Docker/image
prerequisites skip these tests; failures in Git or confinement fail the test.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from tests.thymira.docker_support import require_sandbox_image
from tests.thymira.fixtures_tools import human_approved_context
from thymira.events import verify_events
from thymira.mira.checks import AuditContext, audit_run
from thymira.policies import RiskProfile
from thymira.schemas import SandboxEnforcement, SandboxMode, Severity, ToolCallStatus, new_id
from thymira.tools import Tool, ToolContext, ToolManager, ToolRegistry
from thymira.tools.builtins import GitCommit, GitDiff, GitLog, GitStatus
from thymira.tools.sandbox import ContainerSandbox

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _git(workspace: Path, *arguments: str) -> str:
    """Use host Git only as the independent fixture and repository oracle."""
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    environment.update(
        {
            "HOME": str(workspace),
            "XDG_CONFIG_HOME": str(workspace / ".fixture-home"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.hooksPath={workspace / '.empty-hooks'}",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return result.stdout


@pytest.fixture
def repository(tmp_path: Path) -> ToolContext:
    require_sandbox_image()
    context = replace(
        human_approved_context(tmp_path, run_id=new_id("run")),
        risk_profile=RiskProfile(
            risk_level="limited", activity_category="analysis", confidence=1.0
        ),
    )
    workspace = context.workspace
    workspace.mkdir()
    _git(workspace, "init", "--template=", "--initial-branch=main")
    _git(workspace, "config", "user.name", "Thymira Test")
    _git(workspace, "config", "user.email", "test@example.invalid")
    _git(workspace, "config", "core.autocrlf", "false")
    _git(workspace, "config", "commit.gpgsign", "false")
    _git(workspace, "config", "core.hooksPath", ".git/hooks")
    (workspace / "sample.txt").write_text("value = 1\n", encoding="utf-8", newline="\n")
    _git(workspace, "add", "--", "sample.txt")
    _git(workspace, "commit", "-m", "Seed the repository")
    return context


def _snapshot(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("tool_class", "expected"),
    [(GitStatus, "sample.txt"), (GitDiff, "+value = 2"), (GitLog, "Seed the repository")],
)
def test_container_git_reads_preserve_repository_bytes_and_record_enforcement(
    repository: ToolContext, tool_class: type[GitStatus | GitDiff | GitLog], expected: str
) -> None:
    (repository.workspace / "sample.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
    before = _snapshot(repository.workspace)
    tool = tool_class(sandbox=ContainerSandbox(), mode=SandboxMode.READ_ONLY)
    manager = ToolManager(ToolRegistry((cast("Tool", tool),)))

    execution = manager.execute(repository, tool.name)

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.error
    assert expected in execution.result.stdout
    assert execution.call.sandbox_mode is SandboxMode.READ_ONLY
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert _snapshot(repository.workspace) == before
    assert verify_events(repository.event_log.events()).valid
    assert repository.artifact_store.verify() == []
    report = audit_run(
        AuditContext(
            repository.run_id,
            repository.event_log.events(),
            repository.artifact_store,
            repository.gate.engine.policy_sha256,
        )
    )
    findings = [finding for finding in report.findings if finding.control_id == "A19"]
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM


def test_container_git_commit_changes_only_selected_tracked_content(
    repository: ToolContext,
) -> None:
    workspace = repository.workspace
    old_head = _git(workspace, "rev-parse", "HEAD")
    (workspace / "sample.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
    (workspace / "leave-untracked.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Commit selected data",
            "paths": ["sample.txt"],
            "description": "Commit the selected sample file",
        },
    )

    assert execution.call.status is ToolCallStatus.COMPLETED, execution.result.error
    assert _git(workspace, "rev-parse", "HEAD") != old_head
    assert _git(workspace, "show", "HEAD:sample.txt") == "value = 2\n"
    assert _git(workspace, "status", "--porcelain=v1") == "?? leave-untracked.txt\n"
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert verify_events(repository.event_log.events()).valid


def test_container_git_commit_in_read_only_mode_cannot_stage(repository: ToolContext) -> None:
    (repository.workspace / "sample.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    before = _snapshot(repository.workspace)
    tool = GitCommit(sandbox=ContainerSandbox(), mode=SandboxMode.READ_ONLY)
    manager = ToolManager(ToolRegistry((cast("Tool", tool),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {"message": "Must not commit", "description": "Test read-only commit refusal"},
    )

    assert not execution.result.success
    assert execution.call.sandbox_mode is SandboxMode.READ_ONLY
    assert _snapshot(repository.workspace) == before
    assert verify_events(repository.event_log.events()).valid


def test_container_git_commit_without_repository_identity_reports_failure(
    repository: ToolContext,
) -> None:
    workspace = repository.workspace
    _git(workspace, "config", "--unset", "user.name")
    _git(workspace, "config", "--unset", "user.email")
    old_head = _git(workspace, "rev-parse", "HEAD")
    (workspace / "sample.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {
            "message": "No author configured",
            "paths": ["sample.txt"],
            "description": "Commit without configured author",
        },
    )

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.result.exit_code != 0
    assert execution.result.stderr
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert _git(workspace, "rev-parse", "HEAD") == old_head
    assert _git(workspace, "show", ":sample.txt") == "changed\n"
    assert verify_events(repository.event_log.events()).valid


def test_container_git_hook_runs_inside_the_workspace_boundary(repository: ToolContext) -> None:
    workspace = repository.workspace
    sentinel = workspace.parent / "host-only.txt"
    sentinel.write_text("host sentinel\n", encoding="utf-8", newline="\n")
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        "#!/bin/sh\npython .git/check_boundary.py\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    (workspace / ".git" / "check_boundary.py").write_text(
        "from pathlib import Path\n"
        "Path('hook-ran.txt').write_text('container hook')\n"
        "try:\n"
        "    Path('../host-only.txt').write_text('escaped')\n"
        "except OSError:\n"
        "    Path('outside-denied.txt').write_text('denied')\n"
        "else:\n"
        "    raise SystemExit('workspace escape succeeded')\n",
        encoding="utf-8",
        newline="\n",
    )
    (workspace / "sample.txt").write_text("hook test\n", encoding="utf-8", newline="\n")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Exercise confined hook",
            "paths": ["sample.txt"],
            "description": "Run the confined commit hook",
        },
    )

    assert execution.result.success, execution.result.error
    assert (workspace / "hook-ran.txt").read_text(encoding="utf-8") == "container hook"
    assert (workspace / "outside-denied.txt").read_text(encoding="utf-8") == "denied"
    # The child proves its parent write was denied; this unmounted host file is a separate oracle.
    assert sentinel.read_text(encoding="utf-8") == "host sentinel\n"
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL
    assert verify_events(repository.event_log.events()).valid


def test_container_git_textconv_cannot_write_a_read_only_workspace(repository: ToolContext) -> None:
    workspace = repository.workspace
    (workspace / "sample.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
    (workspace / ".gitattributes").write_text(
        "*.txt diff=confined\n", encoding="utf-8", newline="\n"
    )
    _git(workspace, "config", "diff.confined.textconv", "python .git/textconv.py")
    (workspace / ".git" / "textconv.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "try:\n"
        "    Path('forbidden-write.txt').write_text('escaped')\n"
        "except OSError:\n"
        "    print('read-only write denied')\n"
        "else:\n"
        "    raise SystemExit('read-only write succeeded')\n"
        "print(Path(sys.argv[1]).read_text())\n",
        encoding="utf-8",
        newline="\n",
    )
    before = _snapshot(workspace)
    manager = ToolManager(ToolRegistry((cast("Tool", GitDiff(sandbox=ContainerSandbox())),)))

    execution = manager.execute(repository, "git_diff")

    assert execution.result.success, execution.result.error
    assert "read-only write denied" in execution.result.stdout
    assert "+value = 2" in execution.result.stdout
    assert _snapshot(workspace) == before
    assert execution.call.sandbox_enforcement is SandboxEnforcement.PARTIAL


def test_container_git_refuses_a_real_linked_worktree(repository: ToolContext) -> None:
    linked = repository.workspace.parent / "linked"
    _git(repository.workspace, "worktree", "add", "--detach", str(linked))
    before = _snapshot(repository.workspace)
    linked_before = _snapshot(linked)
    context = replace(repository, workspace=linked)
    manager = ToolManager(ToolRegistry((cast("Tool", GitStatus(sandbox=ContainerSandbox())),)))

    execution = manager.execute(context, "git_status")

    assert execution.call.status is ToolCallStatus.FAILED
    assert execution.call.sandbox_enforcement is SandboxEnforcement.UNUSABLE
    assert execution.call.sandbox_mode is SandboxMode.READ_ONLY
    assert execution.result.sandbox_mode is None
    assert "standalone" in (execution.result.error or "")
    assert _snapshot(repository.workspace) == before
    assert _snapshot(linked) == linked_before
    assert verify_events(context.event_log.events()).valid


def test_container_git_commit_preserves_unrelated_staging_after_a_failed_call(
    repository: ToolContext,
) -> None:
    workspace = repository.workspace
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    (workspace / "first.txt").write_text("first call\n", encoding="utf-8", newline="\n")
    (workspace / "second.txt").write_text("second call\n", encoding="utf-8", newline="\n")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))
    failed = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Rejected by hook",
            "paths": ["first.txt"],
            "description": "Test rejection from the commit hook",
        },
    )
    assert not failed.result.success
    staged_before = _git(workspace, "show", ":first.txt")
    hook.unlink()

    committed = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Commit second path",
            "paths": ["second.txt"],
            "description": "Commit the second selected file",
        },
    )

    assert committed.result.success, committed.result.error
    assert _git(workspace, "show", "--name-only", "--format=", "HEAD") == "second.txt\n"
    assert _git(workspace, "diff", "--cached", "--name-only") == "first.txt\n"
    assert _git(workspace, "show", ":first.txt") == staged_before
    assert verify_events(repository.event_log.events()).valid


def test_container_git_commit_does_not_expand_magic_pathspecs(repository: ToolContext) -> None:
    workspace = repository.workspace
    (workspace / "first.txt").write_text("staged\n", encoding="utf-8", newline="\n")
    (workspace / "second.txt").write_text("untracked\n", encoding="utf-8", newline="\n")
    _git(workspace, "add", "--", "first.txt")
    old_head = _git(workspace, "rev-parse", "HEAD")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Literal selection",
            "paths": [":/"],
            "description": "Commit the literal pathspec",
        },
    )

    assert not execution.result.success
    assert _git(workspace, "rev-parse", "HEAD") == old_head
    assert _git(workspace, "diff", "--cached", "--name-only") == "first.txt\n"
    assert _git(workspace, "show", ":first.txt") == "staged\n"
    assert verify_events(repository.event_log.events()).valid


def test_container_git_commit_treats_brackets_in_filenames_literally(
    repository: ToolContext,
) -> None:
    workspace = repository.workspace
    (workspace / "literal[1].txt").write_text("selected\n", encoding="utf-8", newline="\n")
    (workspace / "literal1.txt").write_text("leave untracked\n", encoding="utf-8", newline="\n")
    manager = ToolManager(ToolRegistry((cast("Tool", GitCommit(sandbox=ContainerSandbox())),)))

    execution = manager.execute(
        repository,
        "git_commit",
        {
            "message": "Literal filename",
            "paths": ["literal[1].txt"],
            "description": "Commit the literal filename",
        },
    )

    assert execution.result.success, execution.result.error
    assert _git(workspace, "show", "--name-only", "--format=", "HEAD") == "literal[1].txt\n"
    assert _git(workspace, "show", "HEAD:literal[1].txt") == "selected\n"
    assert _git(workspace, "status", "--porcelain=v1") == "?? literal1.txt\n"
