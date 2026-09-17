"""MIRA's runtime skill catalog entrypoint.

MIRA keeps its loader entrypoint separate from THY and from the developer ``.agents/skills``
validator. The underlying immutable catalog mechanics are shared with the agent runtime, while
MIRA's composition supplies only its own roots and selected names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.agents import load_runtime_skill_catalog

if TYPE_CHECKING:
    from pathlib import Path

    from thymira.agents import RuntimeSkillCatalog


def load_mira_skill_catalog(
    roots: tuple[Path, ...] = (),
) -> RuntimeSkillCatalog:
    """Load a fresh MIRA runtime skill catalog from ordered roots."""
    return load_runtime_skill_catalog(roots, orchestrator="mira")


__all__ = ["load_mira_skill_catalog"]
