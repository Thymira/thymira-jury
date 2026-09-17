# Governance quickstart

This is the shortest honest path through Thymira's governance surface: run a data-science task, let
**MIRA** audit it, let the deterministic **Policy Engine** decide, and approve as a human when the
decision asks for it. It uses the [`examples/credit-risk`](../../examples/credit-risk) reference
project and mirrors, step for step, the end-to-end test at
`tests/thymira/test_demo_governance.py`.

The one idea to keep in mind: **the LLM proposes, code authorizes.** THY (the orchestrator) weaves a
solution; MIRA follows every thread and raises findings; a deterministic Policy Engine — never a
model — maps findings to `PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`; and a human approves what
the engine escalates. A finding is evidence, never an authorization.

## Before you start

- A Thymira workspace with a `.thymira/` directory. The credit-risk example ships one:
  `examples/credit-risk/.thymira/` (`config.yaml`, `policies.yaml`, `context.md`).
- Copy the demo dataset into place: `data/german_credit.csv` → `examples/credit-risk/data/
  applications.csv`. The project's `config.yaml` declares that dataset, and Inspect registers it
  before any agent runs -- a declared dataset that is missing fails the Run at intake, before
  Plan.
- The `thymira` CLI, talking to a running Thymira API (`adapters/cli` → `apps/api`). Clients own no
  state; the runtime keeps Run history in a local, append-only `events.jsonl`.

## The flow

### 1. Run

```text
thymira run "Analyze the dataset, build two baseline models, compare them and determine whether the best model is suitable for a credit-risk use case."
```

`thymira run` creates a Run and prints its `ID`. The composition graph executes THY (the analysis),
then MIRA (the audit), then a single `Gate.review_findings` call, then a terminal transition. Every
step is recorded as a redacted, hash-chained event; chain-of-thought is never persisted.

### 2. Audit

```text
thymira audit RUN
```

MIRA audits the Run against methodology and the project's governance frameworks (here EU AI Act and
credit-risk regulation). In the demo its credit-risk auditor raises one confident, high-severity
finding — approval rates diverge across a protected attribute. That finding is an input to the
Policy Engine, which returns the run-level decision the CLI shows:

```text
Decision: REQUIRE_HUMAN_REVIEW
  Reason: A high-severity finding with high confidence requires human review.
Next: thymira approve RUN (or thymira reject RUN)
```

The decision is deterministic: the same findings and the same policy always produce the same
decision, and the decision names the rule and the policy hash so it can be replayed. The Run is now
`WAITING_FOR_APPROVAL` — parked, with a human answer requested but not given.

Governance is data, not code. The base rule that fired here (`GOV-103`: a confident high-severity
finding needs a human) ships in `runtime/policies`. A project adds its own rules in
`.thymira/policies.yaml`; the credit-risk example layers `CRX-101/102/103` (for example: a confident
high-severity credit-risk finding always reaches a human, and evidence of train/test leakage blocks
the Run). Overlay rules are evaluated first, first match wins, and a `BLOCK` always takes
precedence.

### 3. Approve (or reject)

```text
thymira approve RUN --actor reviewer --note "Subgroup disparity reviewed."
```

Approval records a checkable `human.approval` event — the reviewer's authenticated identity, their
note, and the exact decision it answers. Only that recorded evidence, never a model's confidence,
unblocks a parked Run:

```text
Run approved

  ID          RUN
  Status      COMPLETED
```

`thymira reject RUN` instead records a rejection and moves the Run to `BLOCKED`. Either way the
answer is evidence on the log.

## What you are left with

- `events.jsonl` — the authoritative, append-only, hash-chained history. `run.json` is
  reconstructible from it.
- MIRA's findings and the Policy Engine's decision, each replayable from the log.
- The recorded human approval.
- An **assurance bundle** (`thymira.mira.build_assurance`) — one replayable record of the audit
  report, the policy decision, the evidence index, and the per-finding review state, pinned to the
  terminal event hash and the policy content hash.

The assurance bundle is deliberate about what it is not. It states what the deterministic controls
verified and the deterministic decision they informed; it is **evidence and a reproducible decision,
never a legal, regulatory or professional certification**, and it never asserts that a human review
took place without the recorded evidence to back it.

## The decision ladder

| Decision | Meaning | What happens |
|---|---|---|
| `PASS` | No finding a rule escalates. | The Run completes. |
| `WARNING` | A finding worth surfacing (e.g. a medium-severity concern). | The Run completes; the warning is on the log. |
| `REQUIRE_HUMAN_REVIEW` | A finding a human must judge. | The Run parks until a recorded approval or rejection. |
| `BLOCK` | A finding that invalidates the result (e.g. train/test leakage, a critical finding). | The Run is blocked; nothing runs after the block. |

All four are covered end to end, from CLI to Policy Engine, in
`tests/thymira/test_e2e_governance.py`; the credit-risk review path above is
`tests/thymira/test_demo_governance.py`.

## See also

- [`examples/credit-risk/README.md`](../../examples/credit-risk/README.md) — the demo project and
  its commands.
- [Architecture baseline](../architecture/e2e-baseline-v2.md) — the target design.
- [ADR-0008](../adr/0008-mira-audit-orchestrator-and-control-plane-authority.md) — `RunController`
  is the only persistent Run-transition writer; MIRA observes and proposes, and an audit `BLOCK` is
  not an authorization.
