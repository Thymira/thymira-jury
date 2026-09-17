# DSH harness acceptance record

## Purpose and status

This record turns the accepted DeepSeek Harness (DSH) reading into closure criteria. It
does not approve an implementation, alter policy, or introduce a delivery plan.

Source: *Thymira · lectura de deepseek-ai/deepseek-harness en 76fda72* (2026-09-03),
§3 F1–F13, §5 model-facing contracts, §6 exclusions, §10 annex mechanisms and §11 decisions
(the **DSH report**);
[ADR-0013](../../adr/0013-harness-fundamentals-six-decisions.md).

Status meanings:

- **OPEN** — required before the corresponding claim can be made.
- **BLOCKED** — an explicit repository decision is needed; this record does not choose one.

An existing module, ADR, or test is a **mechanism anchor**, not proof that a criterion has
closed. At closure, every identifier below records one evidence tuple:

1. the producer and concrete call site that emit or enforce the fact;
2. a real behavioral test node exercising that call site; and
3. an independent oracle where the property requires a relationship between observations
   (event replay, persisted artifact verification or policy/MIRA recomputation).

The closure record must also give the command and its result. A unit assertion against the
same object that produced the fact is not an independent oracle.

## Cross-cutting rules

The MVP's 82 completed tasks do not close this program. F1–F13 remain open until every
accepted COPY/ADAPT criterion has the required evidence. No percentage, completed subset or
PR count compensates for an unmet criterion. F4 and F12 still require closing audits. Each
F heading maps to the same heading in report §3; accepted annex additions remain obligations.

**G.1 [ADVANCED] — F1 policy reconciliation.** The owner ruling recorded in
[ADR-0014](../../adr/0014-log-and-redaction-boundary.md) on 2026-09-08 accepts ADR-0013 decision
1 and defines four sub-decisions: canonical logs preserve model-visible values with source
credential scrubbing; Run storage is protected by verified restrictive permissions; API/SSE/file
exports and traces are fail-closed redacted projections; and MIRA findings retain references and
computed facts without copying raw personal data. Reasoning text remains forbidden. Behavioral
evidence covers JSONL/in-memory/PostgreSQL builders, prompt and provider call sites, ToolManager,
LocalRunStore/LocalEventStore, API/SSE, traces and MIRA finding producers. The F1 foundation remains
open until its other independent criteria close.

**G.2 [OPEN] — claims follow observed facts.** Security, lifecycle, and completion claims
must distinguish requested configuration, backend enforcement, and actual outcome. Unknown
or malformed evidence fails closed; a successful API response alone is not proof.

**G.3 [OPEN] — closure evidence stays durable.** Facts needed by MIRA, policy, recovery, or
later audit are recorded in canonical events or artifacts before a caller can report success.

## F1 — model input, observations, and format

Mechanism anchors: `packages/events`, `runtime/agents`, `runtime/observability`,
`runtime/policies`; ADR-0013 decisions 1, 3, and 6.

**F1.1 [ADVANCED] — resolve the raw/log and redaction boundary.** The accepted policy excludes
known credentials at source and in canonical event builders, preserves canonical model-visible
values for request reconstruction, verifies restrictive local storage permissions, and fails
closed when an export/API/trace value cannot be classified. `redact_export` handles values and
keys, rejects collisions and non-finite numbers, and the assurance projection omits embedded raw
events while verification uses the canonical chain as its independent oracle. Behavioral tests
exercise exact PII/numeric preservation, known-credential absence, API/SSE/export/trace redaction,
MIRA finding sanitization, and real Windows ACL or POSIX mode evidence. F1.5 remains OPEN and is not
claimed here.

Evidence tuple for this slice (2026-09-08): the producers are the canonical event builders,
prompt/model call sites, `ToolManager`, `LocalRunStore`/`LocalEventStore`, API JSON/SSE routes,
Langfuse tracing and MIRA finding copy boundaries; behavioral coverage is exercised by the focused
events, state, API, agent, MIRA and tool tests; independent checks are `verify_events`/`verify_log`,
the API test's separate canonical event reader, assurance verification against externally supplied
events, and the OS permission evidence returned by the storage helper. This tuple advances G.1 and
F1.1 only; it does not close F1.2-F1.6 or claim F1.5 request reconstruction.

**F1.2 [CLOSED 2026-09-08] — reject foreign log formats.** `events.jsonl` has a global monotonically
increasing format version. Readers reject unsupported versions and unknown event types unless
explicitly ignorable; before 1.0 there are no guessed compatibility paths or migrations.

**F1.3 [OPEN] — preserve assistant chunk provenance.** Logged chunks fold into messages
carrying source event sequences and usage. Replay reconstructs message order and content
from those chunks instead of trusting transport arrival order.

**F1.4 [OPEN] — preserve reasoning metadata without reasoning text.** Records may retain
reasoning presence, token count, and digest, but never chain-of-thought content. Absence,
redaction, and an invalid digest remain distinguishable.

**F1.5 [OPEN] — reconstruct every actual provider request.** Every model-visible input has
one durable event owner. Record complete request headers initially and whenever they change,
with the reason; project the ordered append/replace surface from the log. An independent
checker rebuilds every sent request, including messages, schemas and model settings, and
compares it with the provider-bound request. G.1 determines the reconciled data policy.

**F1.6 [OPEN] — isolate provider-required opaque replay state.** Only a provider requirement
justifies adapter-owned unreadable state; exclude it from MIRA's readable corpus and never
persist legible reasoning as replay state.

Closure tuple: producer/call site is the observation/event writer and every model/trace
emitter; test node covers unknown-version rejection, chunk ordering, secret exclusion, and
reasoning-content absence; independent oracle replays the canonical event stream and checks
the exported/trace surface independently.

**2026-09-08 — F1.2 CLOSED.** The event log now has one global supported format authority and
refuses foreign serialized evidence before replay, append or consumer dispatch. `Event.schema_version`
is strict and owned by `thymira.schemas.EVENT_LOG_FORMAT_VERSION` (`"0.3"`); the shared
`deserialize_event` validates the required version and closed `EventType` vocabulary before model
construction, while `verify_events` applies the same refusal to in-memory records before hashing.
JSONL reopen and PostgreSQL read paths use that deserializer. PostgreSQL append takes its
transaction-scoped run lock, validates the complete persisted prefix and its hash/sequence chain on
that same connection, and only then inserts the next event. No compatibility shim or migration is
present. Focused tests cover re-chained foreign, mixed-version, missing, malformed and boolean
versions, unknown types, reopen, append-time recheck and in-memory verification. Their independent
oracle mutates raw JSON records and recomputes hashes directly, without calling the reader; the
reopen case also proves refusal leaves file bytes unchanged. The PostgreSQL review probe uses a
mocked connection to verify that malformed, foreign and broken prefixes result in no insert. F1
remains open because F1.3–F1.6 remain open, and F1.1 stays advanced under G.1.

## F2 — run and queue lifecycle

Mechanism anchors: `runtime/core`, `runtime/thy`, `runtime/mira`, `packages/schemas`.

**F2.1 [OPEN] — reach MIRA on failure.** Define and prove the route to MIRA for successful,
failed, cancelled and recovered interrupted THY execution; no swallowed exception may bypass
the audit. RunController remains the sole persistent Run transition writer.

**F2.2 [OPEN] — preserve pending work through cancellation.** Cancellation retains the
pending queue and its ordering for safe later recovery. It cannot relabel unstarted work as
completed.

**F2.3 [OPEN] — recover unknown outcomes conservatively.** An interrupted execution whose
outcome is unknown is recorded as unknown and recovered through a safe decision path, not a
retry inferred from missing output. Distinguish never-dispatched calls from calls with unknown
effects; retry the latter only after idempotency/read-only checks or external-state/human review.

**F2.4 [OPEN] — make pool behavior bounded and exclusive.** Bounded workers have exclusive
claims, release them durably, and return results in the defined order despite completion
order.

**F2.5 [OPEN] — claim a durable two-lane inbox.** Persist next-turn and next-step inputs with
followup/steer/inject semantics and an atomic transfer on claim. A rejected first claim still
closes a durable zero-step turn; one pre-step interception point determines model input.

**F2.6 [OPEN] — close every turn with a reason.** Every exit records a terminal reason.
Tool errors become model-visible results, driver failures retain their cause and end the turn,
and cancelled undispatched calls receive an aborted-before-dispatch result. Continuation
follows recorded data, not listener order. Host pause aborts the live turn with its initiator
recorded; cancellation preserves queued work it does not target.

Closure tuple: producer/call site is the Run transition writer plus queue claim/release path;
test node exercises failure, cancellation, interruption, and concurrent completion; independent
oracle folds durable events/state and verifies MIRA entry, pending work, exclusive ownership,
and ordered results.

## F3 — filesystem and tool projections

Mechanism anchors: `runtime/tools`, `runtime/state`, `packages/schemas`.

**F3.1 [OPEN] — bound actual file-tool output.** Read, glob, and grep enforce documented line,
character, and byte limits at the producing tool, including boundary and multi-byte cases.

**F3.2 [OPEN] — persist complete ordered discovery results.** Glob and grep expose a stable
order and persist the complete authoritative match list even when a caller-facing rendering is
bounded.

**F3.3 [OPEN] — enforce the read-before-edit policy.** A configured freshness policy requires
a prior read and returns `FS_STALE_VERSION` for stale content. Literal edits require exactly
one match unless replace-all is explicit; error codes retain immediate recovery guidance.

**F3.4 [OPEN] — enforce restrictions at dispatch.** Omitting a tool from the advertised schema
is not access control. Every call passes Tool Manager validation, monotonic guards, execution,
post-processing, finalization and result recording. Guards only narrow authority and their
ordering cannot change authorization.

**F3.5 [OPEN] — project every tool result canonically.** Every tool has a validated output
schema and canonical typed result. Model text and UI representations are pure projections
of that value, not separately authored strings. Test every production call site.

**F3.6 [OPEN] — declare honest tool contracts.** Effectful tools require a concise description.
Capabilities describe code execution, data/filesystem scope, network, side effects and
concurrency safety truthfully. Read/write/edit/glob/grep expose their documented behavior,
including line numbering, paging, total counts and complete discovery-list artifact references.

Closure tuple: producer/call site is each concrete file tool and registry projection builder;
test node covers byte/line boundaries, reorderings, stale writes, omitted fields, and all-tool
registration; independent oracle reads persisted lists/artifacts and recomputes the projection
from the registry contract.

## F4 — prompts and context assembly

Mechanism anchors: `runtime/agents`, `runtime/thy`, `runtime/mira`.

**F4.1 [OPEN] — separate static from runtime context.** Static owner and policy material is
versioned; runtime snapshots are event-backed and superseded explicitly. A prompt cannot
silently combine incompatible versions.

**F4.2 [OPEN] — give each prompt fact one owner.** Identity, configured model, cwd, deployment
guidance and each tool-family paragraph have one owner and event provenance. Runtime snapshots
explicitly supersede older ones, including an empty snapshot that removes obsolete context.

**F4.3 [OPEN] — audit all prompt entry points.** Verify every provider call and changing
catalogue for complete replacement and ownership; compare the request reconstructed under
F1.5 with the request actually sent. Existing persona/guidance tests do not close this audit.

Closure tuple: producer/call site is the prompt/context assembler; test node varies stale and
superseded snapshots; independent oracle rebuilds the request under the policy reconciled in
G.1 and compares it with what was sent.

## F5 — approvals and adaptation

Mechanism anchors: `runtime/policies`, `runtime/tools`, `runtime/core`; ADR-0013 decision 4.

**F5.1 [OPEN] — close approval scopes fail closed.** Scope release, client disconnect, caller
abort, expiration, and consumption close an approval so it cannot authorize a later call.

**F5.2 [OPEN] — adapt only the parked intent.** Later review may park and resume the exact
intent. It cannot select a new model, sandbox mode, or materially different arguments. A retry
feature is not accepted as a TODO or implied future exception.

ADR-0013's later Consequences records the deliberate adaptation of its original decision 4:
authenticated human approval authorizes the persisted mode-bound call once; resume executes
it without a second model invocation or model-selected mode. Rejection is final for that
intent. This replaces the upstream same-turn retry mechanism for this implementation.

**F5.3 [OPEN] — preserve independent OS enforcement.** A policy approval remains distinct
from sandbox/backend enforcement facts and cannot widen a tool's declared capability.

**F5.4 [OPEN] — enforce one-shot and never semantics.** Use closed outcomes for allowed-once,
rejected, cancelled and unavailable. Check `never` before any responder; delegated scope cannot
widen itself. Human questions require live root-agent/user authority, not a caller label or
durable lineage alone.

**F5.5 [OPEN] — enforce each requested mode.** Read-only, workspace-write and explicit
development danger-full-access are separate from approval policy. Unavailable requested
confinement refuses execution; denial is a tool result, with no unconfined fallback.

Closure tuple: producer/call site is Gate/ticket issuance, consumption, and Tool Manager
dispatch; test node covers scope release, disconnect, abort, expiration, replay, and resumed
intent mismatch; independent oracle recomputes the intent hash and policy route from durable
approval records.

## F6 — subprocess, sandbox, and Git execution

Mechanism anchors: `runtime/tools/src/thymira/tools/sandbox`, `runtime/policies`; ADR-0013
decision 2.

**F6.1 [OPEN] — enforce the actual resource boundary.** CPU and RLIMIT where the platform
supports them, memory, PID count, workspace quota, and network restrictions have explicit
backend applicability and evidence. Unsupported controls are not reported as enforced.

**F6.2 [CLOSED 2026-09-07] — resolve specifications at runtime.** The backend records the
resolved execution specification and non-secret environment facts rather than trusting a request
object. Runtime-owned values supersede caller inputs. Exclude credentials at source; a project
`.env` cannot control PATH, preload or proxy startup variables.

**F6.3 [OPEN] — handle hostile execution protocols.** Container/process protocol frames,
private or unpredictable result paths, and output are validated and bounded before parsing or
publication.

**F6.4 [CLOSED 2026-09-08] — clean up with identity evidence.** Reap is bounded; PID reuse cannot
target an unrelated process; a live profile/probe confirms backend state when the platform can
provide it.

**F6.5 [OPEN] — cover every executable surface.** Git and all other subprocess/executable
paths use the same fail-closed capability and outcome evidence rules.

**F6.6 [CLOSED 2026-09-07] — make orthogonal outcomes truthful.** Requested mode, actual mode,
enforcement status, exit outcome, and cleanup result remain separate. PR127-style behavior
must not claim `FULL` merely because an execution completed.

**F6.7 [OPEN] — complete production confinement and its E2E proof.** Select the backend in
runtime configuration and exercise Python, training, inspection, model loading/prediction, Git
and every executable surface through Tool Manager. Prove effective bind-workspace quotas plus
filesystem, network, process, resource and timeout boundaries independently. A19 remains open
while the applicable backend is PARTIAL; PR127's 8-MiB host capture bound proves only that bound.

**F6.8 [OPEN] — contain the Python bootstrap and cleanup.** Apply isolated/unbuffered Python
and applicable strict resource limits, private temporary storage, separate validated control
output and primitives model code cannot replace. Termination reaches quiescence through bounded
tree/group cleanup with PID-identity protection; timeout/interruption remain visible even if
the child exits zero. Spill files are private and verifiable; link-shaped paths cannot redirect
writes. Report the final exit marker consistently.

Closure tuple: producer/call site is every sandbox backend and executable adapter; test node
covers hostile frames, limits, output overflow, cleanup/PID-reuse seams, Git, and unavailable
controls; independent oracle checks backend evidence plus MIRA/policy recomputation against
the declared capability and resolved spec.

## F7 — subagents and result settlement

Mechanism anchors: `runtime/agents`, `runtime/core`, `runtime/thy`, `runtime/mira`;
ADR-0013 decision 5.

**F7.1 [CLOSED 2026-09-08] — close the stop reason enum.** Terminal subagent reasons are exactly
`completed`, `stopped`, `out-of-room`, `declined`, `failed`, or `abnormal`; unknown values are
rejected.

**F7.2 [OPEN] — preserve bounded diagnostics and parent settlement.** Diagnostics are bounded
and redacted, while the parent settlement is durable and selects one canonical result.

**F7.3 [OPEN] — keep depth and delegation monotonic.** Delegation depth is persisted and
monotonic. The orchestrator owns a CAS DAG board for claims and dependencies.

**F7.4 [OPEN] — keep MIRA independent.** MIRA reads independent evidence before its critic
step. A coding harness receives no authority handoff and cannot become an approval or policy
writer.

**F7.5 [OPEN] — validate one uniform SubagentResult.** Every child settles with a closed typed
result rather than rejecting its parent. One shared selector serves all consumers; validate
schema-constrained completion once, with bounded diagnostic fields and a durable settlement
notice to the parent's inbox. Each invocation has exactly one durable settlement; retries and
forks mint fresh task and agent invocation identities, while choosing across distinct attempts is
left to a higher-level consumer. Neither attempt inherits extra authority.

**F7.6 [OPEN] — keep the orchestrator the only peer boundary.** Siblings share artifacts and
an orchestrator-owned CAS/DAG board, never peer messages. MIRA auditors see evidence but not
sibling findings until the orchestrator's critic phase. A coding_harness adapter needs a named
consumer and must enforce the same authorization boundary for every delegated call.

Closure tuple: producer/call site is the orchestrator board, parent settlement writer, and
MIRA audit entry; test node covers each stop reason, competing results, depth races, and
authority-handoff attempts; independent oracle replays the board/events and verifies canonical
selection plus MIRA's separate evidence inputs.

## F8 — checkpoints and compaction

Mechanism anchors: `runtime/state`, `runtime/core`, `packages/events`,
[ADR-0007](../../adr/0007-compaction-is-atomic-no-bracket.md).

**F8.1 [OPEN] — append compaction once and atomically.** Compaction uses ADR-0007's single
atomic `context.compacted` append. It creates no separate bracket or log-parenthesis record.

**F8.2 [OPEN] — preserve tool-call/result safety.** Pruning cannot detach a tool call from its
result, evidence, or required authorization context.

**F8.3 [OPEN] — make large-output pruning deterministic.** The pruner emits head, marker, and
tail deterministically, records its cost, and never relies on a model to choose omitted bytes.

**F8.4 [OPEN] — checkpoint exactly the source sections.** The source checkpoint has exactly
eight sections: Primary Request and Intent; Key Technical Concepts; Files and Code; Errors
and Fixes; Pending Jobs; Current Work; Next Step; Critical Context. Preserve exact identifiers,
user corrections and still-current work, merge prior checkpoints without stale facts, and
emit a continuation header. Summarization has no tool side effects.

**F8.5 [OPEN] — retry overflow only after proven progress.** Context-overflow retry requires
durable evidence that compaction or reduction made progress; repeated unchanged retries stop.

**F8.6 [OPEN] — wire end-to-end context recovery.** The running graph reads the compacted
checkpoint rather than leaving compaction as an isolated utility.

**F8.7 [CLOSED 2026-09-07] — isolate checkpoint namespaces.** Every `get`, `put`, `put_writes`, and `list`
uses the composite `(thread_id, checkpoint_ns)` identity. Tests write two namespaces sharing
a thread in both write orders and prove independent latest checkpoints and pending writes.
This fix precedes any two-subgraph shared-thread use, regardless of delivery order.

Closure tuple: producer/call site is checkpoint storage plus graph recovery; test node covers
atomic append, call/result pairing, deterministic boundary output, all eight sections, no-
progress overflow, and two subgraphs sharing a thread; independent oracle replays events and
reads storage through a separately constructed namespace/query order.

## F9 — skill and catalog loading

Mechanism anchors: `runtime/agents`, `runtime/thy`, `runtime/mira`, `scripts/validate_skills.py`.

**F9.1 [OPEN] — load runtime catalogs separately from developer skills.** THY/MIRA
catalog and body loading is runtime behavior; it is distinct from developer skill discovery and
has no forced canonical development-tree consumer. `RuntimeSkillCatalog` receives explicit
composition-root layers, while `load_mira_skill_catalog` keeps MIRA's entry point separate from
the developer `.agents/skills` tree. THY and MIRA call their own configured catalog through the
Core graph factory.

**F9.2 [OPEN] — replace catalogs completely and deterministically.** A changed
catalog causes a full replacement, applies ordered low-to-high precedence and explicit removals,
and does not retain stale entries. The Core factory reloads configured roots for every Run.

**F9.3 [OPEN] — budget whole files safely.** Byte budgeting omits whole broader
files first; any final truncation is explicit and measured in UTF-8 bytes. More specific files
take precedence without overriding system, developer or direct user instructions. Changes and
removals are recorded, frame delimiters are escaped behind a fresh 128-bit nonce, and
cross-session snapshots are explicitly framed as untrusted read-only data. This closes the
catalog framing seam; F13.1's broader execution protocol remains separately governed.

**F9.4 [OPEN] — load instructions only after selection.** THY Plan exposes names
and descriptions first and writes selected names into `AgentTask`; Execute's `AgentRunner` then
loads the selected body and references. MIRA uses selected names on `AuditAgentSpec` or its
factory configuration and resolves them in the audit runner. A catalog is prompt context rather
than authority, and changed catalogues replace earlier lists completely.

Closure tuple: producer/call site is `RuntimeSkillCatalog` plus the Core, THY Execute and MIRA
audit runner wiring; test node is `tests/thymira/test_agents_runtime_catalog.py` covering changed
catalogs, removals, precedence, budget boundaries, hostile text, and both orchestrators;
independent oracle reloads the persisted `LocalRunStore` export, verifies source-layer and file
digests, and compares the selected provider projection digest.

## F10 — plans, goals, and human provenance

Mechanism anchors: `runtime/core`, `runtime/thy`, `runtime/policies`, `packages/schemas`.

**F10.1 [OPEN] — retain the complete durable board.** Todo and in-progress items remain in the
durable plan while work is pending. `todo_write` replaces the entire list; pending work retains
an in-progress item and completions are recorded promptly. Skip todo tracking for trivial work.

**F10.2 [OPEN] — record human goal provenance.** Create/edit actions record human actor,
goal, revision, and causal evidence. A blocked state is allowed only after the configured N
consecutive rounds for the same reason, not a changing sequence of failures. Creation/editing
requires root human provenance; models and subagents cannot manufacture that authority.

**F10.3 [OPEN] — guard repeated calls and timeouts.** Count consecutive identical tool names
and canonical arguments, then emit the accepted escalating reminders. Timeout is a canonical
`TOOL_TIMEOUT` result with its budget and distinct abort semantics. Goal rounds have bounded
autonomous continuation and durable revision state.

**F10.4 [OPEN] — keep plan artifacts advisory.** A plan artifact can contain explicit review
feedback but is never execution authority or an implicit approval.

Persist plan mode and the full plan artifact; leaving it requires an explicit human review
response. Continued-planning feedback returns as a tool result. Conversational agreement does
not consume the execution Gate or a tool-call ticket.

Closure tuple: producer/call site is plan/goal persistence and the human-facing mutation path;
test node covers pending work, revisions, repeated calls, blocked-round counting, and plan
feedback; independent oracle replays durable revisions and applies policy authorization
separately from the plan artifact.

## F11 — packages, registries, and invariants

Mechanism anchors: workspace package metadata, registry/checker implementation, and
`scripts/lint_imports.py` where applicable.

**F11.1 [OPEN] — name each package and invariant exactly.** Each claimed package has its exact
distribution/import name, registry/checker, invariant, and owner; if any is intentionally
absent, the record gives a scoped justification.

**F11.2 [OPEN] — observe invariants twice.** Append/replay and a second independent observation
both verify the invariant. Invalid inputs have explicit rejection tests.

**F11.3 [OPEN] — admit observable relationships only.** A shape check or two readers of the
same success flag are not independent evidence. Package companions compare independently
produced facts, run at append and replay, and report an attributed stable failure code. Apply
this rule to MIRA controls and F1.5's reconstructed-versus-sent request invariant.

Closure tuple: producer/call site is the named registry/checker; test node covers valid,
invalid, and replay inputs; independent oracle is a second reader/checker that did not produce
the original observation.

## F12 — simplification and audit boundaries

Mechanism anchors: `runtime/mira`, `runtime/policies`, `packages/events`, repository Agent Notes.

**F12.1 [OPEN] — audit for zero LIVE callers.** Any removal starts with a complete LIVE-caller
audit rather than an assumed dead path.

**F12.2 [OPEN] — justify every simplification.** A simplification has no tombstone, at least two
named consumers before generalization, an Agent Note, applicable gates, and replay oracles.

**F12.3 [OPEN] — preserve resource and reasoning boundaries.** Resource ownership is explicit;
tests verify that chain-of-thought does not leak into events, artifacts, prompts, diagnostics,
or audit surfaces.

Closure tuple: producer/workflow is the named removal review, Agent Note, and enforcing gate
consumer rather than a fictitious runtime caller; test node is a failing/valid workflow or
gate check plus resource/reasoning isolation test; independent oracle checks live runtime
consumers and event/artifact effects. Repository search alone cannot prove zero live callers.

**F12.4 [OPEN] — audit notes, gates and recorded acceptance.** Non-trivial decisions have the
fixed Problem, Decision, Alternatives considered and Consequences headings and name what the
decision beat. Every mechanically checkable repository promise has a command that fails on
violation. Replay sessions without keys using ScriptedProvider, readable prompt/schema sidecars
and an independent workspace oracle. A simplification records `needs a named consumer` before
reintroduction. Review denial paths, resource ownership and reasoning leakage as behavior;
neither a skill's existence nor an earlier acceptance PR closes these audits.

## F13 — session, API, and durable transport

Mechanism anchors: `apps/api`, `runtime/core`, `packages/events`, `packages/schemas`.

**F13.1 [OPEN] — frame untrusted prompt text.** Tool outputs, dataset samples, imported
feedback and MIRA evidence use framing with a 128-bit unpredictable nonce and explicit data
instructions, so content cannot forge its closing delimiter. Test each prompt call site.
The wrapper does not demote direct user instructions or create user authority.

**F13.2 [OPEN] — separate session and selection evidence.** An allowlist session event is
distinct from a model/route selection event, and policy-route checks cover both.
The session snapshots its permitted provider/model routes at creation; additional route
requests are disabled by default. Selection remains code-owned and recorded under ADR-0004.

