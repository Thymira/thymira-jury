---
name: python-testing-integration
description: Use when a test needs an external service (MLflow), the Thymira API server, the CLI as a subprocess, real model training, a complete graph run, or anything slow or environment-dependent — including choosing what is real and what is faked, dockerized services, and when such a test is flaky.
metadata:
  version: "1.0.0"
---

# Python integration testing (Thymira)

Integration tests exercise several real collaborators inside a **declared boundary** and fake
everything outside it. Decide the boundary first; the rest follows.

## When to use

- A test drives the API (FastAPI app), the `thymira` CLI, an MLflow tracking server, real
  training, or a full ThyGraph + MiraGraph run.
- You assert on what a run leaves behind: the persisted event log, `manifest.json`, MLflow runs.
- An existing integration test is flaky.
- Not for: one member's logic with fast I/O under `tmp_path` — a unit test
  (`python-testing-unit`), even when it writes a `JsonlEventLog` or a `LocalArtifactStore`.

## Quick start

```bash
just test           # -m "not slow"  — integration tests run here too
just test-slow      # -m slow        — complete graph runs
uv run pytest tests/thymira/test_cli_integration.py -q -k NAME
```

## The boundary

| Inside (real) | Outside (faked) |
|---|---|
| `thymira.*` members wired together; `LocalArtifactStore`, `JsonlEventLog`, `LocalRunStore` under `tmp_path`; the FastAPI app in-process (`TestClient`) | model providers (`ScriptedProvider`, never LiteLLM's network); third-party HTTP; cloud storage; anything with a bill, a key or a queue |

State it in the module docstring ("real: repositories + API; faked: provider").

## Rules

1. **Mark by cost**: a complete graph run → `pytestmark = pytest.mark.slow`; a
   service/API/CLI/single-fit test → `integration`. `just test` must stay fast.
2. **Skip, don't fail, without the service**: `pytest.skip("no MLFLOW_TRACKING_URI")`. Hosts,
   ports and credentials come from environment variables (`MLFLOW_TRACKING_URI`), never literals;
   CI provides the services in the full job.
3. **Models are scripted** (`thymira.agents.llm.ScriptedProvider`): a real provider needs a
   key and network CI lacks, and a scripted answer makes the run reproducible.
4. **Only `tmp_path`**, never `runs/` or a reused directory; resources are created and torn
   down by `yield` fixtures, never in the test body.
5. **Assert on persisted evidence** — `verify_events`/`verify_log`, `manifest.json`,
   `store.verify() == []`, the `AuditReport` status, rows, an exit code — never on call order,
   which a refactor may legitimately change. Each fact once.
6. **One shared run builder** (log + store + gate + provider), never a constructor cascade
   copied per test: `tests/thymira/test_mira_checks.py::_happy_run` is the seed — promote it to
   `tests/thymira/conftest.py` when a second module needs it.
7. **In-process first**: `fastapi.testclient.TestClient` for the API,
   `thymira.cli.__main__.main([...])` for the CLI; a subprocess only to test packaging
   (`uv run thymira --help`).
8. **Small fakes and data factories over patch trees**; a fixture that builds a realistic
   `Run`/`Event` set is reused, a `mock.patch` forest is not.
9. **Flaky → quarantine, never retry**: a marker naming an owner (registered first in
   `pyproject.toml`, `--strict-markers` is on); no `sleep`, no retry loop — they hide ordering
   and seeding bugs.
10. **Name by component and behaviour** in `test_<member>_integration.py`:
    `test_run_repository_round_trips_a_completed_run`.

## Workflow

1. Create or extend `tests/thymira/test_<member>_integration.py` with the marker and the
   boundary statement.
2. Build the run through the shared helper; assert only the evidence the task names:

```python
def test_completed_run_leaves_verifiable_evidence(tmp_path):
    log, store, engine = happy_run(tmp_path)          # helper: log + store + gate + provider

    assert verify_events(log.events()).valid
    assert store.verify() == []
    report = audit_run(AuditContext(log.run_id, log.events(), store, engine.policy_sha256))
    assert report.status == "passed", report.to_markdown()
```

3. Run the file, then the lane: `uv run pytest <file> -q -k name`, then `just test` (and
   `just test-slow` if you touched a `slow` module).

## Dockerized services (MLflow)

MIRA's audit and the experiment tools record to MLflow; when a test needs a real tracking server
rather than a temp file store, start it from the test session rather than by hand:
`uv add --group test testcontainers`, then a session fixture over the compose file under
`infrastructure/docker/`:

```python
@pytest.fixture(scope="session")
def mlflow_server():
    from testcontainers.compose import DockerCompose

    with DockerCompose("infrastructure/docker", compose_file_name="compose.yaml") as compose:
        yield compose   # MLFLOW_TRACKING_URI read from its published port
```

Without Docker the fixture skips (Rule 2). No `testcontainers` dependency exists yet
(2026-08); add it through `packaging-scaffolding` with the first service test. Run persistence is
local JSON/JSONL (ADR-0010), so no database service is needed; PostgreSQL is FINAL work behind its
own ADR.

## In this repository

- Evidence API: `thymira.events` (`InMemoryEventLog`, `JsonlEventLog`, `read_events`,
  `verify_events`, `verify_log`), `thymira.state.LocalArtifactStore` (`manifest.json`, `verify()`),
  `thymira.mira.checks.audit_run` (controls A1–A7/A9–A10/A16/A17 → `AuditReport`).
- `Gate(engine, log, approver=auto_approve)` records `policy.decision` →
  `human.approval_requested` → `human.approval`; `auto_approve` / `auto_reject` are for tests only.
- Lanes: `just test`, `just test-slow`, `just test-all`, `just test-cov`; markers registered in
  `pyproject.toml`.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "I'll wire log/store/gate by hand so the test stands alone." | That clones the helper's body and breaks on the next signature change. Call the helper. |
| "I'll fake the store to make it faster." | Then it is a unit test in the wrong lane. Inside the boundary everything is real. |
| "A retry fixes the flake." | A retry hides an ordering or seeding bug. Quarantine with an owner, then fix it. |

## Related skills

- **REQUIRED ROUTER:** python-testing. **SIBLING:** python-testing-unit. python-debug — when a
  run fails or turns flaky. dockerfile — the compose file the session fixture starts.
