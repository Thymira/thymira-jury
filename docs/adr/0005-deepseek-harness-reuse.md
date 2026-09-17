# ADR-0005 — What Thymira reuses from DeepSeek Harness (ideas only) and what it does not

- **Status:** Accepted — 2026-08-23
- **Deciders:** the owner
- **Extends:** ADR-0001 (harness terminology; `deepseek-harness` rejected as a *base*)
- **Leads to:** ADR-0006 (the log/surface distinction, the first adoption to be built)
- **Related:** ADR-0004 (model routing), `docs/roadmap/product-final.md`,
  `docs/contracts/contract-v0.1.md`

## Context

ADR-0001 rejected `deepseek-ai/deepseek-harness` ("dsh") as a *base* for Thymira on language and
maturity grounds, as one line in a framework survey. On 2026-08-23 the owner asked the narrower
question that ADR-0001 did not answer: not "should we build on it", but **"is anything in it worth
reusing"**.

That question was answered twice. The first pass filtered every subsystem through the three-week
MVP and dismissed most of them as post-MVP. The owner rejected that framing: *"don't limit
yourself to the MVP — if we are missing important things like context management, compaction etc.,
and we can take advantage of DeepSeek Harness's ideas or utilities, we should."* The filter was
removed and the analysis re-run on a full-architecture horizon. **The reversal mattered**: the
highest-value material in the repository sits in the subsystems the MVP filter had discarded.

Evidence comes from a direct read of the repository's subsystem documentation and its GitHub,
PyPI and npm metadata, and from a fan-out research pass (104 agents, 22 sources, 105 claims
extracted, 25 adversarially verified — 11 confirmed, 14 refuted). Where the two disagreed, the
direct read wins; where the research pass refuted an intuitive claim, the claim is *absent* rather
than softened.

### What dsh is, as of 2026-08-23

| Fact | Value |
|---|---|
| Created / last pushed | 2026-08-13 / 2026-08-21 — **ten days old** |
| Releases | `dsh-v0.1.1-rc.2`, `-rc.1`, `dsh-v0.1.0-rc.8`, `-rc.7` — **never a non-RC build** |
| Stars / forks | ~185,000 / ~20,450, accumulated in ~8 days — a launch spike, not adoption |
| Issue tracker | **Disabled**; GitHub Discussions only |
| Stated stability | *"developer preview … THERE WILL BE COMPATIBILITY-BREAKING CHANGES"* |
| Language / license | TypeScript + Node on the Cordis plugin framework; MIT |

It is a competent agent harness, and in context management it is well ahead of us. Its tool
pipeline, sandbox seam and approval seam are real, documented and published as packages
(`@deepseek-ai/dsh-sandbox`, `@deepseek-ai/dsh-user-approval`). Several intuitive criticisms — that
it ships no isolation mechanism, or no allow/deny/ask semantics — were tested against its
documentation and **refuted**; they are not repeated here.

### Why "adopt code" is not available to us

dsh has no Python implementation. `python/` is a *carrier*: `deepseek-harness-sdk` is a ~13 KB
`py3-none-any` wheel that exact-pins `deepseek-harness-runtime-bin==0.1.1rc1`, a platform wheel
holding a single-file compiled Node executable, and drives it as a **subprocess over
newline-delimited JSON-RPC on stdio**. Consequences:

- The runtime ships for `macosx_14_0_arm64` (55.2 MB), `manylinux_2_28_aarch64` (59.8 MB) and
  `manylinux_2_28_x86_64` (60.4 MB). There is **no `win_amd64` wheel**, so
  `pip install deepseek-harness-sdk` cannot resolve on the team's Windows development machines.
- The sidecar owns the session, the agent loop, persistence and bash. Adopting it would make
  **Thymira the client and dsh the runtime** — the exact inversion of the invariant in `AGENTS.md`
  that clients own no state and the runtime owns all state, and it would put the event log in dsh's
  unchained JSONL, outside MIRA's reach.
- It expects `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`, against "LiteLLM is the only model gateway".
  dsh delegates provider and model selection to the Cordis composition — a custom composition mounts
  an `llm-pi-ai` provider route and selects any model in that adapter's catalog — so routing is a
  TypeScript plugin-configuration decision, not a recorded runtime choice. ADR-0004 has nothing to
  gain here either.

### What dsh does not have

Its session log guarantees *context reconstruction and replay*, not tamper-evidence: **no hash
chaining, no signing, no keyed MAC, no third-party verification**. Integrity is structural
(append-only, contiguous `seq`, version gating, atomic writes) plus non-cryptographic Zstandard
frame checksums. Its redaction seam is the **inverse** of ours — it ships zero rules by default and
rewrites only the *exported* telemetry copy, because "the canonical session log is never rewritten".
Ours redacts *before* the event enters the chain.

