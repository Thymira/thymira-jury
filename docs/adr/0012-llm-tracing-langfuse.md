# ADR-0012 — LLM tracing with Langfuse: an opt-in, redacted mirror of the event log

- **Status:** Accepted — 2026-09-01
- **Deciders:** project owner; Runtime (P1); MIRA / Governance (P4)
- **Extends:** ADR-0004 (chain-of-thought is never persisted), ADR-0008 (LLM proposes, code
  authorizes), ADR-0010 (local state is authoritative)
- **Related:** `runtime/observability/README.md`, `docs/architecture/e2e-baseline-v2.md` §23

## Context

Thymira already records everything a Run did: `events.jsonl` is hash-chained, source-scrubbed for
known credentials and verifiable, and every model call leaves a `model.selected` event plus a prompt-provenance
artifact. That record answers *what happened, and can it be proven*. It answers *why was this
run slow, which step burned the context window, what did the model actually see before it
proposed that plan* only by reading raw JSONL by hand.

An LLM-observability backend answers those questions directly, and the roadmap has carried
Langfuse as a "🟡 if the integration is simple" item since the three-week plan. The question is
not whether tracing is useful but on what terms a governed runtime may send anything out of the
process at all.

Three facts constrain the answer:

1. **The record must not move.** `events.jsonl` is the evidence. A trace that could be sampled,
   dropped or switched off cannot be part of the audit trail.
2. **Nothing may leave un-redacted.** `thymira.events.redact_export` is the fail-closed boundary
   for values crossing into telemetry; a second path that bypassed it would silently reopen what
   the first one closes. Canonical events retain model-visible PII under ADR-0014.
3. **Telemetry may not fail a Run.** Langfuse buffers on background threads and `flush()`
   blocks; a backend that is down, slow or misconfigured must be invisible to the work.

The API also already owns an OpenTelemetry pipeline (`thymira.api.telemetry`) that deliberately
never registers a global `TracerProvider`. The Langfuse SDK, given no provider, claims that free
global; given the API's provider, it fans its own spans into the API's OTLP exporter as well.

## Decision

1. **One member owns Langfuse.** `thymira.observability` (`runtime/observability/`) is the only
   code that imports `langfuse`. It sits between `thymira.state` and `thymira.events` in the
   import-linter layer contract, so `agents`, `thy`, `mira`, `core` and `api` can all use it and
   it can use nothing above `events`. Its public API takes **primitives only** — no
   `LLMResponse`, no PydanticAI message — so no annotation ever forces an upward import.
2. **The trace is a non-authoritative mirror.** `events.jsonl` remains the record. A trace may be
   sampled, dropped or absent; no decision, authorization or audit conclusion may read from one.
   A span is not evidence and a Langfuse score is not a `PolicyDecision`.
3. **Redaction happens before export, inside the member.** Every `input`, `output` and `metadata`
   value passes through `thymira.events.redact_export` at one choke point. The SDK's own `mask=`
   hook is registered with the same projection as a
   second, non-authoritative line of defence. Only content that already flows through the system
   is traced: the rendered prompt, the response text, tool arguments and results. Chain-of-thought
   has no call site and therefore no path in.
4. **Explicit observations, not framework instrumentation.** Spans are opened at Thymira's own
   seams rather than by PydanticAI's OpenTelemetry instrumentation. The deciding reason is
   factual: `routed_model` returns a `FunctionModel` named `routed:{role}:{task}`, and
   `FunctionModel.request()` overwrites the response's model name with that synthetic binding, so
   native instrumentation records the routing label instead of the model that answered — and
   with it loses cost and price lookup. Reading `LLMResponse.model` in the closure is the only
   place the real id is still in hand. Explicit observations also let input and output be
   redacted per value rather than switched wholesale on `include_content`.
5. **An isolated `TracerProvider`.** Langfuse is constructed as
   `Langfuse(tracer_provider=TracerProvider(), mask=…)`, so it touches neither the API's provider
   nor the process global. OpenTelemetry's *context* is still shared, which is all that nesting
   needs.
