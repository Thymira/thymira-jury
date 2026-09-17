"""Unit tests for the Docker container lifecycle and its evidence classification."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox import ContainerSandbox
from thymira.tools.sandbox import container as container_module
from thymira.tools.sandbox import container_lifecycle as lifecycle_module
from thymira.tools.sandbox.process_capture import OutputLimitExceeded

if TYPE_CHECKING:
    from pathlib import Path


def _document(
    *,
    exit_code: int = 0,
    started: bool = True,
    error: str = "",
    network: str = "none",
    memory: int = 1073741824,
    cpus_nano: int = 1_000_000_000,
    pids_limit: int = 128,
    read_only: bool = True,
    destination: str = "/workspace",
    read_write: bool = True,
) -> str:
    """Build one inspect document with every knob the probe reads exposed."""
    return json.dumps(
        {
            "State": {
                "Status": "exited" if started else "created",
                "Running": False,
                "OOMKilled": False,
                "ExitCode": exit_code,
                "Error": error,
                "StartedAt": "2026-01-01T00:00:00Z" if started else "0001-01-01T00:00:00Z",
                "FinishedAt": "2026-01-01T00:00:01Z" if started else "0001-01-01T00:00:00Z",
            },
            "HostConfig": {
                "NetworkMode": network,
                "Memory": memory,
                "NanoCpus": cpus_nano,
                "PidsLimit": pids_limit,
                "ReadonlyRootfs": read_only,
            },
            "Mounts": [{"Destination": destination, "RW": read_write}],
        }
    )


def _state(*, exit_code: int, started: bool = True, error: str = "") -> str:
    """Build the whole ``docker inspect`` document, the way the daemon returns it.

    The runtime asks for ``{{json .}}`` rather than ``{{json .State}}``: the same call that
    confirms the exit also carries the live profile of the container that produced it (F6.4).
    """
    return _document(exit_code=exit_code, started=started, error=error)


@dataclass
class _Docker:
    start_code: int = 0
    state: str = field(default_factory=lambda: _state(exit_code=0))
    create_code: int = 0
    inspect_code: int = 0
    remove_code: int = 0
    timeout_on: str | None = None
    overflow_on: str | None = None
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        action = argv[1]
        if action == self.overflow_on:
            raise OutputLimitExceeded(stdout="bounded output", stderr="", limit_bytes=8)
        if action == self.timeout_on:
            raise subprocess.TimeoutExpired(argv, timeout=1, output="partial", stderr="late")
        if action == "create":
            return subprocess.CompletedProcess(argv, self.create_code, "container-id\n", "create")
        if action == "start":
            return subprocess.CompletedProcess(argv, self.start_code, "child-output", "child-error")
        if action == "inspect":
            return subprocess.CompletedProcess(
                argv, self.inspect_code, self.state, "inspect failed"
            )
        if action == "rm":
            return subprocess.CompletedProcess(argv, self.remove_code, "", "remove failed")
        raise AssertionError(f"unexpected docker action: {action}")


class _Clock:
    """Deterministic monotonic clock for separating execution from cleanup time."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current test time."""
        return self.now


class _SlowCleanupDocker(_Docker):
    """Docker double whose child finishes promptly while removal is slow."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self.clock = clock

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Advance the clock only for child execution and cleanup stages."""
        result = super().run(argv, **kwargs)
        if argv[1] == "start":
            self.clock.now += 0.9
        elif argv[1] == "rm":
            self.clock.now += 0.5
        return result


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker: _Docker,
    *,
    mode: SandboxMode = SandboxMode.WORKSPACE_WRITE,
):
    monkeypatch.setattr(container_module.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(lifecycle_module, "run_bounded_process", docker.run)
    return ContainerSandbox(runtime="runsc").run(
        ["python", "-c", "raise SystemExit(125)"],
        workspace=tmp_path,
        mode=mode,
        timeout_s=5,
        env={"THYMIRA_TEST": "secret-value"},
    )


def test_confirmed_child_exit_125_is_partial_not_runtime_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(start_code=125, state=_state(exit_code=125))

    result = _run(tmp_path, monkeypatch, docker)

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert result.stdout == "child-output"
    create = docker.calls[0]
    assert create[:2] == ["docker", "create"]
    assert create[create.index("--pull") + 1] == "never"
    assert create[create.index("--entrypoint") + 1] == ""
    assert create[create.index("--log-driver") + 1] == "none"
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--runtime") + 1] == "runsc"
    assert create[create.index("--env") + 1] == "THYMIRA_TEST=secret-value"
    assert create[-4:] == ["thymira:dev", "python", "-c", "raise SystemExit(125)"]
    assert [call[1] for call in docker.calls] == ["create", "start", "inspect", "rm"]


def test_container_env_points_matplotlib_config_at_the_writable_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MPLCONFIGDIR must point at writable storage: the container's rootfs is --read-only."""
    docker = _Docker(start_code=0, state=_state(exit_code=0))

    _run(tmp_path, monkeypatch, docker)

    create = docker.calls[0]
    env_values = [create[i + 1] for i, arg in enumerate(create) if arg == "--env"]
    assert "MPLCONFIGDIR=/tmp" in env_values  # the container's own per-run tmpfs
    assert "TMPDIR=/tmp" in env_values  # same private storage, same reasoning


def test_create_refusal_is_unusable_and_still_force_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(create_code=1)

    result = _run(tmp_path, monkeypatch, docker)

    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert result.exit_code == 125
    assert "just docker-build" in result.stderr
    assert [call[1] for call in docker.calls] == ["create", "rm"]


def test_unconfirmed_start_exit_one_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(start_code=1, state=_state(exit_code=1, started=False, error="daemon stopped"))

    result = _run(tmp_path, monkeypatch, docker)

    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert "daemon stopped" in result.stderr


def test_attach_exit_must_match_inspected_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        _Docker(start_code=1, state=_state(exit_code=0)),
    )

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.UNUSABLE


