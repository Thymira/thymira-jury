"""AgentSpec: sub-agent declaration as data, and its catalog loader (THY-01)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from tests.thymira.fixtures_agent_output import DummyOutput
from thymira.agents import AgentCatalog, AgentSpec, load_agent_specs

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_YAML = """\
name: {name}
role: agent
task_kinds: [analyze, code]
tool_allowlist: [{tools}]
max_turns: 8
max_depth: 2
system_prompt_ref: prompts/{name}.md
output_schema_ref: tests.thymira.fixtures_agent_output:DummyOutput
"""


def _write_spec(directory: Path, name: str, *, tools: str = "run_python") -> None:
    (directory / f"{name}.yaml").write_text(
        _SPEC_YAML.format(name=name, tools=tools), encoding="utf-8"
    )
    prompts_dir = directory / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / f"{name}.md").write_text(f"You are the {name} agent.", encoding="utf-8")


def test_loads_a_catalog_of_at_least_three_specs(tmp_path: Path) -> None:
    for name in ("data", "coding", "experiment"):
        _write_spec(tmp_path, name)

    catalog = load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))

    assert set(catalog.names()) == {"data", "coding", "experiment"}
    for name in catalog.names():
        spec = catalog.get(name)
        assert spec.role is not None
        assert spec.task_kinds == ("analyze", "code")
        assert spec.tool_allowlist == ("run_python",)
        assert spec.max_turns == 8
        assert spec.max_depth == 2


def test_rejects_a_spec_whose_allowlist_names_an_unknown_capability(tmp_path: Path) -> None:
    _write_spec(tmp_path, "coding", tools="run_python, delete_everything")

    with pytest.raises(ValueError, match="unknown tool capabilities"):
        load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))


def test_the_same_directory_loads_cleanly_once_the_capability_is_known(tmp_path: Path) -> None:
    _write_spec(tmp_path, "coding", tools="run_python, git_commit")

    catalog = load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python", "git_commit"}))

    assert catalog.get("coding").tool_allowlist == ("run_python", "git_commit")


def test_the_catalog_exposes_the_resolved_system_prompt_and_output_schema(tmp_path: Path) -> None:
    _write_spec(tmp_path, "coding")

    catalog = load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))

    assert catalog.system_prompt("coding") == "You are the coding agent."
    assert catalog.output_schema("coding") is DummyOutput


def test_a_system_prompt_ref_that_does_not_resolve_is_refused_at_load_time(tmp_path: Path) -> None:
    (tmp_path / "coding.yaml").write_text(
        _SPEC_YAML.format(name="coding", tools="run_python"), encoding="utf-8"
    )
    # No prompts/coding.md written.

    with pytest.raises(ValueError, match="system_prompt_ref does not resolve"):
        load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))


def test_a_system_prompt_ref_cannot_escape_the_catalog_directory(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    outside_prompt = tmp_path / "outside.md"
    outside_prompt.write_text("must not be loaded", encoding="utf-8")
    (catalog_dir / "coding.yaml").write_text(
        _SPEC_YAML.format(name="coding", tools="run_python").replace(
            "prompts/coding.md", "../outside.md"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside the catalog directory"):
        load_agent_specs(catalog_dir, known_capabilities=frozenset({"run_python"}))


def test_an_output_schema_ref_that_does_not_resolve_is_refused_at_load_time(tmp_path: Path) -> None:
    bad_ref = "tests.thymira.fixtures_agent_output:NoSuchClass"
    (tmp_path / "coding.yaml").write_text(
        _SPEC_YAML.format(name="coding", tools="run_python").replace(
            "tests.thymira.fixtures_agent_output:DummyOutput", bad_ref
        ),
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "coding.md").write_text("You are the coding agent.", encoding="utf-8")

    with pytest.raises(ValueError, match="not a BaseModel subclass"):
        load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))


def test_an_output_schema_ref_with_invalid_import_identifiers_is_refused(
    tmp_path: Path,
) -> None:
    bad_ref = "tests.thymira.fixtures_agent_output:DummyOutput.extra"
    (tmp_path / "coding.yaml").write_text(
        _SPEC_YAML.format(name="coding", tools="run_python").replace(
            "tests.thymira.fixtures_agent_output:DummyOutput", bad_ref
        ),
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "coding.md").write_text("You are the coding agent.", encoding="utf-8")

    with pytest.raises(ValueError, match=r"must be 'module\.path:ClassName'"):
        load_agent_specs(tmp_path, known_capabilities=frozenset({"run_python"}))


def test_catalog_rejects_duplicate_names() -> None:
    spec = AgentSpec(
        name="data",
        role="agent",
        task_kinds=("analyze",),
        max_turns=1,
        max_depth=1,
        system_prompt_ref="p",
        output_schema_ref="s",
    )
    with pytest.raises(ValueError, match="duplicate agent spec name"):
        AgentCatalog((spec, spec))


def test_get_raises_key_error_for_an_unknown_name() -> None:
    with pytest.raises(KeyError, match="unknown agent spec"):
        AgentCatalog().get("nobody")


def test_agent_spec_is_frozen() -> None:
    spec = AgentSpec(
        name="data",
        role="agent",
        task_kinds=("analyze",),
        max_turns=1,
        max_depth=1,
        system_prompt_ref="p",
        output_schema_ref="s",
    )
    with pytest.raises(ValidationError):
        setattr(spec, "max_turns", 99)  # noqa: B010  # frozen model must refuse


def test_max_turns_and_max_depth_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(
            name="data",
            role="agent",
            task_kinds=("analyze",),
            max_turns=0,
            max_depth=1,
            system_prompt_ref="p",
            output_schema_ref="s",
        )


def test_a_spec_with_no_task_kinds_is_refused_at_validation() -> None:
    """An empty ``task_kinds`` is a config error caught at validation, not a bare `IndexError`.

    `AgentRunner.run` routes a step by ``spec.task_kinds[0]``; a spec that declares no task kind
    must fail where this repository prefers to fail -- at validation -- rather than with an
    `IndexError` raised deep in the runner, far from the cause (finding #25-5).
    """
    with pytest.raises(ValidationError, match="task_kinds"):
        AgentSpec(
            name="data",
            role="agent",
            task_kinds=(),
            max_turns=1,
            max_depth=1,
            system_prompt_ref="p",
            output_schema_ref="s",
        )


def test_an_empty_tool_allowlist_stays_valid() -> None:
    """A spec granting no tools is legitimate: the empty allowlist is the no-tools case.

    Guards the `task_kinds` fix against over-correcting into every collection field -- an agent
    with no `tool_allowlist` is valid (the runner simply attaches no tools), so this field keeps
    its empty default.
    """
    spec = AgentSpec(
        name="data",
        role="agent",
        task_kinds=("analyze",),
        max_turns=1,
        max_depth=1,
        system_prompt_ref="p",
        output_schema_ref="s",
    )

    assert spec.tool_allowlist == ()
