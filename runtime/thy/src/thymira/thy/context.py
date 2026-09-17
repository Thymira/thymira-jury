"""Project-context loader: `.thymira/config.yaml` + `context.md` -> `ProjectContext` (THY-10).

`ProjectConfig` (the frozen contract) requires a non-empty `project.name`/`project.domain`, so a
project with no `.thymira` directory cannot degrade to an "empty but valid" `ProjectConfig` --
there is no such thing. It degrades to a `ProjectContext` whose `config` is `None` instead: the
node that seeds `ThyState` from it treats a missing project the same as one that declared nothing,
never as an error.

`apply_agents_overlay` (THY-27) is the second half of a project's THY-tailoring: on the same
AgentSpec-as-data seam (THY-01), a project's `.thymira/agents.yaml` overrides or adds specs over a
built-in `AgentCatalog` -- a project changes an agent's `tier`, `tool_allowlist` or prompt, or adds
a whole agent, without any code. It is additive layering, never a runner/graph change: an override
touches only the fields it names, a new agent is a full spec, and every prompt/schema reference is
resolved (or rejected) at load, and every granted tool capability is checked against the registry
through the same `reject_unknown_capabilities`, exactly as `load_agent_specs` does for a catalog
directory.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel

from thymira.agents import AgentCatalog, AgentSpec
from thymira.agents.spec import reject_unknown_capabilities
from thymira.schemas import ProjectConfig, ThymiraModel

if TYPE_CHECKING:
    from pathlib import Path


class ProjectContext(ThymiraModel):
    """A project's typed config plus its free-form context, or both absent."""

    config: ProjectConfig | None = None
    context_md: str = ""


def load_project_context(project_dir: Path) -> ProjectContext:
    """Load `.thymira/config.yaml` and `.thymira/context.md` under `project_dir`.

    Never raises for a missing `.thymira` directory or a missing `config.yaml`/`context.md`
    inside it -- each absence simply leaves its half of `ProjectContext` at its default. A
    malformed `config.yaml` that *is* present still raises: that is a real error, not an absence.
    """
    thymira_dir = project_dir / ".thymira"
    config_path = thymira_dir / "config.yaml"
    config = (
        ProjectConfig.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
        if config_path.is_file()
        else None
    )
    context_path = thymira_dir / "context.md"
    context_md = context_path.read_text(encoding="utf-8") if context_path.is_file() else ""
    return ProjectContext(config=config, context_md=context_md)


def apply_agents_overlay(
    base: AgentCatalog, project_dir: Path, *, known_capabilities: frozenset[str]
) -> AgentCatalog:
    """Merge a project's `.thymira/agents.yaml` over `base`, returning a new `AgentCatalog`.

    The overlay is a mapping with an ``agents`` list; each entry names an agent by ``name``. An
    entry whose name is already in ``base`` overrides only the fields it provides (``tier``,
    ``tool_allowlist``, ``system_prompt_ref``/``output_schema_ref``, ...); an entry whose name is
    new must be a complete `AgentSpec`. A changed or new ``system_prompt_ref`` is read from the
    ``.thymira`` directory and a changed or new ``output_schema_ref`` is imported, both at load
    time; an unchanged reference keeps ``base``'s already-resolved value.

    Every merged spec the overlay touches is checked against ``known_capabilities`` through the
    same `reject_unknown_capabilities` `load_agent_specs` uses, so a project's ``agents.yaml``
    cannot grant an agent a tool capability the built-in loader would refuse -- the two paths
    share one check rather than agreeing by coincidence (finding 74-3).

    Args:
        base: The built-in catalog the project tailors (e.g. `full_agent_catalog()`).
        project_dir: The project root holding `.thymira/agents.yaml`.
        known_capabilities: The tool capabilities that actually exist (built from the populated
            `ToolRegistry`, exactly as `load_agent_specs` takes them); an overlay naming any
            capability outside this set is refused at load.

    Returns:
        A new merged `AgentCatalog`, or ``base`` unchanged when there is no overlay file.

    Raises:
        ValueError: The overlay is malformed, an entry is not a valid `AgentSpec`, grants an
            unknown tool capability, or a prompt or schema reference does not resolve -- rejected
            at load, never half-merged.
    """
    overlay_path = project_dir / ".thymira" / "agents.yaml"
    if not overlay_path.is_file():
        return base
    entries = _overlay_entries(
        yaml.safe_load(overlay_path.read_text(encoding="utf-8")), overlay_path
    )
    specs = {name: base.get(name) for name in base.names()}
    prompts = {name: base.system_prompt(name) for name in base.names()}
    schemas = {name: base.output_schema(name) for name in base.names()}
    thymira_dir = overlay_path.parent
    for entry in entries:
        name = entry["name"]
        base_spec = specs.get(name)
        merged = (
            {**base_spec.model_dump(mode="python"), **entry} if base_spec is not None else entry
        )
        spec = AgentSpec.model_validate(merged)
        reject_unknown_capabilities(spec, known_capabilities, source=overlay_path)
        specs[name] = spec
        prompts[name] = _overlay_prompt(
            spec, thymira_dir, changed="system_prompt_ref" in entry, inherited=prompts.get(name)
        )
        schemas[name] = _overlay_schema(
            spec, changed="output_schema_ref" in entry, inherited=schemas.get(name)
        )
    return AgentCatalog(tuple(specs.values()), system_prompts=prompts, output_schemas=schemas)


