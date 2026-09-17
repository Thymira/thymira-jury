# Execution-start human review

## Purpose and base

Close the execution-start review defect recorded by harness basics 4, after merged PR #116
(`4b9f967`). This is a correction to the existing composition and resume behavior
(`RA-CORE-06`, `RA-CORE-08`, `RA-CORE-10`), not a new roadmap feature. Work on the isolated
`codex/execution-start-review` branch.

When a policy requires a human to approve `execution.start`, the current graph stops without
parking the Run. The public approval endpoint therefore cannot answer the request. Approval
must resume the execution gate, not jump to the final findings gate or repeat intake.

## Required behavior

- Persist a waiting-for-approval transition naming the exact execution-start decision. No THY,
  MIRA audit or tool execution may occur before the required human approval.
- Accept a nonautomatic human approval only for the recorded decision and Run, preserving
  the existing API identity mode: declared MVP identities remain explicitly unauthenticated.
  Reconstruct authorization from the persisted evidence after rebuilding the service and
  dispatcher; the presence of a boolean answer alone is insufficient.
- Approval resumes the correct graph boundary without issuing a replacement decision that
  strands the original answer. Rejection blocks the Run before execution.
- Cover both the public approval method and a separately recorded answer followed by ordinary
  resume. Unanswered, unrelated, nonhuman or automatic answers grant no authority.
- Preserve the exact policy constraints and the Tool Manager's per-tool authorization. A
  policy's direct start review and a review required by execution constraints must be assessed
  separately; entering a fake THY node alone does not prove a real tool can run correctly.
- Keep RunController as the sole lifecycle writer and retain the established PASS, tool-call
  review, findings review, rework and interview paths.

## Implementation and validation

1. Reproduce the defect in focused tests before production edits, using the real Core graph,
   Gate, local repositories and public service methods. Fake only the child orchestration/model
   boundaries. Include a small real registered tool where authorization is the behavior under test.
2. Keep new approval/routing transformations in small functions, with persistence at the
   existing boundaries. Measure flagged classes before and after; do not grow their aggregate
   or broaden this fix into a general rewrite.
3. Add regressions for approve/reject, fresh dependencies, out-of-band answers and invalid
   approval evidence. Preserve the default recorded demo and its independent oracle files.
4. Update the Core README, the previous plan's deferred item, the changelog and a fixed-heading
   Agent Note once the behavior is established.
5. Obtain an independent review, run targeted tests, `just check`, the full coverage suite and
   pre-commit checks, then commit each coherent change locally. All pytest runs use distinct
   temporary directories; no live model call is needed for this deterministic lifecycle fix.

## Scope limits

This does not resolve the demo's A18 profile-evidence finding, A19 sandbox limitations or A23
reproducibility metadata. It does not add a sandbox, a new client, a worker backend or an LLM
authorization path. The acceptance Run must continue to expose its actual audit outcome.

`ExecutionConstraints.requires_human_review` still refuses every tool after a start approval.
The pending approval summary now states that limitation, including mixed action-rule and
constraint-origin reviews. Converting the blanket refusal into per-call review tickets needs
a separate Tool Manager/MIRA decision, with the credit-risk rule CR-001 as a named consumer.

That follow-up is implemented by [constraint-origin tool reviews](2026-09-07-constraint-tool-review.md).
It retains the start decision and requires a separate one-shot approval for each tool under
the inherited flag. The results below describe this original lifecycle correction's boundary.

The default API's declared human identities remain supported; verification of identity stays
with the existing principal resolver. A future custom writer that appends an automatic or
nonhuman answer can still consume the generic pending request without authorizing execution;
the shipped start-approval path rejects such a resolver before it writes. Recovery of those
invalid records and consolidation of the Core/tools/MIRA human-answer predicates are separate
follow-ups.

## Result and verification

- Pending reviews persist their exact decision and resume origin; approval continues with the
  preserved constraints, rejection blocks, and direct policy BLOCK is terminal.
- Recovery rebuilds the store, dispatcher, graph factory and tool dependencies from disk.
  A crash after the park but before the checkpoint does not remint the decision. An answer
  recorded out of band still requires the public resume transition before THY can execute.
- Conflicting answers cannot erase a rejection. Out-of-band rejection records its original
  human author separately from the caller that triggers resume.
- The API approve/reject routes are covered with a real project policy overlay and a declared
  human actor. Synchronous answers, missing decisions and invalid resolvers are covered too.
- Independent Claude Code review found no remaining blocker after the corrections; its final
  targeted verification passed 421 tests. Additional acceptance cases then pinned complete
  dependency reconstruction and the two pre-execution failure paths.
- `just check`: 1,946 passed, 3 skipped, 32 deselected before the final three added cases;
  zero type diagnostics; four import contracts kept; skills, roadmap and Agent Notes valid.
- Final `just test-cov`, including those added cases: 1,970 passed, 14 skipped; coverage 90.75%
  (merged base: 90.74%). `pre-commit run --all-files` passed; its Windows-name hook had no
  applicable files. No live LLM test was needed for this deterministic change.
- Measured flagged classes: RunService 461 to 454 lines; composition nodes stay at 488 and Gate
  stays at 408. No new dependency, contract version or model configuration was introduced.

Skills applied: repo-skeleton, python-debug, python-testing, python-testing-unit,
python-testing-integration, python-general, python-god-classes, changelog, ruff, ty, check-imports.
