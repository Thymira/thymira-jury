# Contract 0.4 — MIRA activity, applicability, and control evidence

- **Status:** Accepted contract definition; operational integration is pending
- **Date:** 2026-08-25
- **Scope:** Shared `thymira.schemas` records only. This contract creates no storage, pack,
  preflight, orchestrator, agent, Core, or THY implementation.
- **Related:** ADR-0010, ADR-0011, `contract-v0.3.md`

## Purpose

Contract 0.4 makes MIRA's preflight facts, inherent risk, pack applicability, and control
results independently reviewable immutable evidence. They are not operational state and cannot
authorize an effect. For each Run, `events.jsonl` remains the authoritative history and
`run.json` remains a reconstructible projection, as defined by ADR-0010.

## Records

| Record | Meaning | Essential references |
|---|---|---|
| `ActivityProfile` | One version of preflight facts about a stable activity. | `activity_id`, `version`, exactly one `project_id` or `run_id`, `supersedes_id` |
| `RiskAssessment` | Inherent risk only for an exact profile version. It has no mitigation field. | `activity_profile_id`, `activity_profile_version`, method/version, evidence |
| `PackBinding` | Reproducible determination that a reviewed pack does or does not apply. | profile/version, pack/version, rules version, time, jurisdiction, facts, evidence |
| `ControlEvaluation` | Evidence-based result for one control in a bound pack version. | binding, pack/version, control, status, evidence, evaluation time |

All four records are frozen, strict Pydantic contracts with `extra="forbid"`. Their ids use the
existing prefixed-id format: `activity_…`, `profile_…`, `pack_…`, `binding_…`, `control_…`, and
`assessment_…`. A later record links through `supersedes_id`; it never changes the earlier record.

`ActivityProfile` requires exactly one owner (`project_id` or `run_id`). It contains the activity
purpose, affected population, decision effect, autonomy, human oversight, jurisdiction, data
categories, sensitive attributes, potential consequences, and cited `evidence_refs`.

`ControlEvaluationStatus` is closed: `satisfied`, `failed`, `missing_evidence`,
`stale_evidence`, `unverified`, and `requires_human_review`. The evidence-based states require
evidence; `missing_evidence` forbids it. `requires_human_review` accepts either shape because the
reason may be conflicting cited evidence or a documented gap.

## Audit and authorization are different conclusions

`AuditDisposition` expresses MIRA's conclusion:
`PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`.

`AuthorizationDecision` expresses the Policy Engine's possible result for one bounded request:
`ALLOW | ALLOW_WITH_WARNING | REQUIRE_HUMAN_REVIEW | DENY`.

An audit disposition creates no authority. Specifically, `BLOCK` is an audit disposition and
not an authorization value; `DENY` is the authorization refusal. `AuthorizationContext.decision`
uses `AuthorizationDecision`.

The old, published `Decision` name is kept only as an explicit alias for
`AuditDisposition`, preserving the Contract 0.1 vocabulary for existing consumers. New code must
use the unambiguous names above; no compatibility alias is provided for the new MIRA records.

## Event vocabulary

The following event types are the only additions needed to record the new immutable facts:

- `activity_profile.recorded`
- `risk_assessment.recorded`
- `pack_binding.recorded`
- `control_evaluation.recorded`

Their payload schema is intentionally left to the event producer version. The event points to the
immutable JSON evidence; it does not duplicate or replace it.