The gap is visible from outside the project: a third-party npm package,
`qiushi-dsh-evidence-audit`, exists for no purpose other than adding "observe-only hash-chained
evidence receipts for DeepSeek Harness".

**This settles the question ADR-0001 left implicit: a general-purpose agent harness does not supply
Thymira's differentiator.** The deterministic Policy Engine and the hash-chained, redacted,
independently verifiable evidence chain are ours to build, and remain the reason the project exists.

But the converse also holds, and the first pass missed it: **we are ahead on integrity and behind
on structure.** dsh separates the log from the surface, marks events as model-visible or log-only,
derives state by folding the log, and treats compaction as a recorded operation. Thymira has none
of those, and they are orthogonal to the hash chain rather than in tension with it.

## Decision

**1. Thymira takes no dependency on DeepSeek Harness in any form** — not the npm packages, not the
PyPI SDK, not a vendored binary, not a JSON-RPC sidecar. ADR-0001's rejection stands and is now
recorded with evidence rather than as a survey line.

**2. Thymira adopts the following ideas**, re-implemented in Python, with the source recorded here.
Ranked by value to an *auditable* runtime, not by cleverness.

| # | Idea adopted | Where it lands | Source in dsh |
|---|---|---|---|
| 1 | **The log is not the surface.** The chain stays complete; what a model sees is a projection of it, and compaction shadows rather than deletes | `packages/schemas`, `packages/events` — **built; see ADR-0006** | `SessionEventSurface` (`current`/`shadowed`/`log-only`), `surfaceOp: {op:'replace'}` |
| 2 | **Compaction is a bracketed, replayable record**: the summarisation envelope is logged so an auditor can reconstruct which model hid what | `EventType.CONTEXT_COMPACTED` payload; a MIRA control | `compaction/start` · `compaction/summary` · `compaction/end`, "reconstructable from log + code" |
| 3 | **Oversized tool results become referenced artifacts**, not inlined payloads | a `runtime/tools` post-execute policy writing through `LocalArtifactStore` | `SpillStore.saveText` → locator + `retrievalHint` + byte count |
| 4 | **A measurement declares its own confidence** — a number says whether it was measured or estimated | the usage ledger ADR-0004 wants; generalised as a house rule | `TokenMeasurement.baseline` (`usage` \| `estimated`); `SandboxEnforcement` (`full` \| `partial`) |
| 5 | **An interception point before every tool call, plus a deny that later listeners cannot undo** | `thymira.tools` Tool Manager — **already satisfied**: `ToolManager.execute` gates every call through `Gate.check_capability` and `thymira.policies.allows_execution` is fail-closed (a `REQUIRE_HUMAN_REVIEW` tool call is denied synchronously — C-9) | `tools/pre-execute` → guards → `tools/execute` → `tools/post-execute`; `ctx.tools.guard()` |
| 6 | **Enforcement as a reported fact**: record whether confinement was actually complete, not only what was requested | a sandbox-enforcement field on `ToolCall`; a MIRA control | `SandboxEnforcement`; `SandboxMode` (`read-only` / `workspace-write` / `danger-full-access`) |
| 7 | **Approval events are log-only and never enter the model transcript**; the outcome enum is closed and fail-closed, and a grant covers only the action asked about | the `Gate`'s `policy.decision` → `human.approval_requested` → `human.approval` triad | `approval/asked` / `approval/decided`; `ApprovalOutcome` (`allowed-once` / `rejected` / `cancelled` / `unavailable`) |
| 8 | **Run-scoped state is a pure fold of the log**, with no live mirror, so resume and fork recover it for free | every run-scoped mode or flag | `foldPlanMode(events)` — "the state in force is always a pure fold of the session log" |
| 9 | **Bracket ordering makes a crash detectable** rather than falsely successful: open first, close last | the phase machine and the `Gate` | "releasing the lock last turns a crash mid-operation into a detectable orphaned lock" |
| 10 | **A plan is guidance, never an authorization** — enforcement is a separate concern that does not read plan state | confirmation of *LLM proposes, code authorizes* | plan mode is "soft guidance"; sandbox and approval "enforce restrictions independently" |
| 11 | **A crisp step/turn boundary**: "a step is one model request plus the tools it calls; a turn is zero or more steps" | sizing the `Event` enum and the `Task` boundary in `ThyGraph` | its documented turn/step event chain |
| 12 | **Runtime invariants are owned and attributed per package**, asserting over authoritative event streams — never over service presence | reframes MIRA's controls; each member may own its checks | `ctx.invariants`, package-attributed failure |

