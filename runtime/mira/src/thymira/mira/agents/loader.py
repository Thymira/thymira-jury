"""Load the shipped MIRA audit-agent roster from packaged YAML declarations."""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from thymira.mira.agents.spec import load_specs

if TYPE_CHECKING:
    from thymira.mira.agents.spec import AuditAgentSpec

DEFAULTS_PACKAGE = "thymira.mira.agents.defaults"


def load_default_specs() -> tuple[AuditAgentSpec, ...]:
    """Load every packaged audit-agent declaration in deterministic path order.

    A new audit agent ships by dropping one more ``*.yaml`` file into the ``defaults`` package;
    this helper discovers it with no further code change, mirroring
    :func:`thymira.mira.preflight.loader.load_default_packs`.

    Raises:
        ValueError: If a packaged declaration is missing, malformed, or invalid.
    """
    with resources.as_file(resources.files(DEFAULTS_PACKAGE)) as directory:
        return load_specs(directory)


__all__ = ["DEFAULTS_PACKAGE", "load_default_specs"]
