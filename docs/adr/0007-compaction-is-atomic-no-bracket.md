# ADR-0007 — Compaction is atomic: one event, no bracket

- **Status:** Accepted — 2026-08-23
- **Deciders:** the owner
- **Supersedes:** ADR-0006 decision 5, first bullet (the compaction bracket). The rest of ADR-0006
  stands unchanged.
- **Related:** ADR-0005 (where the bracket idea came from), `packages/events`, `packages/schemas`

## Context

ADR-0006 contradicts itself, and the contradiction was found while planning the task catalogue
(`docs/roadmap/product-final.md`, correction C-4).

Decision 3 defines **one** event type: `context.compacted`, carrying the seqs a compaction
shadowed. Decision 5 — deferred to the implementation — requires that "the bracket is `start`
first and `end` last, so a crash mid-compaction leaves a detectable unterminated bracket rather
than a record falsely claiming the compaction finished", and that a MIRA control report an
unmatched bracket as a finding.

**A single event cannot be unterminated.** Either `EventType` gains an opening value, or the
bracket requirement is wrong. `EventType` is a closed vocabulary in a contract that freezes on MVP
day 1, so adding a value later is a breaking change for every consumer — the decision cannot wait
for the compaction engine to be built.

Tracing where the requirement came from settles it. ADR-0005 adopted it from DeepSeek Harness,
whose compaction **mutates** its surface: it appends a summary and rewrites the surface through a
`surfaceOp: {op: 'replace'}`, several steps that are not one write. A crash between them leaves a
genuinely inconsistent log, which is exactly what its bracket detects.

Thymira's compaction does not mutate anything. It appends **one** `context.compacted` event naming
the seqs it shadows, and shadowing is derived by folding the log rather than stored (ADR-0006
decision 2). `EventLog.append` writes one event: before it, nothing has changed; after it, the
compaction is complete. There is no intermediate state, so there is nothing for a bracket to
detect.

**The requirement was imported without the problem that motivated it.** That is a hazard of
adopting ideas across architectures, and it is worth recording as one.

One real gap sits behind the bracket idea and deserves an answer rather than a dismissal: a
compaction that dies during its summarisation call has **spent tokens without producing an
event**, and an audited runtime should not lose a cost. But that is not specific to compaction —
it is true of any model call that fails — and it already has a home: the router records its
choice as a `model.selected` event *before* the call is made
(`runtime/agents/src/thymira/agents/llm/routing.py`), so the attempt is on the chain whether or
not it succeeds. Solving a general accounting concern with a compaction-specific bracket would put
the mechanism in the wrong place.

## Decision

**1. Compaction is one atomic append.** `context.compacted` remains the only event type
compaction adds. No opening event is introduced, and `EventType` does not change. Correction C-4
in `docs/roadmap/product-final.md` is closed with no contract change — the cheapest possible
outcome, and the reason it was worth resolving before the freeze rather than after.

**2. ADR-0006 decision 5's bracket bullet is withdrawn.** A MIRA control for "unmatched
compaction bracket" must not be written; there is no such state. The other two bullets of decision
5 stand and are restated here so the implementer needs only this record:

- The `context.compacted` payload records the summarisation envelope — provider, model, tier and
  token counts — so an auditor can reconstruct *which model hid what*, the way
  `PolicyDecision.policy_sha256` lets an auditor replay a policy decision.
- Only the **safe summary projection** is persisted. Raw provider output is never written to the
  log, which is how ADR-0004's chain-of-thought rule is satisfied by construction.

**3. The cost of a failed compaction is the usage ledger's concern, not compaction's.** The
`model.selected` event already records the attempt before the call. Any accounting for calls that
fail belongs to the ledger for every call kind at once.

**4. Atomicity is now a property to preserve.** Compaction must stay a single append. Any future
design that needs two or more writes to complete one compaction reopens this decision and needs a
new ADR — because at that point the bracket argument becomes correct again.

## Alternatives considered

| Option | Why not |
|---|---|
| Add `context.compaction_started` to `EventType` before the freeze | Spends a permanent contract slot on a state that cannot occur. It would also mislead: a MIRA control looking for unmatched brackets would never fire, and a control that can never fail is worse than no control. |
| Keep one event but add a `phase` field to its payload | Same objection with more moving parts: it encodes a lifecycle that has one step. |
| Leave ADR-0006 as it is and decide when compaction is built | The vocabulary freezes on MVP day 1. Deferring converts a free decision into a breaking change. |
| Treat the failed-summarisation cost as a compaction problem | Puts a general concern (any model call may fail) in the wrong subsystem, and would need the bracket to carry it. |

## Consequences

- **`EventType` is unchanged**, so nothing downstream moves and the contract can freeze as it
  stands. The correction that looked like it would cost a contract slot costs nothing.
- **One task is removed from the catalogue's scope**: the bracket half of the compaction audit
  control (`CMP-01`). What remains of that task — verifying the envelope was recorded and that
  `derive_surface`'s cannot-shadow-forward rule holds — is unaffected.
- **The compaction engine has one fewer thing to get right.** Atomicity is a property of the log,
  not of the engine's discipline, which is a stronger guarantee than an ordering convention.
- **A precedent is recorded**: an idea adopted from another architecture carries the assumptions
  of that architecture. ADR-0005 adopted twelve; this is the first to have needed unwinding, and
  the check that catches the rest is the same one used here — ask what problem the mechanism
  solved *there*, and whether that problem exists *here*.
