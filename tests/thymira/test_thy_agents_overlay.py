"""Project-level agents.yaml overlay over the built-in AgentCatalog (THY-27)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from thymira.agents.llm.routing import ModelTier
from thymira.thy import (
    DataProfile,
    apply_agents_overlay,
    statistics_agent_catalog,
)

if TYPE_CHECKING:
    from pathlib import Path

_CUSTOM_PROMPT = "You are the custom project agent.\n"

_OVERRIDE_AND_ADD = """\
agents:
  - name: statistics-agent
    tier: FRONTIER
  - name: custom-agent
    role: agent
    task_kinds: [analyze]
    tool_allowlist: [run_python]
    max_turns: 4
    max_depth: 1
    system_prompt_ref: prompts/custom.md
    output_schema_ref: thymira.thy.agents.data:DataProfile
"""


# The overlay path now runs the same capability check the built-in loader does, so every
# caller states which capabilities actually exist. These are the ones this module's fixtures
# use; the check itself is covered in test_thy_context_specs.py.
_KNOWN = frozenset({"run_statistics", "run_python"})


def _write_overlay(project_dir: Path, yaml_text: str, *, custom_prompt: str | None = None) -> None:
    thymira_dir = project_dir / ".thymira"
    thymira_dir.mkdir(parents=True, exist_ok=True)
    (thymira_dir / "agents.yaml").write_text(yaml_text, encoding="utf-8")
    if custom_prompt is not None:
        prompts_dir = thymira_dir / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / "custom.md").write_text(custom_prompt, encoding="utf-8")


def test_no_overlay_returns_the_base_catalog_unchanged(tmp_path: Path) -> None:
    base = statistics_agent_catalog()

    merged = apply_agents_overlay(base, tmp_path, known_capabilities=_KNOWN)

    assert merged is base


def test_overlay_overrides_a_tier_and_adds_a_new_agent(tmp_path: Path) -> None:
    base = statistics_agent_catalog()
    _write_overlay(tmp_path, _OVERRIDE_AND_ADD, custom_prompt=_CUSTOM_PROMPT)

    merged = apply_agents_overlay(base, tmp_path, known_capabilities=_KNOWN)

    # The override changes only the tier; the untouched prompt and schema are inherited from base.
    overridden = merged.get("statistics-agent")
    assert overridden.tier is ModelTier.FRONTIER
    assert base.get("statistics-agent").tier is None
    assert merged.system_prompt("statistics-agent") == base.system_prompt("statistics-agent")
    assert merged.output_schema("statistics-agent") is base.output_schema("statistics-agent")

    # The new agent is a full spec, with its own prompt read from .thymira and schema imported.
    added = merged.get("custom-agent")
    assert added.tool_allowlist == ("run_python",)
    assert added.max_turns == 4
    assert merged.system_prompt("custom-agent") == _CUSTOM_PROMPT
    assert merged.output_schema("custom-agent") is DataProfile


def test_an_overlay_with_an_invalid_tier_is_rejected_at_load(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        "agents:\n  - name: statistics-agent\n    tier: SUPERSONIC\n",
    )

    with pytest.raises(ValidationError):
        apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)


def test_a_new_agent_with_an_unresolvable_schema_is_rejected_at_load(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        """\
agents:
  - name: broken-agent
    role: agent
    task_kinds: [analyze]
    max_turns: 2
    max_depth: 1
    system_prompt_ref: prompts/custom.md
    output_schema_ref: thymira.thy.agents.data:NoSuchModel
""",
        custom_prompt=_CUSTOM_PROMPT,
    )

    with pytest.raises(ValueError, match="output_schema_ref"):
        apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)


def test_a_malformed_overlay_shape_is_rejected_at_load(tmp_path: Path) -> None:
    _write_overlay(tmp_path, "agents:\n  not-a-list: true\n")

    with pytest.raises(ValueError, match="must be a list"):
        apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)
