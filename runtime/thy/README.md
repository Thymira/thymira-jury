# thymira-thy

THY — the production / data-science orchestrator (ThyGraph: Inspect -> Plan -> Execute -> Summarize) coordinating Data, Coding/Execution and Experiment agents in the MVP.

| | |
|---|---|
| Import name | `thymira.thy` |
| Owner (MVP roadmap) | P2 (THY / Agents) |
| MVP priority | P0 |
| Location | `runtime/thy/` |

## Status

Implemented and tested (`tests/thymira/test_thy_*.py`): `ThyInput`/`ThyState`/`ThyOutput` and
`AgentTask`, and a compiled LangGraph `StateGraph` (`build_thy_graph`/`run_thy`) wiring
`START -> inspect -> plan -> execute -> summarize -> END` with four real nodes — `inspect_node`
(`load_project_context`), `plan_node` (which gates THY's own plan through
`gate.check_action('plan.proposed')` + `allows_execution`),
`execute_node` (dispatches `ThyState.plan` through `Delegator`/`AgentRunner`) and
`summarize_node`/`compare_experiments` (the `Recommendation` and its report artifact). The Data,
Coding/Execution and Experiment specs (`DATA_AGENT_SPEC`, `CODING_AGENT_SPEC`,
`EXPERIMENT_AGENT_SPEC`) live in `thymira.thy.agents`. Each node stays a bare phase-advance
placeholder when its build-time dependency (`project_dir`, `gate`, `artifact_store`) is omitted
from `build_thy_graph`/`run_thy`. THY does not consume MIRA context snapshots in this MVP.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
