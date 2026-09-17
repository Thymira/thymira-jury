# MVP roadmap — 3 weeks, 5 people

> **Superseded (2026-08-23).** For scope, task definitions and who builds what, use
> [`product-final.md`](product-final.md) and [`mvp-minimum.md`](mvp-minimum.md). This plan predates
> ADR-0005 and ADR-0006 and its task lists are no longer current; in particular its
> PostgreSQL-as-MVP storage items are superseded by ADR-0010, under which operational state for the
> MVP is local JSON/JSONL and PostgreSQL is FINAL work behind its own ADR. It is kept for the
> reasoning that still holds: the levels, what must not change between them, and the management
> rules. The **role definitions in section 5 remain authoritative** — the `P1`–`P5` labels used
> across this repository refer to them.

*English rendering of the team's MVP plan (2026-08-20).*

Yes. And my main recommendation is **not to try to implement the full architecture you describe during these 3 weeks**.

With 5 people and 15 working days you have roughly **75 person-days**. The goal of the first MVP must not be to prove that every component exists, but to prove a product thesis:

> **A Data Scientist can launch a Run from an external interface, THY can execute Data Science work reproducibly, log experiments and artifacts, and MIRA can audit the result with a governed decision.**

If you achieve that end-to-end, you have a defensible MVP. If you try to build Kubernetes + KEDA + RabbitMQ + VS Code + OpenCode + SDKs + MLflow + Langfuse + all the agents + a full sandbox + full governance, you will likely end up with 15 half-finished pieces and no convincing demonstration.

---

## Part 1 — The plan

### 1. MVP objective

#### The demo that must work by the end of the 3 weeks

A user runs:

```bash
thymira run "Analyze this dataset, build a baseline model and evaluate it"
```

The system:

```text
CLI
 │
 ▼
FastAPI
 │
 ▼
Run #123
 │
 ▼
THY
 │
 ├── Data Agent
 ├── Coding/Execution Agent
 └── Experiment Agent
       │
       ├── Python sandbox
       ├── MLflow
       └── Artifacts
              │
              ▼
          Run completed
              │
              ▼
            MIRA
              │
       ┌──────┴──────┐
       ▼             ▼
   Findings       Evidence
       │
       ▼
 Policy Engine
       │
       ▼
PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK
```

And afterwards:

```bash
thymira status 123
thymira audit 123
thymira experiment 123
```

can recover the same state.

**That is your MVP.**

---

### 2. What's in and what's NOT in

This decision is probably the most important one of the three weeks.

| Area | MVP | Later |
|---|---|---|
| FastAPI | ✅ | |
| CLI | ✅ | |
| Run/Session | ✅ | |
| PostgreSQL | ✅ | |
| LangGraph | ✅ | |
| THY | ✅ | |
| 2-3 specialized agents | ✅ | |
| PydanticAI | ✅ | |
| Tool Manager | ✅ | |
| Python execution | ✅ | |
| Basic sandbox | ✅ | Advanced distributed sandbox |
| MLflow | ✅ | |
| Artifact Store | ✅ local/MinIO | S3/Blob |
| MIRA | ✅ | |
| 2-3 audit agents | ✅ | More agents |
| Policy Engine | ✅ | |
| Audit findings | ✅ | |
| Event model | ✅ | Advanced event bus |
| OpenTelemetry | 🟡 basic | Full |
| Langfuse | 🟡 if integration is simple | |
| Web UI | 🟡 very small | Full console |
| VS Code | ❌ | Phase 2 |
| Cursor | ❌ | Phase 2 |
| OpenCode | ❌ | Phase 2 |
| Claude Code | ❌ | Phase 2 |
| Codex | ❌ | Phase 2 |
| Python SDK | ❌ | Phase 2 |
| TypeScript SDK | ❌ | Phase 2 |
| RabbitMQ | ❌ initially | Phase 2 |
| KEDA | ❌ | Phase 3 |
| Kubernetes | ❌ | Phase 2/3 |
| Terraform | ❌ | Phase 3 |
| Redis | ❌ initially | |
| R | ❌ | |
| Custom SQL engine | ❌ | |
| Multi-cloud | ❌ | |
| 10+ agents | ❌ | |
| Dedicated Vector DB | ❌ | |
| Advanced multi-tenancy | ❌ | |

#### Golden rule

**Anything not needed to execute and audit the first Run stays out.**

---

### 3. The vertical slice

I would structure the whole MVP around a single object:

#### `Run`

The Run must be the backbone of the product.

```text
Run
├── id
├── project_id
├── session_id
├── prompt
├── status
├── created_at
├── completed_at
├── git_commit
├── agents[]
├── tool_calls[]
├── experiments[]
├── artifacts[]
├── events[]
├── audit
└── policy_decision
```

And everything else connects to it.

##### Minimal states

```text
CREATED
   ↓
PLANNING
   ↓
RUNNING
   ↓
EXPERIMENTING
   ↓
AUDITING
   ↓
COMPLETED
```

With:

```text
FAILED
BLOCKED
WAITING_FOR_APPROVAL
```

---

### 4. Real MVP architecture

I would not use the full final architecture yet.

I would do this:

```text
                  CLI
                   │
                   ▼
              ┌─────────┐
              │ FastAPI │
              └────┬────┘
                   │
                   ▼
          ┌─────────────────┐
          │ Thymira Runtime │
          │                 │
          │   LangGraph     │
          └───────┬─────────┘
                  │
          ┌───────┴────────┐
          ▼                ▼
        THY              MIRA
          │                │
     PydanticAI       PydanticAI
          │                │
     ┌────┼────┐      ┌────┼────┐
     ▼    ▼    ▼      ▼    ▼    ▼
   Data  Code  Exp   Risk Method Policy
     │    │    │      │    │
     └────┼────┘      └────┼────┘
          │                │
          ▼                ▼
      Tool Manager     Audit Engine
          │
     ┌────┼─────┐
     ▼    ▼     ▼
 Python  Git   MLflow
 Sandbox
          │
          ▼
      Artifacts

          │
          ▼
      PostgreSQL
```

**Everything in a single deployment initially.**

Not because it is the final architecture, but because it is the correct architecture to validate the product.

