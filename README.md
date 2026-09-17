# Thymira

[![CI](https://github.com/Thymira/thymira/actions/workflows/ci.yml/badge.svg)](https://github.com/Thymira/thymira/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

> Governed agentic data science for work that must be reproducible, reviewable, and accountable.

Thymira is a local-first Agentic Data Science Runtime. It turns a data-science request into a
governed Run: agents inspect data, write and execute code, train and compare models, generate
artifacts, and produce reports—while an independent audit path records evidence, checks controls,
and routes sensitive decisions to deterministic policy and human review.

It is designed around one non-negotiable rule:

> Models can propose work. Code and humans decide what is allowed.

## Why Thymira

Most agent systems can act. Thymira is built to explain, constrain, and verify those actions.

- **THY executes the work.** It plans and orchestrates Data, Coding, Experiment, and ML agents.
- **MIRA audits independently.** It evaluates evidence, governance controls, methodology, and
  applicable regulatory frameworks without modifying the workspace.
- **The Policy Engine decides deterministically.** Findings and requested actions resolve to
  `PASS`, `WARNING`, `REQUIRE_HUMAN_REVIEW`, or `BLOCK`.
- **Every important fact is evidence.** Events are hash-chained; artifacts are digested; exports
  are redacted; provenance, model selection, tool execution, approvals, and audit outcomes are
  recorded.
- **Tools fail closed.** Code-executing tools run through the configured sandbox backend. The
  production default is a hardened container; unsupported safe local execution is refused rather
  than silently falling back to the host.

Thymira does not claim legal or regulatory certification. It produces a reviewable evidence trail
for governed data-science work.

## What it can do today

- Run governed data-science workflows through a local API, CLI, or browser console.
- Register CSV and Parquet datasets with captured schema and provenance.
- Profile data, query datasets with SQL, calculate statistics, and generate visual analysis.
- Write and run Python, execute notebooks, train and compare models, and track experiments in
  local MLflow.
- Create verifiable artifacts, including metrics, notebooks, model outputs, charts, and
  evidence-backed PDF reports.
- Classify risk, collect bounded missing-information answers, and pause for human review where
  policy requires it.
- Audit Runs with deterministic MIRA controls, governance preflight, policy packs, grounded
  audit-agent findings, and adversarial verification.
- Inspect Run status, plans, agents, tool calls, approvals, artifacts, audit evidence, events, and
  model usage from the CLI or web console.
- Route models by code-defined task tier, role floor, explicit operator allowlist, and recorded
  usage evidence.

## How it works

```text
CLI / Web Console
       │ HTTP + local API token
       ▼
  Thymira API
       │
       ▼
  Core runtime ───────────────────────────────────────────────┐
       │                                                       │
       ├── THY: inspect → plan → execute → summarize          │
       │       Data · Coding · Experiment · ML agents         │
       │                                                       │
       ├── Tool Manager → Policy Engine → Gate → human review │
       │       sandboxed Python · notebooks · SQL · MLflow     │
       │       files · Git · reports · artifacts               │
       │                                                       │
       └── MIRA: preflight → audit → findings → verification  │
               reads evidence; never changes the workspace    │
                                                               │
  Local state: JSON/JSONL event log · artifact manifest · MLflow
```

THY and MIRA are deliberately independent runtime layers. THY performs work; MIRA observes and
assesses it. Neither a model output nor an audit finding can authorize an action by itself.

## Quick start

### Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- [just](https://github.com/casey/just) (recommended command runner)
- Docker, for normal confined code execution

### 1. Install the workspace

```bash
uv tool install rust-just
just setup
cp .env.example .env
```

On PowerShell, use `Copy-Item` in place of `cp`.

Configure `.env` with the credentials and model routes for your provider. Model identifiers are
configuration, not hard-coded product assumptions.

Every model route you configure must also appear in `THYMIRA_ALLOWED_MODEL_ROUTES`. Leaving that
allowlist empty denies every model call by design.

### 2. Prepare the bundled credit-risk example

```bash
cp data/german_credit.csv examples/credit-risk/data/applications.csv
just docker-build
```

The example declares the dataset, EU AI Act and credit-risk governance scope, project policy
overlay, and research boundary under `examples/credit-risk/.thymira/`.

### 3. Start the local API

```bash
uv run thymira-api --workspace examples/credit-risk
```

The API listens on `http://127.0.0.1:8000` by default. On startup it creates a local API token and
prints the path where it was written.

In another terminal, point the CLI at the API and provide that token:

```bash
export THYMIRA_API_URL=http://127.0.0.1:8000
export THYMIRA_API_TOKEN="$(cat examples/credit-risk/.thymira/runtime/api-token)"
```

Keep the token private. Never commit it, add it to a prompt, or share it in logs.

### 4. Start a Run

```bash
uv run thymira run \
  "Profile the registered dataset, train two baseline classifiers, compare them, and write an evidence-backed report."
```

Use the returned Run id with the commands below:

```bash
uv run thymira status RUN_ID
uv run thymira events RUN_ID
uv run thymira plan RUN_ID
uv run thymira audit RUN_ID
uv run thymira experiment RUN_ID
uv run thymira mlflow RUN_ID
```

A Run may ask bounded risk-interview questions or pause for review. Those are governance controls,
not failures:

```bash
uv run thymira answer RUN_ID "The work is offline research only; no model will make live credit decisions."
uv run thymira review RUN_ID
uv run thymira approve RUN_ID --note "Reviewed the recorded evidence and scope."
```

You can reject or cancel a Run explicitly:

```bash
uv run thymira reject RUN_ID --note "Insufficient evidence for this approval."
uv run thymira cancel RUN_ID
```

## Web console

Start the local browser client after starting the API:

```bash
uv run thymira-web
```

Open <http://127.0.0.1:8080> and paste the API token when prompted.

The console lets you:

- create and inspect Runs;
- answer risk-interview questions;
- review and resolve pending approvals;
- inspect plans, agents, tool calls, artifacts, audit controls, events, and model usage;
- edit declared project datasets and context through the API;
- configure allowed model routes for the local project.

The browser keeps the token only in its current session. The console is a thin API client and owns
no Run state.

## Evidence and safety model

Thymira treats execution evidence as a first-class product surface.

| Concern | Thymira approach |
|---|---|
| Model authorization | Models propose; deterministic policy and human review authorize. |
| Tool execution | Every tool call passes through the Tool Manager and Policy Engine. |
| Code isolation | Code-executing tools use the configured sandbox; safe modes fail closed. |
| Run history | Immutable hash-chained events are the authoritative record. |
| Secrets | Credentials are scrubbed at source; exports and traces are redacted. |
| Artifacts | Registered artifacts have SHA-256 digests and typed provenance. |
| Audit independence | MIRA reads evidence and recomputes checks; it never writes to the project workspace. |
| Human approval | An approval is scoped to the exact decision or tool call it authorizes. |
| Model governance | Routes, task tiers, usage, and provider responses are recorded as evidence. |

## Development

```bash
just check       # lint, types, architecture contracts, skills, roadmap, fast tests
just test-all    # complete test suite
just test-cov    # complete suite with the 90% coverage floor
just docker-build
just docker-run --help
```

Tests use scripted model providers by default. The normal quality suite does not require an API key
or send prompts to a model provider.

## Repository map

| Path | Purpose |
|---|---|
| `packages/schemas` | Shared typed contracts: Runs, events, artifacts, findings, approvals, and policy records |
| `packages/events` | Canonical JSON, hash chains, verification, redaction, and model-visible event surfaces |
| `runtime/core` | Runtime composition, lifecycle, checkpoints, Run state, usage, and control plane |
| `runtime/thy` | THY graph and Data, Coding, Experiment, and ML agents |
| `runtime/mira` | MIRA preflight, deterministic controls, audit agents, grounding, and verification |
| `runtime/tools` | Typed, policy-gated tools and sandboxed execution |
| `runtime/policies` | Deterministic rules, policy evaluation, Gate, and human-review workflow |
| `runtime/state` | Local JSON/JSONL Run state, artifact storage, settings, and MLflow support |
| `apps/api` | Authenticated local FastAPI boundary |
| `apps/web` | Local web console |
| `adapters/cli` | `thymira` command-line client |
| `examples/credit-risk` | Reference governed data-science project |
| `docs/` | Architecture, ADRs, contracts, governance material, and roadmap |

## Scope and current maturity

Thymira is currently a local-first runtime for controlled development and research workflows.

It includes local JSON/JSONL persistence, inline execution, local API credentials, container-based
tool confinement, and a local browser console. It is not yet a hosted multi-tenant service or a
distributed production platform with managed queues, managed persistence, organization-wide
identity, or cloud operations.

See the [roadmap](docs/roadmap/README.md), [architecture](docs/architecture/README.md), and
[governance quickstart](docs/governance/quickstart.md) for design and implementation detail.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[AGENTS.md](AGENTS.md) for repository architecture, quality gates, and invariants.

## License

Apache License 2.0. See [LICENSE](LICENSE).
