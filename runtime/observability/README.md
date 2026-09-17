# thymira-observability

LLM tracing: the single place that knows about Langfuse. It exposes context managers for the
seams a Run already passes through — the Run itself, the THY and MIRA phases, each sub-agent,
each model call, each tool call, each Policy Engine decision — and turns each of them into one
Langfuse observation of the right type. Inputs and outputs use the strict
`thymira.events.redact_export` projection before they leave the process; unsupported values fail
closed. The authoritative event log keeps exact model-visible payloads after source credential
scrubbing so its hash chain can reconstruct the request.

The trace is a **non-authoritative mirror**. `events.jsonl` stays the record: it is hash-chained,
verifiable and local (ADR-0010), while a trace is best effort and may be sampled, dropped or
turned off. Nothing here may fail a Run — every Langfuse and OpenTelemetry call is isolated, and
a helper that cannot record simply yields a no-op handle (ADR-0012).

| | |
|---|---|
| Import name | `thymira.observability` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) |
| MVP priority | P2 |
| Location | `runtime/observability/` |

## Status

Implemented and tested: `configure`/`reset`/`is_enabled`/`flush`/`shutdown`, the seven observation
helpers (`run_trace`, `phase`, `agent`, `generation`, `tool`, `evidence`, `guardrail`),
`update_current_generation` and `bind_context` (`tests/thymira/test_observability.py`, and
`tests/thymira/test_observability_integration.py` for the span tree of a whole scripted Run).

## Layer

It sits between `thymira.state` and `thymira.events` in the import-linter layer contract: it
imports only `thymira.events` and third-party code, and everything above it — `agents`, `thy`,
`mira`, `core`, `api` — imports it. Its public API takes **primitives only**, never an
`LLMResponse` or a PydanticAI message, so no annotation ever needs an upward import.

## Configuration

Off unless `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set in the process
environment (`LANGFUSE_TRACING_ENABLED=false` hard-disables it anyway). Disabled, `langfuse` is
never even imported. See `.env.example`.

## What a trace can still carry

The redaction is a safety net, not a guarantee. The patterns are anchored on shape and prefix:
emails, phone numbers, cards, IBAN, DNI, JWT/Bearer, and keys that announce themselves (`sk-`,
`pk-`, `token-`, …). They do **not** match a personal name, a postal address, a date of birth, or
an unprefixed credential such as a bare hex token or `password=…`. A data agent that prints sample
rows will have the emails in trace fields scrubbed and the names beside them not. The local log
may retain those exact rows under its verified restrictive permissions; API/export projections
redact their copies.

That is an accepted design point. A deployment sending traces to a hosted Langfuse while handling
regulated data should either self-host it — `LANGFUSE_BASE_URL` is all that takes — or harden the
`mask=` hook, which can drop or scan values on the way out *without* changing what `events.jsonl`
records. See ADR-0014 for the canonical-log and redacted-projection boundary.