---

### 5. The 5 team members

I would not organize the team as "frontend/backend/AI" only. I would organize it by **product ownership**.

#### Person 1 — Runtime / Tech Lead

Responsible for:

- FastAPI
- Run API
- Session
- LangGraph
- state
- schemas
- architecture
- integration between components
- code review

Is the **owner of the backbone**.

Must prevent every member from creating their own architecture.

---

#### Person 2 — THY / Agents

Responsible for:

- ThyGraph
- PydanticAI
- Data Agent
- Coding Agent
- Experiment Agent
- LiteLLM
- agent prompts
- delegation
- tool calling

Their goal is not to build 10 agents.

Their goal is that:

> **THY can complete a real Data Science workflow.**

---

#### Person 3 — Tools / Execution / MLflow

Responsible for:

- Tool Manager
- Python execution
- basic sandbox
- Git
- datasets
- artifacts
- MLflow
- experiment tracking

This person is critical.

An agent with no real execution capability proves nothing.

---

#### Person 4 — MIRA / Governance

Responsible for:

- MiraGraph
- Audit Agents
- findings
- evidence
- severity
- Policy Engine
- approval flow
- governance schemas

Must not try to implement the whole regulation.

Must create **one demonstrable governance case**.

For example:

```text
Model
 ↓
MIRA
 ↓
Risk Agent
 ↓
Methodology Agent
 ↓
Finding
 ↓
Policy Engine
 ↓
REQUIRE_HUMAN_REVIEW
```

---

#### Person 5 — CLI / UX / Integration / QA

Responsible for:

- CLI
- Run visualization
- status
- audit
- experiment
- eventually minimal Web
- E2E tests
- demo flow
- documentation
- integration

This person must also act as **Product/QA owner of the MVP**.

Their responsibility is that at the end someone can install it and say:

```bash
thymira run ...
```

and see it work.

---

### 6. Week 1 — Build the executable skeleton

#### Objective

By the end of week 1 this must exist:

```text
CLI
 ↓
FastAPI
 ↓
Run
 ↓
LangGraph
 ↓
THY
 ↓
Tool
 ↓
Python
 ↓
Result
 ↓
PostgreSQL
```

It doesn't have to be pretty.

It has to work.

##### Day 1 — Alignment and contracts

Whole team.

Define and freeze:

- `Run`
- `Session`
- `Agent`
- `ToolCall`
- `Artifact`
- `Experiment`
- `AuditFinding`
- `PolicyDecision`
- Event schema

Also:

```text
POST /runs
GET /runs/{id}
POST /runs/{id}/resume
GET /runs/{id}/events
GET /runs/{id}/audit
GET /runs/{id}/experiments
```

And CLI:

```bash
thymira run
thymira status
thymira audit
thymira experiment
```

##### Deliverable

**Contract v0.1 frozen.**

From this point nobody changes schemas unilaterally.

---

##### Days 2-3 — Runtime

Runtime owner:

```text
FastAPI
   ↓
RunService
   ↓
LangGraph
   ↓
THY
```

Must exist:

```python
run()
resume()
status()
events()
```

PostgreSQL storing the state.

---

##### Days 2-4 — THY

THY starts with only:

###### Data Agent

```text
inspect dataset
profile dataset
```

###### Coding/Execution Agent

```text
generate Python
execute Python
read result
```

###### Experiment Agent

```text
create experiment
log metrics
save artifact
```

No more.

---

##### Days 3-5 — Execution + MLflow

Implement:

```text
run_python()
read_file()
write_file()
git_diff()
mlflow_start_run()
mlflow_log_metric()
mlflow_log_artifact()
```

Everything goes through Tool Manager.

Even if the sandbox is initially simple.

---

##### Friday of week 1

**Mandatory internal demo.**

```bash
thymira run "Analyze iris.csv and train a baseline classifier"
```

Must achieve:

```text
Run #001

THY
 ├── Data Agent
 │    └── dataset inspected
 │
 ├── Coding Agent
 │    └── model.py executed
 │
 └── Experiment Agent
      └── MLflow run created

Artifacts
 ├── model.pkl
 ├── metrics.json
 └── analysis.md
```

If this does not work at the end of week 1, **Web, VS Code, RabbitMQ and Kubernetes are not started**.

---

### 7. Week 2 — Experimentation + Governance

Week 2 is where the product starts to differentiate itself from a coding agent.

#### Objective

```text
Run
 ↓
THY
 ↓
Experiment
 ↓
MLflow
 ↓
Artifacts
 ↓
MIRA
 ↓
Findings
 ↓
Policy Engine
```

---

##### Days 6-7 — Experiments

Implement the concept:

```text
Experiment
├── experiment_id
├── run_id
├── parameters
├── metrics
├── artifacts
├── model
└── status
```

THY must be able to run, for example:

```text
Experiment A
Logistic Regression

Experiment B
Random Forest

Experiment C
XGBoost
```

and log the results in MLflow.

You don't need real parallelism yet.

You can simulate the abstraction and run sequentially.

---

### 8. Days 8-9 — MIRA

MIRA receives:

```text
Run
├── prompt
├── code
├── dataset metadata
├── experiment results
├── model metadata
├── events
└── artifacts
```

And produces:

```json
{
  "finding": "...",
  "evidence": [...],
  "severity": "HIGH",
  "confidence": 0.91,
  "recommendation": "...",
  "decision": "REQUIRE_HUMAN_REVIEW"
}
```

##### Only 2-3 audit agents

I would choose:

1. **Methodology Agent**
2. **Risk Agent**
3. **Regulatory Agent**

No more.

---

### 9. Policy Engine

This point is strategic.

Do not let MIRA say directly:

> "BLOCK".

The architecture must demonstrate that you have understood the LLM/policy separation.

```text
MIRA
 ↓
Finding
 ↓
Policy Engine
 ↓
Decision
```

Example:

```text
severity = HIGH
confidence = 0.92
framework = CREDIT_RISK
human_review_required = true

                 ↓

      REQUIRE_HUMAN_REVIEW
```

This is conceptually much more powerful than an agent saying "BLOCK".

---

### 10. Day 10 — Human in the Loop

