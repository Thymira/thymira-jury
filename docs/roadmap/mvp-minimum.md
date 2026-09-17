# Roadmap — the minimum MVP

*The smallest subset that proves the product thesis end to end. Every task here is defined
in [`product-final.md`](product-final.md) — this document only selects and orders them, so
the two can never disagree.*

**82 of 131 tasks · 187 points** (S=1, M=2, L=4).
**Status: 82 done, 0 partial, 0 todo.**

## What the MVP must prove

```text
thymira run "analyse this dataset, build a baseline model and evaluate it"
  CLI -> API -> Run -> THY (data, coding, experiment agents)
      -> sandboxed execution + MLflow + artifacts
      -> MIRA findings + evidence -> Policy Engine decision -> human approval
```

If that runs and MIRA can reconstruct every step from the hash-chained log, the MVP is
done. Anything that does not serve that sentence is FINAL.

## Load per member

| Member | Role | MVP tasks | Points | vs mean |
|---|---|---|---|---|
| **P1** | Runtime / Tech Lead | 18 | 42 | +12% |
| **P2** | THY / Agents | 18 | 43 | +15% |
| **P3** | Tools / Execution / MLflow | 15 | 35 | -6% |
| **P4** | MIRA / Governance | 15 | 39 | +4% |
| **P5** | CLI / UX / QA / Integration | 16 | 28 | -25% |

**On P5.** P5 cannot be raised further by any move that respects ownership: their whole
lane (every CLI command, all QA and E2E) is already theirs, and the only misfiled test has
been moved to them. P5's lane is **dependency-back-loaded, not light** — every CLI and E2E
task waits on the API and the agents existing. P5 has no day-1 task of their own, so their
early weeks are spent pairing with P1 on the `state -> core -> API` spine and owning the
MVP documentation. Point parity is the wrong metric for P5; schedule coverage is the right
one, and P1's unpriced integration and review load is what P5 absorbs.

## Day 1 — what each member starts on

| Member | Start with | Why |
|---|---|---|
| **P1** | `RA-STATE-01` | Highest-leverage task in the MVP: 24 downstream tasks depend on it. Start it before anything else. |
| **P2** | `THY-01` | The single root of the THY spine; nothing else in THY can begin until `AgentSpec` exists. |
| **P3** | `TOOL-03`, `TOOL-05`, `TOOL-12`, `TOOL-01` | The richest day-1 slate: the Tool protocol gates every tool, and `TOOL-01` is the sandbox contract that gates `run_python`. |
| **P4** | `GOV-01`, `KB-01`, `HITL-01`, `RISK-01` | Four independent roots: the audit contract, the regulation store, the approval flow and the risk classifier. |
| **P5** | pair with P1 | No unblocked task exists; scaffold the CLI and pytest harness, and start the MVP docs. |

## The critical path

The runtime spine is the true critical path — the longest chain and the largest fan-out:

```text
RA-STATE-01 -> RA-STATE-02 -> RA-STATE-03 -> RA-CORE-02 -> RA-CORE-10
            -> RA-API-01  <-- convergence chokepoint: THY, MIRA and tools all wire in here
            -> RA-API-02 -> { RA-API-03 approve/resume, RA-API-04 events, RA-API-05 audit }
```

`RA-API-01` is where every other spine plugs in; 13 tasks depend on it transitively,
including every CLI command and all four acceptance gates. **If the `state -> core -> API`
chain slips, the whole MVP slips** — which is why P1 was the member to unload first.

## The four acceptance gates

| Gate | Owner | Proves |
|---|---|---|
| `RA-QA-01` | P5 | The whole API and CLI surface works end to end |
| `TOOL-27` | P5 | Real execution, MLflow tracking, spill, and an audit that comes back clean |
| `THY-31` | P5 | THY completes a real data-science workflow |
| `QA-E2E-GOV` | P5 | `PASS` / `WARNING` / `REQUIRE_HUMAN_REVIEW` / `BLOCK` and human approval — the product's reason to exist |

## MVP tasks by member

### P1 — Runtime / Tech Lead

