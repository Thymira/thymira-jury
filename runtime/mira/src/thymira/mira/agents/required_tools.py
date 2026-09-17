"""Deterministic evidence checks for mandatory MIRA audit tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from thymira.mira.grounding import regulation_search_result
from thymira.schemas import Event, EventType, Framework, ToolCallStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_REGULATION_SEARCH_TOOL = "search_regulation"
"""Required evidence search whose canonical result must contain at least one match."""

_APPLICABLE_REGULATION_FRAMEWORKS: Mapping[Framework, frozenset[Framework]] = {
    Framework.EU_AI_ACT: frozenset({Framework.EU_AI_ACT}),
    Framework.CREDIT_RISK: frozenset({Framework.CREDIT_RISK, Framework.GDPR}),
    Framework.GDPR: frozenset({Framework.GDPR}),
    Framework.METHODOLOGY: frozenset({Framework.METHODOLOGY}),
    Framework.MODEL_RISK: frozenset({Framework.MODEL_RISK}),
    Framework.INTERNAL: frozenset({Framework.INTERNAL}),
}
"""Regulation-result frameworks that can ground each audit framework."""


def required_tool_failures(
    events: Sequence[Event],
    task_id: str,
    required_tools: tuple[str, ...],
    *,
    audit_framework: Framework,
) -> tuple[tuple[str, str | None], ...]:
    """Return required tools without a successful, currently resolved task-scoped attempt."""
    states: dict[str, tuple[bool, bool, str | None]] = {}
    for event in events:
        if event.type not in {EventType.TOOL_COMPLETED, EventType.TOOL_DENIED}:
            continue
        if event.payload.get("task_id") != task_id:
            continue
        tool = event.payload.get("tool")
        if not isinstance(tool, str) or tool not in required_tools:
            continue
        error = event.payload.get("error")
        if not isinstance(error, str):
            reason = event.payload.get("reason")
            error = reason if isinstance(reason, str) else None
        if event.type is EventType.TOOL_DENIED:
            previously_succeeded = states.get(tool, (False, False, None))[0]
            states[tool] = (previously_succeeded, False, error or "latest attempt was denied")
            continue
        status = event.payload.get("status")
        status_value = status.value if isinstance(status, ToolCallStatus) else status
        exit_code = event.payload.get("exit_code")
        succeeded = status_value == ToolCallStatus.COMPLETED.value and (
            exit_code is None or exit_code == 0
        )
        if succeeded and tool == _REGULATION_SEARCH_TOOL:
            regulation_error = _regulation_result_error(event, audit_framework)
            succeeded = regulation_error is None
            if regulation_error is not None:
                error = regulation_error
        previously_succeeded = states.get(tool, (False, False, None))[0]
        states[tool] = (
            previously_succeeded or succeeded,
            succeeded,
            None if succeeded else error or "latest attempt did not complete successfully",
        )

    failures: list[tuple[str, str | None]] = []
    for tool in required_tools:
        state = states.get(tool)
        if state is None or not state[0]:
            error = state[2] if state is not None else "no successful completion recorded"
            failures.append((tool, error))
        elif not state[1]:
            failures.append((tool, state[2]))
    return tuple(failures)


def unattempted_required_tools(
    events: Sequence[Event],
    task_id: str,
    required_tools: tuple[str, ...],
) -> tuple[str, ...]:
    """Return required tools with no task-scoped attempt in the current agent cycle."""
    attempted = {
        tool
        for event in events
        if event.type
        in {
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_DENIED,
        }
        and event.payload.get("task_id") == task_id
        and isinstance((tool := event.payload.get("tool")), str)
    }
    return tuple(tool for tool in required_tools if tool not in attempted)


def latest_applicable_regulation_search(
    events: Sequence[Event],
    task_id: str,
    audit_framework: Framework,
) -> Event | None:
    """Return the current task's latest resolved search only when it can ground this audit."""
    latest = next(
        (
            event
            for event in reversed(events)
            if event.type in {EventType.TOOL_COMPLETED, EventType.TOOL_DENIED}
            and event.payload.get("task_id") == task_id
            and event.payload.get("tool") == _REGULATION_SEARCH_TOOL
        ),
        None,
    )
    if latest is None or _regulation_result_error(latest, audit_framework) is not None:
        return None
    return latest


def _regulation_result_error(event: Event, audit_framework: Framework) -> str | None:
    """Return why a regulation result cannot ground the audit, or ``None`` when applicable."""
    result, parse_error = regulation_search_result(event)
    if result is None:
        return parse_error or "canonical regulation result is unavailable"
    if not result.matches:
        return "no usable results recorded"

    applicable = _APPLICABLE_REGULATION_FRAMEWORKS.get(
        audit_framework,
        frozenset({audit_framework}),
    )
    observed = frozenset(match.framework for match in result.matches)
    if observed.isdisjoint(applicable):
        expected_text = ", ".join(sorted(framework.value for framework in applicable))
        observed_text = ", ".join(sorted(framework.value for framework in observed))
        return (
            f"no applicable regulation results for {audit_framework.value}; "
            f"expected one of [{expected_text}], got [{observed_text}]"
        )
    if not observed.issubset(applicable):
        expected_text = ", ".join(sorted(framework.value for framework in applicable))
        observed_text = ", ".join(sorted(framework.value for framework in observed))
        return (
            f"regulation results include inapplicable regulation frameworks for "
            f"{audit_framework.value}; expected only [{expected_text}], got [{observed_text}]"
        )
    return None


__all__ = [
    "latest_applicable_regulation_search",
    "required_tool_failures",
    "unattempted_required_tools",
]