Must exist:

```bash
thymira audit 123
```

Result:

```text
AUDIT

Finding #1
HIGH
Potential methodology issue

Finding #2
MEDIUM
Insufficient validation evidence

Decision:
REQUIRE_HUMAN_REVIEW

Approve? [y/n]
```

And:

```bash
thymira approve 123
```

updates the Run.

This demonstrates real governance.

---

### 11. Week 3 — Integration, UX and Demo

Week 3 **is not for adding features**.

It is for turning the prototype into an MVP.

#### Objective

That an external person can use it.

---

### 12. Day 11 — E2E hardening

Run the complete workflow repeatedly:

```text
create project
    ↓
run
    ↓
THY
    ↓
dataset
    ↓
Python
    ↓
experiment
    ↓
MLflow
    ↓
artifact
    ↓
MIRA
    ↓
policy
    ↓
human approval
    ↓
complete
```

Automate it.

Must have at least:

- 1 happy path
- 1 failed execution
- 1 governance warning
- 1 human approval

---

### 13. Day 12 — Event API + Observability

Implement events:

```text
run.started
agent.started
agent.completed
tool.started
tool.completed
experiment.started
experiment.completed
artifact.created
audit.started
audit.finding
human.approval
run.completed
run.failed
```

And add basic OpenTelemetry.

Do not build a full observability system.

The goal is to be able to answer:

> "What did THY do?"

and:

> "Why did this Run end in REQUIRE_HUMAN_REVIEW?"

---

### 14. Day 13 — UX

Here I would choose **CLI + minimal Web**, not VS Code.

The Web only needs:

##### Runs

```text
Run #1842
Status: Completed

Prompt
Agents
Tools
Experiments
Artifacts
Audit
```

##### Audit

```text
PASS
WARNING
REQUIRE_HUMAN_REVIEW
BLOCK
```

##### Experiments

```text
Experiment
Metric
Model
Artifacts
MLflow Run
```

Do not build:

- editor
- complex Monaco
- sophisticated chat
- 25-page dashboard
- full user system.

---

### 15. Day 14 — Packaging + Developer Experience

A new person should be able to do:

```bash
git clone ...
docker compose up
thymira run "..."
```

And have the system working.

Create:

```text
README
Quickstart
Architecture
Example project
Demo dataset
```

And an example:

```text
examples/
└── credit-risk/
    ├── .thymira/
    ├── data/
    └── README.md
```

---

### 16. Day 15 — Demo / Freeze

**Feature freeze.**

Nothing new.

Only:

- bugs;
- performance;
- UX;
- documentation;
- stability;
- demo.

The demo should last approximately **10-15 minutes**.

---

### 17. The demo I would prepare

I would not use Iris.

I would prepare a small **credit risk** case, because it connects directly to your Data Science + Governance positioning.

For example:

```text
credit-risk/
├── data/
│   └── applications.csv
├── train.py
├── .thymira/
│   ├── config.yaml
│   ├── policies.yaml
│   └── context.md
```

The user says:

> "Analyze the dataset, build two baseline models, compare them and determine whether the best model is suitable for a credit-risk use case."

THY:

```text
1. Inspects dataset
2. Profiles missing values
3. Selects baseline strategies
4. Creates Experiment A
5. Creates Experiment B
6. Trains models
7. Logs metrics to MLflow
8. Creates artifacts
9. Compares models
10. Produces recommendation
```

Afterwards:

```text
MIRA
 ↓
Methodology Agent
Risk Agent
Regulatory Agent
 ↓
Findings
 ↓
Policy Engine
 ↓
REQUIRE_HUMAN_REVIEW
```

And the user:

```bash
thymira audit RUN-123

thymira approve RUN-123
```

Finally:

```text
RUN COMPLETED

Model: Random Forest
Experiment: exp_004
MLflow: ...
Artifacts: 7
Audit: APPROVED
Governance: PASS
```

**This demo tells your complete product story.**

---

### 18. Prioritised backlog

I would use this classification:

#### P0 — Must work

- [ ] Run API
- [ ] PostgreSQL state
- [ ] LangGraph
- [ ] THY
- [ ] Data Agent
- [ ] Coding/Execution Agent
- [ ] Experiment Agent
- [ ] Tool Manager
- [ ] Python execution
- [ ] Basic Git integration
- [ ] MLflow
- [ ] Artifact handling
- [ ] MIRA
- [ ] Methodology Agent
- [ ] Risk Agent
- [ ] Policy Engine
- [ ] Audit findings
- [ ] Human approval
- [ ] CLI
- [ ] E2E workflow

#### P1 — Nice to have

- [ ] Web Run viewer
- [ ] Event streaming
- [ ] Langfuse
- [ ] OpenTelemetry
- [ ] MinIO
- [ ] Background execution
- [ ] Resume Run

#### P2 — Out of MVP scope

- [ ] VS Code
- [ ] Cursor
- [ ] OpenCode
- [ ] Claude Code adapter
- [ ] Codex adapter
- [ ] Python SDK
- [ ] TypeScript SDK
- [ ] RabbitMQ
- [ ] KEDA
- [ ] Kubernetes
- [ ] Terraform
- [ ] Redis
- [ ] Multi-cloud
- [ ] R
- [ ] Multiple distributed sandboxes

---

### 19. MVP Definition of Done

I would not declare the MVP done because "the components are implemented."

It must meet these **10 criteria**:

| # | Criterion | Required |
|---|---|---|
| 1 | Create a Run via API/CLI | ✅ |
| 2 | THY can execute a DS workflow | ✅ |
| 3 | Can execute Python through Tool Manager | ✅ |
| 4 | Produces artifacts | ✅ |
| 5 | Logs experiments in MLflow | ✅ |
| 6 | State persists | ✅ |
| 7 | MIRA can audit the Run | ✅ |
| 8 | Policy Engine produces a decision | ✅ |
| 9 | Human can approve/reject | ✅ |
| 10 | A complete Run can be demonstrated E2E | ✅ |

If all 10 work, **you have an MVP**.

Even if you don't have VS Code yet.

---

### 20. Success metrics for these 3 weeks

I would not measure the MVP by lines of code or number of agents.