*18 tasks, 42 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `RA-API-01` | FastAPI app factory + dependency-injection composition root | **MVP** | P1 | M | done | `RA-CORE-02`, `RA-CORE-10` |
| `RA-API-02` | Run endpoints: create, get, list (run/inspect) | **MVP** | P1 | M | done | `RA-API-01` |
| `RA-API-03` | Resume, approve, reject endpoints (HITL) | **MVP** | P1 | M | done | `RA-API-02`, `RA-CORE-08` |
| `RA-API-04` | Event API: GET /runs/{id}/events with SSE streaming | **MVP** | P1 | L | done | `RA-API-02` |
| `RA-API-06` | plan endpoint (Agent-API plan()) | **MVP** | P1 | S | done | `RA-API-02` |
| `RA-API-08` | API error model, status mapping, Idempotency-Key, actor seam | **MVP** | P1 | M | done | `RA-API-02`, `RA-CORE-04` |
| `RA-CORE-01` | SessionService + project/config bootstrap | **MVP** | P1 | M | done | `RA-STATE-01` |
| `RA-CORE-02` | RunService: create, transition, terminal | **MVP** | P1 | L | done | `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`, `RA-CORE-01` |
| `RA-CORE-03` | Usage ledger (measured-vs-estimated cost) | **MVP** | P1 | M | done | — |
| `RA-CORE-04` | Execution idempotency keys + lineage invalidation cascade | **MVP** | P1 | M | done | — |
| `RA-CORE-05` | Runtime graph state + THY/MIRA subgraph interface | **MVP** | P1 | M | done | `RA-STATE-02` |
| `RA-CORE-06` | Composition graph: THY -> MIRA -> Gate | **MVP** | P1 | L | done | `RA-CORE-05`, `RA-CORE-07` |
| `RA-CORE-07` | LangGraph checkpointer over thymira.state | **MVP** | P1 | M | done | `RA-STATE-02` |
| `RA-CORE-08` | resume(run_id): replay to the first unfinished step | **MVP** | P1 | M | done | `RA-CORE-02`, `RA-CORE-04`, `RA-CORE-06`, `RA-CORE-07` |
| `RA-CORE-10` | Execution dispatcher seam + inline runner | **MVP** | P1 | S | done | `RA-CORE-02`, `RA-CORE-06` |
| `RA-STATE-01` | Run & Session repository protocols + local backend | **MVP** | P1 | M | done | — |
| `RA-STATE-02` | Per-run stores: RecordRepository, EventStore, CheckpointRepository (protocols + local) | **MVP** | P1 | L | done | `RA-STATE-01` |
| `RA-STATE-03` | UnitOfWork: atomic multi-record run write | **MVP** | P1 | M | done | `RA-STATE-01`, `RA-STATE-02` |

### P2 — THY / Agents

*18 tasks, 43 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `THY-01` | AgentSpec: sub-agent declaration as data + catalog loader | **MVP** | P2 | M | done | — |
| `THY-02` | PydanticAI model binding over the router + LLMProvider (records model.selected) | **MVP** | P2 | L | done | `THY-01` |
| `THY-03` | AgentRunner: generic spec-driven agent execution loop | **MVP** | P2 | L | done | `THY-02` |
| `THY-04` | Tool bridge: agent tool-calling through the Tool Manager + Gate under an allowlist | **MVP** | P2 | M | done | `THY-03` |
| `THY-05` | PromptBuilder over current_surface + prefix-stable system-prompt catalog | **MVP** | P2 | M | done | `THY-03` |
| `THY-06` | Request/prompt provenance: record what the model actually saw | **MVP** | P2 | M | done | `THY-05`, `THY-02` |
| `THY-07` | RunUsage / UsageLimits: shared budget charged per delegated call | **MVP** | P2 | M | done | `THY-03` |
| `THY-08` | Delegation contract: hub-and-spoke agent.message + depth guard | **MVP** | P2 | M | done | `THY-03` |
| `THY-09` | ThyState + ThyGraph scaffold (Inspect -> Plan -> Execute -> Summarize) | **MVP** | P2 | M | done | `THY-03` |
| `THY-10` | Inspect node + project-context loader | **MVP** | P2 | M | done | `THY-09` |
| `THY-11` | Plan node + plan approval through the Gate | **MVP** | P2 | M | done | `THY-09` |
| `THY-12` | Execute node: sequential dispatch of the plan to specialist agents | **MVP** | P2 | M | done | `THY-11`, `THY-08` |
| `THY-13` | Summarize node: model comparison + scientific recommendation + report artifact | **MVP** | P2 | L | done | `THY-12` |
| `THY-14` | Data agent (inspect + profile dataset) | **MVP** | P2 | M | done | `THY-04`, `THY-05` |
| `THY-15` | Coding/Execution agent (generate + execute Python, read result) | **MVP** | P2 | L | done | `THY-04`, `THY-05` |
| `THY-16` | Experiment agent (create experiment, log metrics/params, save artifacts) | **MVP** | P2 | M | done | `THY-04`, `THY-05` |
| `THY-17` | Coding agent error recovery (diagnose failed run, bounded retry) | **MVP** | P2 | M | done | `THY-15` |
| `THY-33` | Lazy public imports for agents without tools | **MVP** | P2 | S | done | `THY-04` |