**F13.3 [OPEN] — centralize privileged writes.** Acquire an explicit session/Run handle at
publication and reject a second live writer. The lifecycle owner performs crash repair; a
crash before materialization creates no session. Authenticate every API route with a real
per-process credential; loopback location and endpoint names never grant authority.

**F13.4 [OPEN] — preserve transport durability details.** Distinguish structured transport
errors from domain outcomes. Enqueue receipts support event observation; connect the follow
stream before paging, repair cursor gaps and deduplicate using higher sequence wins. Headless
stdout is the final response and stderr progress; exit derives from durable turn end, with
bounded SIGTERM/SIGINT shutdown semantics.

**F13.5 [OPEN] — verify persistence and settings seams.** Derived `run.json` cannot prevent
opening the authoritative log. List sessions from metadata, not all log bytes. Canonical
workspace identity and registry deletion preserve user data. Settings have one mutable user
layer, namespace CAS/conflict values and schema-led secret redaction. Validate whole attachment
batches, normalize, commit atomically, and verify content hashes/fsync/read-back behavior.

**F13.6 [OPEN] — prove every policy call site.** Tool Manager, authentication, the reconciled
redaction boundary, model routing and proxy configuration each need behavioral bypass tests
at every caller. Proxy resolution has one canonical function shared by installed and manual
dispatch; explicit refusal cannot silently fall back.

Closure tuple: producer/call site is the authenticated API handler and single event/session
writer; test node covers low-entropy/non-authority nonce use, allowlist/selection separation,
all handlers, CAS races, restart/read-back, proxy failure, headless settlement, and reconnect;
independent oracle replays the event log and attempts authorized and unauthorized requests via
a separate client path.

## Deferred or rejected report ideas

Report §6 rejects Cordis/plugin-bus adoption, readable reasoning persistence, peer-agent
mailboxes and replacing the Policy Engine with coarse permissions. ADR-0007 rejects the
upstream multi-event compaction bracket. ADR-0013's later Consequences substitutes exact-intent
review/resume for model-requested same-turn sandbox escalation.

PTC, historical-session tools, webhooks/scheduling, web/LSP/E2B and product-specific browser/TS
features remain deferred where the report names no current consumer. The annex's future Python
SDK explicit-home rule and future network hardening retain their source obligations for that
consumer; absence today does not prove their behavior. Accepted annex gate DAG/doc-budget
adaptations also need an identified enforcing workflow before acceptance. Deferral cannot
silently remove an accepted F1–F13 criterion or reclassify it as proved.

## Delivery order

Work through vertical slices: F5/F6 and A19 first (Git, all executable surfaces, real quotas,
clean source environment, runtime-owned backend, complete E2E); then F1 after policy
reconciliation; then F2/F7/F10 durable coordination; then F3/F11/F13 output, invariants and
trust boundaries; then operational F8/F9 context and skills. F8.7's namespace fix moves ahead
of any shared-thread subgraph consumer. F4 and F12 closing audits remain required. The number
of PRs is not an acceptance criterion.

## Closure record

An implementation may mark an identifier closed only by adding a dated entry containing:

- identifier and changed behavior;
- producer and concrete call site, or for F12 the named workflow/gate consumer;
- real behavioral test node and command result;
- independent oracle and its result;
- applicable MIRA/policy evidence; and
- any platform limitation retained in the claim.

No criterion closes by reference to this document, an ADR, a design assertion, or an existing
mechanism anchor alone.

**2026-09-08 — F13.6 proxy leg implemented (criterion remains open).** The LLM proxy producer now
resolves explicit endpoints through one runtime-owned function shared by installed and manual
dispatch, rejects undeclared or unsafe destinations without fallback, and refuses an explicit
proxy request when a manually injected provider cannot prove that it honors the endpoint.

- Identifier and changed behavior: F13.6 proxy configuration leg. `None` selects direct LiteLLM
  dispatch only after the runtime confirms that `OPENAI_BASE_URL`, `OPENAI_API_BASE` and mutable
  LiteLLM `api_base` are absent; a configured endpoint must exactly match the declared safe set and
  use HTTPS (or loopback HTTP for an owned local gateway), then reaches LiteLLM as `api_base` on
  free-text, structured-output and tool-capable calls. Construction and dispatch re-check ambient
  state, and explicit forwarding does not mutate the SDK or process environment.
- Producer and concrete call site: `resolve_proxy_endpoint` and `with_proxy_endpoint`
  (`runtime/agents/src/thymira/agents/llm/proxy.py`); `LiteLLMProvider._call`; installed factories
  `get_provider` and `provider_for`; and `routed_model`'s default and injected-provider branches
  (`runtime/agents/src/thymira/agents/model_binding.py`).
- Real behavioral test node and command result: `tests/thymira/test_agents_proxy.py` covers exact
  declaration, malformed credentials/query/fragment, unsafe HTTP, loopback HTTP and omitted
  endpoint; its installed-SDK probes confirm LiteLLM's ambient resolver, both installed factories,
  the routed default path and explicit no-mutation forwarding without a network call.
  `tests/thymira/test_agents_llm.py` captures the actual fake LiteLLM kwargs for all three
  completion forms and ambient refusal; `tests/thymira/test_agents_model_binding.py` proves an
  injected provider cannot bypass an explicit endpoint. `uv run pytest
  tests/thymira/test_agents_proxy.py tests/thymira/test_agents_llm.py
  tests/thymira/test_agents_model_binding.py -q
  --basetemp=.pytest-tmp-proxy-boundaries-followup2` → `100 passed` (one unrelated PydanticAI
  event-loop deprecation warning).
- Independent oracle and its result: the resolver's allowlist/URL checks are pure and do not
  import LiteLLM; the fake gateway only observes call kwargs and is separate from the provider
  completion implementation. The installed SDK's private resolver independently demonstrates the
  ambient bypass, while the runtime refuses it and explicit completion forwarding carries exactly
  the selected `api_base` without changing the SDK global or environment.
- Applicable MIRA/policy evidence: endpoint selection remains runtime configuration and is never
  authorization; an injected provider with an explicit endpoint is refused before invocation.
  Tool Manager, authentication, redaction and model-route policy caller legs remain open for the
  parent F13.6 audit. The provider ledger integration must record the same resolved endpoint before
  its source dispatches the request.

### 2026-09-08 — durable control lanes advanced (F2.5, F2.6; no identifier closed)

The implementation below proves the local owner and gateway slice while the remaining distributed
and cancellation legs stay open.

- *Identifier and changed behavior:* F2.5/F2.6. Local `ControlInput` rows now carry bounded
  claim ownership metadata. The owner loop atomically transfers `NEXT_TURN`/`NEXT_STEP` claims,
  keeps `FOLLOWUP` in `NEXT_TURN`, rolls an unconsumed `NEXT_STEP` to `NEXT_TURN`, preserves
  untargeted controls, and exposes one live-owner interception point before every routed THY model
  request. A consumer rejection marks the exact input `REJECTED`, settles undispatched work and
  emits a `turn.ended` with `REJECTED` and zero steps. The gateway hook runs again for PydanticAI
  tool and retry turns, and prompt provenance records its post-hook request bytes.
- *Producer and concrete call site:* `LifecycleOwnerLoop.process` and
  `LifecycleOwnerLoop.intercept_next_model_step` in
  `runtime/core/src/thymira/core/lifecycle_worker.py`; `ControlInbox.claim` and
  `ControlInbox.mark_applied` in `runtime/state/src/thymira/state/_lifecycle_inbox.py`;
  `routed_model._call` in `runtime/agents/src/thymira/agents/model_binding.py`; production
  composition in `runtime/core/src/thymira/core/worker.py` and the `SubgraphDeps` -> THY path.
- *Real behavioral test node and command result:* `uv run pytest
  tests/thymira/test_state_lifecycle_integration.py tests/thymira/test_agents_model_binding.py
  tests/thymira/test_agents_prompt_provenance.py -q --basetemp=.pytest-tmp-control-lanes-final` →
  `43 passed, 1 warning`. The nodes cover ordered lane separation, idempotent owner claims,
  next-step rollover, live interception, rejected zero-step settlement, every-request tool-turn
  interception and prompt artifact binding.
- *Independent oracle and its result:* `verify_events(repository.events(run.id))` independently
  replays the hash chain in the lifecycle tests; the tests also reload the durable control and work
  JSON records to assert terminal state, lane, claim metadata and exact `claimed_input_ids`. The
  prompt test reloads the artifact and compares its sha256 with the recorded `model.selected`
  payload. These oracles pass in the command above.
- *Applicable MIRA/policy evidence:* authenticated `AuthorityProof` remains required and bound to
  the exact control payload before enqueue. Rejected controls record a bounded `FailureCause` and
  never produce `MODEL_SELECTED`; transition controls retain lifecycle-only authority. MIRA does
  not consume model-visible control text as authorization.
- *Retained platform limitation:* PostgreSQL lifecycle composition is still fail closed. Its
  future `claim_control` transaction must implement the same owner epoch check, deterministic
  ordinal ordering, row CAS, claim token/expiry/attempt fields, and exact acknowledge/requeue
  identity. Cancellation targeting and host-pause turn abortion remain open F2 legs. F2.5, F2.6
  and the F2 foundation therefore remain open.

**2026-09-07 — F8.7 CLOSED.** Every `CheckpointRepository.put`/`get`/`list` and every
`StateCheckpointer.get_tuple`/`put`/`put_writes` now keys on the composite `(thread_id,
checkpoint_ns)` identity instead of `thread_id` alone.

- Identifier and changed behavior: F8.7. Two namespaces sharing a thread no longer collide in
  either write order, in either backend; `list` is namespace-scoped through `get_tuple` rather
  than a thread-wide probe.
- Producer and concrete call site: `StateCheckpointer._checkpoint_key`
  (`runtime/core/src/thymira/core/graph/checkpoint.py`) feeding every
  `CheckpointRepository.put`/`get` call; `LocalCheckpointRepository._path`
  (`runtime/state/src/thymira/state/checkpoints.py`); `PgCheckpointRepository.put`/`get`
  (`runtime/state/src/thymira/state/postgres/checkpoints.py`); the composite primary key on
  `tables.checkpoints` (`runtime/state/src/thymira/state/postgres/tables.py`).
- Real behavioral test node and command result:
  `uv run pytest tests/thymira/test_core_checkpoint.py -q` → `7 passed` (includes
  `test_state_checkpointer_isolates_two_namespaces_sharing_a_thread[root-first]` and
  `[child-first]`, `test_state_checkpointer_appends_pending_writes_to_one_namespace`,
  `test_state_checkpointer_reads_namespaces_through_a_separate_saver`, and
  `test_two_subgraphs_sharing_a_thread_checkpoint_and_resume_independently`, the closure tuple's
  producer pair of checkpoint storage plus graph recovery, with two real compiled LangGraph
  subgraphs sharing one thread and a resume through a separately constructed saver).
  `uv run pytest "tests/thymira/test_state_repositories.py::test_local_checkpoint_repository_isolates_namespaces_on_disk" "tests/thymira/test_state_repositories.py::test_local_record_and_checkpoint_repositories_round_trip" -q`
  → `2 passed`. `uv run pytest "tests/thymira/test_repo_conformance.py::test_event_and_checkpoint_repository_contract" -q`
  → `1 passed`. Regression sweep over every composed-graph consumer of `StateCheckpointer`
  (`test_core_composition.py`, `test_core_dispatch.py`, `test_core_execution_review.py`,
  `test_core_queue_dispatch.py`, `test_core_rework_assurance.py`, `test_core_graph_adapters.py`,
  `test_acceptance_demo_run.py`, `test_thy_graph.py`) → `116 passed`.
- Independent oracle and its result: `test_two_subgraphs_sharing_a_thread_checkpoint_and_resume_independently`
  lists the thread's on-disk directory itself, decodes every blob with a separately constructed
  `JsonPlusSerializer` (never `thymira.core.graph.checkpoint._decode`), reads the
  `checkpoint_ns` each stored envelope declares in its own config, and asserts the file it was
  found in equals that namespace's independently recomputed `sha256(ns)[:32] + ".bin"` -- three
  distinct namespaces (root, one `alpha:`-prefixed, one `beta:`-prefixed) resolve to three
  distinct, correctly named files, and the interrupted `beta` subgraph resumes to completion
  without replaying `alpha`. On PostgreSQL, `sa.inspect(engine).get_pk_constraint("checkpoints")`
  -- a schema reader that performed none of the writes -- confirms the composite primary key
  the unchanged `0001_initial` baseline migration materialises **on a fresh database**:
  `uv run pytest "tests/thymira/test_state_postgres.py::test_checkpoints_table_has_a_composite_namespace_primary_key" "tests/thymira/test_state_postgres.py::test_event_and_checkpoint_repository_contract" -q -m "integration and slow"`
  → `2 passed` against a real dockerized `postgres:16-alpine` (Docker 29.7, this environment).
  On a database that already ran revision 0001 -- the only kind that exists after any prior
  deployment -- `upgrade head` is a no-op instead (`create_all`'s `checkfirst=True` skips the
  existing table), so the widening never reaches it:
  `uv run pytest "tests/thymira/test_state_postgres.py::test_upgrade_head_does_not_widen_an_already_migrated_checkpoints_table" -q -m "integration and slow"`
  → `1 passed`, reproducing `UndefinedColumn` on every subsequent `put`. See Retained platform
  limitation below. The full Docker-gated lane, `uv run pytest tests/thymira/test_state_postgres.py
  tests/thymira/test_e2e_durable.py -q -m "integration and slow"` → `11 passed, 1 deselected`,
  actually ran here rather than being skipped.
- Applicable MIRA/policy evidence: none. Checkpoints are opaque LangGraph blobs, internal to
  graph execution; they carry no authorization, are never part of MIRA's evidence corpus, and no
  Run transition, `PolicyDecision` or `AuthorizationContext` is touched by this change. Stated
  explicitly rather than omitted.
