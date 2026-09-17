"""Registry of tools available to the Tool Manager."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel

from thymira.schemas import ThymiraModel
from thymira.tools.results import result_model_for

if TYPE_CHECKING:
    from collections.abc import Iterator

    from thymira.policies import ToolCapability
    from thymira.tools.models import Tool


class ToolRegistry:
    """Own the unique, name-based catalog of executable tools."""

    def __init__(self, tools: tuple[Tool, ...] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register one tool and reject duplicate or inconsistent names."""
        if tool.name != tool.capability.id:
            msg = f"tool name {tool.name!r} differs from capability id {tool.capability.id!r}"
            raise ValueError(msg)
        if tool.name in self._tools:
            msg = f"tool already registered: {tool.name}"
            raise ValueError(msg)
        result_model = result_model_for(tool)
        if result_model is None:
            raise ValueError(f"tool {tool.name!r} must declare a typed result model")
        _validate_result_model(tool.name, result_model)

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Return a registered tool or raise ``KeyError`` for an unknown name."""
        try:
            return self._tools[name]
        except KeyError as exc:
            msg = f"unknown tool: {name}"
            raise KeyError(msg) from exc

    def __iter__(self) -> Iterator[Tool]:
        """Iterate over registered tools in registration order."""
        return iter(self._tools.values())

    def capabilities(self) -> tuple[ToolCapability, ...]:
        """Return the immutable capabilities Core supplies to the pre-execution Gate."""
        return tuple(tool.capability for tool in self._tools.values())


def _validate_result_model(tool_name: str, result_model: type[BaseModel]) -> None:
    """Reject result models that cannot provide a strict, replayable value contract."""
    if result_model is BaseModel or not issubclass(result_model, ThymiraModel):
        raise ValueError(
            f"tool {tool_name!r} must declare a result model inheriting from ThymiraModel"
        )
    config = result_model.model_config
    if config.get("extra") != "forbid" or config.get("frozen") is not True:
        raise ValueError(f"tool {tool_name!r} result model must be frozen with extra='forbid'")
    try:
        properties = result_model.model_json_schema().get("properties")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"tool {tool_name!r} has an invalid typed result model") from exc
    if not isinstance(properties, dict) or not properties:
        raise ValueError(f"tool {tool_name!r} must declare a concrete result schema")
    if _contains_open_type(result_model, set()):
        raise ValueError(
            f"tool {tool_name!r} result model must not contain Any, object, or open mappings"
        )


def _contains_open_type(model: type[BaseModel], seen: set[type[BaseModel]]) -> bool:
    """Return whether a model or nested model contains an unconstrained JSON value."""
    if model in seen:
        return False
    seen.add(model)
    return any(_annotation_is_open(field.annotation, seen) for field in model.model_fields.values())


def _annotation_is_open(annotation: Any, seen: set[type[BaseModel]]) -> bool:
    """Return whether one field annotation admits values outside a closed JSON domain."""
    if annotation is Any or annotation is object:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if (
        annotation in {list, tuple, set, frozenset}
        or origin in {list, tuple, set, frozenset, Sequence}
        or annotation is dict
        or annotation is Mapping
        or origin in {dict, Mapping}
    ):
        return not args or any(_annotation_is_open(argument, seen) for argument in args)
    if args and any(_annotation_is_open(argument, seen) for argument in args):
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        config = annotation.model_config
        if config.get("extra") != "forbid" or config.get("frozen") is not True:
            return True
        return _contains_open_type(annotation, seen)
    return False
