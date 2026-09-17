# F8.7 vertical slice: composite (thread_id, checkpoint_ns) checkpoint identity

1. Reproduce the thread-only-keying collision with failing tests: two namespaces sharing a
   thread, in both write orders, at the repository conformance seam and the `StateCheckpointer`
   seam, plus two real compiled LangGraph subgraphs sharing one thread.
2. Widen `CheckpointRepository.put/get/list` to a required `(thread_id, checkpoint_ns)` pair;
   store local blobs under `<root>/checkpoints/<thread_id>/<sha256(checkpoint_ns)[:32]>.bin` so
   LangGraph's reserved `"|"`/`":"` separators and the empty root namespace both survive on
   Windows.
3. Give the PostgreSQL `checkpoints` table a composite `(thread_id, checkpoint_ns)` primary key
   and widen `PgCheckpointRepository` to match, with no new Alembic revision.
4. Replace `StateCheckpointer._thread_id` with a single `_checkpoint_key` extractor and route
   `get_tuple`, `put` and `put_writes` through it.
5. Prove both write orders keep independent latest checkpoints and pending writes at the
   checkpointer, on the local backend's own disk layout, and in the shared backend-agnostic
   conformance suite; prove two real subgraphs sharing a thread checkpoint under three distinct
   namespaces and resume the interrupted one without replaying the other.
6. Run the PostgreSQL integration lane against dockerized `postgres:16-alpine`: the identical
   conformance suite plus an independent schema oracle
   (`sa.inspect(engine).get_pk_constraint("checkpoints")`) confirming the composite key.
7. `just check` (lint, ty ratchet at zero, check-imports, validate-skills, check-roadmap,
   check-notes, fast lane), then record the Agent Note and the closure record entry.

The [DSH acceptance record](2026-09-07-dsh-acceptance.md) governs foundation completion.
This slice does not complete F8.1-F8.6 (compaction, pruning, checkpoint sections, overflow
retry, end-to-end context recovery), does not key `put_writes` by `checkpoint_id` (that needs
multi-checkpoint retention per namespace, a separate storage-model slice), does not set
`checkpoint_ns` at any production call site, and does not touch ThyGraph's or MiraGraph's
checkpointer stance. F8 stays open.
