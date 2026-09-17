# MIRA regulatory risk, compliance packs, and control-plane model

> **First-phase closeout:** implemented and E2E verified locally on 2026-08-25.

- **Status:** Accepted design — first bounded phase implemented and E2E verified (2026-08-25); later phases pending
- **Date:** 2026-08-25
- **Implementation:** evidence records and the first bounded `MiraControlPlane` path are present;
  THY consumption of context snapshots remains out of scope.
- **Authority:** ADR-0011; ADR-0008 defines authorization boundaries and ADR-0010 defines local
  JSON/JSONL evidence storage.

## Scope and current implementation

MIRA currently provides deterministic controls A1–A7, A9–A10, A16, A17 and `audit_run()`, which
returns an `AuditReport` and findings. It is an observer: it reads evidence and does not modify
the workspace.

`ActivityProfile`, `PackBinding`, reviewed control packs, `MiraAuditOrchestrator`, and the context
analyst now produce immutable evidence and bounded intents. `MiraControlPlane` in `thymira.core`
implements the first authorization path for `request_information`, `pause_run`, `resume_run`,
`request_approval`, and `review_findings`. Context snapshots are still not consumed by THY.

The local credit-risk E2E closeout (`tests/thymira/test_mira_e2e.py`) composes the existing
components without a new runtime abstraction. It exercises the full MIRA evidence sequence and
then separately proves the authority boundary: an audit result, finding, intent, or scripted
context does not change `RunState`; an isolated approval does not transition a Run; only the
recorded decision, exact authorization context, recorded approval, and `RunController` do. The
test verifies the event chain before deliberately detecting a JSONL edit, rebuilds the disposable
`run.json` projection, and keeps the older context JSON readable and unchanged as historical
evidence.

## Model

MIRA evaluates five distinct concerns in order. Governance preflight contains inherent risk and
applicability and may run before THY; evidence controls run only after the relevant evidence exists:

```text
governance preflight (inherent risk + applicability) → evidence controls → findings and proposed actions
```

| Concern | Output | Meaning | Authority |
|---|---|---|---|
| Preflight | versioned context snapshot | Declared activity facts, evidence references, and missing information | MIRA evidence only |
| Inherent risk | risk assessment | Internal estimate of potential harm and needed scrutiny | MIRA evidence only |
| Applicability | pack binding | Why a reviewed regulatory or domain pack applies to one context version | deterministic rule result |
| Controls | control evaluation | Whether a required control is satisfied, failed, missing, stale, or unverifiable | MIRA evidence only |
| Findings and action requests | `AuditFinding` and bounded request | What needs attention and what action may be evaluated | MIRA proposes only |

These outputs must never be collapsed into one score. Inherent risk is not a legal conclusion.
Applicability is not compliance. A failed control does not by itself change inherent risk, and no
MIRA output authorizes an effect.

## Versioned local evidence

Every material context change produces a new immutable JSON snapshot. Risk, applicability, and
control evaluations identify the snapshot they used. New work supersedes or references older
work; it never edits the old JSON file.

For the MVP, the Run-local `events.jsonl` remains the authoritative append-only history.
`run.json` is a reconstructible projection. MIRA snapshots and evaluations are additional
immutable JSON evidence, not another source of operational truth. The single-writer and
non-transactional file limitations are those stated in ADR-0010.

## Preflight and continuous context

Preflight records only evidence-backed facts such as intended purpose, affected people, decision
effect, data categories, autonomy, jurisdiction, safeguards, and missing information. It can be
rerun when a material fact changes.

The context subagent (`DecisionContextAnalyst`) extracts and organizes cited facts into these
snapshots on demand; there is no automatic trigger, latest pointer, or cache in this MVP. It must
report uncertainty and gaps rather than invent facts or make legal conclusions. It is a snapshot
producer for MIRA only: **THY does not consume context snapshots in this MVP.**

## Pack applicability and controls

The `ActivityProfile` is the versioned set of preflight facts. A `PackBinding` links
a reviewed pack version to one profile version and records `applicable`, its rationale, the rule
version, and activating facts. A new material fact creates a new profile and fresh bindings; it
does not rewrite the old binding.

A pack is a reviewed, bounded set of applicability rules, control identifiers, accepted evidence,
freshness requirements, and deterministic checks. It is not raw legislation in a prompt and does
not create policy authority. The first implementation should prefer a generic deterministic
control runner; a specialist subagent is justified only when an independently testable technical
method is required.

## Authority boundary

```text
THY evidence → MIRA observations → findings / action requests
                                      ↓
                         Policy Engine → Gate → authorized effect
```

MIRA may observe, assess, create findings, and request a bounded action. It must not authorize,
execute a tool, update Run state, modify the workspace, or directly instruct THY. The Policy
Engine evaluates the request deterministically; the Gate records the decision and any required
approval; only `RunController` writes `run.transitioned`. The authorization context is exact,
short-lived, and single-use, so an approval cannot expand a scope. MIRA and THY remain independent
and never import or invoke one another.

## Delivery boundary

The remaining work excludes THY consumption of context snapshots, tools and external effects,
database persistence, and workflow-engine abstractions. Those concerns remain outside this
control-plane slice.

## Future directions requiring cross-owner decisions

The following are recorded here as design directions, not as committed roadmap tasks. They need
an owner agreement before entering the single task catalogue in `docs/roadmap/product-final.md`.

- **MIRA quality evaluation.** A versioned golden corpus, mutation cases, and calibration
  measures would assess the quality of MIRA's findings rather than merely its control flow. It
  spans P4's audit semantics and P5's evaluation and release gates.
- **Automatic freshness response.** MIRA can report stale audit evidence through its passive
  freshness control, but deciding whether to invalidate an audit, schedule a re-audit, or reopen
  a workflow changes runtime authority and belongs to a P1/P4 decision. No MIRA finding may make
  that transition itself.
- **Evidence access and retention.** Per-role access to audit evidence, retention periods, and
  deletion/export handling span state storage, API authorization, and user-facing surfaces
  (P1/P4/P5). They must preserve the event and artifact verification guarantees before being
  designed as tasks.
