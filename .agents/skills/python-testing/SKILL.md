---
name: python-testing
description: Use when deciding what kind of test to write for a change, where the test file goes, which pytest marker (slow, integration) applies, what is worth covering, how to fix or refactor an existing test, or how to run only part of the suite in this repository.
metadata:
  version: "1.0.0"
---

# Python testing router

Tests run in three lanes. Pick the lane from what the test touches, place it in the right
file with the right marker, then run the matching command. Test quality beats test count: a
few tests that pin business rules outlive many that pin implementation details. Two child
skills hold the how-to.

## Pick the lane, file and marker

| The test touches… | Lane | Module marker | File |
|---|---|---|---|
| logic of one member, including fast I/O under `tmp_path` (event logs, the local store, YAML policies) and scripted LLM calls | unit | none | `tests/thymira/test_<member>.py` |
| an external service (MLflow), the API server, the CLI as a subprocess, or real model training | integration | `pytestmark = pytest.mark.integration` | `tests/thymira/test_<member>_integration.py` (whole file in one lane) |
| a complete graph run (ThyGraph + MiraGraph) that takes minutes | slow | `pytestmark = pytest.mark.slow` | its own module |

A repository script (not an importable package) is tested in `tests/tooling/test_<script>.py`.
Vocabulary from other guides maps as: `unit` → no marker, `e2e` → `slow`.

Example: a new `FindingRule` matcher in `thymira.policies.models` → unit, no marker, in
`tests/thymira/test_policies.py`; the CLI as a subprocess → `integration`, in
`tests/thymira/test_cli_integration.py`; the end-to-end credit-risk example through the API →
`slow`.

## Rules

1. A module-level `pytestmark` marks the whole file — position never scopes it to some tests.
   One lane per file; never mix lanes.
2. `--strict-markers` is on: only `slow` and `integration` exist; a mistyped marker fails
   collection.
3. `just test` excludes only `slow`; `integration` tests still run in the fast lane (skip
   themselves cleanly when their service is absent). `slow` is the one lane you skip while
   iterating.
4. Never push on the fast lane alone: run the full suite, then check coverage. No PR lowers
   coverage.
5. LLM boundary: `thymira.agents.llm.ScriptedProvider`; never the network. Files: only under
   `tmp_path`; concurrent runs use their own `--basetemp`.
6. Cover what can be wrong, in this order: the business rule (a decision, a hash, a
   transition), error handling and raised exceptions, each branch, boundaries (empty, `None`
   where valid, limits), and every regression you fixed. Never add a test whose only purpose
   is a coverage percentage; never test a trivial getter.
7. Do not change production code to make a test pass unless the test exposed a real defect —
   then fix the defect, not the assertion.

## Workflows

- **Add tests for a change**: locate the member and its `tests/thymira/test_<member>.py`; read
  what is already covered; add the cases from Rule 6; run the file, then `just test`.
- **Fix a failing test**: reproduce (`uv run pytest <node> -x`); decide whether the expectation
  or the implementation is wrong — the `python-debug` skill owns that decision; apply the
  smallest fix on the wrong side.
- **Refactor tests**: preserve behaviour and assertions; move repeated setup into a
  function-scoped fixture (shared by two or more modules → `tests/thymira/conftest.py`);
  simplify assertions; keep one behaviour per test.

## Lanes and commands

| Command | Runs |
|---|---|
| `just test` | everything except `slow` (unit + integration) |
| `just test-all` | full suite, including `slow` |
| `just test-slow` | only `slow` |
| `just test-cov` | full suite + coverage report |
| `uv run pytest tests/thymira/test_x.py -k name` | one file or test while iterating (`-n auto` to parallelise) |

Before pushing, in order: `just test` → `just test-all` → `just test-cov`.

## Final checklist

- All lanes pass; no test depends on order, the clock, the network or a key.
- Every new rule, branch and raised exception has a test; nothing tests a getter.
- Slow tests carry the `slow` marker; integration tests skip without their service.
- No new dependency was added just for a test (see `packaging-scaffolding` if one is needed).

## Related skills

- **REQUIRED SUB-SKILL:** python-testing-unit — fast, isolated tests for one function or class.
- **REQUIRED SUB-SKILL:** python-testing-integration — services, API, CLI, training, and the
  `slow`/`integration` markers.
- python-debug — the reproduce-first discipline when a test fails.
