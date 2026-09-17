# infrastructure/docker

Docker-related service files and images. The root `Dockerfile` / `compose.yaml` build and run the
workspace image (`thymira:dev`, the `thymira` CLI).

| | |
|---|---|
| Owner (MVP roadmap) | P1 / P5 |
| Priority | P2 — no service stack is required for the local JSON/JSONL MVP |

The MVP requires no database service: Run evidence is local (ADR-0010) — `events.jsonl` is the
authoritative append-only history and `run.json` a reconstructible projection. The one service the
`integration` tests reach is MLflow, configured through `MLFLOW_TRACKING_URI` (see the
`python-testing-integration` skill); the planned first file here is a `compose.yaml` `mlflow`
service. A PostgreSQL backend is FINAL work (`RA-STATE-04`) that, per ADR-0010, requires its own
ADR before it is introduced; it must not be assumed by integration tests. See
`docs/architecture/e2e-baseline-v2.md` for the target design and `docs/roadmap/`
(`product-final.md` and `mvp-minimum.md`) for when this area starts.