- Retained platform limitation: `StateCheckpointer.put_writes` still appends to whichever
  checkpoint is latest within its `(thread_id, checkpoint_ns)` pair rather than matching
  `config["configurable"]["checkpoint_id"]`, the way LangGraph's own `InMemorySaver` does;
  fixing that needs multi-checkpoint retention per namespace, a separate storage-model slice.
  `StateCheckpointer.list(None)` still yields nothing where `InMemorySaver` iterates every
  thread; no production caller and no criterion requires it. Pre-1.0 local checkpoints written
  under the old `<thread_id>.bin` layout are no longer reachable (ADR-0013 decision 6: no
  migrations, no shims). A PostgreSQL database that already ran revision 0001 -- every deployed
  database -- is strictly worse off: `alembic upgrade head` does not widen its `checkpoints`
  table (`create_all`'s `checkfirst=True` skips a table that already exists), so no new Alembic
  revision reaches it and every `PgCheckpointRepository.put` against it raises `UndefinedColumn`
  rather than starting fresh; consistent with ADR-0013 decision 6, such a database must be
  dropped and recreated, not upgraded in place. F8.1-F8.6 are untouched and remain open, so F8
  (foundation) is not closed by this entry.

### 2026-09-07 — approval scopes close fail closed (F5.1, F5.4; F5.2, G.2, G.3 advanced)

**No identifier closes.** A foundation stays open until every accepted leg has evidence, and three
legs here do not. Every F5 marker above stays `[OPEN]`.

**F5.1 [ADVANCED] — close approval scopes fail closed.**

- *Changed behavior:* every ticketed approval request now records a scope -- the Run, the exact
  call, an absolute deadline and the delegation depth it was raised at, digested into
  `approval_scope_sha256`. A credit whose Run ended, whose THY pass ended, whose deadline passed or
  which belongs to another delegation depth authorizes nothing.
- *Producer and concrete call site:* `ToolManager.execute`
  (`runtime/tools/src/thymira/tools/manager.py`) builds `ApprovalScope(...).to_payload()` into the
  `details` it passes to `Gate.check_capability`, and consumes the fold through
  `ticket_disposition` (`runtime/tools/src/thymira/tools/approval_ticket.py`). Caller abort is
  `RunService.cancel` (`runtime/core/src/thymira/core/runs.py`) through
  `RunController.advance(RunTransitionKind.CANCEL)` -- that command's first production caller
  anywhere -- exposed as `POST /runs/{run_id}/cancel` and `thymira cancel`. The stale governance
  resolve is closed by the lifecycle filter in `pending_approvals`
  (`runtime/policies/src/thymira/policies/approval.py`).
- *Real behavioral test nodes and command result:*
  `tests/thymira/test_tools_approval_scope.py::test_a_credit_dies_with_the_run_it_was_raised_in`,
  `::test_a_credit_does_not_survive_the_pass_that_asked_for_it[begin_audit|begin_reporting|reopen]`,
  `::test_a_credit_expires_and_authorizes_nothing_afterwards`,
  `::test_an_answer_recorded_after_the_deadline_is_not_a_credit`,
  `::test_a_closure_that_leaves_the_run_alive_is_still_recorded_on_the_denial[released|expired]`,
  `::test_a_fresh_approval_after_an_expiry_buys_exactly_one_execution` (one human answer buys one
  execution over the whole log: a closure narrows what a credit may pay for, it never doubles the
  price),
  `::test_a_transition_that_disagrees_with_itself_closes_nothing[bare|versions-only|flat-disagrees|version-gap|state-disagrees]`,
  `::test_a_caller_whose_clock_names_no_instant_is_denied_rather_than_crashing`;
  `tests/thymira/test_policies_approval.py::test_pending_approvals_drops_a_request_the_run_terminated_on[cancel|block|fail|complete]`
  and `::test_pending_approvals_keeps_a_request_across_a_park_and_resume`;
  `tests/thymira/test_api_runs.py::test_cancel_run_records_a_cancelled_transition_and_closes_the_pending_approval`
  and `::test_cancel_run_is_idempotent_on_a_terminal_run`;
  `tests/thymira/test_api_governance.py::test_approving_a_decision_after_the_run_was_cancelled_is_refused`;
  `tests/thymira/test_cli_cancel.py::test_cancel_command_reports_the_cancelled_run`;
  `tests/thymira/test_e2e_tool_approval.py::test_cancelling_a_parked_run_closes_the_review_and_the_call_never_runs`.
  `uv run pytest tests/thymira/test_tools_approval_scope.py -q` → `29 passed`.
  `uv run pytest tests/thymira/test_policies_approval.py tests/thymira/test_policies.py
  tests/thymira/test_policies_execution_review.py -q` → `111 passed`.
  `uv run pytest tests/thymira/test_api_runs.py tests/thymira/test_api_governance.py
  tests/thymira/test_api_contract.py tests/thymira/test_cli_cancel.py tests/thymira/test_cli_runs.py
  -q` → `75 passed`. `uv run pytest tests/thymira/test_e2e_tool_approval.py -q` → `7 passed`.
- *Independent oracle and its result:* two. (1) MIRA control A3 through the new
  `ApprovalScopeLedger` (`runtime/mira/src/thymira/mira/checks/approval_scope_evidence.py`), which
  imports neither `thymira.tools` nor `thymira.policies`: it recomputes each scope digest from the
  request's own four fields and folds Core's `run.transitioned` chain through
  `trusted_lifecycle_command`. The facts it relates come from three writers -- `RunController`
  writes the cancel, the Gate writes the request, the Tool Manager writes the start -- and MIRA
  calls none of them.
  `tests/thymira/test_mira_approval_scope.py::test_a3_refuses_a_start_that_spends_a_credit_after_the_run_was_cancelled`
  makes A3 report `FAILED` for a forged start the manager itself declined to write, while
  `::test_a3_accepts_the_same_credit_spent_before_the_cancel` keeps `PASSED`. With the two
  `credit_open` conjuncts removed from `tool_authorization.py`, that node and four others report
  `PASSED` (`5 failed, 2 passed`) -- the measure of what the oracle was blind to.
  `uv run pytest tests/thymira/test_mira_approval_scope.py tests/thymira/test_mira_checks.py
  tests/thymira/test_mira_execution_review.py tests/thymira/test_mira_sandbox_approval.py -q` →
  `210 passed`. (2) Event replay through a separately constructed reader: the e2e abort node
  re-opens the persisted `runs/<id>/events.jsonl` with a fresh `JsonlEventLog`, runs `verify_log`
  over the chain (`valid`, `event_count` equal to the store's own count) and `audit_run` over that
  independently read history, asserting no `tool.started` for the ticket.
- *Applicable MIRA/policy evidence:* the closure facts are on the chain before any caller can
  report success -- `ticket_outcome` and `closed_scope_sha256` on `tool.denied`,
  `approval_scope_sha256` and `delegation_depth` on `tool.started`, and
  `approval_expires_at`/`delegation_depth`/`approval_scope_sha256` on
  `human.approval_requested`. All three `ScopeClosureReason` values are recorded as
  `ticket_outcome: cancelled`, not only the run-terminal one: a closure that leaves the Run alive
  still raises a fresh review, and the denial that raises it names the closure rather than
  reporting `pending`
  (`tests/thymira/test_tools_approval_scope.py::test_a_closure_that_leaves_the_run_alive_is_still_recorded_on_the_denial`).
  Authorization stays the Policy Engine's `allows_execution`; the scope fold only ever removes a
  credit, never creates one. A scope closes only on an `Actor.system()` event produced by
  `thymira.core` **whose payload agrees with itself** -- the serialised `RunState`, its flattened
  copy, the command's own stage or terminal outcome and the version reached are all held against
  each other, the same standard MIRA's `trusted_lifecycle_command` already applied to the same
  evidence class. The envelope alone would not have been enough: `producer` is a free argument on
  `EventLog.append`, so a bare `{"command": "cancel"}` would otherwise have emptied the pending
  set and made the manager deny outright
  (`::test_a_transition_that_disagrees_with_itself_closes_nothing`, five shapes;
  `tests/thymira/test_policies_approval.py::test_pending_approvals_ignores_a_transition_core_did_not_write`).
- *Retained platform limitation:* **client disconnect is not covered.** No live-connection or
  session-handle concept exists anywhere near `apps/api` (the only `StreamingResponse` is the
  unrelated log-follow in `routes/events.py`); this slice covers the observable consequences of a
  vanished caller -- a deadline and an explicit abort -- and the disconnect leg belongs with
  F13.3/F13.4. **Within-pass cross-task release of an unconsumed credit is not covered:** there is
  no stable per-step identity to key it on -- `ThySubgraph.invoke` mints a fresh `agent_id` per
  pass and `Delegator.delegate` a fresh `Task.id` per delegation -- and it belongs with F7.3.

**F5.4 [ADVANCED] — enforce one-shot and never semantics.**

- *Changed behavior:* a closed outcome vocabulary decides every ticketed call, `never` is decided
  before any responder is read, and a delegated scope cannot widen itself.
- *Producer and concrete call site:* `ticket_disposition`
  (`runtime/tools/src/thymira/tools/approval_ticket.py`) evaluates the Policy Engine through
  `_current_evaluation` and returns `TicketOutcome.UNAVAILABLE` on `Decision.BLOCK` before
  `_ticket_answers` reads a single `human.approval` event; `ToolManager.execute` and
  `_record_review_denial` record the outcome. `Delegator.delegate`
  (`runtime/agents/src/thymira/agents/delegation.py`) runs its child on a context carrying the
  child's own `agent_id`, `task_id` and `depth + 1` -- the named consumer for `delegation_depth`.
- *Real behavioral test nodes and command result:*
  `tests/thymira/test_tools_approval_scope.py::test_a_never_decision_is_checked_before_any_human_answer`,
  `::test_a_human_rejection_never_outranks_a_never_decision` (the ordering proof: with a rejection
  on the log and a BLOCKing engine the recorded `ticket_outcome` is `unavailable`, not `rejected`),
  `::test_a_credit_is_spendable_only_at_the_delegation_depth_it_was_raised_at[deeper|shallower]`,
  `::test_the_recorded_scope_digest_covers_every_scope_field[run|ticket|deadline|depth]`,
  `::test_an_allowed_execution_records_the_scope_it_spent`;
  `tests/thymira/test_agents_delegation.py::test_delegate_narrows_the_child_tool_context_to_its_own_task_and_depth`.
  The reordering regression gate is the existing hardening suite:
  `uv run pytest tests/thymira/test_tools.py tests/thymira/test_tools_manager.py
  tests/thymira/test_tools_sandbox_approval.py -q` → `154 passed` (first-ticketed-request-wins,
  rejection finality, cross-policy answers, the automatic/system-actor refusals, and the two
  currency nodes all unchanged). `uv run pytest tests/thymira/test_agents_delegation.py
  tests/thymira/test_agents_tool_bridge.py tests/thymira/test_thy_execute.py -q` → `33 passed`.
- *Independent oracle and its result:* A3 refuses a start deeper than its request from the log
  alone --
  `tests/thymira/test_mira_approval_scope.py::test_a3_refuses_a_start_at_a_deeper_delegation_depth_than_its_request[deeper|absent]`
  reports `FAILED`, and reports `PASSED` without the `credit_open` conjuncts.
- *Applicable MIRA/policy evidence:* `Decision.BLOCK` remains the only never outcome and
  `allows_execution` still refuses it unconditionally; no rule model or policy file changed. The
  outcome vocabulary is member-local (ADR-0013 decision 6) and every member of it is recorded as
  `ticket_outcome` on `tool.denied` -- `cancelled` for each of the three closure reasons, with the
  closed scope's digest beside it. The delegation-depth refusal narrows the credit pool *and* the
  spend fold: a start is charged only against the pool of the `delegation_depth` it recorded, so a
  delegate approved on its own request really may run, and its own yes is not eaten by the
  parent's earlier legitimate execution
  (`::test_a_delegates_own_approval_is_not_spent_by_its_parents_execution`,
  `::test_a_delegates_execution_does_not_spend_its_parents_credit`).
- *Retained platform limitation:* **"human questions require live root-agent/user authority" is not
  proved.** The Gate's authenticated-non-automatic-actor check plus scope liveness is not a live
  authority proof; it needs F13.3's session handle.

**F5.2 [ADVANCED] — adapt only the parked intent.**

- *Changed behavior:* none; the missing behavioural evidence lands.
- *Producer and concrete call site:* `bind_tool_intent`
  (`runtime/tools/src/thymira/tools/intent.py`) and `tool_intent_sha256`, unchanged.
- *Real behavioral test node and command result:*
  `tests/thymira/test_e2e_tool_approval.py::test_a_resume_proposing_different_arguments_gets_no_credit_from_the_parked_review`
  -- through the real Core + THY + Gate composition, the resumed pass proposing
  `echo(value="y")` after a human approved `echo(value="x")` is denied, raises a review of its own
  under a different ticket and decision, and the tool is invoked zero times.
  `uv run pytest tests/thymira/test_e2e_tool_approval.py -q` → `7 passed`.
- *Independent oracle and its result:* each request's `tool_intent_sha256` is recomputed in the
  node from the `tool` and `arguments` that request itself recorded, so the two tickets differing
  is a property of the two calls rather than of two values the producer happened to write;
  `verify_events` over the store's own hash-chained history is the separate, weaker check that the
  payloads recomputed from are the ones persisted.
- *Applicable MIRA/policy evidence:* one `human.approval` recorded, two tool-call decisions, no
  `tool.started`.
- *Retained platform limitation:* the criterion's remaining tuple -- a model cannot select a new
  model on resume -- is untouched here.

**G.2 [ADVANCED] — claims follow observed facts.** Scope evidence that is present and unreadable
fails closed in both readers, where unreadable covers naming no instant as well as failing to
parse: a recorded `approval_expires_at` that does not parse, is not a string, or carries no UTC
offset closes the scope
(`tests/thymira/test_tools_approval_scope.py::test_a_malformed_recorded_deadline_closes_the_scope[not-a-timestamp|naive-timestamp|not-a-string]`,
`::test_a_caller_whose_clock_names_no_instant_is_denied_rather_than_crashing`, and in MIRA
`tests/thymira/test_mira_approval_scope.py::test_a3_refuses_a_request_whose_deadline_names_no_instant`),
and a request whose recorded digest does not recompute authorises nothing in MIRA
(`tests/thymira/test_mira_approval_scope.py::test_a3_refuses_a_start_whose_claimed_scope_digest_does_not_recompute`,
`::test_a3_refuses_a_request_that_records_a_scope_it_cannot_substantiate`). *Absence* of scope
evidence keeps prior behaviour and is stated as such rather than reported as enforced:
`::test_a3_still_passes_a_log_that_records_no_approval_scope`.

**G.3 [ADVANCED] — closure evidence stays durable.** The scope digest, deadline, delegation depth
and ticket outcome are written to canonical events before any caller can report success, so MIRA,
policy and a later audit read them from the chain rather than from a producer's return value.
`tests/thymira/test_tools_approval_scope.py::test_an_allowed_execution_records_the_scope_it_spent`
covers the allowed path: the digest on `tool.started` equals the request's, and equals a
recomputation whose four inputs are each read back off the recorded event -- the run id, the
ticket, the deadline and the depth -- so a future redaction rule touching any one of them breaks
the recomputation instead of passing silently.
`::test_a_closure_that_leaves_the_run_alive_is_still_recorded_on_the_denial[released|expired]`
covers the refused path: an expired or released credit is recorded as
`ticket_outcome: cancelled` with the closed scope's digest, not as the `pending` that would be
indistinguishable on the chain from a review nobody has answered yet.

*Gate:* `just check` (lint, ty ratchet at 0, 4 import contracts kept, skills, roadmap, notes,
fast lane `2357 passed, 32 deselected`). Slow and integration lanes were not run for this slice.

*Review corrections (same branch).* Five findings from the branch review were reproduced as
failing nodes, fixed and kept: the credit fold double-charged after any closure and charged a
depth-filtered pool with depth-blind starts (one execution cost two approvals, and a delegate
approved on its own request was still denied); only a run-terminal closure was recorded on the
denial, so `expired` and `scope_released` left no durable outcome; the scope-closing envelope
check was weaker than MIRA's on the same evidence, making a bare forged transition an availability
attack; and a tz-naive deadline -- from an injected naive clock or from a recorded request -- raised
`TypeError` out of `ToolManager.execute` and out of A3 instead of failing closed. The bullets above
describe the corrected behaviour; nothing new is claimed closed.

**2026-09-07 — F6.2 CLOSED.** The backend records the resolved execution specification and
non-secret environment facts rather than trusting a request object; runtime-owned values
supersede caller inputs; a project `.env` cannot control PATH, preload, proxy startup or
`THYMIRA_SANDBOX_*` variables, and neither can the packaged compose deployment's own `.env`.

- Identifier and changed behavior: F6.2. `ContainerSandbox`/`LocalSubprocessSandbox` compute a
  `ResolvedExecutionSpec` (image, mount, network, memory, cpus, pids limit, forwarded/excluded
  environment names) together with the argv that produced it, so the two can never diverge; a
  caller-supplied environment is scrubbed of credential-shaped and interpreter-bootstrap names
  before either backend uses it; the local backend applies its own `PATH` last. Both `.env`
  loaders refuse an interpreter-bootstrap/proxy name (including `PYTHONUSERBASE`, added in
  review) and any `THYMIRA_SANDBOX_*` name (added in review — the initial pass filtered only
  bootstrap names, leaving the sandbox backend itself choosable from a project file).
  `compose.yaml` (the third producer the initial pass missed entirely: `env_file: .env` handed
  Docker the raw file before either Python loader ran, bypassing both refusals) now lists every
  forwarded variable explicitly instead.
- Producer and concrete call site: `ContainerSandbox._resolve_execution` and
  `LocalSubprocessSandbox.run` (`runtime/tools/src/thymira/tools/sandbox/{container,local}.py`),
  `scrub_environment` (`runtime/tools/src/thymira/tools/sandbox/environment.py`); both `.env`
  loaders, `apps/api/src/thymira/api/env.py:load_env_file` (raises `EnvFileError`) and
  `adapters/cli/src/thymira/cli/env.py:load_env_file` (silently skips, since the CLI spawns no
  child); both production composition roots,
  `apps/api/src/thymira/api/deps.py:178` and `runtime/core/src/thymira/core/worker.py:151`, via
  `configured_builtins_registry()`; `compose.yaml`'s `thymira` service, whose `environment:` list
  is now the only path a variable can reach the container through.
- Real behavioral test node and command result:
  `uv run pytest tests/thymira/test_tools_sandbox_environment.py::test_container_sandbox_records_the_resolved_specification_without_credential_values tests/thymira/test_tools_sandbox_environment.py::test_local_sandbox_keeps_the_runtime_owned_path_over_a_caller_value tests/thymira/test_tools_sandbox_environment.py::test_scrub_environment_drops_pythonuserbase tests/thymira/test_api_env.py::test_env_file_refuses_a_path_or_preload_variable tests/thymira/test_api_env.py::test_env_file_refuses_a_proxy_startup_variable tests/thymira/test_api_env.py::test_env_file_refuses_pythonuserbase tests/thymira/test_api_env.py::test_env_file_refuses_a_sandbox_backend_variable tests/thymira/test_cli_env.py::test_cli_env_file_skips_sandbox_variables tests/thymira/test_compose_config.py tests/thymira/test_tools_sandbox_production_integration.py::test_configured_container_registry_confines_run_python_through_tool_manager -q`
  → `11 passed` (the last node, `test_configured_container_registry_confines_run_python_through_tool_manager`,
  runs a real child through the configured container registry and Tool Manager against Docker
  29.7 / `thymira:dev` on this host; `test_compose_config.py` contributes two of the eleven,
  parsing `compose.yaml` itself and asserting no service uses `env_file` and no forwarded
  variable is bootstrap- or sandbox-shaped).
- Independent oracle and its result:
  `test_configured_container_registry_confines_run_python_through_tool_manager` closes the run,
  re-opens `events.jsonl` through a separately constructed `JsonlEventLog`, calls `verify_log`
  (valid) and reads the replayed `sandbox_spec` payload; the confined child's own observations
  (root filesystem write refused, a second write elsewhere on the container's own root filesystem
  refused, a TCP connect refused) agree with the recorded `network: "none"` and `workspace_mount`
  ending `:/workspace:rw`, and `audit_run`'s MIRA control A29, recomputing independently over the
  same replayed events, PASSES with `A29` absent from the FAILED set. Separately,
  `test_compose_config.py` parses the checked-in `compose.yaml` with a plain YAML loader (no
  compose tooling) and is itself the independent oracle for the third producer: it fails if
  `env_file` reappears or if a bootstrap/sandbox name is ever added to `environment:`.
- Applicable MIRA/policy evidence: MIRA control A29 (`runtime/mira/src/thymira/mira/checks/
  controls.py:a29_resolved_execution_spec`), which recomputes spec presence, the type of every
  field (not key presence alone -- an all-`None` spec is malformed even with all ten keys), the
  agreement of `sandbox_mode` with `requested_sandbox_mode`, mode/network and mode/mount
  consistency (respecting a backend's own honest `unenforced` declaration), and the absence of a
  credential-shaped forwarded name from MIRA's own fragment literal, never importing the
  producer's. It runs this whenever a spec is recorded at all, not only under `PARTIAL`/`FULL`,
  so an `UNUSABLE` execution's spec (this closure's own independent-oracle run never has one, but
  the local-backend acceptance path does) is graded too rather than passing unexamined.
- Retained platform limitation: F1's redaction boundary (blocked on G.1) is not depended on here
  — only environment variable *names* are ever recorded, never a value. `run_experiment`,
  `inspect_model` and `audit_model` carry the same evidence fields but are not yet exercised
  against the container backend (tracked under F6.7). A deployment that still wants to pass a
  `THYMIRA_SANDBOX_*` or credential value into the packaged container now does so by exporting it
  in the shell that runs `docker compose`/`just docker-run` (compose's own `${VAR}` substitution
  reads that, or a `.env` beside `compose.yaml`, only for names `environment:` lists explicitly)
  — never by editing `compose.yaml` back to `env_file:`.

**2026-09-07 — F6.6 CLOSED.** Requested mode, actual mode, enforcement status, exit outcome and
cleanup result remain five separate facts; a cleanup failure no longer overwrites an
already-confirmed successful execution.

- Identifier and changed behavior: F6.6. `run_container_lifecycle`'s `finally` block used to
  replace a confirmed `ContainerOutcome` (real `exit_code`, `child_confirmed=True`) with a single
  fail-closed `UNUSABLE`/125 shape whenever `docker rm` failed or timed out; it now rebuilds the
  outcome with `cleanup_confirmed=False` and an appended stderr note, keeping the observed
  `exit_code`/`child_confirmed` untouched.
- Producer and concrete call site: `run_container_lifecycle`'s `finally` block
  (`runtime/tools/src/thymira/tools/sandbox/container_lifecycle.py`), `ContainerOutcome.
  cleanup_confirmed`, `SandboxRun.cleanup_confirmed`
  (`runtime/tools/src/thymira/tools/sandbox/base.py`), `ToolResult.sandbox_cleanup_confirmed`
  (`runtime/tools/src/thymira/tools/models.py`), and the `tool.completed` payload keys
  `requested_sandbox_mode` / `sandbox_mode` / `sandbox_enforcement` / `exit_code` /
  `sandbox_cleanup_confirmed` (`runtime/tools/src/thymira/tools/manager.py:_close_record`).
- Real behavioral test node and command result:
  `uv run pytest tests/thymira/test_tools_sandbox_cleanup.py::test_failed_cleanup_preserves_the_child_exit_outcome tests/thymira/test_tools_sandbox_cleanup.py::test_failed_cleanup_is_recorded_as_a_separate_fact tests/thymira/test_mira_checks.py::test_a29_failed_cleanup_is_a_medium_finding_that_keeps_the_exit_outcome -q`
  → `3 passed`. `uv run pytest tests/thymira/test_tools_sandbox_container_unit.py -q` → `18
  passed` (includes the two renamed regressions, `test_cleanup_failure_keeps_the_confirmed_exit_outcome`
  and `test_cleanup_timeout_keeps_the_confirmed_exit_outcome`, driven through the full
  `ContainerSandbox.run` path with a stubbed docker client).
- Independent oracle and its result: `test_a29_failed_cleanup_is_a_medium_finding_that_keeps_the_exit_outcome`
  grades the cleanup failure through `audit_run`'s MIRA control A29 from a replayed synthetic
  `tool.completed` event — a MEDIUM finding, at `Severity.MEDIUM`, while the same event's
  `exit_code` is asserted unchanged — a computation that never reads `container_lifecycle`'s own
  internal state. Neither backend claims `FULL`, so A19's separate confinement grading is
  untouched by this fix.
- Applicable MIRA/policy evidence: MIRA control A29 grades a cleanup failure MEDIUM independently
  of A19's confinement-strength grading; the two never read each other's fact.
- Retained platform limitation: cleanup is still a single `docker rm --force` attempt with no
  retry; a persistently un-removable container is recorded as `cleanup_confirmed=False` and left
  for operator cleanup, which this slice treats as an honest fact rather than a bug to paper over.

**2026-09-07 — F6.1, F6.5 and F6.7 ADVANCED, REMAIN OPEN.** Backend selection now exists in
runtime configuration (`THYMIRA_SANDBOX_BACKEND` and siblings, `SandboxSettings.from_env()` /
`build_sandbox()` / `configured_builtins_registry()`,
`runtime/tools/src/thymira/tools/sandbox/settings.py` and
`runtime/tools/src/thymira/tools/builtins/configured.py`) and is proved end-to-end through Tool
Manager for `run_python` and `git_status` against real Docker
(`tests/thymira/test_tools_sandbox_production_integration.py`, 5 passed), with filesystem
(root and outside-workspace writes refused, a `read_only` mode's `:ro` mount enforced), network
(no route under `network: none`), process (`--pids-limit 16` stops a fork loop at the recorded
value) and memory (`--memory 64m` kills an oversized allocation) boundaries each independently
observed by the confined child. All eight sandbox-backed tool call sites now carry the same four
confinement facts through `with_sandbox_evidence()`. Still open: effective bind-workspace quotas
(Docker applies none to a bind mount; `ResolvedExecutionSpec.unenforced` declares
`workspace_quota` rather than claiming it), container timeout evidence (a start-stage timeout
still collapses to `exit_code=125`/`UNUSABLE` rather than a distinct fact), the training,
model-loading and prediction surfaces (`run_experiment`, `inspect_model`, `audit_model`) under the
container backend, and the packaged compose deployment, which has no Docker socket. RLIMIT and
CPU-share enforcement remain unproved. A19 stays FAILED because both backends report `PARTIAL`,
never `FULL` — closing it needs a Linux Landlock/bubblewrap backend or a proven quota mechanism
(ADR-0013 decision 2), out of scope for this slice.

**2026-09-07 — G.2 and G.3 advanced for this slice, not closable by one slice.** Requested
configuration (`THYMIRA_SANDBOX_*`), backend enforcement (`SandboxEnforcement` plus
`ResolvedExecutionSpec.unenforced`) and actual outcome (`exit_code`, `cleanup_confirmed`) are
now three separate recorded facts on every sandbox-backed `tool.completed` event, and an unknown
backend name, an unsafe argv value (image/memory/cpus regex-validated in
`ContainerSandbox.__init__`), a malformed spec, or a refused bootstrap variable all fail closed
before code can run or a claim can be recorded (G.2). Every fact MIRA's A29 and A19 need is in
the canonical `tool.completed` payload before the caller can report success (G.3). Both remain
open program-wide.

### 2026-09-08 — the Python bootstrap is contained and cleanup carries bounded identity evidence (F6.4 closed; F6.8, F6.1, F6.3, F6.5, F6.7, G.2, G.3 advanced)

**F6.4 [CLOSED 2026-09-08] — clean up with identity evidence.**

- *Identifier and changed behavior:* F6.4. The reap is bounded by one global deadline on Windows
  across the `taskkill /T` tree request and final child wait, records its observed duration and
  scope, re-reads the final monotonic time before calling the reap quiesced, never signals a pid it
  has already reaped, and the container's own state is confirmed by a live `docker inspect`
  profile of the container the run actually used rather than by the request object. Each of those
  is durable evidence on `tool.completed`, not an inference.
- *Producer and concrete call site:* `reap_process_tree`
  (`runtime/tools/src/thymira/tools/sandbox/reaper.py`), reached from `_terminate` and
  `_wait_bounded` in `runtime/tools/src/thymira/tools/sandbox/process_capture.py`; its first
  statement is the identity rule (`if process.poll() is not None: return
  ReapOutcome(REAP_SKIPPED_REAPED, ...)`), replacing the unconditional
  `os.killpg(process.pid, SIGKILL)` the POSIX branch used to run. The probe is `_probe_facts`
  (`runtime/tools/src/thymira/tools/sandbox/container_lifecycle.py`), filled from the widened
  `docker inspect --format {{json .}}` stage and attached through `_termination` to
  `SandboxRun.termination`; `with_sandbox_evidence`
  (`runtime/tools/src/thymira/tools/builtins/subprocess_command.py`) carries it onto every
  code-executing tool's `ToolResult`, `recordable_result`
  (`runtime/tools/src/thymira/tools/result_evidence.py`) validates it, and
  `ToolManager._close_record` (`runtime/tools/src/thymira/tools/manager.py`) writes it as the
  `sandbox_termination` payload key.
- *Real behavioral test node and command result:*
  `tests/thymira/test_tools_sandbox_reaper.py::test_the_reaper_never_signals_a_pid_it_has_already_reaped`
  starts a real sentinel process, hands the reaper a handle whose recorded pid is the sentinel's
  and which already reports an exit status — the PID-reuse hazard arranged deliberately — and
  asserts the outcome is `skipped_reaped`, that nothing was signalled, and that **the sentinel is
  still alive**; `::test_a_timed_out_run_reaps_the_whole_tree_inside_its_bound` runs a real child
  that spawns a detached grandchild appending to a file, and after the timeout the file's size is
  stable across two samples inside the bound; `::test_a_live_child_is_reaped_and_its_quiescence_recorded`
  and `::test_reap_process_tree_rejects_a_non_positive_bound` cover the bound itself.
  `tests/thymira/test_tools_sandbox_container_unit.py::test_the_live_inspection_is_recorded_as_a_probe`,
  `::test_a_probe_that_disagrees_with_the_specification_is_recorded_verbatim`,
  `::test_a_malformed_probe_is_absent_rather_than_guessed` and
  `::test_the_inspection_asks_the_daemon_for_the_whole_document` pin the probe's shape, its
  fail-closed absence and the widened format string.
  `tests/thymira/test_tools_sandbox_production_integration.py::test_the_live_container_probe_matches_the_resolved_specification`
  runs it against the real daemon (Docker 29.7, `thymira:dev`, this environment). The added
  `::test_windows_tree_reap_reports_direct_scope_when_taskkill_fails` node proves that a nonzero
  `taskkill` result records `tree_scope="direct_child"` with the tree reach unconfirmed,
  `::test_windows_tree_reap_shares_one_deadline_between_taskkill_and_wait` proves that Windows
  cleanup cannot spend the full bound twice, and the added container unit node proves that cleanup
  time is excluded from the child execution duration. Local and container termination records now
  persist `reap_duration_s` beside `reap_deadline_s`.
  `::test_windows_tree_reap_uses_final_clock_for_the_global_deadline` also covers inside, exact,
  late-wait and signal-phase overruns; a late `wait()` is recorded `unbounded` even when it returns
  an exit status. The follow-up focused reaper/process/A30 run passed `54 tests`.
  Command results, all on this machine: the focused sandbox/A30, E2E and acceptance command
  → `54 passed, 1 warning`; `uv run pytest tests/thymira/test_tools_sandbox_production_integration.py
  -q -m integration` → `7 passed`; `just check` → `All checks passed!`, `ty diagnostics: 0
  (baseline 0)`, `Contracts: 4 kept, 0 broken.`, `Agent Notes: valid`, `2522 passed, 36
  deselected`; `just test-cov` → `2558 passed, 61 warnings`, `91.77%` total coverage.
- *Independent oracle and its result:* MIRA control **A30**
  (`runtime/mira/src/thymira/mira/checks/termination_evidence.py`, registered in `CONTROLS` after
  A29). It grades the daemon's probe against the `ResolvedExecutionSpec` the runtime built before
  starting the container — two different observers of the same container — and fails
  `signalled_after_reap`, an unbounded reap under a claimed success, a reap that exceeds its
  recorded bound, incompatible outcome/channel pairs, and a confirmed container child that
  recorded no probe. A skipped reap may legitimately carry null reap timing because no cleanup was
  attempted. It imports nothing from `thymira.tools`: the payload key list, the
  `"64m" -> 67108864` memory parser and the severity table are its own, and
  `tests/thymira/test_mira_checks_termination.py::test_a30_never_imports_the_producer_it_audits`
  parses the module with `ast` and asserts it. Not blind:
  `::test_a30_is_not_blind[probe_disagrees_with_the_specification]`,
  `[probe_missing_on_a_confirmed_container_child]`, `[signalled_after_reap]`, `[unbounded_reap]`,
  `[reap_within_deadline]` and `[outcome_channel_relationship]`
  grade the *same* failing record with that one conjunct removed and require the verdict to return
  to `PASSED`. Shape validation additionally rejects missing, unknown, non-finite, overlong and
  malformed termination/specification/probe values before semantic parsing; the adversarial
  regressions include uppercase completion status without an event exit code, an unhashable spec
  control list, and overlong `memory`/`cpus` strings.
- *Applicable MIRA/policy evidence:* A30 (new, HIGH) and A29 (unchanged) both grade the recorded
  evidence; no `PolicyDecision`, `AuthorizationContext` or Run transition is touched — a probe
  disagreement is a finding, never an authorization, and MIRA still proposes rather than acts.
- *Retained platform limitation:* the POSIX branch of the reap (`os.killpg` over the session
  `start_new_session` created) is **not exercised here** — this host is Windows, so the tree node
  proves the `taskkill /F /T` branch and records `tree_scope="process_tree"` only when that command
  succeeds; a missing or failing command is recorded as `tree_scope="direct_child"` with the tree
  reach unconfirmed. The Linux container lane reaps through `docker rm --force`, not `killpg`.
  Windows tree reap depends on `taskkill` being on PATH, and the outcome is declared per run,
  never assumed. The local backend records `probe: null`, which the criterion's own "when the
  platform can provide it" covers and which A30 treats as not-applicable rather than as a pass.
  F6.1, F6.3, F6.5, F6.7 and F6.8 remain open, so F6 (foundation) is **not** closed by this entry.

**F6.8 [ADVANCED] — contain the Python bootstrap and cleanup.** Six of its eight clauses now have
behavioral evidence and the A30 oracle. Two do not, and they are named below; the identifier stays
`[OPEN]`.

- *Changed behavior:* every Python child is bootstrapped `python -I -u -B` — isolated (no user
  site, no `PYTHON*` variables honoured, no script directory on `sys.path`), unbuffered, and
  writing no `__pycache__` into the workspace the audit reads. Each execution gets private
  temporary storage: the local backend a fresh `0o700` directory removed afterwards, the container
  its own per-run `/tmp` tmpfs, with `TMPDIR`/`TEMP`/`TMP` set by the runtime *after* the caller's
  environment is scrubbed. A deadline that passed is recorded as a timeout whatever the child's own
  exit code was. Link-shaped staging paths are refused as links, before anything is written through
  them. The exit marker is the runtime's, reported consistently on both backends.
- *Producer and concrete call site:* `PYTHON_BOOTSTRAP_FLAGS` and `python_workspace_command` /
  `python_module_command` (`runtime/tools/src/thymira/tools/builtins/subprocess_command.py`),
  which every staged-script tool and `audit_model`'s worker argv go through;
  `private_execution_storage` / `container_private_storage`
  (`runtime/tools/src/thymira/tools/sandbox/private_storage.py`) applied through
  `apply_runtime_owned` (`.../sandbox/environment.py`) in `LocalSubprocessSandbox.run` and
  `ContainerSandbox._resolve_execution`; `_record_deadline`
  (`.../sandbox/process_capture.py`), read after the wait confirms the exit;
  `assert_no_link_components` and `link_shaped_refusal`
  (`.../builtins/subprocess_command.py`), the second called from `RunPython.execute` before
  `.thymira` is created; `TerminationEvidence` (`.../sandbox/termination.py`) reaching
  `tool.completed` through the seam named in the F6.4 entry above.
- *Real behavioral test node and command result:* the independent observer is **the child
  itself**. `tests/thymira/test_tools_sandbox_bootstrap.py::test_the_child_reports_an_isolated_unbuffered_interpreter[local]`
  and `[container]` have the child print `sys.flags.isolated`, `no_user_site`,
  `ignore_environment`, `sys.dont_write_bytecode` and `sys.stdout.write_through` with no knowledge
  of the argv the runtime built; `::test_the_local_child_gets_private_temporary_storage_that_is_removed`
  has it print `tempfile.gettempdir()` and write there, and the test asserts that directory is
  neither the host's shared temp nor inside the workspace and no longer exists after the call;
  `::test_the_container_child_gets_the_container_private_tmpfs` and
  `::test_a_caller_supplied_temp_directory_cannot_redirect_the_child` cover the container and the
  supersession. Control output:
  `::test_a_forged_control_frame_cannot_change_the_recorded_outcome[local]` and `[container]` have
  the child print a convincing `{"thymira_control": {"exit_code": 0, ...}}` frame plus a fake
  `[exit code: 0]` line and then exit 3 — the recorded exit code is 3, and
  `thymira.agents.tool_bridge.render_tool_output`, the one text projection the model reads, still
  ends with the runtime's own `[exit code: 3]`;
  `::test_a_child_that_replaces_the_exit_primitives_still_yields_the_true_exit_code[local|container]`
  rebinds `sys.exit`, `os._exit` and `sys.stdout` first; `::test_the_child_cannot_write_to_a_control_descriptor[local|container]`
  has the child report its own `OSError` on `os.write(3, ...)`. **These four control-output nodes
  passed before this slice's changes**: they pin a property the runtime already had (it never
  parsed the child's output for a control fact) rather than one this slice created; what is new is
  that the fact is now *recorded* as `control_channel`/`control_channel_validated`.
  Deadline: `tests/thymira/test_tools_sandbox_termination.py::test_run_bounded_process_reports_a_timeout_when_the_exit_was_confirmed_too_late`
  and `::test_local_sandbox_never_reports_a_clean_success_for_a_child_that_outlived_its_deadline`.
  Links: `tests/thymira/test_tools_sandbox_links.py::test_run_python_refuses_a_link_shaped_staging_directory`,
  `::test_workspace_relative_path_refuses_a_link_component`, `::test_a_plain_workspace_path_is_still_accepted`,
  `::test_a_container_child_cannot_write_outside_the_workspace_through_its_own_link`. Spill:
  `tests/thymira/test_tools_spill.py::test_a_truncated_spill_is_detected_rather_than_served` and
  `::test_a_spilled_result_lands_outside_the_sandbox_workspace` — **both passed against the
  unchanged store**: the mechanism was already correct, the evidence was missing.
- *Independent oracle and its result:* three, layered. (1) The **child** is the observer for the
  bootstrap, the temporary storage and the control descriptor — it reports what it sees, knowing
  nothing of the argv or environment the producer built. (2) **A30** recomputes the termination
  verdict from the replayed chain with none of the producer's constants, and
  `test_a30_is_not_blind[<conjunct>]` proves each conjunct is load-bearing. (3) For the spill,
  `LocalArtifactStore.verify()` and MIRA's **A5** recompute the manifest sha256 of a truncated
  artifact — readers that performed none of the writes and import no tools code.
  End to end: `tests/thymira/test_tools_sandbox_production_integration.py::test_container_run_python_records_verifiable_termination_evidence_end_to_end`
  (`integration and slow`) runs a hostile child through `ToolManager` against the real daemon,
  closes and re-opens `events.jsonl` through a separately constructed `JsonlEventLog`, checks
  `verify_log(...).valid`, and grades A29 and A30 over the replay; it then shows the oracle bites —
  a payload rewritten after the fact never reaches A30 because A1 refuses the chain, so the same
  shape is appended honestly onto the live chain and A30 fails it CRITICAL over a log `verify_log`
  still calls valid.
- *Applicable MIRA/policy evidence:* A30 (new) and A29 (unchanged) grade every execution's
  recorded evidence; A5 grades the spilled artifact. No authorization path changes: a termination
  finding is a finding, and the Policy Engine remains the only thing that authorizes.
- *Legs still open, one by one:*
  1. **"applicable strict resource limits."** POSIX `RLIMIT` and CPU shares are still not applied
     by the local backend. They are honestly declared in `ResolvedExecutionSpec.unenforced` and
     owned by F6.1, which stays open; this slice added the daemon-side probe that confirms the
     container's memory/pids/read-only/network ceilings against the recorded spec, but applied no
     new limit.
  2. **"Spill files are private."** The containment half is proved — a spilled artifact lands
     outside the sandbox workspace, so the confined child cannot reach it — and the verifiable half
     is proved. The host-user half is not: the spilled file is written with the default mode, and
     no POSIX `0o600` was added (`runtime/state/src/thymira/state/local_store.py` is untouched).
- *Retained platform limitations:* the private temporary directory's `0o700` mode and the reaper's
  POSIX branch are unexercised on this Windows host; the link nodes prove a **junction**
  (`mklink /J`, which needs no privilege) plus a real POSIX symlink created by the container child,
  never an unprivileged host symlink, so `assert_no_link_components`' `is_symlink` branch is
  covered only through the container lane. The leg-4 zero-exit-past-the-deadline node needs an
  **injected clock** (keyed on the observation's own `child_exit_code`, so it advances only after
  the wait has confirmed the exit) to reach that race deterministically; the accompanying node with
  a real child that outlives its deadline needs no injection. `-I` drops `PYTHONPATH`, so
  `python -m thymira.tools.model_prediction_worker` now requires `thymira` genuinely installed —
  true in the venv and in `thymira:dev`, but a deployment that relied on `PYTHONPATH` would break.

**Also advanced, still open:** **F6.1** — memory/cpus/pids remain container-only and are now
confirmed by a live probe as well as by the child; RLIMIT and CPU-share enforcement stay unproved
and bind-workspace quotas stay unenforced. **F6.3** — the container's protocol frame is now
validated as a whole document (`{{json .}}`) with a fail-closed probe parser, and the daemon's own
metadata is bounded separately from the child's output budget, but the criterion's full hostile
frame and unpredictable-result-path surface is not audited here. **F6.5** — `run_python` and Git
carry the new evidence through the one `with_sandbox_evidence` seam, but `run_experiment`,
`inspect_model` and `audit_model` are still not exercised against the container backend end to end.
**F6.7** — the container timeout now has a distinct recorded fact and the probe adds independent
backend evidence, closing two gaps its own entry named; the training, model-loading and prediction
surfaces, the socket-less compose deployment and the workspace-quota mechanism stay open, and A19
remains FAILED because both backends still report PARTIAL, never FULL — untouched by this slice.
**G.2** — a fourth kind of claim (how an execution ended) now distinguishes requested configuration
from observed outcome and fails closed on unreadable evidence. **G.3** — that evidence is durable
on `tool.completed` before the caller can report success. Both remain open program-wide.

### 2026-09-08 — resource settings and CPU probe made fail closed (F6.1, F6.3, F6.5, F6.7, F6.8 advanced; all remain open)

- *Identifier and changed behavior:* F6.1/F6.7. `THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES` and
  `THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES` are runtime-owned settings carried into
  `ResolvedExecutionSpec`. The output budget remains enforced by the existing bounded capture
  helper. A requested workspace quota now returns `UNUSABLE` before a local child or Docker
  container starts, because the current execution boundary is a host bind mount and Docker does
  not quota it. The local and container constructors also reject zero, overlong, and malformed
  memory/CPU values before they become subprocess or Docker arguments. Docker's daemon probe now
  records `HostConfig.NanoCpus` from the inspected container.
- *Producer and concrete call site:* `SandboxSettings.from_env()` / `build_sandbox()` resolve the
  values; `LocalSubprocessSandbox.run()` and `ContainerSandbox.run()` refuse an unsupported quota;
  `ResolvedExecutionSpec.as_payload()` persists the values; `_probe_facts()` reads the daemon's
  CPU ceiling; MIRA's A29 validates optional limit fields and A30 independently parses the
  recorded CPU ceiling and compares it with `cpus_nano`.
- *Behavioral test node and command result:* the focused non-Docker regression command
  `uv run pytest tests/thymira/test_mira_checks_termination.py tests/thymira/test_mira_checks.py
  tests/thymira/test_tools_sandbox_settings.py tests/thymira/test_tools_sandbox_local.py
  tests/thymira/test_tools_sandbox_container_unit.py -q -m 'not slow'` → `292 passed in 6.56s`.
  It covers quota refusal with no child/container start, bounded output configuration, malformed
  and overlong resource values, daemon CPU probe capture, A29 optional-field shape validation,
  and A30 CPU disagreement/zero-ceiling findings. A separate sandbox/A30 module sweep also ran
  the six non-slow Docker integration tests and ended `186 passed, 1 deselected in 59.84s`.
- *Independent oracle and result:* A30's parser and probe comparison in
  `runtime/mira/src/thymira/mira/checks/termination_evidence.py` imports no producer module and
  fails a CPU ceiling that disagrees with the daemon's `NanoCpus`; A29 independently rejects
  malformed optional output/quota fields. The tests that remove one disagreement or shape fact
  remain the oracle's non-blindness evidence.
- *Applicable policy evidence:* this changes only sandbox configuration and audit evidence. A19
  still reports `PARTIAL`/`UNUSABLE` according to the backend's actual execution, and the Policy
  Engine remains the only authorization path. No redaction or F1 policy behavior changed.
- *Retained limitation:* an effective writable-workspace quota mechanism is still absent for both
  current backends; local RLIMIT/CPU-share enforcement and full container E2E coverage for every
  executable surface remain open. The refusal and `unenforced` declaration are evidence of that
  limitation, not closure of F6.1 or F6.7.

### 2026-09-08 — bounded local tmpfs workspace quota advanced F6.1/F6.7 (still open)

**F6.1/F6.7 [ADVANCED, OPEN].** A configured positive workspace quota now selects a Docker
local-driver `tmpfs` volume with a requested byte and inode bound. A fixed helper mounts the source
read-only and export staging separately, while the worker receives only the named volume at
`/workspace`; there is no writable host bind, Docker socket, privileged flag, or `FULL` claim.
The runtime reads back the daemon volume options, helper `statvfs` and `tmpfs` mount type, worker
mount identity, helper/host tree digests, journaled publication state, and cleanup identities.
Docker Desktop remounts the worker volume root-owned, so this backend runs its capability-dropped
worker as container root for the private volume and records the fact in the implementation note;
the ordinary container backend remains non-root. Publication failure makes the tool result fail
even when the child exit was zero, and child exit remains recorded separately.

- *Identifier and changed behavior:* F6.1/F6.7 workspace quota clauses. `ContainerSandbox` routes
  `workspace_quota_bytes` to `quota_workspace.py`; `workspace_helper.py` owns fixed stage/export
  operations; `workspace_tree.py` performs the host no-follow portable walk, sibling lock, CAS
  journal and recovery. `ToolManager` holds that lock through execution and writeback. MIRA A32
  independently grades the completed evidence tuple and imports no producer implementation.
- *Behavioral evidence (historical pre-staging baseline, superseded by the repaired run below):*
  `uv run pytest tests/thymira/test_tools_workspace_tree.py
  tests/thymira/test_tools_workspace_helper.py tests/thymira/test_tools_sandbox_quota_unit.py
  tests/thymira/test_mira_workspace_quota.py tests/thymira/test_tools_manager.py -q` → `36 passed,
  1 skipped` (the host-specific Docker test is excluded); `uv run pytest
  tests/thymira/test_tools_workspace_quota_integration.py -q -m "integration and slow" -s` →
  `4 passed` on Docker Server `29.7.2`, image `thymira:quota-f6`, image id
  `sha256:de25925ef96604bf6cd23bedc620d25b241dd77589960ff881ec46a4a8382210`. The second test
  executed configured production `run_python` through Tool Manager, reopened `events.jsonl`,
  verified the hash chain, and ran a fresh MIRA audit whose independent A30 and A32 statuses were
  `PASSED`; the child observed the tmpfs ENOSPC boundary and only the bounded output was published.
- *Why this does not close F6:* the slice proves the named-volume quota path and one production
  consumer. It does not prove every executable surface, all resource/process/network controls,
  or a bind-mounted quota, and it deliberately does not call two directory renames globally
  atomic. F6.1, F6.5 and F6.7 therefore remain open.

### 2026-09-08 — quota input staging and helper race repair (F6.1/F6.7 still open)

**Repair scope [IMPLEMENTED, Docker verified].** The four named executable consumers now
pass runtime-owned `StagedInput` bytes through the sandbox contract. A quota execution stages them
in a private helper source mount and charges their bytes together with the read-only workspace tree
before the worker starts; oversized source or inputs therefore leave the host workspace unchanged.
The helper uses descriptor-relative no-follow opens, pinned inode and size checks, bounded chunked
copy, growth and digest detection, and a deadline; ENOSPC becomes a bounded refusal. Runtime inputs
are pruned before helper export so only child outputs enter the existing journaled digest-CAS
writeback. The ordinary local and bind-mounted container backends use the same manifest contract.

- *Identifier and changed behavior:* additive F6.1/F6.7 quota staging contract and helper copy
  boundary; independent A32 now requires exact request equality, source-plus-input accounting,
  final/export byte and entry agreement, inode capacity, and input cleanup when input evidence is
  present.
- *Offline behavioral evidence:* `uv run pytest -q tests/thymira/test_tools_workspace_helper.py
  tests/thymira/test_mira_workspace_quota.py tests/thymira/test_tools_sandbox_quota_unit.py
  tests/thymira/test_tools_sandbox_local.py tests/thymira/test_tools_sandbox_container_unit.py
  tests/thymira/test_tools.py -k "run_python or staged_subprocess or run_experiment_stages or
  inspect_model_stages or quota or workspace_helper or local_sandbox or container_sandbox"` →
  `52 passed`; helper staged-input, growth-after-snapshot, request mismatch, and writeback
  mismatch cases are included in that focused result.
- *Docker behavioral evidence:* `uv run pytest tests/thymira/test_tools_workspace_quota_integration.py
  -q -m "integration and slow" -s` → `4 passed` on Docker Server `29.7.2`, using the checked-in
  default image `thymira:quota-93f8c70` built from commit `93f8c70b` with
  `docker build --pull=false -t thymira:quota-93f8c70 .`; image id
  `sha256:edacf5731006ef875a25f37e0d395b1d05c780e5175a2491c12f0311b4ad0d1f`. The run observed
  real tmpfs ENOSPC and bounded writeback, read-only and danger modes, and a reopened Tool
  Manager JSONL replay with fresh A30/A32 `PASSED`. A separate bounded driver against the same
  image refused oversized staged inputs for `run_python`, `run_experiment`, `inspect_model`, and
  `audit_model` before worker start, left each workspace unchanged, and confirmed journal recovery;
  every private input cleanup fact was true. The independent A32 replay passed valid evidence and
  failed a reconstructed-chain request mismatch.
- *Why this does not close F6:* the slice still does not prove every resource/process/network
  control or a bind-mounted quota, and it deliberately does not call two directory renames
  globally atomic. F6.5 and the remaining F6 closure criteria therefore remain open.

Focused evidence on this rebased worktree:
`uv run pytest tests/thymira/test_thy_execute.py tests/thymira/test_thy_execute_parallel.py tests/thymira/test_e2e_agent_roster.py -q --basetemp .pytest-tmp/roster-focused-green2 -m 'not slow'`
→ **18 passed, 1 warning**. The duplicate-ID probe was red before the positional fan-in change and
green afterward. Ruff check and format check pass for all four changed Python files. Full `just
check`, full-suite coverage, Docker, and real-model API lanes were intentionally left to the parent
lane; no F2/F7 foundation closure is claimed here.
### 2026-09-08 — one uniform subagent settlement (F7.1 CLOSED; F7.2, F7.5, G.2, G.3 advanced)

**F7 (the foundation) is NOT closed.** F7.3 (persisted monotonic depth, the CAS DAG board), F7.4
(MIRA independence before its critic step, no authority handoff to a coding harness) and F7.6 (the
orchestrator as the only peer boundary) are untouched by this slice and stay `[OPEN]`. Only the
`F7.1` marker above changes.

**F7.1 [CLOSED 2026-09-08] — close the stop reason enum.**

- *Changed behavior:* terminal subagent reasons are now exactly `completed`, `stopped`,
  `out-of-room`, `declined`, `failed` and `abnormal`, and an unknown value is refused at the model
  boundary rather than coerced or defaulted. `out-of-room` and `failed` used to be the same fact:
  both PydanticAI exceptions were caught by one handler and recorded as one
  `AgentEndReason.MAX_TURNS`.
- *Producer and concrete call site:* `StopReason`
  (`packages/schemas/src/thymira/schemas/enums.py`) and its rejection at the `SubagentResult` model
  boundary (`packages/schemas/src/thymira/schemas/subagent.py`); `AgentRunner.run`
  (`runtime/agents/src/thymira/agents/runner.py`), whose split `except UsageLimitExceeded` /
  `except UnexpectedModelBehavior` handlers feed `_record_exhaustion` and are the only place that
  knows which bound ended a step; `build_settlement` and `settle_stopped`
  (`runtime/agents/src/thymira/agents/settlement.py`); `Delegator._settle`
  (`runtime/agents/src/thymira/agents/delegation.py`), whose `except Exception` arm settles
  `abnormal`; `_stopped_settlement` (`runtime/thy/src/thymira/thy/nodes/execute.py`) for `stopped`.
  The mapping was **observed, not assumed**: `pydantic_ai.Agent.run_sync` was instrumented in this
  tree, and two invalid outputs raise `UnexpectedModelBehavior` ("Exceeded maximum output retries
  (1)") while `max_turns` tool calls raise `UsageLimitExceeded` ("The next request would exceed the
  request_limit of 2").
- *Real behavioral test nodes and command result:* one real run per reason, each with a genuine
  producer already reachable in this tree —
  `tests/thymira/test_agents_delegation.py::test_a_completed_child_settles_completed_with_its_validated_result`,
  `::test_a_child_that_exhausts_output_validation_settles_failed`,
  `::test_a_child_that_runs_out_of_its_request_budget_settles_out_of_room`,
  `::test_a_child_waiting_on_a_human_review_settles_declined`,
  `::test_a_child_that_raises_settles_abnormal_and_never_rejects_its_parent`, and
  `tests/thymira/test_thy_execute.py::test_a_task_skipped_because_a_dependency_failed_settles_stopped`
  for `stopped`. The boundary-rejection node is
  `tests/thymira/test_schemas_subagent.py::test_an_unknown_stop_reason_is_rejected_at_the_model_boundary`
  (`"cancelled"` and the underscore spelling `"out_of_room"` both raise `ValidationError`; the
  hyphen is what makes the second half sharp), with
  `::test_the_stop_reason_vocabulary_is_exactly_six_members` and
  `::test_a_stop_reason_has_no_default_so_a_settlement_cannot_omit_it` beside it.
  `uv run pytest tests/thymira/test_schemas_subagent.py tests/thymira/test_schemas.py
  tests/thymira/test_schemas_contract_v03.py -q` → `63 passed`.
  `uv run pytest tests/thymira/test_agents_settlement.py tests/thymira/test_agents_delegation.py
  tests/thymira/test_agents_runner.py tests/thymira/test_agents_tool_bridge.py
  tests/thymira/test_thy_agent_coding_recovery.py -q` → `57 passed`.
- *Independent oracle and its result:* MIRA control **A31**
  (`runtime/mira/src/thymira/mira/checks/subagent_settlement.py`), conjuncts (1) and (4). Its
  first conjunct validates each raw settlement through the shared `SubagentResult` contract and
  also re-states the six values as MIRA's own literal `frozenset` — never trusting the producer's
  enum — so a settlement whose reason is outside them is refused
  (`tests/thymira/test_mira_subagent_settlement.py::test_a31_refuses_a_settlement_whose_stop_reason_is_outside_the_six`,
  FAILED), and it holds the runner's own recorded `stop_reason` on `agent.completed` against the
  settlement's, so the two writers must agree
  (`::test_a31_refuses_a_settlement_whose_stop_reason_disagrees_with_the_runners_own`, FAILED). A
  settlement claiming a reason other than `stopped`/`abnormal` with no `agent.started` at all also
  fails (`::test_a31_refuses_a_settlement_that_claims_a_completion_no_step_ever_started`), so a
  forger can only ever downgrade itself to `stopped`, which grants nothing.
  `uv run pytest tests/thymira/test_mira_subagent_settlement.py
  tests/thymira/test_e2e_subagent_settlement.py -q` → `43 passed`.
- *Applicable MIRA/policy evidence:* A31, `Severity.HIGH`, registered in `CONTROLS` and folded by
  `audit_run`. No policy rule, rule model or policy file changed; a stop reason is a recorded fact
  and never an authorization. `EventType.SUBAGENT_SETTLED` is `EventSurface.LOG_ONLY` by default —
  the settlement is evidence, and the model-visible rendering stays on `agent.message`.
- *Retained platform limitation:* the mapping from this runtime's exception classes and
  `TaskStatus` onto the six DSH reasons is a **repository decision**, recorded in
  `.agents/notes/implemented/uniform-subagent-settlement.md`, not a verified equivalence with the
  DSH harness. `AgentEndReason` deliberately keeps `MAX_TURNS` for both exhaustion shapes: it is
  the coarse lifecycle fact existing consumers read, and `stop_reason` is the finer one beside it.

**F7.2 [ADVANCED] — preserve bounded diagnostics and parent settlement.**

- *Landed:* diagnostics are redacted **and then** truncated at the producer
  (`settlement.bounded_diagnostics`), with the bound recorded as `diagnostics_limit` /
  `diagnostics_truncated` rather than applied silently; the settlement is on the hash chain before
  the parent's own message and before `delegate` returns; each invocation has exactly one durable
  settlement, its keyed lifecycle is ordered and cardinality checked, and one selector chooses a
  canonical record within that invocation key.
- *Producer and concrete call site:* `bounded_diagnostics`, `build_settlement`,
  `record_settlement` and `select_canonical`
  (`runtime/agents/src/thymira/agents/settlement.py`), reached from `Delegator._settle`
  (`runtime/agents/src/thymira/agents/delegation.py`) and `_stopped_settlement`
  (`runtime/thy/src/thymira/thy/nodes/execute.py`).
- *Real behavioral test nodes and command result:*
  `tests/thymira/test_agents_settlement.py::test_diagnostics_are_redacted_before_they_are_truncated`
  (a secret past the limit: no prefix of it of length ≥ 8 survives — truncating first would have
  written one verbatim, past `EventLog.append`'s own redaction, because a half secret matches no
  pattern), `::test_the_bound_is_recorded_as_a_fact_not_applied_silently`,
  `::test_the_delegation_key_includes_the_invocation_and_parent_identity`,
  `::test_the_selector_picks_the_latest_settlement_in_log_order`,
  `::test_the_selector_refuses_a_candidate_set_spanning_two_delegation_keys`;
  `tests/thymira/test_agents_delegation.py::test_the_settlement_is_on_the_chain_before_the_parents_message`
  (G.3, asserted on `seq`). `uv run pytest tests/thymira/test_agents_settlement.py -q` → `8 passed`.
- *Independent oracle and its result:* two. (1) A31 conjunct (3) holds the bound the settlement
  *declares* against `SETTLEMENT_DIAGNOSTICS_LIMIT`, MIRA's **own** constant, so a producer that
  quietly raised its own bound is caught rather than self-certifying
  (`::test_a31_refuses_a_settlement_that_declares_a_bound_of_its_own`,
  `::test_a31_refuses_diagnostics_longer_than_the_bound_the_settlement_declares`, both FAILED), and
  conjunct (6) recomputes each `delegation_key` from the seven persisted invocation and parent
  identity fields the settlement itself recorded
  (`::test_a31_refuses_a_settlement_whose_delegation_key_does_not_recompute`, FAILED). (2) Bytes on
  disk: `tests/thymira/test_e2e_subagent_settlement.py::test_a_fresh_reader_reconstructs_which_child_settled_with_what`
  runs a real composed THY pass to `runs/<id>/events.jsonl`, re-opens it with a **separately
  constructed** `JsonlEventLog`, checks `verify_log` is valid with an `event_count` equal to the
  store's own, and reconstructs all three settlements — completed, failed, stopped — with
  `settlements_from_events`, from bytes rather than from any producer's return value.
  `::test_every_settlement_precedes_the_parents_message_on_the_persisted_chain` re-checks the G.3
  ordering on the re-read chain and `::test_a_settlement_written_before_a_crash_is_still_readable`
  abandons a pass between the settlement and the message, showing the settlement survives and it is
  the parent's rendering that is missing. The missing rendering is an allowed crash-window result;
  a durable parent inbox and recovery reader remain open under F7.3. `uv run pytest
  tests/thymira/test_e2e_subagent_settlement.py -q` → `5 passed` (fast lane: in-process graph,
  `ScriptedProvider`, no server, no subprocess, no Docker). The repeated-instruction regression
  also asserts three distinct task and invocation keys and a passing A31 audit.
- *Applicable MIRA/policy evidence:* every fact MIRA needs — `stop_reason`, `result_json`,
  `result_schema`, `diagnostics`, `diagnostics_limit`, `diagnostics_truncated`, `delegation_key`,
  `task_id`, `agent_id`, `delegation_depth`, `parent_agent`, `objective` — is on the
  `subagent.settled` payload
  before the parent can report success, and `agent.started`/`agent.completed` carry the delegation
  identity so a delegation is distinguishable from a direct `AgentRunner().run` (MIRA's own audit
  agents, THY's
  `run_ml_agent` primary call) and a missing settlement is therefore detectable.
- *OPEN LEG, named:* **the parent's own recovery view is not derived from the settlements.** After a
  park, `execute_node._carried_outcomes` rebuilds outcomes from `ThyProgress.agent_messages`
  (`tuple[Task, ...]`) carried in graph checkpoint state, not from `subagent.settled`; if checkpoint
  state and the log ever disagreed, the parent would trust the checkpoint. "Durable parent
  settlement" is therefore proved for a fresh reader and for an auditor, **not** for the parent's
  own restart path, and closing it needs F7.3's persisted board, which this slice was told not to
  build.

**F7.5 [ADVANCED] — validate one uniform SubagentResult.**

- *Landed:* one uniform `SubagentResult` every child settles with — a child that raises, that
  exhausts output validation, that runs out of its request budget, that is left waiting on a human
  and that the orchestrator stopped all settle, and the parent is never rejected by its child; one
  shared selector, with a mechanical test naming every production module allowed to touch
  settlement selection; the schema-constrained completion validated exactly once, at settlement, and
  read by consumers as an object instead of re-parsed; bounded, redacted diagnostic fields; a
  durable settlement notice written before the parent's message; each invocation has exactly one
  durable settlement with at most one matching start and completion in strict
  `started < completed < settled` order, while retries and forks mint fresh task and agent
  invocation identities and keys. A stopped or abnormal child may lack completion after a crash;
  any lifecycle facts that exist still have to be coherent. Selection across distinct attempts
  remains a higher-level consumer concern, and neither attempt inherits approval credit or a
  widened capability.
- *Producer and concrete call site:* `SubagentResult`
  (`packages/schemas/src/thymira/schemas/subagent.py`); `Delegator.delegate` / `Delegator._settle`
  (`runtime/agents/src/thymira/agents/delegation.py`), which returns a `Settlement` and whose
  `except Exception` arm carries `# noqa: BLE001  # F7.5: a child settles, it never rejects its
  parent`; `AgentRunner.run` carries the delegation key on every `agent.completed` outcome;
  `settlement.select_canonical`; `_experiment_or_none`
  (`runtime/thy/src/thymira/thy/nodes/execute.py`), which now narrows `Settlement.completion` with
  `isinstance` — the second `ExperimentResult.model_validate_json` and its `ValidationError` branch
  are deleted, not kept in agreement.
- *Real behavioral test nodes and command result:* the six stop-reason nodes listed under F7.1;
  `tests/thymira/test_agents_delegation.py::test_repeated_instruction_delegations_keep_distinct_invocations`
  (same objective and depth, but distinct task, agent and delegation identities) and
  `::test_a_forked_child_inherits_no_approval_credit` (a human answered the root's
  depth-0 call; the forked child proposing the identical call at depth 1 is denied and the tool is
  invoked zero times — this **reads** wave 1's
  `thymira.policies.approval_scope.RecordedApprovalScope.spendable_at_depth` through
  `ToolManager.execute` and duplicates none of it. Neutralising that predicate to `return True`
  makes the node fail, so it is not vacuous);
  `tests/thymira/test_agents_settlement.py::test_no_production_module_selects_a_settlement_outside_the_shared_selector`
  (walks every `packages|runtime|apps|adapters` `src/thymira` tree from a repo root resolved the way
  `test_compose_config.py` resolves it, and asserts the set of modules naming `select_canonical`,
  `SUBAGENT_SETTLED` or `"subagent.settled"` is exactly `{schemas/enums.py,
  agents/settlement.py, agents/__init__.py, mira/checks/subagent_settlement.py,
  mira/checks/controls.py}` — `delegation.py` is *not* among them: it goes through the writer and
  never names the event type or the selector);
  `tests/thymira/test_thy_execute.py::test_the_experiment_fold_reads_the_completion_the_runner_already_validated`
  (with `ExperimentResult.model_validate_json` monkeypatched to raise, the experiment still folds —
  no consumer re-parses) and `::test_a_task_skipped_because_a_dependency_failed_settles_stopped`;
  `tests/thymira/test_thy_execute_parallel.py::test_each_wave_task_lands_exactly_one_settlement_on_the_shared_log`
  (the wave path replays only the child's *new* events, so a prefix settlement is never duplicated).
  `tests/thymira/test_mira_subagent_settlement.py::test_a31_requires_the_objective_on_a_started_lifecycle`
  also proves that the producer's required start identity cannot borrow the intentional omission
  on `agent.completed`; a present malformed completion objective remains rejected.
  `uv run pytest tests/thymira/test_thy_execute.py tests/thymira/test_thy_execute_parallel.py
  tests/thymira/test_thy_agent_ml.py tests/thymira/test_thy_graph.py tests/thymira/test_thy_e2e.py
  -q` → `34 passed`. `tests/thymira/test_thy_execute.py::test_a_malformed_experiment_completion_can_no_longer_reach_the_fold`
  is recorded honestly as a **regression guard, not new evidence**: it passed before the change too,
  because the deleted defensive branch caught the same case.
- *Independent oracle and its result:* A31 has six conjuncts: raw settlement schema/result
  validation, exactly one durable settlement per invocation with lifecycle cardinality, diagnostics
  bounds, agreement between lifecycle writers including strict ordering, every keyed parent
  rendering after its matching settlement, and delegation-key recomputation. Each conjunct has a
  hostile-chain blindness test that removes only that conjunct
  from `SETTLEMENT_CONJUNCTS` and observes the expected bypass: `::test_a31_is_blind_without_the_result_validation_conjunct`,
  `::test_a31_is_blind_without_the_exactly_one_settlement_conjunct`,
  `::test_a31_is_blind_without_the_diagnostics_conjunct`,
  `::test_a31_is_blind_without_the_two_writers_conjunct`,
  `::test_a31_is_blind_without_the_canonical_selection_conjunct`, and
  `::test_a31_is_blind_without_the_key_recomputation_conjunct` all pass. The hostile inputs are,
  respectively, a foreign event run in an otherwise valid result, a started or duplicated
  unstarted settlement, oversized diagnostics, a conflicting runner stop reason, a parent message
  that omits or misrenders the canonical record, and a forged key; all six controls refuse the
  intact chain. The three Terra probe families are retained as refusal regressions in
  `tests/thymira/test_mira_subagent_settlement.py`: invalid result fields, lifecycle key mismatch,
  duplicate unstarted stopped records, reordered or duplicated lifecycle facts, orphaned keyed
  facts, and stale earlier parent renderings. A9 independently refuses keyed lifecycle order and
  cardinality violations while preserving the legacy subject-pairing behavior for direct events.
  A31 also rejects a hash-valid start that omits its objective, while its completion-only omission
  allowance is type-scoped to the current `agent.completed` writer shape; A9's boundary remains
  keyed ordering/cardinality rather than full seven-field identity.
- *Why the oracle is independent:* `thymira.mira.checks.subagent_settlement` imports only
  `thymira.events` (`canonical_json`, `sha256_text`), the shared `SubagentResult` schema and its
  own sibling `models` — never `thymira.agents`, `thymira.core` or `thymira.thy` (`just
  check-imports`: `Contracts: 4 kept, 0 broken`). Shared schema validation is the boundary contract;
  MIRA independently recomputes the six-value vocabulary, delegation key and canonical selector,
  and independently matches lifecycle and every parent rendering across the event log, including
  ordering and cardinality. A9's keyed matrix is likewise computed from raw event payloads. That duplication
  is the oracle and is the one documented exception to F7.5's "one shared selector" rule — the same
  deliberate duplication `tool_intent_evidence` applies to the ticket and
  `approval_scope_evidence` to the scope digest. Its conjuncts relate facts from **three different
  writers** (`AgentRunner`'s `agent.started`/`agent.completed`, `Delegator`'s `subagent.settled`,
  the parent's `agent.message`), so none of them can be satisfied by one producer asserting about
  itself.
- *Applicable MIRA/policy evidence:* A31 at `Severity.HIGH`, not in
  `_IN_FLIGHT_EXCLUDED_CONTROL_IDS`; `AuditMode.IN_FLIGHT` accepts a keyed start with no terminal
  evidence as pending, while terminal evidence without a settlement and impossible ordering or
  cardinality still fail. A31 is **not** relaxed on `ctx.run_failed` the way `a9_lifecycle_pairing`
  is: a child that dies must still settle. A log with no delegation is `NOT_APPLICABLE`, so every
  pre-existing audited fixture keeps its verdict
  (`::test_a31_is_not_applicable_to_a_log_with_no_delegation`). Authorization is untouched: a
  settlement is a recorded fact, never an authorization, and a forked child draws on no credit its
  parent was granted.
- *THREE OPEN LEGS, named one by one:* (a) **"the parent's inbox" is the event log, not a
  parent-owned durable inbox the parent reads back** — the same leg as F7.2's, blocked on F7.3; a
  crash after the settlement and before `agent.message` therefore leaves an allowed missing parent
  action, and A31 does not claim that action occurred. A settlement reader under `runtime/core`
  was rejected on `needs a named consumer`, since `thymira.core` carries `ThyProgress` opaquely and
  has no reader for settlements today. (b) **one
  class of child still ends its parent:** `thymira.agents.usage.UsageLimitExceededError` settles the
  child `abnormal` and is then re-raised, because a run-budget breach is a run-level fact
  (`AgentRunner`'s documented contract), so for that class the child does still reject its parent,
  by design — `tests/thymira/test_agents_delegation.py::test_a_run_budget_breach_settles_the_child_before_it_ends_the_run`
  proves only that the settlement is on the chain before it propagates. (c) **a hard process kill
  settles nothing:** a `SIGKILL` between `agent.started` and the settlement leaves a started
  delegation unsettled, which A31 correctly reports as FAILED; that is the honest limit of "never
  rejects its parent". A fourth, smaller limit: a task the orchestrator stopped settles but gets no
  `agent.message`, because THY has never rendered a skipped task into the model-visible history and
  this slice does not start; the e2e node asserts that absence explicitly rather than assuming it.

**G.2 [ADVANCED] — claims follow observed facts.** An unknown stop reason is rejected at the model
boundary rather than coerced or defaulted (`test_an_unknown_stop_reason_is_rejected_at_the_model_boundary`),
a `subagent.settled` payload that does not validate is *skipped and never coerced* by
`settlements_from_events`
(`tests/thymira/test_agents_settlement.py::test_settlements_are_read_back_from_the_chain_and_a_malformed_one_is_skipped`)
and reported as a finding by A31, a `diagnostics_limit` a producer raised for itself is caught
against MIRA's own constant, an unreadable bound (absent, non-integer, or a `bool`) fails closed,
and a `delegation_key` that does not recompute authorises nothing. A `result_json` and a
`result_schema` exist exactly when the reason is `completed` and are refused otherwise, so "the
child completed" and "here is what it produced" cannot disagree. Not closable by one slice.

**G.3 [ADVANCED] — closure evidence stays durable.** The stop reason, the canonical result, the
schema that validated it, the bounded diagnostics, the declared bound, the delegation identity and
the depth are appended to the canonical chain *before* the parent appends its own `agent.message`
and before `delegate` returns, so MIRA, policy and a later audit read them from the log rather than
from a producer's return value. Asserted on `seq` in memory
(`test_the_settlement_is_on_the_chain_before_the_parents_message`) and again on persisted bytes
after a separate reader re-opens the file
(`test_every_settlement_precedes_the_parents_message_on_the_persisted_chain`), with the crash window
covered by `test_a_settlement_written_before_a_crash_is_still_readable`. Not closable by one slice.

*Gate:* `just check` (lint, ty ratchet at 0, `Contracts: 4 kept, 0 broken`, skills, roadmap, Agent
Notes valid, fast lane `2459 passed, 34 deselected`). Wide regression sweep over every consumer A31
now grades: `uv run pytest tests/thymira/test_mira_subagent_settlement.py
tests/thymira/test_mira_checks.py tests/thymira/test_mira_e2e.py tests/thymira/test_mira_flow.py
tests/thymira/test_mira_approval_scope.py tests/thymira/test_mira_orchestrator.py
tests/thymira/test_e2e_governance.py tests/thymira/test_e2e_runtime_spine.py
tests/thymira/test_e2e_tool_approval.py tests/thymira/test_acceptance_demo_run.py -q` →
`244 passed`. The Docker-gated lanes actually ran here rather than being skipped:
`uv run pytest tests/ -q -m "integration and slow"` → `16 passed, 2477 deselected` against Docker
29.7 on this host, and `uv run pytest tests/ -q -m "slow or integration"` → `379 passed, 2114
deselected`.

*Shared-file touches declared:* `AGENTS.md` (`Contract v0.8` → `v0.9`, plus the same token in the
"Read more" line) and `tests/thymira/test_schemas.py` (`CONTRACT_VERSION == "0.8"` → `"0.9"`).
Neither is in this slice's owned-file list; both are unavoidable consequences of the contract bump
to 0.9 and are called out in the commit bodies for the concurrent slice.
**2026-09-08 — F1.2 CLOSED.** The event log has one global supported format authority and refuses
foreign serialized evidence before replay, append or consumer dispatch.

- Identifier and changed behavior: F1.2. `Event.schema_version` is now strict and globally owned
  by `thymira.schemas.EVENT_LOG_FORMAT_VERSION` (`"0.3"`); `deserialize_event` checks the required
  version and closed `EventType` vocabulary before model construction. Missing, malformed and
  unsupported versions and unknown event types have distinct safe errors. `verify_events` applies
  the same refusal to in-memory records before hash verification. JSONL reopen and PostgreSQL read
  paths use the same canonical deserializer, so an invalid persisted chain cannot be dispatched.
  PostgreSQL append takes its transaction-scoped run lock, validates the complete persisted prefix
  and its hash/sequence chain on that same connection, and only then inserts the next event. No
  compatibility shim or migration is present.
- Producer and concrete call site: `thymira.events._ChainState._next`,
  `thymira.events.deserialize_event`, `thymira.events.read_events`,
  `thymira.events.verify_events`, `thymira.events.JsonlEventLog.__init__`, and
  `thymira.state.postgres.events._load_chain` / `_read_events`.
- Real behavioral test nodes and command result:
  `uv run pytest tests/thymira/test_events.py tests/thymira/test_schemas.py
  tests/thymira/test_schemas_contract_v03.py tests/thymira/test_state_repositories.py
  tests/thymira/test_state_uow.py tests/thymira/test_cli_events.py
  tests/thymira/test_cli_approval.py tests/thymira/test_cli_thy_view.py -q` → `221 passed`.
  The F1.2 nodes cover re-chained foreign, mixed-version, missing, boolean and malformed version,
  unknown-type, JSONL reopen, append-time recheck and in-memory verification cases.
- Independent oracle and its result: the tests mutate raw JSON records and recompute each hash
  directly from `sha256_text(canonical_json(record))`, without calling `hash_event` or the reader;
  `verify_log` returns invalid for the re-chained foreign log and reports zero parsed events for a
  mixed-version log. The reopen test also proves the file bytes are unchanged after refusal.
- PostgreSQL append review correction and result: `tests/thymira/test_review_log_format_probes.py`
  uses a mocked SQLAlchemy connection to return a re-chained two-row prefix whose first row is
  foreign, malformed or missing its version. `uv run pytest
  tests/thymira/test_review_log_format_probes.py -q --basetemp .pytest-tmp/log-format-review` →
  `13 passed`. The probe observes the lock and complete-prefix read and confirms no insert occurs,
  including a re-chained broken sequence; the live PostgreSQL integration remains in the parent
  lane.
- Applicable MIRA/policy evidence: no MIRA or Policy Engine rule changed. MIRA's event evidence
  readers consume the verified event sequences supplied by the local and PostgreSQL state seams;
  format and vocabulary refusal occurs before any audit or policy consumer can fold a record.
- Retained platform limitation: only format `"0.3"` is supported. A future monotonic bump must
  update the single authority, writers, readers and fixtures together; no pre-1.0 migration or
  compatibility path is claimed. F1 remains open because F1.1 is blocked by G.1 and F1.3–F1.6
  remain open.

### 2026-09-08 — resource settings and CPU probe made fail closed (F6.1, F6.3, F6.5, F6.7, F6.8 advanced; all remain open)

- *Identifier and changed behavior:* F6.1/F6.7. `THYMIRA_SANDBOX_OUTPUT_LIMIT_BYTES` and
  `THYMIRA_SANDBOX_WORKSPACE_QUOTA_BYTES` are runtime-owned settings carried into
  `ResolvedExecutionSpec`. The output budget remains enforced by the existing bounded capture
  helper. A requested workspace quota now returns `UNUSABLE` before a local child or Docker
  container starts, because the current execution boundary is a host bind mount and Docker does
  not quota it. The local and container constructors also reject zero, overlong, and malformed
  memory/CPU values before they become subprocess or Docker arguments. Docker's daemon probe now
  records `HostConfig.NanoCpus` from the inspected container.
- *Producer and concrete call site:* `SandboxSettings.from_env()` / `build_sandbox()` resolve the
  values; `LocalSubprocessSandbox.run()` and `ContainerSandbox.run()` refuse an unsupported quota;
  `ResolvedExecutionSpec.as_payload()` persists the values; `_probe_facts()` reads the daemon's
  CPU ceiling; MIRA's A29 validates optional limit fields and A30 independently parses the
  recorded CPU ceiling and compares it with `cpus_nano`.
- *Behavioral test node and command result:* the focused non-Docker regression command
  `uv run pytest tests/thymira/test_mira_checks_termination.py tests/thymira/test_mira_checks.py
  tests/thymira/test_tools_sandbox_settings.py tests/thymira/test_tools_sandbox_local.py
  tests/thymira/test_tools_sandbox_container_unit.py -q -m 'not slow'` → `292 passed in 6.56s`.
  It covers quota refusal with no child/container start, bounded output configuration, malformed
  and overlong resource values, daemon CPU probe capture, A29 optional-field shape validation,
  and A30 CPU disagreement/zero-ceiling findings. A separate sandbox/A30 module sweep also ran
  the six non-slow Docker integration tests and ended `186 passed, 1 deselected in 59.84s`.
- *Independent oracle and result:* A30's parser and probe comparison in
  `runtime/mira/src/thymira/mira/checks/termination_evidence.py` imports no producer module and
  fails a CPU ceiling that disagrees with the daemon's `NanoCpus`; A29 independently rejects
  malformed optional output/quota fields. The tests that remove one disagreement or shape fact
  remain the oracle's non-blindness evidence.
- *Applicable policy evidence:* this changes only sandbox configuration and audit evidence. A19
  still reports `PARTIAL`/`UNUSABLE` according to the backend's actual execution, and the Policy
  Engine remains the only authorization path. No redaction or F1 policy behavior changed.
- *Retained limitation:* an effective writable-workspace quota mechanism is still absent for both
  current backends; local RLIMIT/CPU-share enforcement and full container E2E coverage for every
  executable surface remain open. The refusal and `unenforced` declaration are evidence of that
  limitation, not closure of F6.1 or F6.7.

### 2026-09-08 — MIRA evidence and hub identity boundaries are explicit (F7.4/F7.6 advanced)

**F7 remains open.** This entry records two runtime invariants with their adversarial nodes; it
does not close F7.4 or F7.6. F7.2, F7.3, and F7.5 remain open, and there is still no named
`coding_harness` production consumer.

- *Changed behavior:* `MiraAuditFlow.audit` captures a deep-copied `IndependentAuditEvidence`
  tuple before producer execution and passes that same pre-agent witness to every MIRA producer and
  the final finding critic. Producer events may still be re-read from the live log to ground the
  producer's own candidate, but cannot enter the independent critic input. `Delegator` now binds
  the root dispatch to `thy` and nested dispatch to the active child spec stamped in its context;
  a mismatched sibling or authority-handoff label is refused before task minting.
- *Producer and concrete call site:* `MiraAuditFlow.audit` and `_run_agents` in
  `runtime/mira/src/thymira/mira/flow.py`, `IndependentAuditEvidence` in
  `runtime/mira/src/thymira/mira/verification.py`; `execute_node`'s THY dispatch reaches
  `Delegator.delegate` in `runtime/thy/src/thymira/thy/nodes/execute.py`, with nested ML helper
  dispatch using the same bound context.
- *Real behavioral test nodes and command result:* `tests/thymira/test_mira_verification.py::test_mira_critic_receives_the_pre_agent_evidence_snapshot`
  appends producer-only messages from two configured MIRA producers and proves both producer and
  critic inputs equal the pre-agent graph evidence; `tests/thymira/test_agents_delegation.py::test_root_delegation_cannot_claim_a_sibling_parent`
  and `::test_a_delegated_child_cannot_relabel_its_parent_as_a_sibling` prove refusal before any
  forged child event, including the actual context stamped by the first dispatch. Focused command:
  `uv run pytest tests/thymira/test_agents_delegation.py tests/thymira/test_thy_agent_ml.py tests/thymira/test_mira_verification.py -q`
  → **36 passed, 1 warning**.
- *Independent oracle and applicable authority evidence:* the test compares independent copies
  against the caller's pre-agent event tuple and checks the live producer-only marker is absent;
  no producer return value is used to build the critic input. MIRA's existing A31 remains the
  independent settlement oracle, and no Policy Engine, approval, Run transition, or coding-harness
  authority path is changed. The focused test also leaves the event log unchanged after each
  rejected parent handoff.
- *Remaining clause inventory:* F7.4 still needs the full independent-evidence/critic closure
  tuple and a coding-harness authority consumer; F7.6 still needs the orchestrator-owned CAS/DAG
  board integration, all competing/depth-race nodes, and that same named consumer. F7.2/F7.3/F7.5
  remain open and are intentionally untouched.

### 2026-09-08 — fenced owner settlement envelope and parent DAG recovery (F7.2/F7.5 advanced)

**F7 remains open.** This entry advances the durable parent recovery leg only. It does not close
F7.2, F7.3, F7.4, F7.5, or F7.6; the full MIRA board oracle, competing/depth-race matrix, and a
named coding-harness consumer remain outside this slice.

- *Changed behavior and concrete consumer:* `WorkSettlementEnvelope` in
  `packages/schemas/src/thymira/schemas/lifecycle.py` is persisted as the lifecycle owner's
  `WorkResult.payload` by `OrchestratorBoard._work_result`. It binds the exact Run, `work_id`,
  attempt, claimant, claim token, and typed `SubagentResult`. `OrchestratorBoard.recover_settlement`
  and `recover_settlements` are the named Core parent recovery consumers: after owner settlement,
  they validate the envelope against the persisted `WorkItem` claim and owner result, then repair a
  missing board projection through the existing root-owned CAS path. `settlement_notice` and
  `canonical_settlement` expose only an envelope that agrees with the board projection and use the
  shared `select_canonical` selector.
- *Real behavioral test nodes and command results:* malformed owner payload and canonical notice
  recovery are covered by `tests/thymira/test_goal_board.py::test_orchestrator_recovery_reads_the_owner_inbox_and_shared_selector`;
  a forged claim token is refused by `::test_orchestrator_rejects_a_parent_notice_bound_to_another_claim`;
  a persistent three-attempt DAG CAS outage followed by idempotent repair is covered by
  `::test_orchestrator_recovers_a_persisted_owner_settlement_after_cas_outage`; the real
  `LocalLifecycleRepository` and `LocalDagRepository` path is covered by
  `tests/thymira/test_state_lifecycle_integration.py::test_orchestrator_board_projects_the_owner_settlement_for_recovery`.
  Focused commands:
  `uv run pytest tests/thymira/test_goal_board.py::test_orchestrator_recovery_reads_the_owner_inbox_and_shared_selector tests/thymira/test_goal_board.py::test_orchestrator_rejects_a_parent_notice_bound_to_another_claim tests/thymira/test_goal_board.py::test_orchestrator_recovers_a_persisted_owner_settlement_after_cas_outage tests/thymira/test_state_lifecycle_integration.py::test_orchestrator_board_projects_the_owner_settlement_for_recovery -q --basetemp C:\\Users\\lucas\\AppData\\Local\\Temp\\f7-envelope-recovery-final-20260908`
  → **4 passed**; the earlier envelope smoke command → **4 passed**.
- *Independent invariant and authority evidence:* validation is recomputed from three retained
  facts: the persisted lifecycle `WorkItem` claim identity, the typed owner `WorkResult`, and the
  orchestrator's independently loaded `DagNode`; equality with a copied payload alone is
  insufficient. Removing or changing the claim token, omitting the envelope, or withholding the
  board projection therefore causes refusal or explicit recovery. Only the live root owner may
  mutate the board, and the lifecycle owner remains the sole claim/effect-settlement writer; no
  Policy Engine, approval, MIRA, THY/MIRA boundary, or checkpoint state is used as authority.
- *Remaining clause inventory:* F7.2 still needs the full parent action/rendering and bounded
  diagnostics closure; F7.3 still needs its complete concurrent CAS, dependency, depth, and
  restart matrix; F7.4/F7.6 still need independent MIRA board evidence and the named
  coding-harness authority consumer; F7.5 still needs every higher-level consumer and selection
  across distinct attempts. The existing checkpoint `agent_messages` path remains open and is not
  claimed as repaired by this entry.

**2026-09-08 — F3.1, F3.2 and F3.3 CLOSED; F3.6 ADVANCED.** File discovery persistence now

**2026-09-08 — F3.1 and F3.3 CLOSED; F3.2 and F3.6 ADVANCED.** File discovery persistence now
fails closed: `glob`, `grep` and `list_files` retain their bounded caller rendering but return a
failed result with `FS_DISCOVERY_PERSISTENCE` when the complete authoritative list cannot be
saved. `ToolManager` records that result as `tool.completed` with `status=FAILED`. The content
and footer of `read_file`, `glob` and `grep` fit their documented UTF-8 byte budgets, including
multi-byte and exact-boundary cases; `read_file` counts a final newline in its exact fast path;
`list_files` budgets its JSON envelope and never emits a character-truncated path. Successful
discovery calls still persist a fresh, path-ordered full list before returning, with grep's
skipped files included. The configured production registry shares an in-memory read ledger and
requires a prior read for existing-file writes/edits, with stable stale, unread, no-match and
ambiguous-match refusals. Every six file tools declares and exercises a machine-readable contract.

- Identifier and changed behavior: F3.1 and F3.3 are closed for the covered file tools. F3.2 is
  advanced: the manager now refuses `os.walk` enumeration errors, retains path and digest facts
  for every observed file, retains the exact UTF-8 content snapshot needed for `grep`, and MIRA
  independently recomputes each `glob`, `grep` and `list_files` projection from the recorded query
  and retained source facts. F3.2 remains open until the source-content artifact has the private
  visibility required by G1 and its bounded evidence policy is accepted. F3.6 is advanced: all
  fifteen registered effectful tools now require a bounded `description` argument, and the
  production-registry inventory reports zero omissions (`analyze_dataset`, `audit_model`,
  `compare_models`, `git_commit`, `git_worktree_create`, `git_worktree_remove`, `inspect_model`,
  `mlflow_end_run`, `mlflow_log_artifact`, `mlflow_log_metric`, `mlflow_log_param`,
  `mlflow_start_run`, `profile_dataset`, `query_sql` and `run_statistics`). Their existing
  capability declarations were reviewed against actual local artifact, workspace, sandbox and
  tracker behavior; broader concurrency contracts, shared registry strictness and guidance remain
  open. F3.4 (dispatch-time restrictions) and F3.5 (canonical output schemas for every tool)
  remain open. A missing discovery artifact is now an unsuccessful operation even when a useful
  bounded prefix remains available for recovery.
- Producer and concrete call site: `ReadFile`, `Glob`, `Grep` and `ListFiles` in
  `runtime/tools/src/thymira/tools/builtins/{files,search}.py`; shared limits in
  `output_bounds.py`; `persist_discovery` in `discovery.py`; `ToolManager` independently captures
  and rechecks a source witness in `discovery_witness.py`, persists it beside the producer
  artifact, and records the witness ID. `builtins_registry()` wires the configured
  `FreshnessPolicy` and shared `ReadLedger`; `ToolManager._close_record` records the failed
  result. MIRA's independent `a32_discovery_evidence` reads the event log and both artifacts
  without importing `thymira.tools` or rescanning the later workspace.
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_mira_discovery_evidence.py tests/thymira/test_review_filetools_a32_completeness.py
  tests/thymira/test_tools_discovery.py
  tests/thymira/test_tools_files.py tests/thymira/test_tools_output_bounds.py
  tests/thymira/test_tools_contracts.py tests/thymira/test_tools_freshness.py
  tests/thymira/test_tools_mcp_contract.py -q
  --basetemp .pytest-tmp/dsh-filetools-a32-final` → `81 passed`. This includes actual producer
  paths for all three discovery tools under a failing store, the real manager lifecycle, byte,
  character and multi-byte output boundaries including the footer, adversarial path ordering,
  fresh-name persistence, stale/unread writes, literal zero/ambiguous matches, and all six
  registry contracts.
- Independent oracle and its result: `test_a32_passes_a_real_capped_glob` reopens the JSONL
  event log and artifact store through separately constructed readers, verifies the event hash
  chain and store manifest, and compares the persisted list to an independent `os.walk`/`fnmatch`
  enumeration. The manager-owned source witness is a separate artifact, captured around
  execution; A33 binds its query identity and source digest to `tool.started`, checks the witness
  snapshot, then independently recomputes each projection from the recorded query and retained
  source facts before requiring the producer list to equal that projection. The companion A33
  nodes refuse missing artifacts, malformed totals/prefixes, tampered bytes, duplicate entries,
  out-of-order lists, a hash-valid witness projection that omits a real `b.txt`, and a grep witness
  without content facts. The control re-declares its schema and relationship checks instead of
  trusting producer success flags or the tools helper. The manager failure node independently
  confirms enumeration errors become a durable failed call before `tool.started`, with the stable
  error.
- Applicable MIRA/policy evidence: MIRA control A33 passes the real capped discovery artifact
  and fails each forged/missing/malformed evidence case; the freshness refusal fold recomputes
  `FS_READ_REQUIRED`/`FS_STALE_VERSION` from the recorded request path. The Policy Engine still
  authorizes the tool call before execution, while persistence failure determines the final
  result status in code. F11.1–F11.3 and G.3 are advanced for these `thymira-tools`/
  `thymira-mira` observations, with append/replay and independent-reader evidence, but remain
  open as program-wide foundations because other packages and tool-result projections are not
  covered here.
- Retained limitations: the source witness is an execution-time snapshot; A33 does not claim
  that a later workspace is unchanged, and the manager refuses a pre/post execution drift. The
  witness scan duplicates discovery semantics in an independent manager-owned module so the
  oracle need not import producers; it is not a MIRA live filesystem scan. Grep source content is
  currently retained inside the manager witness artifact without a dedicated private-visibility
  field; G1 must close that data-handling contract before F3.2 can close. Freshness evidence is
  in-memory and scoped to the registry; direct ad hoc tool construction leaves that policy off
  for legacy callers. `list_files` has no offset/limit paging, and `line_numbering` remains
  intentionally `none`. F3.4, F3.5, F3.6, F11 and G.3 are not closed by this slice.

**2026-09-08 — F3.2 bounded-witness repair ADVANCED, REMAIN OPEN.** The final pre-dispatch
boundary review found three remaining gaps in the manager witness and they are now covered by
the production path. `_stream_file` enforces the passed remaining scan and retained-content
budgets on each chunk and after the actual read, so a file growing after `stat()` is refused before
`tool.started`. `capture_discovery_source` passes one monotonic deadline through enumeration,
metadata, hashing, glob/grep projection and incremental witness serialization; the serializer
emits canonical fields and list items incrementally and applies a conservative per-value bound
before encoding. The registered Grep producer and MIRA's independent retained-snapshot projection
use direct `regex` timeouts, with `regex` declared in both runtime members and locked in `uv.lock`.

- Real behavioral nodes and exact command results:
  `uv run pytest tests/thymira/test_review_filetools_a32_completeness.py tests/thymira/test_mira_discovery_evidence.py tests/thymira/test_tools_discovery.py tests/thymira/test_tools_files.py tests/thymira/test_tools_output_bounds.py tests/thymira/test_tools_contracts.py tests/thymira/test_tools_freshness.py tests/thymira/test_tools_mcp_contract.py -q --basetemp C:\Users\lucas\AppData\Local\Temp\filetools-final-focused-20260908` → `93 passed`; the final recheck after the deadline/hash refinement was `40 passed` on the core witness/MIRA/discovery subset. New actual-path nodes cover a growing file crossing a 100-byte aggregate budget, a delayed `os.walk` crossing a 10-ms witness deadline, incremental serialized overflow without calling `payload()`, producer regex timeout, and independent MIRA regex timeout.
- Independent proof: the growing-file node uses the real `ToolManager` and records no
  `tool.started`; the serialization node patches `DiscoverySourceWitness.payload` to fail if the
  complete object is materialized; the MIRA timeout node runs the manager to create genuine events
  and artifacts, then changes only MIRA's independent timeout before `audit_run`. Existing A33
  nodes continue to reopen the event log and artifact store independently, verify the hash chain
  and manifest, recompute the query projection from the retained snapshot, and reject forged
  membership, missing content and malformed/oversized witness evidence.
- Exact frozen provenance for the rejected baseline: `bdbb3ad76adb60836c98a90df9658b2f3d9659d0`.
  Repair commit provenance is recorded by the implementer after the atomic commit. F3.1/F3.3
  remain closed as previously recorded; F3.4 (dispatch restrictions), F3.5 (all tool output
  schemas), G1 private visibility for retained grep content, and program-wide F3/F11/G3 remain
  open. The witness remains an execution-time snapshot and refuses pre/post workspace drift; a
  later live scan is not used as proof.

**2026-09-08 — F3.2/G1 source-duplication boundary ADVANCED, OPEN.** The registered `Grep`
producer now reads in bounded binary chunks with the existing per-file limit, a shared aggregate
scan limit, a match cap and its own monotonic deadline through enumeration, regex projection,
rendering and discovery persistence. A file-growth race is refused before an unbounded text read.
The manager's witness uses the shared G1 credential classifier and refuses a known credential in a
retained grep source before `tool.started` or a duplicate `LOG` artifact, with a stable explicit
`FS_DISCOVERY_CREDENTIAL_EXCLUDED` outcome. This preserves the arbitrary-dataset witness path
without redacting bytes and pretending A33 can recompute facts from an altered snapshot.

- Real behavioral nodes: the focused repair probes in
  `tests/thymira/test_review_filetools_a32_completeness.py` cover the growth race, producer read
  deadline, aggregate scan bound, post-persistence deadline and credential refusal. The five
  probes pass in isolation;
  the broader discovery/A33 run is currently blocked by the unrelated Windows read-only
  `LocalArtifactStore` `fsync` defect inherited from the selected `b831890` integration base and
  owned by the storage repair lane.
- Independent proof and retained limitation: MIRA A33 continues to read only durable witness
  facts and independently recompute the query; a refused credential-bearing call has no
  `tool.started`/witness to audit and therefore cannot become a false pass. Noncredential grep
  snapshots remain byte-faithful but still have no dedicated private-visibility field, so G1 and
  F3.2 remain open program-wide. No foundation or policy closure is claimed.

**2026-09-08 — F3.2/G1 immutable-object consumer repair ADVANCED, OPEN.** The bounded public
`ArtifactStore` reader now resolves a logical manifest name to its recorded immutable content URI
before applying its byte bound and returning bytes. This repairs MIRA A33's independent witness
read against the G1 `.batches` layout without adding a filesystem reader or trusting producer
claims. Tamper probes derive their mutation target from the recorded URI, while MIRA still reads
through `ArtifactStore`, compares the independent artifact-created digest and recomputes the query
projection from retained facts.

- Real behavioral nodes: `tests/thymira/test_review_filetools_a32_completeness.py` passes all 22
  focused source, projection, tamper, bounded-read and producer-budget probes; storage's public
  logical-name bounded-read assertions pass for both current and preserved immutable revisions.
- F3.2 and G1 remain open program-wide: retained grep content still lacks the accepted private
  visibility field, and no foundation closure is claimed.

**2026-09-10 — F13.2 ADVANCED, REMAINS OPEN.** `codex/dsh-model-route-policy` landed with
integration wave 10 (PR #161, `main` `2adb25be`). A frozen `ModelRoutePolicy` (exact route
allowlist, version, authority, sha256) is snapshotted at Session creation, copied into the
authoritative `run.started` record and published as a distinct `model.route_policy_snapshotted`
event before dispatch; every production gateway seam (agent runner, plan/summarize, risk, MIRA
verification, compaction, injected doubles) requires a bound policy and runs the code-owned
`enforce_model_route` after `model.selected` and before the provider call, recording
`model.route_denied` as durable evidence; an unset `THYMIRA_ALLOWED_MODEL_ROUTES` or an
unavailable Run identity is an explicit empty fail-closed snapshot, and updates can only narrow.
Independent review: `handoff-codex/model-route-policy-review-final.md` (ACCEPT for the bounded
scope at b27a8dd). Evidence: wave gate `just check` 3246 passed, 3 skipped, 42 slow deselected; ty 0 diagnostics, `Contracts: 4 kept, 0 broken`, Agent Notes valid; `just test-slow` 41 passed, 1 skipped; the suite
binds `TEST_ROUTE_POLICY` at every provider seam (63 test modules) and `tests/conftest.py` clears
the routing environment so a nested worktree's `.env` cannot rebind it. Open remainder: the
session snapshot is published only when a Run is materialized, in that Run's chain (it is not
independent pre-Run Session provenance); the owner publication that creates a Session after Run
materialization is integration-pending; F13.3 and the remaining F13 criteria were not evaluated.

**2026-09-10 — Real-model end-to-end smoke: two runtime defects found and repaired (F5 evidence).**
Smoke #6 on `main` 93b3dec4 (`scripts/real_e2e_smoke.py`, real THY/MIRA model, `examples/credit-risk`,
run `run_5a9e9662d6ce4527bc1c250bf6b5b9fd`) completed the MIRA risk interview only through its
bounded question limit: the model re-asked `sensitive_attributes` nine times because the smoke's
canned answer left completeness open and the smoke repeated the same sentence, the runtime
escalated the limit to a human review (GOV-006, REQUIRE_HUMAN_REVIEW), and the human approval then
killed the Run — the park carried no resume target, `resolve_approval` recorded
`resume_from: approval`, the dispatcher re-entered the Gate, and the Gate's outgoing edge demanded a
policy decision the Run had never reached (`runtime graph Gate node did not produce a policy
decision`, events 174–180). Repaired in PR #162 (`main` `c83fd595`): the question-limit park
persists `INTERVIEW_LIMIT_RESUME_TARGET`, `review_resume_target` accepts it and the dispatcher
anchors it at `start` so the interview records the resolved review and proceeds to preflight; a
refusal recorded out of band (governance reject route) followed by a plain `resume` now blocks
the Run with the rejector's attribution instead of re-requesting the same review without bound
(found by adversarial review of the first repair). Evidence: composed-graph regressions for both
approval paths and both refusal paths (the out-of-band refusal test fails without the `runs.py`
change), `review_resume_target` unit tests, Agent Note
`interview-limit-review-resumes-the-interview.md`; gate `just check` 3262 passed, 3 skipped, 42 slow deselected; ty 0, `Contracts: 4 kept, 0 broken`, Agent Notes valid, `just test-slow`
41 passed, 1 skipped. The smoke now answers every one of the nine interview fields with dataset-grounded facts
(21 columns; protected attributes `personal_status_sex`, `age`, `foreign_worker`) and never repeats
itself blindly. Smoke #7 on `main` c83fd595 then failed on its first provider call because wave 10's session
model-route policy denies every route until the operator sets `THYMIRA_ALLOWED_MODEL_ROUTES`
(undocumented until PR #163); smoke #8 on `main` c83fd595 with the allowlist set: PASS —
`run_635b0c4c9ce64cdab27aaaba6d82fd25` completed with governance decision `WARNING` (449
hash-chained events; 12 interview questions / 16 answers, the bounded limit escalated to a human
review that was approved and resumed into the interview as designed; 9 human approvals for 9
policy-gated tool calls, all 9 executed; 2 MIRA audit findings; 0 route denials;
`handoff-codex/claude-real-smoke-main-8.log`).

**2026-09-10 — F3.1/F3.3 citation correction.** The 2026-09-08 closure entry at line 1752 ("F3.1
and F3.3 CLOSED; F3.2 and F3.6 ADVANCED") carries no commit citation at all: the pre-integration
worktree hashes that slice was verified in — `b2bbd5d`, `a6e50a3`, `ea3f76f1` — appear nowhere in
this record (a grep over the whole file returns no match) and all three fail `git merge-base
--is-ancestor <sha> main` (exit 1) while remaining real objects in this clone (`git cat-file -t`
reports `commit`), so the closure had no anchor on `main`, quoted or reachable. That entry also
names the recomputation control inconsistently — line 1786 calls it `a32_discovery_evidence`
while lines 1803, 1805, 1812 and 1820 call the identical relation "A33" — and its oracle bullet
at line 1799 names `test_a32_passes_a_real_capped_glob`, a node that does not exist on `main`
(`tests/thymira/test_review_filetools_a32_completeness.py` collects 22 nodes from 20 test
functions, none of that name). This entry anchors both closures to `main` commit
`93b3dec48a39923380053c1c5487e80249756b18` ("feat: integrate DSH wave 9 (#159)";
`git merge-base --is-ancestor 93b3dec4 main` exits 0), the commit that introduced the F3
producers, MIRA's discovery-evidence check and the 2026-09-08 entry itself, and it corrects the
control name: `runtime/mira/src/thymira/mira/checks/controls.py` registers
`Control("A32", "Workspace quota evidence", Severity.CRITICAL, a32_workspace_quota)` at line 1641
— a distinct tmpfs-quota check whose function is defined at line 1524 with the docstring
"Recompute the bounded tmpfs workspace quota from replayed event evidence." at line 1525 — and
registers A33 over lines 1643-1648 with control id `"A33"`, title "Discovery evidence
substantiates the rendered result", `Severity.HIGH` and `a33_discovery_evidence`. The
recomputation the 2026-09-08 entry describes executes as `A33`. This entry makes no new
behavioral claim and does not reopen or expand F3.1/F3.3: it replaces a missing citation, a dead
node id and a stale control label under the CLOSED status that entry already recorded, and it
reports three defects it does not repair.

- Identifier and changed behavior: F3.1 ("bound actual file-tool output") and F3.3 ("enforce the
  read-before-edit policy") keep the CLOSED status the 2026-09-08 entry recorded, unchanged in
  scope. This is a citation and nomenclature correction only: no code, test or prior entry was
  edited. It claims nothing for F3.2, F3.4, F3.5 or F3.6. Two record-state contradictions are
  left as found. (a) Line 1750 carries a truncated duplicate headline ("F3.1, F3.2 and F3.3
  CLOSED; F3.6 ADVANCED." followed by the dangling clause "File discovery persistence now" and a
  blank line) claiming F3.2 CLOSED, while line 1752 leaves F3.2 ADVANCED; `git log -S` over the
  record shows commit `93b3dec4` added both. (b) The F3 criteria headings still read
  `**F3.1 [OPEN]**` (line 158) and `**F3.3 [OPEN]**` (line 165), so the section headings and the
  2026-09-08 entry disagree about both identifiers; this entry corrects citations only and
  changes no heading.
- Producer and concrete call site: `ReadFile` (`runtime/tools/src/thymira/tools/builtins/files.py`
  line 184) and `ListFiles` (same file, line 508); `Glob`
  (`runtime/tools/src/thymira/tools/builtins/search.py` line 150) and `Grep` (same file, line
  295); the shared limits in `runtime/tools/src/thymira/tools/builtins/output_bounds.py`;
  `persist_discovery` at `runtime/tools/src/thymira/tools/builtins/discovery.py` line 46; the
  manager-owned source witness in `runtime/tools/src/thymira/tools/discovery_witness.py`. Every
  path resolves at the anchor (`git cat-file -e 93b3dec4:<path>`), and `git diff 93b3dec4..HEAD`
  over those six files plus `controls.py`, `discovery_evidence.py` and the four test files below
  is empty, so a run at today's HEAD is evidence for the `93b3dec4` tree of exactly those files.
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_tools_output_bounds.py tests/thymira/test_tools_files.py
  tests/thymira/test_tools_freshness.py tests/thymira/test_review_filetools_a32_completeness.py -q
  -p no:randomly --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F3` → `68 passed in 9.04s`
  (8 + 26 + 12 + 22 collected, confirmed by four separate `--collect-only` runs). Run from a clean
  git worktree on branch `docs/dsh-closure-entries-2026-09-10`, created from `main` HEAD
  `f949f87c2c16ee0971dcf7f5b0c9e078cdffa3b5`, which has `93b3dec4` as an ancestor; no tracked file
  under `runtime/` or `tests/` differs from that HEAD.
- Independent oracle and its result: on `main` the discovery-evidence recomputation is registered
  and asserted only as `A33`. `tests/thymira/test_review_filetools_a32_completeness.py` makes six
  `control.control_id == "A33"` selections against a real `audit_run` report (lines 168, 295, 332,
  399, 622, 707) and contains zero occurrences of the string `"A32"`;
  `test_a33_recomputes_grep_from_the_retained_content_snapshot` (line 300) asserts
  `a33.status is ControlStatus.PASSED` at line 333, and five sibling nodes assert `FAILED` (lines
  169, 296, 400, 623, 708) for incomplete, non-recomputable, forged and oversized evidence. The
  file keeps its pre-renumbering `a32` filename. The oracle node the 2026-09-08 entry cites,
  `test_a32_passes_a_real_capped_glob`, is absent on `main` and is withdrawn as a citation; the
  passing node named here replaces it.
- Applicable MIRA/policy evidence: the `A33` control registered at `controls.py` lines 1643-1648
  delegates to `check_discovery_evidence`
  (`runtime/mira/src/thymira/mira/checks/discovery_evidence.py` line 727), which the 2026-09-08
  entry established recomputes the freshness refusal and the rendered-versus-persisted discovery
  projection from event and artifact facts without importing `thymira.tools`. Only the label
  attached to that evidence changes here; the 68-node run above re-exercises the evidence
  unchanged. No policy decision, approval or authorization path is touched by this entry.
- Retained limitations: this correction re-verifies nothing beyond the citations, the control name
  and that test run. Every substantive limitation of the 2026-09-08 entries still applies and none
  is restated as closed — grep source content retained inside the manager witness artifact with no
  dedicated private-visibility field, so G1 and F3.2 stay open; freshness evidence in-memory and
  scoped to the registry, leaving ad hoc tool construction unpoliced; `list_files` with no
  offset/limit paging and `line_numbering` intentionally `none`; the witness an execution-time
  snapshot, not a MIRA live filesystem scan. Platform: the 2026-09-08 follow-up at lines 1868-1871
  recorded the broader discovery/A33 run as blocked on Windows by a read-only `LocalArtifactStore`
  `fsync` defect; that blockage no longer holds at the anchor — all 22 nodes of that file passed on
  Windows 11 in the run above — and no platform limitation is carried forward for F3.1/F3.3. A
  third defect is reported and not repaired: `A32` is not free of the discovery relation on `main`
  — `controls.py` `INVARIANT_CLAIMS` (lines 1695-1714) declares `invariant_id="A32"`,
  `control_id="A32"`, `producer="thymira.tools.builtins.discovery.persist_discovery"`,
  `checker="check_discovery_evidence"` and `failure_code="MIRA_A32_DISCOVERY_MISMATCH"`, and
  `build_invariant_registry` (`controls.py:2096`) binds `"A32": _a32_independent_relationship`
  (line 2113), so that discovery claim resolves through `by_id` to the workspace-quota control.
  That mis-binding is a code fact reported here, not a claim this entry closes or corrects;
  F3.1/F3.3 are anchored on the `CONTROLS`/`audit_run` path, which the tests above exercise as
  `A33`.

**2026-09-10 — F5.4 ADVANCED, REMAINS OPEN.** PR #145 (`main` `b69f14e2`) narrows one clause of
F5.4's heading (line 227): "Human questions require live root-agent/user authority, not a caller
label or durable lineage alone." A merely-declared, unauthenticated human actor can no longer
resolve an execution-start review, the Policy Engine's `allows_execution` no longer honours an
`Approval` from one, and the API composition can no longer be built in the permissive mode that
skipped authentication entirely. The heading's other three clauses — closed outcomes for
allowed-once/rejected/cancelled/unavailable, `never` decided before any responder, and a delegated
scope that cannot widen itself — are untouched by this commit and rest on the 2026-09-07 entry
(lines 782-823); this entry adds no evidence for them. The criterion still cannot close: nothing
here ties the approving principal to a live session/Run handle, which remains F13.3's unmet work.

- *Changed behavior:* `validate_execution_resolver` now rejects a declared-but-unauthenticated
  human `Actor` before it can resolve an execution-start Gate review, and both
  `human_approval_outcome` and `human_rejection_actor` count a `human.approval` event only from an
  authenticated actor — the same commit also replaced their
  `event.payload.get("automatic") is not True` test with
  `("automatic" not in event.payload or event.payload["automatic"] is False)`, so a non-bool
  `automatic` marker no longer passes either. Separately, `RuntimeDeps.principal_resolver` is a
  required field with no default and `build_default_deps` raises instead of composing an anonymous
  API when it is `None`; `enforce_permission` no longer has a branch that returns without
  authenticating.
- *Producer and concrete call site:* `validate_execution_resolver`
  (`runtime/core/src/thymira/core/execution_review.py:105-112`) raises
  `ValueError("execution approval requires an authenticated human actor")` at lines 109-110 when
  `actor.authenticated` is `False`; its call site is `RunService.resolve_approval`
  (`runtime/core/src/thymira/core/runs.py:581-585`), which calls it on the
  `EXECUTION_START_RESUME_TARGET` branch and re-raises that `ValueError` as `RunApprovalError`.
  `human_approval_outcome` (`execution_review.py:174-190`) and `human_rejection_actor`
  (`execution_review.py:193-209`) carry the same `event.actor.authenticated` conjunct.
  `RuntimeDeps.principal_resolver` (`apps/api/src/thymira/api/deps.py:135`) is declared
  `principal_resolver: PrincipalResolver = field(repr=False)` — no `| None`, no default (it was
  `PrincipalResolver | None = None`); `build_default_deps`'s own parameter (`deps.py:222`) lost
  the same default, and `_require_composition_credentials` (`deps.py:166-172`, called at
  `deps.py:249`) raises
  `TypeError("principal_resolver is required; anonymous API composition is disabled")`.
  `enforce_permission` (`apps/api/src/thymira/api/principal.py:96-114`) reads
  `get_runtime_deps(request).principal_resolver` at line 106 and always authenticates; the prior
  `if resolver is None: return` early exit is deleted in the `b69f14e2` diff and absent from the
  current function body.
- *Real behavioral test node and command result:*
  `tests/thymira/test_core_execution_review.py::test_declared_unauthenticated_human_cannot_resolve_execution_review`
  (`tests/thymira/test_core_execution_review.py:547-562`, added by `b69f14e2`) builds a Gate with
  `Actor(kind="human", id="cli-reviewer", authenticated=False)`, asserts
  `pytest.raises(RunApprovalError, match="authenticated")` on `resolve_approval`, then that the Run
  stays `WAITING_FOR_APPROVAL` and no tool call was recorded. `uv run pytest
  tests/thymira/test_core_execution_review.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F54` → `27 passed in 64.16s (0:01:04)`.
  The API half is
  `tests/thymira/test_api_auth.py::test_the_api_composition_refuses_an_optional_resolver_escape`
  (`tests/thymira/test_api_auth.py:83-89`), which asserts the exact `TypeError`: `uv run pytest
  tests/thymira/test_api_auth.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F54api` → `9 passed in 3.20s`.
- *Independent oracle and its result:* MIRA recomputes the predicate from the log alone and refuses
  the declared answer.
  `tests/thymira/test_mira_checks.py::test_a7_does_not_credit_a_declared_human_answer`
  (parametrized `[True]`/`[False]`) drives control A7 over a hash-valid `human.approval` written by
  `Actor(kind=ActorKind.HUMAN, id="claimed-reviewer", authenticated=False)` and asserts
  `ControlStatus.FAILED` with `"without an answer"`;
  `tests/thymira/test_policies.py::test_allows_execution_refuses_an_approval_from_a_declared_human`
  asserts `allows_execution` is `False` for that `Approval`; and
  `tests/thymira/test_policies_approval.py::test_pending_fold_ignores_a_valid_chain_answer_from_an_unauthenticated_human`
  (also parametrized `[True]`/`[False]`) asserts the request stays pending. Those three node ids
  collect five nodes; `uv run pytest` over them `-q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F54oracle` → `5 passed in 3.27s`.
  Provenance oracle: `git merge-base --is-ancestor b69f14e2069af474b29e21ef12a524842d22fc7a main`
  exits 0, and `git show b69f14e2` is what removes both the permissive
  `principal_resolver: PrincipalResolver | None = None` field and the missing
  `actor.authenticated` check, so the change is on `main`, not on an unmerged branch.
- *Applicable MIRA/policy evidence:* the same commit tightened the Policy Engine and MIRA, so this
  is not a Core-only change. `allows_execution`
  (`runtime/policies/src/thymira/policies/engine.py:591-597`) now also requires
  `approval.approved_by.kind is ActorKind.HUMAN` and `approval.approved_by.authenticated`;
  `_answering_actor` (`runtime/policies/src/thymira/policies/gate.py:100-112`) raises
  `ValueError("approval requires an authenticated human actor")`; `LocalApprovalService.resolve`
  (`runtime/policies/src/thymira/policies/approval.py:245`) raises the same message, and
  `_closes_pending_approval` (`approval.py:274-290`, applied at `approval.py:106`) keeps a declared
  human's answer out of the pending fold. On the MIRA side `_approval_answers_request`
  (`runtime/mira/src/thymira/mira/checks/controls.py:162-177`, used by the A7/A8 resolution folds)
  and `is_human_answer` (`runtime/mira/src/thymira/mira/checks/ticket_pool.py:34-51`) carry the same
  predicate, so the independent audit refuses what the runtime refuses. No rule model
  (`ActionRule`/`CapabilityRule`/`FindingRule`) and no policy YAML under `runtime/policies` changed;
  the only YAML in the commit is `.pre-commit-config.yaml` (`git show --stat b69f14e2 -- "*.yaml"
  "*.yml"` lists that file alone). `apply_execution_decision` (`execution_review.py:61-88`) did
  change here too: the module it imports `policy_decision_binding` from
  (`runtime/core/src/thymira/core/governance_binding.py`) is a **new file** in `b69f14e2`
  (`git show --diff-filter=A --stat b69f14e2 -- <path>` reports 145 insertions), and the same
  commit added the `policy_decision_binding(decision)` payload to both `RunTransitionKind.BLOCK`
  advances — it is not pre-existing wiring.
- *Retained platform limitation:* F5.4 stays ADVANCED, not CLOSED. F13.3 `[OPEN]`
  (`docs/superpowers/plans/2026-09-07-dsh-acceptance.md:486-489`) still requires "an explicit
  session/Run handle at publication" that "reject[s] a second live writer"; nothing in
  `runtime/core` or `apps/api` acquires such a handle today. An authenticated principal is now
  required, which rules out an unverified caller-declared identity, but nothing binds that
  authenticated principal to live, exclusive authority over the one Run it is approving: a
  different authenticated human with the same permission, or a replayed credential from a resolved
  session, is not distinguished from the Run's actual live approver. That is exactly the unmet
  "live root-agent/user authority" proof the 2026-09-07 entry named (lines 821-823), and it remains
  unmet on `main`. This entry also carries no evidence for F5.4's other three heading clauses, and
  F13.3 itself was not evaluated beyond confirming its `[OPEN]` tag is current.

**2026-09-10 — F8.1 CLOSED.** Both clauses of the criterion have behavioral evidence on `main`:
compaction writes exactly one `context.compacted` event as the last statement of `compact_surface`
(ADR-0007's single atomic append), and the closed `EventType` enum declares no companion
opening/bracket member, so no separate bracket or log-parenthesis record can exist to be left
behind by a partial write.

- *Identifier and changed behavior:* F8.1. `compact_surface` selects the events to shadow, obtains
  the summary, rebuilds the source checkpoint and computes the before/after digests first, then
  performs a single `ctx.event_log.append(EventType.CONTEXT_COMPACTED, ...)` as its final
  statement. That append is the only write of `EventType.CONTEXT_COMPACTED` anywhere in production
  code, not merely in its own file: the member's eight other production occurrences
  (`runtime/thy/src/thymira/thy/compaction.py:131`,
  `runtime/agents/src/thymira/agents/context_recovery.py:356`,
  `runtime/agents/src/thymira/agents/prompts.py:183`,
  `runtime/agents/src/thymira/agents/runner.py:613` and `:647`,
  `runtime/mira/src/thymira/mira/agents/compaction_fidelity.py:126`,
  `runtime/mira/src/thymira/mira/checks/compaction.py:61`,
  `packages/events/src/thymira/events/surface.py:63`) are all read-side `event.type is ...`
  comparisons over already-written events. The closed `EventType` enum
  (`packages/schemas/src/thymira/schemas/enums.py:245`) declares exactly one compaction member,
  `CONTEXT_COMPACTED = "context.compacted"`; there is no "compaction started" or bracket type to
  pair it with, which is what makes a partial write unrepresentable rather than merely unobserved.
  The "single append" claim is scoped precisely: no second compaction record and no opening event
  is written -- not that the function is otherwise log-silent. Earlier statements can write *other*
  event types: `enforce_model_route(..., event_log=ctx.event_log, actor=ctx.actor)`
  (`compaction.py:289`) appends `model.route_denied` on the deny path before raising
  (`runtime/agents/src/thymira/agents/route_policy.py:77`), and when the provider exposes
  `attach_request_ledger` (`LiteLLMProvider`,
  `runtime/agents/src/thymira/agents/llm/litellm_provider.py:265`) the
  `RequestLedger(ctx.event_log)` built at `compaction.py:297` records the model request/response.
  Neither is a compaction record nor a bracket half.
- *Producer and concrete call site:* producer `compact_surface`
  (`runtime/thy/src/thymira/thy/compaction.py:260-348`; the append is lines 322-348,
  `return ctx.event_log.append(EventType.CONTEXT_COMPACTED, ...)`, blamed to `main` `e04ee869`,
  confirmed by `git merge-base --is-ancestor e04ee869 main`). Its only production call site is
  `ContextBudget.fit` (`runtime/thy/src/thymira/thy/budget.py:158`) at `budget.py:226`, inside the
  overflow retry loop: each iteration that has something to shadow produces exactly one event, and
  an iteration with nothing to shadow returns `None` and writes nothing. "Once" is therefore per
  compaction operation, not per Run -- a Run whose prompt overflows repeatedly records one
  well-formed event per successful compaction.
- *Real behavioral test node and command result:* `uv run pytest
  tests/thymira/test_thy_context_recovery.py tests/thymira/test_mira_compaction.py -q
  -p no:randomly --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F81` ->
  `20 passed, 1 warning in 13.75s` (20 collected, 20 passed). The assigned node,
  `tests/thymira/test_thy_context_recovery.py::test_running_graph_consumes_checkpoint_and_independent_log_reader_reconstructs_it`
  (`:271`), runs a real compiled ThyGraph through a scripted budget overflow and a real compaction;
  it asserts *presence* (`any(event.type is EventType.CONTEXT_COMPACTED ...)`, `:314`), not an
  exact count. The exact-count ("once") evidence is two further live-produced nodes:
  `tests/thymira/test_thy_context_recovery.py::test_overflow_stops_after_a_durable_unchanged_reduction_attempt`
  (`:195`), in the same module and the same passing run, asserts
  `len([event for event in log.events() if event.type is EventType.CONTEXT_COMPACTED]) == 1`
  (`:231`) after one real `compact_surface` call reached through `ContextBudget.fit`; and the
  dedicated node `tests/thymira/test_thy_compaction.py::test_compaction_is_a_single_atomic_append`
  (`:191`) asserts `len(log.events()) == count_before + 1` -- the whole call adds exactly one event
  to the log -- run separately as `uv run pytest
  "tests/thymira/test_thy_compaction.py::test_compaction_is_a_single_atomic_append" -q
  -p no:randomly --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F81b` -> `1 passed in
  1.78s`.
- *Independent oracle and its result:* the assigned node rebuilds the log from raw JSONL with a
  fresh reader that never consults the producer's live list or the provider's returned object --
  `reconstructed = read_events(path)` (`:310`) -- and asserts `verify_log(path).valid` (`:319`), so
  the one compaction record is hash-chain-verified from outside the producer. MIRA's registered
  control A27 ("Compaction integrity", CMP-01) is the second, independent oracle:
  `check_compaction_integrity` (`runtime/mira/src/thymira/mira/checks/compaction.py:50`) folds
  every `context.compacted` event and returns `FAILED` for one that shadows forward, carries a
  malformed `shadowed_seqs`, or omits/incompletely fills the summarisation envelope; it is
  dispatched by `a27_compaction_integrity` (`runtime/mira/src/thymira/mira/checks/controls.py:1284`)
  and registered into `audit_run` at `controls.py:1637`
  (`Control("A27", "Compaction integrity", Severity.HIGH, a27_compaction_integrity)`).
  `tests/thymira/test_mira_compaction.py::test_compaction_control_is_registered_in_audit_run`
  (`:126`) asserts `audit_run` returns a `PASSED` A27 control for a well-formed single compaction,
  and the five `test_compaction_control_fails_*` nodes in the same passing run
  (`test_mira_compaction.py:73`, `:84`, `:94`, `:106`, `:116`) each exercise one malformed shape.
- *Applicable MIRA/policy evidence:* control A27/CMP-01 above is the applicable evidence; it is
  folded into `audit_run`'s `AuditReport` at `Severity.HIGH`, so a Run carrying a compaction is not
  certifiable without a well-formed, envelope-complete `context.compacted` event. A27 checks
  *structure, not cardinality*: it passes N well-formed compactions (its PASSED message is
  `"{len(compactions)} well-formed compaction(s); {shadowed} event(s) shadowed"`,
  `checks/compaction.py:74`) and does not itself enforce "once" -- that clause rests on the single
  production write site and the two exact-count behavioral nodes above, not on MIRA. No Run
  transition, `PolicyDecision` or `AuthorizationContext` is otherwise touched by compaction.
- *Retained platform limitation:* "atomic" is used in the criterion's own sense -- one append with
  no bracket half (ADR-0007) -- not an OS-level crash- or fault-injection test across a process
  boundary. Durability of that single write is inherited from the `JsonlEventLog` /
  `InMemoryEventLog` append every event type uses and is not re-proven per event type by this
  entry. The exact-count nodes run with `ScriptedProvider` and an allowing `TEST_ROUTE_POLICY`, so
  the "whole call adds exactly one event" assertion holds for that configuration; under a
  ledger-capable provider the call additionally writes the request/response ledger records named
  above, which are not compaction records and are outside this criterion. The F8.1 criteria
  heading at line 329 still reads `[OPEN]`; this entry supplies the closure tuple and does not
  edit that heading.

**2026-09-10 — F8.2 ADVANCED, REMAINS OPEN.** Compaction's pair-preserving group keeps a tool call
together with its result across a compaction boundary, proven in isolation by a unit node that
fails when the mechanism is removed. The running ThyGraph compacts under a real
`Gate`/`PolicyEngine` and keeps the fresh call's authorization identifiers consistent, but its
compaction never brings the old approval/tool group near the keep boundary, so the real-graph node
is not evidence for pairing: it passes unchanged with the pairing mechanism neutralised. Of the
heading's three clauses — result, evidence, required authorization context — only *result* has
discriminating behavioral evidence on `main`. F8.2 stays OPEN.

- Identifier and changed behavior: F8.2. `_select_shadowed` unions the current surface into
  connected groups by identity value and pulls a whole group back out of the shadow set the moment
  any member would otherwise be split, so a group is wholly visible or wholly shadowed. The
  identities are the payload keys in `_PAIR_ID_KEYS` (`subject_id`, `task_id`, `tool_call_id`,
  `decision_id`, `policy_decision_id`, `approval_id`, `authorization_context_sha256`,
  `approval_scope_sha256`, `tool_intent_sha256`, `evidence_id`, `artifact_id`, `evidence_ids`,
  `artifact_ids`, `finding_ids`, `input_artifact_ids`; compaction.py:148-164) plus the event-level
  `subject_id`, `authorization_context_sha256` and `causation_id` (compaction.py:169-174). Only
  `subject_id`/`tool_call_id` grouping is behaviorally exercised on `main`; see the limitation.
- Producer and concrete call site: `_select_shadowed`
  (`runtime/thy/src/thymira/thy/compaction.py:222-249`) builds the groups via `_pair_groups` /
  `_pair_ids` (compaction.py:167-219) and pulls a boundary-crossing group out of the shadow set at
  compaction.py:246-248. Its only caller is `compact_surface` at compaction.py:286
  (`compact_surface` is defined at compaction.py:260); the running graph reaches it from
  `ContextBudget` (`runtime/thy/src/thymira/thy/budget.py:226`), which `build_thy_graph` wires
  (`runtime/thy/src/thymira/thy/graph.py:171,217`), so the path is real task execution, not tests
  alone.
- Real behavioral test node and command result:
  `uv run pytest -q -p no:randomly --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F82
  tests/thymira/test_thy_context_recovery.py::test_compaction_keeps_a_cross_boundary_tool_pair_together
  tests/thymira/test_thy_context_recovery.py::test_running_graph_keeps_authorized_tool_pairs_intact_across_compaction`
  → `2 passed, 1 warning in 2.30s`. The unit node
  (`tests/thymira/test_thy_context_recovery.py:114`) calls `compact_surface` directly over
  TOOL_STARTED/TOOL_COMPLETED events sharing `subject_id`/`tool_call_id` and asserts the unrelated
  middle event is shadowed while neither half of the pair is (assertions at
  test_thy_context_recovery.py:152-157). The real-graph node
  (`tests/thymira/test_thy_context_recovery.py:325`) invokes `build_thy_graph(...)` with a
  synchronous `Gate`, drives one compaction and one fresh reviewed tool call, and asserts
  `old_seqs.isdisjoint(shadowed) or old_seqs <= shadowed` (line 454) plus identifier consistency
  for the fresh call (lines 461-466). Measured here by wrapping `compact_surface` from a pytest
  plugin, that compaction shadows seqs `[0, 1, 2, 9]` of a ten-event surface while the old
  HUMAN_APPROVAL_REQUESTED/HUMAN_APPROVAL/TOOL_STARTED/TOOL_COMPLETED group is seqs 4-7, so its
  disjunction is satisfied by the disjoint branch without the pairing mechanism doing anything.
- Independent oracle and its result: the real-graph node rebuilds its view from a fresh
  `JsonlEventLog(path, run.id).events()` reader (test_thy_context_recovery.py:450), separate from
  the `log` object the graph wrote through, and asserts `verify_log(path).valid`
  (test_thy_context_recovery.py:467) — `packages/events` hash-chain verification; both ran inside
  the `2 passed` above. That oracle proves durable persistence and chain integrity, not the shadow
  selection: both nodes read the pruning decision from the producer's own
  `compacted.payload["shadowed_seqs"]`, and nothing recomputes the pruned surface independently.
  The discriminating oracle used for this entry is a counterfactual, run without changing the
  repository: with `thymira.thy.compaction._pair_groups` neutralised to `lambda surface: []` from a
  pytest plugin, the unit node fails (`AssertionError: assert 0 not in [0, 1]`,
  test_thy_context_recovery.py:155) while the real-graph node still passes with an identical
  measured shadow set `[0, 1, 2, 9]` — `1 failed, 1 passed, 1 warning in 2.46s`.
- Applicable MIRA/policy evidence: the real-graph node drives its reviewed call through a real
  `Gate(PolicyEngine(load_policy_stack()), log, approver=..., mode=GateMode.SYNCHRONOUS)`
  (test_thy_context_recovery.py:415-421), not a stubbed decision, and asserts the Policy Engine's
  own `decision_id`, `tool_intent_sha256`, `authorization_context_sha256` and
  `approval_scope_sha256` still tie the fresh request/approval/start/complete events together after
  the compaction (test_thy_context_recovery.py:461-466). No MIRA control is part of this evidence:
  `audit_compaction_fidelity` / `COMPACTION_FIDELITY_CONTROL_ID` (`"COMPACTION-FIDELITY"`,
  `runtime/mira/src/thymira/mira/agents/compaction_fidelity.py:107,72`) is not invoked by either
  node, and it would not settle this criterion anyway — it recovers the events a compaction
  *recorded* in its own `shadowed_seqs` (`_recovered_shadowed_seqs`,
  compaction_fidelity.py:242-244) and has a model judge summary fidelity; it does not recompute
  which events should have been shadowed.
- Retained limitation: (a) the *evidence* clause of the heading is untested. The same mechanism
  groups by `evidence_id`, `artifact_id`, `evidence_ids`, `artifact_ids`, `finding_ids` and
  `input_artifact_ids` (compaction.py:158-163), and no test in the eight modules that reference
  compaction (`test_thy_compaction.py`, `test_thy_context_recovery.py`, `test_thy_budget.py`,
  `test_mira_compaction.py`, `test_mira_compaction_fidelity.py`, `test_mira_agent_dispatch.py`,
  `test_agents_prompts.py`, `test_events.py`) drives a compaction across an evidence- or
  artifact-linked pair. (b) The *required authorization context* clause is likewise untested: the
  only node that fails when the pairing mechanism is removed groups on `subject_id`/`tool_call_id`
  alone, so no test on `main` shows that an approval request/approval record survives a compaction
  boundary attached to its call. F8.2 therefore closes neither clause and stays OPEN; the
  real-graph node still stands as evidence that the running graph compacts and re-reads its own
  log, not that pruning preserves pairs. (c) `ad48595b` ("feat(thy): add durable context recovery")
  and `a6e63465` ("fix(thy): bound resumed context dispatch"), the review/repair pair, are **not**
  ancestors of `main` (`git merge-base --is-ancestor` exits 1 for both); their content reached
  `main` only inside the squash-merged `bd5669a3` ("feat: integrate DSH wave 3 (#148)", confirmed
  an ancestor of `main`, and the first commit on `main` to contain both `_pair_groups` and the
  real-graph node). That commit's body is corroboration only — the behavioral runs above are the
  evidence.

**2026-09-10 — F8.3 CLOSED.** `prune_head_marker_tail` is a pure, synchronous function of
`(text, max_chars, head_chars, tail_chars)`: it takes no `LLMProvider` or model handle, its module
imports no model gateway, and it returns an identical head/marker/tail decomposition and identical
recorded accounting for identical arguments on every call, so no model chooses which characters
are omitted.

- Identifier and changed behavior: F8.3, mapped clause by clause to its heading (line 335) -- "The
  pruner emits head, marker, and tail deterministically, records its cost, and never relies on a
  model to choose omitted bytes". The boundary and the recorded input/output token cost are
  deterministic functions of `(text, max_chars, head_chars, tail_chars)`; the signature carries no
  model or provider dependency.
- Producer and concrete call site: `prune_head_marker_tail`
  (`runtime/agents/src/thymira/agents/context_recovery.py:115-176`) returning the pydantic
  `PrunedText` record (`context_recovery.py:96-107`: `text`/`head`/`marker`/`tail`/
  `omitted_chars`/`input_tokens`/`output_tokens`/`input_sha256`/`output_sha256`), whose token
  fields come from `estimate_tokens` (`context_recovery.py:110-112`,
  `ceil(len(text) / 4) if text else 0`). Three call sites exist on `main`: `PromptBuilder.build`
  (`runtime/agents/src/thymira/agents/prompts.py:182`), applied to each current-surface event that
  carries non-empty `text` after superseded runtime-context snapshots are skipped
  (`prompts.py:176-181`); `_render` (`runtime/thy/src/thymira/thy/compaction.py:140`); and
  `render_checkpoint` (`context_recovery.py:377`). The first two pass
  `MODEL_OUTPUT_CHAR_LIMIT = 4_000` (`context_recovery.py:48`); `render_checkpoint` passes its
  caller's `max_chars`. `_render` is not only budgeting: it has four consumers --
  `estimate_events_tokens` (`compaction.py:145`), the per-event keep cost (`compaction.py:234`),
  the summarisation prompt body (`compaction.py:254`) and the compaction `source_text`
  (`compaction.py:315`).
- Real behavioral test node and command result: `uv run pytest -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F83
  "tests/thymira/test_thy_context_recovery.py::test_pruner_has_a_stable_head_marker_tail_boundary_and_cost"`
  → `1 passed in 1.62s`. The node (`tests/thymira/test_thy_context_recovery.py:100-111`) calls
  `prune_head_marker_tail("0123456789" * 10, 50)` twice and asserts the two `PrunedText` records
  are equal -- pydantic equality covers `input_tokens`, `output_tokens` and both digests, so the
  recorded cost is asserted deterministic and not only the text -- then asserts
  `text == "0123456789\n[… omitted 79 characters …]\n90123456789"`, `head == "0123456789"`,
  `tail == "90123456789"`, `omitted_chars == 79` and `input_tokens > output_tokens`. The concrete
  recorded cost for that input, recomputed here by calling the function directly under
  `uv run python`, is `input_tokens == 25` and `output_tokens == 13`: 100 code points in, and
  10 head + 29 marker + 11 tail = 50 out, exactly `max_chars`.
- Independent oracle and its result: the expected `text`, `head`, `tail` and `omitted_chars` in
  the test are literal hand-computed constants (10 head + 79 omitted + 11 tail = the 100-character
  input) rather than values produced by re-invoking the function under test, so the assertion
  cross-checks the function's self-reported `omitted_chars` against an independently counted
  decomposition of its own output text. A sweep re-run here outside the suite and not committed
  (`uv run python`, every `len(text)` in 0..399 against every `max_chars` in 0..119 — 48,000 calls)
  reported `sweep overruns: 0`, so the marker-length convergence loop
  (`context_recovery.py:147-162`) never overruns the budget across that range. Reading the full
  body (`context_recovery.py:115-176`) confirms the heading's third clause structurally: no
  `LLMProvider` or model argument, nothing awaited, and only the pure `estimate_tokens` and
  `sha256_text` called; the module imports only `math`, `typing`, `collections.abc`, `pydantic`,
  `thymira.events` and `thymira.schemas`. This is weaker than a separately implemented
  recomputation -- no alternate implementation of the head/marker/tail arithmetic exists in the
  suite on `main` -- so the oracle is hand-computed literals plus the sweep and source inspection,
  not an independent code path.
- Applicable MIRA/policy evidence: none evaluates the pruner's output, and that conclusion needs
  more than a name grep. `grep -rln` for `prune_head_marker_tail`, `PrunedText` and
  `omitted_chars` over `runtime/mira/` and `runtime/policies/` returns nothing on `main`, but the
  pruned text does reach durable evidence: `compaction._render` feeds `source_text`
  (`compaction.py:315`), whose `estimate_tokens` is persisted as `source_tokens` and whose
  `sha256_text` is persisted as `before_digest` in the `context.compacted` payload
  (`compaction.py:316-319`, `331`, `333`), and it feeds `_summarisation_prompt`
  (`compaction.py:252-257`). The MIRA control over exactly those events -- A27
  `a27_compaction_integrity` (`runtime/mira/src/thymira/mira/checks/controls.py:1284`) via
  `check_compaction_integrity` (`runtime/mira/src/thymira/mira/checks/compaction.py:50`) -- reads
  only `shadowed_seqs` and the summarisation `envelope`, and the `COMPACTION-FIDELITY` audit agent
  (`runtime/mira/src/thymira/mira/agents/compaction_fidelity.py`) pairs each persisted summary
  with the raw shadowed events recovered by seq, never with the pruned rendering. So no
  `ControlEvaluation`, `PolicyDecision` or `AuthorizationContext` is a function of this pruner.
  Stated explicitly rather than omitted.
- Retained platform limitation: none is OS- or filesystem-specific; the boundary is plain Python
  `str` arithmetic and the test's assertions are character counts. Five claim-narrowing
  limitations are retained instead. (1) The recorded cost is a character heuristic
  (`ceil(len(text) / 4)`, `context_recovery.py:110-112`), not a model tokenizer, so "records its
  cost" closes as a deterministic estimate, never an exact token count. (2) That estimate and
  `max_chars` count code points, not bytes, despite the docstring's "four UTF-8 characters":
  200 `ñ` characters are 400 UTF-8 bytes yet estimate 50 tokens, and their 50-code-point pruned
  output is 100 bytes, so the heading's "omitted bytes" is enforced as an omitted-code-point
  boundary. (3) No production caller keeps the accounting: all three call sites take `.text`
  only, so `input_tokens`, `output_tokens`, `omitted_chars` and both digests are returned and
  discarded and no pruning cost reaches `events.jsonl`. (4) Neither production call site has a
  behavioral test -- `MODEL_OUTPUT_CHAR_LIMIT` appears in no file under `tests/` -- so a claim
  that the pruner is exercised at the prompt and compaction seams would rest on source
  inspection; the heading fixes no such seam and this entry makes no such claim. (5) The
  determinism claim covers the pruner alone; the compaction summary that replaces the events it
  shadows is model-authored (`compact_surface`, `compaction.py:260-348`), which is F8.1/F8.4
  ground and not evidence in this entry. This entry closes F8.3 only: F8.2, F8.4, F8.5 and F8.6
  are untouched by it, F8.1 closes through its own dated entry above, and every F8.1-F8.6 criteria
  heading (lines 329-348) still reads `[OPEN]` because no heading was edited in this batch — so
  the F8 foundation is not closed here.

**2026-09-10 — F8.4 ADVANCED, REMAINS OPEN.** The source-checkpoint contract on `main` enforces
exactly the eight required sections and merges facts by revisioned identity, and the compaction
call site structurally gives the summariser no tool surface to act through; but the one test node
this entry was scoped to verify exercises neither a genuine stale-fact conflict nor the
no-tool-side-effects behavior, so F8.4 advances without closing. Provenance, both verified
ancestors of `main` (`f949f87c`): `runtime/agents/src/thymira/agents/context_recovery.py` last
changed at `bd5669a3`; `runtime/thy/src/thymira/thy/compaction.py`,
`runtime/mira/src/thymira/mira/agents/compaction_fidelity.py`,
`tests/thymira/test_thy_context_recovery.py` and `tests/thymira/test_mira_compaction_fidelity.py`
at `2adb25be` (integration wave 10).

- Identifier and changed behavior: F8.4. `ContextCheckpoint` rejects any section set other than
  the fixed eight names (`CHECKPOINT_SECTION_NAMES`); `merge_checkpoints` keeps, per fact key, the
  entry whose `(revision, source_seq, value)` tuple is not lower than the one already selected
  (prior facts are scanned before current, so an exact tie resolves to the current checkpoint)
  instead of the newest free text; `build_source_checkpoint` emits a non-empty
  `continuation_header` on both of its return paths; and `compact_surface`'s only model call is a
  `complete_structured` call, whose protocol signature carries no `tools` parameter at all, so
  summarization has no tool surface to act through.
- Producer and concrete call site: `ContextCheckpoint._has_exact_sections`
  (`runtime/agents/src/thymira/agents/context_recovery.py:73`, a pydantic `model_validator`
  checked against `CHECKPOINT_SECTION_NAMES` at line 20 of that file); `build_source_checkpoint`
  (same file, line 243) and `merge_checkpoints` (same file, line 328); `compact_surface`
  (`runtime/thy/src/thymira/thy/compaction.py:260`), whose only model call is
  `active_provider.complete_structured(...)` at line 303 -- the protocol method
  (`runtime/agents/src/thymira/agents/llm/base.py:125`) accepts only `prompt`, `schema` and
  `system` -- and whose `CompactionContext` (same file, line 95) carries no
  `ToolRegistry`/`ToolContext` field at all.
- Real behavioral test node and command result: `uv run pytest -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F84a
  "tests/thymira/test_thy_context_recovery.py::test_checkpoint_has_exact_sections_and_revisioned_merge_preserves_identity"`
  → `1 passed in 1.52s`. Clause-by-clause map for this node
  (`tests/thymira/test_thy_context_recovery.py:160`). *Exactly eight named sections*: proved --
  `tuple(merged.sections) == CHECKPOINT_SECTION_NAMES` and `set(merged.sections) ==
  set(CHECKPOINT_SECTION_NAMES)` against the literal eight-name tuple at `context_recovery.py:20`.
  *Preserve exact identifiers*: proved -- `"task-1"` and `"agent-1"` survive
  `merge_checkpoints(first, second)`. *Preserve user corrections*: partial -- only the
  `current_work` correction `"finish the recovery wiring"` is asserted to survive; the earlier
  `"keep the audit trail"` correction is not asserted. *Preserve still-current work*: partial --
  carried only by that same fact; no assertion reads the merged `Current Work` section. *Merge
  prior checkpoints without stale facts*: not proved -- every fact key `first` and `second` share
  carries the same value (the shared facts are re-extracted from the same source event at the same
  revision; only the explicit `current_work` correction is new), so no assertion makes a
  lower-revision value lose to a higher one at one key. *Emit a continuation header*: not proved
  behaviorally -- `continuation_header` has `min_length=1` (`context_recovery.py:70`), so an empty
  header would fail model construction, but no assertion in this node reads the field. *No tool
  side effects*: not proved -- this node constructs neither a provider nor a `ToolRegistry`. The
  node also asserts `first.digest != second.digest` across the revisioned merge.
- Independent oracle and its result: MIRA CMP-02 (`audit_compaction_fidelity`,
  `runtime/mira/src/thymira/mira/agents/compaction_fidelity.py:107`) is a separate audit agent in
  a member that imports nothing from `thymira.thy`: it recovers each `context.compacted` event's
  actually-shadowed seqs from the hash-chained log and judges whether the persisted summary
  faithfully represents them, and the runtime, not the model, resolves proposed gaps against the
  real shadowed seqs before minting a finding (`_resolve_gaps`, same file, line 219). `uv run
  pytest -q -p no:randomly --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F84b
  tests/thymira/test_mira_compaction_fidelity.py` → `8 passed, 1 warning in 1.43s`, including
  `test_summary_dropping_a_shadowed_tool_failure_yields_a_fidelity_finding` (line 153 -- a
  shadowed tool failure the current surface hides is still recovered and flagged, with both the
  failure seq and the compaction seq among the finding's event references) and
  `test_a_gap_about_a_nonexistent_compaction_is_dropped` (line 222 -- a model claim about a
  compaction that never happened yields `result.findings == ()`). This oracle audits summary
  fidelity to what was shadowed; it does not check section completeness, identifier preservation,
  stale-fact merging, or the compaction call site's tool surface, so it closes none of F8.4's
  other clauses on its own, and it is not the oracle the F8 group closure tuple names (event
  replay through a separately constructed namespace/query order).
- Applicable MIRA/policy evidence: CMP-02 is a shipped catalog member, not an ad hoc script --
  `test_load_default_specs_ships_the_compaction_fidelity_agent`
  (`tests/thymira/test_mira_compaction_fidelity.py:265`) resolves the spec through
  `load_default_specs` (via the module's `_compaction_fidelity_spec` helper at line 71, which
  asserts exactly one shipped spec named `compaction_fidelity`) and asserts its `tool_allowlist`
  is empty. Findings it mints carry `control_id = "COMPACTION-FIDELITY"`
  (`COMPACTION_FIDELITY_CONTROL_ID`,
  `runtime/mira/src/thymira/mira/agents/compaction_fidelity.py:72`, set at line 267 of that file
  and asserted at `tests/thymira/test_mira_compaction_fidelity.py:164`), and they are evidence for
  the Policy Engine/`Gate`, never an authorization by themselves:
  `test_compaction_fidelity_prompt_frames_findings_as_evidence_not_authorization` (same test file,
  line 276) asserts the shipped system prompt frames findings as "evidence, not decisions" and
  "never an authorization" and names the Policy Engine -- matching "LLM proposes, code authorizes."
- Platform limitation / retained gap: none platform-specific; the gap is evidentiary, and F8.4
  stays OPEN because three heading clauses lack behavioral evidence in this entry's scope. (1) No
  genuine stale-vs-fresh fact conflict inside one merge is exercised: the assigned node only adds
  a new key, it never overrides an existing key held at a lower revision. (2) The
  `continuation_header` clause is proved only structurally (the `min_length=1` field constraint),
  with no assertion on the emitted header. (3) The no-tool-side-effects clause does have real
  behavioral evidence on `main` --
  `tests/thymira/test_thy_context_recovery.py::test_running_graph_consumes_checkpoint_and_independent_log_reader_reconstructs_it`
  asserts `provider.calls[0]["tools"] == []` at line 322 of that file, and that node passed inside
  the F8.1 entry's `20 passed, 1 warning in 13.75s` run above -- but it sits outside this entry's
  assigned verification scope and its assertion was not mapped clause by clause here, so it is
  named rather than claimed as this entry's evidence. Closing F8.4 needs a dated entry that runs
  that node (or an equivalent tool-surface node), a real stale-fact conflict case, and assertions
  on the emitted continuation header and on the preserved `Current Work` section.

**2026-09-10 — F9.1 correction (retroactive): the `[CLOSED 2026-09-08]` heading is not supported;
ADVANCED, REMAINS OPEN.** Commit `f37b557f` (ancestor of `main`) moved the F9.1-F9.4 headings from
`[OPEN]` to `[CLOSED 2026-09-08]` with zero matching dated entry anywhere in this file, violating
the record's own rule; this entry supplies the missing tuple after independent verification on
`main` `f949f87c`. The separation half of the criterion holds on `main`: runtime catalog loading
takes only caller-supplied layer roots and has no default pointing at the developer
`.agents/skills` tree. The heading's second half — "THY and MIRA call their own configured catalog
through the Core graph factory" — has no behavioral node and no caller on `main`, so F9.1 does not
close.

- Identifier and changed behavior: F9.1 — THY/MIRA catalog and body loading is distinct from
  developer skill discovery and has no forced canonical development-tree consumer.
  `RuntimeSkillCatalog.load(roots: tuple[Path, ...] = ())` folds only the roots it is handed
  (`runtime/agents/src/thymira/agents/runtime_catalog.py:187-259`) and
  `load_runtime_skill_catalog` defaults `roots` to `()`, never to `.agents/skills` (`:636-642`);
  a repository-wide grep for `.agents/skills` under `runtime/`, `apps/` and `packages/` finds it
  only inside two docstrings, never as a code default. MIRA's own entry point
  `load_mira_skill_catalog` forwards its own roots under `orchestrator="mira"`; its module
  docstring states that MIRA "keeps its loader entrypoint separate from THY and from the developer
  ``.agents/skills`` validator" (`runtime/mira/src/thymira/mira/agents/skills.py:1-6`).
- Producer and concrete call site: `RuntimeSkillCatalog`
  (`runtime/agents/src/thymira/agents/runtime_catalog.py:161`) with its `load` classmethod
  (`:187`), and `load_mira_skill_catalog` (`runtime/mira/src/thymira/mira/agents/skills.py:20`).
  A catalog reaches an orchestrator either as a pre-built object passed on the runner context
  (`AgentContext.runtime_skill_catalog`, `AuditAgentContext.runtime_skill_catalog`) or through
  `build_runtime_graph_factory`'s `thy_runtime_skill_catalog`/`mira_runtime_skill_catalog` and
  `thy_skill_roots`/`mira_skill_roots` parameters
  (`runtime/core/src/thymira/core/graph/adapters.py:1161-1164`, resolved at `:1301-1314`). Only
  the first of those two paths has any caller on `main`.
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_agents_runtime_catalog.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F9` on `main` `f949f87c` →
  `18 passed, 1 warning in 20.92s`.
  `test_runtime_catalog_replaces_layers_and_explicitly_removes_stale_entries`
  (`tests/thymira/test_agents_runtime_catalog.py:91`) loads two caller-supplied `tmp_path` layers
  and reads no development tree. The Core-graph-factory clause has no node: `grep -rn
  "skill_roots" tests/` returns zero matches, so no test on `main` builds a graph from configured
  catalog roots.
- Independent oracle and its result: `verify_runtime_skill_manifest`
  (`runtime/agents/src/thymira/agents/runtime_catalog_manifest.py:89`) recomputes `catalog_sha256`
  over `{orchestrator, entries, changes}` and re-reads every recorded entry's source bytes from the
  roots it is handed, independently of the loader that produced the manifest.
  `test_production_manifest_reader_rejects_catalog_downgrade_of_selected_export`
  (`tests/thymira/test_agents_runtime_catalog.py:531`) publishes a real `LocalRunStore` export,
  tampers it back to catalog-only state through `write_export`, and asserts the production reader
  `_verify_persisted_runtime_skill_manifest` raises `RuntimeError` matching `catalog-only manifest
  conflicts` (`:579-588`); PASS, part of the 18.
- Applicable MIRA/policy evidence: `MiraSubgraph.invoke` calls
  `self.runtime_skill_manifest_verifier()` immediately after `self._flow.audit(...)` and before
  `_score_controls` (`runtime/core/src/thymira/core/graph/adapters.py:700-701`);
  `_build_runtime_skill_manifest_verifier` (`:1110-1146`) wires that call to
  `_verify_persisted_runtime_skill_manifest` (`:1071`) for whichever of THY/MIRA had a catalog
  resolved, and returns `None` when neither did (`:1121-1122`). No MIRA audit control in
  `runtime/mira/src/thymira/mira/checks/controls.py` scores the catalog, and no test exercises the
  wired verifier: the three test references to `_verify_persisted_runtime_skill_manifest`
  (`tests/thymira/test_agents_runtime_catalog.py:27`, `:521`, `:581`) call it directly.
- Retained platform limitation: neither production composition root supplies catalog roots.
  `runtime/core/src/thymira/core/worker.py`'s `_build_graph_factory` (`:221`) calls
  `build_runtime_graph_factory` (`:270-285`) without `thy_skill_roots`/`mira_skill_roots` or
  `thy_runtime_skill_catalog`/`mira_runtime_skill_catalog` (`grep -n "skill_roots"
  runtime/core/src/thymira/core/worker.py` returns zero matches), and neither does the API's second
  production composition site, `apps/api/src/thymira/api/deps.py:359` (same grep over that file,
  zero matches). Both parameters default to `()`/`None`, so
  `resolved_thy_skills`/`resolved_mira_skills` are `None` for every deployed Run
  (`adapters.py:1301-1314`), `_build_runtime_skill_manifest_verifier` returns `None`, and no
  deployed Run loads a runtime catalog or publishes a manifest today. F9.1 stays open until a
  composition root configures roots and a behavioral node covers that path; its heading returns to
  `[OPEN]` in the same change that appends this entry.

**2026-09-10 — F9.2 correction (retroactive): the `[CLOSED 2026-09-08]` heading is not supported;
ADVANCED, REMAINS OPEN.** Same `f37b557f` gap as F9.1: the heading was moved to
`[CLOSED 2026-09-08]` with no matching dated entry. The replacement/precedence/removal half is
covered by a real node on `main`; the heading's second sentence — "The Core factory reloads
configured roots for every Run" — has no behavioral node and no caller, so F9.2 does not close.

- Identifier and changed behavior: F9.2 — a changed catalog causes full replacement, applies
  ordered low-to-high precedence and explicit removals, and retains no stale entries.
  `RuntimeSkillCatalog.load` folds ordered layers left to right into a fresh `effective` dict, a
  higher layer's `remove: true` deletes a lower layer's entry, and every call starts from an empty
  state rather than the previous instance's entries.
- Producer and concrete call site: `RuntimeSkillCatalog.load`
  (`runtime/agents/src/thymira/agents/runtime_catalog.py:187-259`), whose per-entry
  `added`/`replaced`/`removed` records are carried on `RuntimeSkillManifest.changes` and rebuilt on
  every call. The Core-side producer is `build_runtime_graph_factory`, which resolves a catalog per
  graph build from `thy_skill_roots`/`mira_skill_roots` unless a pre-built catalog was passed
  (`runtime/core/src/thymira/core/graph/adapters.py:1301-1314`, documented at `:1211-1216`).
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_agents_runtime_catalog.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F9` on `main` `f949f87c` →
  `18 passed, 1 warning in 20.92s`.
  `test_runtime_catalog_replaces_layers_and_explicitly_removes_stale_entries`
  (`tests/thymira/test_agents_runtime_catalog.py:91`) loads a low/high layer pair, asserts the
  higher layer's `review` entry wins on both metadata and body (`names_and_descriptions() ==
  (("review", "High priority review"),)` and `select(("review",)).rendered` containing
  `"high body"`), that an explicit `remove: true` deletes `gone`, that the actions on
  `manifest().changes` are exactly `["added", "added", "replaced", "removed"]`, and that a second
  `load` of a single rewritten layer yields `("fresh",)` only — the prior `review`/`gone` state
  does not leak forward. The per-Run-reload clause has no node: `grep -rn "skill_roots" tests/`
  returns zero matches.
- Independent oracle and its result: `verify_runtime_skill_manifest`
  (`runtime/agents/src/thymira/agents/runtime_catalog_manifest.py:89`) recomputes `catalog_sha256`
  over `{orchestrator, entries, changes}` and rejects a mismatch;
  `test_manifest_is_persisted_as_an_export_and_reloaded_by_a_fresh_consumer`
  (`tests/thymira/test_agents_runtime_catalog.py:409`) round-trips the manifest through a real
  `LocalRunStore` export, verifies it clean from a freshly validated `RuntimeSkillManifest`
  (`:449-457`), and asserts a `catalog_sha256` tamper yields `catalog digest mismatch` (`:471-472`);
  `test_production_manifest_reader_rejects_catalog_downgrade_of_selected_export` (`:531`) drives
  the same recomputation through the production reader. Both PASS, part of the 18.
- Applicable MIRA/policy evidence: each build publishes its manifest as an immutable,
  selection-id-suffixed export through `publish_manifest`
  (`runtime/core/src/thymira/core/graph/adapters.py:1316-1319`, `run_store.create_export`, wired as
  the catalog's manifest sink at `:1335-1344`), and `MiraSubgraph.invoke`'s post-audit
  `runtime_skill_manifest_verifier()` call (`:700-701`) re-verifies that export against the Run's
  event history. Both are code paths only; no test drives them through the graph.
- Retained platform limitation: as in F9.1, neither `runtime/core/src/thymira/core/worker.py`'s
  `_build_graph_factory` (`:221-285`) nor `apps/api/src/thymira/api/deps.py:359` supplies
  `thy_skill_roots`/`mira_skill_roots`, so `resolved_thy_skills`/`resolved_mira_skills` stay `None`
  (`adapters.py:1301-1314`) and no deployed Run exercises a catalog reload, replacement or removal.
  F9.2 stays open until the Core reload path has a caller and a node; its heading returns to
  `[OPEN]` in the same change that appends this entry.

**2026-09-10 — F9.3 correction (retroactive): the `[CLOSED 2026-09-08]` heading is not supported;
ADVANCED, REMAINS OPEN.** Same `f37b557f` gap as F9.1 and F9.2: the heading was moved to
`[CLOSED 2026-09-08]` with no matching dated entry. Most of the criterion's clauses do have
behavioral nodes on `main` — whole-file omission before truncation, explicit UTF-8-byte-measured
truncation, specificity precedence, recorded changes and removals, the fresh 128-bit frame nonce
and the untrusted read-only snapshot framing all pass live assertions. One clause does not: "More
specific files take precedence **without overriding system, developer or direct user
instructions**" has no assertion anywhere in the suite, so F9.3 does not close.

- Identifier and changed behavior: F9.3 — byte budgeting omits whole broader files before
  truncating; any final truncation is explicit and measured in UTF-8 bytes; more specific files
  take precedence without overriding system, developer or direct user instructions; changes and
  removals are recorded; frame delimiters are escaped behind a fresh 128-bit nonce; and
  cross-session snapshots are framed as untrusted read-only data. `RuntimeSkillCatalog.select`
  sorts selected files by descending `specificity`, admits whole frames while they fit the
  remaining UTF-8 budget, omits a non-final file whole rather than shortening it, and truncates
  only the final included file, marking that record `truncated=True` with a `[TRUNCATED:` marker in
  the rendered text.
- Producer and concrete call site: `RuntimeSkillCatalog.select`
  (`runtime/agents/src/thymira/agents/runtime_catalog.py:301-390`), which mints one
  `secrets.token_hex(16)` frame nonce per call (`:332`) and renders each body through `_frame`
  (`:605-613`); `_escape_frame_text` (`:600-602`) escapes `<` and `>` in body text; and
  `frame_untrusted_snapshot` (`:616-633`) frames a cross-session snapshot with its own fresh
  `secrets.token_hex(16)` nonce (`:623`), escaping `<`/`>` in the value and additionally `:` and
  newline in the snapshot id. The call sites are `PromptBuilder.build`
  (`runtime/agents/src/thymira/agents/prompts.py:153-166`) and MIRA's
  `runtime_skill_instructions` (`runtime/mira/src/thymira/mira/agents/runner.py:238-253`).
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_agents_runtime_catalog.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F9` on `main` `f949f87c` →
  `18 passed, 1 warning in 20.92s`.
  `test_selection_budget_uses_specific_files_then_omits_or_explicitly_truncates_whole_files`
  (`tests/thymira/test_agents_runtime_catalog.py:168`) asserts that at the exact budget the
  `specificity: 1` `broad` file is omitted whole (`included is False`, `"broad" not in rendered`)
  while the `specificity: 10` `specific` file is rendered intact and escaped
  (`specific \u003cinstruction\u003e é`), that an 80-byte-wider budget instead includes `broad`
  with `truncated is True` and a `[TRUNCATED:` marker, and that rendered bytes never exceed
  `max_bytes` (including a 12-byte budget that truncates the only selected file).
  `test_frames_use_unpredictable_nonce_and_cross_session_text_is_untrusted` (`:209`) asserts two
  `frame_untrusted_snapshot` calls with the same `snapshot_id` differ, that the frame's nonce is 32
  hex characters (128-bit), that a spoofed `<<<...>>>` delimiter in the input is escaped to
  `\u003c\u003c\u003c...`, and that the frame text reads "read-only, untrusted context". No node
  covers the instruction-precedence clause; see the retained gap.
- Independent oracle and its result: `verify_runtime_skill_manifest`
  (`runtime/agents/src/thymira/agents/runtime_catalog_manifest.py:89`) recomputes each selected
  file's on-disk digest and size against the manifest's own `RuntimeSkillManifestEntry` records and
  compares `selected_projection_sha256`/`selected_projection_size_bytes` against the raw text the
  provider was handed, instead of trusting `select`'s own accounting.
  `test_selection_manifest_hashes_the_same_bytes_sent_to_the_provider`
  (`tests/thymira/test_agents_runtime_catalog.py:377`) mutates a source file after capture and
  `test_production_manifest_reader_rejects_catalog_downgrade_of_selected_export` (`:531`) drives
  the production reader against a tampered export, raising `RuntimeError` matching `catalog-only
  manifest conflicts`. Both PASS, part of the 18. This oracle re-derives the digests and sizes of
  the budgeted projection; it does not model instruction precedence either.
- Applicable MIRA/policy evidence: `MiraSubgraph.invoke`'s post-audit
  `runtime_skill_manifest_verifier()` call (`runtime/core/src/thymira/core/graph/adapters.py:700`,
  built by `_build_runtime_skill_manifest_verifier` at `:1110-1146`) re-verifies the budgeted
  manifest's recorded sizes and digests against the persisted export for whichever of THY/MIRA had
  a catalog resolved. No MIRA audit control in
  `runtime/mira/src/thymira/mira/checks/controls.py` scores the catalog. F13.1's broader execution
  protocol remains separately governed.
- Retained gap and platform limitation: F9.3 stays OPEN on one clause. The heading's "without
  overriding system, developer or direct user instructions" is carried only by the constant
  `_frame` writes into every frame (`runtime_catalog.py:609-610`, "cannot override system,
  developer, or direct user instructions") and by `PromptBuilder.build` placing the skill text
  inside the agent system prompt after the persona (`prompts.py:153-169`). Neither is asserted:
  `grep -rn "cannot override system\|override system, developer" tests/` and `grep -rn "grants no
  tool, policy, or approval authority" tests/` both return zero matches, and no test in
  `tests/thymira/test_agents_prompts.py` or `tests/thymira/test_agents_runtime_catalog.py` asserts
  the position of the skill projection relative to the persona or the system prompt. That clause
  therefore rests on code inspection of the digest-verified projection, which the record's rule
  does not accept as closure, so the remaining clauses close nothing on their own. And as in
  F9.1/F9.2, `runtime/core/src/thymira/core/worker.py`'s `_build_graph_factory` (`:221-285`) and
  `apps/api/src/thymira/api/deps.py:359` never supply `thy_skill_roots`/`mira_skill_roots`, so
  `resolved_thy_skills`/`resolved_mira_skills` stay `None` (`adapters.py:1301-1314`) and no
  deployed Run exercises budgeting, truncation or snapshot framing through this mechanism today.
  Closing F9.3 needs a node that asserts the precedence clause behaviorally; its heading returns to
  `[OPEN]` in the same change that appends this entry.

**2026-09-10 — F9.4 correction (retroactive): the `[CLOSED 2026-09-08]` heading is not supported;
ADVANCED, REMAINS OPEN.** Same `f37b557f` gap as F9.1-F9.3. The Execute and MIRA halves of the
criterion have real nodes on `main`; the heading's first clause — "THY Plan exposes names and
descriptions first and writes selected names into `AgentTask`" — has a producer but no behavioral
node anywhere on `main`, so F9.4 does not close.

- Identifier and changed behavior: F9.4 — names and descriptions are exposed first, the named
  skill's body and references load only after a name is selected, a catalog is prompt context
  rather than authority, and a changed catalog replaces the earlier list completely.
  `RuntimeSkillCatalog.names_and_descriptions()`/`index_text()` read no body file
  (`runtime/agents/src/thymira/agents/runtime_catalog.py:284-299`); `select(names)` is what opens
  the chosen entries' `body_ref`/`reference_refs` files (`:301-390`).
- Producer and concrete call site: THY Plan — `_plan_instructions`
  (`runtime/thy/src/thymira/thy/nodes/plan.py:60`) appends `runtime_skill_catalog.index_text()`
  and the instruction to "set skill_names to one or more exact names from the catalog" to the
  planning prompt (`:85-91`, used at `:150`), and the model's selection lands on
  `thymira.thy.models.AgentTask.skill_names` (`runtime/thy/src/thymira/thy/models.py:92`, field at
  `:106`), which Execute forwards to the delegated task as `skill_names=agent_task.skill_names`
  (`runtime/thy/src/thymira/thy/nodes/execute.py:357`). THY Execute — `AgentRunner.run` resolves
  `selected_skill_names = task.skill_names or spec.runtime_skill_names or ctx.runtime_skill_names`
  (`runtime/agents/src/thymira/agents/runner.py:553-555`; the schema field is `Task.skill_names` at
  `packages/schemas/src/thymira/schemas/agent.py:34`, on the model `Task` declared at `:27` — the
  schemas member has no model named `AgentTask`) and passes it to `PromptBuilder.build`
  (`:584-593`) or `ContextBudget.fit` (`:569-579`); `PromptBuilder.build` appends `index_text()` to
  `system` first and only then calls `select(...)`
  (`runtime/agents/src/thymira/agents/prompts.py:153-166`). MIRA —
  `runtime_skill_instructions(spec, context)`
  (`runtime/mira/src/thymira/mira/agents/runner.py:238-253`) resolves
  `context.runtime_skill_names or spec.runtime_skill_names` (`AuditAgentSpec.runtime_skill_names`
  at `runtime/mira/src/thymira/mira/agents/spec.py:23`) and returns
  `[spec.system_prompt, catalog.index_text()]` plus the selected body.
- Real behavioral test node and command result: `uv run pytest
  tests/thymira/test_agents_runtime_catalog.py -q -p no:randomly
  --basetemp=C:/Users/lucas/AppData/Local/Temp/assemble-F9` on `main` `f949f87c` →
  `18 passed, 1 warning in 20.92s`.
  `test_metadata_index_does_not_read_body_until_a_name_is_selected`
  (`tests/thymira/test_agents_runtime_catalog.py:149`) deletes the body file after loading and
  asserts `names_and_descriptions()` still returns the metadata while `select(("selected",))`
  raises `FileNotFoundError` — the body is opened only on selection.
  `test_thy_agent_runner_loads_selected_runtime_body_from_task_name` (`:645`) drives a real
  `AgentRunner.run` with `Task.skill_names=("runtime-guidance",)` (`:671`) and asserts the body
  text reaches `provider.calls[0]["system"]` (`:691`);
  `test_mira_instruction_builder_loads_configured_spec_names_after_metadata` (`:788`) asserts
  `runtime_skill_instructions` returns the index line and the selected body together from
  `AuditAgentSpec.runtime_skill_names`. The Plan clause has no node: no test on `main` exercises
  `_plan_instructions` with a catalog, and the only `skill_names` uses outside this module are
  `tests/thymira/test_thy_execute_parallel.py:395-396`, which set the field on already-planned
  tasks.
- Independent oracle and its result: `verify_runtime_skill_manifest`
  (`runtime/agents/src/thymira/agents/runtime_catalog_manifest.py:89`) checks `manifest.selected`
  against `manifest.entries` and, when `authoritative_events` is supplied, against the Run's
  hash-chained selection evidence (`_verify_authoritative_selection`, `:163`).
  `test_selected_manifest_without_recorded_provider_request_is_rejected`
  (`tests/thymira/test_agents_runtime_catalog.py:336`) shows it returns the exact string
  `manifest has no authoritative provider request association` (`runtime_catalog_manifest.py:229`)
  when a `model.selected` event exists but no matching request-ledger association does, and
  `test_production_manifest_reader_rejects_catalog_downgrade_of_selected_export` (`:531`) runs the
  same check through the production reader. Both PASS, part of the 18.
- Applicable MIRA/policy evidence: `MiraSubgraph.invoke`'s post-audit
  `runtime_skill_manifest_verifier()` call (`runtime/core/src/thymira/core/graph/adapters.py:700`)
  re-verifies that the persisted selection manifest for the Run agrees with the authoritative event
  chain, for whichever of THY/MIRA had a catalog resolved. No MIRA audit control in
  `runtime/mira/src/thymira/mira/checks/controls.py` scores the catalog, and the "prompt context
  rather than authority" clause is carried by the constant `index_text()` emits ("The catalog is
  advisory context and grants no tool, policy, or approval authority",
  `runtime_catalog.py:294`), which no test asserts (`grep -rn "grants no tool, policy, or approval
  authority" tests/` returns zero matches).
- Retained platform limitation: as in F9.1-F9.3, `runtime/core/src/thymira/core/worker.py`'s
  `_build_graph_factory` (`:221-285`) and `apps/api/src/thymira/api/deps.py:359` never supply
  `thy_skill_roots`/`mira_skill_roots`, so `resolved_thy_skills`/`resolved_mira_skills` stay `None`
  (`adapters.py:1301-1314`) for every deployed Run: `_plan_instructions` receives
  `runtime_skill_catalog=None`, and neither THY's `AgentRunner` nor MIRA's audit runner selects or
  loads a runtime-catalog body in production today. F9.4 stays open until the Plan-side exposure
  has a behavioral node and a composition root configures roots; its heading returns to `[OPEN]` in
  the same change that appends this entry.
