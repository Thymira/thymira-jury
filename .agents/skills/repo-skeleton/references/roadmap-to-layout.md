# Roadmap item → location

Where each deliverable of `docs/roadmap/` (`product-final.md` and `mvp-minimum.md`) lives. Module names are the suggested
shape (one concern per module, Rule 4 of the skill); the *member* and *layer* are not
negotiable. Owners: P1 Runtime/Tech Lead, P2 THY/Agents, P3 Tools/Execution/MLflow, P4
MIRA/Governance, P5 CLI/UX/QA.

## Week 1 — a real Run end to end

| Deliverable | Owner | Location | Notes |
|---|---|---|---|
| Run / Session services, state transitions | P1 | `runtime/core/src/thymira/core/runs.py`, `sessions.py` | uses `RUN_TRANSITIONS`; emits `run.*` events through `thymira.events` |
| LangGraph composition (ThyGraph + MiraGraph wiring) | P1 | `runtime/core/src/thymira/core/graph/compose.py` (`build_runtime_graph`, `RA-CORE-06`) | the only place THY and MIRA meet |
| Recoverable state / checkpoints | P1 | `runtime/core/src/thymira/core/graph/checkpoint.py` + `thymira.state` (`CheckpointRepository`) | `StateCheckpointer` backed by `thymira.state` |
| Run persistence (local JSON/JSONL, ADR-0010) | P1 | `runtime/state/src/thymira/state/local_run_store.py`, `local_store.py` (artifacts) | one writer per Run; PostgreSQL repositories are FINAL (`RA-STATE-04`) behind a new ADR |
| FastAPI app and routes (`POST /runs`, `GET /runs/{id}`, `/resume`, `/events`, `/audit`, `/experiments`) | P1 | `apps/api/src/thymira/api/app.py` (`create_app`), `deps.py`, `routes/runs.py`, `routes/events.py`, `routes/audit.py`, `schemas.py` (request/response only) | domain types come from `thymira.schemas`; the API never redefines them |
| ThyGraph (Inspect → Plan → Execute → Summarize) | P2 | `runtime/thy/src/thymira/thy/graph.py`, `models.py` (`ThyState`), `nodes/inspect.py`, `nodes/plan.py`, `nodes/execute.py`, `nodes/summarize.py` | nodes are functions over the state; no tool call outside `thymira.tools` |
| PydanticAI agents (Data, Coding/Execution, Experiment) | P2 | `runtime/agents/src/thymira/agents/data.py`, `coding.py`, `experiment.py`, `prompts/` | every model call through `thymira.agents.llm`; prompts are data files |
| Tool Manager + Permission Policy | P3 | `runtime/tools/src/thymira/tools/manager.py` (the policy gate lives in `ToolManager.execute`), `registry.py` | the permission check calls `thymira.policies` (`Gate`) |
| `run_python` sandbox tool | P3 | `runtime/tools/src/thymira/tools/builtins/run_python.py` over `sandbox/` (`base.py`, `local.py`, `container.py`) | stdout/stderr/exit_code/metrics/artifacts as a `ToolCall` result; subprocess only here |
| git tools, MLflow tools | P3 | `runtime/tools/src/thymira/tools/builtins/git.py`, `builtins/mlflow_tools.py` over the `mlflow/` tracker subpackage | one module per tool family; shared protocols get a subpackage |
| MCP exposure | P3 | `runtime/tools/src/thymira/tools/mcp/` | the standard; adapters never bypass it |
| Sandbox manager (process isolation, later containers) | P3 | `services/sandbox-manager/` → member `thymira-sandbox-manager` when it has code | consumes `thymira.schemas`; the Tool Manager talks to it over HTTP/IPC |
| Audit input → Finding → Policy Decision as an executable contract | P4 | `runtime/mira/src/thymira/mira/checks/` (`audit_run` → `AuditReport`, exists), `thymira.schemas` (`AuditFinding`, `PolicyDecision`), `thymira.policies` (engine, exists) | deterministic first; LLM audit agents come in week 2 |
| CLI (`run`, `status`, `runs`, `audit`, `experiment`, `approve`, `reject`) | P5 | `adapters/cli/src/thymira/cli/__main__.py`, `client.py` (httpx), `commands/run.py`, … | typer; no runtime import (contract 4) |
| Week-1 integration tests and `WEEK-1-BUGS.md` | P5 | `tests/thymira/test_api_integration.py`, `tests/thymira/test_cli_integration.py`; `docs/roadmap/week-1-bugs.md` | `integration` marker; bugs classified P0/P1/P2 |
| Dev stack (MLflow) | P1/P5 | `infrastructure/docker/compose.yaml` | the root `compose.yaml` stays the dev image; no database service (ADR-0010), PostgreSQL is FINAL behind a new ADR |

