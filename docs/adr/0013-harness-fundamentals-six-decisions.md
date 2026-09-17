# ADR-0013 — Harness fundamentals: six decisions after reading DeepSeek Harness end to end

- **Status:** Accepted — 2026-09-04
- **Deciders:** the owner
- **Supersedes:** ADR-0005's "redact before the event enters the chain" stance and its "the
  contract freezes on MVP day 1" sequencing (decision 1 and decision 6 below). Everything else in
  ADR-0005 stands, including "no dependency on dsh in any form".
- **Extends:** ADR-0004 (hub-and-spoke topology), ADR-0006 / ADR-0007 (log vs surface; atomic
  compaction), ADR-0010 (local JSONL state)
- **Related:** the bug-hunt report of 2026-09-02 (196 root causes, 12 critical) and the reading
  report "DeepSeek Harness: qué copiar" of 2026-09-03 (27 lots, ~1,400 files, 13 fundamentals)
- **Implementation acceptance:** [F1–F13 acceptance record](../superpowers/plans/2026-09-07-dsh-acceptance.md).
  Accepted decisions and completed MVP tasks do not establish completion of this program.
  Every accepted COPY/ADAPT criterion needs its own behavioral evidence before its foundation
  can be declared complete. ADR-0014 records the owner's 2026-09-08 ruling that reconciles
  decision 1 with repository guidance: canonical events preserve model-visible values, while
  source credentials and exported/traced projections have their own boundaries.

## Context

ADR-0005 (2026-08-23) reused twelve ideas from `deepseek-ai/deepseek-harness` (dsh) after reading
its subsystem documentation. Two things happened since. A bug hunt over commit `36b5f7f` showed
that the demo Run fails or blocks on every real execution and that the ad hoc layer, not the
frameworks, is the cause: a failed sub-agent aborted the Run before MIRA ran, causes were
swallowed, no dataset was ever registered, the human approval never gated a tool call
(`interrupt()` unused), the policy passed every tool because every tool declared
`external_effects=()`, `run_python` ran unconfined, MIRA saw ~6 % of the log, the CARD redaction
regex destroyed decimal metrics before they reached the log, and ~22 % of the runtime had no
production caller. Then the whole dsh repository (commit `76fda72`, 2026-09-03) was read: docs,
the 184 architecture notes, 155 process/testing/simplification notes, 27 proposed, 10 rejected,
128 feature and 65 bug-fix notes, the 11 repository skills, every package except the browser
client, and the 27 real system prompts and 61 tool schemas in its recorded sessions.