@pytest.mark.parametrize(
    "docker",
    [_Docker(state="not-json"), _Docker(inspect_code=1)],
    ids=["malformed-state", "inspect-refused"],
)
def test_unverifiable_inspection_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker: _Docker
) -> None:
    result = _run(tmp_path, monkeypatch, docker)

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.UNUSABLE


def test_confirmed_child_exit_one_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(
        tmp_path,
        monkeypatch,
        _Docker(start_code=1, state=_state(exit_code=1)),
    )

    assert result.exit_code == 1
    assert result.enforcement is SandboxEnforcement.PARTIAL


def test_cleanup_failure_keeps_the_confirmed_exit_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6.6: cleanup and exit outcome are two separate facts.

    A cleanup failure never rewrites an already-confirmed successful execution to UNUSABLE/125.
    """
    result = _run(tmp_path, monkeypatch, _Docker(remove_code=1))

    assert result.exit_code == 0
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert result.cleanup_confirmed is False
    assert "remove failed" in result.stderr
    assert "container cleanup failed" in result.stderr


def test_cleanup_timeout_keeps_the_confirmed_exit_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(tmp_path, monkeypatch, _Docker(timeout_on="rm"))

    assert result.exit_code == 0
    assert result.enforcement is SandboxEnforcement.PARTIAL
    assert result.cleanup_confirmed is False


def test_execution_duration_excludes_slow_container_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow bounded removal cannot make a child appear to exceed its execution deadline."""
    clock = _Clock()
    docker = _SlowCleanupDocker(clock)
    monkeypatch.setattr(container_module.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(lifecycle_module, "time", clock)
    monkeypatch.setattr(lifecycle_module, "run_bounded_process", docker.run)

    result = ContainerSandbox().run(
        ["python", "-c", "print(1)"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=1.0,
    )

    assert result.exit_code == 0
    assert result.cleanup_confirmed is True
    assert result.termination is not None
    assert result.termination.duration_s == pytest.approx(0.9)
    assert result.termination.deadline_exceeded is False
    assert result.termination.timed_out is False


def test_start_timeout_force_removes_and_reports_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(timeout_on="start")

    result = _run(tmp_path, monkeypatch, docker)

    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert [call[1] for call in docker.calls] == ["create", "start", "rm"]
    assert docker.calls[-1][:3] == ["docker", "rm", "--force"]
    assert "secret-value" not in result.stderr


def test_start_output_overflow_force_removes_and_reports_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(overflow_on="start")

    result = _run(tmp_path, monkeypatch, docker)

    assert result.exit_code == 125
    assert result.enforcement is SandboxEnforcement.UNUSABLE
    assert result.stdout == "bounded output"
    assert "output exceeded 8 bytes" in result.stderr
    assert [call[1] for call in docker.calls] == ["create", "start", "rm"]


def test_windows_and_root_hosts_use_nonroot_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker()
    monkeypatch.setattr(container_module.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(container_module.os, "getgid", lambda: 0, raising=False)

    _run(tmp_path, monkeypatch, docker)

    create = docker.calls[0]
    assert create[create.index("--user") + 1] == "999:999"


def test_nonroot_posix_identity_and_custom_seccomp_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker()
    monkeypatch.setattr(container_module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(container_module.os, "getgid", lambda: 1001, raising=False)
    monkeypatch.setattr(container_module.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(lifecycle_module, "run_bounded_process", docker.run)

    ContainerSandbox(seccomp_profile="/profiles/strict.json").run(
        ["python", "-c", "pass"],
        workspace=tmp_path,
        mode=SandboxMode.READ_ONLY,
        timeout_s=5,
    )

    create = docker.calls[0]
    assert create[create.index("--user") + 1] == "1000:1001"
    assert "seccomp=/profiles/strict.json" in create
    assert create[create.index("--volume") + 1].endswith(":/workspace:ro")


def test_unexpected_start_failure_still_force_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker()
    original = docker.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1] == "start":
            docker.calls.append(argv)
            raise AssertionError("unexpected driver defect")
        return original(argv, **kwargs)

    monkeypatch.setattr(container_module.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(lifecycle_module, "run_bounded_process", run)

    with pytest.raises(AssertionError, match="driver defect"):
        ContainerSandbox().run(
            ["python"],
            workspace=tmp_path,
            mode=SandboxMode.READ_ONLY,
            timeout_s=5,
        )

    assert docker.calls[-1][:3] == ["docker", "rm", "--force"]


def test_invalid_mode_and_unconfined_seccomp_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"seccomp.*unconfined"):
        ContainerSandbox(seccomp_profile="unconfined")
    with pytest.raises(TypeError, match="SandboxMode"):
        ContainerSandbox().run(
            ["python"], workspace=tmp_path, mode=cast("Any", "read_only"), timeout_s=1
        )


@pytest.mark.parametrize("limit", [0, -1, True])
def test_container_sandbox_rejects_an_invalid_output_limit(limit: int) -> None:
    with pytest.raises((TypeError, ValueError), match="output_limit_bytes"):
        ContainerSandbox(output_limit_bytes=limit)


@pytest.mark.parametrize("value", ["0", "0.0", "0.0000000001", "1" * 129])
def test_container_sandbox_rejects_an_unusable_cpu_limit(value: str) -> None:
    with pytest.raises(ValueError, match="cpus"):
        ContainerSandbox(cpus=value)


def test_container_sandbox_accepts_nanocpu_precision_boundary() -> None:
    sandbox = ContainerSandbox(cpus="0.000000001")

    assert sandbox.cpus == "0.000000001"


def test_container_sandbox_dispatches_a_requested_quota_to_the_volume_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quota request selects the capability-proven volume path instead of a bind path."""
    called: dict[str, object] = {}
    monkeypatch.setattr(container_module.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        container_module,
        "run_quota_workspace",
        lambda docker, **kwargs: called.update(docker=docker, **kwargs) or "quota-result",
    )

    result = ContainerSandbox(workspace_quota_bytes=4096).run(
        ["python", "-c", "print('reachable')"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5,
    )

    assert result == "quota-result"
    assert called["docker"] == "docker"
    assert called["requested_bytes"] == 4096
    assert called["workspace"] == tmp_path.resolve()
    assert called["argv"] == ["python", "-c", "print('reachable')"]


def test_the_live_inspection_is_recorded_as_a_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6.4: backend state is confirmed by a live profile, not by the request object."""
    result = _run(tmp_path, monkeypatch, _Docker(state=_document()))

    assert result.exit_code == 0
    assert result.termination is not None
    probe = result.termination.probe
    assert probe is not None
    assert probe["network"] == "none"
    assert probe["memory_bytes"] == 1073741824
    assert probe["cpus_nano"] == 1_000_000_000
    assert probe["pids_limit"] == 128
    assert probe["read_only_rootfs"] is True
    assert probe["mounts"] == [{"destination": "/workspace", "read_write": True}]
    assert probe["oom_killed"] is False


def test_a_probe_that_disagrees_with_the_specification_is_recorded_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer records what the daemon said; judging the disagreement is MIRA's job."""
    result = _run(tmp_path, monkeypatch, _Docker(state=_document(network="bridge")))

    assert result.spec is not None
    assert result.spec.network == "none"
    assert result.termination is not None
    assert result.termination.probe is not None
    assert result.termination.probe["network"] == "bridge"


def test_a_malformed_probe_is_absent_rather_than_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = json.dumps(
        {"State": json.loads(_document())["State"], "HostConfig": "not-a-mapping"}
    )

    result = _run(tmp_path, monkeypatch, _Docker(state=document))

    assert result.exit_code == 0
    assert result.termination is not None
    assert result.termination.probe is None


def test_the_inspection_asks_the_daemon_for_the_whole_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(state=_document())

    _run(tmp_path, monkeypatch, docker)

    inspect = next(call for call in docker.calls if call[1] == "inspect")
    assert inspect[inspect.index("--format") + 1] == "{{json .}}"