def _overlay_entries(data: Any, source: Path) -> list[dict[str, Any]]:
    """Validate the overlay's shape and return its agent entries, each a mapping with a ``name``."""
    if not isinstance(data, dict) or "agents" not in data:
        msg = f"{source}: overlay must be a mapping with an 'agents' list"
        raise ValueError(msg)
    agents = data["agents"]
    if not isinstance(agents, list):
        msg = f"{source}: 'agents' must be a list"
        raise ValueError(msg)  # noqa: TRY004  # a malformed overlay is a config error, not a TypeError
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in agents:
        if not isinstance(raw, dict):
            msg = f"{source}: each agent entry must be a mapping"
            raise ValueError(msg)  # noqa: TRY004  # a malformed overlay is a config error, not a TypeError
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            msg = f"{source}: each agent entry needs a non-empty 'name'"
            raise ValueError(msg)
        if name in seen:
            msg = f"{source}: duplicate agent entry: {name!r}"
            raise ValueError(msg)
        seen.add(name)
        entries.append(raw)
    return entries


def _overlay_prompt(
    spec: AgentSpec, thymira_dir: Path, *, changed: bool, inherited: str | None
) -> str:
    """Resolve a spec's prompt: re-read from `.thymira` when the ref changed, else inherit."""
    if not changed and inherited is not None:
        return inherited
    catalog_root = thymira_dir.resolve()
    prompt_path = (thymira_dir / spec.system_prompt_ref).resolve()
    try:
        prompt_path.relative_to(catalog_root)
    except ValueError as exc:
        msg = (
            f"agent {spec.name!r} system_prompt_ref points outside .thymira: "
            f"{spec.system_prompt_ref!r}"
        )
        raise ValueError(msg) from exc
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"agent {spec.name!r} system_prompt_ref does not resolve: {prompt_path}"
        raise ValueError(msg) from exc


def _overlay_schema(
    spec: AgentSpec, *, changed: bool, inherited: type[BaseModel] | None
) -> type[BaseModel]:
    """Resolve a spec's `module:Class` output schema when the ref changed, else inherit."""
    if not changed and inherited is not None:
        return inherited
    ref = spec.output_schema_ref
    module_path, _, class_name = ref.partition(":")
    if (
        not module_path
        or not class_name
        or not class_name.isidentifier()
        or any(not part.isidentifier() for part in module_path.split("."))
    ):
        msg = f"agent {spec.name!r} output_schema_ref must be 'module.path:ClassName', got {ref!r}"
        raise ValueError(msg)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = f"agent {spec.name!r} output_schema_ref module not importable: {module_path!r}"
        raise ValueError(msg) from exc
    schema = getattr(module, class_name, None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        msg = f"agent {spec.name!r} output_schema_ref {ref!r} is not a BaseModel subclass"
        raise ValueError(msg)  # noqa: TRY004  # a config error, consistent with the sibling checks
    return schema


__all__ = ["ProjectContext", "apply_agents_overlay", "load_project_context"]
