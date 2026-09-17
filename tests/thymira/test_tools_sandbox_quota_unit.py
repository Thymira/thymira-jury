"""Unit coverage for quota command identity, writeback, and independent cleanup facts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox import StagedInput, quota_workspace, workspace_helper
from thymira.tools.sandbox.container_lifecycle import ContainerOutcome
from thymira.tools.sandbox.termination import (
    CONTROL_CHANNEL_CONTAINER_STATE,
    OUTCOME_COMPLETED,
    REAP_UNBOUNDED,
    TREE_SCOPE_CONTAINER,
    TerminationEvidence,
)


def test_quota_runner_stages_exports_and_keeps_worker_without_host_rw_bind(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")
    fake_volume = tmp_path.parent / "fake-volume"
    fake_volume.mkdir()
    state: dict[str, Path] = {}
    worker_command: list[str] = []
    calls: list[list[str]] = []

    def command(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["volume", "create"]:
            return subprocess.CompletedProcess(argv, 0, "thymira-quota-ok\n", "")
        if argv[1] == "create" and any("thymira-quota-helper-" in argument for argument in argv):
            export_mount = next(argument for argument in argv if ":/export:" in argument)
            state["export"] = Path(export_mount.split(":/export:", 1)[0])
            input_mount = next((argument for argument in argv if ":/inputs:" in argument), None)
            if input_mount:
                state["inputs"] = Path(input_mount.split(":/inputs:", 1)[0])
            return subprocess.CompletedProcess(argv, 0, "helper-id\n", "")
        if argv[1] == "start":
            return subprocess.CompletedProcess(argv, 0, "helper-id\n", "")
        if argv[1] == "exec" and "stage" in argv:
            result = workspace_helper.stage(
                tmp_path,
                fake_volume,
                inputs=state.get("inputs"),
                requested=4096,
                max_entries=100_000,
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
        if argv[1] == "exec" and "export" in argv:
            result = workspace_helper.export(
                fake_volume,
                state["export"],
                inputs=state.get("inputs"),
                requested=4096,
                max_entries=100_000,
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(quota_workspace, "_run_command", command)
    monkeypatch.setattr(quota_workspace, "_inspect_volume", lambda *args, **kwargs: object())
    monkeypatch.setattr(quota_workspace, "_worker_inspection", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        quota_workspace,
        "_remove_container",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(quota_workspace, "_cleanup", lambda *args, **kwargs: (True, True, ""))
    monkeypatch.setattr(
        quota_workspace,
        "run_container_lifecycle",
        lambda *args, **kwargs: ContainerOutcome(
            stdout="child",
            stderr="",
            exit_code=0,
            child_confirmed=True,
            cleanup_confirmed=False,
            termination=TerminationEvidence(
                outcome=OUTCOME_COMPLETED,
                exit_source=CONTROL_CHANNEL_CONTAINER_STATE,
                control_channel=CONTROL_CHANNEL_CONTAINER_STATE,
                control_channel_validated=True,
                child_exit_code=0,
                reap=REAP_UNBOUNDED,
                tree_scope=TREE_SCOPE_CONTAINER,
                probe={
                    "network": "none",
                    "memory_bytes": 1024**3,
                    "cpus_nano": 1_000_000_000,
                    "pids_limit": 128,
                    "read_only_rootfs": True,
                    "mounts": [{"destination": "/workspace", "read_write": True}],
                    "oom_killed": False,
                },
            ),
        ),
    )
    monkeypatch.setattr(
        workspace_helper,
        "_probe",
        lambda _path, _requested: {
            "capacity_bytes": 4096,
            "free_bytes": 4096,
            "inode_capacity": 128,
            "inode_free": 128,
            "filesystem": "tmpfs",
        },
    )

    def worker_side_effect(*_args: Any, **_kwargs: Any) -> ContainerOutcome:
        worker_command.extend(_args[1])
        (fake_volume / "output.txt").write_text("output", encoding="utf-8")
        return ContainerOutcome(
            stdout="child",
            stderr="",
            exit_code=0,
            child_confirmed=True,
            cleanup_confirmed=False,
            termination=TerminationEvidence(
                outcome=OUTCOME_COMPLETED,
                exit_source=CONTROL_CHANNEL_CONTAINER_STATE,
                control_channel=CONTROL_CHANNEL_CONTAINER_STATE,
                control_channel_validated=True,
                child_exit_code=0,
                reap=REAP_UNBOUNDED,
                tree_scope=TREE_SCOPE_CONTAINER,
                probe={
                    "network": "none",
                    "memory_bytes": 1024**3,
                    "cpus_nano": 1_000_000_000,
                    "pids_limit": 128,
                    "read_only_rootfs": True,
                    "mounts": [{"destination": "/workspace", "read_write": True}],
                    "oom_killed": False,
                },
            ),
        )

    monkeypatch.setattr(quota_workspace, "run_container_lifecycle", worker_side_effect)
    result = quota_workspace.run_quota_workspace(
        "docker",
        image="thymira:dev",
        workspace=tmp_path,
        argv=["python", "-c", "print('ok')"],
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5,
        env=None,
        memory="1g",
        cpus="1.0",
        pids_limit=128,
        runtime=None,
        requested_bytes=4096,
        staged_inputs=(StagedInput(".thymira/script.py", b"print('staged')"),),
    )

    assert result.tool_succeeded
    assert result.enforcement is SandboxEnforcement.FULL
    assert result.quota_evidence is not None
    assert result.quota_evidence.publication == "committed"
    assert result.quota_evidence.cleanup == {
        "helper": True,
        "worker": True,
        "volume": True,
        "staging": True,
        "inputs": True,
    }
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "output"
    worker_create = worker_command
    assert any(
        ":/workspace:rw" in argument and argument.startswith("thymira-quota-")
        for argument in worker_create
    )
    assert all(
        "/source" not in argument and "/export" not in argument for argument in worker_create
    )


def test_quota_runner_records_oversize_manifest_before_private_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An input manifest over capacity is charged and refused before Docker starts."""

    def unexpected_docker(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an overlarge staged manifest must refuse before Docker")

    monkeypatch.setattr(quota_workspace, "_run_command", unexpected_docker)
    result = quota_workspace.run_quota_workspace(
        "docker",
        image="thymira:dev",
        workspace=tmp_path,
        argv=["python", "-c", "print('unreachable')"],
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5,
        env=None,
        memory="1g",
        cpus="1.0",
        pids_limit=128,
        runtime=None,
        requested_bytes=1024,
        staged_inputs=(StagedInput(".thymira/script.py", b"x" * 2048),),
    )

    assert result.exit_code == 125
    assert result.enforcement.value == "unusable"
    assert result.quota_evidence is not None
    assert result.quota_evidence.staged_input_logical_bytes == 2048
    assert result.quota_evidence.staged_input_entries == 2
    assert result.quota_evidence.publication == "failed"
    assert result.quota_evidence.cleanup["inputs"] is True
    assert not (tmp_path / ".thymira").exists()
