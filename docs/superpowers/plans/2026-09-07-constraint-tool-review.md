# Constraint-origin tool reviews

## Purpose and base

Complete the follow-up to merged PR #118 (`dd0e586`): an approved execution-start review must
be able to proceed through the Tool Manager while preserving the run-wide review requirement.
The current unconditional refusal prevents even a separately approved tool from executing.
The shipped credit-risk rule CR-001 is the named consumer. This extends the existing HITL-01
ticket flow and Core composition; it does not introduce another approval subsystem.

## Decision and boundary

Keep the immutable execution-start decision and its constraints. Interpret
`ExecutionConstraints.requires_human_review` as requiring a distinct human approval for every
tool call under the constraint, including a capability that would otherwise pass on its own.
The Policy Engine applies this escalation before the Gate records the tool decision. Start
approval permits continuation to THY; it never supplies a tool ticket or clears a constraint.

The existing exact-effect ticket binds the validated, redacted tool arguments and tool name.
One human approval permits one execution. Rejection remains final for that effect in the Run;
automatic and nonhuman answers confer no authority. An earlier approval cannot bypass newly
inherited restrictions. Declared MVP human identities remain accepted with their actual
authentication status recorded.

Cached approvals use the same deterministic, non-recording evaluation as fresh decisions and
must match both policy hash and complete constraints. MIRA independently pairs credits by those
recorded identities and rejects older review decisions lacking the active inherited requirement.
Both folds consume credits chronologically, including starts that falsely name another decision.

Other constraints remain enforced: tool allow-lists and prohibitions, external effects,
required evidence and call limits. A policy BLOCK is never downgraded. MIRA independently
reconstructs the authorization evidence through A3/A6, including rejecting a start approval
used as tool authorization or a permissive tool verdict that ignores an active review flag.
Its verification must not invoke the Tool Manager's enforcement logic.

The constraint governs THY's tool context; MIRA's independent read context does not inherit it.
Use trusted Core `run.transitioned` events to suspend this scope at `begin_audit` and restore
it on rework, reporting or terminal exits, checking the system writer, producer, Run identity
and validated state. Keep the requirement across waits/resumes and reject tool-origin or
malformed exemption claims.
`audit.started` is insufficient because it also occurs during preflight, before THY.

When the new inherited/capability combination has disjoint nonempty allow-lists, the Policy
Engine records BLOCK. Do not let the shared model's empty-as-unrestricted convention relax the
call's authorization; a general change to the constraint representation is separate work.

## Work and acceptance

1. Reproduce the unconditional refusal with focused failing tests before changing production
   code. Measure flagged classes and keep new transformations outside their growing bodies.
2. Route inherited constraints into capability decisions and use the existing one-shot ticket
   path. Preserve the recorded decision, exact arguments and policy identity on continuation.
3. Cover policy escalation, synchronous and deferred tool approvals, invalid or spent evidence,
   remaining hard refusals, and independent MIRA verification of valid and fabricated records.
4. Through public RunService methods and real local stores, approve start, observe a separate
   tool review, reconstruct dependencies and approve or reject that tool. Assert persisted
   effects, distinct decisions, unchanged start constraints and A3/A6 outcomes.
5. Update the approval summary, member documentation, earlier deferred-item references,
   changelog and fixed-heading Agent Note. Run targeted tests, independent review, `just check`,
   full coverage and pre-commit checks, then commit the bounded change locally.

Models are deterministic at test boundaries; no live LLM call is needed. Concurrent pytest
runs use distinct `--basetemp` directories. Astra owns architecture, integration and git;
the existing Claude Code session owns production changes and focused policy/tools/MIRA tests,
with a separate Codex agent implementing Core acceptance tests.

## Scope limits

Do not change CR-001's policy rules, default demo fixtures or the recorded A18/A19/A23 findings.
Independent dataset-based profile verification, stronger sandbox enforcement and experiment
reproducibility metadata remain separate work. Do not introduce a new contract field, dependency,
worker backend, client or model configuration merely to route an existing review requirement.
Generic malformed-approval repair and unrelated authorization-fold refactoring stay deferred.

## Validation

- Reproduced the original refusal before implementation: two public Core continuation cases
  failed, and four focused Tool Manager cases could not produce a pending approval.
- `just check`: 2,025 passed, 3 skipped, 32 slow tests deselected; lint, zero-diagnostic type
  gate, four import contracts, skills, roadmap and Agent Notes checks all passed.
- `just test-cov`: 2,046 passed, 14 skipped; 90.86% coverage. The merged-base result was
  1,970 passed, 14 skipped, with 90.75% coverage.
- `uv run pre-commit run --all-files`: passed after staging all new modules and tests.
- Independent adversarial probes covered stale credits, current-policy BLOCKs, older MIRA
  review evidence, malformed ticket identities and contradictory lifecycle records. Passing
  maintained regressions cover these cases, alongside real local-store approve/reject/resume
  flows. MIRA's own read after a genuine Core audit transition remains authorized.
- Existing flagged classes shrank: ToolManager 445 to 436 lines, Gate 408 to 401 and
  PolicyEngine 370 to 359. The manager module shrank from 838 to 673 lines; the extracted
  modules and MIRA authorization ledger introduce no new size or cohesion findings.
- No live LLM test was needed: the changed authorization and audit decisions are deterministic.

Independent review disposition: the bounded Claude review identified an audit-scope exemption
that survived entry into reporting; the final correction closes genuine audit exits and pins
them with regressions. Historical unticketed records and the public legacy refusal export remain
compatible. Controller-less test compositions deliberately fail closed without Core lifecycle
evidence. Broader reconciliation across changing execution-constraint surfaces is separate:
the current Core flow reuses the persisted execution-start decision, including after checkpoint
recovery, and rework routes directly to THY. The generic allow-list merge limitation above is
also pre-existing and remains outside this change.

Skills applied: repo-skeleton, python-debug, python-testing, python-testing-unit,
python-testing-integration, python-general, python-god-classes, check-imports, ruff, ty, changelog.
