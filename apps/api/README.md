# thymira-api

Thymira API (FastAPI): POST /runs, GET /runs/{id}, POST and GET /runs/{id}/plan, durable lifecycle
mutation routes, GET /runs/{id}/events, GET /runs/{id}/audit, GET /runs/{id}/experiments,
GET /runs/{id}/artifacts and GET /runs/{id}/artifacts/{artifact_id}. Clients never hold run state.

| | |
|---|---|
| Import name | `thymira.api` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) |
| MVP priority | P0 |
| Location | `apps/api/` |

## Status

The HTTP boundary is defined in [`docs/contracts/api-v0.1.md`](../../docs/contracts/api-v0.1.md),
and its request/response envelope models are exported by `thymira.api`. `create_app` builds the
FastAPI shell from an explicit `RuntimeDeps` composition root, provides `/healthz`, and exposes
Run creation, inspection, cursor-based listing, durable plan generation, checkpoint resume,
human approval/rejection, idempotent retries, actor resolution, event history/SSE streaming,
receipt-based asynchronous lifecycle observation, and deterministic audit and experiment read
surfaces for its configured project. The MVP still uses
local JSON/JSONL persistence. Basic OpenTelemetry request tracing is installed with a no-op
exporter by default; set `OTEL_TRACES_EXPORTER=otlp` to configure an OTLP collector.

Run the local MVP server from the repository root with:

```bash
uv run thymira-api --workspace examples/credit-risk
```

The command validates the workspace configuration before starting Uvicorn. It uses local
JSON/JSONL state and inline dispatch; production persistence, queues, authentication and external
model/tracking services are outside this local entrypoint's scope.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
