"""Runtime-owned sandbox backend selection, read from the process environment (F6.7)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from thymira.schemas import SandboxMode
from thymira.tools.builtins.configured import configured_builtins_registry
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox
from thymira.tools.sandbox.settings import (
    SandboxConfigurationError,
    SandboxSettings,
    build_sandbox,
)

_SUBPROCESS_TOOLS = frozenset(
    {
        "run_python",
        "run_experiment",
        "inspect_model",
        "audit_model",
        "git_status",
        "git_diff",
        "git_log",
        "git_commit",
        "git_worktree_create",
        "git_worktree_list",
        "git_worktree_remove",
    }
)


def test_settings_default_to_the_container_backend() -> None:
    settings = SandboxSettings.from_env({})

    assert settings.backend == "container"
    assert settings.image == "thymira:dev"
    assert settings.memory == "1g"
    assert settings.cpus == "1.0"
    assert settings.pids_limit == 128
    assert settings.output_limit_bytes == 8 * 1024 * 1024
    assert settings.workspace_quota_bytes is None
    assert settings.mode is None


def test_settings_refuse_an_unknown_backend() -> None:
    with pytest.raises(SandboxConfigurationError, match="chroot"):
        SandboxSettings.from_env({"THYMIRA_SANDBOX_BACKEND": "chroot"})


def test_settings_refuse_a_nonpositive_pids_limit() -> None:
    with pytest.raises(SandboxConfigurationError, match="positive"):
        SandboxSettings.from_env({"THYMIRA_SANDBOX_PIDS_LIMIT": "0"})


def test_settings_parse_output_and_workspace_limits_as_runtime_owned_values() -> None:
    settings = SandboxSettings.from_env(
        {
            "THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES": "4096",
            "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES": "8192",
        }
    )

    assert settings.output_limit_bytes == 4096
    assert settings.workspace_quota_bytes == 8192


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES", "0"),
        ("THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES", "not-an-integer"),
        ("THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES", "0"),
        ("THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES", "not-an-integer"),
    ],
)
def test_settings_refuse_invalid_disk_or_output_limits(name: str, value: str) -> None:
    with pytest.raises(SandboxConfigurationError, match=name):
        SandboxSettings.from_env({name: value})


def test_settings_refuse_an_unknown_sandbox_mode() -> None:
    with pytest.raises(SandboxConfigurationError, match="THYMIRA_SANDBOX_MODE"):
        SandboxSettings.from_env({"THYMIRA_SANDBOX_MODE": "godmode"})


def test_settings_refuse_an_image_or_memory_value_that_is_not_argv_safe() -> None:
    settings = SandboxSettings.from_env({"THYMIRA_SANDBOX_IMAGE": "--privileged"})

    with pytest.raises(SandboxConfigurationError):
        build_sandbox(settings)

    for name in ("THYMIRA_SANDBOX_MEMORY", "THYMIRA_SANDBOX_CPUS"):
        settings = SandboxSettings.from_env({name: "1" * 129})
        with pytest.raises(SandboxConfigurationError):
            build_sandbox(settings)

    for value in ("0", "0.0"):
        settings = SandboxSettings.from_env({"THYMIRA_SANDBOX_CPUS": value})
        with pytest.raises(SandboxConfigurationError):
            build_sandbox(settings)

    settings = SandboxSettings.from_env({"THYMIRA_SANDBOX_MEMORY": "1g; rm -rf /"})

    with pytest.raises(SandboxConfigurationError):
        build_sandbox(settings)


def test_build_sandbox_applies_the_configured_container_limits() -> None:
    settings = SandboxSettings.from_env(
        {
            "THYMIRA_SANDBOX_BACKEND": "container",
            "THYMIRA_SANDBOX_IMAGE": "thymira:custom",
            "THYMIRA_SANDBOX_MEMORY": "2g",
            "THYMIRA_SANDBOX_CPUS": "2.0",
            "THYMIRA_SANDBOX_PIDS_LIMIT": "64",
        }
    )

    sandbox = build_sandbox(settings)

    assert isinstance(sandbox, ContainerSandbox)
    assert sandbox.image == "thymira:custom"
    assert sandbox.memory == "2g"
    assert sandbox.cpus == "2.0"
    assert sandbox.pids_limit == 64


def test_build_sandbox_applies_output_and_workspace_limits_without_claiming_quota() -> None:
    settings = SandboxSettings.from_env(
        {
            "THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES": "4096",
            "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES": "8192",
        }
    )

    sandbox = build_sandbox(settings)

    assert isinstance(sandbox, ContainerSandbox)
    assert sandbox.output_limit_bytes == 4096
    assert sandbox.workspace_quota_bytes == 8192


def test_configured_registry_selects_the_container_backend_from_the_environment() -> None:
    registry = configured_builtins_registry(source={})

    subprocess_tools = [tool for tool in registry if tool.name in _SUBPROCESS_TOOLS]
    assert subprocess_tools
    for tool in subprocess_tools:
        sandbox = cast("Any", tool).sandbox
        assert isinstance(sandbox, ContainerSandbox)
        assert sandbox.image == "thymira:dev"


def test_configured_registry_selects_the_local_development_backend_and_mode() -> None:
    registry = configured_builtins_registry(
        source={
            "THYMIRA_SANDBOX_BACKEND": "local",
            "THYMIRA_SANDBOX_MODE": "danger_full_access",
        }
    )

    subprocess_tools = [tool for tool in registry if tool.name in _SUBPROCESS_TOOLS]
    assert subprocess_tools
    for tool in subprocess_tools:
        assert isinstance(cast("Any", tool).sandbox, LocalSubprocessSandbox)
        assert tool.capability.sandbox_mode is SandboxMode.DANGER_FULL_ACCESS
