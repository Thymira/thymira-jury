# E2E Architecture Baseline v2 — Agentic Data Science Platform (Thymira)

*English rendering of the team's architecture baseline (authored in Spanish, 2026-08-20). This document is the target design; the repository skeleton under apps/, runtime/, adapters/, packages/, services/ and infrastructure/ follows its section 29.*

1. Product vision

The product is an Agentic Data Science Runtime / Thymira specialized in Data Science, capable of executing complex data science processes through multiple specialized agents, with reproducible experimentation and built-in governance.

The product is not an IDE.

The product is not tied to Cursor, VS Code, Claude Code, Codex, or OpenCode.

The product is the runtime, state, knowledge, tools, orchestration, experimentation, and governance that different interfaces can consume.

```
                         THYMIRA CORE
                              │
              ┌───────────────┼────────────────┐
              │               │                │
              ▼               ▼                ▼
          Agent API        Tool API         Event API
              │               │                │
      ┌───────┼───────┐       │       ┌────────┼────────┐
      ▼       ▼       ▼       ▼       ▼        ▼        ▼
   CLI     VS Code  OpenCode MCP    Web      SDK     External
            /Cursor Plugin                  Clients   Agents
```

The same "Run", "Session", "Experiment", "Artifact", and "Audit" must be viewable and continuable from any client.

Founding principle

«The client is replaceable. Thymira is the product.»

---

2. Two orchestration layers

Layer 1 — Production / Data Science

Main orchestrator: THY.

Coordinates:

- Data Agent
- Coding Agent
- Statistics Agent
- Experiment Agent
- ML Agent
- Data Quality Agent
- Visualization Agent
- Research Agent
- other specialized agents

THY can:

- analyze projects;
- understand code;
- inspect datasets;
- execute Python/R/SQL;
- modify code;
- run experiments;
- delegate tasks;
- work in parallel;
- use MCP;
- create artifacts;
- log experiments to MLflow;
- request review from MIRA.

Layer 2 — Audit / Governance

Independent orchestrator: MIRA.

Coordinates:

- EU AI Act Agent
- Credit Risk Agent
- Methodology Agent
- Risk Agent
- Regulatory Evidence Agent
- Compliance Agent
- Model Risk Agent

Consults the regulatory and methodological Knowledge Base.

Can produce:

PASS
WARNING
REQUIRE_HUMAN_REVIEW
BLOCK

Final enforcement decision belongs to the Policy Engine, not the LLM.

---

3. Thymira as a consumable platform

The user can work from:

Claude Code
Cursor
VS Code
Codex
OpenCode
terminal
Web UI
Python SDK
TypeScript SDK

without changing the core.

Architecture:

```
                    USER
                      │
       ┌──────────────┼────────────────┐
       ▼              ▼                ▼
   Claude Code      Cursor            Codex
       │              │                │
       ▼              ▼                ▼
   Adapter/MCP    VSCode Plugin    Adapter/MCP
       │              │                │
       └──────────────┼────────────────┘
                      ▼
                Thymira API
                      │
                      ▼
                Thymira Runtime
                      │
              ┌───────┴───────┐
              ▼               ▼
            THY             MIRA
```

The interface does not own the primary execution state.

The state belongs to Thymira.

---

4. Definitive E2E architecture

```
                           USER
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
   Claude Code          VS Code/Cursor         Codex
   OpenCode                  │                 Other IDEs
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                    Adapters / MCP / SDK
                             │
                             ▼
                  ┌──────────────────────┐
                  │     Thymira API      │
                  │      FastAPI         │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │   Thymira Runtime    │
                  │                      │
                  │      LangGraph       │
                  └──────────┬───────────┘
                             │
                 ┌───────────┴───────────┐
                 │                       │
                 ▼                       ▼
             THY GRAPH               MIRA GRAPH
                 │                       │
          PydanticAI Agents        PydanticAI Agents
                 │                       │
          ┌──────┼──────┐          ┌─────┼──────┐
          ▼      ▼      ▼          ▼     ▼      ▼
        Data    ML    Stats      Risk   Legal  Methodology
          │      │      │          │     │      │
          └──────┼──────┘          └─────┼──────┘
                 │                       │
                 └──────────┬────────────┘
                            ▼
                      Tool Manager
                            │
                    Permission Policy
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
             MCP         Sandbox        MLflow
              │             │             │
            Tools       Python/R/SQL   Experiments
                            │             │
                            └──────┬──────┘
                                   ▼
                              Artifacts
                                   │
                                   ▼
                            Object Storage
```