I would measure:

#### Product

**Time to First Run**

> How long does it take a new user to execute their first Run?

Target: **< 10 minutes** from local deployment.

#### Runtime

**Run completion rate**

> % of demo Runs that finish successfully.

Initial target: **>80%**.

#### Reproducibility

Same:

```text
dataset
code
config
parameters
```

→ must allow reproducing the experiment.

#### Governance

100% of Runs that require audit must produce:

```text
finding
evidence
policy decision
```

#### UX

The user should not have to know:

- LangGraph
- PydanticAI
- MLflow internals
- PostgreSQL
- MCP
- LiteLLM

**That is infrastructure, not product.**

---

### 21. The management rule I would impose

With a team of 5 people and three weeks, I would impose these rules:

##### Rule 1 — A single backlog

Not:

```text
AI backlog
Frontend backlog
Infrastructure backlog
Research backlog
```

A single backlog ordered by E2E impact.

##### Rule 2 — Daily integration

Do not wait until Friday to integrate.

Every day:

```text
main
 ↓
Run
 ↓
THY
 ↓
Tool
 ↓
MLflow
```

must keep working.

##### Rule 3 — No week-long branches

Small branches.

Small PRs.

Daily merge.

##### Rule 4 — No speculative architecture

If something is not needed for the demo, document it as:

> **Future architecture**

and do not implement it.

##### Rule 5 — One owner per component

There cannot be two people "responsible" for Runtime.

One person has ownership.

The others collaborate.

##### Rule 6 — Demo every Friday

Week 1 Friday:

**THY executes.**

Week 2 Friday:

**THY + MLflow + MIRA work.**

Week 3 Friday:

**An external user can use it.**

---

### 22. Roadmap after the MVP

Once the vertical slice is demonstrated, then yes, you would follow your full architecture.

##### MVP — 3 weeks

```text
CLI
 │
 ▼
Thymira
 │
 ├── THY
 │    ├── Data
 │    ├── Coding
 │    └── Experiment
 │
 ├── MLflow
 │
 └── MIRA
      ├── Risk
      └── Methodology
```

##### V0.2 — 4-6 weeks

```text
              Thymira
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
      CLI      VS Code     Web
       │         │         │
       └─────────┼─────────┘
                 │
              Agent API
                 │
          THY + MIRA
```

Add:

- VS Code
- Improved Web
- MCP
- background agents
- resume
- event streaming
- Langfuse
- more robust sandbox.

##### V0.3

```text
Adapters
├── VS Code
├── Cursor
├── Claude Code
├── Codex
└── OpenCode
```

Plus:

- SDK
- RabbitMQ
- workers
- distributed execution
- Kubernetes.

##### V1

Then yes:

```text
Multi-project
Multi-tenant
Cloud
Kubernetes
KEDA
S3/Blob
RBAC
Enterprise governance
Advanced audit
Policy-as-code
```

---

### 23. The most important decision

There is a very strong temptation in your design: **build the platform you designed first and look for the use case afterward.**

I would do exactly the opposite.

I would build:

> **an extremely good Data Science case that forces Thymira to exist.**

The first case should simultaneously demonstrate:

```text
DATA
 ↓
AGENT
 ↓
CODE
 ↓
EXPERIMENT
 ↓
MODEL
 ↓
ARTIFACT
 ↓
AUDIT
 ↓
POLICY
 ↓
HUMAN
```

Because that flow is your product.

Not:

> "We have LangGraph + PydanticAI + MCP + MLflow + Kubernetes."

But rather:

> **"Give me a Data Science project and Thymira can execute, experiment, log, explain and govern the entire process, regardless of which interface the scientist uses."**

That is the message I would use to lead the team over the next three weeks.

#### In one sentence, the objective of each week

**Week 1:**
> **THY can do Data Science.**

**Week 2:**
> **Thymira can demonstrate and govern what THY did.**

**Week 3:**
> **An external person can use and understand the whole process.**

And if at the end of the three weeks you have to sacrifice something, **sacrifice breadth of interfaces, distributed infrastructure and number of agents; never sacrifice the vertical slice Run → THY → Experiment → Artifact → MIRA → Policy.**

---

## Part 2 — Tasks per person and week

Yes. For a team of 5 people and 3 weeks, I would organize it as **a single E2E delivery train**, not as five parallel projects.

The rule is: **each week has an integrable objective**, and each person has tasks with explicit dependencies on the others.

### Team structure

| Person | Ownership | Main responsibility |
|---|---|---|
| **P1** | Runtime / Tech Lead | FastAPI, Run, state, LangGraph, contracts |
| **P2** | THY / Agents | ThyGraph, DS agents, prompts, delegation |
| **P3** | Tools / Execution / MLflow | Tool Manager, Python, Git, artifacts, MLflow |
| **P4** | MIRA / Governance | MiraGraph, audit agents, findings, Policy Engine |
| **P5** | CLI / UX / QA | CLI, integration, E2E, tests, demo, documentation |

One person can help another, but **ownership is not shared**.

---

### Week 1 — Get a real Run end to end

#### Week objective

By Friday:

```text
CLI
 ↓
API
 ↓
Run
 ↓
THY
 ↓
Tool
 ↓
Python
 ↓
Result
 ↓
PostgreSQL
```

We still don't need the full MIRA or Web.

---

#### P1 — Runtime / Tech Lead

##### Monday
Defines and freezes the contracts:

```text
Run
Session
Agent
Task
ToolCall
Artifact
Experiment
Event
AuditFinding
PolicyDecision
```

Creates the base repository:

```text
apps/api
runtime/core
runtime/thy
runtime/mira
runtime/agents
runtime/tools
runtime/state
packages/schemas
packages/events
adapters/cli
tests
docs
```

Defines:

```text
POST /runs
GET /runs/{id}
POST /runs/{id}/resume
GET /runs/{id}/events
```

##### Tuesday
Implements:

```text
RunService
SessionService
PostgreSQL repository
```

and the basic LangGraph state.

##### Wednesday
Integrates:

```text
FastAPI
   ↓
RunService
   ↓
LangGraph
```

Must be able to create a real Run.

##### Thursday
Integrates THY.

