"""AgentSpec: sub-agent declaration as data, and its catalog loader.

ADR-0004 dec.4 fixes this seam: sub-agents are declared as data, loaded, never hard-coded. The
full field set exists from the MVP, so adding the FINAL agents (THY-18..22) or a per-project
overlay (THY-27) is adding data, never a code change.

Scope note: `AgentSpec` is not `AuditAgentSpec` (GOV-01 ships `thymira.mira.agents.spec.
AuditAgentSpec` with a different field set and its own loader). Both are built independently on
purpose; do not widen either to serve the other.

Reference resolution: `system_prompt_ref` is a path (relative to the loaded directory) to a text
file, read verbatim. `output_schema_ref` is a `module.path:ClassName` import path to a
`pydantic.BaseModel` subclass already defined in code — never a JSON Schema file, which would
have to be reconstructed into a Python type with no guarantee of round-tripping. Both are resolved
eagerly by `load_agent_specs`, refusing at load time (never at agent-run time) exactly like
`tool_allowlist`, and the resolved values are what `AgentCatalog.system_prompt`/`.output_schema`
expose — the spec itself only ever stores the reference string (THY-03 consumes the resolved
values; it never re-resolves them).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from thymira.agents.llm.routing import ModelTier, Role, TaskKind
from thymira.schemas import ThymiraModel


class AgentSpec(ThymiraModel):
    """A sub-agent's declared identity, permissions, and prompt/schema references.

    `system_prompt_ref`/`output_schema_ref` are keys resolved by the loader (`load_agent_specs`)
    into the prompt text and the output type; the catalog exposes the resolved value
    (`AgentCatalog.system_prompt`/`.output_schema`). This spec stores only the reference, never
    the resolved text or type, so editing a prompt never touches this module.
    """

    name: str = Field(min_length=1)
    role: Role
    task_kinds: tuple[TaskKind, ...] = Field(min_length=1)
    tool_allowlist: tuple[str, ...] = ()
    tier: ModelTier | None = None
    max_turns: int = Field(gt=0)
    max_depth: int = Field(gt=0)
    runtime_skill_names: tuple[str, ...] = ()
    system_prompt_ref: str = Field(min_length=1)
    output_schema_ref: str = Field(min_length=1)


class AgentCatalog:
    """Owns a name-keyed set of `AgentSpec`s loaded from data, plus their resolved references."""

    def __init__(
        self,
        specs: tuple[AgentSpec, ...] = (),
        *,
        system_prompts: dict[str, str] | None = None,
        output_schemas: dict[str, type[BaseModel]] | None = None,
    ) -> None:
        self._specs: dict[str, AgentSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                msg = f"duplicate agent spec name: {spec.name}"
                raise ValueError(msg)
            self._specs[spec.name] = spec
        self._system_prompts = dict(system_prompts or {})
        self._output_schemas = dict(output_schemas or {})

    def get(self, name: str) -> AgentSpec:
        """Return a registered spec, or raise ``KeyError`` for an unknown name."""
        try:
            return self._specs[name]
        except KeyError as exc:
            msg = f"unknown agent spec: {name}"
            raise KeyError(msg) from exc

    def names(self) -> tuple[str, ...]:
        """Registered spec names, in load order."""
        return tuple(self._specs)

    def system_prompt(self, name: str) -> str:
        """The text `spec.system_prompt_ref` resolved to, for a spec from `load_agent_specs`."""
        self.get(name)
        try:
            return self._system_prompts[name]
        except KeyError as exc:
            msg = f"no resolved system prompt for agent spec: {name}"
            raise KeyError(msg) from exc

    def output_schema(self, name: str) -> type[BaseModel]:
        """The type `spec.output_schema_ref` resolved to, for a spec from `load_agent_specs`."""
        self.get(name)
        try:
            return self._output_schemas[name]
        except KeyError as exc:
            msg = f"no resolved output schema for agent spec: {name}"
            raise KeyError(msg) from exc


def reject_unknown_capabilities(
    spec: AgentSpec, known_capabilities: frozenset[str], *, source: Path
) -> None:
    """Refuse a spec whose ``tool_allowlist`` names a capability outside ``known_capabilities``.

    The single capability check both `load_agent_specs` and the project overlay
    (`thymira.thy.apply_agents_overlay`) run, so a project's ``agents.yaml`` can never grant an
    agent a tool capability the built-in loader would refuse -- the two paths share this guarantee
    instead of agreeing by coincidence. ``source`` locates the offending file in the message.

    Raises:
        ValueError: A named capability is not in ``known_capabilities``.
    """
    unknown = set(spec.tool_allowlist) - known_capabilities
    if unknown:
        msg = f"{source}: agent {spec.name!r} allows unknown tool capabilities: {sorted(unknown)}"
        raise ValueError(msg)


def load_agent_specs(directory: Path, *, known_capabilities: frozenset[str]) -> AgentCatalog:
    """Load every ``*.yaml`` file in ``directory`` into an `AgentCatalog`.

    Refuses at load time — never at agent-run time — a spec whose `tool_allowlist` names a
    capability outside `known_capabilities`, whose `system_prompt_ref` does not resolve to a
    readable file under `directory`, or whose `output_schema_ref` does not resolve to a
    `BaseModel` subclass. Callers build `known_capabilities` from the populated `ToolRegistry`
    (``{tool.capability.id for tool in registry}``), so adding a tool never means editing this
    module.
    """
    directory = Path(directory)
    specs: list[AgentSpec] = []
    system_prompts: dict[str, str] = {}
    output_schemas: dict[str, type[BaseModel]] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec = AgentSpec.model_validate(data)
        reject_unknown_capabilities(spec, known_capabilities, source=path)
        system_prompts[spec.name] = _resolve_system_prompt(spec, directory, source=path)
        output_schemas[spec.name] = _resolve_output_schema(spec, source=path)
        specs.append(spec)
    return AgentCatalog(tuple(specs), system_prompts=system_prompts, output_schemas=output_schemas)


def _resolve_system_prompt(spec: AgentSpec, directory: Path, *, source: Path) -> str:
    catalog_root = directory.resolve()
    prompt_path = (directory / spec.system_prompt_ref).resolve()
    try:
        prompt_path.relative_to(catalog_root)
    except ValueError as exc:
        msg = (
            f"{source}: agent {spec.name!r} system_prompt_ref points outside the "
            "catalog directory: "
            f"{spec.system_prompt_ref!r}"
        )
        raise ValueError(msg) from exc
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"{source}: agent {spec.name!r} system_prompt_ref does not resolve: {prompt_path}"
        raise ValueError(msg) from exc


def _resolve_output_schema(spec: AgentSpec, *, source: Path) -> type[BaseModel]:
    module_path, _, class_name = spec.output_schema_ref.partition(":")
    if (
        not module_path
        or not class_name
        or not class_name.isidentifier()
        or any(not part.isidentifier() for part in module_path.split("."))
    ):
        msg = (
            f"{source}: agent {spec.name!r} output_schema_ref must be 'module.path:ClassName', "
            f"got {spec.output_schema_ref!r}"
        )
        raise ValueError(msg)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = (
            f"{source}: agent {spec.name!r} output_schema_ref module not importable: "
            f"{module_path!r}"
        )
        raise ValueError(msg) from exc
    schema = getattr(module, class_name, None)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        msg = (
            f"{source}: agent {spec.name!r} output_schema_ref {spec.output_schema_ref!r} "
            "is not a BaseModel subclass"
        )
        raise ValueError(msg)  # noqa: TRY004  # a config error, consistent with the sibling checks above
    return schema


__all__ = ["AgentCatalog", "AgentSpec", "load_agent_specs", "reject_unknown_capabilities"]
