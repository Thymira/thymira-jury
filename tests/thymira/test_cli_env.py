"""The CLI honours the same `.env` the server does, without importing any runtime member."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from thymira.cli.env import ENV_FILE_ENV_VAR, load_env_file

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_DEMO = "THYMIRA_DEMO_CLI_VALUE"


@pytest.fixture(autouse=True)
def _clean_environment() -> Iterator[None]:
    """`load_dotenv` writes to `os.environ` directly, so monkeypatch cannot undo it for us."""
    os.environ.pop(_DEMO, None)
    os.environ.pop(ENV_FILE_ENV_VAR, None)
    yield
    os.environ.pop(_DEMO, None)
    os.environ.pop(ENV_FILE_ENV_VAR, None)


def test_the_nearest_env_file_reaches_the_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{_DEMO}=from-file\n", encoding="utf-8", newline="\n")
    nested = tmp_path / "a"
    nested.mkdir()

    assert load_env_file(start=nested) == tmp_path / ".env"
    assert os.environ[_DEMO] == "from-file"


def test_the_real_environment_always_wins(tmp_path: Path) -> None:
    os.environ[_DEMO] = "from-shell"
    (tmp_path / ".env").write_text(f"{_DEMO}=from-file\n", encoding="utf-8", newline="\n")

    load_env_file(start=tmp_path)

    assert os.environ[_DEMO] == "from-shell"


def test_a_missing_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the CLI does may fail because a convenience file is absent."""
    monkeypatch.setattr("thymira.cli.env._find_upwards", lambda _start: None)

    assert load_env_file() is None


def test_a_named_file_that_is_absent_is_skipped_rather_than_raised(tmp_path: Path) -> None:
    """Unlike the server, the CLI has no `--env-file` to contradict, so it stays quiet."""
    os.environ[ENV_FILE_ENV_VAR] = str(tmp_path / "absent.env")

    assert load_env_file(start=tmp_path) is None


def test_cli_env_file_skips_bootstrap_variables_and_keeps_the_api_url(tmp_path: Path) -> None:
    """The CLI applies the same refusal without failing the command: refused keys are skipped."""
    (tmp_path / ".env").write_text(
        "LD_PRELOAD=/tmp/evil.so\nHTTPS_PROXY=http://attacker.invalid\nTHYMIRA_API_URL=http://x\n",
        encoding="utf-8",
        newline="\n",
    )
    os.environ.pop("LD_PRELOAD", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("THYMIRA_API_URL", None)

    try:
        assert load_env_file(start=tmp_path) == tmp_path / ".env"
        assert "LD_PRELOAD" not in os.environ
        assert "HTTPS_PROXY" not in os.environ
        assert os.environ["THYMIRA_API_URL"] == "http://x"
    finally:
        os.environ.pop("LD_PRELOAD", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("THYMIRA_API_URL", None)


def test_cli_env_file_skips_sandbox_variables(tmp_path: Path) -> None:
    """The CLI applies the same sandbox-kill-switch refusal, skipping rather than failing."""
    (tmp_path / ".env").write_text(
        "THYMIRA_SANDBOX_BACKEND=local\nTHYMIRA_API_URL=http://x\n",
        encoding="utf-8",
        newline="\n",
    )
    os.environ.pop("THYMIRA_SANDBOX_BACKEND", None)
    os.environ.pop("THYMIRA_API_URL", None)

    try:
        assert load_env_file(start=tmp_path) == tmp_path / ".env"
        assert "THYMIRA_SANDBOX_BACKEND" not in os.environ
        assert os.environ["THYMIRA_API_URL"] == "http://x"
    finally:
        os.environ.pop("THYMIRA_SANDBOX_BACKEND", None)
        os.environ.pop("THYMIRA_API_URL", None)


def test_cli_bootstrap_blocklist_matches_the_runtime_boundary() -> None:
    """A mechanical gate on the deliberate duplication the CLI's layering rule forces (F12.4)."""
    from thymira.cli.env import (
        _BOOTSTRAP_ENVIRONMENT_NAMES,
        _BOOTSTRAP_ENVIRONMENT_SUFFIXES,
    )
    from thymira.tools.sandbox import (
        BOOTSTRAP_ENVIRONMENT_NAMES,
        BOOTSTRAP_ENVIRONMENT_SUFFIXES,
    )

    assert set(_BOOTSTRAP_ENVIRONMENT_NAMES) == set(BOOTSTRAP_ENVIRONMENT_NAMES)
    assert set(_BOOTSTRAP_ENVIRONMENT_SUFFIXES) == set(BOOTSTRAP_ENVIRONMENT_SUFFIXES)


def test_importing_a_cli_module_does_not_touch_the_environment(tmp_path: Path) -> None:
    """Only `main` loads the file; importing a command must not inherit a stray `.env`."""
    import importlib

    (tmp_path / ".env").write_text(f"{_DEMO}=leaked\n", encoding="utf-8", newline="\n")

    importlib.reload(importlib.import_module("thymira.cli.__main__"))

    assert _DEMO not in os.environ