---

5. Interfaces

CLI

First official interface.

Examples:

thymira
thymira run "Analyze model degradation"
thymira plan "Build a baseline model"
thymira status
thymira runs
thymira audit
thymira experiment
thymira mlflow

The CLI does not contain THY/MIRA logic.

It is a client of the Runtime.

VS Code / Cursor

Lightweight extension.

Must provide:

- Chat with THY;
- Plan;
- Agent;
- selected context;
- file context;
- diffs;
- runs;
- experiments;
- findings from MIRA;
- approvals.

Do not build a custom editor.

Cursor is supported mainly through compatibility with the VS Code extension/API and, where needed, through dedicated integration.

OpenCode

Will be implemented as an optional adapter/plugin.

OpenCode can act as:

```
User
 ↓
OpenCode
 ↓
Thymira
 ↓
THY
```

or as a coding tool for THY:

```
THY
 ↓
Coding Agent
 ↓
OpenCode
 ↓
Git worktree
```

OpenCode is not an architectural dependency of Thymira.

Claude Code / Codex

Integration based on standard interfaces will be prioritized:

- MCP;
- CLI;
- SDK/API;
- specific adapters when they add value.

The domain will not be coupled to a provider's private APIs.

Web UI

Next.js + TypeScript.

Will be the advanced console for:

- Runs;
- Experiments;
- MLflow;
- Audit;
- Observability;
- Projects;
- Artifacts;
- Governance.

The web is one more interface, not the center of the product.

---

6. Agent API

Thymira must expose a common conceptual API for all clients:

run()
plan()
resume()
delegate()
inspect()
experiment()
audit()
approve()
reject()

Example:

```
Client
  ↓
run()
  ↓
Run #1842
  ↓
THY
```

A single Run can continue from another client:

```
Claude Code
    ↓
Run #1842
    ↓
VS Code
    ↓
Web UI
```

---

7. Tool API

Data Science capabilities will be tools consumable by external agents where appropriate.

Examples:

analyze_dataset
profile_dataset
query_sql
run_python
run_statistics
run_experiment
query_mlflow
compare_models
inspect_model
audit_model
search_regulation

MCP will be the primary standard for tool exposure.

---

8. Event API

All clients will be able to observe Runtime events:

run.started
agent.started
agent.completed
tool.started
tool.completed
experiment.started
experiment.completed
model.trained
artifact.created
audit.started
audit.finding
audit.block
human.approval
run.completed
run.failed

This allows building different clients without duplicating logic.

---

9. LangGraph

LangGraph is the orchestration engine.

Responsibilities:

- state;
- checkpoints;
- cycles;
- subgraphs;
- parallelism;
- delegation;
- human-in-the-loop;
- durable execution.

Main graphs:

ThyGraph
MiraGraph

LangGraph remains internal to the Runtime.

---

10. PydanticAI

Implements the individual agents.

```
LangGraph
    │
    ├── Data Agent ───── PydanticAI
    ├── Coding Agent ─── PydanticAI
    ├── ML Agent ─────── PydanticAI
    ├── Stats Agent ──── PydanticAI
    └── Audit Agents ─── PydanticAI
```

LangGraph decides:

«who, when, and in what order.»

PydanticAI decides:

«how each agent works.»

---

11. LLM Layer

LiteLLM:

```
Agent
 ↓
LiteLLM
 ↓
Provider
```

Providers:

- OpenAI
- Anthropic
- Azure OpenAI
- AWS Bedrock
- others
- local models