Ideas 5 and 10 are **confirmations, not discoveries**. Claude Code's `PreToolUse`, the OpenAI
Agents SDK's guardrails and LangGraph's middleware all supply the interception seam, and several
sit inside the stack ADR-0001 already chose. We adopt the seam *placement*; we do not adopt dsh's
authority model, in which listener code decides. In Thymira the deterministic Policy Engine
decides, outside the agent loop.

Idea 11 is adopted with one exclusion: dsh retains raw assistant stream chunks for replay fidelity.
Thymira never persists chain-of-thought, so that part is not copied.

**3. Where Thymira improves on the source rather than copying it.** dsh's spill locator is opaque
and undigested — "consumers treat it as opaque … do not parse it". Thymira already declares in
`ToolCall` that "results are referenced by digest, not inlined" and already has `result_sha256` and
a sha256 artifact manifest. Spilled content therefore routes into `LocalArtifactStore` and gains a
digest, which the source does not provide. Its filesystem hardening is worth copying verbatim: a
private `0700` root and an exclusive owner-only create "so a planted symlink cannot redirect it",
and a failed spill keeps the inline result rather than turning a successful call into an error.

Two patterns were examined and are **already ours**: `AuditFinding` pairs `control_id` with prose,
and `PolicyDecision` pairs `rule_id` with `reason` and pins `policy_sha256` for replay. dsh's
machine-code-plus-human-text convention needs no adoption.

**4. Thymira explicitly does not adopt the following**, and no further evaluation is owed:

| Gap | Verdict | Reason |
|---|---|---|
| MCP surface | **Ignore** | `packages/mcp` contains only `mcp-client`. dsh *consumes* MCP servers; our gap is *exposing* the Tool Manager as one. Opposite direction; MCP is a published spec we implement directly. |
| Model routing | **Ignore** | Routing is a Cordis plugin-configuration decision, not a recorded runtime choice. ADR-0004 is strictly more auditable. |
| Event log *integrity* | **Ignore** | We are ahead: hash chain, redact-before-write, stateless verification. Only its *structure* is adopted, as ideas 1, 2 and 8. |
| Agent memory / recall | **Ignore** | `session-query` is full-text search over past sessions, not semantic memory. Our Knowledge Base (normative sources over pgvector) is our own problem. |
| Web UI | **Ignore for now** | Preview-stage and Cordis-bound. The transferable shape — one append-only stream, every surface a projection of it — we now hold through idea 1. |
| IDE adapters | **Ignore for now** | Agent Client Protocol is the piece to re-examine when `adapters/generic` starts. |
| The Cordis plugin architecture | **Ignore** | A language-bound DI container. Adopting any dsh unit as code means adopting it wholesale. |

**5. `native/landlock-run` is a design reference, not a dependency.** It is the one artifact in the
repository consumable from Python without TypeScript in the loop: ~300 lines of C11 over the raw
kernel UAPI, statically linked against musl, which installs a Landlock ruleset **on itself** and
then `exec`s the wrapped command, so the ruleset is inherited across `execve` and confines the child
and every descendant while the invoker stays unrestricted. It is **fail-closed** — if the kernel
cannot enforce, it exits without running the command — and reports `full`, `partial` or `unusable`.

We do not take it: it is distributed only as npm platform packages (`linux-x64`, `linux-arm64`), it
confines the **filesystem only** (no network, process or resource limits, which `run_python` needs),
it requires Linux 5.13+, and it would place a preview-stage third-party binary inside an audited
runtime. Docker plus a seccomp/gVisor profile covers the same ground on a supply chain we already
own. Its `docs/cli-contract.md` is worth one hour as a model of a fail-closed exec-wrapper contract
(argv grammar, reserved failure exit code 125, report lines) before we design our own.

**6. Attribution.** dsh is MIT, and its runtime wheel carries a ~16 KB `THIRD_PARTY_NOTICES.md`.
Copying source would be permissible but would carry attribution plus that transitive notice surface,
and would drag in the Cordis framework. Adopting only ideas and wire-format shapes carries **no
notice obligation and no dependency**. This ADR is the provenance record; for a project whose thesis
is traceability, recording where a design came from is the consistent act even where no licence
compels it.

**7. Sequencing**, by dependency rather than by effort:

- **Now, before the contract freezes.** Idea 1 — it changes `Event`, and `packages/schemas` is
  frozen on MVP day 1. Adding a defaulted field now costs nothing; adding it later breaks every
  consumer. **Done: ADR-0006.**
