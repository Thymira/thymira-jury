# ADR-0010 — Local JSON/JSONL operational state and verifiable evidence

- **Status:** Accepted — 2026-08-25
- **Deciders:** project owner; Runtime (P1); MIRA / Governance (P4)
- **Supersedes:** ADR-0009 and the operational-storage decision in ADR-0001
- **Related:** ADR-0008, ADR-0011, `docs/contracts/contract-v0.3.md`,
  `docs/architecture/mira-regulatory-risk-and-control-plane.md`

## Context

The MVP already has a local artifact store and hash-chained JSONL event logs. It does not have
database persistence, a transactional RunController, or production operations. Requiring a
database now would add deployment, migration, backup, and concurrency work before the first
complete THY → MIRA → Policy Engine loop is proven.

The MVP still needs a precise distinction between evidence and convenience state. It must not
claim database transactions, coordinated multi-file writes, or production fault tolerance that
the local implementation does not provide.

## Decision

1. **Each Run has a local JSON/JSONL state directory.** `events.jsonl` is the authoritative,
   append-only history. It contains the redacted, hash-chained event envelopes for that Run.
2. **`run.json` is a reconstructible projection.** It is a current-state convenience file, not
   evidence. A reader can rebuild it from `events.jsonl`; a disagreement is an integrity problem
   to investigate, never a reason to prefer the projection over the event history.
3. **MIRA outputs immutable JSON evidence.** Context snapshots, risk assessments, applicability
   evaluations, control evaluations, and audit reports are written as new JSON files. A later
   result supersedes or refers to an earlier result; it does not rewrite it.
4. **The MVP permits one writer per Run.** The process that owns a Run is its only operational
   writer. Concurrent writers, distributed workers, and cross-process locking are outside the
   MVP. Callers must serialize their work rather than relying on the file layout to coordinate it.
5. **Verification detects corruption.** Verification checks the Run-local sequence, predecessor
   hashes, and event hashes. It detects gaps, collisions, reordering, and modified envelopes; it
   does not prevent a privileged filesystem writer from changing files.
6. **No multi-file transaction is claimed.** An append to `events.jsonl`, an update of
   `run.json`, and creation of a MIRA JSON record are separate filesystem operations. A crash or
   interruption can leave a projection stale or an expected snapshot absent. Recovery rebuilds
   the projection from the verified event history and reports missing expected evidence; it is
   not a production-grade durability guarantee.
7. **No broker, database, generic persistence interface, or migration machinery is added for the
   MVP.** The local layout is deliberately direct and bounded to the first vertical slice.

## Alternatives considered

### A database for the MVP

Rejected. It would solve operational problems the MVP does not yet have while creating a larger
deployment and recovery surface than the local single-writer workflow requires.

### Treat `run.json` as the source of truth

Rejected. A mutable projection cannot prove event order, policy context, or historical state.
The hash-chained `events.jsonl` record is the evidence from which the projection is derived.

### Claim atomic projection-and-event updates over files

Rejected. Ordinary filesystem writes do not provide a transaction across those files. The
documentation and future implementation must expose that limitation rather than simulate a
database guarantee with terminology.

## Consequences

- The MVP is easy to inspect and run locally, and MIRA can recompute from portable evidence.
- Recovery is explicit: verify `events.jsonl`, rebuild `run.json`, then inspect missing or
  superseding JSON evidence.
- The single-writer restriction limits concurrency and availability. This is an MVP constraint,
  not a production claim.

## Future evolution

If a later deployment needs concurrent writers, durable multi-record transactions, operational
queries, or managed backup and recovery, PostgreSQL may be evaluated as a replacement for the
local operational projection. Such a decision must preserve the authoritative event semantics,
make migration and recovery explicit, and be recorded in a new ADR. It is not part of this MVP.
