# Harness basics 4: recorded acceptance through the composed runtime

## Purpose

Move the recorded credit-risk acceptance Run from direct `run_thy` invocation to the production
Core composition. Harness basics 3 established one-shot tool approvals and correct park/resume
behavior; this slice proves the demo's real files and metrics through intake, risk classification,
execution authorization, THY, MIRA's deterministic audit, and the final Gate.

Base: merged PR #114, commit `b8687a9`. The implementation branch is
`codex/harness-basics-4`.

## Boundary and acceptance criteria

- Use `RunService`, `InlineDispatcher`, `build_runtime_graph_factory`, `RiskInterviewService`,
  the real repositories, builtin tools, local training, and MIRA's deterministic controls.
  Drive intake answers and approvals through the existing public service methods.
- Use `ScriptedProvider` for model responses, including classification. Intake facts must
  describe the actual dataset and intended activity; a scripted low-risk claim cannot conceal
  affected people or sensitive attributes. No network model calls or external services.
- Require a real policy-requested review and an authenticated human answer recorded as
  non-automatic. Verify the Run waits before authorized execution and subsequently resumes.
- Verify persisted evidence: profile versions and classification precede tool execution;
  authorization and denial controls A3/A6 and risk classification A15 accept the evidence;
  MIRA records its report; the final Gate and Run state agree. Other findings must be explained
  by actual evidence, never suppressed to manufacture a passing report.
- Preserve the existing prompt/tool-schema sidecars, metric comparison and independent
  workspace oracle. Any additional Core/MIRA artifacts or changed sidecars require a specific
  explanation and review. Replay remains read-only; snapshot refresh is deliberate.
- The controlled audit-agent roster may be empty: deterministic MIRA remains inside the
  boundary, while regulatory LLM fan-out retains its dedicated tests. State this explicitly.
- Mark this complete graph acceptance as `slow`, with all writable state under `tmp_path`.

## Work sequence

1. Record the existing acceptance baseline and review the composition/service seams.
2. Replace direct THY setup with a shared acceptance runtime builder, adding only the scripted
   classification/intake/approval fixture data needed for the composed route.
3. Add assertions over actual persisted effects and review any oracle changes individually.
   If this exposes a production defect, reproduce it first and fix it separately with a
   focused regression; do not weaken the acceptance criterion or change unrelated behavior.
4. Update the acceptance README and ADR-0013's acceptance status. Reconcile the old plan's
   acceptance follow-up with this implementation, without reopening its other deferred work.
5. Independently review the test boundary and evidence, run targeted tests, then `just check`
   and the full coverage suite. Commit each concern separately with exact file staging.

## Scope limits

This does not implement export-only redaction, new sandbox backends, durable usage recovery,
checkpoint namespaces, multi-review fan-out, agent task boards, skills or external coding
harnesses. Existing findings are not waived. The current sandbox's limits must remain visible
in the acceptance evidence and documentation.

## Validation

The baseline is `tests/thymira/test_acceptance_demo_run.py` on `b8687a9`.
Targeted runs use a unique `--basetemp`; the final complete suite uses `just test-cov`, which
also covers the slow lane. Verify fixture replay leaves the working tree unchanged, lint/types
and import contracts pass, and coverage remains above the repository floor without a regression.

Recorded local results (Windows, Python 3.13):

- `just check`: 1923 passed, 3 skipped, 32 deselected; zero type diagnostics; four import
  contracts kept, none broken; skills, roadmap and Agent Notes valid.
- `just test-cov`: 1944 passed, 14 skipped; 90.74% coverage (90% required). The final explicit
  approval-count and A6 assertions also pass in a focused acceptance rerun.
- `uv lock --check`, generated-skill synchronization, and wheel content checks pass for all
  11 workspace members. Existing prompt, tool-schema, metric and workspace oracle files have
  no diff; no snapshot refresh was used.
- `pre-commit run --all-files` passes after recognizing the existing shared test helpers in
  the naming hook and normalizing three empty Agent Note placeholders.
- An independent review reran the interview regression, experiment output/record check and
  composed acceptance: 3 passed. Its persisted evidence confirms all eight questions, the two
  human approvals and the exact final audit findings.
- A user-authorized manual smoke used the configured fast-tier provider through LiteLLM with
  a synthetic CSV: real risk classification, `AgentRunner`, one policy-gated `profile_dataset`
  call and a validated `DataProfile` output. The artifact and event chain verified. Two attempts
  made six real model calls in total, with estimated provider cost USD 0.0016115; the first
  attempt's local assertion used the wrong serialized tool-status case and was corrected.
  This checks provider connectivity, tool execution and structured-output parsing, not model
  quality: the smoke prompt supplies the expected shape. The complete composed graph remains
  covered by the offline acceptance. Credentials and scratch evidence stay outside the repo.

Dependency deprecation warnings remain. Docker and the remote Linux/Python-version CI matrix
were not run locally.

## Findings from the composed acceptance

The original direct-THY acceptance passed once on the merge base. The composed test exposed two
bounded production defects, each verified with a failing regression before its fix:

- An information resume anchored LangGraph at the interview node's outgoing edge and skipped
  the remaining questions. Anchoring at the predecessor runs the interview again; a fresh
  dispatcher between answers now preserves every required question and profile version.
- `run_experiment` persisted the model and tracker identifiers but omitted them from its
  model-visible result. The output now carries the same identifiers as the recorded experiment.

The recorded full Run currently ends `BLOCKED`. A3/A6/A15 pass; A18 fails critically because
the structured profile is a REPORT whose numeric facts are not independently established by
experiment metrics. A19 and A23 report partial sandbox enforcement and missing reproducibility
metadata. The acceptance pins these actual findings and the Gate's BLOCK. A future change must
decide whether profiles are intermediate evidence or add independent dataset-based verification;
their own numbers cannot serve as their audit oracle.

A separate custom-policy probe established the execution-start review follow-up documented in
[the execution-start review plan](2026-09-06-execution-start-review.md).
At the Core lifecycle boundary, `REQUIRE_HUMAN_REVIEW` parks the Run with an exact persisted
`execution_start` resume origin; a reconstructed dispatcher reads the human answer, an approved
action-rule review enters THY without repeating intake or minting a new decision, and rejection
blocks. Individual tools retain their own Policy Engine and ToolManager authorization.
`ExecutionConstraints.requires_human_review` remains an independent enforced restriction; start
approval does not waive it or guarantee that every constraint-origin review produces an executing
tool. The subsequent [constraint-origin review slice](2026-09-07-constraint-tool-review.md)
routes that restriction through separate one-shot tool approvals while preserving the recorded
start decision. The default demo A18/A19/A23 outcomes remain unchanged.

The later [independent-profile slice](2026-09-07-independent-profile-evidence.md) resolves the
profile follow-up with dataset-based verification. Its replay passes A18, retains A19/A23 and
reaches bounded correction followed by an unanswered human review. The BLOCKED result above
records this plan's original acceptance, not the later outcome.

Skills applied: python-testing, python-testing-integration, python-testing-unit, python-general,
python-debug, repo-skeleton, task-runner, changelog, ruff, ty, check-imports, python-god-classes,
langfuse.
