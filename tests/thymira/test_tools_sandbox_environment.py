"""Environment scrubbing and the resolved execution specification both backends record (F6.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from thymira.schemas import SandboxEnforcement, SandboxMode
from thymira.tools.sandbox import ContainerSandbox, LocalSubprocessSandbox
from thymira.tools.sandbox.environment import scrub_environment

_CREDENTIAL_NAMES = (
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "aws_secret_access_key",
    "GITHUB_TOKEN",
    "DB_PASSWORD",
    "DB_PASSWD",
    "SOME_CREDENTIAL",
)

_BOOTSTRAP_NAMES = (
    "PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "HTTPS_PROXY",
    "no_proxy",
)

_CONTAINER_GIT_NAMES = ("GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS")


def test_scrub_environment_drops_credential_shaped_names() -> None:
    env = dict.fromkeys(_CREDENTIAL_NAMES, "secret-value")
    env["HARMLESS"] = "kept"

    scrubbed = scrub_environment(env)

    assert scrubbed.values == {"HARMLESS": "kept"}
    assert set(scrubbed.excluded) == set(_CREDENTIAL_NAMES)
    assert "secret-value" not in scrubbed.values.values()


def test_scrub_environment_matches_source_classifier_for_concatenated_names() -> None:
    """Credential families stay excluded while configuration suffixes remain usable."""
    env = {
        "OPENAI_APIKEY": "secret-value",
        "AWSACCESSKEY": "secret-value",
        "CLIENTSECRET": "secret-value",
        "THYMIRA_MAX_TOKENS": "128",
        "TOKEN_LIMIT": "128",
        "KEY_PATH": r"C:\secrets\provider.key",
    }

    scrubbed = scrub_environment(env)

    assert set(scrubbed.excluded) == {"OPENAI_APIKEY", "AWSACCESSKEY", "CLIENTSECRET"}
    assert scrubbed.values == {
        "THYMIRA_MAX_TOKENS": "128",
        "TOKEN_LIMIT": "128",
        "KEY_PATH": r"C:\secrets\provider.key",
    }


def test_scrub_environment_keeps_credential_fragment_configuration_names() -> None:
    """The shared classifier distinguishes source names from non-secret configuration bounds."""
    scrubbed = scrub_environment(
        {
            "THYMIRA_KEY_PATH": "C:/secrets/provider.key",
            "TOKEN_LIMIT": "128",
            "SAFE_FLAG": "1",
        }
    )

    assert scrubbed.values == {
        "THYMIRA_KEY_PATH": "C:/secrets/provider.key",
        "TOKEN_LIMIT": "128",
        "SAFE_FLAG": "1",
    }
    assert scrubbed.excluded == ()


def test_scrub_environment_drops_bootstrap_variables() -> None:
    env = dict.fromkeys(_BOOTSTRAP_NAMES, "injected")
    env["HARMLESS"] = "kept"

    scrubbed = scrub_environment(env)

    assert scrubbed.values == {"HARMLESS": "kept"}
    assert set(scrubbed.excluded) == set(_BOOTSTRAP_NAMES)


def test_scrub_environment_keeps_the_declared_container_git_variables() -> None:
    env = dict.fromkeys(_CONTAINER_GIT_NAMES, "1")

    scrubbed = scrub_environment(env)

    assert set(scrubbed.values) == set(_CONTAINER_GIT_NAMES)
    assert scrubbed.excluded == ()


def test_container_sandbox_records_the_resolved_specification_without_credential_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Docker: `shutil.which` is stubbed, and the docker-missing refusal still records it."""
    monkeypatch.setattr("thymira.tools.sandbox.container.shutil.which", lambda _name: None)
    sandbox = ContainerSandbox(image="thymira:dev", memory="512m", cpus="0.5", pids_limit=32)

    run = sandbox.run(
        ["python", "-c", "print(1)"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5,
        env={"OPENAI_API_KEY": "sk-super-secret", "SAFE_FLAG": "1"},
    )

    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert run.spec is not None
    spec = run.spec
    assert spec.backend == "container"
    assert spec.image == "thymira:dev"
    assert spec.memory == "512m"
    assert spec.cpus == "0.5"
    assert spec.pids_limit == 32
    assert spec.output_limit_bytes == 8 * 1024 * 1024
    assert spec.workspace_quota_bytes is None
    assert spec.network == "none"
    assert spec.workspace_mount.endswith(":/workspace:rw")
    # The runtime owns the child's temporary storage and matplotlib config directory, so the
    # names it sets are part of the environment the child sees and are recorded as such (F6.8).
    assert spec.environment_names == ("MPLCONFIGDIR", "SAFE_FLAG", "TEMP", "TMP", "TMPDIR")
    assert spec.excluded_environment_names == ("OPENAI_API_KEY",)
    assert "workspace_quota" in spec.unenforced
    payload = spec.as_payload()
    assert "sk-super-secret" not in str(payload)
    assert "sk-super-secret" not in run.stdout
    assert "sk-super-secret" not in run.stderr


def test_local_sandbox_records_an_unenforced_specification_when_it_refuses_a_confined_mode(
    tmp_path: Path,
) -> None:
    sandbox = LocalSubprocessSandbox()

    run = sandbox.run(
        ["python", "-c", "print(1)"],
        workspace=tmp_path,
        mode=SandboxMode.WORKSPACE_WRITE,
        timeout_s=5,
    )

    assert run.enforcement is SandboxEnforcement.UNUSABLE
    assert run.spec is not None
    assert run.spec.backend == "local_subprocess"
    assert run.spec.network == "host"
    assert set(run.spec.unenforced) == {
        "cpu",
        "filesystem",
        "memory",
        "network",
        "pids",
        "rlimits",
        "workspace_quota",
    }


@pytest.mark.integration
def test_local_sandbox_keeps_the_runtime_owned_path_over_a_caller_value(tmp_path: Path) -> None:
    sandbox = LocalSubprocessSandbox()
    interpreter_dir = str(Path(sys.executable).absolute().parent)

    run = sandbox.run(
        [sys.executable, "-c", "import os; print(os.environ.get('PATH', ''))"],
        workspace=tmp_path,
        mode=SandboxMode.DANGER_FULL_ACCESS,
        timeout_s=10,
        env={"PATH": "/an/attacker/path"},
    )

    assert run.exit_code == 0
    assert "/an/attacker/path" not in run.stdout
    assert interpreter_dir in run.stdout


def test_scrub_environment_drops_pythonuserbase() -> None:
    """PYTHONUSERBASE is the same import-injection lever as PYTHONPATH (site.py user-site)."""
    env = {"PYTHONUSERBASE": "injected-user-base", "HARMLESS": "kept"}

    scrubbed = scrub_environment(env)

    assert scrubbed.values == {"HARMLESS": "kept"}
    assert "PYTHONUSERBASE" in scrubbed.excluded