```text
API
 ↓
Run
 ↓
ThyGraph
```

##### Friday
Integrates everything with P3.

Objective:

```bash
thymira run "Analyze this dataset"
```

→ creates Run → executes → persists result.

##### Definition of Done P1

- Run persisted
- LangGraph working
- API working
- recoverable state
- Run tests
- shared contracts working

---

#### P2 — THY / Agents

##### Monday
Designs THY's contract:

```text
ThyInput
ThyState
ThyOutput
AgentTask
```

Defines the minimal graph:

```text
START
 ↓
Inspect
 ↓
Plan
 ↓
Execute
 ↓
Summarize
 ↓
END
```

##### Tuesday
Builds the **Data Agent**.

Must be able to:

- list files;
- read dataset;
- inspect columns;
- detect types;
- generate basic profiling.

##### Wednesday
Builds the **Coding/Execution Agent**.

Must be able to request:

```text
run_python
read_file
write_file
```

from P3's Tool Manager.

##### Thursday
Builds a minimal **Experiment Agent**.

Must be able to:

```text
create experiment
run experiment
return metrics
```

##### Friday
Integrates the three:

```text
THY
 ├── Data Agent
 ├── Coding Agent
 └── Experiment Agent
```

##### Definition of Done P2

Given:

> "Analyze dataset X and build a baseline"

THY must:

1. inspect the dataset;
2. generate a plan;
3. execute Python;
4. receive results;
5. produce a structured response.

---

#### P3 — Tools / Execution / MLflow

This person is probably the **most critical during week 1**, because they turn the agent into something capable of doing real work.

##### Monday
Defines the Tool API:

```text
Tool
ToolInput
ToolOutput
ToolPermission
```

Implements Tool Manager.

##### Tuesday
Implements:

```text
run_python()
read_file()
write_file()
list_files()
```

Initially can use a controlled local sandbox.

##### Wednesday
Adds Git:

```text
git_status()
git_diff()
git_commit()
```

We don't need automatic commits yet.

##### Thursday
Integrates basic MLflow:

```text
start_run
log_param
log_metric
log_artifact
end_run
```

##### Friday
Integrates with THY.

The flow must be:

```text
THY
 ↓
Tool Manager
 ↓
Python
 ↓
MLflow
 ↓
Artifact
 ↓
Result
```

##### Definition of Done P3

Must be able to run:

```python
run_python(...)
```

and produce:

```text
stdout
stderr
exit_code
metrics
artifacts
```

and optionally create an MLflow Run.

---

#### P4 — MIRA / Governance

In week 1 they **must not build the full MIRA**.

Their main job is to prepare the contract for week 2.

##### Monday
Defines:

```text
AuditFinding
Evidence
Severity
Confidence
Recommendation
PolicyDecision
```

##### Tuesday
Designs:

```text
AuditInput
AuditOutput
```

and how MIRA consumes a Run.

##### Wednesday
Builds a mock of:

```text
MiraGraph
```

that can receive:

```text
run_id
events
artifacts
experiment results
```

##### Thursday
Implements a first Policy Engine rule.

For example:

```text
HIGH severity
+
missing evidence
→ REQUIRE_HUMAN_REVIEW
```

##### Friday
Integrates reads from PostgreSQL and leaves the real pipeline prepared.

##### Definition of Done P4

Without needing a sophisticated LLM yet:

```text
Run
 ↓
Audit Input
 ↓
Finding
 ↓
Policy Decision
```

must exist as an executable contract.

---

#### P5 — CLI / QA / Integration

##### Monday
Implements:

```bash
thymira run
thymira status
```

##### Tuesday
Connects CLI to FastAPI.

##### Wednesday
Implements:

```bash
thymira runs
```

and structured output:

```text
Run ID
Status
Created
Prompt
```

##### Thursday
Starts E2E tests.

First test:

```text
create run
→ execute
→ completed
→ retrieve
```

##### Friday
Runs the first internal demo and logs all bugs.

Must produce a document:

```text
WEEK-1-BUGS.md
```

classified:

```text
P0 — blocks demo
P1 — important
P2 — cosmetic
```

---

#### Week 1 workflow

The actual order between people must be:

```text
P1
Contracts + Runtime
      │
      ├──────────────► P2
      │                THY
      │
      └──────────────► P3
                       Tools
                            │
                            ▼
                         MLflow
                            │
                            ▼
P2 ◄──────────────────── Tool Manager

P1 + P2 + P3
      │
      ▼
    P5
   E2E QA
```

Meanwhile:

```text
P4
Audit contracts
     │
     ▼
prepared for Week 2
```

##### What must NOT happen

P2 cannot invent a tools interface different from P3's.

P4 cannot create an Audit model incompatible with `Run`.

P5 does not wait until Friday to test.

**P5 tests the latest state of `main` daily.**

---

### Week 2 — Experiments + MIRA + Governance

#### Objective

By Friday:

```text
Run
 ↓
THY
 ↓
Experiment
 ↓
MLflow
 ↓
Artifacts
 ↓
MIRA
 ↓
Findings
 ↓
Policy
```

---

#### P1 — Runtime / Tech Lead

##### Monday
Extends the Run model:

```text
runs
tasks
tool_calls
artifacts
experiments
audit_events
```

##### Tuesday
Implements the internal event system:

```text
run.started
agent.started
agent.completed
tool.started
tool.completed
experiment.started
experiment.completed
artifact.created
```

##### Wednesday
Implements:

```text
resume(run_id)
```

The same Run must be able to continue.

##### Thursday
Integrates THY + MIRA as two independent subgraphs.

```text
Runtime
 ├── ThyGraph
 └── MiraGraph
```

##### Friday
Stabilizes contracts and does global code review.

---

#### P2 — THY / Agents

##### Monday
Makes the Experiment Agent use MLflow for real.

##### Tuesday
Implements two experiments.

For example:

```text
Experiment A → Logistic Regression
Experiment B → Random Forest
```

##### Wednesday
Adds comparison:

```text
compare_models()
```

##### Thursday
Makes THY produce a scientific recommendation:

```text
best_model
metrics
tradeoffs
limitations
```

##### Friday
Integrates artifacts:

