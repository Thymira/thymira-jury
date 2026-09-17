"""Load a `.env` file into the process environment at start-up.

Thymira reads configuration straight from the environment — `THYMIRA_*` for routing,
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` for LiteLLM, `LANGFUSE_*` for tracing, `OTEL_*` for the API's
own spans — but until now nothing put a `.env` file *into* that environment except
`compose.yaml`'s `env_file`. Running `thymira-api` directly therefore ignored the very file
`.env.example` tells you to write, and a missing key looked like an unconfigured feature rather
than an unread file.

Three rules make this safe to do at all:

*The real environment always wins.* Loading never overrides a variable that is already set, so a
shell export, a container's `-e`, a CI secret and compose's own `env_file` all take precedence
over the file on disk. `.env` fills gaps; it never overrules the deployment.

*Only a process entry point may call this.* `thymira.api.server.main` does; `create_app` and
`build_default_deps` deliberately do not. A library factory that reached for a developer's `.env`
would make every test that builds an app depend on whatever happens to be in that developer's
working tree.

*A project `.env` may not set an interpreter bootstrap or proxy start-up variable.* `PATH`,
`PYTHONPATH`, `LD_PRELOAD` and the rest of
:data:`thymira.tools.sandbox.BOOTSTRAP_ENVIRONMENT_NAMES` (plus any `*_PROXY` name) select which
interpreter, shared library or proxy every subprocess the runtime launches actually uses --
letting a checked-in `.env` control one of them would let it steer code execution regardless of
what the sandbox layer otherwise confines. This loader raises rather than silently dropping the
line, because the server has not started yet and there is no Run whose evidence would otherwise
explain why a variable did not take effect. Credential-shaped names (`OPENAI_API_KEY` and friends)
are deliberately *not* filtered here -- a `.env` is exactly where they belong; they are excluded at
the sandbox child boundary instead (`thymira.tools.sandbox.scrub_environment`).

*A project `.env` may not choose or weaken the sandbox backend either.* Any `THYMIRA_SANDBOX_*`
name -- `THYMIRA_SANDBOX_BACKEND`, `THYMIRA_SANDBOX_MODE` and the rest -- is an operator decision
read once at the composition root, never a project-declared fact; a checked-in `.env` setting
`THYMIRA_SANDBOX_MODE=danger_full_access` would otherwise turn every code-executing tool into an
unconfined host subprocess, which is strictly more powerful than the interpreter-bootstrap levers
refused above.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from thymira.tools.sandbox import is_bootstrap_environment_name

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ENV_FILE_ENV_VAR", "EnvFileError", "load_env_file"]

ENV_FILE_ENV_VAR = "THYMIRA_ENV_FILE"
"""Names the file to load, for a deployment whose env file is not a `.env` beside the project."""

_ENV_FILE_NAME = ".env"

_SANDBOX_ENV_VAR_PREFIX = "THYMIRA_SANDBOX_"
"""A project `.env` may not choose or weaken the sandbox backend -- that is an operator decision
read once at the composition root (`configured_builtins_registry`), never a project-declared
fact; without this, a discovered `.env` setting ``THYMIRA_SANDBOX_BACKEND=local`` and
``THYMIRA_SANDBOX_MODE=danger_full_access`` would turn every code-executing tool into an
unconfined host subprocess, which is strictly more powerful than the interpreter-bootstrap levers
refused above."""


class EnvFileError(RuntimeError):
    """A file named explicitly — by argument or by `THYMIRA_ENV_FILE` — could not be read."""


def _apply(values: Mapping[str, str | None], source: Path) -> None:
    """Write ``values`` into `os.environ`, refusing a bootstrap name before writing anything.

    Checked in a first pass so a refusal never leaves an earlier key from the same file already
    applied: the file is refused as a whole, not partially loaded up to the offending line. A key
    already present in `os.environ` is left untouched either way (the real environment wins).
    """
    for key, value in values.items():
        if value is not None and is_bootstrap_environment_name(key):
            raise EnvFileError(f"env file may not set the start-up variable {key}: {source}")
        if value is not None and key.startswith(_SANDBOX_ENV_VAR_PREFIX):
            raise EnvFileError(
                f"env file may not set the sandbox configuration variable {key}: {source}"
            )
    for key, value in values.items():
        if value is None or key in os.environ:
            continue
        os.environ[key] = value


def load_env_file(path: Path | str | None = None, *, start: Path | None = None) -> Path | None:
    """Load an env file into `os.environ` without overriding anything already set.

    Args:
        path: The file to load. When given it must exist: naming a file that is not there is a
            configuration error, not a reason to fall back to searching.
        start: Where the search for a `.env` begins when no file is named; defaults to the
            current working directory. The search walks upward, so the server can be started
            from anywhere inside the project.

    Returns:
        The file that was loaded, or None when no file was named and none was found — an absent
        `.env` is the normal case in CI and in a container, never an error.

    Raises:
        EnvFileError: A file was named explicitly and is missing or unreadable, or the file (named
            or discovered) sets an interpreter bootstrap or proxy start-up variable, or a
            `THYMIRA_SANDBOX_*` variable.
    """
    named = path if path is not None else os.environ.get(ENV_FILE_ENV_VAR) or None
    if named is not None:
        resolved = Path(named).expanduser()
        if not resolved.is_file():
            raise EnvFileError(f"env file not found: {resolved}")
        try:
            values = dotenv_values(resolved)
        except OSError as exc:
            raise EnvFileError(f"env file could not be read: {resolved}") from exc
        _apply(values, resolved)
        return resolved

    found = _find_upwards(start or Path.cwd())
    if found is None:
        return None
    try:
        values = dotenv_values(found)
    except OSError:
        # A discovered file is a convenience, not a contract: an unreadable one is skipped rather
        # than turned into a start-up failure the operator never asked for.
        return None
    _apply(values, found)
    return found


def _find_upwards(start: Path) -> Path | None:
    """Return the nearest `.env` at or above `start`, or None."""
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / _ENV_FILE_NAME
        if candidate.is_file():
            return candidate
    return None