The reading confirmed the owner's thesis (frameworks are fine, the ad hoc layer is not) and
surfaced eight points where dsh's design and Thymira's invariants disagree. The owner and the
assistant went through them one by one; where dsh is right about harness engineering it is
copied, and where it is right only for its own product (a developer-facing coding harness with
third-party plugins) Thymira keeps its differentiator: an independent auditor, decisions by code,
verifiable evidence. The owner confirmed all six recommendations below ("sigo todas tus
recomendaciones", 2026-09-03).

## Decision

**1. The log stores what the model saw; redaction moves to export.** `events.jsonl` records
prompts, tool results and model outputs verbatim. Credentials are kept out of every prompt and
every subprocess at the source: a scrubbed environment (`*KEY*`, `*SECRET*`, `*TOKEN*`,
`*PASSWORD*` dropped), credential values never entering a request prefix, secrets referenced
never copied. PII redaction is applied to exported or traced copies only (Langfuse, exports),
through a fail-closed redaction chain, and never to the chained log. This replaces
redact-before-write, which made the log unable to reconstruct a request ("model-visible ⟺ logged"
cannot hold if the log holds something the model did not see) and which corrupted evidence in
practice. The hash chain, stateless verification and the artifact digests are unchanged. The
local log lives in the same trust domain as the dataset it describes; that is the security
argument, and it is the owner's call as data-protection owner.

**2. The sandbox fails closed.** `run_python` (and every subprocess tool) refuses to run when
the requested confinement cannot be enforced, with an error that names what is missing; running
unconfined requires an explicit `danger-full-access` mode, intended for development. Three modes,
copied from dsh: `read-only`, `workspace-write` (the Run workspace only), `danger-full-access`.
Enforcement is recorded on the tool result as `full` or `partial`, never silently degraded, so
MIRA can see under what confinement code ran (ADR-0005 idea 6 becomes mandatory). On Windows the
backend is a restricted-token (ACL) runner or a container; on Linux, Landlock or bubblewrap
through our own wrapper, not dsh's binary (ADR-0005 §5 stands).

**3. Chain-of-thought is still never persisted.** The rule from ADR-0004 stands. What changes: the
`llm.completed`-class event records that reasoning occurred, its token count, and a sha256 of its
content, so an auditor can prove it existed and was not altered without being able to read it.
If a provider needs opaque replay state to resume (signed thinking blocks), that state is stored
as an adapter-owned field marked unreadable and excluded from MIRA's corpus. dsh persists
reasoning for replay and UI fidelity; a regulated data-science Run cannot present a model's
reasoning as the explanation of a decision, so Thymira does not store it.

**4. Permissions have two layers.** Underneath, the OS sandbox modes of decision 2. On top, the
Policy Engine's action, capability and finding rules, unchanged in authority. Two conditions
follow: every tool declares honest metadata (side effects, filesystem scope, network, whether it
is concurrency-safe), because a fine table over dishonest metadata is worse than dsh's coarse one;
and the model experience is dsh's: a denial is a fact in the tool result, not an exception; a
denied call may be retried exactly once in the same turn with `sandbox_permissions` and a
one-sentence `justification`, which is what raises the human approval; a `never` approval policy
is checked before any answerer so no listener order can bypass it; a rejected escalation is final
for that call.

**5. Agents talk only to their orchestrator.** Restates ADR-0004 decision 5 and closes what dsh's
Agent Teams left open. A sub-agent sends to and receives from its orchestrator only. Siblings
share artifacts and an orchestrator-owned task board (tasks with compare-and-set revisions and
validated DAG dependencies, folded from the log), never messages. Inside MIRA, each audit agent
sees evidence and never another auditor's findings until a critic stage that MIRA's orchestrator
runs after all auditors have reported, because corroboration needs independent observations.

**6. No compatibility promises before 1.0.** `events.jsonl` carries a monotonic format version;
readers refuse a foreign version with a distinct error, never guess; there are no migrations, no
compatibility shims and no frozen contract until a Run completes end to end under the acceptance
test. ADR-0005's "add the field now because the contract freezes on day 1" sequencing is
withdrawn: the contract is versioned, not frozen.

**Not decisions, recorded so they are not re-asked.** The loop contracts dsh gets right — a turn
always ends with a reason, a durable inbox claimed atomically, three failure classes (tool error
visible to the model, driver failure that ends the turn, calls skipped by cancellation), one
interception point before each step — are implemented over PydanticAI and LangGraph
(`history_processors`, tool `prepare`, `DeferredToolRequests` + `interrupt()`, `ModelRetry`,
`UsageLimits`), not as a hand-written loop. Cordis is not adopted (ADR-0005); only its
extension-point vocabulary is: an observer that never blocks (MIRA), a policy that may
short-circuit (the Policy Engine), a terminal decision (the Gate).

**First pull request.** In a worktree off `main`: an acceptance test that records the demo Run
with `ScriptedProvider`, commits the system prompt and tool schemas sent to the model as
sidecars and the expected workspace as an independent oracle; the environment block (persona with
model and cwd, a runtime-context snapshot delivered as a superseding user message); the eight
tools with dsh's model-facing descriptions and a mandatory `description` argument. Second pull
request: failure as data and the real gate. Rule for both: no abstraction without a caller in the
same change.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep redact-before-write and add a separate unredacted copy | Two logs, one of them unchained; the chained one still cannot reconstruct a request. The problem is where redaction sits, not how many copies exist. |
| Redact only credentials before write, PII on export | Credentials never reach the log if they never reach a prompt; scrubbing the source is stronger than scrubbing the sink and has no false positives on numbers. |
| Fail open with a warning when no sandbox backend is usable | This is today's behaviour and it is invisible in the evidence. dsh's `SANDBOX_UNAVAILABLE` names the missing backend and the escape hatch; a warning nobody reads does not. |
| Persist reasoning in a separate file with redaction (dsh's model) | Storage is not the objection; presentation is. A stored transcript will be read as the reason for a decision, which it is not. Tokens and a digest prove existence without that risk. |
| dsh's two-axis permissions only (sandbox mode × approval policy) | Removes the Policy Engine's per-action and per-finding rules, which are the product's differentiator. |
| dsh's Agent Teams (peer mailbox, claimable task board) | Sibling messages bypass the plan and the log's attribution; auditors that read each other anchor. The board mechanics are kept, the mailbox is not. |
| Freeze Contract v0.5 now and migrate later | No Run has completed; every freeze so far has cost a breaking change (ADR-0007). A version number and refusal is cheaper than a migration nobody needs yet. |
| Copy dsh's compaction bracket | Already rejected by ADR-0007: Thymira's compaction is one atomic append, so an unterminated bracket cannot exist. The reading report was corrected on this point. |

## Consequences

- `packages/events` loses the redact-before-write path and gains a format version, a refusal
  error and an export-time redaction chain; the CARD regex stops touching evidence.
- `runtime/tools` gains the three sandbox modes, the enforcement field, honest tool metadata, the
  denial-as-fact result (`[denied]`) and the human's one-shot ticket matched by intent; the
  same-call escalation with a `justification` was not adopted: the step ends on the review and
  the mandatory `description` is what the human reads (third pull request, 2026-09-05, plan
  `docs/superpowers/plans/2026-09-05-harness-basics-3.md`). `run_python` stops running unconfined
  by default, which will break the current developer flow until `danger-full-access` is set
  explicitly. That cost is accepted.
- `thymira.agents.llm` records reasoning tokens and a digest and never the text.
- ADR-0004's topology gains a task board and MIRA gains a critic stage; no new communication
  channel is opened.
- ADR-0005 keeps its no-dependency decision and its twelve ideas, minus the two stances
  superseded here; ADR-0007 keeps atomic compaction.
- The acceptance test becomes the roadmap's unit of progress: each pull request must leave it
  greener, and every mechanism above lands only with the test that exercises it.
- The acceptance test recorded on 2026-09-04 found two product defects it works around and which
  the second pull request owns: `register_dataset` refuses the shipped `data/german_credit.csv`
  (two ragged rows), and `run_experiment`'s default training code cannot handle categorical
  columns. Both were fixed by the second pull request (2026-09-05, plan
  `docs/superpowers/plans/2026-09-05-harness-basics-2.md`), which also moved dataset registration
  into Inspect.
- The third pull request's composed proof (2026-09-05, plan
  `docs/superpowers/plans/2026-09-05-harness-basics-3.md`) found one product defect it fixed in
  place: a `ThyGraph` compiled with LangGraph's default checkpointer and invoked from inside the
  Core's `thy` node runs as a subgraph that borrows the parent composition's checkpointer, so a
  resumed composition replayed the *parent's* checkpoint into ThyGraph's channels instead of the
  seeded state a tool-call review resumes with. `ThyGraph` now compiles with no checkpointer and
  is invoked at its own root with an explicit checkpoint coordinate, which makes LangGraph drop
  the inherited `configurable` so every pass starts from the seeded state. The final fix wave
  also closed the `InlineDispatcher` exception gap: every `Exception` escaping its execution
  boundary now becomes a recorded `run.failed`, with its cause. One checkpoint follow-up remains
  out of scope: `StateCheckpointer` keys on `thread_id` alone (`checkpoint_ns` is accepted but
  ignored, latent until two subgraphs share a thread id).
- The fourth acceptance follow-up, described in plan
  `docs/superpowers/plans/2026-09-06-harness-basics-4.md`, drives the recorded demo through Core's
  `RunService`/`InlineDispatcher` composition with `RiskInterviewService` intake and scripted
  classification, authenticated non-automatic human answers, deterministic MIRA controls, and
  the final Gate. Its controlled audit-agent roster is explicitly empty (`specs=()`); regulatory
  LLM fan-out remains covered by dedicated tests. A3/A6/A15 pass. The recorded Run is blocked:
  A18 cannot independently support the structured profile's numbers under its current REPORT
  contract; A19 and A23 also report the existing sandbox and reproducibility limitations. The
  test preserves those findings and the Policy Engine's final BLOCK. This follow-up also fixed
  interview resumes skipping remaining questions and exposed actual model/tracker identifiers
  in `run_experiment` output instead of requiring the agent to invent them.
- The subsequent profile-evidence slice (`docs/superpowers/plans/2026-09-07-independent-profile-evidence.md`)
  keeps TOOL-14 reports as REPORT artifacts and independently verifies their versioned source
  declaration and statistics. A18 now passes the recorded demo. A19/A23 remain warnings; the
  extended replay reaches Core's two-reopen budget and then waits for human review. This does
  not change the policy or certify that the remaining findings have been resolved.
