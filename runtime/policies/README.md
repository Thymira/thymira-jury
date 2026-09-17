# thymira-policies

Policy Engine: deterministic rules that turn audit findings (severity, confidence, evidence, framework) into PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK. The LLM never decides enforcement.

| | |
|---|---|
| Import name | `thymira.policies` |
| Owner (MVP roadmap) | P4 (MIRA / Governance) |
| MVP priority | P0 |
| Location | `runtime/policies/` |

## Status

Implemented and tested (`tests/thymira/test_policies.py`, `test_examples.py`): rules as data, `PolicyEngine` with fail-safe escalation, `Gate`, loaders and the `base` / `credit_risk` defaults.

Capability decisions preserve inherited execution constraints. A run-wide human-review flag
escalates an otherwise passing or warning capability to `REQUIRE_HUMAN_REVIEW`; it never lowers
BLOCK. The Gate records this decision before asking about the exact tool effect. Its
execution-start summary explains that each tool will need separate human approval, so an
approved start does not imply approval for subsequent calls.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
