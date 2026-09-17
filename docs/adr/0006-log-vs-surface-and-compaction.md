# ADR-0006 — The log is not the surface: compaction shadows, it never deletes

- **Status:** Accepted — 2026-08-23
- **Deciders:** the owner
- **Extends:** ADR-0005 (idea 1 of the adoption map), ADR-0004 (chain-of-thought is never persisted)
- **Related:** `docs/contracts/contract-v0.1.md` (Contract v0.2), `packages/schemas`,
  `packages/events`

## Context

Thymira has no context management. There is no compaction, no token budget, no truncation and no
spill anywhere in the workspace, even though ADR-0001 defines a harness as "the machinery around
the models — tools, **context management**, subagents, hooks/events, permissions, durable state and
provenance". Long data-science runs will overflow a model's context; something must eventually
shrink what the model is shown.

That is where an audited runtime and an ordinary agent runtime part company. For a coding agent,
compaction is an efficiency concern. For Thymira it is an **evidence-integrity** concern, because
three invariants collide:

- The event log is hash-chained. Editing, removing or reordering any event breaks the chain from
  that point on — that is the whole point of `verify_events`.
- MIRA recomputes its conclusions from the recorded log instead of trusting success flags. If
  compaction removed events, MIRA would audit a redacted history and not know it.
- ADR-0004 forbids persisting chain-of-thought. A compaction summary is model-written reasoning
  *about* prior steps, so writing one naively into the log would breach that rule.

`Event` as frozen in Contract v0.1 has no concept of what a model actually saw. Every event is
simply a fact, and there is no way to express "this was shown to the model, and later it was not".

DeepSeek Harness solves the same problem with a distinction Thymira lacks (ADR-0005): a **log**
(complete, append-only) and a **surface** (the projection a model is shown). Compaction there does
not delete; it appends a bracketed record naming the events it *shadowed* (a bracket Thymira
does not need — ADR-0007), and a query layer
classifies each event as `current | shadowed | log-only`. Its own log is not hash-chained, so it
can afford to store that classification on the record. Thymira cannot.

## Decision

**1. `Event` gains `surface`, fixed at append time and never changed.**

```text
EventSurface = model_visible | log_only        # what the event IS
```

The default is `log_only` everywhere, including for every event type that already exists. An event
reaches a model only by explicitly saying so, so adding a new event type is fail-safe: forget the
flag and the event stays out of the model's context rather than silently entering it. Governance
events — `policy.decision`, `human.approval_requested`, `human.approval`, `audit.finding` — are
`log_only` by construction: they are evidence about the run, not input to it.

**2. Shadowing is derived by folding the log, never stored.**

```text
SurfaceState = current | shadowed | log_only   # what the event IS NOW, in the model's view
```

`SurfaceState.SHADOWED` is deliberately absent from `EventSurface`. An event is immutable once it
is chained; writing a shadowing mark back onto the record would break the chain it exists to
protect. `thymira.events.derive_surface(events)` returns one `SurfaceState` per `seq`, and
`current_surface(events)` returns the events a model is shown right now. This is stricter than the
design it came from, and it is stricter because our log is hash-chained.

**3. Compaction is an appended record, not a mutation.** A new event type `context.compacted`
carries the seqs it shadowed under the payload key `shadowed_seqs`. A compaction may only shadow
events that *precede* it: `derive_surface` ignores any seq at or after the compaction's own, so a
forged or reordered payload cannot retroactively hide the future. A malformed payload hides
nothing — non-integer entries are dropped rather than trusted.

**4. The chain stays whole.** Nothing is ever removed from the log. The model sees less; MIRA sees
everything. Compaction is lossy for the model and lossless for the auditor, and `verify_events`
keeps passing across compaction because no chained record was touched.

**5. Deferred to the implementation of compaction itself** (not built here — `runtime/thy` and
`runtime/tools` are stubs, and compaction has no agent loop to serve yet):

- The `context.compacted` payload must also record the summarisation envelope — provider, model,
  tier and token counts — so an auditor can reconstruct *which model hid what*, the way
  `PolicyDecision.policy_sha256` already lets an auditor replay a policy decision.
- Only the **safe summary projection** is persisted. The raw provider output is never written to
  the log, which is how ADR-0004's chain-of-thought rule is satisfied by construction rather than
  by discipline.
- ~~The bracket is `start` first and `end` last, so a crash mid-compaction leaves a detectable
  unterminated bracket rather than a record falsely claiming the compaction finished. A MIRA
  control reports an unmatched bracket as a finding.~~ **Withdrawn by
  [ADR-0007](0007-compaction-is-atomic-no-bracket.md) (Accepted).** This bullet contradicted
  decision 3 above, which defines a *single* `context.compacted` event — and a single append
  cannot be unterminated. The bracket was inherited from a system whose compaction *mutates* its
  log; ours only appends, so a compaction is atomic by construction and there is no intermediate
  state to detect. Do not implement it.

## Alternatives considered

| Option | Why not |
|---|---|
| Store `shadowed` on the event and update it | Impossible: `Event` is frozen and hash-chained; any edit invalidates every later event. This is the option dsh takes, and it is available to it only because its log has no chain. |
| Delete or rewrite compacted events | Destroys the evidence MIRA audits and breaks `verify_events`. Never acceptable. |
| Keep a separate mutable "what the model saw" store | Un-auditable state outside the chain, and it contradicts "the runtime owns all state, recorded as events". A fold over the log needs no second source of truth and survives resume for free. |
| Do nothing until a run actually overflows | The field lands in a frozen contract. Adding it after Contract v0.2 freezes is a breaking change for every consumer; adding it now is a defaulted field nobody has to notice. |
| A three-valued stored enum, with `shadowed` simply unused at write time | A type that can express a state the writer must never produce invites someone to produce it. Two enums make the invariant unrepresentable rather than merely discouraged. |

## Consequences

- **Contract v0.2 changes shape before it is frozen**, which is the cheap moment. `Event` gains one
  defaulted field; existing constructors are unaffected. Every event hash changes, which is
  irrelevant pre-MVP because no production log exists.
- **`thymira.events` gains a pure fold** (`packages/events/src/thymira/events/surface.py`) and no
  new state. Five tests cover the default, shadowing across a compaction, the projection, the
  cannot-shadow-forward rule, and malformed payloads.
- **A prompt builder now has a defined input**: `current_surface(events)`. Whoever assembles the
  model's context reads a derived projection instead of inventing its own notion of history — the
  un-audited-surface risk in `LLMProvider.complete(prompt: str, ...)` narrows to one function.
- **MIRA gains a new class of control** it could not previously express: *what was hidden from the
  model, by whom, and does the summary faithfully represent what it replaced?* Compaction becomes
  an audited operation rather than an invisible one.
- **The rest of the context-management stack is unblocked but unbuilt.** Spill, token budgeting and
  the compaction engine all depend on this distinction and now have somewhere to attach.