Agents will not be coupled to a single provider.

---

12. MCP and Tools

MCP will be the primary tools protocol.

```
MCP
├── filesystem
├── git
├── Python
├── R
├── SQL
├── browser/search
├── data sources
├── MLflow
├── statistics
├── experiments
└── governance
```

The Tool Manager sits in front:

```
Agent
 ↓
Tool Manager
 ↓
Permission Policy
 ↓
MCP
 ↓
Tool
```

---

13. MLflow

MLflow becomes part of the official stack.

Its responsibility is:

- experiment tracking;
- parameters;
- metrics;
- artifacts;
- model registry;
- model versions;
- experiment comparison;
- ML lineage.

It does not replace the local Run JSON/JSONL evidence or the Artifact Store.

```
THY
 │
 └── Experiment Agent
          │
          ▼
        MLflow
          │
     ┌────┼────┐
     ▼    ▼    ▼
 metrics models artifacts
```

The separation is:

Langfuse → What did the agents do?

MLflow   → What experiments/models did the process produce?

---

14. Data Science Context

Thymira will have knowledge of:

```
Git
+
Code
+
Datasets
+
Schemas
+
Data lineage
+
Experiments
+
MLflow
+
Models
+
Artifacts
+
Project policies
```

This is the main differentiator versus a generic coding agent.

---

15. Project Context

Each project can contain:

```
.thymira/
├── config.yaml
├── agents.yaml
├── policies.yaml
├── context.md
└── data.yaml
```

Example:

```
project:
  name: credit-risk
  domain: credit_risk

experiments:
  tracking: mlflow

agents:
  default: thy

governance:
  audit_required: true
  frameworks:
    - EU_AI_ACT
    - CREDIT_RISK
```

The project becomes an Agent-aware Data Science project.

---

16. Background Agents

Thymira will support background executions:

```
thymira run --background \
  "Compare five feature selection strategies"
```

Each execution can have:

```
Git worktree
+
Sandbox
+
MLflow run
+
Agent trace
+
Artifacts
+
Audit
```

This allows:

```
THY
├── Experiment A
├── Experiment B
├── Experiment C
└── Experiment D
```

in parallel.

---

17. Sandbox

Agents will only be able to execute code through the Sandbox Manager.

```
Agent
 ↓
Sandbox Manager
 ↓
Ephemeral Environment
 ↓
Python / R / SQL
 ↓
Artifacts
```

Security:

- non-root;
- no privileged;
- no Docker socket;
- isolated filesystem;
- CPU/RAM limits;
- process limits;
- timeout;
- filesystem quota;
- network policies.

---

18. RabbitMQ / Workers

RabbitMQ manages asynchronous work (FINAL; not in the MVP):

agent.tasks
audit.tasks
sandbox.tasks
human.approvals
dead-letter

Workers:

Data Worker
Coding Worker
Statistics Worker
Experiment Worker
Research Worker
Audit Worker
Sandbox Worker

KEDA scales according to workload.

---

19. State

Local MVP Run state:

events.jsonl (authoritative, append-only)
run.json (reconstructible projection)
immutable MIRA snapshots and evaluations
local artifact manifest and artifacts

---

20. Artifact Store

Abstract interface:

ArtifactStore

Implementations:

Local → MinIO
AWS   → S3
Azure → Blob

MLflow can use this storage for ML artifacts, while Thymira maintains its own artifact and lineage model.

---

21. Audit Architecture

```
Production Events
       │
       ├──────────────────┐
       ▼                  ▼
    ThyGraph           MiraGraph
                          │
                         MIRA
                          │
               ┌──────────┼──────────┐
               ▼          ▼          ▼
             Legal       Risk     Methodology
               │          │          │
               └──────────┼──────────┘
                          ▼
                    Audit Findings
                          │
                          ▼
                    Policy Engine
```

MIRA generates:

finding
evidence
severity
confidence
recommendation

Policy Engine decides:

PASS
WARNING
REQUIRE_HUMAN_REVIEW
BLOCK

---

22. Security

Least privilege.