- **Next.** Idea 5 is already satisfied by the Tool Manager. Ideas 3 and 6 attach to it directly —
  `ToolContext` already carries the artifact store a spill policy needs, and `ToolResult` is where
  the enforcement field belongs. Ideas 2 and 4 wait for an agent loop, so they follow `runtime/thy`,
  which is still a stub.
- **Later.** Ideas 8, 9, 10, 11, 12 — conventions and refinements that cost little whenever they
  are applied and block nothing.

**8. Revisit trigger.** The *no-dependency* decision is re-examined only if dsh ships a non-RC
release *and* opens an issue tracker *and* a gap is still open at that time. A new star count is not
a trigger.

## Alternatives considered

| Option | Why not |
|---|---|
| Adopt dsh as the base runtime | ADR-0001's decision; reinforced by ten days of age, RC-only releases, no issue tracker and an all-caps breaking-change warning. |
| Consume dsh through the Python SDK sidecar | Inverts state ownership, moves the event log outside MIRA, bypasses LiteLLM, and does not install on Windows. |
| Vendor the `landlock-run` binary into our container image | Filesystem-only confinement, npm-only distribution, preview-stage binary inside an audited runtime; Docker + seccomp/gVisor already covers it. |
| Port dsh's tool pipeline or compaction engine to Python as code | Both are Cordis-shaped; porting them means porting a DI container. The seams are worth about a page of Python each of our own. |
| Keep the MVP filter and defer the whole context-management cluster | The filter hid the most valuable material in the repository, and idea 1 changes a contract that is about to freeze. Deferring it would have made it a breaking change. |
| Ignore the repository entirely | Would have missed ideas 1, 4 and 6 — none of which we had specified — and the evidence-chain weakness noted below. |

## Consequences

- **One contract change lands now** (ADR-0006); everything else is recorded as a decision and built
  when its host member exists. No new dependency, no lockfile entry, no schedule impact.
- **Context management stops being an unspecified hole.** Compaction, spill and token budgeting now
  have a defined place to attach and a rule they must obey: the chain never loses anything.
- **`runtime/tools` is designed before it is written.** The Tool Manager's seam placement, the
  non-overridable deny, the spill policy and the enforcement-as-fact field are settled before P3
  starts.
- **MIRA gains new controls to write**: ~~an unmatched compaction bracket~~ (withdrawn by
  [ADR-0007](0007-compaction-is-atomic-no-bracket.md): a compaction is a single append and has no
  unterminated state), a compaction that shadows forward, a run whose code executed
  under partial confinement, and a summary that does not faithfully represent what it replaced.
- **We inherit no attribution obligation**, and the provenance of twelve design choices is recorded.
- **Follow-up, discovered by this investigation and outside its scope:** the third-party evidence
  plugin above documents its own weakness as an *unkeyed* chain with *no external anchor*, so a
  valid suffix can be deleted and the remainder still verifies. `thymira.events` has the same shape
  today, and `verify_log` cannot currently answer "was this run silently truncated?" — a question an
  EU AI Act or credit-risk auditor will ask. This is tracked separately; it does not belong to this
  decision.

## Sources

- Repository, subsystem documentation and metadata, read 2026-08-23:
  <https://github.com/deepseek-ai/deepseek-harness> — `README.md`, `docs/subsystems/` (`approval`,
  `permission-presets`, `sandbox`, `persistence`, `session-telemetry`, `tools`, `core`,
  `code-runtime`, `compaction`, `spill`, `token-meter`, `session-query`, `plan`, `goal`,
  `invariants`), `packages/` (`core`, `mcp`, `sandbox`, `shell`, `guard`, `hooks`, `llm`, `acp`,
  `skill`, `subagent`, `e2b`) READMEs, `python/README.md`, `python/sdk/README.md`,
  `native/README.md`, `native/landlock-run/README.md`; GitHub REST API for releases, tags and
  repository metadata.
- <https://pypi.org/project/deepseek-harness-sdk/> and
  <https://pypi.org/project/deepseek-harness-runtime-bin/> — versions, `requires-python`, wheel
  platform tags and sizes.
- npm registry — `qiushi-dsh-evidence-audit`, `@deepseek-ai/dsh-sandbox`,
  `@deepseek-ai/dsh-user-approval`.
- Fan-out research pass, 2026-08-23 (104 agents, 22 sources, 105 claims extracted, 25 verified,
  11 confirmed, 14 refuted).
- ADR-0001, ADR-0004, ADR-0006, `AGENTS.md`, `docs/roadmap/mvp-3-weeks.md`.
