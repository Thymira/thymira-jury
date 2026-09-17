"""Runtime-owned sandbox backend selection, read from the process environment.

Mirrors the existing ``RabbitMqSettings.from_env()`` / ``PostgresSettings.from_env()`` pattern
(``runtime/core/src/thymira/core/rabbitmq.py``): an operator decision read at the composition
root, never a project-declared fact. ``builtins_registry()`` itself stays pure and explicitly
parameterised; :func:`thymira.tools.builtins.configured_builtins_registry` is the only production
caller of :func:`build_sandbox`.

Defaulting ``THYMIRA_SANDBOX_BACKEND`` to ``container`` (rather than today's local-only behaviour)
costs nothing where Docker is absent -- :class:`~thymira.tools.sandbox.ContainerSandbox.__init__`
performs no I/O, and a missing daemon returns the same ``UNUSABLE``/125 shape production gets
today, with an honest reason -- and it makes confinement the default rather than an opt-in nobody
knows to set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from thymira.schemas import SandboxMode
from thymira.tools.sandbox.container import ContainerSandbox
from thymira.tools.sandbox.local import LocalSubprocessSandbox
from thymira.tools.sandbox.process_capture import DEFAULT_OUTPUT_LIMIT_BYTES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from thymira.tools.sandbox.base import Sandbox

__all__ = ["SandboxConfigurationError", "SandboxSettings", "build_sandbox"]

_BACKEND_ENV_VAR = "THYMIRA_SANDBOX_BACKEND"
_IMAGE_ENV_VAR = "THYMIRA_SANDBOX_IMAGE"
_MEMORY_ENV_VAR = "THYMIRA_SANDBOX_MEMORY"
_CPUS_ENV_VAR = "THYMIRA_SANDBOX_CPUS"
_PIDS_LIMIT_ENV_VAR = "THYMIRA_SANDBOX_PIDS_LIMIT"
_OUTPUT_LIMIT_ENV_VAR = "THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES"
_WORKSPACE_QUOTA_ENV_VAR = "THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES"
_MODE_ENV_VAR = "THYMIRA_SANDBOX_MODE"

_KNOWN_BACKENDS = ("container", "local")


class SandboxConfigurationError(RuntimeError):
    """Sandbox configuration read from the environment is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """The sandbox backend and its limits, resolved once at the composition root."""

    backend: str = "container"
    image: str = "thymira:dev"
    memory: str = "1g"
    cpus: str = "1.0"
    pids_limit: int = 128
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    workspace_quota_bytes: int | None = None
    mode: SandboxMode | None = None

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> SandboxSettings:
        """Build settings from ``THYMIRA_SANDBOX_*`` environment variables, failing closed.

        An unknown backend, a non-integer or non-positive pids limit, or an unknown
        ``THYMIRA_SANDBOX_MODE`` all raise :class:`SandboxConfigurationError` rather than
        silently falling back to a default (G.2).
        """
        env = os.environ if source is None else source
        defaults = cls()
        backend = env.get(_BACKEND_ENV_VAR, defaults.backend)
        if backend not in _KNOWN_BACKENDS:
            raise SandboxConfigurationError(
                f"unknown {_BACKEND_ENV_VAR}: {backend!r} (expected one of {_KNOWN_BACKENDS})"
            )
        pids_limit = _positive_int(env, _PIDS_LIMIT_ENV_VAR, defaults.pids_limit)
        output_limit_bytes = _positive_int(env, _OUTPUT_LIMIT_ENV_VAR, defaults.output_limit_bytes)
        workspace_quota_raw = env.get(_WORKSPACE_QUOTA_ENV_VAR)
        workspace_quota_bytes = (
            None
            if workspace_quota_raw in (None, "")
            else _positive_int(env, _WORKSPACE_QUOTA_ENV_VAR, 0)
        )
        mode_raw = env.get(_MODE_ENV_VAR)
        mode: SandboxMode | None = None
        if mode_raw:
            try:
                mode = SandboxMode(mode_raw)
            except ValueError as exc:
                raise SandboxConfigurationError(f"unknown {_MODE_ENV_VAR}: {mode_raw!r}") from exc
        return cls(
            backend=backend,
            image=env.get(_IMAGE_ENV_VAR, defaults.image),
            memory=env.get(_MEMORY_ENV_VAR, defaults.memory),
            cpus=env.get(_CPUS_ENV_VAR, defaults.cpus),
            pids_limit=pids_limit,
            output_limit_bytes=output_limit_bytes,
            workspace_quota_bytes=workspace_quota_bytes,
            mode=mode,
        )


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    """Read one positive integer setting without leaking parser exceptions past configuration."""
    raw = source.get(name, str(default))
    if isinstance(raw, bool):
        raise SandboxConfigurationError(f"{name} must be an integer, got {raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise SandboxConfigurationError(f"{name} must be positive")
    return value


def build_sandbox(settings: SandboxSettings) -> Sandbox:
    """Build the sandbox backend ``settings`` selects.

    A backend construction ``ValueError``/``TypeError`` -- an argv-unsafe image, memory or cpus
    value, or a non-positive pids limit -- is turned into :class:`SandboxConfigurationError` so an
    environment variable can never inject a docker flag past the composition root.
    """
    if settings.backend == "local":
        try:
            return LocalSubprocessSandbox(
                memory=settings.memory,
                cpus=settings.cpus,
                pids_limit=settings.pids_limit,
                output_limit_bytes=settings.output_limit_bytes,
                workspace_quota_bytes=settings.workspace_quota_bytes,
            )
        except (ValueError, TypeError) as exc:
            raise SandboxConfigurationError(str(exc)) from exc
    try:
        return ContainerSandbox(
            image=settings.image,
            memory=settings.memory,
            cpus=settings.cpus,
            pids_limit=settings.pids_limit,
            output_limit_bytes=settings.output_limit_bytes,
            workspace_quota_bytes=settings.workspace_quota_bytes,
        )
    except (ValueError, TypeError) as exc:
        raise SandboxConfigurationError(str(exc)) from exc
