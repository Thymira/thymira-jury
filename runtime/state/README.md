# thymira-state

Persistence: repository protocols with local JSON/JSONL backends for the MVP. `events.jsonl` is the
authoritative append-only Run history and `run.json` a reconstructible projection; artifact bytes
remain in `LocalArtifactStore` with a verified manifest. PostgreSQL implementations of the same
protocols are FINAL work (`RA-STATE-04`) that, per ADR-0010, requires its own ADR before it is
introduced.

| | |
|---|---|
| Import name | `thymira.state` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) with P3 for artifacts |
| MVP priority | P0 |
| Location | `runtime/state/` |

## Status

Implemented and tested: the `ArtifactStore` protocol and `LocalArtifactStore` with a verified
manifest and cross-process attachment-batch publication (`tests/thymira/test_state_artifact_store.py`,
`tests/thymira/test_state_durable_storage_integration.py`); paginated Run and Session repositories,
per-run child records, the hash-chained `EventStore`, LangGraph checkpoints, and the `UnitOfWork`
write seam (`tests/thymira/test_state_repositories.py`, `test_state_uow.py`, and the
backend-agnostic conformance suite `tests/thymira/test_repo_conformance.py`, `RA-STATE-05` done);
and `LocalRunStore` for one local writer per Run (`tests/thymira/test_state_local_run_store.py`).
Single artifact saves delegate to the same content-addressed batch path, so logical names in the
manifest never point at mutable content URIs.

`LocalRunStore` writes `runs/<run_id>/events.jsonl`, a derived `run.json`, `mira-context/`, and
`exports/`. Opening a Run verifies the event chain and rebuilds the projection when it is missing,
invalid, or stale. `LocalRunStore.create_context()` creates one immutable
`mira-context/<context_id>.json` snapshot and records `mira.context_created` with the caller's
expected Run version; the snapshot file and event append remain separate operations, with no
multi-file transaction or latest pointer.

The repository, `EventStore`, `CheckpointRepository` and `UnitOfWork` protocols are implemented,
tested P1 foundation code; `RunController` and `RunService` now use `LocalRunStore` as the
control-plane persistence seam. The local `UnitOfWork` restores only files staged by its
transaction when a commit fails; it is a development backend, and concurrent Run/UnitOfWork
writers are out of scope for the MVP. Artifact batch writers are serialized by the manifest lock.
PostgreSQL implementations of every
repository protocol are FINAL work (`RA-STATE-04`) that, per ADR-0010, requires its own ADR before
it is introduced.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Do not claim multi-file transactions, concurrent Run writers, or production fault tolerance. The
  MVP permits one writer per Run; verification and projection rebuild use `events.jsonl`. Artifact
  batch publication is the bounded exception: its manifest transaction coordinates local
  processes, and its content-addressed objects remain immutable.
- `run.json` replacement is atomic within that file only. Event append, projection replacement,
  and MIRA evidence files are separate operations.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
