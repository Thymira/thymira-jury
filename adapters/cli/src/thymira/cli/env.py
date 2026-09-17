"""Load a `.env` file into the CLI's environment at start-up.

The CLI reads `THYMIRA_API_URL` from the environment, and `.env.example` is where a project
records it, so `thymira` honours the same file the server does.

This deliberately repeats `thymira.api.env` rather than importing it. `thymira.cli` may not
import any runtime member — the "clients own no state" contract is machine-checked by
import-linter, and `adapters/cli` declares no `thymira-*` dependency at all — so this file
duplicates both the loader and the bootstrap-name blocklist rather than importing
`thymira.tools.sandbox.is_bootstrap_environment_name`; `tests/thymira/test_cli_env.py` pins the
two lists equal. The CLI needs only the simple case (no `--env-file` flag) and never raises on a
refused key (it spawns no child a bootstrap variable could steer, so a stray line is silently
skipped rather than failing the command), which is why the two loaders are not the same function.
A `THYMIRA_SANDBOX_*` line is skipped for a different reason: the CLI talks to the API over HTTP
and never selects a sandbox backend itself, but a project `.env` still must not be able to steer
whichever process reads this loader's environment next.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

__all__ = ["ENV_FILE_ENV_VAR", "load_env_file"]

ENV_FILE_ENV_VAR = "THYMIRA_ENV_FILE"

_BOOTSTRAP_ENVIRONMENT_NAMES = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "NODE_OPTIONS",
    "PYTHONUSERBASE",
)
_BOOTSTRAP_ENVIRONMENT_SUFFIXES = ("_PROXY",)

_SANDBOX_ENV_VAR_PREFIX = "THYMIRA_SANDBOX_"
"""A duplicate of ``thymira.api.env``'s same-named constant, kept for the layering reason in the
module docstring: a project ``.env`` may not choose or weaken the sandbox backend."""


def _is_bootstrap_name(name: str) -> bool:
    """Return whether ``name`` selects an interpreter, preloaded library or proxy, case-folded.

    A literal duplicate of ``thymira.tools.sandbox.is_bootstrap_environment_name`` (see the module
    docstring); ``test_cli_bootstrap_blocklist_matches_the_runtime_boundary`` keeps the two lists
    equal by construction.
    """
    folded = name.strip().upper()
    return folded in _BOOTSTRAP_ENVIRONMENT_NAMES or folded.endswith(
        _BOOTSTRAP_ENVIRONMENT_SUFFIXES
    )


def load_env_file(*, start: Path | None = None) -> Path | None:
    """Load `$THYMIRA_ENV_FILE`, or the nearest `.env` at or above the working directory.

    A variable already present in the environment is never overridden, a bootstrap or proxy
    start-up name is skipped rather than applied, and a missing file is the normal case rather
    than an error, so this can run unconditionally before any command.
    """
    named = os.environ.get(ENV_FILE_ENV_VAR)
    candidate = Path(named).expanduser() if named else _find_upwards(start or Path.cwd())
    if candidate is None or not candidate.is_file():
        return None
    try:
        values = dotenv_values(candidate)
    except OSError:
        return None
    for key, value in values.items():
        if (
            value is None
            or _is_bootstrap_name(key)
            or key.startswith(_SANDBOX_ENV_VAR_PREFIX)
            or key in os.environ
        ):
            continue
        os.environ[key] = value
    return candidate


def _find_upwards(start: Path) -> Path | None:
    """Return the nearest `.env` at or above `start`, or None."""
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None
