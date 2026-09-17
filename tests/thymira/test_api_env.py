"""Loading `.env` at start-up: the deployment always wins, and only entry points may load it."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from tests.thymira.api_support import TEST_CREDENTIAL
from thymira.api.env import ENV_FILE_ENV_VAR, EnvFileError, load_env_file

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_DEMO = "THYMIRA_DEMO_ENV_VALUE"


@pytest.fixture(autouse=True)
def _clean_environment() -> Iterator[None]:
    """`load_dotenv` writes to `os.environ` directly, so monkeypatch cannot undo it for us."""
    os.environ.pop(_DEMO, None)
    os.environ.pop(ENV_FILE_ENV_VAR, None)
    yield
    os.environ.pop(_DEMO, None)
    os.environ.pop(ENV_FILE_ENV_VAR, None)


def _write(path: Path, value: str) -> Path:
    path.write_text(f"{_DEMO}={value}\n", encoding="utf-8", newline="\n")
    return path


def test_a_named_file_reaches_the_environment(tmp_path: Path) -> None:
    env_file = _write(tmp_path / ".env", "from-file")

    loaded = load_env_file(env_file)

    assert loaded == env_file
    assert os.environ[_DEMO] == "from-file"


def test_the_real_environment_always_wins(tmp_path: Path) -> None:
    """A shell export, a container's `-e` and a CI secret all outrank the file on disk."""
    os.environ[_DEMO] = "from-shell"
    env_file = _write(tmp_path / ".env", "from-file")

    load_env_file(env_file)

    assert os.environ[_DEMO] == "from-shell"


def test_naming_a_file_that_is_not_there_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError, match="not found"):
        load_env_file(tmp_path / "absent.env")


def test_finding_no_file_at_all_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Having no `.env` is the normal case in CI and in a container.

    The search is stubbed rather than pointed at a temp directory: `--basetemp` puts `tmp_path`
    inside the repository, whose own `.env` the upward walk would legitimately find.
    """
    monkeypatch.setattr("thymira.api.env._find_upwards", lambda _start: None)

    assert load_env_file() is None


def test_the_env_file_variable_names_the_file(tmp_path: Path) -> None:
    env_file = _write(tmp_path / "deployment.env", "named")
    os.environ[ENV_FILE_ENV_VAR] = str(env_file)

    assert load_env_file() == env_file
    assert os.environ[_DEMO] == "named"


def test_a_named_file_that_is_missing_is_not_quietly_replaced_by_a_search(tmp_path: Path) -> None:
    """Falling back to a nearby `.env` would load a file the operator did not ask for."""
    _write(tmp_path / ".env", "discovered")
    os.environ[ENV_FILE_ENV_VAR] = str(tmp_path / "absent.env")

    with pytest.raises(EnvFileError, match="not found"):
        load_env_file(start=tmp_path)

    assert _DEMO not in os.environ


def test_the_search_walks_upward_so_the_server_starts_from_anywhere(tmp_path: Path) -> None:
    env_file = _write(tmp_path / ".env", "above")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert load_env_file(start=nested) == env_file
    assert os.environ[_DEMO] == "above"


def test_env_file_refuses_a_path_or_preload_variable(tmp_path: Path) -> None:
    """F6.2: a project `.env` cannot control PATH, preload or interpreter start-up variables."""
    env_file = tmp_path / ".env"
    env_file.write_text("LD_PRELOAD=/tmp/evil.so\n", encoding="utf-8", newline="\n")

    with pytest.raises(EnvFileError, match="LD_PRELOAD"):
        load_env_file(env_file)

    assert "LD_PRELOAD" not in os.environ


def test_env_file_refuses_a_proxy_startup_variable(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HTTPS_PROXY=http://attacker.invalid:8080\n", encoding="utf-8", newline="\n"
    )

    with pytest.raises(EnvFileError, match="HTTPS_PROXY"):
        load_env_file(env_file)

    assert "HTTPS_PROXY" not in os.environ


def test_env_file_still_loads_credentials_and_thymira_variables(tmp_path: Path) -> None:
    """The blocklist did not break the loader's actual job."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-test\nTHYMIRA_MODEL=gpt-test\n", encoding="utf-8", newline="\n"
    )
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("THYMIRA_MODEL", None)

    try:
        assert load_env_file(env_file) == env_file
        assert os.environ["OPENAI_API_KEY"] == "sk-test"
        assert os.environ["THYMIRA_MODEL"] == "gpt-test"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("THYMIRA_MODEL", None)


def test_building_an_app_never_reads_an_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory is a library seam: a test that builds an app must not inherit a stray `.env`.

    Only `thymira.api.server.main` loads one. If `create_app` or `build_default_deps` ever did,
    every test in this suite would start depending on the developer's own working tree.
    """
    from thymira.api import build_default_deps, create_app

    workspace = tmp_path / "workspace"
    (workspace / ".thymira").mkdir(parents=True)
    (workspace / ".thymira" / "config.yaml").write_text(
        "project:\n  name: demo\n  domain: credit_risk\n", encoding="utf-8", newline="\n"
    )
    _write(workspace / ".env", "leaked")
    monkeypatch.chdir(workspace)

    create_app(
        build_default_deps(
            tmp_path / "runtime", workspace=workspace, principal_resolver=TEST_CREDENTIAL
        )
    )

    assert _DEMO not in os.environ


def test_env_file_refuses_pythonuserbase(tmp_path: Path) -> None:
    """PYTHONUSERBASE steers CPython's user site-packages the same way PYTHONPATH does."""
    env_file = tmp_path / ".env"
    env_file.write_text("PYTHONUSERBASE=/tmp/evil\n", encoding="utf-8", newline="\n")

    with pytest.raises(EnvFileError, match="PYTHONUSERBASE"):
        load_env_file(env_file)

    assert "PYTHONUSERBASE" not in os.environ


def test_env_file_refuses_a_sandbox_backend_variable(tmp_path: Path) -> None:
    """F6.2: a project `.env` may not switch the sandbox backend or mode off."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "THYMIRA_SANDBOX_BACKEND=local\nTHYMIRA_SANDBOX_MODE=danger_full_access\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(EnvFileError, match="THYMIRA_SANDBOX_"):
        load_env_file(env_file)

    assert "THYMIRA_SANDBOX_BACKEND" not in os.environ
    assert "THYMIRA_SANDBOX_MODE" not in os.environ