6. **Off unless configured, and never fatal.** Tracing is enabled only when both
   `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set and `LANGFUSE_TRACING_ENABLED` is not
   `false`. Disabled, `langfuse` is never imported. Enabled, every Langfuse and OpenTelemetry call
   is isolated: a failure degrades to a no-op handle and a DEBUG log. Exceptions raised by the
   traced *body* propagate unchanged.
7. **The trace shape follows the composition.** One Run is one trace, its id derived from the Run
   id; `thy` and `mira` are `agent` observations; each sub-agent is an `agent`; each model call is
   a `generation` carrying the answering model, token counts and ingested cost; each tool call is
   a `tool` beside the generation that requested it; MIRA's deterministic evidence steps are
   `chain`s; each Policy Engine decision is a `guardrail`. The root carries the Run's own prompt
   as its input and its decision as its output, so the trace list is legible without opening
   anything. A Run parked for human review executes twice and its second episode opens as
   `run-resumed`, not as a second root named `run`: the agent-graph view groups nodes by name, so
   two roots sharing one name would render as a single node that ran twice.
   Names are low cardinality throughout — `role:task`, spec names, registry tool names, static
   phase and guardrail names — because Langfuse's aggregated graph and its saved filters key on
   them. A failed agent or model call is marked `ERROR` with a redacted status message so a
   reviewer can filter for failures rather than reading every Run's outputs. A denied tool call is
   *not* marked `ERROR`: a refusal by the Permission Policy is the system working, and conflating
   it with a crash would make the filter useless — the tool observation's own output says which
   it was.
8. **No `user_id`.** The only identity Thymira has is `Actor.id`, which in header mode is a
   caller-supplied string that may be an email. `session_id` (the Session's own id) and a
   project tag carry the grouping instead.
9. **External egress is an explicit, revocable opt-in.** ADR-0010 makes local state
   authoritative; this ADR does not weaken that, because nothing authoritative is sent. What a
   trace does carry — redacted prompts and outputs, model ids, token counts, costs, timings — is
   still a disclosure. `.env.example` therefore documents self-hosting first and names any
   `*.cloud.langfuse.com` host as external egress. Configuring no keys is a complete and
   supported deployment.

## Alternatives considered

### PydanticAI's native OpenTelemetry instrumentation

Rejected as the primary mechanism. It gives correct nesting for free, but records the synthetic
`routed:{role}:{task}` model name (see decision 4), reports no USD cost, and offers only the
all-or-nothing `include_content` switch — `True` ships raw prompts, `False` leaves a trace a
reviewer cannot read. Redacting it would require `mask_otel_spans`, whose documented failure mode
is dropping the entire export batch.

### A LiteLLM callback (`litellm.callbacks = ["langfuse"]`)

Rejected. It mutates global LiteLLM state, sees only the innermost call, and would produce
generations with no agent, phase or Run above them — the structure is the point.

### A fold over `events.jsonl` into Langfuse

Rejected for now, though it remains the most architecturally pure option: the event log is
already the redacted, ordered, verifiable record, and an exporter over it would need no call-site
changes at all. It cannot reconstruct wall-clock nesting or per-observation durations, which are
most of what a trace is for, and it would put an export step on the evidence path. Worth
revisiting as an *offline* replay tool, not as the live seam.

### Sending the trace through the API's existing OTLP exporter

Rejected. It would put LLM content into whatever generic collector the deployment points at, with
no redaction hook and no LLM-aware model.

## Consequences

- One new workspace member and one new import-linter layer; five existing members declare it.
- `langfuse` is a regular dependency of that member, not an extra: `just setup` runs
  `uv sync --all-groups --all-packages` with no `--all-extras`, so an extra would be missing from
  the type-check environment and break ty's zero baseline. The runtime no-op comes from the key
  gate, not from the install.
- Every seam pays one context-manager entry per Run, phase, agent, model call and tool call. With
  tracing off that is a `None` check.
- THY's parallel Execute wave needed one explicit fix: it submits to a bare `ThreadPoolExecutor`,
  which does not carry contextvars, so `observability.bind_context` hands the OpenTelemetry
  context to each worker. LangGraph's own dispatch already runs every node through
  `contextvars.copy_context()` and needed nothing.
- A Langfuse client is process-global (the SDK keys its singleton by `public_key`), unlike the
  API's per-app OpenTelemetry setup. `configure()` is idempotent and `reset()` exists for tests,
  but a test that wants its own exporter must use its own `public_key`.

## Considered and declined

An audit of this repository's Langfuse usage against the full documentation raised four things
that were looked at and deliberately not built. They are recorded here so they are not re-opened
as oversights.

**Model tier as a chart dimension.** Langfuse's breakdown dimensions are a fixed set — Total,
Model, Name, Level, Type, Environment — and metadata is filterable but not groupable, so a "tier
distribution" chart cannot be built by clicking. Both workarounds cost more than they return:
putting the tier in the observation `name` fragments the agent-graph nodes, and a trace-level tag
attaches a per-generation value to the whole Run. Grouping by **Model** already gives the
equivalent distribution, because tier → model is deterministic configuration (ADR-0004).

**Prompt management.** Linking a generation to a Langfuse prompt would unlock per-prompt-version
metrics, and `propagate_attributes(prompt=...)` makes it cheap. It is declined in the pulling
direction: a prompt fetched at run time can change server-side with no commit, which is exactly
what decision 2 forbids and what MIRA's audit cannot verify. If per-version metrics are ever
wanted, the safe shape is push-only — publish the checked-in prompt to Langfuse and reference the
version it was published as — and it needs its own ADR.

**Media attachments.** Langfuse renders images inline when they are wrapped as `LangfuseMedia`,
and the Visualization agent's SVG output reaches the trace as opaque text today. It is not built
because the redaction choke point has no path for media: `redact_value` cannot descend into a
`LangfuseMedia`, and the SDK uploads its bytes straight to object storage. Attaching media first
requires a rule about *which* artifact kinds may be attached — a plot rendered from aggregate data
can be, a `DATASET` or `LOG` cannot — and that rule belongs in this ADR before any code.

**MCP tool tracing.** A tool invoked over the MCP surface bypasses the `tool()` seam entirely and
ignores the W3C context Langfuse propagates through MCP's `_meta`, so an external client cannot
join its trace to Thymira's execution. Gated on the MCP adapter being wired at all; when it is,
the observation belongs in `ToolManager.execute` so every entry point is covered by construction,
rather than in each bridge.

## Future evolution

Prompt management, datasets, experiments and LLM-as-a-judge evaluation are all outside this
decision. So is the durable queue path (`RunWorker`), which needs the same `run_trace` wrap
around its own `graph.invoke`, and `risk.classify_risk` and `DecisionContextAnalyst`, which
bypass `routed_model` and are unwired today. Any of these that would let a Langfuse artefact
influence a Run — a score gating a decision, a prompt fetched at run time — requires its own ADR:
decision 2 forbids it as written.