Zero trust.

Permission separation:

THY
├── read dataset
├── execute Python
├── modify workspace
├── run experiments
└── propose model

MIRA
├── read execution events
├── read evidence
├── read regulatory KB
├── create findings
└── request intervention

Human
├── approve
├── reject
└── override where allowed

MIRA does not arbitrarily modify the workspace.

---

23. Observability

OpenTelemetry is the common layer.

```
                 OpenTelemetry
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Langfuse    Grafana      Datadog
          │           │           │
       AI/LLM       Infra         APM
       Agents       K8s
       Tools
```

Langfuse:

- LLM traces;
- agents;
- tools;
- tokens;
- costs;
- evaluations.

MLflow remains separate as ML tracking.

---

24. Infrastructure

Kubernetes is the execution abstraction (FINAL; not in the MVP):

API
THY Runtime
Audit Runtime
Workers
Sandbox Manager
KEDA
RabbitMQ
OpenTelemetry

Targets:

Local
Azure / AKS
AWS / EKS
GCP / GKE

Terraform manages infrastructure.

Docker packages services.

---

25. Frontend

Next.js + TypeScript.

```
apps/
└── web/
```

Uses:

- React;
- Tailwind;
- shadcn/ui;
- TanStack Query;
- TanStack Table;
- Monaco;
- Plotly/ECharts.

The UI shows:

Workspace
Runs
Agents
Experiments
MLflow
Artifacts
Audit
Evidence
Observability

But does not contain Thymira's core logic.

---

26. Developer Experience

The product must integrate into the Data Scientist's usual workflow:

```
Cursor / VS Code
+
Claude Code
+
Codex
+
OpenCode
+
Terminal
+
Git
+
Jupyter
```

The goal is not to force the user to change tools.

The user can keep using their favorite IDE.

```
Existing Workflow
       +
     Thymira
       ↓
Agentic Data Science Workflow
```

---

27. OpenCode / Cline / Aider / Continue

These projects are considered sources of inspiration and integration, not mandatory dependencies.

In particular:

- OpenCode → agent runtime, subagents, permissions, TUI, plugins;
- Aider → terminal-first workflow, Git, repo context;
- Continue → IDE + CLI + agents;
- Cline → agent UX, permissions, multi-agent/background workflows.

We will not build yet another generic coding agent if we can reuse existing capabilities through adapters, MCP, or integration.

---

28. Adapter Architecture

Thymira will have an explicit adapter layer:

```
packages/
└── adapters/
    ├── cli/
    ├── vscode/
    ├── opencode/
    ├── claude-code/
    ├── codex/
    └── generic/
```

Adapters translate:

```
External Agent / IDE
        ↓
Thymira Agent API
```

They do not contain THY or MIRA.

---

29. Updated monorepo

```
project/
│
├── apps/
│   ├── web/
│   └── api/
│
├── runtime/
│   ├── core/
│   ├── thy/
│   ├── mira/
│   ├── agents/
│   ├── tools/
│   ├── policies/
│   └── state/
│
├── adapters/
│   ├── cli/
│   ├── vscode/
│   ├── opencode/
│   ├── claude-code/
│   └── codex/
│
├── packages/
│   ├── schemas/
│   ├── events/
│   ├── sdk-python/
│   └── sdk-typescript/
│
├── services/
│   ├── workers/
│   └── sandbox-manager/
│
├── infrastructure/
│   ├── docker/
│   ├── kubernetes/
│   └── terraform/
│
├── tests/
└── docs/
```

---

30. Languages

Python

Core:

- FastAPI;
- LangGraph;
- PydanticAI;
- Pydantic;
- LiteLLM;
- MCP;
- MLflow;
- Data Science;
- workers;
- sandbox orchestration.

TypeScript

- Web;
- VS Code extension;
- SDK;
- integrations where appropriate.

Rust/Go

Not part of the MVP.

Could be used later for:

- high-performance CLI;
- terminal UI;
- process management;
- lightweight local daemon.

Domain logic remains in Python.

---

