# thymira-events

Event model and emitters for the Event API (run.started, agent.*, tool.*, experiment.*, artifact.created, audit.*, human.approval, run.completed, run.failed).

| | |
|---|---|
| Import name | `thymira.events` |
| Owner (MVP roadmap) | P1 (Runtime / Tech Lead) |
| MVP priority | P0 |
| Location | `packages/events/` |

## Status

Implemented and tested (`tests/thymira/test_events.py`): canonical JSON and sha256, source
credential scrubbing, fail-closed export redaction, hash-chained in-memory and JSONL logs, a single
supported global event-log format version, strict refusal of missing or foreign versions and
unknown event types, Contract 0.3 event-envelope metadata, authorization-
context hashes (`hash_authorization_context`), stateless verification, and the log-vs-surface
fold (`derive_surface` / `current_surface`, ADR-0006).

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
