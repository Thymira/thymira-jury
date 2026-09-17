"""The project agents overlay shares the catalog loader's capability check (finding 74-3).

`load_agent_specs` refuses a spec granting a tool capability no registry holds; the overlay
(`apply_agents_overlay`) used to accept the identical spec. Both paths now run the one shared
`reject_unknown_capabilities`, so a project's `.thymira/agents.yaml` can never grant a capability
that does not exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from thymira.thy import apply_agents_overlay, statistics_agent_catalog

if TYPE_CHECKING:
    from pathlib import Path

# The real statistics-agent's built-in capabilities, plus nothing else.
_KNOWN = frozenset({"run_statistics", "run_python"})

_ADDS_UNKNOWN_CAPABILITY = """\
agents:
  - name: rogue-agent
    role: agent
    task_kinds: [analyze]
    tool_allowlist: [run_python, delete_everything]
    max_turns: 2
    max_depth: 1
    system_prompt_ref: prompts/custom.md
    output_schema_ref: thymira.thy.agents.data:DataProfile
"""

_ADDS_KNOWN_CAPABILITY = """\
agents:
  - name: helper-agent
    role: agent
    task_kinds: [analyze]
    tool_allowlist: [run_python]
    max_turns: 2
    max_depth: 1
    system_prompt_ref: prompts/custom.md
    output_schema_ref: thymira.thy.agents.data:DataProfile
"""

_OVERRIDE_ADDS_UNKNOWN_CAPABILITY = """\
agents:
  - name: statistics-agent
    tool_allowlist: [run_statistics, run_python, delete_everything]
"""


def _write_overlay(project_dir: Path, yaml_text: str, *, custom_prompt: bool = False) -> None:
    """Write an `agents.yaml` (and an optional custom prompt) under a project's `.thymira`."""
    thymira_dir = project_dir / ".thymira"
    thymira_dir.mkdir(parents=True, exist_ok=True)
    (thymira_dir / "agents.yaml").write_text(yaml_text, encoding="utf-8")
    if custom_prompt:
        prompts_dir = thymira_dir / "prompts"
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / "custom.md").write_text("You are the custom agent.\n", encoding="utf-8")


def test_an_overlay_adding_a_capability_no_registry_holds_is_refused(tmp_path: Path) -> None:
    """A new overlay agent granting an unknown capability is refused, as the loader would."""
    _write_overlay(tmp_path, _ADDS_UNKNOWN_CAPABILITY, custom_prompt=True)

    with pytest.raises(ValueError, match="unknown tool capabilities"):
        apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)


def test_an_overlay_override_that_adds_an_unknown_capability_is_refused(tmp_path: Path) -> None:
    """Overriding an existing agent's allowlist with an unknown capability is refused too.

    The check runs on the *merged* spec, so the override branch (not only a brand-new agent) is
    guarded -- a project cannot smuggle a capability in by editing a built-in agent's allowlist.
    """
    _write_overlay(tmp_path, _OVERRIDE_ADDS_UNKNOWN_CAPABILITY)

    with pytest.raises(ValueError, match="unknown tool capabilities"):
        apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)


def test_an_overlay_capability_the_registry_holds_is_accepted(tmp_path: Path) -> None:
    """A capability that does exist is accepted: the check refuses the unknown, not the legitimate.

    Guards the fix against over-correcting into a denial of service -- an overlay naming only real
    capabilities merges cleanly.
    """
    _write_overlay(tmp_path, _ADDS_KNOWN_CAPABILITY, custom_prompt=True)

    merged = apply_agents_overlay(statistics_agent_catalog(), tmp_path, known_capabilities=_KNOWN)

    assert merged.get("helper-agent").tool_allowlist == ("run_python",)
