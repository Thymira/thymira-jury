---
name: python-testing-unit
description: Use when writing or reviewing a fast, isolated pytest test for one function or class — pure logic, validators, policy rules, event hashing, prompt builders — with no network, model training or real LLM call; covers naming, Arrange-Act-Assert, what to cover, fixtures, parametrize and fakes vs mocks.
metadata:
  version: "1.0.0"
---

# Python unit tests: fast and isolated

## Overview

A unit test pins one behaviour of one function or class in milliseconds — no network, no
training, no real LLM (fast I/O under `tmp_path` is fine). Core principle: **drive the unit
through its public entry point with a fake at the real boundary, and assert on what it
returns — never reach past that entry point into private helpers.**

## When to use

- Pure logic, validators, policy rules, hashing/redaction, prompt builders, deterministic
  escalation rules (for example `PolicyEngine.decide_capability`'s fail-safe escalation).
- The fast lane: `just test` (`pytest -m "not slow"`).
- **Not for**: external services, the API server, the CLI as a subprocess, real training or a
  full graph run (`python-testing-integration`); choosing the lane at all (`python-testing`).

## Quick reference

| Need | Use |
|---|---|
| the LLM boundary | `thymira.agents.llm.ScriptedProvider([...])`, or a tiny class implementing `LLMProvider` |
| an event log / store | `InMemoryEventLog(run_id)`; `JsonlEventLog(tmp_path / "events.jsonl", run_id)`; `LocalArtifactStore(tmp_path / "artifacts", run_id)` |
| a policy | `Policy(...)` with only the rules under test, or `load_policy_stack("credit_risk")` |
| environment, clock, git | `monkeypatch.setenv` / `monkeypatch.setattr` on the module under test — never `unittest.mock.patch` strings |
| many input rows | `@pytest.mark.parametrize`, not a `for` loop of `assert`s |
| a value domain (hashes, confidences) | `hypothesis.given(...)`, not two or three hand-picked literals |
| shared setup | a function-scoped fixture in the module; used by two or more modules → `tests/thymira/conftest.py` |

## Rules

1. **Name the behaviour**: `test_<unit>_<expected behaviour>[_when_<condition>]` — the name is
   the failure message. Never `test_decide_2`.
2. **Arrange – Act – Assert, one behaviour per test.** One call to the public entry point;
   assert on the returned value or persisted state (`decision.decision`, `decision.reason`),
   never on whether a boundary was called. Two `# Act` sections mean two tests.
3. **Cover**: the happy path, each branch, the empty case, `None` where the type allows it,
   boundaries (`confidence` at the threshold, `seq` 0, an empty log), invalid input with
   `pytest.raises(<PreciseError>, match=...)`. Skip trivial getters and duplicates.
4. **Fake the boundary, never the subject.** Inject the collaborator; `monkeypatch` only when
   injection is impossible; a fake that behaves beats a `MagicMock` that records calls. Private
   methods are reached through the public seam; module-level pure functions (`canonical_json`,
   `redact`, `hash_event`, `phase_precedes`) are called directly.
5. **Real inputs only**: build models through their constructors so validation runs; never
   `model_construct()` to reach a state the contract forbids.
6. **Deterministic**: fixed seeds, `utc_now` patched when time matters, no `sleep`, only
   `tmp_path`.

## In this repository (worked example)

The fail-safe escalation in `thymira.policies.engine` is tested through `decide_capability()`,
not by calling a private `_escalate`:

```python
from thymira.policies import PolicyEngine, RiskProfile, ToolCapability, load_policy_stack
from thymira.schemas import Decision


def test_low_confidence_risk_escalates_a_passing_capability():
    engine = PolicyEngine(load_policy_stack())
    risk = RiskProfile(risk_level="limited", activity_category="scoring", confidence=0.4)
    capability = ToolCapability(id="read_csv", external_effects=())

    decision = engine.decide_capability(
        run_id="run-1", subject_id="tool-1", capability=capability, risk=risk
    )

    assert decision.decision is Decision.REQUIRE_HUMAN_REVIEW
    assert "Fail-safe escalation" in decision.reason
```

Iterate on one file, then the lane: `uv run pytest tests/thymira/test_policies.py -q`, then
`just test`. `tests/**` already ignores `S101`, magic numbers, missing docstrings/annotations
and `SLF001` — the last one is for the rare unavoidable private touch, not a licence.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "`SLF001` is ignored in tests, so calling the private method is accepted." | A test that names a private member breaks on every internal refactor even when behaviour is unchanged. Reach the logic through the public method. |
| "My provider double must `raise` if called, to prove the LLM is never used." | That is an interaction test. Assert the returned decision; use `ScriptedProvider`, never a must-not-be-called stub or `MagicMock`. |
| "`model_construct()` lets me check the method re-imposes the invariant." | The frozen models validate first; a state that cannot occur tests a ghost. Prove bounds on the validator or on a pure function with hypothesis. |
| "One big test that walks the whole flow is more efficient." | When it fails you learn nothing. One behaviour per test, a name that states the expectation. |

**Red flags — stop if your test:** calls a `_name` from outside; asserts `.called` or a call
count; uses `MagicMock` for the LLM; calls `.model_construct(`; needs more than two fakes;
loops with `assert` inside instead of `parametrize`; is named `test_<function>_<number>`.

## Related skills

- `python-testing` — pick the kind of test and the lane. `python-testing-integration` —
  services, API, CLI, training, full runs. `python-god-classes` — when one unit needs more
  than two fakes. `python-debug` — when the failing test is a bug to reproduce first.
