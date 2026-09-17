# thymira-schemas

Typed contracts shared by every component: Run, Session, Agent, Task, ToolCall, Artifact,
Experiment, Event, AuditFinding, PolicyDecision, Approval, and Contract 0.5 MIRA records.

| | |
|---|---|
| Import name | `thymira.schemas` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) |
| MVP priority | P0 |
| Location | `packages/schemas/` |

## Status

The Contract 0.5 shared records are implemented and tested (`tests/thymira/test_schemas.py`,
`tests/thymira/test_schemas_contract_v03.py`, `tests/thymira/test_mira_contracts.py`): frozen
records, closed enums, prefixed ids, composite Run state, separate approvals, authorization
contexts, MIRA activity profiles, pack bindings, control evaluations, and decision contexts.
Operational
integration is partial; the local JSON/JSONL semantics are documented in
`docs/contracts/contract-v0.3.md` and the new record semantics in
`docs/contracts/contract-v0.4.md` and `docs/contracts/contract-v0.5.md`. Contract 0.2's
`Event.surface` (`EventSurface`) and the `ToolCall` sandbox fields (`sandbox_mode`,
`sandbox_enforcement`) remain in place.

## Rules

`tool_refusals` provides pure refusal wording and facts derived from `ExecutionConstraints`.
The Tool Manager and MIRA share these functions without importing each other's execution code.
They add no persisted fields or enum values; the contract version is unchanged.

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
