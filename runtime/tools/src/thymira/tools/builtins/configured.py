"""The registry constructor both production composition roots call.

``builtins_registry()`` stays pure and explicitly parameterised -- it is used by roughly ten
existing tests and by the MCP contract suite, and a hidden environment read there would change
every one of them. This module is the one seam that reads ``THYMIRA_SANDBOX_*`` at all: both
``apps/api/src/thymira/api/deps.py`` and ``runtime/core/src/thymira/core/worker.py`` call
:func:`configured_builtins_registry` instead of ``builtins_registry()`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.tools.builtins.run_python import builtins_registry
from thymira.tools.sandbox import SandboxSettings, build_sandbox

if TYPE_CHECKING:
    from collections.abc import Mapping

    from thymira.tools.registry import ToolRegistry

__all__ = ["configured_builtins_registry"]


def configured_builtins_registry(*, source: Mapping[str, str] | None = None) -> ToolRegistry:
    """Build the production tool registry, selecting its sandbox backend from the environment.

    ``source`` overrides ``os.environ`` for tests; production callers pass nothing, and
    :meth:`SandboxSettings.from_env` reads the real process environment.
    """
    settings = SandboxSettings.from_env(source)
    return builtins_registry(sandbox=build_sandbox(settings), subprocess_mode=settings.mode)
