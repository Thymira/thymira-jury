# thymira-core

Thymira Runtime: Run/Session services, LangGraph wiring, checkpoints, resume, the single place where ThyGraph and MiraGraph are composed.

| | |
|---|---|
| Import name | `thymira.core` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) |
| MVP priority | P0 |
| Location | `runtime/core/` |

## Status

Implemented and tested (`tests/thymira/test_core.py` and the `test_core_*` suites): provenance
capture, the analytical phase machine, project resolution, a repository-backed `SessionService`,
and an event-backed `RunService` that persists lifecycle changes through the state seams.
`RuntimeState` and the structural `Subgraph`/`SubgraphDeps` contract form the neutral
runtime-to-LangGraph boundary; the initial `RunGraphState` remains available for compatibility.
The run-scoped `UsageLedger` and the execution idempotency/lineage seam (`compute_execution_key`,
`IdempotencyJournal`, `cascade_invalidation`) are implemented, and `StateCheckpointer` persists the
latest LangGraph checkpoint through the backend-agnostic `CheckpointRepository` (`RA-CORE-07`).
The injected `ExecutionDispatcher` seam and synchronous `InlineDispatcher` (`RA-CORE-10`) now
compile and invoke the composed graph for a newly created Run. A queue worker remains a separate
FINAL task (`RA-CORE-11`); `background` is accepted as a reserved option until that dispatcher exists.

The bounded MIRA-to-Run control plane is also implemented
(`tests/thymira/test_core_control_plane.py`): `MiraControlPlane` routes an `ActionIntent` through
deterministic policy evaluation, `Gate` evidence and an optional scoped `Approval`, and
`RunController` is the sole persistent transition writer — no graph, agent, API adapter, policy
rule or approval handler writes Run lifecycle state directly (ADR-0008). It replays
`run.transitioned` events and persists through `LocalRunStore`, where `events.jsonl` is
authoritative and `run.json` is rebuilt from it; the local files are not a multi-file transaction.
THY does not consume MIRA context snapshots in this slice.

`RunController.advance()` now handles a Run's ordinary forward progress without policy bypasses;
`RunService` uses that controller and `LocalRunStore` for all lifecycle writes. The older
repository/`UnitOfWork` seams remain available as foundation code for future backend work, but are
not used by the Run lifecycle. `LocalRunStore` (ADR-0010) is the current persistence boundary.
Composition of THY and MIRA (`RA-CORE-06`) is implemented in
`thymira.core.graph.compose.build_runtime_graph`, which compiles the sequential
governance preflight -> THY -> MIRA evidence audit -> Gate graph. The preflight records inherent
risk and applicable packs before THY; MIRA evaluates produced evidence after THY in `IN_FLIGHT`
mode, so A2 remains a final-closeout control. A PostgreSQL backend (FINAL, behind a new ADR) still
follows the MVP roadmap (`docs/roadmap/product-final.md`, `docs/roadmap/mvp-minimum.md`).

The execution-start review is a Run-level gate before THY execution. A `REQUIRE_HUMAN_REVIEW`
decision parks the Run with an exact persisted `execution_start` resume origin; a reconstructed
dispatcher reads the recorded human answer. An approved start review enters THY without
repeating intake or minting a new decision, while rejection blocks the Run. Each tool receives its own
Policy Engine and ToolManager authorization. `ExecutionConstraints.requires_human_review` remains
an independent enforced restriction: each tool call needs a separate one-shot human approval.
The Run parks again on that exact tool decision, and the existing public approval and resume
methods continue the task after dependencies are reconstructed from disk. Start approval never
waives other restrictions or supplies a tool ticket. The pending start summary discloses these
separate reviews. The default demo A18/A19/A23 outcomes are unchanged.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
