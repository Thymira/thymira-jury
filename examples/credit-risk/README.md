# Example project: credit-risk

The reference **governance demo** for the MVP. It is an *Agent-aware Data Science project*:
Thymira reads `.thymira/` to know the domain, the experiment tracker, the default orchestrator and
the governance frameworks that apply.

- `.thymira/config.yaml` — project context (domain `credit_risk`; frameworks `EU_AI_ACT`,
  `CREDIT_RISK`; tracker MLflow; default orchestrator THY).
- `.thymira/policies.yaml` — the project's own finding rules (`CRX-101/102/103`), layered on the
  shipped governance defaults by `thymira.policies` and unit-tested in
  `tests/thymira/test_examples.py`.
- `.thymira/context.md` — the goal and expected flow THY and MIRA read.
- `data/` — the demo data set goes here as `applications.csv` (copy `data/german_credit.csv` from
  the repository root; it is not committed twice). `.thymira/config.yaml` declares it under
  `datasets:`, and THY's Inspect registers it into the Run before any agent works.

A full walkthrough — what each phase does and why the decision is deterministic — is the
[governance quickstart](../../docs/governance/quickstart.md).

## The governance demo path

The demo runs a credit-risk analysis, audits it, parks it for a human when MIRA raises a confident
high-severity credit-risk finding, and completes it once a human approval is recorded. **THY weaves
the solution; MIRA follows every thread**, the deterministic Policy Engine decides, and a human
approves — a model's finding is never the authorization.

```text
thymira run "Analyze the dataset, build two baseline models, compare them and determine whether the best model is suitable for a credit-risk use case."
thymira audit RUN
thymira approve RUN
```

Replace `RUN` with the identifier `thymira run` prints (the `ID` field).

### 1. Run, then audit — MIRA finds a credit-risk concern and the Policy Engine requires review

MIRA's credit-risk auditor raises a confident, high-severity finding (approval rates diverge across
a protected attribute). The deterministic Policy Engine — never a model — turns that finding into a
decision, and `thymira audit RUN` shows it (the deterministic control table is elided here):

```text
Audit for RUN

  Status  failed

  Control ID   Status              Severity  Detail
  ----------------------------------------------------------------------
  ...          ...                 ...       ...

Decision: REQUIRE_HUMAN_REVIEW
  Reason: A high-severity finding with high confidence requires human review.
Next: thymira approve RUN (or thymira reject RUN)
```

The Run is now parked (`Status: WAITING_FOR_APPROVAL`): the decision is recorded and a human answer
is requested, but not yet given. Nothing completes on a model's say-so.

### 2. Approve — a recorded human answer unblocks the Run

```text
thymira approve RUN --actor reviewer --note "Subgroup disparity reviewed."
```

```text
Run approved

  ID          RUN
  Status      COMPLETED
  ...
```

The approval is a checkable `human.approval` event carrying the reviewer's identity — the evidence
the assurance record needs before it can state that a human reviewed the finding.

### What the demo produces (and what it does not)

The run leaves behind **evidence and a deterministic decision, never a certification**: a
hash-chained `events.jsonl`, MIRA's credit-risk finding, the Policy Engine's `REQUIRE_HUMAN_REVIEW`
decision, the recorded human approval, and an **assurance bundle** (`thymira.mira.build_assurance`)
that pins the terminal event hash and the policy content hash so the decision can be replayed. The
bundle always carries the disclaimer that it is not a legal, regulatory or professional
certification and never claims a human review happened without recorded evidence.

The end-to-end demo is regression-locked:

```text
uv run pytest tests/thymira/test_demo_governance.py -m integration -q
```

It drives the real CLI → API → composition graph (THY → MIRA → `Gate.review_findings` → terminal)
with a `ScriptedProvider` in place of every model, so no network call is ever made. The commands
and the decision above are asserted against the real CLI output.

## Local MIRA closeout evidence

The first MIRA phase has a small, reproducible local E2E test for this example:

```text
uv run pytest tests/thymira/test_mira_e2e.py -q
```

It uses this project's credit-risk policy, local JSON/JSONL Run state, deterministic MIRA packs,
and `ScriptedProvider`. It does not invoke THY, automatically refresh context, call a real model,
or require PostgreSQL or any distributed service.
