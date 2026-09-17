"""Read-only Tool API: the registered tool capabilities and their risk metadata (baseline 7).

This is the read side of the Tool API (RA-API-07): it surfaces each registered tool's declared
:class:`~thymira.policies.ToolCapability` so external agents can discover what a tool does and the
risk it carries. It never executes a tool -- every invocation still goes through the Tool Manager
and the Permission Policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query

from thymira.api.deps import RuntimeDeps, get_runtime_deps
from thymira.api.errors import problem
from thymira.api.permissions import Permission
from thymira.api.principal import require_permission
from thymira.api.schemas import ToolDescriptor, ToolListResponse
from thymira.tools import input_schema, result_schema

if TYPE_CHECKING:
    from thymira.tools import Tool

router = APIRouter(prefix="/tools", tags=["tools"])
_DEPS = Depends(get_runtime_deps)
_REQUIRE_READ = Depends(require_permission(Permission.READ))
_RISK_TAG = Query(default=None)


def _describe(tool: Tool) -> ToolDescriptor:
    """Project one registered tool into its external read-only descriptor."""
    return ToolDescriptor(
        name=tool.name,
        description=tool.description,
        capability=tool.capability,
        input_schema=input_schema(tool),
        output_schema=result_schema(tool),
    )


@router.get("", response_model=ToolListResponse, dependencies=[_REQUIRE_READ])
def list_tools(risk_tag: str | None = _RISK_TAG, deps: RuntimeDeps = _DEPS) -> ToolListResponse:
    """List registered tool capabilities, optionally filtered by a declared risk tag."""
    descriptors = tuple(
        _describe(tool)
        for tool in deps.tool_registry
        if risk_tag is None or risk_tag in tool.capability.risk_tags
    )
    return ToolListResponse(items=descriptors)


@router.get("/{name}", response_model=ToolDescriptor, dependencies=[_REQUIRE_READ])
def get_tool(name: str, deps: RuntimeDeps = _DEPS) -> ToolDescriptor:
    """Return one registered tool capability descriptor by name."""
    try:
        tool = deps.tool_registry.get(name)
    except KeyError as exc:
        raise problem(404, "tool_not_found", f"No tool is registered as {name!r}.") from exc
    return _describe(tool)


__all__ = ["router"]
