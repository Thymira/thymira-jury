# Contract 0.5 — MIRA decision context evidence

- **Status:** Accepted contract definition; THY consumption is explicitly out of scope
- **Date:** 2026-08-25
- **Scope:** Shared immutable MIRA decision-context records and the `mira.context_created` event
- **Related:** `contract-v0.4.md`, ADR-0010, ADR-0011

## Purpose

`DecisionContext` is a bounded snapshot of the facts MIRA can state about one exact Run head. It
helps a caller inspect the current audit situation; it is not a plan, instruction, authorization,
or replacement for a Policy Engine decision. It stores neither chain-of-thought nor a latest
pointer, and it does not create a dependency from THY to MIRA.

## Records

| Record | Meaning | Evidence rule |
|---|---|---|
| `DecisionContextStatement` | One concise fact, constraint, risk, gap, question, or checkpoint. | It has `evidence_refs`, or `unknown=true` explicitly states the limit of MIRA's knowledge. |
| `DecisionContext` | Immutable snapshot with summary, facts, constraints, risks, evidence availability/gaps, questions, and checkpoints. | Its cited statement references are included in top-level `evidence_refs`; it is pinned to Run version, terminal event sequence/hash, and generation time. |

All records are frozen, strict Pydantic contracts with `extra="forbid"`. Context ids use the
`context_` prefix. Every text field and collection is bounded; a complete serialized context may
not exceed 12,000 characters. The models reject explicit chain-of-thought markers.

## Event and local persistence

`EventType.MIRA_CONTEXT_CREATED` has the wire value `mira.context_created`. The local Run store
writes one `mira-context/<context_id>.json` file with exclusive creation and then appends that
typed event using the caller's expected version. These are intentionally separate file
operations: the MVP makes no multi-file atomicity claim and provides no latest-context pointer.

## Non-authority boundary

The context analyst can use the shared LLM provider only through structured output. It must redact
secrets before persistence, reject unknown evidence references, and declare unknowns rather than
invent facts. The resulting context creates no `ActionIntent`, calls no Gate, changes no Run
state, and is not read by THY in this milestone.