### P3 — Tools / Execution / MLflow

*15 tasks, 35 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `RA-API-05` | Audit & experiments endpoints | **MVP** | P3 | M | done | `RA-API-02` |
| `TOOL-01` | Sandbox vocabulary and ToolCall enforcement fields (contract, pre-freeze) | **MVP** | P3 | M | done | — |
| `TOOL-02` | ToolResult sandbox fields and manager propagation into the recorded ToolCall | **MVP** | P3 | S | done | `TOOL-01` |
| `TOOL-03` | Self-describing Tool protocol: arguments model, input schema and validation | **MVP** | P3 | M | done | — |
| `TOOL-04` | Manager emits artifact.created for tool-produced artifacts (closes control A10) | **MVP** | P3 | M | done | `TOOL-05` |
| `TOOL-05` | Spill policy: oversized tool results become referenced artifacts | **MVP** | P3 | M | done | — |
| `TOOL-06` | Sandbox protocol and LocalSubprocessSandbox (reports PARTIAL) | **MVP** | P3 | L | done | `TOOL-01` |
| `TOOL-08` | run_python tool over the Sandbox protocol | **MVP** | P3 | M | done | `TOOL-06`, `TOOL-03`, `TOOL-02` |
| `TOOL-09` | File tools: read_file, write_file, list_files (workspace-contained) | **MVP** | P3 | M | done | `TOOL-03` |
| `TOOL-10` | Git tools: git_status, git_diff, git_log, git_commit | **MVP** | P3 | M | done | `TOOL-03`, `TOOL-06` |
| `TOOL-12` | Dataset registry and loader (Artifact kind=dataset with captured schema) | **MVP** | P3 | M | done | — |
| `TOOL-13` | analyze_dataset tool | **MVP** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-14` | profile_dataset tool | **MVP** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-17` | MLflow integration: ExperimentTracker protocol, local impl, and mlflow_* tools | **MVP** | P3 | L | done | `TOOL-03`, `TOOL-04` |
| `TOOL-18` | run_experiment tool (Experiment record + experiment/model events + model artifact) | **MVP** | P3 | L | done | `TOOL-17`, `TOOL-12`, `TOOL-08` |

### P4 — MIRA / Governance

