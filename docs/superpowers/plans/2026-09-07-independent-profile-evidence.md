# Independent verification of dataset profiles

Owner: P4, with a bounded P3 producer declaration. Parent: `CTRL-REPORT` and `TOOL-14`.
Decision: `.agents/notes/implemented/independent-profile-evidence.md`.

## Problem and boundary

The harness-basics-4 replay intentionally exposed A18 rejecting a correct profile because its
histogram and correlation values were absent from experiment metrics. The public A18 regression
reproduces this with four CSV rows: without metrics it returns NOT_APPLICABLE; with an unrelated
accuracy metric it returns FAILED. Neither result independently checks the dataset profile.

Keep REPORT as the profile artifact kind. Add its source declaration and teach MIRA to verify
the source and recompute the fields. Ordinary analytical reports retain their existing checks;
profile values never enter the generic bag of numeric evidence. Policy decisions, authorization,
sandbox enforcement and reproducibility requirements remain their own controls.

## Implementation

1. `ProfileDataset` writes `profile_version: 1` and `source` with the registered artifact name,
   dataset digest, schema digest, row cap and ordered column selection (or null for all columns).
   Verify that the producer resolves active source artifacts before reporting their identity.
2. MIRA reads CSV/Parquet with its direct Polars dependency but independently computes the
   profile facts. Verify report/source/schema integrity and schema linkage before comparing
   shape, missingness, cardinality, top frequencies, histogram bins and Pearson correlations.
   Fail on incomplete, unknown or malformed declarations instead of trusting producer output.
3. A18 evaluates profiles even without experiments and includes the checked source digests in
   its control evidence. Keep failures attributable to a report and field. Preserve ordinary
   report fidelity and the read-only ArtifactStore boundary.
4. Replay the production acceptance with real tools and ScriptedProvider. Inspect the new
   audit and final Gate decision before updating expected outcome assertions. Preserve prompt,
   schema, metric and workspace sidecars unless an actual intended difference requires review.

The replay now reaches the pre-existing A23 correction route: A18 passes, A19/A23 produce
WARNING, and two scripted correction attempts leave the missing historical provenance
unresolved. Core's two-reopen budget escalates through the Gate to an unanswered human review.
The recorded session includes those model turns and the Coding agent's prompt/tool-schema
sidecars. Existing data/experiment prompts, schemas, metrics and workspace oracle are unchanged.
No production policy or rework behavior is changed by this slice.

## Verification

- Red reproduction: `test_a18_recomputes_profile_facts_from_the_registered_dataset` fails on
  base `6e80be8` with unsupported truthful histogram/correlation numbers. The no-metrics case
  returns NOT_APPLICABLE. Source-evidence regression also starts red.
- Unit coverage: declared scope, altered facts, missing/tampered evidence, cutoff ties,
  constants, empty and nonfinite inputs, CSV/Parquet, and ordinary-report isolation.
- Integration: the existing recorded acceptance uses the production Core/THY/MIRA/Gate path
  and real local training. Models remain scripted; no live LLM call is needed for deterministic
  evidence verification.
- Gates: `just check`, `just test-cov`, `just check-notes`, locked packaging and workspace wheels.
  Concurrent test processes use different temporary roots.

## Result

Implemented on `codex/profile-evidence-verification`, based on `6e80be8`.

- `just check`: 2,086 passed, 3 skipped, 32 deselected; zero ty diagnostics; four import
  contracts kept; skill and roadmap checks passed.
- `just test-cov`: 2,107 passed, 14 skipped; 90.74% coverage (90% required).
- Recorded acceptance replay: passed, including unchanged existing sidecars and the added
  Coding sidecars; A18 passes with hash-pinned evidence and the Run waits for human review.
- All pre-commit hooks, `just check-notes`, `uv lock --check`, workspace builds and the eleven
  checked member wheels passed. No new source module trips the size/cohesion detector; the
  existing flagged controls module remains at 1,163 lines and 49 top-level definitions.
- Independent review found and closed scale overflow/underflow in Pearson calculation,
  an absolute-tolerance bypass for tiny histogram edges, mixed-report false PASS without
  ordinary metric evidence, and deeply nested JSON escaping as a recursion error. Each has
  a regression test. Source-freshness tests also change captured schema facts while refreshing
  the schema digest, forcing verification against decoded data instead of a stale-hash shortcut.

Models remain scripted for the acceptance; no live LLM call was needed. Runtime model routing,
policy authority, sandbox enforcement and rework policy are unchanged.

Skills applied: repo-skeleton, python-debug, python-testing, python-testing-unit,
python-testing-integration, python-general, python-god-classes, check-imports,
packaging-scaffolding, changelog, ruff, ty.
