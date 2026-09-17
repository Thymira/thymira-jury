"""Langfuse tracing for Thymira: redacted observations at the seams a Run already passes through.

`events.jsonl` stays the authoritative record; a trace is a best-effort mirror that is off unless
configured and can never fail the Run it observes (ADR-0012). See `tracing` for the whole design.
"""

from thymira.observability.tracing import (
    Handle,
    agent,
    bind_context,
    configure,
    cost_details,
    evidence,
    flush,
    generation,
    guardrail,
    is_enabled,
    phase,
    reset,
    run_trace,
    score_run,
    shutdown,
    tool,
    trace_url,
    update_current,
    update_current_generation,
    usage_details,
)

__all__ = [
    "Handle",
    "agent",
    "bind_context",
    "configure",
    "cost_details",
    "evidence",
    "flush",
    "generation",
    "guardrail",
    "is_enabled",
    "phase",
    "reset",
    "run_trace",
    "score_run",
    "shutdown",
    "tool",
    "trace_url",
    "update_current",
    "update_current_generation",
    "usage_details",
]
