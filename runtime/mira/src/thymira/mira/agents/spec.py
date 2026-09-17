"""AuditAgentSpec declarations and deterministic YAML loading for MIRA."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, ValidationError, model_validator

from thymira.agents.llm.routing import ModelTier, TaskKind
from thymira.schemas import Framework, ThymiraModel


class AuditAgentSpec(ThymiraModel):
    """A MIRA audit agent's identity, framework, tasks, permissions, and prompt."""

    name: str = Field(min_length=1)
    framework: Framework
    task_kinds: tuple[TaskKind, ...]
    tool_allowlist: tuple[str, ...] = ()
    required_tool_calls: tuple[str, ...] = ()
    tier: ModelTier | None = None
    max_turns: int = Field(gt=0)
    runtime_skill_names: tuple[str, ...] = ()
    system_prompt: str = Field(min_length=1)

    @model_validator(mode="after")
    def _required_tools_are_allowed(self) -> AuditAgentSpec:
        """Require every mandatory tool call to be present in the agent's allowlist."""
        disallowed = tuple(
            tool for tool in self.required_tool_calls if tool not in self.tool_allowlist
        )
        if disallowed:
            rendered = ", ".join(repr(tool) for tool in disallowed)
            raise ValueError(
                f"required_tool_calls must be a subset of tool_allowlist; not allowed: {rendered}"
            )
        return self


def load_specs(directory: Path) -> tuple[AuditAgentSpec, ...]:
    """Load MIRA audit-agent YAML declarations in deterministic path order.

    Raises:
        ValueError: If the directory, YAML, declaration values, or spec names are invalid.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"audit agent spec directory does not exist: {directory}")

    paths = sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))
    specs: list[AuditAgentSpec] = []
    sources: dict[str, Path] = {}
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: malformed audit agent YAML: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"{path}: cannot read audit agent spec: {exc}") from exc
        try:
            spec = AuditAgentSpec.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"{path}: invalid audit agent spec: {exc}") from exc
        if spec.name in sources:
            raise ValueError(
                f"duplicate audit agent spec name {spec.name!r}: {sources[spec.name]} and {path}"
            )
        sources[spec.name] = path
        specs.append(spec)
    return tuple(specs)


__all__ = ["AuditAgentSpec", "load_specs"]
