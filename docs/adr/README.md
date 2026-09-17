# Architecture Decision Records

This directory holds the Architecture Decision Records (ADRs) for the Thymira repository (including the legacy `mads` prototype).

An ADR captures a single significant architectural decision: the context that forced
the choice, the option that was taken, the alternatives that were rejected, and the
consequences the project now lives with. ADRs are immutable once accepted — a decision
that changes is not edited in place; a new ADR is written that supersedes the old one,
so the history of the architecture stays readable.

## Numbering

Files are named `NNNN-short-slug.md`, where `NNNN` is a zero-padded, monotonically
increasing integer assigned in order of acceptance (`0001`, `0002`, …). The number is
never reused. The slug is a short kebab-case summary of the decision.

## Statuses

Every ADR declares one status in its header:

- **Proposed** — drafted and under discussion; not yet binding.
- **Accepted** — agreed and in force. This is the normal state of a decision the code
  is expected to follow.
- **Superseded by ADR-NNNN** — replaced by a later record. The superseding ADR links
  back to the one it replaces. The old ADR is kept for the historical trail.
- **Deprecated** — no longer relevant, but not replaced by a specific successor.

## Template

Each ADR follows the same shape:

- **Title** — `ADR-NNNN: <decision>`
- **Status** — one of the values above, with the acceptance date.
- **Context** — the forces and constraints that made a decision necessary.
- **Decision** — what was chosen, stated plainly.
- **Alternatives considered** — the options that were rejected, each with a reason.
- **Consequences** — what becomes easier and what becomes harder as a result.

The style follows Michael Nygard's original ADR format
(<https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions>).

## Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-harness-and-two-layer-architecture.md) | Build Thymira on LangGraph + PydanticAI + LiteLLM with two orchestrators and a Policy Engine | Accepted (2026-08-20) |
| [0002](0002-legacy-disposition.md) | Disposition of the legacy `mads` thesis code (port / keep-data / drop) | Accepted (2026-08-21) |
| [0003](0003-naming-thymira-thy-mira-and-model-tiers.md) | Naming: Thymira, THY and MIRA; models are configuration in two tiers | Accepted (2026-08-22) |
| [0004](0004-model-routing-and-agent-topology.md) | Model routing (per-orchestrator models, three tiers, floors, `model.selected`) and agent topology (graphs, declared sub-agents, hub-and-spoke messages) | Accepted (2026-08-22) |
| [0005](0005-deepseek-harness-reuse.md) | What Thymira reuses from DeepSeek Harness (ideas only) and what it does not | Accepted (2026-08-23) |
| [0006](0006-log-vs-surface-and-compaction.md) | The log is not the surface: compaction shadows, it never deletes | Accepted (2026-08-23); decision 5's bracket bullet superseded by ADR-0007 |
| [0007](0007-compaction-is-atomic-no-bracket.md) | Compaction is atomic: one event, no bracket | Accepted (2026-08-23) |
| [0008](0008-mira-audit-orchestrator-and-control-plane-authority.md) | MIRA as the Audit Orchestrator and control-plane authority boundaries | Accepted (2026-08-23) |
| [0009](0009-transactional-operational-state-and-verifiable-evidence.md) | Transactional operational state and verifiable evidence | Superseded by ADR-0010 (2026-08-25) |
| [0010](0010-local-jsonl-operational-state-and-verifiable-evidence.md) | Local JSON/JSONL operational state and verifiable evidence | Accepted (2026-08-25) |
| [0011](0011-mira-preflight-continuous-context-and-control-model.md) | MIRA preflight, continuous context, and control model | Accepted (2026-08-25) |
| [0012](0012-llm-tracing-langfuse.md) | LLM tracing with Langfuse: an opt-in, redacted mirror of the event log | Accepted (2026-09-01) |
| [0013](0013-harness-fundamentals-six-decisions.md) | Harness fundamentals: six decisions after reading DeepSeek Harness end to end (verbatim log with export-time redaction, fail-closed sandbox, reasoning digest only, two-layer permissions, orchestrator-only messaging, no compatibility before 1.0) | Accepted (2026-09-04); supersedes ADR-0005's redact-before-write stance and day-1 freeze |
| [0014](0014-log-and-redaction-boundary.md) | The log/redaction boundary: what the chained log, exports, traces and prompts hold (owner ruling, four sub-decisions, source credential scrubbing, native storage ACLs and fail-closed projections) | **Accepted (2026-09-08)**; DSH acceptance record G.1/F1.1 advanced with behavioral evidence |

> **Renumbered on integration (2026-08-25).** `dev-auditor`'s ADR-0005–0008 collided by number
> with `main`'s own ADR-0005–0007 (both branches assigned numbers independently after the shared
> base commit `0ad41d4`). `main`'s ADR-0005–0007 keep their numbers because they were reached
> first on the branch that already carries THY, tools, the API and the CLI; `dev-auditor`'s four
> records move to 0008–0011 with no change to their content. Their historical references to each
> other and to "ADR-0005/0006/0007/0008" have been updated to the new numbers throughout the
> integration branch. See `docs/roadmap/product-final.md` corrections C-8/C-9 for the two design
> decisions this collision made visible (Run-transition authority; audit disposition vs.
> authorization decision).