## Week 2 — experiments, MIRA, governance

| Deliverable | Owner | Location |
|---|---|---|
| Experiment tracking (MLflow runs, params, metrics, artifacts) | P3 | `runtime/tools/src/thymira/tools/mlflow/`, `thymira.schemas.Experiment` |
| Experiment agent and comparison | P2 | `runtime/agents/src/thymira/agents/experiment.py`, `runtime/thy/.../nodes/` |
| MiraGraph (Audit Preparation → parallel audit agents → Findings) | P4 | `runtime/mira/src/thymira/mira/graph.py`, `agents/risk.py`, `agents/methodology.py`, `agents/regulation.py` |
| Regulation knowledge base (EU AI Act, credit-risk guidance) | P4 | `runtime/mira/src/thymira/mira/knowledge/` (data files + loader); sources in `docs/legacy/compliance/` until translated |
| Policy rules per domain | P4 | `runtime/policies/src/thymira/policies/defaults/<domain>.yaml` |
| Human in the loop (approval channel, resume) | P1/P5 | `thymira.policies` `Gate` (records `human.approval`), `runtime/core/src/thymira/core/control_plane.py` `RunController` (resume); API `POST /runs/{id}/approve\|reject`, CLI `approve`/`reject`; async checkable `Approval` = `HITL-01` |
| Data-dependent controls A11, A14, A20–A23 (A12/A13/A18 reserved, unbuilt) | P4 | `runtime/mira/src/thymira/mira/checks/controls.py` (+ `data_checks.py` when it grows) |

## Week 3 — product, stability, demo

| Deliverable | Owner | Location |
|---|---|---|
| E2E hardening, retries, idempotency/lineage | P1 | `runtime/core/src/thymira/core/idempotency.py`, `lineage.py` |
| Event API + observability | P1 | `apps/api/.../routes/events.py` (stream), `runtime/core/.../usage.py` (ledger) |
| UX: CLI output, status views, web skeleton | P5 | `adapters/cli/.../render.py`; `apps/web/` (TypeScript, own toolchain) |
| Packaging + developer experience | P5 | `Dockerfile`, `infrastructure/docker/`, `justfile`, `docs/` |
| Demo project | P5 | `examples/credit-risk/` (data README, `.thymira/`, a recorded run under `docs/roadmap/demo/`) |

## Mechanisms the graphs need (ADR-0004, from the Claude Code workflow study)

| Mechanism | Owner | Location |
|---|---|---|
| Idempotency / lineage: every agent and tool step keyed by an input hash; resume replays the thread to the first unfinished step | P1 | `runtime/core/src/thymira/core/idempotency.py`, `lineage.py`; checkpoints via `thymira.state` |
| Usage ledger + budgets: per-run limits charged by every call; soft/hard thresholds as Policy Engine rules | P1 / P4 | `runtime/core/src/thymira/core/usage.py`; rules in `runtime/policies/.../defaults/base.yaml` |
| Graph provenance: ThyGraph / MiraGraph definition hash in `run.started` next to `policy_sha256` | P1 | `runtime/core/src/thymira/core/provenance.py` |
| Sub-agent declarations (name, task kinds, tool allowlist, tier, max turns, depth) | P2 / P4 | `runtime/thy/src/thymira/thy/agents/*.yaml`, `runtime/mira/src/thymira/mira/agents/*.yaml` + a loader |
| Run recipes (graph, phases, agents, budgets, policies as data; `args` at invocation) | P1, post-MVP | `examples/<project>/.thymira/recipes/<name>.yaml`; loader in `thymira.core` |

## After the MVP (baseline §29)

`packages/sdk-python`, `packages/sdk-typescript` (generated from the API contract),
`adapters/{vscode,opencode,claude-code,codex,generic}` (API clients), `services/workers`
(queue consumers), `infrastructure/{kubernetes,terraform}`. Each starts as its README says:
purpose, owner, priority — and becomes a member or a toolchain directory only when its first
deliverable is scheduled.
