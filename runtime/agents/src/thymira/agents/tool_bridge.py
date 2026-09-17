"""Tool bridge: agent tool-calling through the Tool Manager + Gate under an allowlist (THY-04).

Enforces 'every tool call goes through the Tool Manager + Permission Policy'. Only
`spec.tool_allowlist` is ever exposed to the model as a callable tool — a tool the registry holds
but the spec does not list is never built, so an agent cannot even attempt it. A denied capability
comes back from `ToolManager.execute` as a `ToolExecution` whose result carries the reason, never
an exception, so a PydanticAI tool call here can only ever produce a bounded error the model can
read and adapt to, not a crash of the run — unless a human still has to answer it, in which case
the step ends with `ApprovalRequired` instead (`ToolExecution.pending_approval`, Task 1).

`usage` (THY-07) is optional: given, every call — attempted, denied or failed alike, since each one
went through the Tool Manager pipeline regardless of outcome — charges one against
`RunUsage.tool_calls`, raising `UsageLimitExceededError` the moment `max_tool_calls` is crossed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai import ApprovalRequired, Tool, ToolReturn

from thymira.agents.history import TOOL_RETURN_SUCCESS_METADATA_KEY
from thymira.agents.prompt_framing import frame_untrusted
from thymira.observability import tool as tool_observation
from thymira.schemas import EventType, ToolCallStatus
from thymira.tools import ToolExecution, ToolManager, canonical_value, input_schema, value_text

if TYPE_CHECKING:
    from thymira.agents.spec import AgentSpec
    from thymira.agents.usage import RunUsage
    from thymira.tools import Tool as ToolContract
    from thymira.tools import ToolContext, ToolRegistry

DENIED_MARKER = "[denied]"
"""The last line of every denied call's rendering: the fact the model anchors on to tell "not
allowed" from "failed". A denied call is not retried unchanged; the agent adapts or reports."""


def build_agent_tools(
    spec: AgentSpec,
    registry: ToolRegistry,
    context: ToolContext,
    *,
    usage: RunUsage | None = None,
) -> tuple[Tool[None], ...]:
    """Build the PydanticAI tools `spec.tool_allowlist` authorizes, bound to one `ToolContext`.

    Raises:
        KeyError: `spec.tool_allowlist` names a tool `registry` does not hold — a spec/registry
            mismatch this refuses to paper over by silently registering fewer tools than declared.
    """
    manager = ToolManager(registry)
    tools: list[Tool[None]] = []
    for tool_name in spec.tool_allowlist:
        registered_tool = registry.get(tool_name)
        tools.append(_build_one_tool(manager, context, registered_tool, usage))
    return tuple(tools)


TRACE_OUTPUT_LIMIT = 1024 * 1024
"""How much of a tool's output reaches the trace. The agent still receives all of it.

`write_file` refuses more than a megabyte before it writes, but `run_python` captures whatever the
subprocess printed with no cap at all, and both feed the same observation. A step that forgets to
bound a `print` — a whole dataframe, or the SVG buffer the Visualization agent is told to emit —
would hand Langfuse a multi-megabyte output, run it through a full redaction scan on the way, and
meet Langfuse Cloud's 5 MB per-request ceiling with nothing able to truncate it: the SDK batches by
event count, never by size, and dropped its own size cap in v3.
"""

_SHA256_LENGTH = 64


def _bounded(output: str) -> str:
    """What the trace records — the agent's own context is never shortened by this."""
    if len(output) <= TRACE_OUTPUT_LIMIT:
        return output
    return f"{output[:TRACE_OUTPUT_LIMIT]}…[truncated, {len(output)} characters in full]"


STDERR_TAIL_LINES = 40
"""How many trailing stderr lines the model sees; the full text stays on the ToolCall."""


