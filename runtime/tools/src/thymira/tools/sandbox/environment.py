"""Credential and interpreter-bootstrap scrubbing for a sandboxed child's environment.

Both backends scrub a caller-supplied environment through :func:`scrub_environment` before it
reaches a child process: the container before its ``--env`` loop, the local backend before it
updates its clean environment. Credential-shaped names (``*KEY*``, ``*SECRET*``, ``*TOKEN*``,
``*PASSWORD*``, ``*PASSWD*``, ``*CREDENTIAL*``) are excluded at this child boundary rather than
filtered out of a project ``.env`` -- that file is exactly where ``OPENAI_API_KEY`` belongs
(ADR-0013 decision 1). Interpreter-bootstrap and proxy names (``PATH``, ``PYTHONPATH``,
``LD_PRELOAD``, ``*_PROXY``, ...) are excluded here *and* refused outright from a ``.env`` file by
the two entry-point loaders (``thymira.api.env``, ``thymira.cli.env``), because a caller value for
one of them would steer which interpreter, shared library or proxy the tool subprocess actually
uses regardless of the runtime's own configuration.

All comparisons are case-folded, so ``no_proxy`` and ``aws_secret_access_key`` are caught the same
as their upper-case forms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.schemas import is_credential_environment_name as _is_credential_environment_name

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "BOOTSTRAP_ENVIRONMENT_NAMES",
    "BOOTSTRAP_ENVIRONMENT_SUFFIXES",
    "SECRET_NAME_FRAGMENTS",
    "ScrubbedEnvironment",
    "apply_runtime_owned",
    "is_bootstrap_environment_name",
    "is_credential_environment_name",
    "scrub_environment",
]

SECRET_NAME_FRAGMENTS: tuple[str, ...] = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)
"""Case-folded substrings that mark an environment variable name as credential-shaped."""

BOOTSTRAP_ENVIRONMENT_NAMES: tuple[str, ...] = (
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
"""Case-folded names that select an interpreter, a preloaded library or an injected runtime."""

BOOTSTRAP_ENVIRONMENT_SUFFIXES: tuple[str, ...] = ("_PROXY",)
"""Case-folded suffixes for a proxy start-up variable (``HTTPS_PROXY``, ``no_proxy``)."""


def is_bootstrap_environment_name(name: str) -> bool:
    """Return whether ``name`` selects an interpreter, preloaded library or proxy, case-folded."""
    folded = name.strip().upper()
    return folded in BOOTSTRAP_ENVIRONMENT_NAMES or folded.endswith(BOOTSTRAP_ENVIRONMENT_SUFFIXES)


def is_credential_environment_name(name: str) -> bool:
    """Return whether ``name`` looks like it carries a secret, case-folded.

    The source and child-process boundaries share the schemas package's classifier so a name that
    carries a credential cannot be excluded in one path and exposed in another. Its documented
    configuration suffixes (``_PATH``, ``_FILE``, ``_NAME``, ``_COUNT``, ``_LIMIT`` and
    ``_TOKENS``) remain available to child processes as configuration facts.
    """
    return _is_credential_environment_name(name)


@dataclass(frozen=True, slots=True)
class ScrubbedEnvironment:
    """The environment a child may see, and the names excluded from it."""

    values: dict[str, str]
    excluded: tuple[str, ...]


def scrub_environment(env: Mapping[str, str] | None) -> ScrubbedEnvironment:
    """Drop credential-shaped and bootstrap-shaped names from a caller-supplied environment.

    Names only are ever recorded as evidence (:class:`ScrubbedEnvironment.excluded`); no value is
    ever retained for a dropped name, and no value is echoed anywhere at all -- the resolved
    specification records names, never values (F6.2).
    """
    if not env:
        return ScrubbedEnvironment(values={}, excluded=())
    values: dict[str, str] = {}
    excluded: list[str] = []
    for key, value in env.items():
        if is_bootstrap_environment_name(key) or is_credential_environment_name(key):
            excluded.append(key)
            continue
        values[key] = value
    return ScrubbedEnvironment(values=values, excluded=tuple(sorted(excluded)))


def apply_runtime_owned(
    scrubbed: ScrubbedEnvironment, overrides: Mapping[str, str]
) -> ScrubbedEnvironment:
    """Let runtime-owned values supersede a caller's, and record every name they displaced.

    Applied *after* :func:`scrub_environment`, so the runtime's value is the last word on any
    name it owns -- the same ordering the local backend already uses for ``PATH``. A caller name
    that was displaced is added to ``excluded``: the evidence then says the caller's value was
    dropped, rather than leaving the reader to notice that a name it supplied is now carrying
    someone else's value. Comparison is case-folded, because Windows environment lookups are.
    """
    folded = {name.upper() for name in overrides}
    superseded = [name for name in scrubbed.values if name.upper() in folded]
    values = {key: value for key, value in scrubbed.values.items() if key.upper() not in folded}
    values.update(overrides)
    return ScrubbedEnvironment(
        values=values, excluded=tuple(sorted({*scrubbed.excluded, *superseded}))
    )