```text
analysis.md
metrics.json
model.pkl
comparison.csv
```

##### Definition of Done

THY can produce a reproducible and comparable experiment.

---

#### P3 — Tools / Execution / MLflow

##### Monday
Completes MLflow integration:

```text
params
metrics
artifacts
model
```

##### Tuesday
Builds an abstract ArtifactStore:

```text
ArtifactStore
 └── LocalArtifactStore
```

Optionally:

```text
MinIOArtifactStore
```

##### Wednesday
Improves Python isolation:

```text
timeout
CPU limit
memory limit
filesystem restriction
non-root
```

No Kubernetes system needed.

##### Thursday
Adds:

```text
execution metadata
environment metadata
git commit
python version
dependencies
```

This is important for your reproducibility thesis.

##### Friday
Verifies:

```text
same input
+
same code
+
same parameters
=
reproducible experiment
```

---

#### P4 — MIRA / Governance

Here their critical week begins.

##### Monday
Implements MiraGraph.

```text
Run
 ↓
Audit Preparation
 ↓
Parallel Audit Agents
 ↓
Findings
```

##### Tuesday
Builds the **Methodology Agent**.

Must review:

- train/test methodology;
- leakage;
- validation;
- metrics;
- statistical limitations.

##### Wednesday
Builds the **Risk Agent**.

For the credit-risk case:

- bias/risk indicators;
- missing evidence;
- model limitations;
- risk-related findings.

##### Thursday
Adds a real Policy Engine:

```text
Finding(s)
 ↓
Rules
 ↓
Decision
```

Results:

```text
PASS
WARNING
REQUIRE_HUMAN_REVIEW
BLOCK
```

##### Friday
Integrates with P2's real Run.

##### Definition of Done

Given a real Run:

```text
MIRA
 ↓
2+ findings
 ↓
evidence
 ↓
severity
 ↓
Policy Decision
```

---

#### P5 — CLI / QA / Integration

##### Monday
Adds:

```bash
thymira experiment RUN_ID
```

##### Tuesday
Adds:

```bash
thymira audit RUN_ID
```

##### Wednesday
Adds:

```bash
thymira approve RUN_ID
thymira reject RUN_ID
```

##### Thursday
Builds a complete E2E test:

```text
run
→ experiment
→ artifact
→ audit
→ policy
→ approval
```

##### Friday
Runs the Week 2 demo.

Must be able to show the complete flow even if the interface is ugly.

---

#### Week 2 workflow

Here the order changes:

```text
P2 ──────┐
         │
         ▼
      Experiments
         │
         ▼
P3 ──► MLflow
         │
         ▼
      Artifacts
         │
         ▼
P1 ──► Run State
         │
         ▼
P4 ──► MIRA
         │
         ▼
    Policy Engine
         │
         ▼
P5 ──► CLI / E2E
```

And P1 keeps everything together.

---

### Week 3 — Product, stability and demo

#### Objective

No new architecture.

The objective is:

> **That someone who has not participated in development can run the system and understand what it just did.**

---

#### P1 — Runtime / Tech Lead

##### Monday
Hardening:

- errors;
- basic retries;
- timeouts;
- inconsistent states;
- recovery.

##### Tuesday
Implements a consumable event stream:

```text
GET /runs/{id}/events
```

##### Wednesday
Adds basic tracing with OpenTelemetry.

##### Thursday
Performance and reliability.

Test:

```text
10 consecutive Runs
```

##### Friday
Runtime freeze.

Only P0/P1 bugs.

---

#### P2 — THY / Agents

##### Monday
Improves THY's planning.

Must explain:

```text
Plan
1. inspect dataset
2. prepare data
3. train baseline
4. compare
5. report
```

##### Tuesday
Improves tool selection.

##### Wednesday
Handles errors:

```text
Python failure
 ↓
diagnose
 ↓
retry/fix
 ↓
continue
```

##### Thursday
Optimizes prompts and outputs.

##### Friday
No new features.

Only THY stability.

---

#### P3 — Tools / Execution / MLflow

##### Monday
Sandbox hardening.

##### Tuesday
Improves artifact handling.

##### Wednesday
Verifies end-to-end reproducibility.

##### Thursday
Prepares reset/cleanup:

```text
workspace
artifacts
MLflow
```

##### Friday
Freeze.

---

#### P4 — MIRA / Governance

##### Monday
Improves finding quality.

##### Tuesday
Ensures traceable evidence:

```text
Finding
 ↓
Evidence
 ↓
Run / Artifact / Event
```

This point is very important for the demo.

##### Wednesday
Completes human-in-the-loop.

```text
REQUIRE_HUMAN_REVIEW
 ↓
approval
 ↓
Run continues/completes
```

##### Thursday
Prepares cases:

```text
PASS
WARNING
REQUIRE_HUMAN_REVIEW
BLOCK
```

##### Friday
Freeze.

---

#### P5 — CLI / UX / QA

This person leads the last week.

##### Monday
Polishes CLI:

```bash
thymira run
thymira status
thymira audit
thymira experiment
thymira approve
```

##### Tuesday
Creates minimal Web UI if the team is on track.

Only:

```text
Runs
Run detail
Experiments
Audit
Artifacts
```

##### Wednesday
Documentation:

```text
README
Quickstart
Architecture
Example
Troubleshooting
```

##### Thursday
Full demo rehearsal.

Must be done by **someone who did not develop the component**.

##### Friday
Release candidate.

---

### Daily work order

I would impose this rhythm during the three weeks:

#### 09:00 — 15 minutes

Each person answers only:

```text
Yesterday:
Today:
Blocker:
```

And a single question:

> **What do I need from another person to be able to move forward today?**

---

#### During the day

I would not work in "isolated areas."

I would work by interfaces:

```text
P1 → P2
P1 → P3
P2 → P3
P1/P4 → P5
```

Every integration must happen as soon as it is available.

---

#### 17:00 — Integration checkpoint

Everyone updates `main`.

P5 runs:

```text
Smoke Test
```

If it fails:

> **nobody starts a new feature until the problem is identified.**

---

### Full dependency flow

This is the flow I would literally put on your board:

```text
                    P1
             Runtime / Contracts
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
         P2                   P3
        THY                 Tools
          │                   │
          └─────────┬─────────┘
                    ▼
                 Run E2E
                    │
                    ▼
                   P4
               MIRA / Audit
                    │
                    ▼
              Policy Engine
                    │
                    ▼
                   P5
              CLI / QA / UX
                    │
                    ▼
                 DEMO
```

But there is a second, very important dependency:

```text
P1
 │
 ├──── Contract
 │
 ├──── State
 │
 └──── Events
        │
        ├────► P2
        ├────► P3
        ├────► P4
        └────► P5
```

**P1 must function as the backbone, not as a bottleneck.**

---

### The board I would use

I would not have 100 tickets.

I would have approximately **35-45 tickets**, grouped like this:

```text
EPIC 01 — Runtime
EPIC 02 — THY
EPIC 03 — Tools & MLflow
EPIC 04 — MIRA & Governance
EPIC 05 — CLI / UX
EPIC 06 — E2E / Release
```

Each ticket has:

```text
Owner
Dependency
Definition of Done
Target day
```

Example:

| ID | Ticket | Owner | Dependency | Day |
|---|---|---|---|---|
| R01 | Run schema | P1 | — | D1 |
| R02 | PostgreSQL Run repository | P1 | R01 | D2 |
| S01 | THY state | P2 | R01 | D2 |
| T01 | Tool schema | P3 | R01 | D1 |
| T02 | run_python | P3 | T01 | D2 |
| S02 | Data Agent | P2 | T01 | D3 |
| S03 | Coding Agent | P2 | T02 | D3 |
| M01 | MLflow wrapper | P3 | T02 | D4 |
| R03 | ThyGraph integration | P1 | S02/S03 | D4 |
| C01 | CLI run | P5 | R03 | D4 |
| A01 | Audit schema | P4 | R01 | D2 |
| A02 | Methodology Agent | P4 | A01 | D7 |
| A03 | Risk Agent | P4 | A01 | D8 |
| A04 | Policy Engine | P4 | A02/A03 | D9 |
| E01 | E2E Run | P5 | P1/P2/P3 | D5 |
| E02 | E2E Audit | P5 | P4 | D10 |
| E03 | Approval flow | P5 | A04 | D10 |
| H01 | Hardening | Everyone | E02 | D11+ |
| H02 | Release | Everyone | H01 | D15 |

---

### And a golden rule for these 3 weeks

Each person must have **one main task**, not five.

During any given day:

```text
70% → main objective
20% → integration with others
10% → bugs / support
```

And the Tech Lead must be able to look at the board and answer immediately:

> **"What is the critical path for the E2E Run to work?"**

In your case it is:

```text
Contracts
   ↓
Runtime
   ↓
Tools
   ↓
THY
   ↓
Experiment
   ↓
MLflow
   ↓
Artifacts
   ↓
MIRA
   ↓
Policy
   ↓
Human Approval
   ↓
CLI / Demo
```

That is the **critical path**. Anything that does not help unblock that chain during these three weeks must go to the later backlog.

---

## Part 3 — What the result is

Exactly. **That should be the final result of the 3 weeks**, but with an important distinction:

> **It would not be the final product nor an enterprise-scale "production-ready" platform. It would be a functional MVP built on an architecture that already points toward the final product, so that you don't have to throw it away and redo it afterward.**

The key is that the MVP be a **thin vertical slice of the final architecture**, not a disposable prototype.

### What you should have by the end

```text
                         MVP — WEEK 3
                              │
                              ▼
                         Thymira Core
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
                THY                     MIRA
                  │                       │
           Data / Coding /          Methodology /
           Experiment              Risk / Audit
                  │                       │
                  └───────────┬───────────┘
                              ▼
                        Tool Manager
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  Python     Git       MLflow
                    │                   │
                    └────────┬──────────┘
                             ▼
                         Artifacts
                             │
                             ▼
                       PostgreSQL
                             │
                             ▼
                       Policy Engine
                             │
                             ▼
                    Human Approval
```

And all of it initially accessible via:

```text
CLI
 │
 ▼
Thymira API
 │
 ▼
Thymira Runtime
```

That would already be **a product**, not just a technical demo.

---

### But there is a fundamental difference

I would separate your result into three levels.

#### Level 1 — What works at the end of the 3 weeks

**Functional MVP.**

Must be able to do:

```text
User
 ↓
Run
 ↓
THY
 ↓
Data Science
 ↓
Experiment
 ↓
MLflow
 ↓
Artifacts
 ↓
MIRA
 ↓
Audit
 ↓
Policy
 ↓
Human approval
```

This demonstrates the value proposition.

---

#### Level 2 — What will already be architecturally prepared

Here is the important part of your question.

The components should have **stable interfaces**, even if their implementations are simple.

For example:

```text
ArtifactStore
    │
    ├── LocalArtifactStore       ← MVP
    ├── MinIOArtifactStore       ← next
    ├── S3ArtifactStore          ← future
    └── AzureBlobStore            ← future
```

The same for:

```text
ToolManager
    │
    └── MCP
         │
         ├── Python
         ├── SQL
         ├── Git
         ├── Data Sources
         └── ...
```

And:

```text
Agent API
    │
    ├── CLI
    ├── Web
    ├── VS Code
    ├── Cursor
    ├── Claude Code
    ├── Codex
    └── OpenCode
```

**You don't implement all of them now.**

But the core must not prevent implementing them later.

---

### Level 3 — Final product

The evolution would be roughly:

```text
                     TODAY
                       │
                       ▼
                ┌─────────────┐
                │     MVP     │
                │             │
                │ CLI         │
                │ THY         │
                │ MIRA        │
                │ MLflow      │
                │ Audit       │
                │ PostgreSQL  │
                └──────┬──────┘
                       │
                       ▼
                 V0.2 / V0.3
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
       VS Code       Web        MCP/SDK
          │            │            │
          └────────────┼────────────┘
                       ▼
                 Distributed
                   Runtime
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
      RabbitMQ      Workers      Sandbox
                       │
                       ▼
                  Kubernetes
                       │
                       ▼
                    KEDA
                       │
                       ▼
                Cloud / Managed
                       │
                       ▼
                  ENTERPRISE
```