31. Definitive stack

```
                     USER INTERFACES
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
     Terminal          VS Code             Web
        │              Cursor
        │                 │
        ├──── Claude Code ┤
        ├──── Codex ──────┤
        └──── OpenCode ───┘
                 │
                 ▼
          Adapters / MCP
                 │
                 ▼
          Thymira Agent API
                 │
                 ▼
        ┌─────────────────────┐
        │   Thymira Runtime   │
        │                     │
        │     LangGraph       │
        │                     │
        │   ┌──────┴──────┐   │
        │   │             │   │
        │  THY           MIRA │
        │   │             │   │
        │ PydanticAI   PydanticAI
        └──────┬──────────────┘
               │
         Tool Manager
               │
        Permission Policy
               │
       ┌───────┼────────┐
       ▼       ▼        ▼
      MCP   Sandbox   MLflow
       │       │        │
     Tools   Python   Experiments
             R/SQL
               │
               ▼
           Artifacts
               │
          S3/Blob/MinIO

────────────────────────────────────────────

STATE
Local JSON/JSONL evidence and local artifacts

────────────────────────────────────────────

ASYNC EXECUTION (FINAL; not in the MVP)
RabbitMQ + Workers + KEDA

────────────────────────────────────────────

OBSERVABILITY
OpenTelemetry + Langfuse + Grafana
Datadog optional

────────────────────────────────────────────

INFRA
Docker + Kubernetes + Terraform
```

---

32. What we do NOT build

We will not build:

- another Cursor;
- another Claude Code;
- another Codex;
- another OpenCode;
- another editor;
- another LLM gateway;
- another ML tracking system;
- another unnecessary vector database;
- another observability framework;
- another MCP.

We reuse or integrate wherever it makes sense.

---

33. What IS our IP

```
                         OUR IP
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
 Data Science          Governance           Thymira Runtime
 Orchestration         & Audit              & Context
       │                    │                    │
       └────────────────────┼────────────────────┘
                            │
                   ┌────────┼────────┐
                   ▼        ▼        ▼
                Agents   Policies   State
                   │        │        │
                   └────────┼────────┘
                            ▼
                    Scientific Lineage
                            │
                    Experiments / MLflow
                            │
                            ▼
                     Reproducible Run
```

Our differentiator is not "having an agent that writes Python."

It is:

«Turning a Data Science project into a reproducible, experimental, auditable, and governed agentic environment, regardless of which IDE or agent the data scientist uses.»

---

34. Updated architectural principles

1. Cloud agnostic
2. Vendor neutral
3. Client agnostic
4. API-first
5. MCP-compatible
6. Agent interoperability
7. Event-driven
8. Durable agent state
9. Typed agent contracts
10. Data Science first
11. Experiment tracking first-class
12. MLflow integration
13. Least privilege
14. Sandboxed execution
15. Observable by default
16. Auditable by design
17. Human-in-the-loop
18. Open-source first
19. Managed services replaceable
20. No provider-specific domain logic

---

35. The definitive sentence

«Thymira is an Agentic Data Science Runtime that can be consumed from any IDE, coding agent, CLI, API, or web interface. THY coordinates the scientific work, MIRA provides governance and audit, MLflow provides the experiment and model registry, and the Runtime maintains context, state, tools, permissions, execution, and lineage.»

Therefore:

```
Claude Code
Cursor
Codex
OpenCode
VS Code
Terminal
Web
        │
        ▼
     THYMIRA
        │
   ┌────┴────┐
   ▼         ▼
  THY       MIRA
   │         │
   ▼         ▼
Science    Governance
   │         │
   └────┬────┘
        ▼
   Reproducible
   Data Science
```

This would be the architecture I would take as baseline from now on. The key strategic decision is that Thymira does not compete with existing interfaces: it turns them into entry points to our Runtime. This greatly reduces product risk while making our true differentiating value — Data Science + experimentation + governance + audit — independent of whether the user prefers Cursor, Claude Code, Codex, OpenCode, or any other IDE/agent tomorrow.
