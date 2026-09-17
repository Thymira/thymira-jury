# Contract 0.3 — Operational state, authorization, and evidence contract

- **Status:** Accepted; first MIRA-to-Run control-plane flow implemented
- **Date:** 2026-08-25
- **Scope:** Shared records and evidence semantics. This document adds no storage implementation,
  API, migration, or future infrastructure abstraction.
- **Related:** ADR-0008, ADR-0010, ADR-0011, Contract 0.2

## Purpose

Contract 0.3 keeps proposals, policy decisions, human approvals, the current Run projection, and
evidence as separate concepts. The implemented first flow keeps all of those records separate and
does not claim a multi-file transaction.

## Event envelope

Every persisted event has a stable, source-credential-scrubbed envelope. Its payload remains typed
by `type` and `schema_version`; model-visible PII is preserved in the canonical chain and redacted
only in exports.

| Field | Meaning |
|---|---|
| `event_id` | Globally unique immutable event identifier. |
| `run_id` | The Run that owns this event. |
| `seq` | Strictly increasing, gap-free position within that Run. |
| `ts` | UTC time at which the event was accepted into the history. |
| `type` and `schema_version` | Closed event kind and its payload version. |
| `actor`, `producer`, `producer_version` | Declared origin; never inferred from a prompt. |
| `correlation_id`, `causation_id` | Optional links to the request and preceding cause. |
| `authorization_context_sha256` | Optional hash that scopes an authorized effect. |
| `payload` | Canonicalizable model-visible facts with known credentials scrubbed at source; never chain-of-thought. |
| `prev_hash`, `hash` | Hash-chain links within the Run. |

An accepted event is logically immutable. Corrections or invalidations are later linked events;
they do not rewrite prior envelopes or payloads.

## Operational storage in the MVP

For one Run, `events.jsonl` is the authoritative append-only history. Verification detects a
sequence gap or collision and an invalid predecessor or event hash. It detects corruption; it
does not stop a privileged filesystem writer from altering files.

`run.json` is a reconstructible current-state projection. It is not evidence and must be rebuilt
from verified `events.jsonl` if the files disagree. MIRA context snapshots, risk assessments,
applicability evaluations, control evaluations, and audit reports are separate immutable JSON
evidence. A later record links to or supersedes an earlier record instead of replacing it.

The MVP has one writer per Run. It does not provide a transaction across `events.jsonl`,
`run.json`, and MIRA JSON files, and it makes no production fault-tolerance or concurrent-writer
claim. Interrupted work can leave a stale projection or an absent expected snapshot; recovery is
verification followed by projection rebuild and explicit inspection of missing evidence.

## Shared records

| Record | Boundary |
|---|---|
| `RunState` | Immutable current-state projection dimensions: stage, condition, wait reason, outcome, version. |
| `RiskAssessment` | Versioned evidence about risk; never a policy decision. |
| `ActionIntent` | Bounded requested effect; creating it does not execute it. |
| `PolicyDecision` | Deterministic Policy Engine result. |
| `AuthorizationContext` | Immutable scope for one possible authorized effect. |
| `Approval` | Separate append-only response to one authorization context. |

`PolicyDecision` does not contain a mutable human response. An `Approval` resolves only the
referenced review and cannot widen its authorization context. A MIRA finding, assessment, or
`ActionIntent` is evidence or a request only; it cannot authorize a Run transition, tool call, or
workspace change.

## First control-plane flow

`thymira.core.MiraControlPlane` accepts only the bounded actions `request_information`,
`pause_run`, `resume_run`, `request_approval`, and `review_findings`. It obtains the deterministic
`PolicyDecision` through the Gate, creates a short-lived `AuthorizationContext`, and records a
separate exact-hash `Approval` where required. `RunController` is the only component that accepts
the resulting persistent transition. It rejects a mismatched Run, intent, subject, policy, stale
state version, expired context, reused context, rejected approval, and an approval that attempts
to widen a non-review context.

Each accepted `run.transitioned` event carries the authorization-context hash and the serialized
scope. The local store derives `run.json.run_state` from these transition events. Appending the
event and replacing `run.json` remain separate operations; on a failed projection update, the
verified event history rebuilds the projection.

## Run state invariants

The shared `RunState` rejects these combinations:

1. A waiting condition without a wait reason, or a wait reason in another condition.
2. A terminal condition without an outcome, or an outcome in another condition.
3. A completed outcome outside the reporting stage.

Operational code must also preserve the evidence rules above. Contract 0.3 does not claim that
the currently partial operational wiring enforces every future authorization or recovery path.

## Compatibility

Contract 0.2 event migration and cross-version operational compatibility are out of scope. A
later change that needs either one must state its reading, retention, and migration behavior
explicitly rather than silently treating versions as equivalent.