---

### What matters: what should NOT change

If you do the MVP well, within 6-12 months the core concept should still be:

```text
Client
  ↓
Agent API
  ↓
Thymira Runtime
  ↓
THY / MIRA
  ↓
Tool Manager
  ↓
Execution / MLflow / Artifacts
  ↓
State / Governance
```

What changes is **the scale and the number of adapters**, not the conceptual core.

---

### For example: today vs future

#### Today

```text
FastAPI
    │
    ▼
LangGraph
    │
    ▼
THY
    │
    ▼
Python subprocess
```

#### Future

```text
FastAPI / SDK / MCP
        │
        ▼
   Thymira API
        │
        ▼
 Distributed Runtime
        │
   ┌────┼─────┐
   ▼    ▼     ▼
Worker Worker Worker
   │
Sandbox Manager
   │
Kubernetes
```

The **contract** can remain:

```python
run_python(...)
```

That is exactly what you want.

---

### The same with the agents

Now:

```text
THY
 ├── Data Agent
 ├── Coding Agent
 └── Experiment Agent
```

Later:

```text
THY
 ├── Data Agent
 ├── Coding Agent
 ├── Statistics Agent
 ├── ML Agent
 ├── Visualization Agent
 ├── Research Agent
 ├── Data Quality Agent
 └── ...
```

You don't need to change THY.

Its catalog of agents and tools simply grows.

---

### The same with MIRA

Now:

```text
MIRA
 ├── Methodology
 └── Risk
```

Later:

```text
MIRA
 ├── EU AI Act
 ├── Credit Risk
 ├── Model Risk
 ├── Methodology
 ├── Compliance
 ├── Regulatory Evidence
 └── ...
```

And the contract stays:

```text
Audit Input
      ↓
Audit Agents
      ↓
Findings
      ↓
Policy Engine
      ↓
Decision
```

---

### What I would NOT do

There are two possible mistakes.

#### Mistake 1 — Disposable prototype

Building:

```text
CLI
 ↓
script.py
 ↓
LLM
 ↓
print()
```

and then saying:

> "Now let's turn this into Thymira."

That is likely to force a massive rewrite.

---

#### Mistake 2 — Trying to build the final platform

Building now:

```text
Kubernetes
RabbitMQ
KEDA
Terraform
Redis
S3
MinIO
MCP
VSCode
Cursor
OpenCode
Claude Code
Codex
Web
SDK
10 agents
multi-tenancy
RBAC
...
```

With 5 people in 3 weeks.

That kills the MVP.

---

### The right strategy

I would call it:

#### **Production-shaped MVP**

It is not:

> "Production-ready."

But it is:

> **"Designed with the same conceptual boundaries production will have."**

For example:

```text
                    INTERFACES
                        │
                        ▼
                  Thymira API
                        │
                        ▼
                 DOMAIN / RUNTIME
                        │
          ┌─────────────┴─────────────┐
          ▼                           ▼
         THY                         MIRA
          │                           │
          └─────────────┬─────────────┘
                        ▼
                  TOOL MANAGER
                        │
             ┌──────────┼──────────┐
             ▼          ▼          ▼
          EXECUTION   MLflow    MCP
             │
             ▼
         ARTIFACTS
             │
             ▼
           STATE
```

That is what must be well defined.

---

### What does "ready to evolve" mean?

I would set **5 technical conditions**.

#### 1. Contracts

The main objects are typed:

```text
Run
Agent
Task
ToolCall
Artifact
Experiment
AuditFinding
PolicyDecision
Event
```

#### 2. Interfaces

You do not couple domain to infrastructure:

```text
ArtifactStore
ToolManager
LLMProvider
ExperimentTracker
ExecutionBackend
```

#### 3. API-first

The CLI contains no business logic.

```text
CLI
 ↓
API
 ↓
Runtime
```

#### 4. Event-driven from the model

Even if you don't have RabbitMQ initially:

```text
run.started
agent.started
tool.completed
experiment.completed
audit.finding
run.completed
```

these must already exist conceptually.

That way you can later introduce:

```text
RabbitMQ
Kafka
workers
event streaming
```

without redesigning the domain.

#### 5. Stateless where appropriate

Important state lives in:

```text
PostgreSQL
Artifact Store
MLflow
```

and not in:

```text
CLI
Web
VS Code
THY process
```

This is fundamental to being able to scale.

---

### And here is the definitive test

After the three weeks, you should be able to say:

> **"We have a functional MVP of Thymira capable of executing and governing a real Data Science workflow. The runtime, the contracts, the state, the agents, tools, experiment tracking and governance are separated through interfaces that let us replace infrastructure and onboard new clients and workers without changing the domain."**

And that would be a technically reasonable claim.

What you should **not** say yet is:

> "We have an enterprise cloud-native platform ready for production."

That will come later.

---

### My recommendation for the official objective

I would set as the **North Star** of these three weeks:

> **Build the smallest production-shaped Thymira that proves the complete Data Science → Experiment → Artifact → Audit → Governance loop.**

And the success criterion:

```text
                ┌───────────────────┐
                │   DATA SCIENTIST  │
                └─────────┬─────────┘
                          │
                          ▼
                    thymira run
                          │
                          ▼
                    ┌──────────┐
                    │   THY    │
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
             Data      Code     Experiment
              │          │          │
              └──────────┼──────────┘
                         ▼
                       MLflow
                         │
                      Artifacts
                         │
                         ▼
                     ┌───────┐
                     │ MIRA  │
                     └───┬───┘
                         │
                      Findings
                         │
                         ▼
                  Policy Engine
                         │
               ┌─────────┼─────────┐
               ▼         ▼         ▼
             PASS      WARNING    REVIEW
                         │
                         ▼
                       HUMAN
```

**If this circuit works, you have built the right core.**

After that, VS Code, Cursor, Claude Code, Codex, OpenCode, Web, SDKs, RabbitMQ, Kubernetes, KEDA, multi-cloud and more agents are **expansion layers**, not rebuilds of the product.

That is, in my opinion, the right way to make the 3 weeks produce **something demonstrable today and architecturally reusable tomorrow**.
