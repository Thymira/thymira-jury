"""Closed, executable constraints authorised for one THY run."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from thymira.schemas.base import ThymiraModel


class ExecutionAction(StrEnum):
    """Execution actions that the pre-THY Gate and planner can refuse."""

    START = "execution.start"
    PLAN = "plan.proposed"


class ExecutionConstraints(ThymiraModel):
    """The bounded conditions the runtime can enforce before and during execution.

    Empty tool lists mean that this constraint does not narrow the corresponding set.  Several
    applicable rules are combined by :meth:`merged_with`: allow-lists intersect, denials and
    evidence requirements accumulate, and numeric limits become stricter.  The model deliberately
    has no extension bag: a policy must not claim to constrain an effect no runtime component can
    check.
    """

    allowed_tools: tuple[str, ...] = ()
    prohibited_tools: tuple[str, ...] = ()
    local_execution_only: bool = False
    requires_human_review: bool = False
    required_evidence: tuple[str, ...] = ()
    prohibited_actions: tuple[ExecutionAction, ...] = ()
    max_tool_calls: int | None = Field(default=None, ge=0)

    def merged_with(self, other: ExecutionConstraints) -> ExecutionConstraints:
        """Return the least-permissive combination of two applicable constraints."""
        if self.allowed_tools and other.allowed_tools:
            allowed = tuple(tool for tool in self.allowed_tools if tool in other.allowed_tools)
        else:
            allowed = self.allowed_tools or other.allowed_tools
        limits = (
            limit for limit in (self.max_tool_calls, other.max_tool_calls) if limit is not None
        )
        return ExecutionConstraints(
            allowed_tools=allowed,
            prohibited_tools=_unique((*self.prohibited_tools, *other.prohibited_tools)),
            local_execution_only=self.local_execution_only or other.local_execution_only,
            requires_human_review=self.requires_human_review or other.requires_human_review,
            required_evidence=_unique((*self.required_evidence, *other.required_evidence)),
            prohibited_actions=_unique((*self.prohibited_actions, *other.prohibited_actions)),
            max_tool_calls=min(limits, default=None),
        )

    def denies_tool(self, tool_name: str, external_effects: tuple[str, ...]) -> str | None:
        """Return the deterministic reason a tool is outside this authorised surface."""
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return f"tool {tool_name!r} is not in the authorised allow-list"
        if tool_name in self.prohibited_tools:
            return f"tool {tool_name!r} is prohibited by the execution constraints"
        if self.local_execution_only and external_effects:
            return f"tool {tool_name!r} has external effects under local-only execution"
        return None


def _unique[ValueT: str](values: tuple[ValueT, ...]) -> tuple[ValueT, ...]:
    """Keep declaration order while removing duplicate constraint values."""
    return tuple(dict.fromkeys(values))
