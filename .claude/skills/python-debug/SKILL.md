---
name: python-debug
description: Use when a test fails, a stack trace or exception appears, a run behaves differently from expected, a bug report arrives, or a failure is intermittent or flaky — before proposing or writing any fix in this Python code base.
metadata:
  version: "1.0.0"
---

# Debugging Python failures

## Overview

No fix before a reproduction you can run on demand. The order is fixed: reproduce → confirm
the exact cause → one hypothesis, one change → regression test that went red first. Reading the
code is a hypothesis; it is never a reproduction and never a confirmed cause.

## When to use

- A test fails, an exception or stack trace appears, or a run behaves differently from expected.
- A bug report arrives, or a failure is intermittent ("about one run in five").
- Before proposing **or** writing any fix.
- Not for: building a new feature (use test-driven-development); a red ruff or ty diagnostic
  (use ruff / ty); a design smell in a large class (use python-god-classes).

## Workflow

Do these in order. The baseline agent skipped 1, 2 and 5 and guessed at 3.

1. **Reproduce on demand, first.** Turn the report into a failing test you can rerun. A runtime
   bug reproduces in-process with `thymira.agents.llm.ScriptedProvider` (no model, no key), an
   `InMemoryEventLog` or a `JsonlEventLog` under `tmp_path`, and a `LocalArtifactStore(tmp_path)`.
   Read the evidence of the reported run first: its event log (`thymira.events.read_events` /
   `verify_log`), the artifact manifest (`store.verify()`), and `thymira.mira.checks.audit_run`
   over both — the controls name the first event that breaks an invariant.
2. **Write the regression test and watch it fail.** Add it *before* any fix and confirm it fails
   for the reported reason (same exception, same key), not a different one. A fix behind a test
   that was never seen red is a guess.
3. **Confirm the exact cause.** Read the real traceback (`pytest -x`, `--pdb`, `python -X dev`);
   name the failing line and value. Do not infer "the missing key is probably X" from reading —
   the traceback names the key, and it is often not the one you would guess.
4. **One hypothesis, one change.** Change only what your hypothesis predicts, then rerun. Still
   failing → revert and form a new hypothesis. Do not wrap the whole function in try/except, bump
   the version and add logging in one pass; you will not know which edit mattered.
5. **Isolate intermittency; never mask it.** "One in five" is an uncontrolled input, not luck:
   pin the varying seed / event order / clock until it fails *every* time, then treat it as an
   ordinary bug. Never paper over a flake with a sleep or a retry. When the suspect range is
   more than ~5 commits, bisect:
   `.agents/skills/python-debug/scripts/bisect_test.py <good> HEAD -- <test node>`.

## Quick reference

| Need | Command |
|---|---|
| Rerun only last failures, stop at first | `uv run pytest --lf -x` |
| One test or keyword | `uv run pytest -k name -x` |
| Drop into pdb on the failure | `uv run pytest --pdb` (or a `breakpoint()` in the code) |
| Find the breaking commit | `uv run python .agents/skills/python-debug/scripts/bisect_test.py <good> HEAD -- <node>` |

Prefer one `breakpoint()` over more than three `print`s. Fast lane while iterating: `just test`;
full suite: `just test-all`.

## In this repository

- Every run is an append-only, hash-chained event log (`thymira.events`). `verify_events`
  reports the first broken link (`seq`, "hash mismatch", "broken sequence"); `audit_run`
  (`thymira.mira.checks`) reports which control failed and at which `seq`. Read those before
  reading code.
- `tests/thymira/test_mira_checks.py::_happy_run` is the smallest complete run (log + store +
  gate + approval); copy its shape to reproduce a runtime bug under `tmp_path`, then assert on
  the persisted events and manifest — not on internal call order.
- Policy surprises: `PolicyEngine.decide_*` returns the matched `rule_id` and a reason; a
  "wrong" decision is usually the fail-safe escalation (unknown risk level, low confidence) —
  read `PolicyDecision.reason` before touching rules.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "I can't run a real model, so I'll diagnose statically." | `ScriptedProvider` + `tmp_path` reproduces it with no model, key or network. Static reading is a hypothesis, not a reproduction. |
| "I can see the fix — add `.get()` / try-except." | You have not shown it fails, nor that this is the failing line. Reproduce and confirm the exact key first. |
| "It's 1 in 5 — just LLM randomness." | Intermittent means an uncontrolled input. Pin it until it fails every time; then it is an ordinary, fixable bug. |
| "I'll make it fail-safe everywhere while I'm here." | Several changes at once on an unconfirmed cause. One hypothesis, one change, rerun. |

**Red flags — STOP if any is true:**

- You are about to edit source with no test that reproduces the failure.
- Your regression test has never been seen to fail.
- You named the cause from reading code, not from a traceback.
- Your fix changes more than one thing.
- Your plan contains "retry", "sleep", or "add `.get()` everywhere".

## Related skills

- python-testing-integration — reproducing API/CLI/service bugs under `tmp_path`.
- python-god-classes — when the bug lives in an overgrown class that resists a localised fix.
