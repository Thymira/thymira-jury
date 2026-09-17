# ADR-0011 — MIRA preflight, continuous context, and control model

- **Status:** Accepted — 2026-08-25
- **Deciders:** project owner; MIRA / Governance (P4); Runtime (P1)
- **Related:** ADR-0008, ADR-0010, `docs/contracts/contract-v0.3.md`,
  `docs/architecture/mira-regulatory-risk-and-control-plane.md`

## Context

The implemented MIRA surface is deterministic controls A1–A7, A9–A10, A16, A17 and
`audit_run()`. It produces findings and an `AuditReport`. It does not yet contain an
`ActivityProfile`, `PackBinding`, control packs, a `MiraAuditOrchestrator`, or a context
subagent.

The next MIRA design needs to make inherent risk, regulatory applicability, and control results
separate without allowing an audit result to become an authorization. It also needs a bounded
way to preserve evolving context as local evidence.

## Decision

1. **MIRA starts with preflight.** Before audit work, it gathers declared activity facts and
   evidence into a versioned context snapshot. A missing fact is recorded as missing; it is not
   invented by a model.
2. **Inherent risk is assessed independently.** The assessment describes the activity's risk
   under a reviewed internal method. It is not a legal classification, compliance result, or
   authorization.
3. **Applicability is a separate deterministic result.** Reviewed rules bind a regulatory or
   domain control pack to one version of the activity context. A binding records why a pack is
   applicable or not applicable.
4. **Controls evaluate an applicable pack against evidence.** Their results distinguish
   satisfied, failed, missing, stale or unverifiable evidence. Findings aggregate those results;
   a control result does not alter inherent risk by itself.
5. **Context is continuous and versioned.** Material new facts produce a new immutable JSON
   snapshot and new evaluations that reference the snapshot they used. Older snapshots and
   evaluations remain evidence and are never rewritten.
6. **MIRA observes and proposes.** It may create findings and bounded action requests. It cannot
   authorize a tool, transition a Run, alter a workspace, or directly command THY. The Policy
   Engine decides effects and the Gate records required human approval, as required by ADR-0008.
7. **The context subagent is a snapshot producer only.** When implemented, it produces cited,
   versioned context snapshots for MIRA. THY does not consume those snapshots in this MVP; no
   direct MIRA-to-THY data path is introduced.

## Alternatives considered

### One combined risk/compliance score

Rejected. It hides whether the issue is inherent activity risk, legal applicability, missing
evidence, a failed control, or a policy authorization.

### Let MIRA findings control THY directly

Rejected. It breaks the MIRA observer/proposer boundary and bypasses the Policy Engine.

### Add specialist agents and pack infrastructure before the first flow

Rejected. The current deterministic controls and `audit_run()` remain the implemented baseline.
Profiles, bindings, packs, the audit orchestrator, and the context subagent are documented
next-step work, not code introduced by this ADR.

## Consequences

- The MIRA design has an explicit, auditable sequence: preflight → inherent risk → applicability
  → controls → findings and proposed actions.
- Each result can be replayed from its JSON evidence without treating an LLM output as authority.
- The first implementation remains intentionally small. It does not add new shared contracts,
  persistence code, or a consumer in THY.