def render_tool_output(execution: ToolExecution) -> str:
    """Render the one text projection of a tool result the model reads.

    The order is fixed so a reader (model or parser) can anchor on the last line: a denied or
    failed call is ``Error: <reason>``; a successful call is its stdout or ``(no output)``; a
    non-empty stderr follows as a bounded tail; the exit-code marker comes next, the way dsh's
    ``[exit code: N]`` marker is anchored, when the tool ran a process; a denied call appends
    ``[denied]`` after everything else, so ``[denied]`` -- not the exit-code marker -- is the
    last line whenever the call was denied.
    """
    result = execution.result
    lines: list[str] = []
    text = value_text(result.value) if result.value is not None else result.stdout
    # Once a typed value exists, the model must see only fields reconstructable from the
    # canonical value.  Legacy envelope stderr is retained for direct scalar callers, but a
    # producer's unmodelled auxiliary field must not cross this bridge.
    stderr = result.stderr
    if result.value is not None:
        value_payload = canonical_value(result.value)
        projected_stderr = value_payload.get("stderr", "")
        stderr = projected_stderr if isinstance(projected_stderr, str) else ""
    if not result.success:
        lines.append(f"Error: {text or result.error or 'tool call failed'}")
    else:
        lines.append(text.rstrip("\n") if text.strip() else "(no output)")
    if stderr.strip():
        stderr_lines = stderr.rstrip("\n").splitlines()
        if len(stderr_lines) > STDERR_TAIL_LINES:
            stderr_lines = [
                f"[stderr truncated to the last {STDERR_TAIL_LINES} lines]",
                *stderr_lines[-STDERR_TAIL_LINES:],
            ]
        lines.append("")
        lines.append("stderr:")
        lines.extend(stderr_lines)
    if result.exit_code is not None:
        lines.append(f"[exit code: {result.exit_code}]")
    if execution.call.status is ToolCallStatus.DENIED:
        lines.append(DENIED_MARKER)
    return "\n".join(lines)


def _build_one_tool(
    manager: ToolManager, context: ToolContext, tool: ToolContract, usage: RunUsage | None
) -> Tool[None]:
    tool_name = tool.name

    def _call(**arguments: Any) -> ToolReturn[str]:
        with tool_observation(name=tool_name, arguments=arguments) as observation:
            execution = manager.execute(context, tool_name, arguments)
            if usage is not None:
                usage.charge_tool()
            output = render_tool_output(execution)
            framed_output = frame_untrusted(output, label="tool-output")
            observation.update(output=_bounded(framed_output))
            if execution.pending_approval is not None:
                # The step ends here, cleanly: PydanticAI hands back `DeferredToolRequests`
                # instead of looping, the runner records the step as PENDING, and the runtime --
                # never the model -- decides when this exact call runs (`ToolManager`'s ticket).
                identity = _pending_tool_identity(context, execution.call.id)
                raise ApprovalRequired(
                    metadata={
                        "decision_id": execution.pending_approval.id,
                        "tool_call_id": execution.call.id,
                        **identity,
                    }
                )
            return ToolReturn(
                framed_output,
                metadata={TOOL_RETURN_SUCCESS_METADATA_KEY: execution.result.success},
            )

    return Tool.from_schema(
        _call,
        name=tool_name,
        description=tool.description,
        json_schema=input_schema(tool),
    )


def _pending_tool_identity(context: ToolContext, tool_call_id: str) -> dict[str, str | None]:
    """Read the exact ticket and mode the Tool Manager recorded for a pending call."""
    event = next(
        (
            event
            for event in reversed(context.event_log.events())
            if event.type is EventType.TOOL_DENIED
            and event.payload.get("tool_call_id") == tool_call_id
        ),
        None,
    )
    if event is None:
        raise RuntimeError("pending tool call has no denial evidence")
    ticket = event.payload.get("tool_intent_sha256")
    if not isinstance(ticket, str) or len(ticket) != _SHA256_LENGTH:
        raise RuntimeError("pending tool call has no valid ticket evidence")
    mode = event.payload.get("sandbox_mode")
    if mode is not None and not isinstance(mode, str):
        mode = getattr(mode, "value", None)
    if mode is not None and not isinstance(mode, str):
        raise RuntimeError("pending tool call has invalid sandbox mode evidence")
    return {"tool_intent_sha256": ticket, "sandbox_mode": mode}


__all__ = ["DENIED_MARKER", "build_agent_tools", "render_tool_output"]