*15 tasks, 39 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `API-GOV` | Governance API routes: audit, approvals, approve/reject | **MVP** | P4 | M | done | `HITL-01`, `MIRA-01` |
| `ASSUR-01` | Assurance bundle (report + decision + evidence index + disclaimer) | **MVP** | P4 | M | done | `MIRA-01` |
| `AUD-EUAIACT` | EU AI Act audit agent (the MVP 'Regulatory' agent) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01`, `KB-02` |
| `AUD-METHODOLOGY` | Methodology audit agent (train/test, leakage, validation, metrics) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01` |
| `AUD-RISK` | Risk audit agent (bias/risk indicators, model limitations) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01` |
| `CTRL-MVP` | Deterministic controls A8, A15 and the routing-floor control | **MVP** | P4 | M | done | `RISK-01` |
| `GOV-01` | Audit contract: AuditInput, AuditAgentOutput, AuditAgentSpec | **MVP** | P4 | M | done | — |
| `GOV-03` | BudgetRule type + Policy.budget_rules field (defaulted empty) | **MVP** | P4 | S | done | — |
| `HITL-01` | Async human-approval flow: ApprovalService + PendingApproval fold + deferred Gate mode | **MVP** | P4 | L | done | — |
| `KB-01` | RegulationStore protocol + LocalRegulationStore + search_regulation tool | **MVP** | P4 | L | done | — |
| `KB-02` | requirements->controls mapping (English) + seed corpus + ingest script | **MVP** | P4 | M | done | `KB-01` |
| `MIRA-01` | MiraAuditFlow + MiraSubgraph: prep -> preflight -> deterministic controls -> agent fan-out/fan-in -> Gate | **MVP** | P4 | L | done | `MIRA-02`, `GOV-01` |
| `MIRA-02` | Audit agent node runner with bounded evidence projection (spec -> structured findings) | **MVP** | P4 | L | done | `GOV-01` |
| `RISK-01` | Deterministic Risk classifier override layer (thymira.agents.risk) | **MVP** | P4 | L | done | — |
| `TOOL-24` | MIRA control: run executed under partial or unusable confinement | **MVP** | P4 | M | done | `TOOL-02` |

### P5 — CLI / UX / QA / Integration

*16 tasks, 28 points.*

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `CLI-APPROVE` | thymira approve / thymira reject RUN_ID | **MVP** | P5 | S | done | `API-GOV`, `HITL-01` |
| `CLI-AUDIT` | thymira audit RUN_ID | **MVP** | P5 | M | done | `API-GOV` |
| `QA-E2E-GOV` | E2E governance test: PASS / WARNING / REQUIRE_HUMAN_REVIEW+approve / BLOCK | **MVP** | P5 | L | done | `MIRA-01`, `HITL-01`, `CLI-AUDIT`, `CLI-APPROVE`, `AUD-METHODOLOGY`, `AUD-RISK`, `AUD-EUAIACT` |
| `RA-API-09` | Basic OpenTelemetry + graph-hash provenance middleware | **MVP** | P5 | S | done | `RA-API-01`, `RA-CORE-06` |
| `RA-CLI-01` | thymira runs + richer status | **MVP** | P5 | S | done | `RA-API-02` |
| `RA-CLI-02` | thymira resume | **MVP** | P5 | S | done | `RA-API-03` |
| `RA-CLI-03` | thymira events --follow (SSE client) | **MVP** | P5 | M | done | `RA-API-04` |
| `RA-CLI-04` | thymira approve / thymira reject | **MVP** | P5 | S | done | `RA-API-03` |
| `RA-QA-01` | Runtime-spine E2E (in-process, scripted agents, four cases) | **MVP** | P5 | M | done | `RA-API-03`, `RA-API-04`, `RA-API-05`, `RA-CLI-01` |
| `RA-QA-02` | API contract test suite | **MVP** | P5 | S | done | `RA-API-05`, `RA-API-06` |
| `RA-STATE-05` | Backend-agnostic repository conformance suite | **MVP** | P5 | S | done | `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03` |
| `THY-30` | CLI: render THY activity (plan, delegation tree, per-agent tokens/cost) | **MVP** | P5 | M | done | `THY-07`, `THY-08` |
| `THY-31` | E2E: THY completes the data-science workflow (happy path + failed run) | **MVP** | P5 | M | done | `THY-13`, `THY-17` |
| `TOOL-25` | CLI: thymira experiment <run_id> | **MVP** | P5 | S | done | `TOOL-18` |
| `TOOL-27` | QA: end-to-end execution test (run_python + MLflow + artifacts + spill, truthful audit findings) | **MVP** | P5 | L | done | `TOOL-08`, `TOOL-18`, `TOOL-05`, `TOOL-04`, `TOOL-12` |
| `TOOL-28` | QA: sandbox enforcement is a recorded fact and drives the MIRA finding | **MVP** | P5 | M | done | `TOOL-24`, `TOOL-08` |
