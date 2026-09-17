# Roadmap — the final product

*The complete Thymira, with no gaps. This document is the **single catalogue**: every task
in the project is defined here exactly once, and `mvp-minimum.md` is a view over the
subset marked MVP. A task never says two different things in two places.*

**131 tasks — 82 MVP, 49 FINAL.**
**Status: 130 done, 1 partial, 0 todo.**

## How to read a task

Each task names the modules to create, an acceptance criterion that is a runnable test or
command, and — for MVP tasks — the **seam**: the contract that must exist up front so the
FINAL version is a drop-in replacement. A task is written to be handed to one engineer (or
one engineer's AI assistant) with no other context.

## The rule that governs both roadmaps

> **The MVP cuts implementations, never contracts or seams.**

Going from MVP to FINAL must require no refactor: no rewritten logic, no changed call
sites, no contract migration. That only holds if every MVP task defines the **full,
final-shape** type, protocol and event vocabulary, and puts a reduced implementation
behind it. Three rules follow, and they are not negotiable:

1. **Define the whole type now, even when the MVP only ever populates one value.** A frozen
   contract that gains a field later is a breaking change for every consumer.
2. **A reduced implementation goes *behind* a Protocol, never *instead of* one.** If the
   FINAL version swaps a backend, the MVP must already call through the seam that backend
   will implement.
3. **If a task cannot be split that way, it is FINAL, not MVP.** Shipping half a contract is
   worse than shipping none of it.

## Corrections to apply before the contract freezes

`packages/schemas` freezes on MVP day 1. Everything below is a **contract** decision: cheap now,
a breaking change for every consumer afterwards. Apply these before the freeze, not after.

**C-1 — Any future `Policy` field must land now.** `PolicyEngine.policy_sha256` hashes
`policy.to_json_dict()`, which is `model_dump(mode="json")` with no `exclude_defaults`. Adding
*any* field to `Policy` — even one defaulting to empty — adds a key to the serialised dict and
therefore **changes the hash of every existing policy**, invalidating every recorded
`policy_sha256` and breaking replay (control A16). So `Policy.budget_rules` and
`Policy.model_rules` must both be added pre-freeze, defaulted empty, even though the MVP evaluates
neither. Only the *decision logic* is FINAL.

**C-2 — Define the sandbox enums at full final width.** `SandboxMode` must carry all three of
`read_only | workspace_write | danger_full_access`, and `SandboxEnforcement` all three of
`full | partial | unusable` — even though the MVP only ever emits some of them. `enums.py` states
that adding a value is a contract change, and `unusable` is only produced by the FINAL fail-closed
container sandbox.

**C-3 — The MVP's local sandbox reports `PARTIAL`, never `FULL`.** `FULL` means confinement was
*actually complete* (ADR-0005 idea 6). An unconfined local subprocess is the weakest case, so
reporting `FULL` would be a false statement inside the evidence chain — and it would silently
disable the very MIRA control meant to catch it, making the FINAL change a logic rewrite instead
of a value change. `SandboxMode` records what was *requested*; `SandboxEnforcement` records what
was *achieved*.

**C-4 — Resolved by [ADR-0007](../adr/0007-compaction-is-atomic-no-bracket.md): no contract
change.** ADR-0006 decision 3 defined a single `context.compacted` event while decision 5 required
a bracket whose unterminated state is detectable after a crash — and a single event cannot be
unterminated. The bracket was inherited from a system whose compaction *mutates* its log; ours only
appends one event, so a compaction is atomic by construction and there is no intermediate state to
detect. `EventType` is unchanged, and the bracket half of `CMP-01` is out of scope.

**C-5 — Already satisfied by the tasks as written; do not weaken it.** An audit agent's output
must be `tuple[AuditFinding, ...]` — the frozen schema, which already carries `agent_id` and
`Evidence` — and deterministic findings (`audit_run`) plus agent findings must merge into a
**single** `Gate.review_findings` call. One call produces one aggregated `PolicyDecision`; one call
per agent produces several, and FINAL would have to rewire governance to fix it. `GOV-01` freezes
`AuditAgentOutput.findings` at that type; `MIRA-01` aggregates deterministic and agent findings in
node 3 and calls `Gate.review_findings` once in node 4, and its acceptance criterion asserts
**exactly one** `policy.decision`. Nothing needs changing — but an implementer who calls the Gate
per agent will still pass their own unit tests, which is why this stays on the list.

**C-6 — Resolved: both duplicate pairs are folded.** `GOV-02` is merged into **`TOOL-01`** and
`CTRL-SANDBOX` into **`TOOL-24`**; the fold reduced the catalogue to 128 tasks at that point. It
was not bookkeeping. The
two halves of the schema pair contradicted each other on the values of a *frozen enum* — `TOOL-01`
wrote `read-only` / `workspace-write` / `danger-full-access` where the shipped code and `GOV-02`
have `read_only` / `workspace_write` / `danger_full_access` — and `GOV-02` instructed the
implementer to report `SandboxEnforcement.FULL` in the MVP, which **C-3 forbids**. Whichever task
was picked up second would have overwritten the first. The control pair split a single check
across two tiers with two different severity ladders; the merged ladder is recorded in `TOOL-24`.
Two tasks owning one frozen field is how a contract diverges.

**C-7 — `A1`–`A18` is a closed namespace; an inherited id keeps its inherited meaning.** The
control ids come from the thesis meta-auditor and each already means something specific
(`docs/legacy/trazabilidad.md` lines 101–118; ADR-0002). They are not free labels, because a
*shipped* policy already speaks that vocabulary: `credit_risk.yaml`'s `CR-101` maps
`[A11, A12, A14, LEAKAGE-001]` to **`BLOCK`** with the reason *"evidence of train/test leakage"*,
and `FindingRule.matches` matches on `control_id` alone. An id that changes meaning silently
changes what that rule blocks, and puts a false statement in the evidence chain as the reason.
`TOOL-24` already hit this once and now uses `A19` (see its **Control id** note).

**The rule: a new check takes an id at `A19` or above; an inherited id keeps its inherited
meaning; a compound control is split rather than widened.**

Six ids disagreed with that rule. The **document half is now reconciled** — the tasks below carry
the corrected ids and a **Control ids** note saying why, so nobody renumbers them back:

| Id | Inherited meaning it now carries | The check it used to carry, re-homed |
|---|---|---|
| `A11` | train/test overlap, duplicates, gaps, invalid indices — **split integrity** | leakage indicators → **`A20`** |
| `A12` | lineage across split → candidates → selection → model → evaluation | *(was split integrity, which is `A11`'s)* — **reserved, nobody builds it** |
| `A13` | model selection that does not match an independent recomputation, or whose strategy cannot be verified | fold-local CV → **`A21`** — **reserved, nobody builds it** |
| `A14` | early test access, wrong partition, access outside the authorised task — **test-once**, and nothing else | the trivial-baseline half → **`A22`** |
| `A18` | report facts that do not match the source artifacts | reproducibility metadata → **`A23`** — **reserved, nobody builds it** |
| `A24` | *(new)* routing floor | was filed as `A-ROUTE`, outside the numeric scheme |

`A14` was the sharpest: a leakage-free model that merely skipped a baseline comparison raised
`A14`, `CR-101` has no `min_severity`, so a `LOW` finding **blocked the run recording "evidence of
train/test leakage" as the reason**. Splitting `A22` out removes it.

**`A12`, `A13` and `A18` now have owners.** Reserving a number is not the same as keeping its
meaning: an inherited control with no task is how a meaning disappears without anyone deciding to
drop it — this same defect in reverse. The three are filed as `CTRL-LINEAGE`, `CTRL-SELECTION` and
`CTRL-REPORT` (FINAL, P4, behind `MIRA-01`), so each is a decision someone can make rather than a
gap someone has to notice.

**C-8 — Resolved: `RunController` is the sole transition writer; ordinary progress is not
policy-gated.** Reconciling `dev-auditor`'s governance model (ADR-0008, formerly ADR-0005) with
this catalogue's `RA-CORE-02`/`RA-STATE-03` surfaced a real gap neither branch had alone:
`RunService.advance()`, as specified, would call `RunRepository.save(run.with_status(...))`
directly — no `PolicyDecision`, no authorization context, and no single writer. Confirmed while
reconciling `runtime/tools` (ported from `main`) against `dev-auditor`'s control plane on
`integration/reconcile-main-dev-auditor`: `RunController.transition()` already
enforces exact, single-use authorization for the five bounded control-plane actions
(`request_information`, `pause_run`, `resume_run`, `request_approval`, `review_findings`), but has
no path for a Run's own forward progress (`start`, `begin_execution`, `begin_audit`, `complete`,
…) — and it should not gain one shaped like a policy check, because nobody "requests permission"
to move from `PLANNING` to `EXECUTING`. The fix is not to gate ordinary progress; it is to make
sure only one component ever writes it. `RunController` needs a second, ungated method (`advance`
or similar) that applies `apply_transition` and persists through the same single-writer path as
`transition()`, so `RunService` calls `RunController` for **every** status change — routine or
governance-gated — and never `RunRepository.save()` directly. `RunRepository`/`UnitOfWork` are not
lifecycle writers; `LocalRunStore` (ADR-0010, formerly ADR-0007) is the persistence layer beneath
both paths. `RA-CORE-02` and `RA-STATE-03` now implement this
correction: `RunService` routes lifecycle changes through `RunController`, while `LocalRunStore`
remains the authoritative local persistence boundary. The repository protocols and UnitOfWork
remain available as tested backend/conformance seams for the later PostgreSQL implementation.

**C-9 — Resolved: `ToolManager` never trusts a synchronous approval for execution.** `Gate`'s
older `check_action`/`check_capability`/`review_findings` path records a `human.approval` event
when its built-in `approver` answers, but — unlike the newer `check_intent` → `AuthorizationContext`
→ `request_approval` path — it never returns anything a caller can check; `allows_execution`
(`thymira.policies.engine`) only trusts a separately matched `Approval`, by design (see
`tests/thymira/test_policies.py::test_gate_rejection_pending_and_block`). `runtime/tools/manager.py`,
as ported from `main`, read a `.approved` field off `PolicyDecision` that Contract 0.3's
`PolicyDecision` does not have — `main` embedded the human resolution in the decision (Contract
0.2); Contract 0.3 (ADR-0008) deliberately separates them. Fixed in the same integration branch:
`ToolManager.execute` now calls `allows_execution(decision)` with no approval object, so a tool
call escalated to `REQUIRE_HUMAN_REVIEW` is denied synchronously regardless of what the approver
answers — the answer is still recorded as evidence, never as authority. A tool call that
genuinely needs human sign-off before running is out of scope until `HITL-01` gives `ToolManager`
an asynchronous, checkable `Approval` path; until then, "deny and record" is the correct fail-safe
behavior, not a placeholder bug. `tests/thymira/test_tools.py`'s approval pair was rewritten to
assert this (`test_a_synchronous_approval_still_never_runs_a_review_gated_tool`).
`runtime/tools` intentionally records `Experiment` as a full `EXPERIMENT_COMPLETED` event payload
(`thymira.events`/`LocalRunStore`'s model) instead of using a `RecordRepository` for this path;
the repository protocols remain stable seams for other callers and the later PostgreSQL backend.

**C-10 — Resolved and implemented: `GOV-03`'s frozen fields, ported from `main`.** `main` had
already done the `GOV-03` work this catalogue marked `partial` — `BudgetRule`, `ModelRule`, and
the two matching fields on `Policy` — but `runtime/policies` was not adopted wholesale from
`main` (`dev-auditor`'s `Gate`/`PolicyEngine`/`Approval` model is the one this catalogue keeps;
see C-8/C-9). Porting `runtime/policies` in full would have re-introduced Contract 0.2's
`PolicyDecision.approved`, which C-9 just removed. Ported only the two frozen record types and
fields instead, onto `dev-auditor`'s `Policy`: `BudgetRule` (`POL-01`'s frozen shape) and
`ModelRule` (`POL-02`'s), both with `decision: Decision`, and `Policy.budget_rules`/`.model_rules`
added as empty-defaulted tuples, threaded through `Policy.merged_with()` overlay-first like every
other rule list. `GOV-03` is **done**: `tests/thymira/test_policies.py` pins
`policy_sha256(load_default_policy("base"))` against future silent field additions (the exact
regression `GOV-03`'s acceptance criterion asked for) and proves the fields overlay and
participate in the hash. `POL-01`/`POL-02` remain FINAL — only the deciding logic is missing, not
the frozen shape it will read.

**C-11 — Resolved: `RiskProfile` and `RiskAssessment` stay two types, but `RISK-01` derives from
`RiskAssessment` rather than reclassifying from nothing.** `RISK-01` (`thymira.agents.risk`) and
MIRA's already-built `BaseRiskEvaluator` (`thymira.mira.preflight`) turned out to solve
adjacent-sounding problems with near-identical fields (`risk_level`, `activity_category`,
`confidence`, `missing_information`) under different names — `RiskProfile`
(`thymira.policies.models`) and `RiskAssessment` (`thymira.schemas.risk`) — which is exactly the
"duplicated concept across members" the initial audit warned against. They are not the same
concept, though: `RiskAssessment` is versioned, evidence-backed, MIRA-preflight evidence for one
`ActivityProfile` version — computed once per material fact change, never per call.
`RiskProfile` is an ephemeral input to one `Gate.decide_capability` check for one tool call —
never versioned, never persisted on its own. Collapsing them into one type would force a
capability check to carry MIRA's evidence machinery, or force MIRA's versioned evidence to be
recomputed on every tool call. **Decision:** keep both types, but `RISK-01`'s `classify_risk()`
must take the Run's current `RiskAssessment` (when one exists for its `ActivityProfile`) as an
input and use its `risk_level` as a floor — `RiskProfile.risk_level` may only be escalated by the
eleven deterministic override rules `RISK-01` already specifies, never set below the activity's
assessed inherent risk. `RISK-01`'s deliverable and acceptance criterion need a line added for
this before implementation starts; the override rules themselves are unaffected.

**C-12 — Resolved: `MIRA-01`'s node order already matches what `MiraAuditOrchestrator` builds; the
task text needs updating, not the code.** `MIRA-01` was written before `dev-auditor`'s preflight
(`thymira.mira.preflight`: inherent risk, pack applicability, evidence controls) existed, so its
described sequence — deterministic controls → agent fan-out → Gate — omits a step that already
runs first in practice. Read `runtime/mira/src/thymira/mira/orchestrator.py::MiraAuditOrchestrator.audit`:
it already evaluates risk and pack bindings/controls *before* calling the deterministic `audit_run`
checks, then deduplicates every finding together (see C-13). The correct node sequence for the
`MiraGraph` `MIRA-01` builds is **prep → preflight (risk, applicability, evidence controls;
already built) → deterministic controls (already built) → agent fan-out (`MIRA-02`, not yet
built) → aggregation → Gate**. Update `MIRA-01`'s deliverable text to name preflight as the first
audit step before implementing it as a LangGraph graph; nothing in the preflight or control code
needs to change to fit this order, because it already runs that way.

**C-13 — Resolved: `AuditAgentOutput.findings` (`GOV-01`) merges through the deduplication
`MiraAuditOrchestrator` already has, not a new mechanism.** Neither `GOV-01` nor `MIRA-01`
describes how LLM-agent findings and preflight/deterministic findings become one list before
reaching `Gate.review_findings`. `orchestrator.py::_deduplicate_findings` already merges three
sources (risk findings, control-evaluation findings, deterministic-check findings) into one
tuple, keyed on `(control_id, finding text, evidence tuple)` so the same underlying observation
from two sources collapses to one `AuditFinding`. Once `MIRA-02`'s agent runner exists,
`AuditAgentOutput.findings` is a fourth source into that same call — not a parallel merge step,
and not a second `Gate.review_findings` call (`GOV-01`'s `AuditAgentSpec.framework` already gives
each finding a `Framework`, so no information is lost by merging before the Gate rather than
after). `MIRA-01`'s deliverable text should name this explicitly when it is rewritten under C-12.

**C-14 — Resolved: `MiraGraph` gets an `EventLog`, not `LocalRunStore`; its artifact store stays
read-only and its audit-agent runner is injected.** Static inspection and
`tests/thymira/test_mira_e2e.py` establish two different existing boundaries that `MIRA-01`'s
old `build_mira_graph(specs, store, gate)` signature conflated. `MiraAuditOrchestrator.audit()`
is a pure transformation over `MiraAuditSnapshot`: its optional `ArtifactStore` is read only by
the deterministic controls, it returns every assessment/binding/evaluation/finding/report/intent,
and the E2E caller currently persists those records. Separately, `Gate.review_findings()` appends
`policy.decision` (and `audit.block` on `BLOCK`) through its injected `EventLog`. The event types
for `audit.started`, preflight evidence, `audit.finding`, and `audit.completed` already exist, but
no MIRA append seam owns them. Reaching into `LocalRunStore` from graph nodes would couple MIRA to
the MVP backend and make the ambiguous `store` parameter both an evidence writer and an artifact
reader.

**Decision:** keep `MiraAuditOrchestrator` pure, and make the graph wrapper's side effects narrow
and explicit. `build_mira_graph` takes an `EventLog` for MIRA lifecycle/evidence events and an
optional read-only `ArtifactStore`; production composition in `thymira.core` supplies
`RunEventLog(LocalRunStore, run_id)`. The supplied `Gate` must target that same `run_id` and remains
the only writer of policy/approval/block events. Neither MIRA component imports or receives
`LocalRunStore`, calls `RunController`, or appends `run.transitioned`, so C-8's exclusive Run-state
writer is unchanged. The graph also receives the `MIRA-02` runner as a MIRA-owned callable rather
than importing `thymira.agents` or `thymira.tools`: in the current bounded environment even
`import thymira.agents.llm.routing` executes the eager package chain into `thymira.tools` and
fails on unavailable scientific dependencies, while `import thymira.mira` succeeds. Core or the
future full runtime may inject the real runner; graph tests inject a scripted one without widening
MIRA's runtime import surface.

**C-15 — Resolved: THY's plan checkpoint denies a review-gated plan synchronously, exactly like a
tool call.** Reconciling `main`'s `plan_node` (`THY-11`) with `dev-auditor`'s Contract 0.3 surfaced
a gap neither branch had alone: `main`'s node authorized `plan.proposed` through
`PolicyDecision.approved`, the field Contract 0.3 removed (C-9), and `ActionKind` is a closed enum
with no plan value, so the newer `Gate.check_intent` → `AuthorizationContext` → `Approval` path
cannot carry a plan without a contract change. The resolution kept in the merge reuses the one
authorization rule already in the codebase rather than adding a second beside it: `plan_node`
(`runtime/thy/src/thymira/thy/nodes/plan.py`) calls `gate.check_action(subject_kind="run",
subject_id=..., action_type="plan.proposed", payload=..., summary="plan.proposed")` — the engine
decides and the `Gate` records `policy.decision` → `human.approval_requested` → `human.approval`
exactly as the older path always did — then trusts the decision through
`thymira.policies.allows_execution(decision)` with no approval object, precisely as `ToolManager`
does for a tool call (C-9). `allows_execution` returns `True` only for `PASS`/`WARNING`, or for
`REQUIRE_HUMAN_REVIEW` with a separately matched `Approval` the plan node never has, so under the
shipped default policy (no `ActionRule` for `plan.proposed`, hence `REQUIRE_HUMAN_REVIEW`) the graph
halts at Plan regardless of what the `Gate`'s synchronous approver answers — the answer is still
recorded as a `human.approval` event, evidence, never authority. This is **not** a weaker rule than
C-9's tool path but the same one: a plan is a proposal with no effect of its own, yet it reuses the
single `allows_execution` gate rather than trusting a synchronous yes, and every tool the plan later
runs is gated independently through `ToolManager` + `allows_execution` too. A plan that genuinely
needs human sign-off before Execute is out of scope until `HITL-01` gives the plan checkpoint the
same asynchronous, checkable `Approval` path the tool one waits for; until then, or until a project
policy passes `plan.proposed` with a deliberate `ActionRule` (a recorded policy decision, not a
model's or an approver's word), "deny, record and halt" is the correct fail-safe, not a placeholder
bug. `tests/thymira/test_thy_plan.py` asserts this
(`test_a_synchronous_approval_never_advances_a_review_gated_plan`).

**C-16 — Resolved: `MIRA-02` produces candidate findings through an explicit execution context;
the graph owns their persistence and authorization path.** The previous runner signature named a
`provider_factory` and optional evidence reader but supplied no event log, actor, agent/task
identity, or Tool Manager context, even though its acceptance criterion required model, agent, and
tool events. It also asked the runner to append `audit.finding`, which would duplicate the final
events that `MIRA-01` already writes after combining and deduplicating every finding source
(C-13). The corrected task adds a MIRA-owned, in-process `AuditAgentContext`; keeps `GOV-01`'s
frozen persisted contracts unchanged; and makes the runner return candidate findings only.
`MIRA-01` remains the single merge, persistence, and Gate boundary.

The model-visible evidence boundary is now explicit too: event payloads come only from
`current_surface(input.events)` (ADR-0006), while the deterministic `AuditReport` is a curated
summary. A read-only evidence reader may add only hash-pinned artifact excerpts cited by an
`Evidence` record already present in that report, under redaction and size limits. The runner
reuses `routed_model` and the Tool Manager unchanged so callable tools and structured output share
the existing execution loop; it neither modifies P2/P3-owned code nor invents a second gateway.
`agent.started` opens the lifecycle before the first request, while `routed_model` continues to
record one `model.selected` immediately before every provider call. A24 therefore audits selections
before effects and completion, not before the non-effectful lifecycle opener. These lifecycle,
evidence, tool, normalization, and failure responsibilities make `MIRA-02` an **L**, not an M.

**C-17 — Resolved: `MIRA-01`'s standalone `MiraGraph` LangGraph wrapper is retired; `MiraAuditFlow`
+ `MiraSubgraph` is the one production path (2026-09-04).** Production composition never called
`build_mira_graph`/`audit_run_graph` (`thymira.mira.graph`): `runtime/core/graph/adapters.py`'s
`MiraSubgraph` has always invoked `MiraAuditFlow` directly from Core's `preflight` and `mira` graph
nodes, leaving its own `Gate.review_findings()` call to Core's `review` node so a Run never
receives two findings decisions. The standalone wrapper duplicated that same sequence behind its
own `Gate` call and its own `graph_definition_hash()`, a second function answering "what graph
executed this Run" distinct from `MiraAuditFlow`'s `canonical_graph_definition_hash()` — two
functions that could disagree on the provenance recorded in `run.started`. `build_mira_graph`'s
`compile()` also carried no checkpointer, so the wrapper offered no LangGraph state history either;
nothing about MIRA's replay guarantees came from it. `thymira/mira/graph.py`, `MiraGraphOutput`,
`build_mira_graph`, and `audit_run_graph` are removed; `tests/thymira/test_mira_graph.py` is
replaced by `tests/thymira/test_mira_flow.py`. `MIRA-01`'s deliverable text below is corrected to
name the two components Core actually composes.

### Still open — P4's call

- **Do not edit `CR-101` yet.** It stays coherent under the inherited vocabulary and is the
  evidence of which vocabulary the runtime speaks. The edit — `[A11, A12, A14, LEAKAGE-001]` →
  `[A11, A14, A20]` — belongs in the PR that registers `A20` in `CONTROLS`, together with the
  `CRX-103` edit (`examples/credit-risk/.thymira/policies.yaml:27`) and the rewrite of the live
  assertion at `tests/thymira/test_policies.py:181-184`. Pointing the rule at `A20` before the
  control exists just moves the dangling reference. `LEAKAGE-001` is **retired** there, not
  reserved: it was never an inherited id and never had a producer — `A20` is the control it stood
  in for.
- **Decide `CR-101`'s `min_severity` in that same change.** Renumbering alone still leaves a `LOW`
  finding able to block a run, which is half the defect.
- **Confirm `CTRL-MVP`'s `A8`** is not a duplicate of the shipped `A7` — see its open question.
- **`HITL-01` needs to decide how a tool call resumes after an async approval** (C-9): today
  `ToolManager` has no way to re-execute a call once a human answers outside the original request.

Nothing above blocks anyone: `CTRL-LEAKAGE-SPLIT` and `CTRL-CV-BASELINE-REPRO` are FINAL behind
`MIRA-01`, and `CR-101` cannot fire today anyway — MIRA emits only `A1`–`A7`, `A9`, `A10`, `A16`,
`A17` with `framework=INTERNAL`, none of which that rule matches.

## What this catalogue deliberately does not cover

Everything else in `docs/architecture/e2e-baseline-v2.md` has tasks. One section does not, and it
is recorded here so it reads as **deferred rather than forgotten** — an absent subsystem and a
descoped one look identical in a task list, and only one of them is a decision.

**§24 Infrastructure** (Kubernetes, KEDA, RabbitMQ, the Sandbox Manager as a deployed service) has
no task and needs none yet. It is deployment topology, not product surface: nothing in it changes
a contract or a seam, so the governing rule — *the MVP cuts implementations, never contracts or
seams* — does not force it early. The shape is held by the `infrastructure/{docker,kubernetes,
terraform}` and `services/{sandbox-manager,workers}` placeholders, and the seam that matters is
already owned: `TOOL-06` defines the Sandbox protocol and the `LocalSubprocessSandbox` behind it,
so a `ContainerSandbox` replaces the local subprocess without touching a call site. File
infrastructure tasks when there is something to deploy to.

Two neighbouring sections **are** covered, in case their absence from a domain heading suggests
otherwise: **§22 Security** is the permission-separation model, which lives throughout the
catalogue as `ToolCapability`, `tool_allowlist` on every agent spec, and the sandbox modes;
**§23 Observability** is `RA-API-09` (MVP), which installs the OpenTelemetry seam with a no-op
exporter so the Langfuse/Grafana backends are configuration later, not a retrofit.

## The tier boundary, subsystem by subsystem

The decision procedure when you are unsure whether something belongs in the MVP.

| Subsystem | MVP does | FINAL adds | The seam |
|---|---|---|---|
| **Repositories** | Run/Session/Record/Event/Checkpoint as **Protocols** with the full final method set (pagination cursor included); local backend | PostgreSQL backends + migrations | Callers import only the Protocol. Proven by one conformance suite both backends pass **unchanged** |
| **Artifact store** | `ArtifactStore` + `LocalArtifactStore` (built) | object-store backend | Protocol already carries the full method set |
| **Execution dispatcher** | `ExecutionDispatcher.submit` Protocol + inline runner; `create_run` returns before completion | queue dispatcher + durable worker | Async-shaped return from day 1, so clients never see the change |
| **Event observation** | `GET /runs/{id}/events` SSE with `since` / `Last-Event-ID` | push bus | `EventStore.read(run_id, *, since=None)` — the cursor exists from day 1 |
| **Sandbox** | `Sandbox` Protocol; restricted local requests refuse, explicit development execution reports `PARTIAL` | container execution currently reports `PARTIAL`; verified full confinement and workspace quotas remain pending | Tools use portable workspace paths; the backend must prove its reported boundary |
| **Tool results** | spill oversized results into the artifact store (gaining a digest) | tuned threshold, retrieval hint | Attaches to the manager's post-execute step; `ToolContext` already carries the store |
| **Prompt building** | builds from `current_surface(events)` — the derived projection | compaction engine appends `context.compacted` | Reading the projection is exactly what makes compaction a drop-in; the builder never invents its own history |
| **Agents** | 3 specialist agents (Data, Coding, Experiment) | 5 more | Each is an `AgentSpec` + output schema + prompt on the same runner: drop-in data |
| **Graphs** | real LangGraph Inspect→Plan→Execute→Summarize, sequential | parallel fan-out, THY↔MIRA rework loop | Real graph from day 1, so FINAL only **adds edges and nodes**; THY and MIRA are composed only in `core` and never import each other |
| **Usage** | ledger + hard ceiling | soft limits as Policy rules | The type and per-call charging are the seam; thresholds arrive as rules, not a new type |
| **Auth** | `resolve_actor` returning a default trusted actor | real principal | Every write already threads `Actor`; only the resolver changes |

**Tie-breaker.** If you cannot express the FINAL version as *a different implementation behind the
same seam, or a different value in the same field*, then the task is FINAL — not a smaller MVP.

## Summary

| Task | Title | Tier | Owner | Size | Status | Depends on |
|---|---|---|---|---|---|---|
| `API-GOV` | Governance API routes: audit, approvals, approve/reject | **MVP** | P4 | M | done | `HITL-01`, `MIRA-01` |
| `ASSUR-01` | Assurance bundle (report + decision + evidence index + disclaimer) | **MVP** | P4 | M | done | `MIRA-01` |
| `ASSUR-02` | Finding-level expert review + Finding->Evidence->source traceability index | **FINAL** | P4 | M | done | `ASSUR-01` |
| `AUD-COMPLIANCE` | Compliance audit agent (requirements coverage narrative) | **FINAL** | P4 | M | done | `MIRA-02`, `KB-02` |
| `AUD-CREDIT-RISK` | Credit Risk audit agent | **FINAL** | P4 | M | done | `MIRA-02`, `KB-01`, `KB-02` |
| `AUD-EUAIACT` | EU AI Act audit agent (the MVP 'Regulatory' agent) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01`, `KB-02` |
| `AUD-METHODOLOGY` | Methodology audit agent (train/test, leakage, validation, metrics) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01` |
| `AUD-MODEL-RISK` | Model Risk audit agent | **FINAL** | P4 | M | done | `MIRA-02`, `KB-01` |
| `AUD-REGULATORY-EVIDENCE` | Regulatory Evidence agent (citation-backed evidence bundles) | **FINAL** | P4 | M | done | `MIRA-01`, `KB-01` |
| `AUD-RISK` | Risk audit agent (bias/risk indicators, model limitations) | **MVP** | P4 | M | done | `MIRA-02`, `KB-01` |
| `CLI-APPROVE` | thymira approve / thymira reject RUN_ID | **MVP** | P5 | S | done | `API-GOV`, `HITL-01` |
| `CLI-AUDIT` | thymira audit RUN_ID | **MVP** | P5 | M | done | `API-GOV` |
| `CMP-01` | Compaction audit control (envelope, cannot-shadow-forward, payload shape) | **FINAL** | P4 | M | done | — |
| `CMP-02` | Compaction fidelity audit agent (summary vs shadowed events) | **FINAL** | P4 | M | done | `MIRA-02`, `CMP-01` |
| `CTRL-AUDIT-FRESHNESS` | Audit snapshot freshness control (later material evidence or invalidated artifacts) | **FINAL** | P4 | M | done | `MIRA-01`, `RA-CORE-04` |
| `CTRL-COVERAGE` | Requirements-coverage control (every framework requirement has evidence) | **FINAL** | P4 | M | done | `KB-02` |
| `CTRL-CV-BASELINE-REPRO` | Modelling controls A14 (test-once), A21 (fold-local CV), A22 (baseline) and A23 (reproducibility) | **FINAL** | P4 | L | done | `MIRA-01` |
| `CTRL-LEAKAGE-SPLIT` | Modelling controls A11 (split integrity) and A20 (leakage indicators) — recomputation | **FINAL** | P4 | M | done | `MIRA-01` |
| `CTRL-LINEAGE` | Modelling control A12 (lineage coherence across the modelling chain) | **FINAL** | P4 | M | done | `MIRA-01` |
| `CTRL-MVP` | Deterministic controls A8, A15 and the routing-floor control | **MVP** | P4 | M | done | `RISK-01` |
| `CTRL-REPORT` | Audit control A18 (report facts match the source artifacts) | **FINAL** | P4 | M | done | `MIRA-01` |
| `CTRL-SELECTION` | Modelling control A13 (model selection re-derived independently) | **FINAL** | P4 | L | done | `MIRA-01` |
| `GOV-01` | Audit contract: AuditInput, AuditAgentOutput, AuditAgentSpec | **MVP** | P4 | M | done | — |
| `GOV-03` | BudgetRule type + Policy.budget_rules field (defaulted empty) | **MVP** | P4 | S | done | — |
| `HITL-01` | Async human-approval flow: ApprovalService + PendingApproval fold + deferred Gate mode | **MVP** | P4 | L | done | — |
| `KB-01` | RegulationStore protocol + LocalRegulationStore + search_regulation tool | **MVP** | P4 | L | done | — |
| `KB-02` | requirements->controls mapping (English) + seed corpus + ingest script | **MVP** | P4 | M | done | `KB-01` |
| `KB-04` | PgVectorRegulationStore backend + lexical-vector ingestion | **FINAL** | P4 | L | done | `KB-01`, `KB-02` |
| `MIRA-01` | MiraAuditFlow + MiraSubgraph: prep -> preflight -> deterministic controls -> agent fan-out/fan-in -> Gate | **MVP** | P4 | L | done | `MIRA-02`, `GOV-01` |
| `MIRA-02` | Audit agent node runner with bounded evidence projection (spec -> structured findings) | **MVP** | P4 | L | done | `GOV-01` |
| `MIRA-03` | Adversarial finding verification, discovery loop, evaluator-optimizer rework signal | **FINAL** | P4 | L | done | `MIRA-01` |
| `POL-01` | Budget-threshold decisions (decide_budget + Gate.check_budget) | **FINAL** | P4 | M | done | `GOV-03` |
| `POL-02` | Model allow-list policy (a substitution is a WARNING) | **FINAL** | P4 | S | done | — |
| `QA-DEMO-GOV` | Credit-risk governance demo path + documentation | **FINAL** | P5 | M | done | `QA-E2E-GOV` |
| `QA-E2E-GOV` | E2E governance test: PASS / WARNING / REQUIRE_HUMAN_REVIEW+approve / BLOCK | **MVP** | P5 | L | done | `MIRA-01`, `HITL-01`, `CLI-AUDIT`, `CLI-APPROVE`, `AUD-METHODOLOGY`, `AUD-RISK`, `AUD-EUAIACT` |
| `RA-API-01` | FastAPI app factory + dependency-injection composition root | **MVP** | P1 | M | done | `RA-CORE-02`, `RA-CORE-10` |
| `RA-API-02` | Run endpoints: create, get, list (run/inspect) | **MVP** | P1 | M | done | `RA-API-01` |
| `RA-API-03` | Resume, approve, reject endpoints (HITL) | **MVP** | P1 | M | done | `RA-API-02`, `RA-CORE-08` |
| `RA-API-04` | Event API: GET /runs/{id}/events with SSE streaming | **MVP** | P1 | L | done | `RA-API-02` |
| `RA-API-05` | Audit & experiments endpoints | **MVP** | P3 | M | done | `RA-API-02` |
| `RA-API-06` | plan endpoint (Agent-API plan()) | **MVP** | P1 | S | done | `RA-API-02` |
| `RA-API-07` | delegate endpoint + Tool API read surface | **FINAL** | P1 | M | done | `RA-API-02` |
| `RA-API-08` | API error model, status mapping, Idempotency-Key, actor seam | **MVP** | P1 | M | done | `RA-API-02`, `RA-CORE-04` |
| `RA-API-09` | Basic OpenTelemetry + graph-hash provenance middleware | **MVP** | P5 | S | done | `RA-API-01`, `RA-CORE-06` |
| `RA-API-10` | AuthN/Z principal + route permissions | **FINAL** | P1 | M | done | `RA-API-08` |
| `RA-CLI-01` | thymira runs + richer status | **MVP** | P5 | S | done | `RA-API-02` |
| `RA-CLI-02` | thymira resume | **MVP** | P5 | S | done | `RA-API-03` |
| `RA-CLI-03` | thymira events --follow (SSE client) | **MVP** | P5 | M | done | `RA-API-04` |
| `RA-CLI-04` | thymira approve / thymira reject | **MVP** | P5 | S | done | `RA-API-03` |
| `RA-CORE-01` | SessionService + project/config bootstrap | **MVP** | P1 | M | done | `RA-STATE-01` |
| `RA-CORE-02` | RunService: create, transition, terminal | **MVP** | P1 | L | done | `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`, `RA-CORE-01` |
| `RA-CORE-03` | Usage ledger (measured-vs-estimated cost) | **MVP** | P1 | M | done | — |
| `RA-CORE-04` | Execution idempotency keys + lineage invalidation cascade | **MVP** | P1 | M | done | — |
| `RA-CORE-05` | Runtime graph state + THY/MIRA subgraph interface | **MVP** | P1 | M | done | `RA-STATE-02` |
| `RA-CORE-06` | Composition graph: THY -> MIRA -> Gate | **MVP** | P1 | L | done | `RA-CORE-05`, `RA-CORE-07` |
| `RA-CORE-07` | LangGraph checkpointer over thymira.state | **MVP** | P1 | M | done | `RA-STATE-02` |
| `RA-CORE-08` | resume(run_id): replay to the first unfinished step | **MVP** | P1 | M | done | `RA-CORE-02`, `RA-CORE-04`, `RA-CORE-06`, `RA-CORE-07` |
| `RA-CORE-09` | HITL decision channel (DecisionResolver) | **FINAL** | P1 | M | done | `RA-CORE-02` |
| `RA-CORE-10` | Execution dispatcher seam + inline runner | **MVP** | P1 | S | done | `RA-CORE-02`, `RA-CORE-06` |
| `RA-CORE-11` | Queue dispatcher + durable run worker | **FINAL** | P1 | L | done | `RA-CORE-10`, `RA-STATE-04` |
| `RA-QA-01` | Runtime-spine E2E (in-process, scripted agents, four cases) | **MVP** | P5 | M | done | `RA-API-03`, `RA-API-04`, `RA-API-05`, `RA-CLI-01` |
| `RA-QA-02` | API contract test suite | **MVP** | P5 | S | done | `RA-API-05`, `RA-API-06` |
| `RA-QA-03` | Durable-resume E2E against PostgreSQL + worker | **FINAL** | P5 | M | done | `RA-CORE-11`, `RA-STATE-04` |
| `RA-STATE-01` | Run & Session repository protocols + local backend | **MVP** | P1 | M | done | — |
| `RA-STATE-02` | Per-run stores: RecordRepository, EventStore, CheckpointRepository (protocols + local) | **MVP** | P1 | L | done | `RA-STATE-01` |
| `RA-STATE-03` | UnitOfWork: atomic multi-record run write | **MVP** | P1 | M | done | `RA-STATE-01`, `RA-STATE-02` |
| `RA-STATE-04` | PostgreSQL implementations of all repository protocols + migrations | **FINAL** | P1 | L | done | `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`, `RA-STATE-05` |
| `RA-STATE-05` | Backend-agnostic repository conformance suite | **MVP** | P5 | S | done | `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03` |
| `RISK-01` | Deterministic Risk classifier override layer (thymira.agents.risk) | **MVP** | P4 | L | done | — |
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
| `THY-18` | Statistics agent | **FINAL** | P2 | M | done | `THY-14` |
| `THY-19` | ML agent (model selection, training, tuning; may delegate to helpers) | **FINAL** | P2 | L | done | `THY-08`, `THY-16` |
| `THY-20` | Data Quality agent | **FINAL** | P2 | M | done | `THY-14` |
| `THY-21` | Visualization agent | **FINAL** | P2 | M | done | `THY-14` |
| `THY-22` | Research agent (web/search + Knowledge Base) | **FINAL** | P2 | L | done | `THY-14` |
| `THY-23` | Parallel fan-out execution in the Execute node | **FINAL** | P2 | L | done | `THY-12` |
| `THY-24` | THY <-> MIRA evaluator-optimizer rework loop | **FINAL** | P2 | L | done | `THY-13`, `THY-11` |
| `THY-25` | Context compaction engine (context.compacted, atomic, envelope-logged) | **FINAL** | P2 | L | done | `THY-05`, `THY-06` |
| `THY-26` | Token budgeting / context-window management | **FINAL** | P2 | M | done | `THY-25` |
| `THY-27` | Project-level agents.yaml overlay | **FINAL** | P2 | M | done | `THY-01`, `THY-10` |
| `THY-28` | Run recipes as data (.thymira/recipes/<name>.yaml) | **FINAL** | P2 | L | done | `THY-09`, `THY-07` |
| `THY-29` | CLI: `thymira plan` (produce and show the plan without executing) | **FINAL** | P5 | S | done | `THY-11` |
| `THY-30` | CLI: render THY activity (plan, delegation tree, per-agent tokens/cost) | **MVP** | P5 | M | done | `THY-07`, `THY-08` |
| `THY-31` | E2E: THY completes the data-science workflow (happy path + failed run) | **MVP** | P5 | M | done | `THY-13`, `THY-17` |
| `THY-32` | Specialist agent roster: open `ThyAgentKind` to the shipped FINAL agents | **FINAL** | P2 | M | done | `THY-12`, `THY-18`, `THY-20`, `THY-21` |
| `THY-33` | Lazy public imports for agents without tools | **MVP** | P2 | S | done | `THY-04` |
| `TOOL-01` | Sandbox vocabulary and ToolCall enforcement fields (contract, pre-freeze) | **MVP** | P3 | M | done | — |
| `TOOL-02` | ToolResult sandbox fields and manager propagation into the recorded ToolCall | **MVP** | P3 | S | done | `TOOL-01` |
| `TOOL-03` | Self-describing Tool protocol: arguments model, input schema and validation | **MVP** | P3 | M | done | — |
| `TOOL-04` | Manager emits artifact.created for tool-produced artifacts (closes control A10) | **MVP** | P3 | M | done | `TOOL-05` |
| `TOOL-05` | Spill policy: oversized tool results become referenced artifacts | **MVP** | P3 | M | done | — |
| `TOOL-06` | Sandbox protocol and LocalSubprocessSandbox (reports PARTIAL) | **MVP** | P3 | L | done | `TOOL-01` |
| `TOOL-07` | ContainerSandbox with resource limits (reports PARTIAL, fail-closed) | **FINAL** | P3 | L | partial | `TOOL-06` |
| `TOOL-08` | run_python tool over the Sandbox protocol | **MVP** | P3 | M | done | `TOOL-06`, `TOOL-03`, `TOOL-02` |
| `TOOL-09` | File tools: read_file, write_file, list_files (workspace-contained) | **MVP** | P3 | M | done | `TOOL-03` |
| `TOOL-10` | Git tools: git_status, git_diff, git_log, git_commit | **MVP** | P3 | M | done | `TOOL-03`, `TOOL-06` |
| `TOOL-11` | Git worktree tools for parallel/background experiment isolation | **FINAL** | P3 | M | done | `TOOL-10` |
| `TOOL-12` | Dataset registry and loader (Artifact kind=dataset with captured schema) | **MVP** | P3 | M | done | — |
| `TOOL-13` | analyze_dataset tool | **MVP** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-14` | profile_dataset tool | **MVP** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-15` | query_sql tool (read-only, embedded engine over the store) | **FINAL** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-16` | run_statistics tool | **FINAL** | P3 | M | done | `TOOL-12`, `TOOL-03`, `TOOL-04` |
| `TOOL-17` | MLflow integration: ExperimentTracker protocol, local impl, and mlflow_* tools | **MVP** | P3 | L | done | `TOOL-03`, `TOOL-04` |
| `TOOL-18` | run_experiment tool (Experiment record + experiment/model events + model artifact) | **MVP** | P3 | L | done | `TOOL-17`, `TOOL-12`, `TOOL-08` |
| `TOOL-19` | query_mlflow tool (read runs, params, metrics) | **FINAL** | P3 | S | done | `TOOL-17` |
| `TOOL-20` | compare_models tool | **FINAL** | P3 | M | done | `TOOL-19` |
| `TOOL-21` | inspect_model tool | **FINAL** | P3 | M | done | `TOOL-18`, `TOOL-03` |
| `TOOL-22` | audit_model tool (deterministic governance evidence) | **FINAL** | P3 | L | done | `TOOL-18`, `TOOL-12`, `TOOL-03` |
| `TOOL-23` | MCP server exposing the tool set through the Tool Manager | **FINAL** | P3 | L | done | `TOOL-03` |
| `TOOL-24` | MIRA control: run executed under partial or unusable confinement | **MVP** | P4 | M | done | `TOOL-02` |
| `TOOL-25` | CLI: thymira experiment <run_id> | **MVP** | P5 | S | done | `TOOL-18` |
| `TOOL-26` | CLI: thymira mlflow | **FINAL** | P5 | S | done | `TOOL-19` |
| `TOOL-27` | QA: end-to-end execution test (run_python + MLflow + artifacts + spill, truthful audit findings) | **MVP** | P5 | L | done | `TOOL-08`, `TOOL-18`, `TOOL-05`, `TOOL-04`, `TOOL-12` |
| `TOOL-28` | QA: sandbox enforcement is a recorded fact and drives the MIRA finding | **MVP** | P5 | M | done | `TOOL-24`, `TOOL-08` |
| `TOOL-29` | QA: tool descriptor and MCP round-trip contract test | **FINAL** | P5 | M | done | `TOOL-23`, `TOOL-03` |

## Tasks by domain

### Runtime and API

*33 tasks — 27 MVP, 6 FINAL.*

#### `RA-API-01` — FastAPI app factory + dependency-injection composition root

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-CORE-02`, `RA-CORE-10`
- **Status** done
- **Deliverable** `thymira.api.app.create_app(deps: RuntimeDeps)->FastAPI`; `thymira.api.deps.RuntimeDeps` dataclass (RunService, SessionService, repositories, EventStore, Gate factory, ExecutionDispatcher) plus `build_default_deps(root)` using the local backends + `InlineDispatcher`; `GET /healthz`; app-level validation exception handling; and registered route groups for runs, events, audit and experiments.
- **Done when** `tests/thymira/test_api_app.py`: `TestClient(create_app(build_default_deps(tmp_path)))` returns 200 on `/healthz` and the app exposes the run/events/audit/experiments routes. Business handlers remain owned by their dedicated API tasks.
- **Seam (why FINAL needs no refactor)** Services are injected via RuntimeDeps, so swapping local->Postgres (RA-STATE-04) or inline->queue (RA-CORE-11) is a build_*_deps change with no route or service edit; create_app is the single composition root.
- **Specified at** docs/architecture/e2e-baseline-v2.md:143; apps/api/src/thymira/api/__init__.py:1

#### `RA-API-02` — Run endpoints: create, get, list (run/inspect)

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-01`
- **Status** done
- **Deliverable** `thymira.api.routes.runs`: `POST /runs` (body `CreateRunRequest{prompt, project_id?, session_id?}` -> configured project/session resolution, `RunService.create_run` and the injected dispatcher, returns the Run JSON, `201`), `GET /runs/{id}` (inspect, project-scoped `404` unknown), and `GET /runs` (list with `?status=&project_id=&cursor=&limit=`); pydantic request/response models mirroring the frozen Run contract.
- **Done when** `tests/thymira/test_api_runs.py`: `POST /runs` without a project resolves the configured workspace and returns `201` with a `run_` id and status; `GET /runs/{id}` echoes it; `GET /runs` paginates via cursor; an unknown id returns `404` with a problem+json body; and foreign projects/sessions are rejected.
- **Seam (why FINAL needs no refactor)** Request/response bodies serialize the frozen Run contract; POST /runs returns immediately with the created Run (async-shaped) so FINAL background/worker execution (RA-CORE-11) needs no client or endpoint change. Covers Agent-API run() and inspect() (baseline §6).
- **Specified at** docs/architecture/e2e-baseline-v2.md:308; docs/roadmap/mvp-3-weeks.md:419

#### `RA-API-03` — Resume, approve, reject endpoints (HITL)

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-02`, `RA-CORE-08`
- **Status** done
- **Deliverable** POST /runs/{id}/resume (-> RunService.resume); POST /runs/{id}/approve and POST /runs/{id}/reject resolving a WAITING_FOR_APPROVAL run by driving the pending PolicyDecision through an HTTP-backed Approver injected into the built Gate, appending human.approval; body {actor, note?}.
- **Done when** tests/thymira/test_api_runs.py: a run parked in WAITING_FOR_APPROVAL resolves through approve/reject, records human.approval, resumes through the injected dispatcher on approval, and rejects approval for a non-waiting Run with 409.
- **Seam (why FINAL needs no refactor)** The API supplies an Approver to the already-built, injectable Gate, so the human-in-the-loop seam is unchanged; MVP resolves synchronously, FINAL routes approvals through a durable queue behind the same endpoints. Covers Agent-API resume(), approve(), reject().
- **Specified at** docs/architecture/e2e-baseline-v2.md:314; docs/roadmap/mvp-3-weeks.md:2166

#### `RA-API-04` — Event API: GET /runs/{id}/events with SSE streaming

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `apps/api`
- **Depends on** `RA-API-02`
- **Status** done
- **Implemented** `apps/api/src/thymira/api/routes/events.py` (JSON and SSE routes),
  `apps/api/src/thymira/api/subscription.py` (EventSubscription and the local polling MVP), and
  `tests/thymira/test_api_events.py`; `EventPage` remains the JSON response contract.
- **Deliverable** thymira.api.routes.events: GET /runs/{id}/events returning the event list as JSON, and — with Accept: text/event-stream or ?follow=1 — a Server-Sent-Events stream framed one event per message with id:=seq and a ?since=<seq> cursor to replay/tail; thymira.api.subscription.EventSubscription Protocol (subscribe(run_id, since)->async iterator) with a PollingEventSubscription MVP impl tailing the EventStore.
- **Done when** tests/thymira/test_api_events.py: GET /runs/{id}/events returns all events after a given cursor; the SSE stream yields events appended after connection and resumes correctly from Last-Event-ID / ?since.
- **Seam (why FINAL needs no refactor)** The SSE framing + since/Last-Event-ID cursor is the observation contract every client depends on (baseline §8: 'build different clients without duplicating logic'); MVP polls the log, FINAL swaps EventSubscription for a push bus (Redis/RabbitMQ, baseline §18) with the identical wire contract.
- **Specified at** docs/architecture/e2e-baseline-v2.md:370; docs/roadmap/mvp-3-weeks.md:2255

#### `RA-API-05` — Audit & experiments endpoints

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-02`
- **Status** done
- **Deliverable** GET /runs/{id}/audit running/reading the built thymira.mira.audit_run over the run's events + ArtifactStore and returning the AuditReport plus the run-level PolicyDecision folded from the policy.decision event; GET /runs/{id}/experiments listing the run's Experiment records via RecordRepository.
- **Done when** tests/thymira/test_api_audit_experiments.py: for a completed run, GET /audit returns an AuditReport JSON with controls and a decision; GET /experiments returns the stored experiments in order.
- **Seam (why FINAL needs no refactor)** Audit reuses the already-built audit_run + AuditReport contract and folds PolicyDecision/AuditFinding from the event log (no new store); experiments read the RecordRepository seam, so FINAL Postgres storage needs no endpoint change. Covers Agent-API audit() and experiment().
- **Specified at** docs/architecture/e2e-baseline-v2.md:308; docs/roadmap/mvp-3-weeks.md:424

#### `RA-API-06` — plan endpoint (Agent-API plan())

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** S · **Member path** `apps/api`
- **Depends on** `RA-API-02`
- **Status** done
- **Deliverable** POST /runs/{id}/plan returning THY's proposed phase plan and recording a policy.decision -> human.approval_requested for sign-off before execution; MVP returns the deterministic PHASE_ORDER-derived plan (from the built thymira.core.phases) when THY has no planner yet.
- **Done when** tests/thymira/test_api_plan.py: POST /plan returns an ordered phase plan and the run's log gains a policy.decision event for approval.
- **Seam (why FINAL needs no refactor)** plan() is one of the nine Agent-API methods (baseline §6); the endpoint + response shape exist in MVP so FINAL's real THY planner fills the plan body without a contract change. Phase boundaries are Gate checkpoints (ADR-0004:90).
- **Specified at** docs/architecture/e2e-baseline-v2.md:313; docs/adr/0004-model-routing-and-agent-topology.md:90

#### `RA-API-07` — delegate endpoint + Tool API read surface

- **Tier** FINAL · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-02`
- **Status** done
- **Deliverable** POST /runs/{id}/delegate (create a Task for a named sub-agent, returns the Task); GET /tools and GET /tools/{name} exposing registered ToolCapability descriptors (the read side of the Tool API, baseline §7).
- **Done when** tests/thymira/test_api_delegate_tools.py: POST /delegate creates a Task in PENDING linked to the run; GET /tools lists the registered tool capabilities with their risk metadata.
- **Seam (why FINAL needs no refactor)** delegate() and the Tool API complete the nine Agent-API methods and the Tool surface (baseline §6, §7); Task and ToolCapability contracts already exist, so these are additive endpoints, not a contract change. MCP exposure of the Tool API is post-MVP.
- **Specified at** docs/architecture/e2e-baseline-v2.md:348; docs/architecture/e2e-baseline-v2.md:315

#### `RA-API-08` — API error model, status mapping, Idempotency-Key, actor seam

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-02`, `RA-CORE-04`
- **Status** done
- **Implemented** `apps/api/src/thymira/api/errors.py` (problem+json handlers and domain status mapping), `apps/api/src/thymira/api/principal.py` (MVP actor resolver), Idempotency-Key deduplication for `POST /runs`, and `tests/thymira/test_api_errors.py`.
- **Deliverable** thymira.api.errors: a problem+json error body model + handlers mapping domain errors to 404/409/422; Idempotency-Key header support on POST /runs (dedupe repeated creates via the idempotency journal); thymira.api.principal.resolve_actor(request)->Actor extracting/threading the acting Actor (MVP returns a default trusted actor from a header).
- **Done when** tests/thymira/test_api_errors.py: a forbidden transition returns 409 with a problem+json body; POSTing /runs twice with the same Idempotency-Key returns the same run id; a request with no actor gets the default system actor.
- **Seam (why FINAL needs no refactor)** resolve_actor is the auth seam — MVP returns a default/trusted actor, FINAL (RA-API-10) resolves a real principal from a token behind the same function; every write already threads an Actor and the Gate already records authenticated, so RBAC is additive, not a refactor.
- **Specified at** docs/architecture/e2e-baseline-v2.md:794; docs/contracts/contract-v0.1.md:70

#### `RA-API-09` — Basic OpenTelemetry + graph-hash provenance middleware

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `apps/api`
- **Depends on** `RA-API-01`, `RA-CORE-06`
- **Status** done
- **Deliverable** thymira.api.telemetry.install_tracing(app) adding an OTel span per request (no-op exporter by default, configured by env); assert the composition graph_definition_hash flows into run.started end to end.
- **Done when** tests/thymira/test_api_telemetry.py: with an in-memory span exporter a POST /runs produces a span named for the route; the run's run.started event carries a 64-hex graph_definition_hash.
- **Seam (why FINAL needs no refactor)** Basic OTel now (roadmap week 3 P1), full stack later (baseline §23) — the middleware seam and env-driven exporter mean FINAL adds Langfuse/Grafana exporters via configuration, not code.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2261; docs/architecture/e2e-baseline-v2.md:825

#### `RA-API-10` — AuthN/Z principal + route permissions

- **Tier** FINAL · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `apps/api`
- **Depends on** `RA-API-08`
- **Status** done
- **Deliverable** A real resolve_actor (bearer token -> authenticated Actor with role) and a permissions layer mapping route+actor -> allow/deny (baseline §19 permissions, §22 least privilege), with 401/403 handling and the resulting events carrying the authenticated actor.
- **Done when** integration test: an unauthenticated write returns 401; a read-only principal POSTing /runs returns 403; a valid principal succeeds and the emitted events carry the authenticated actor.
- **Seam (why FINAL needs no refactor)** Fills the resolve_actor seam from RA-API-08; every write already threads Actor and the Gate already records authenticated, so authorization is purely additive — no endpoint or service rewrite.
- **Specified at** docs/architecture/e2e-baseline-v2.md:794

#### `RA-CLI-01` — thymira runs + richer status

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `RA-API-02`
- **Status** done
- **Already in the repository** adapters/cli/src/thymira/cli/commands/status.py; adapters/cli/src/thymira/cli/client.py. Still missing: ApiClient.list_runs, thymira.cli.commands.runs (list_runs) / 'thymira runs' command, phase/tool_count/experiment_count/final_decision fields on status, tests/thymira/test_cli_runs.py
- **Deliverable** ApiClient.list_runs(...) + thymira.cli.commands.runs.list_runs (thymira runs) rendering id/status/created/prompt; extend the status command to show phase, agent/tool/experiment/artifact counts and final_decision.
- **Done when** tests/thymira/test_cli_runs.py: with a stubbed httpx transport, thymira runs lists runs and thymira status <id> renders the new fields; exit code 0.
- **Seam (why FINAL needs no refactor)** The CLI stays a pure HTTP client (invariant: clients own no state — enforced by the import-linter contract) consuming the RA-API-02 list contract, so nothing in the CLI changes when the backend swaps.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1791; docs/architecture/e2e-baseline-v2.md:206

#### `RA-CLI-02` — thymira resume

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `RA-API-03`
- **Status** done
- **Deliverable** ApiClient.resume(run_id) + thymira.cli.commands.resume hitting POST /runs/{id}/resume and rendering the resulting status; run-id validation reused from the status command.
- **Done when** tests/thymira/test_cli_resume.py: thymira resume <id> calls the resume endpoint and prints the new status; a malformed id exits with code 2 (same validation as status).
- **Seam (why FINAL needs no refactor)** Consumes RA-API-03; the command shape is final, so FINAL durable/cross-process resume is invisible to the CLI.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1938

#### `RA-CLI-03` — thymira events --follow (SSE client)

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `adapters/cli`
- **Depends on** `RA-API-04`
- **Status** done
- **Deliverable** ApiClient.stream_events(run_id, *, since, follow) consuming the SSE stream over httpx with reconnect via Last-Event-ID + thymira.cli.commands.events (thymira events <id> [--follow] [--since N]) rendering 'type seq actor' lines.
- **Done when** tests/thymira/test_cli_events.py: against a stub SSE transport, thymira events <id> prints buffered events and --follow prints events pushed after connect; reconnection resumes from the last seq.
- **Seam (why FINAL needs no refactor)** The client implements the RA-API-04 SSE + since/Last-Event-ID contract; when FINAL moves to a push bus the client is unchanged because the wire contract is fixed. This is the client side of 'the event subscription/stream clients observe'.
- **Specified at** docs/architecture/e2e-baseline-v2.md:370

#### `RA-CLI-04` — thymira approve / thymira reject

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `RA-API-03`
- **Status** done
- **Deliverable** ApiClient.approve/reject(run_id, note?) + thymira.cli.commands.approve and reject hitting the HITL endpoints and rendering the resolved decision; 409 mapped to a clear 'run is not awaiting approval' message.
- **Done when** tests/thymira/test_cli_approval.py: thymira approve <id> prints the resolved status; approving a non-waiting run exits non-zero with the mapped message.
- **Seam (why FINAL needs no refactor)** Consumes RA-API-03; approval remains a runtime decision recorded by the built Gate — the CLI never decides, keeping the LLM/policy separation intact.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2166

#### `RA-CORE-01` — SessionService + project/config bootstrap

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** `RA-STATE-01`
- **Status** done
- **Deliverable** thymira.core.sessions.SessionService (create_session, get_session, attach_run) over SessionRepository; thymira.core.projects.load_project_config(path)->ProjectConfig reading .thymira/config.yaml; ProjectResolver mapping a workspace dir to a project_id + the governance frameworks that select which policy stack to load.
- **Done when** tests/thymira/test_core_sessions.py: creating a session then attaching a run appends the run id to Session.run_ids; load_project_config(examples/credit-risk/.thymira/config.yaml) returns a ProjectConfig whose governance.frameworks contains EU_AI_ACT and CREDIT_RISK.
- **Seam (why FINAL needs no refactor)** SessionService depends only on the SessionRepository Protocol (RA-STATE-01) so JSONL->Postgres swaps invisibly; ProjectConfig is the frozen schema already in thymira.schemas, and the resolved frameworks feed the built policy loader unchanged.
- **Specified at** docs/architecture/e2e-baseline-v2.md:570; docs/contracts/contract-v0.1.md:26

#### `RA-CORE-02` — RunService: create, transition, terminal

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `runtime/core`
- **Depends on** `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`, `RA-CORE-01`
- **Status** done
- **Deliverable** thymira.core.runs.RunService: create_run(session_id, prompt, *, actor, workspace)->Run (captures provenance and persists run.started with run_environment, policy_sha256 and graph_definition_hash), get_run, list_runs(...), advance/transition(run,status) through RunController and LocalRunStore, and complete/fail helpers emitting run.completed/run.failed. LocalRunStore is the single local lifecycle writer; repository and UnitOfWork protocols remain backend/conformance seams.
- **Scope note — C-8 is implemented.** `thymira.core.runs.RunService` (`create_run`, `advance`/`transition`, `complete`, `fail`) routes every lifecycle transition through `RunController` and persists it through the authoritative `LocalRunStore` (ADR-0010). The repository and `UnitOfWork` protocols remain tested foundation seams for their own backends; they are not competing Run lifecycle writers in the MVP. A PostgreSQL backend (`RA-STATE-04`) is FINAL work behind its own ADR (ADR-0010).
- **Done when** tests/thymira/test_core.py: create_run persists a Run in CREATED and the first event is run.started with keys run_environment, policy_sha256 and a 64-hex graph_definition_hash; a forbidden status transition raises ValueError; complete() emits run.completed and sets completed_at.
- **Seam (why FINAL needs no refactor)** The run.started payload keys (run_environment, policy_sha256, graph_definition_hash) are fixed now (ADR-0004:88) so MIRA control A17 and any auditor read the same shape after FINAL distributed execution; RunService depends on the event-backed local persistence boundary and preserves the repository protocols for backend conformance.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:88; packages/schemas/src/thymira/schemas/run.py:58

#### `RA-CORE-03` — Usage ledger (measured-vs-estimated cost)

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** none
- **Status** done
- **Deliverable** thymira.core.usage: UsageLedger (charge(*, requests, tokens, cost_usd\|None), snapshot()->dict, exceeds(limits)->tuple[str,...]); unknown cost degrades the running total to None (ADR-0005 idea 4). snapshot() is wired into the ApprovalRequest.cost_so_far field at the core-owned Gate call sites. **`UsageLimits` is imported from `thymira.agents.usage`, not redefined here** — see the scope note.
- **Scope note — one `UsageLimits`, owned by `THY-07`.** `UsageLimits` is defined once in `thymira.agents.usage` (`THY-07`) and imported upwards by `UsageLedger`, preserving the runtime layer order. The composition graph now reads `SubgraphDeps.usage_ledger.snapshot()`, passes that authoritative snapshot to the Gate, and stores the same value in `RuntimeState.usage`; unit and graph-level tests prove the flow.
- **Done when** tests/thymira/test_core_usage.py: charging two calls where one has cost_usd=None yields a snapshot whose cost_usd is None but whose requests/tokens are summed; exceeds() names each breached limit; the value flows into ApprovalRequest.cost_so_far.
- **Seam (why FINAL needs no refactor)** snapshot() fills the ALREADY-present ApprovalRequest.cost_so_far field, and budget thresholds become Policy Engine rules later (ADR-0004:87) — so FINAL budget enforcement adds rules, not a new type; the ledger is final-shape from day 1.
- **Specified at** docs/adr/0002-legacy-disposition.md:118; docs/adr/0004-model-routing-and-agent-topology.md:65

#### `RA-CORE-04` — Execution idempotency keys + lineage invalidation cascade

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** none
- **Status** done
- **Deliverable** thymira.core.idempotency.compute_execution_key(*, tool, input_fingerprints, parameters, phase_revision, decision_fingerprint)->str (sha256); IdempotencyJournal (seen(key), record(key, artifact_ids)) refusing a duplicate key; thymira.core.lineage.cascade_invalidation(store, changed_names, downstream_graph, reason) calling the built ArtifactStore.invalidate + set_lineage.
- **Done when** tests/thymira/test_core_idempotency.py: identical inputs produce the same key and re-recording it raises; changing any component changes the key; invalidating an upstream artifact cascades to declared downstream names with the reason recorded.
- **Seam (why FINAL needs no refactor)** execution_key is already an Artifact field and ArtifactStore.set_lineage already exists — this fills the seam ADR-0004's resume/journal needs; MVP journal is in-memory, FINAL persists it (RA-STATE) and replays completed steps on resume behind the same compute_execution_key signature.
- **Specified at** docs/adr/0002-legacy-disposition.md:106; docs/adr/0004-model-routing-and-agent-topology.md:89

#### `RA-CORE-05` — Runtime graph state + THY/MIRA subgraph interface

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** `RA-STATE-02`
- **Status** done
- **Deliverable** thymira.core.graph.state.RuntimeState (LangGraph state: run_id, project_id, phase, plan, task_ids, usage snapshot, findings, decision) and thymira.core.graph.protocol.Subgraph Protocol (name, graph_version()->str, invoke(state, *, deps)->state) that ThyGraph (P2) and MiraGraph (P4) implement; a SubgraphDeps dataclass carrying the runtime handles (EventStore log, Gate, ArtifactStore, ToolManager, RecordRepository, UsageLedger).
- **Done when** tests/thymira/test_core_graph_state.py: a scripted subgraph implementing Subgraph runs against RuntimeState, mutates phase, and returns a stable graph_version() hash; RuntimeState round-trips through the checkpointer serializer.
- **Seam (why FINAL needs no refactor)** This is the THY/MIRA composition seam (invariant: composed only in core, never importing each other). Subgraph + SubgraphDeps are final-shape so P2/P4 build real graphs behind them and FINAL adds nodes/parallelism without touching the composition.
- **Specified at** docs/architecture/e2e-baseline-v2.md:394; docs/adr/0004-model-routing-and-agent-topology.md:49

#### `RA-CORE-06` — Composition graph: THY -> MIRA -> Gate

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `runtime/core`
- **Depends on** `RA-CORE-05`, `RA-CORE-07`
- **Status** done
- **Deliverable** thymira.core.graph.compose.build_runtime_graph(thy: Subgraph, mira: Subgraph, *, checkpointer)->CompiledGraph wiring THY(execution)->MIRA(audit)->Gate.review_findings->terminal, driving Run status (PLANNING->RUNNING->...->AUDITING->COMPLETED/WAITING_FOR_APPROVAL/BLOCKED) and emitting the lifecycle events; graph_definition_hash() combining both subgraph versions for run.started.
- **Done when** tests/thymira/test_core_composition.py: with scripted THY/MIRA subgraphs the graph drives a run to COMPLETED and the log contains run.started..audit.completed..policy.decision..run.completed in order and verifies; a MIRA finding that maps to REQUIRE_HUMAN_REVIEW parks the run in WAITING_FOR_APPROVAL.
- **Seam (why FINAL needs no refactor)** Control flow is code (ADR-0004 dec.3); model choices live only inside subgraph nodes. The composition depends on the Subgraph protocol so the real ThyGraph/MiraGraph drop in unchanged, and FINAL parallel audit fan-out is a node-internal change, not a rewrite here.
- **Specified at** docs/architecture/e2e-baseline-v2.md:394; docs/adr/0004-model-routing-and-agent-topology.md:94

#### `RA-CORE-07` — LangGraph checkpointer over thymira.state

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** `RA-STATE-02`
- **Status** done
- **Deliverable** thymira.core.graph.checkpoint.StateCheckpointer implementing LangGraph's BaseCheckpointSaver (put/get_tuple/list) backed by the CheckpointRepository Protocol defined in thymira.state (RA-STATE-02); serializes RuntimeState deterministically.
- **Done when** tests/thymira/test_core_checkpoint.py: a graph run interrupted after the THY node leaves a checkpoint that get_tuple restores; re-invoking with the same thread_id continues from the MIRA node, not from the start.
- **Seam (why FINAL needs no refactor)** BaseCheckpointSaver is LangGraph's stable interface; MVP persists via LocalCheckpointRepository, FINAL swaps to the Postgres checkpoints table (RA-STATE-04) with the same saver — durable execution and resume need no graph rewrite.
- **Specified at** docs/architecture/e2e-baseline-v2.md:394; docs/adr/0004-model-routing-and-agent-topology.md:89

#### `RA-CORE-08` — resume(run_id): replay to the first unfinished step

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** `RA-CORE-02`, `RA-CORE-04`, `RA-CORE-06`, `RA-CORE-07`
- **Status** done
- **Deliverable** RunService.resume(run_id, *, actor)->Run that verifies the run's event chain (EventLog.verify) and refuses a tampered log before writing anything, loads the latest checkpoint, and continues the composition graph from the first unfinished step without replaying completed child nodes; resuming a terminal run is an idempotent no-op returning the run.
- **Done when** tests/thymira/test_core_dispatch.py and tests/thymira/test_core_composition.py: a Run resumes through the injected dispatcher, a parked approval graph continues from its Gate checkpoint without re-executing THY or MIRA, and resuming a Run whose JSONL was edited raises before any new event is appended.
- **Seam (why FINAL needs no refactor)** resume replays to the first unfinished step (ADR-0004:89) via the checkpointer (RA-CORE-07) and idempotency journal (RA-CORE-04); the resume(run_id) signature is final — FINAL adds cross-process/worker resume behind it while MVP resumes in-process.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1938; docs/adr/0004-model-routing-and-agent-topology.md:89

#### `RA-CORE-09` — HITL decision channel (DecisionResolver)

- **Tier** FINAL · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/core`
- **Depends on** `RA-CORE-02`
- **Status** done
- **Deliverable** thymira.core.decisions: DecisionRequest/DecisionResolution contracts and a DecisionResolver Protocol (resolve(DecisionRequest)->DecisionResolution) with modes automatic, human_confirm and (FINAL) llm_proposes_human_confirms; every Q&A traced as agent.message events on the log, kept strictly separate from Gate authorization; the resolver never emits a Decision.
- **Done when** tests/thymira/test_core_decisions.py: an automatic resolver answers and traces the Q&A; a human_confirm resolver records the human's answer as an event; asserting the resolver output type is never a schemas.Decision.
- **Seam (why FINAL needs no refactor)** The DecisionResolver Protocol is the seam and is a new module nothing else calls yet, so it is FINAL (not needed for the demo); it is deliberately separate from the Gate so analytical Q&A never becomes an authorization (invariant: LLM proposes, code authorizes).
- **Specified at** docs/adr/0002-legacy-disposition.md:49

#### `RA-CORE-10` — Execution dispatcher seam + inline runner

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** S · **Member path** `runtime/core`
- **Depends on** `RA-CORE-02`, `RA-CORE-06`
- **Status** done
- **Deliverable** `thymira.core.dispatch.ExecutionDispatcher` Protocol (`submit(run_id)->None`) and `InlineDispatcher` (compiles + runs the composition graph synchronously, driving status + events); `RunService.create_run` submits through the injected dispatcher and accepts the reserved `background` option. `resume` remains owned by `RA-CORE-08` and will use the same seam when implemented.
- **Done when** `tests/thymira/test_core_dispatch.py`: creating a Run with the `InlineDispatcher` drives it to a terminal state and persists verifiable events; the dispatcher is injected into `RunService`, not constructed inside it.
- **Seam (why FINAL needs no refactor)** ExecutionDispatcher.submit is the async-execution seam (baseline §16). MVP runs inline; FINAL (RA-CORE-11) enqueues to workers behind the same Protocol. Because create_run already returns before completion, moving execution off-process needs no client or endpoint change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:607

#### `RA-CORE-11` — Queue dispatcher + durable run worker

- **Tier** FINAL · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `runtime/core`
- **Depends on** `RA-CORE-10`, `RA-STATE-04`
- **Status** done
- **Deliverable** QueueDispatcher (publishes run tasks to RabbitMQ) implementing ExecutionDispatcher; a services/workers run-worker consuming the queue and invoking the composition graph with the Postgres checkpointer; dead-letter handling and at-least-once + idempotent execution (baseline §18).
- **Done when** integration test (slow): submitting via QueueDispatcher, a separate worker process drives the run to COMPLETED; killing the worker mid-run and restarting resumes from the last checkpoint and produces no duplicate experiments/artifacts.
- **Seam (why FINAL needs no refactor)** Drop-in for the ExecutionDispatcher Protocol (RA-CORE-10); resume (RA-CORE-08), the checkpointer (RA-CORE-07/RA-STATE-04) and the idempotency journal (RA-CORE-04) already make cross-process continuation correct, so no domain rewrite.
- **Specified at** docs/architecture/e2e-baseline-v2.md:676

#### `RA-QA-01` — Runtime-spine E2E (in-process, scripted agents, four cases)

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `tests/thymira`
- **Depends on** `RA-API-03`, `RA-API-04`, `RA-API-05`, `RA-CLI-01`
- **Status** done
- **Deliverable** tests/thymira/test_e2e_runtime_spine.py (integration) driving the CLI ApiClient -> in-process API (TestClient) -> composition graph with scripted THY/MIRA subgraphs -> local persistence, covering the four roadmap day-11 cases: happy path to COMPLETED, a failed execution -> FAILED, a governance WARNING, and REQUIRE_HUMAN_REVIEW -> approve -> COMPLETED.
- **Done when** Runs under just test-all (marked integration): for all four cases the event log verifies and the status/audit/experiments endpoints agree with the events.
- **Seam (why FINAL needs no refactor)** Exercises every runtime-api seam with reduced agents; when real THY/MIRA and Postgres land, the same test runs unchanged because it targets the API/contract surface, not implementations — which is the no-refactor proof at the E2E level.
- **Specified at** docs/roadmap/mvp-3-weeks.md:746

#### `RA-QA-02` — API contract test suite

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `tests/thymira`
- **Depends on** `RA-API-05`, `RA-API-06`
- **Status** done
- **Deliverable** tests/thymira/test_api_contract.py: schema + status-code assertions for every endpoint (runs create/get/list, resume, events, audit, experiments, plan, approve, reject) via TestClient, including problem+json error bodies and pagination cursors.
- **Done when** just test runs it; each endpoint's success path and primary error path are asserted against the response schema.
- **Seam (why FINAL needs no refactor)** Pins the HTTP contract every client (CLI, web, SDK, MCP adapters) depends on; FINAL backends must keep this suite green, proving the API surface is stable across the MVP->FINAL swap.
- **Specified at** docs/architecture/e2e-baseline-v2.md:308

#### `RA-QA-03` — Durable-resume E2E against PostgreSQL + worker

- **Tier** FINAL · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `tests/thymira`
- **Depends on** `RA-CORE-11`, `RA-STATE-04`
- **Status** done
- **Deliverable** tests/thymira/test_e2e_durable.py (integration, slow): a full run against dockerized Postgres + QueueDispatcher/worker, killing the worker mid-run and asserting resume completes it exactly once.
- **Done when** Runs under just test-slow with compose services up; asserts no duplicated experiments/artifacts after resume and that verify_events passes on the reloaded log.
- **Seam (why FINAL needs no refactor)** Proves the MVP seams (checkpointer, idempotency journal, dispatcher, UnitOfWork) carry to the FINAL distributed backend with no domain change — the end-to-end verification of the owner's no-refactor constraint.
- **Specified at** docs/architecture/e2e-baseline-v2.md:676

#### `RA-STATE-01` — Run & Session repository protocols + local backend

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/state`
- **Depends on** none
- **Status** done
- **Deliverable** New module thymira.state.repositories with runtime_checkable Protocols RunRepository (save(Run), get(run_id)->Run\|None, list(*, project_id=None, status=None, limit, cursor)->Page[Run]) and SessionRepository (save/get/list); a frozen Page[T] result model (items, next_cursor); LocalRunRepository and LocalSessionRepository writing one canonical-JSON file per record under a per-run root dir.
- **Scope note — C-8 adoption is complete.** `thymira.state.repositories` (`RunRepository`/`SessionRepository` protocols and their local implementations) is implemented and tested (`tests/thymira/test_state_repositories.py`, `tests/thymira/test_repo_conformance.py`, `RA-STATE-05` done). `LocalRunStore` (ADR-0010) is the authoritative persistence layer beneath the control-plane and ordinary-progress paths, while these repository protocols remain stable backend/conformance seams. A PostgreSQL backend (`RA-STATE-04`) is FINAL work behind its own ADR (ADR-0010).
- **Done when** tests/thymira/test_state_repositories.py: save then get round-trips a Run and a Session; list() paginates by created_at with a stable opaque cursor; isinstance(LocalRunRepository(...), RunRepository) is True.
- **Seam (why FINAL needs no refactor)** The Protocols carry the full final method set (pagination cursor included) so RA-STATE-04's PostgreSQL backend is a drop-in; repository consumers depend on the Protocol, never a concrete repository class. Baseline §19 lists the runs/sessions tables the FINAL backend fills.
- **Specified at** docs/architecture/e2e-baseline-v2.md:700; docs/contracts/contract-v0.1.md:26

#### `RA-STATE-02` — Per-run stores: RecordRepository, EventStore, CheckpointRepository (protocols + local)

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `runtime/state`
- **Depends on** `RA-STATE-01`
- **Status** done
- **Deliverable** In thymira.state: RecordRepository Protocol storing/reading Agent, Task, ToolCall and Experiment records keyed by (run_id, id) with list_for_run(run_id, kind); EventStore Protocol (open(run_id)->EventLog, read(run_id)->list[Event]) with LocalEventStore wrapping the already-built JsonlEventLog; CheckpointRepository Protocol (put/get/list by thread_id) for LangGraph checkpoints; LocalRecordRepository and LocalCheckpointRepository. Artifacts are NOT stored here — they are read from the built ArtifactStore; AuditFinding/PolicyDecision are folded from the event log.
- **Scope note — per-run store foundation is complete.** The `RecordRepository`/`EventStore`/`CheckpointRepository` protocols and local implementations are implemented and tested (`tests/thymira/test_state_repositories.py`, `tests/thymira/test_repo_conformance.py`, `RA-STATE-05` done). `LocalRunStore` (ADR-0010) remains the persistence layer for Run state, and `runtime/tools`'s event-backed Experiment records are intentionally preserved by C-9. A PostgreSQL backend (`RA-STATE-04`) is FINAL work behind its own ADR (ADR-0010).
- **Done when** tests/thymira/test_state_repositories.py: three Experiments saved for a run come back in insertion order from list_for_run(run_id,'experiment'); EventStore.open(run_id) returns an append-able EventLog whose chain still verifies after a re-open; CheckpointRepository round-trips a checkpoint blob by thread_id.
- **Seam (why FINAL needs no refactor)** These Protocols are the seam; MVP reuses the built JsonlEventLog and local JSON files, FINAL swaps to Postgres tables (agents/tasks/tool_calls/experiments/events/checkpoints, baseline §19) with no caller change. Per-run event logs keep MIRA's hash chain intact across backends.
- **Specified at** docs/architecture/e2e-baseline-v2.md:700; docs/adr/0004-model-routing-and-agent-topology.md:89

#### `RA-STATE-03` — UnitOfWork: atomic multi-record run write

- **Tier** MVP · **Owner** P1 (Runtime / Tech Lead) · **Size** M · **Member path** `runtime/state`
- **Depends on** `RA-STATE-01`, `RA-STATE-02`
- **Status** done
- **Implemented foundation** docs/adr/0005-deepseek-harness-reuse.md (idea 9) records the design rationale; the UnitOfWork protocol, LocalUnitOfWork (temporary-file + atomic-rename), and tests are present.
- **Deliverable** thymira.state.unit_of_work.UnitOfWork context-manager Protocol grouping Run save + child-record saves + event appends so they commit together or not at all, exposing save_run/save_record/append_event; LocalUnitOfWork implementing all-or-nothing via write-to-temp + atomic rename, appending the log event last (open bracket first, close last — ADR-0005 idea 9).
- **Scope note — local atomic-write foundation is complete.** `thymira.state.unit_of_work` (`UnitOfWork` protocol and `LocalUnitOfWork` with temporary-file + atomic-rename rollback) is implemented and tested (`tests/thymira/test_state_uow.py`). Under ADR-0010 the MVP uses one writer per Run through `LocalRunStore`; `UnitOfWork` remains the tested transaction seam for multi-record writes and the future PostgreSQL transaction. A PostgreSQL backend (`RA-STATE-04`) is FINAL work behind its own ADR (ADR-0010).
- **Done when** tests/thymira/test_state_uow.py: an exception raised inside the with-block leaves the store byte-for-byte unchanged (no partial Run, no orphan event); a clean block persists every write and verify_events still passes.
- **Seam (why FINAL needs no refactor)** UnitOfWork is the transaction seam — MVP is filesystem-atomic, FINAL is a single Postgres transaction. RunService and graph nodes use this context manager from day 1, so wrapping writes in a real transaction later needs no call-site edit (retrofitting it would be a refactor).
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:108

#### `RA-STATE-04` — PostgreSQL implementations of all repository protocols + migrations

- **Tier** FINAL · **Owner** P1 (Runtime / Tech Lead) · **Size** L · **Member path** `runtime/state`
- **Depends on** `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`, `RA-STATE-05`
- **Status** done
- **Deliverable** thymira.state.postgres package: SQLAlchemy tables + Alembic migrations for sessions, runs, agents, tasks, tool_calls, experiments, audit_findings, policy_decisions, events, checkpoints (baseline §19); PgRunRepository, PgSessionRepository, PgRecordRepository, PgEventStore, PgCheckpointRepository and PgUnitOfWork implementing the RA-STATE protocols; connection/pool config from env.
- **Done when** tests/thymira/test_state_postgres.py (integration, slow): the RA-STATE-05 conformance suite that passes for the local backends passes unchanged for the Pg backends against a dockerized Postgres; event append preserves the hash chain and verify_events passes after reload.
- **Seam (why FINAL needs no refactor)** Drop-in for the RA-STATE-01/02/03 protocols with zero caller changes; substitutability is proven by running the identical conformance suite against both backends.
- **Specified at** docs/architecture/e2e-baseline-v2.md:700

#### `RA-STATE-05` — Backend-agnostic repository conformance suite

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `tests/thymira`
- **Depends on** `RA-STATE-01`, `RA-STATE-02`, `RA-STATE-03`
- **Status** done
- **Deliverable** tests/thymira/repo_conformance.py: a pytest suite parametrized over a repository-factory fixture, asserting the RunRepository, SessionRepository, RecordRepository, EventStore, CheckpointRepository and UnitOfWork contracts (round-trip, pagination, ordering, atomicity, chain integrity). Imports only the Protocols, never a concrete backend.
- **Done when** just test runs the suite against the local backends and it passes; the module contains no import of thymira.state.postgres or any concrete class name.
- **Seam (why FINAL needs no refactor)** The conformance suite is the executable definition of the persistence seam; RA-STATE-04's Postgres backends must pass the identical suite, which is how the 'no refactor' guarantee is proven mechanically rather than asserted.
- **Specified at** docs/architecture/e2e-baseline-v2.md:700

### THY and its agents

*33 tasks — 20 MVP, 13 FINAL.*

#### `THY-01` — AgentSpec: sub-agent declaration as data + catalog loader

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** none
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/spec.py: AgentSpec(ThymiraModel){name, role(routing.Role), task_kinds(tuple[TaskKind]), tool_allowlist(tuple[str]), tier(ModelTier\|None request), max_turns:int, max_depth:int, system_prompt_ref:str, output_schema_ref:str}; AgentCatalog with get(name)/names(); load_agent_specs(dir: Path, \*, known_capabilities: frozenset[str]) reading YAML declarations and refusing a spec whose tool_allowlist names a capability outside that set. The set is passed in, never hard-coded: callers build it from the populated ToolRegistry (`{tool.capability.id for tool in registry}` — `thymira.agents` already depends on `thymira-tools`), so adding a tool never edits this module. `system_prompt_ref` and `output_schema_ref` are keys into the catalog directory, resolved by the loader into the prompt text and the pydantic output type; the spec stores the ref, the catalog exposes the resolved value. Full final shape; no field added later.
- **Done when** uv run pytest tests/thymira/test_agents_spec.py — loads a catalog of >=3 specs, asserts every spec carries role/task_kinds/tool_allowlist/max_turns/max_depth; load_agent_specs(dir, known_capabilities=frozenset({...})) raises at load time for a spec whose tool_allowlist names a capability outside that set, and the same directory loads cleanly when the set contains it.
- **Seam (why FINAL needs no refactor)** This IS the seam ADR-0004 dec.4 fixes ('sub-agents declared as data ... loaded, never hard-coded'). The full field set (incl. max_turns/max_depth/tier request) exists from MVP so adding the FINAL agents (THY-18..22) and per-project overlay (THY-27) is pure data, no code change. Lives in runtime/agents because `thymira.mira` sits above `thymira.agents`
in the layer order, so MIRA may import from it.
- **Scope note — `AgentSpec` is not `AuditAgentSpec`.** An earlier version of this seam said MIRA
  "reuses the identical type". It does not: `GOV-01` ships `thymira.mira.agents.spec.AuditAgentSpec`
  with a different field set (`framework` instead of `role`, `system_prompt` inline instead of
  `system_prompt_ref`, no `max_depth`, no `output_schema_ref`) and its own `load_specs`. Both tasks
  are unblocked at once, so build to the field set written *here* and let P4 own `AuditAgentSpec`
  separately; do not widen either type to serve the other. If the two are ever unified, the shared
  base belongs in `thymira.agents` and `AuditAgentSpec` extends it — never the reverse, which the
  layer order forbids.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:56

#### `THY-02` — PydanticAI model binding over the router + LLMProvider (records model.selected)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/agents`
- **Depends on** `THY-01`
- **Status** done
- **Already in the repository** runtime/agents/src/thymira/agents/llm/routing.py and runtime/agents/src/thymira/agents/model_binding.py, with coverage in tests/thymira/test_agents_model_binding.py. `model_binding.py` provides the `routed_model` factory — a `pydantic_ai` model built on `FunctionModel` that routes each request through `routing.choose`, appends a `model.selected` event before the call, and carries `LLMResponse` cost/tokens back into pydantic_ai usage; pydantic-ai-slim is imported here, not merely declared. The ADR-0001 open point shipped as a `FunctionModel` adapter over the existing `LLMProvider`, not a `RoutedModel` subclass of `pydantic_ai.models.Model` as the Deliverable below still phrases it.
- **Deliverable** runtime/agents/src/thymira/agents/model_binding.py: decide ADR-0001 open point (PydanticAI Model adapter over the existing LLMProvider, NOT a raw LiteLLM proxy, so cost/model.selected stay ours); RoutedModel implementing pydantic_ai.models.Model that, per request, calls routing.choose(role, task_kind), builds the provider via provider_for, and appends a model.selected event (ModelChoice.event_payload) before the call; carries LLMResponse cost/tokens back into pydantic_ai usage.
- **Done when** uv run pytest tests/thymira/test_agents_model_binding.py — with an injected ScriptedProvider, one agent request for (role=agent, task='code') appends exactly one model.selected event whose tier_applied respects the AGENT floor; no network call; ADR decision recorded in docs/adr as a short note.
- **Seam (why FINAL needs no refactor)** Closes the ADR-0001 dec.4 'open integration point'. The seam is: agents depend on RoutedModel, never on LiteLLM directly, so swapping proxy-vs-adapter or vendor is an internal change. model.selected is emitted here in MVP shape, so FINAL never needs to add per-call routing evidence.
- **Specified at** docs/adr/0001-harness-and-two-layer-architecture.md:40

#### `THY-03` — AgentRunner: generic spec-driven agent execution loop

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/agents`
- **Depends on** `THY-02`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/runner.py: AgentRunner.run(spec: AgentSpec, task: AgentTask, ctx: AgentContext) -> AgentResult; builds a PydanticAI Agent from the spec + RoutedModel + isolated context (seeded only by the task, not the full thread), drives the step loop bounded by spec.max_turns, returns the output type the catalog resolved from spec.output_schema_ref via complete_structured (the spec stores the ref, not the type — `spec.output_schema` does not exist, see `THY-01`), and appends agent.started/agent.completed; AgentResult{output(BaseModel), usage, task_status}. A step = one model request plus its tool calls (ADR-0005 idea 11).
- **Done when** uv run pytest tests/thymira/test_agents_runner.py — running a spec against a ScriptedProvider returning a valid structured output appends agent.started then agent.completed, returns the validated model, and a run exceeding max_turns ends with Task FAILED (never a silent drop).
- **Seam (why FINAL needs no refactor)** The uniform spoke interface every specialist agent is driven through. MVP runs 3 agents, FINAL runs 8 through the SAME runner. Each step is keyed by a hash of its inputs so P1's resume/idempotency (ADR-0004) replays without a runner change. Isolated-context seeding is the MVP seam so FINAL compaction (THY-25) only shrinks what the runner already reads.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:110

#### `THY-04` — Tool bridge: agent tool-calling through the Tool Manager + Gate under an allowlist

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** `THY-03`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/tool_bridge.py: build_agent_tools(spec, ToolRegistry, ToolContext) exposing ONLY spec.tool_allowlist as PydanticAI tools, each delegating to ToolManager.execute (which gates via Gate.check_capability and is fail-closed); a tool outside the allowlist is never registered, and a denied capability surfaces to the model as a bounded error, not an exception.
- **Done when** uv run pytest tests/thymira/test_agents_tool_bridge.py — an agent whose allowlist omits 'run_python' has no such tool and a denied capability yields tool.denied on the log; an allowed tool routes through ToolManager producing tool.started/tool.completed; the model never touches subprocess/filesystem directly.
- **Seam (why FINAL needs no refactor)** Enforces the invariant 'every tool call goes through the Tool Manager + Permission Policy'. The allowlist-as-data seam means FINAL agents gain tools by editing their AgentSpec, never the bridge. Consumes P3's ToolManager/registry and P4's Gate unchanged.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:91

#### `THY-05` — PromptBuilder over current_surface + prefix-stable system-prompt catalog

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** `THY-03`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/prompts.py: PromptBuilder.build(spec, task, events) -> AssembledPrompt{system:str, user:str, surface_seqs:tuple[int]} reading ONLY current_surface(events) for history; SystemPromptCatalog loading role/agent system prompts from runtime/thy/.../prompts/*.md (files, not string literals), assembled prefix-first so a stable prefix precedes the variable tail for LiteLLM provider caching.
- **Done when** uv run pytest tests/thymira/test_agents_prompts.py — the assembled history excludes an event that a context.compacted has shadowed (uses current_surface); the system prefix is byte-identical across two builds with different tasks (prefix stability); prompts resolve from files.
- **Seam (why FINAL needs no refactor)** ADR-0006's consequence 'a prompt builder now has a defined input: current_surface(events)'. Reading the derived projection from MVP is the seam that makes compaction (THY-25) a drop-in — the builder never invents its own notion of history. Prefix stability is the seam for ADR-0004 fan-out cache sharing (THY-23).
- **Specified at** docs/adr/0006-log-vs-surface-and-compaction.md:106

#### `THY-06` — Request/prompt provenance: record what the model actually saw

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** `THY-05`, `THY-02`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/prompt_provenance.py: record_prompt(ctx, assembled, choice) persisting the exact credential-scrubbed rendered prompt as a content-addressed artifact (ArtifactKind.LOG via the verified private ArtifactStore, sha256) and enriching the model.selected payload with {prompt_sha256, surface_seqs}; never persists model reasoning/chain-of-thought; wired into AgentRunner so every model call is covered.
- **Done when** uv run pytest tests/thymira/test_agents_prompt_provenance.py — after one agent call a LOG artifact holding the exact credential-scrubbed prompt exists with a matching sha256, the model.selected payload references that sha256 and the surface_seqs used, and a known provider credential embedded in the prompt is absent while model-visible PII remains available only under the restricted local store. API/export/trace copies are redacted projections.
- **Seam (why FINAL needs no refactor)** This is the domain's core auditability deliverable ('what the model actually saw'), distinct from core's run-environment provenance. Recording the envelope (surface_seqs + prompt digest) in MVP shape is the exact slot ADR-0006's compaction envelope (THY-25) and MIRA's 'was the summary faithful?' control plug into with no contract change; reuses existing artifact.created + model.selected.
- **Specified at** docs/adr/0006-log-vs-surface-and-compaction.md:74

#### `THY-07` — RunUsage / UsageLimits: shared budget charged per delegated call

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** `THY-03`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/usage.py: frozen UsageLimits{max_cost_usd, max_requests, max_tokens, max_tool_calls}, **the single definition of that type for the whole runtime** (`RA-CORE-03`'s `UsageLedger.exceeds()` imports it from here; `thymira.agents` is below `thymira.core`, so this is the only direction that works); RunUsage accumulator with charge(LLMResponse)/charge_tool(); UsageLimitExceeded; AgentRunner charges every call against the shared RunUsage (PydanticAI usage=ctx.usage) and raises at the hard ceiling so the graph stops cleanly. If `RA-CORE-03` landed first it already created the module with `UsageLimits` in it — extend that file, do not replace the type.
- **Done when** uv run pytest tests/thymira/test_agents_usage.py — per-call cost_usd/tokens sum into RunUsage; a run that crosses max_cost_usd raises UsageLimitExceeded and no further model call is made; a run with different per-agent models still aggregates correctly (cost taken from LLMResponse, not token guesses).
- **Seam (why FINAL needs no refactor)** ADR-0004 dec.6: budgets shared and enforced. The RunUsage/UsageLimits type + per-call charging is the seam; MVP enforces only the hard ceiling. FINAL soft-limit WARNING / past-hard REQUIRE_HUMAN_REVIEW are Policy Engine rules (P4) reading the same RunUsage, and the aggregating usage ledger (ADR-0002 backlog) folds it — neither changes this type.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:66

#### `THY-08` — Delegation contract: hub-and-spoke agent.message + depth guard

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/agents`
- **Depends on** `THY-03`
- **Status** done
- **Deliverable** runtime/agents/src/thymira/agents/delegation.py: Delegator.delegate(parent_agent, spec, objective, depth) creating a Task, running it via AgentRunner, and recording the returned outcome as an agent.message event (structured summary only, never chain-of-thought); DepthGuard enforcing orchestrator->sub-agent->helper (max_depth); rejects any peer-to-peer or over-depth call.
- **Done when** uv run pytest tests/thymira/test_agents_delegation.py — a completed sub-agent produces one agent.message with a summary field and no reasoning; a delegation deeper than max_depth is rejected; there is no code path for a sub-agent to message a sibling directly.
- **Seam (why FINAL needs no refactor)** ADR-0004 dec.5. agent.message + the hub-and-spoke topology + depth cap are fixed in MVP so FINAL agents that themselves delegate to helpers (e.g. ML agent) are drop-ins. THY and MIRA never exchange messages here — MIRA reaches THY only via policy.decision (THY-24).
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:60

#### `THY-09` — ThyState + ThyGraph scaffold (Inspect -> Plan -> Execute -> Summarize)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-03`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/state.py: ThyInput, ThyState (phase, plan: tuple[AgentTask], completed, agent_messages, usage: RunUsage, policy_signals for rework, surface source), ThyOutput, AgentTask; runtime/thy/src/thymira/thy/graph.py: build_thy_graph() -> LangGraph StateGraph with the four nodes and edges, plus graph_definition_hash(); add langgraph to runtime/thy/pyproject.toml + uv.lock.
- **Done when** uv run pytest tests/thymira/test_thy_graph.py — build_thy_graph() compiles; a run driven by scripted agents traverses Inspect->Plan->Execute->Summarize and yields a ThyOutput; graph_definition_hash() is stable and changes when a node is added. `just check-imports` still passes.
- **Seam (why FINAL needs no refactor)** ADR-0004 dec.3: ThyGraph is a real LangGraph graph from MVP (sequential control flow), so FINAL parallel fan-out (THY-23) and the rework loop (THY-24) only add edges/nodes — no rewrite. build_thy_graph() is consumed and composed with MiraGraph by runtime/core (P1), which injects the checkpointer (thymira.state); graph_definition_hash() is the value P1 records at run.started next to policy_sha256.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:50

#### `THY-10` — Inspect node + project-context loader

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-09`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/context.py: load_project_context(project_dir) parsing .thymira/config.yaml into ProjectConfig and reading .thymira/context.md; runtime/thy/src/thymira/thy/nodes/inspect.py: inspect_node seeding ThyState with domain, governance frameworks, project context and dataset facts (via the Data agent / tool bridge) before planning.
- **Done when** uv run pytest tests/thymira/test_thy_inspect.py — against examples/credit-risk, the node loads ProjectConfig (domain credit_risk, frameworks EU_AI_ACT+CREDIT_RISK) and populates ThyState with the context; a project without .thymira degrades to an empty-but-valid context.
- **Seam (why FINAL needs no refactor)** Consumes the frozen ProjectConfig type. The loader boundary is the seam for THY-27's project agents.yaml overlay and keeps THY's project-awareness (the baseline differentiator, section 14) inside one function so richer context (schemas, lineage) is additive.
- **Specified at** docs/architecture/e2e-baseline-v2.md:570

#### `THY-11` — Plan node + plan approval through the Gate

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-09`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/nodes/plan.py: plan_node calling THY (FRONTIER, task='plan') to produce an ordered plan of AgentTasks tagged with Phase; then routing the plan through Gate.check_action(action_type='plan.proposed') and allows_execution(decision) so it becomes policy.decision -> (human.approval when REQUIRE_HUMAN_REVIEW) before Execute; a plan the decision does not allow to execute halts the graph.
- **Scope note — the plan checkpoint uses `check_action` + `allows_execution`, exactly like a tool call (correction C-15).** `plan_node` calls `gate.check_action(subject_kind='run', subject_id=..., action_type='plan.proposed', payload=..., summary='plan.proposed')`, then trusts the decision through `allows_execution(decision)` with no approval object — the same single rule `ToolManager` uses (C-9). It advances to Execute only when that returns `True` (`PASS`/`WARNING`), and appends the decision to `state.policy_signals` either way. Under the shipped default policy (no `ActionRule` for `plan.proposed`, hence `REQUIRE_HUMAN_REVIEW`) the graph halts at Plan whatever the `Gate`'s synchronous approver answers; that answer is recorded as a `human.approval` event, evidence, never authority. A plan is a proposal with no effect of its own, and every tool it later runs is gated independently the same way. An asynchronous, checkable plan approval is `HITL-01` work.
- **Done when** uv run pytest tests/thymira/test_thy_plan.py — plan_node yields >=1 phase-tagged AgentTask and appends a policy.decision; with a policy ActionRule that PASSes plan.proposed the graph proceeds through Execute to Summarize; under the default policy a synchronous approver never advances the review-gated plan; with a rejecting approver the graph does not enter Execute.
- **Seam (why FINAL needs no refactor)** ADR-0004: 'THY's plan is a policy.decision -> human.approval before execution' — the plan-through-Gate seam exists in MVP even if the P4 rule defaults to PASS, so FINAL stricter plan rules need no THY change. Gate/PolicyEngine are owned by P4 and injected via state.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:90

#### `THY-12` — Execute node: sequential dispatch of the plan to specialist agents

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-11`, `THY-08`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/nodes/execute.py: execute_node iterating the approved plan in dependency order, resolving each AgentTask's agent from AgentCatalog, invoking it via Delegator/AgentRunner, and recording Task outcomes into ThyState; a FAILED task is kept as Task FAILED and handed to a decision point (retry/degrade/stop), never dropped.
- **Done when** uv run pytest tests/thymira/test_thy_execute.py — three scripted AgentTasks run in order and complete; a task whose depends_on failed is SKIPPED; a failing task is recorded FAILED and the node routes to the failure branch rather than continuing silently.
- **Seam (why FINAL needs no refactor)** MVP dispatches sequentially; the node contract (iterate plan -> Delegator per AgentTask -> collect Task outcomes) is exactly what THY-23 parallelizes via LangGraph fan-out/fan-in, so parallelism is an edge change, not a rewrite. Depends_on ordering is honored from MVP.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1581

#### `THY-13` — Summarize node: model comparison + scientific recommendation + report artifact

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-12`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/nodes/summarize.py: summarize_node calling THY (FRONTIER, task='synthesize') to produce Recommendation{best_model, metrics, tradeoffs, limitations}; compare_experiments(runs) ranking by the configured metric; writes analysis.md (ArtifactKind.REPORT) via ArtifactStore and populates ThyOutput.
- **Done when** uv run pytest tests/thymira/test_thy_summarize.py — given two completed Experiments the node picks best_model by metric, emits a Recommendation carrying tradeoffs+limitations, and produces an analysis.md artifact with a sha256 in the manifest.
- **Seam (why FINAL needs no refactor)** Recommendation is the FINAL output shape (best_model/metrics/tradeoffs/limitations from roadmap week 2). compare_experiments takes a list so adding experiments (THY-23/parallel) needs no signature change; the artifact-by-digest path is the seam MIRA reads as evidence.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1976

#### `THY-14` — Data agent (inspect + profile dataset)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-04`, `THY-05`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/data.py: AgentSpec 'data-agent' (role AGENT, tools: list_files/read_file/run_python, tiers analyze/extract) + DataProfile output schema (columns, dtypes, missing, target candidates) + prompts/data.md.
- **Done when** uv run pytest tests/thymira/test_thy_agent_data.py — with a ScriptedProvider and a fixture CSV under tmp_path, the agent returns a DataProfile naming columns, dtypes and missing-value counts, calling only allowlisted tools.
- **Seam (why FINAL needs no refactor)** Pure AgentSpec + output schema + prompt on the THY-01/03/04 framework; adding it is data, proving the drop-in pattern the FINAL agents (THY-18..22) follow.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1546

#### `THY-15` — Coding/Execution agent (generate + execute Python, read result)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-04`, `THY-05`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/coding.py: AgentSpec 'coding-agent' (tools: run_python/read_file/write_file/git_diff, tiers code) + CodeResult output schema (stdout, artifacts, exit_code) + prompts/coding.md; drives the write->run->read tool loop via AgentRunner.
- **Done when** uv run pytest tests/thymira/test_thy_agent_coding.py — the agent writes a script, runs it through run_python (tool.started/tool.completed on the log), and returns a CodeResult with the captured stdout and any artifact ids; no direct subprocess use.
- **Seam (why FINAL needs no refactor)** Uses the tool bridge; the run_python tool's sandbox mode/enforcement is P3's fact on ToolResult (ADR-0005 idea 6) and reaches THY unchanged, so confining execution later needs no coding-agent change.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1557

#### `THY-16` — Experiment agent (create experiment, log metrics/params, save artifacts)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-04`, `THY-05`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/experiment.py: AgentSpec 'experiment-agent' (tools: mlflow_start_run/log_param/log_metric/log_artifact/end_run, run_python) + ExperimentResult output schema mapping to the Experiment contract (parameters, metrics, seed, model_artifact_id, tracker_run_id) + prompts/experiment.md.
- **Done when** uv run pytest tests/thymira/test_thy_agent_experiment.py — with scripted MLflow tools the agent runs one experiment and returns an ExperimentResult whose fields populate an Experiment record (metrics + tracker_run_id + seed).
- **Seam (why FINAL needs no refactor)** Emits into the frozen Experiment contract via P3's MLflow tools; the ExperimentResult->Experiment mapping is the seam so running many experiments (THY-23) and richer tracking are additive.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1570

#### `THY-17` — Coding agent error recovery (diagnose failed run, bounded retry)

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-15`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/coding.py (extend): on a run_python failure, feed stderr/exit_code back to the model for a diagnose-and-fix step and retry once within max_turns; a persistent failure returns Task FAILED with the diagnosis in the summary.
- **Done when** uv run pytest tests/thymira/test_thy_agent_coding_recovery.py — a first scripted run_python fails, the agent diagnoses and a second scripted run succeeds and the Task completes; a run that fails twice ends FAILED with a structured diagnosis, not an exception.
- **Seam (why FINAL needs no refactor)** Roadmap week-3 hardening (Python failure -> diagnose -> retry -> continue). Reuses AgentRunner's turn loop; the FINAL multi-strategy self-repair / evaluator-optimizer (THY-24) extends this without changing the failure-record shape.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2299

#### `THY-18` — Statistics agent

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-14`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/statistics.py: AgentSpec 'statistics-agent' (tools: run_statistics/run_python) + StatsResult output schema (tests, effect sizes, assumptions, caveats) + prompts/statistics.md.
- **Done when** uv run pytest tests/thymira/test_thy_agent_statistics.py — with a ScriptedProvider the agent returns a StatsResult naming the test performed, the statistic, and stated assumptions; only allowlisted tools are used.
- **Seam (why FINAL needs no refactor)** Drop-in on the THY-01/03 framework: an AgentSpec + output schema + prompt, registered in AgentCatalog, needing no runner/graph change. Baseline agent list, section 2.
- **Specified at** docs/architecture/e2e-baseline-v2.md:44

#### `THY-19` — ML agent (model selection, training, tuning; may delegate to helpers)

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-08`, `THY-16`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/ml.py: AgentSpec 'ml-agent' (tools: run_python/mlflow_*; max_depth allows one helper) + MLResult output schema (chosen family, hyperparameters, cv strategy, metrics) + prompts/ml.md.
- **Scope note — the deliverable names an `AgentSpec` and a schema; it does not name who decides to delegate.** `AgentRunner`'s tool-calling loop only ever exposes `spec.tool_allowlist`; there is no path for a model to call `Delegator` itself mid-turn, so "the agent delegates" cannot literally mean the model invokes a tool named `delegate`. `MLResult` grew two fields no other specialist output has (`needs_tuning_help: bool`, `tuning_objective: str | None`) so the model can *signal* the need; a new orchestration wrapper, `run_ml_agent` (not in the deliverable's file list, added in the same module), reads that signal *after* the primary run completes and calls `Delegator.delegate` itself — the same shape `execute_node` already uses to turn `PlanOutput` into delegation, applied one level deeper. A second `AgentSpec`, `ML_TUNING_HELPER_SPEC` (`max_depth=2`, `run_python` only), is the delegate target; both specs share one `AgentCatalog` (`ml_agent_catalog`) since `Delegator.delegate`'s inner `AgentRunner.run` resolves the helper by name against the caller's own catalog. The helper's `TuningResult` is recorded only as the `agent.message` event; it is not merged back into the returned `MLResult`, which stays exactly what the primary run reported.
- **Done when** uv run pytest tests/thymira/test_thy_agent_ml.py — the agent produces an MLResult and, when it delegates a tuning sub-task, the depth guard permits exactly one helper level and records the exchange as agent.message.
- **Seam (why FINAL needs no refactor)** First agent to exercise sub-agent->helper delegation; relies on THY-08's max_depth seam built in MVP, so no delegation change is needed to add it.
- **Specified at** docs/architecture/e2e-baseline-v2.md:44

#### `THY-20` — Data Quality agent

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-14`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/data_quality.py: AgentSpec 'data-quality-agent' (tools: run_python/read_file) + DataQualityReport output schema (leakage risks, imbalance, drift signals, sensitive-attribute presence) + prompts/data_quality.md.
- **Done when** uv run pytest tests/thymira/test_thy_agent_data_quality.py — the agent returns a DataQualityReport flagging a leakage risk and a declared sensitive attribute on a scripted fixture.
- **Seam (why FINAL needs no refactor)** THY-side quality checks feeding evidence MIRA later audits (distinct from MIRA's own A-controls); drop-in AgentSpec, no framework change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:44

#### `THY-21` — Visualization agent

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-14`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/visualization.py: AgentSpec 'visualization-agent' (tools: run_python/write_file) + PlotResult output schema (artifact ids + captions) + prompts/visualization.md; plots persist as ArtifactKind.PLOT.
- **Scope note — `write_file` gained an optional `kind` argument to make this reachable.** Neither `run_python` nor `write_file` called `ArtifactStore.save_bytes` on their own, so with only those two tools this task's own "Done when" (a real `PLOT` artifact with a sha256 in the manifest) was not reachable — the `ToolManager`'s artifact.created diff (`TOOL-04`) only fires when a tool calls `artifact_store.save_*` itself. `WriteFileArguments` (`thymira.tools.builtins.files`) grew an optional `kind: ArtifactKind | None`; passed, `WriteFile.execute` also registers the written bytes as an `Artifact` and returns its id in `ToolResult.artifact_ids`. A plain `write_file` call (no `kind`) is unchanged. This is the smallest change that keeps `run_python`/`write_file` as the only allowlisted tools, as specified.
- **Done when** uv run pytest tests/thymira/test_thy_agent_visualization.py — the agent produces >=1 PLOT artifact with a sha256 in the manifest and a caption in PlotResult.
- **Seam (why FINAL needs no refactor)** Uses the existing Artifact/ArtifactStore contract (ArtifactKind.PLOT already defined); drop-in AgentSpec.
- **Specified at** docs/architecture/e2e-baseline-v2.md:44

#### `THY-22` — Research agent (web/search + Knowledge Base)

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-14`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/agents/research.py: AgentSpec 'research-agent' (tools: search/kb_query) + ResearchResult output schema (claims with source citations) + prompts/research.md.
- **Done when** uv run pytest tests/thymira/test_thy_agent_research.py — with scripted search/KB tools the agent returns a ResearchResult whose every claim carries a source reference; no unallowlisted tool is used.
- **Seam (why FINAL needs no refactor)** Drop-in AgentSpec; consumes the KB/search tools (owned by P3/P1) through the tool bridge, so the KB backend can arrive later without a research-agent change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:44

#### `THY-23` — Parallel fan-out execution in the Execute node

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-12`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/nodes/execute.py (extend): LangGraph fan-out over independent AgentTasks with a fan-in barrier before Summarize, honoring depends_on and the run concurrency cap; shared prompt prefixes preserved for provider cache sharing.
- **Done when** uv run pytest tests/thymira/test_thy_execute_parallel.py — two independent AgentTasks execute concurrently and both complete before Summarize; a dependent task still waits for its predecessor; results match the sequential run.
- **Seam (why FINAL needs no refactor)** Pure addition of fan-out/fan-in edges over THY-12's node contract and THY-08's AgentTask/Delegator; no change to agents, runner, or state shape. Enables background/parallel experiments (baseline section 16).
- **Specified at** docs/architecture/e2e-baseline-v2.md:607

#### `THY-24` — THY <-> MIRA evaluator-optimizer rework loop

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-13`, `THY-11`
- **Status** done
- **Already in the repository** runtime/core/src/thymira/core/phases.py. Still missing: wiring can_reopen into ThyGraph, nodes/rework.py, reopen-budget fields on ThyState, policy.decision-triggered re-entry, and the acceptance test
- **Deliverable** runtime/thy/src/thymira/thy/graph.py + nodes/rework.py: on a policy.decision requiring rework, re-enter ThyGraph, use can_reopen(current, target, reopen_count, max_reopens, justification) to reopen a phase, and re-execute the affected tasks; reopen budget tracked in ThyState.
- **Done when** uv run pytest tests/thymira/test_thy_rework.py — a rework-signalling policy.decision reopens PREPARATION/MODELING (never a non-reopenable phase), re-runs the tasks, and a run over max_reopens stops with a budgeted, justified refusal rather than looping.
- **Seam (why FINAL needs no refactor)** MIRA reaches THY only as a policy.decision (ADR-0004 dec.5), so the seam is a policy signal in ThyState (present from MVP) + the already-built can_reopen phase machine; MVP simply never triggers a reopen. No new THY<->MIRA channel.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:94

#### `THY-25` — Context compaction engine (context.compacted, atomic, envelope-logged)

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-05`, `THY-06`
- **Status** done
- **Already in the repository** packages/events/src/thymira/events/surface.py. Still missing: compaction.py itself (compact_surface, budget-driven summarisation, envelope with provider/model/tier/token counts), and the acceptance test
- **Deliverable** runtime/thy/src/thymira/thy/compaction.py: compact_surface(events, budget) summarizing shadowable model_visible events and appending context.compacted with shadowed_seqs + a summarisation envelope (provider, model, tier, token counts) and only the safe summary projection; never persists raw provider output/chain-of-thought. The append is the **last** step: summarise first, then append once, so a crash before the append leaves the surface untouched and a crash after it leaves a complete compaction. Emit no opening event — see the scope note.
- **Done when** uv run pytest tests/thymira/test_thy_compaction.py — after compaction current_surface() shrinks, derive_surface marks the named seqs SHADOWED, verify_events() still passes (chain intact), the envelope records model/tier/tokens, and the raw provider text is absent from the log.
- **Seam (why FINAL needs no refactor)** ADR-0006 dec.5 / ADR-0005 idea 2: the Event.surface field, current_surface fold and context.compacted type already exist, and THY-05/06 already read/record the surface — so the engine appends records only; nothing in the prompt path is refactored. MVP never compacts (the log/surface distinction is the pre-built seam).
- **Scope note** [ADR-0007](../adr/0007-compaction-is-atomic-no-bracket.md) (Accepted) withdrew the start/end compaction bracket: one append is atomic by construction, so there is no unterminated state and no opening event. `EventType` has exactly one compaction value, and adding a second is a contract change, not an implementation detail.
- **Specified at** docs/adr/0006-log-vs-surface-and-compaction.md:74

#### `THY-26` — Token budgeting / context-window management

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-25`
- **Status** done
- **Already in the repository** runtime/agents/src/thymira/agents/llm/base.py. Still missing: budget.py itself (estimate_prompt_tokens, ContextBudget, pre-call trigger into compact_surface, measured/estimated flag), and the acceptance test
- **Deliverable** runtime/thy/src/thymira/thy/budget.py: estimate_prompt_tokens(assembled, model) and a ContextBudget that decides when the projected prompt exceeds the model window and triggers compact_surface before the call; token counts flagged measured-vs-estimated (ADR-0005 idea 4).
- **Done when** uv run pytest tests/thymira/test_thy_budget.py — when the projected prompt exceeds the model budget, compaction runs before the model call and the subsequent prompt fits; a measurement carries its baseline (usage\|estimated).
- **Seam (why FINAL needs no refactor)** Trigger for THY-25; hooks into the AgentRunner's pre-call step (THY-03), which in MVP simply never fires. Measurement-declares-its-confidence (ADR-0005 idea 4) is the house rule the token counts follow.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:103

#### `THY-27` — Project-level agents.yaml overlay

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-01`, `THY-10`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/context.py (extend) + spec loader: merge .thymira/agents.yaml over the built-in AgentCatalog (add/override specs: tool_allowlist, tier request, prompt ref) with validation, so a project tailors THY's agents without code.
- **Done when** uv run pytest tests/thymira/test_thy_agents_overlay.py — a project agents.yaml that overrides an agent's tier request and adds a new agent yields a merged catalog reflecting both, and an invalid overlay is rejected at load.
- **Seam (why FINAL needs no refactor)** AgentSpec-as-data (THY-01) and the project-context loader (THY-10) are the MVP seams; the overlay is additive layering, no runner/graph change. Baseline section 15 lists .thymira/agents.yaml.
- **Specified at** docs/architecture/e2e-baseline-v2.md:578

#### `THY-28` — Run recipes as data (.thymira/recipes/<name>.yaml)

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** L · **Member path** `runtime/thy`
- **Depends on** `THY-09`, `THY-07`
- **Status** done
- **Deliverable** runtime/thy/src/thymira/thy/recipes.py: load_recipe(path) selecting/parameterizing the graph, phase list, agent subset and per-run budgets/policy refs; a recipe drives a run with args, recorded in the run's provenance.
- **Done when** uv run pytest tests/thymira/test_thy_recipes.py — a recipe selecting a two-experiment phase plan and a token budget produces a run that uses exactly those agents/phases and enforces that budget.
- **Seam (why FINAL needs no refactor)** ADR-0004 explicitly post-MVP ('run recipes as data'). Recipes only compose existing pieces (graph builder, AgentCatalog, UsageLimits), so they add a config layer without touching the graph/runner. Budget/policy portions reference P4/P1-owned types unchanged.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:93

#### `THY-29` — CLI: `thymira plan` (produce and show the plan without executing)

- **Tier** FINAL · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `THY-11`
- **Status** done
- **Deliverable** adapters/cli/src/thymira/cli/commands/plan.py: `thymira plan "<prompt>"` calling the API plan() verb and rendering the phase-tagged AgentTask plan (and its policy.decision) without entering Execute; thin API client, no THY logic.
- **Done when** uv run pytest tests/thymira/test_cli_plan.py (integration) — `thymira plan "..."` prints the ordered plan with phases and exits 0 having created no experiments/artifacts.
- **Seam (why FINAL needs no refactor)** Surfaces the Agent API plan() verb (baseline section 6). MVP already produces+approves the plan inside run (THY-11); this command just renders it, so no runtime change is needed to add it.
- **Specified at** docs/architecture/e2e-baseline-v2.md:216

#### `THY-30` — CLI: render THY activity (plan, delegation tree, per-agent tokens/cost)

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `adapters/cli`
- **Depends on** `THY-07`, `THY-08`
- **Status** done
- **Already in the repository** apps/api/src/thymira/api/schemas.py. Still missing: actual event-log folding into phases/agent-tree/tokens, the GET /runs/{id}/events route + CLI client call, and the acceptance test
- **Deliverable** adapters/cli/src/thymira/cli: fold the run's events into a view showing phases, the agent delegation tree (from agent.started/agent.message), and per-agent tokens/cost (from model.selected) in `thymira run`/`thymira status`; Rich output under UTF-8.
- **Done when** uv run pytest tests/thymira/test_cli_thy_view.py — given a recorded event log the CLI renders each phase, the agent tree and a per-agent token/cost line; totals match the summed model.selected/usage.
- **Seam (why FINAL needs no refactor)** ADR-0004: 'the Event API + thymira status render phases, agents, tokens and cost from the thread'. Renders purely from the event log (no THY coupling), so more agents/phases appear automatically as FINAL agents are added.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:88

#### `THY-31` — E2E: THY completes the data-science workflow (happy path + failed run)

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `adapters/cli`
- **Depends on** `THY-13`, `THY-17`
- **Status** done
- **Deliverable** tests/thymira/test_thy_e2e.py (integration marker): drive a full THY run over examples/credit-risk with ScriptedProvider + fake tools, asserting Inspect->Plan->Execute->Summarize produces a Recommendation and artifacts (analysis.md, metrics, model), plus a second scenario where a run_python failure is diagnosed/retried and the run still completes or fails cleanly.
- **Done when** uv run pytest -m integration tests/thymira/test_thy_e2e.py — the happy path yields a ThyOutput with a Recommendation and >=3 artifacts and a verifiable event log; the failure scenario ends in a clean FAILED/recovered state, never a hang or silent drop.
- **Seam (why FINAL needs no refactor)** Roadmap day-11 hardening (1 happy path, 1 failed execution). Uses ScriptedProvider only (no network), so the same test guards every FINAL addition; the assertions are on the stable ThyOutput/event-log seams, not on agent internals.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2299

#### `THY-32` — Specialist agent roster: open `ThyAgentKind` to the shipped FINAL agents

- **Tier** FINAL · **Owner** P2 (THY / Agents) · **Size** M · **Member path** `runtime/thy`
- **Depends on** `THY-12`, `THY-18`, `THY-20`, `THY-21`
- **Status** done
- **Scope note — this task did not exist until now; it was found, not specified.** Implementing THY-18/20/21 surfaced that no task anywhere covers opening `ThyAgentKind` to more than the three MVP agents; see "Specified at" below.
- **Deliverable** `THY-18`/`THY-20`/`THY-21` each ship a real, tested `AgentSpec` (`statistics-agent`, `data-quality-agent`, `visualization-agent`), but none is reachable from a real run: `AgentTask.agent` (`thymira.thy.models`) is typed `ThyAgentKind`, a closed enum holding only `DATA`/`CODING`/`EXPERIMENT` (the MVP roster) — the Plan node's structured output cannot select any other agent, and `execute_node`'s `catalog.get(agent_task.agent.value)` is never even asked to. This task: (1) adds one `ThyAgentKind` member per shipped FINAL agent, its value equal to that agent's `AgentSpec.name` exactly (same rule THY-01 already documents for the MVP roster); (2) a composed catalog builder (e.g. `thymira.thy.agents.full_agent_catalog()`) merging every specialist `AgentCatalog`, MVP and shipped FINAL alike, for callers (`build_thy_graph`/`run_thy`) that want the fuller roster; (3) the Plan node's system prompt updated to name the newly reachable agents and when to choose them — today it names only the three MVP agents by hand, nothing derives that text from a catalog. A specialist agent shipped later (`THY-19`, `THY-22`, ...) adds its own `ThyAgentKind` member the same way; this task is not re-triggered by that, it only has to happen once for the mechanism to exist.
- **Done when** a test drives `build_thy_graph`/`run_thy` (not `AgentRunner` directly) with a scripted Plan output naming the statistics agent's `ThyAgentKind` value; Execute resolves it through the composed catalog, `AgentRunner` runs it for real, and the resulting `Task` reaches `COMPLETED` with `StatsResult` as its output — proving the path a real `thymira run` would take, not just direct-`AgentRunner` reachability the way THY-18/20/21's own tests already prove.
- **Seam (why FINAL needs no refactor)** None needed for future agents: once this task lands, adding `THY-19`/`THY-22` is an enum member + a catalog entry + a prompt line, never a change to `execute_node`, `Delegator` or the graph. `ThyAgentKind` staying closed *today* was itself never a documented decision — it is simply what THY-01/09/12 happened to need for three agents; no ADR or roadmap task says "and no more agents than that."
- **Specified at** — none; this gap was found, not specified. `docs/architecture/e2e-baseline-v2.md:44` lists all seven FINAL agents (Statistics, ML, Data Quality, Visualization, Research, "other specialized agents") as part of THY's coordinated roster, but no task anywhere turns that sentence into the mechanism that lets Plan actually pick one — `THY-19`/`THY-22`'s own "Seam" notes say only "drop-in AgentSpec, no framework change" about themselves, which is true of each agent alone and silent on who opens the door.
#### `THY-33` — Lazy public imports for agents without tools

- **Tier** MVP · **Owner** P2 (THY / Agents) · **Size** S · **Member path** `runtime/agents`
- **Depends on** `THY-04`
- **Status** done
- **Deliverable** Make `thymira.agents` load the runner and tool bridge only when callers request them. Importing routing types, agent specifications, or an agent that has no tools must not load `AgentRunner`, `thymira.agents.tool_bridge`, `thymira.tools`, or `ToolManager`. Preserve the supported public imports from `thymira.agents`; agents that do use tools must continue to use the unchanged Tool Manager and Gate path.
- **Done when** `uv run pytest tests/thymira/test_agents_imports.py -q` passes in fresh interpreter processes: importing `thymira.agents.llm.routing` and `AuditAgentSpec` does not load the tool modules, while importing `AgentRunner` or `build_agent_tools` still exposes the same public API and permits the existing tool-bridge tests to pass.
- **Seam (why FINAL needs no refactor)** This is an MVP release must for the common agent package: lightweight agents remain independent of tool infrastructure, while tool-using agents retain their existing authorization and execution path. It removes the import-boundary limitation documented by `MIRA-02` without changing MIRA's contract.
- **Specified at** `MIRA-02` known import-boundary limitation (2026-08-28)

### Tools, execution and MLflow

*29 tasks — 18 MVP, 11 FINAL.*

#### `TOOL-01` — Sandbox vocabulary and ToolCall enforcement fields (contract, pre-freeze)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `packages/schemas`
- **Depends on** none
- **Status** done
- **Already in the repository** packages/schemas/src/thymira/schemas/enums.py, packages/schemas/src/thymira/schemas/tool_call.py. The sandbox round-trip, unknown-enum, omitted-field, and public-export tests now close the remaining acceptance criteria. **The `CONTRACT_VERSION` question is closed: no bump.** The sandbox pair is part of 0.2 and `contract-v0.1.md`'s 0.2 entry documents it, so at 0.2 `CONTRACT_VERSION = "0.2"` already described the shape consumers got (the contract has since advanced past 0.2; the current value lives in `thymira.schemas` and the status line of `contract-v0.1.md`), and bumping to 0.3 for the sandbox pair would have made every consumer re-pin for a change that never happened.
- **Deliverable** Add closed StrEnums SandboxMode (values `read_only` / `workspace_write` / `danger_full_access`) and SandboxEnforcement (values `full` / `partial` / `unusable`) to packages/schemas/src/thymira/schemas/enums.py; add optional fields sandbox_mode: SandboxMode \| None = None and sandbox_enforcement: SandboxEnforcement \| None = None to ToolCall (tool_call.py); export both enums from thymira.schemas.__init__ __all__; document in docs/contracts/contract-v0.1.md that tool.completed payloads carry both keys, and decide there whether these fields carry a CONTRACT_VERSION bump to 0.3. Add both enum names to tests/thymira/test_workspace_imports.py exports.
- **Done when** `uv run pytest tests/thymira/test_schemas.py -k sandbox` passes: a ToolCall built with sandbox_mode=SandboxMode.WORKSPACE_WRITE and sandbox_enforcement=SandboxEnforcement.PARTIAL round-trips through to_json_dict/model_validate, an unknown enum string is rejected, and a ToolCall with the fields omitted still validates (defaults None). `just typecheck` stays at 0 and `just check-imports` still reports 4 kept.
- **Seam (why FINAL needs no refactor)** The closed enums and the two defaulted ToolCall fields exist from day one, so a FINAL confined run that reports 'full' is a value change, never a contract migration; defaulting to None keeps every existing ToolCall constructor valid. Must land before the contract freezes (ADR-0005 idea 6, ADR-0006 pattern).
- **Folded in** `GOV-02` (the governance-facing half of the same schema change) was merged here by correction C-6. GOV-02 carried the correct enum values, which this task had wrong; it also claimed the MVP reports `SandboxEnforcement.FULL`, which **C-3 forbids** — an unconfined local subprocess reports `partial`. The producing side is `TOOL-02`, the control that reads it is `TOOL-24`.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:105

#### `TOOL-02` — ToolResult sandbox fields and manager propagation into the recorded ToolCall

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** S · **Member path** `runtime/tools`
- **Depends on** `TOOL-01`
- **Status** done
- **Already in the repository** runtime/tools/src/thymira/tools/models.py, runtime/tools/src/thymira/tools/manager.py, tests/thymira/test_tools.py
- **Deliverable** Add sandbox_mode: SandboxMode \| None = None and sandbox_enforcement: SandboxEnforcement \| None = None to the ToolResult dataclass in runtime/tools/src/thymira/tools/models.py; in ToolManager.execute (manager.py) copy those two values from the ToolResult onto the completed ToolCall.model_copy(update=...) and into the tool.completed event payload alongside status/exit_code.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k sandbox` passes: a fake tool returning ToolResult(sandbox_enforcement=PARTIAL, sandbox_mode=WORKSPACE_WRITE) yields an execution whose completed call carries those values and a tool.completed event whose payload includes sandbox_enforcement='partial'.
- **Seam (why FINAL needs no refactor)** The manager is already the single result->call->event write path; the enforcement fact flows through it unchanged, so tightening confinement later only changes the value a tool reports, not any signature or call site.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:173

#### `TOOL-03` — Self-describing Tool protocol: arguments model, input schema and validation

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** none
- **Status** done
- **Deliverable** Extend the Tool protocol (models.py) with description: str and arguments_model: type[pydantic.BaseModel] \| None; add a helper input_schema(tool) that returns arguments_model.model_json_schema() (or {} when None). In ToolManager.execute, before the policy decision, validate raw arguments against arguments_model and raise ToolExecutionError on invalid arguments (fail-closed, recorded as a failed call). The PydanticAI bridge consumes each registered tool's description and schema, and existing test tools provide compatible descriptors.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k schema` passes: a tool declaring an arguments_model exposes a non-empty input_schema; calling it with a missing required argument yields a FAILED call and a tool.completed event (not an unhandled exception); calling it with valid arguments succeeds.
- **Seam (why FINAL needs no refactor)** The descriptor (description + JSON schema) is the seam MCP exposure (TOOL-23) and P2's PydanticAI tool-calling both consume; adding it now means neither needs to edit any tool later. Validation stays in the manager so every entry point (agent, MCP) is gated identically.
- **Specified at** docs/architecture/e2e-baseline-v2.md:366

#### `TOOL-04` — Manager emits artifact.created for tool-produced artifacts (closes control A10)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-05`
- **Status** done
- **Deliverable** In ToolManager.execute, snapshot the set of active artifact ids from context.artifact_store.list_active() immediately before tool.execute and again after (and after spill, TOOL-05); for each newly active Artifact append an EventType.ARTIFACT_CREATED event with payload {name, sha256, artifact_id, produced_by} via context.event_log, ordered after execute and before tool.completed. Add a _tool_actor-based actor.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k artifact_created` passes: a tool that calls context.artifact_store.save_json produces exactly one artifact.created event whose name and sha256 match the stored Artifact, and MIRA control A10 (a10_declared_artifacts) reports PASSED for that run.
- **Seam (why FINAL needs no refactor)** The single emission point in the manager works over the ArtifactStore protocol, so the FINAL object-store backend (S3/MinIO) changes nothing here; the event carries the digest inside the hash chain that A10 compares against the manifest.
- **Specified at** runtime/mira/src/thymira/mira/checks/controls.py:212

#### `TOOL-05` — Spill policy: oversized tool results become referenced artifacts

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** none
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/spill.py with spill_result(result, context, *, max_inline_bytes) that, when stdout or stderr exceeds the threshold, **redacts** the oversized field once (`thymira.events.redact`) — the spiller is the trust boundary, not the artifact store, whose contract is byte fidelity — stores the redacted text through context.artifact_store.save_text(kind=ArtifactKind.LOG), and replaces the inline field with a truncated redacted head plus a marker naming the stored artifact id. result_sha256 means a digest a verifier can recompute from stored evidence: a digest the tool already set is left untouched; if the tool set none and exactly one stream spilled it is set to that artifact's manifest sha256; if both streams spilled it is left None — there is no canonical single text, and a fabricated digest (for instance over a private concatenation of the streams) is worse than none. Fail-safe: a failed save is swallowed for that field, which keeps its full inline value and records no artifact or digest, and never raises. Call it in ToolManager.execute post-execute (before artifact.created emission).
- **Done when** `uv run pytest tests/thymira/test_tools_spill.py` passes: a tool returning 1 MB of stdout yields a completed call whose stdout is truncated, whose artifact_ids reference a stored LOG artifact whose content equals the **redacted** stdout (the sha256 the manifest holds), and whose result_sha256 is that stored digest when only one stream spilled; a result_sha256 the tool already set is preserved; a call that spills both stdout and stderr leaves result_sha256 None; a secret in oversized output is redacted before it is persisted or left inline; and a store made to fail leaves the inline stdout intact with no artifact or digest and the call still COMPLETED.
- **Seam (why FINAL needs no refactor)** The threshold-and-store seam attaches to the manager's existing post-execute step and writes through the ArtifactStore protocol; FINAL only tunes the threshold and adds a retrieval hint, no caller changes. Improves on dsh's opaque locator by giving spilled content a sha256 digest.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:102

#### `TOOL-06` — Sandbox protocol and LocalSubprocessSandbox (reports PARTIAL)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-01`
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/sandbox/base.py defining a Sandbox Protocol with run(argv, *, workspace: Path, mode: SandboxMode, timeout_s: float, env: Mapping[str,str]) -> SandboxRun (fields stdout, stderr, exit_code, enforcement: SandboxEnforcement, mode). Add sandbox/local.py with LocalSubprocessSandbox that runs a subprocess with cwd=workspace, a wall-clock timeout (kills the process group on expiry), a scrubbed environment, and reports enforcement=SandboxEnforcement.PARTIAL (a bare subprocess is not truly confined) and mode as requested.
- **Done when** local sandbox tests prove restricted requests return `UNUSABLE` without launching a child; explicit `danger_full_access` runs the current Python interpreter, reports `PARTIAL`, and terminates timed-out execution. Development execution does not promise workspace confinement.
- **Seam (why FINAL needs no refactor)** The Sandbox protocol is the swap point: FINAL can register a ContainerSandbox without changing run_python (TOOL-08), because the tool depends only on the protocol and ToolContext.workspace. Full confinement remains a future target until every boundary is effective and verified.
- **Specified at** docs/architecture/e2e-baseline-v2.md:648

#### `TOOL-07` — ContainerSandbox with resource limits (reports PARTIAL, fail-closed)

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-06`
- **Status** partial
- **Deliverable** Add sandbox/container.py implementing the Sandbox protocol through a non-root, no-privileged, no-docker-socket container with Docker's default seccomp profile, CPU/RAM/pid/timeout controls, and a network policy per SandboxMode; mount the workspace read-only or read-write per mode. Report `PARTIAL` for confirmed container execution until filesystem and process boundaries are all effective and verified. Report `UNUSABLE` (refuse to run, non-zero) when the runtime cannot establish or verify the container lifecycle. Full confinement, including an effective filesystem quota, remains the pending target; no `filesystem_limit` option is accepted as proof of it.
- **Done when** effective workspace quotas and every filesystem, process, resource, timeout and network boundary have independent execution proofs before a backend reports `FULL`. Current progress: `uv run pytest tests/thymira/test_tools_sandbox_container.py` verifies actual Docker boundaries, and `uv run pytest tests/thymira/test_tools_e2e.py -k container` executes Python, default training and inspection through the Tool Manager with verified events/artifacts, A23 PASS and A19 MEDIUM. These tests skip only missing Docker/image prerequisites; execution defects fail. Full confinement remains unimplemented.
- **Seam (why FINAL needs no refactor)** Implements the exact Sandbox protocol from TOOL-06, so run_python, the ToolResult/ToolCall enforcement fields and the MIRA control remain drop-in. A future fully confined backend can change the reported value only after effective filesystem, process, resource, timeout and network boundaries are verified.
- **Specified at** docs/architecture/e2e-baseline-v2.md:662

#### `TOOL-08` — run_python tool over the Sandbox protocol

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-06`, `TOOL-03`, `TOOL-02`
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/builtins/run_python.py: a RunPython Tool (name 'run_python', arguments_model with fields code: str and optional timeout_s) whose ToolCapability declares risk_tags=('code_execution',), side_effects=('workspace_write',), external_effects=() (MVP local), reversibility='reversible'. It writes the code to a file under ToolContext.workspace, runs it via the injected Sandbox in SandboxMode.WORKSPACE_WRITE, and returns a ToolResult with stdout/stderr/exit_code, success=(exit_code==0), and sandbox_mode/sandbox_enforcement copied from the SandboxRun. Register it in a builtins registry factory.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k run_python` passes: executing code that prints and writes a file yields a COMPLETED call with the printed stdout, exit_code 0, sandbox_enforcement recorded (PARTIAL under the local sandbox), and the event log verifies. Failing code (exit_code!=0) yields a FAILED call, not an exception.
- **Seam (why FINAL needs no refactor)** The Tool protocol + ToolCapability + injected Sandbox are the seam; FINAL swaps the Sandbox impl and, once confinement is real, may narrow external_effects, without changing the tool signature or any agent call site (roadmap: the run_python(...) contract is stable).
- **Specified at** docs/roadmap/mvp-3-weeks.md:2578

#### `TOOL-09` — File tools: read_file, write_file, list_files (workspace-contained)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-03`
- **Status** done
- **Deliverable** Add builtins/files.py with ReadFile, WriteFile, ListFiles Tools operating on ToolContext.workspace, each with an arguments_model and a ToolCapability (write_file side_effects=('workspace_write',), reads external_effects=()). Reuse a containment check equivalent to LocalArtifactStore._store_key/_contained (reject '..', absolute, Windows-anchored, and symlink escapes) so no path leaves the workspace. write_file caps size; read_file spills oversized content via TOOL-05.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k files` passes: write_file then read_file round-trips a file under workspace; list_files returns it; a path containing '..' or an absolute path is rejected with a FAILED call; a planted symlink pointing outside the workspace cannot be written through.
- **Seam (why FINAL needs no refactor)** The Tool + workspace-containment seam is fixed now; FINAL runs these same tools inside the sandbox (mounts) without changing their signatures or the containment contract.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1563

#### `TOOL-10` — Git tools: git_status, git_diff, git_log, git_commit

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-03`, `TOOL-06`
- **Status** done
- **Deliverable** Add builtins/git.py with GitStatus, GitDiff, GitLog (read; external_effects=(), data_access=('repository',)) and GitCommit (side_effects=('workspace_write',), reversibility='reversible') Tools. Run git through the Sandbox protocol (or subprocess in MVP) with cwd=ToolContext.workspace; parse porcelain output into structured ToolResult fields; git_commit takes message and optional paths and returns the new commit sha. No automatic commits are wired to agents.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k git` passes on a temp git repo under tmp_path: git_status reports a staged change, git_diff returns the unified diff text, git_commit creates a commit whose sha git_log then reports; running outside a repo yields a FAILED call, not an exception.
- **Seam (why FINAL needs no refactor)** Each git tool is a registry entry over the existing Tool/Sandbox seam; routing git through the Sandbox means FINAL confinement applies to it for free, and git worktrees (TOOL-11) add tools without any contract change.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1601

#### `TOOL-11` — Git worktree tools for parallel/background experiment isolation

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-10`
- **Status** done
- **Deliverable** Add git worktree management (create/list/remove) to builtins/worktrees.py so a background run gets its own worktree per experiment (baseline section 16: worktree + sandbox + MLflow run + artifacts per background execution), used by the ThyGraph background-agent flow. Each worktree is created under a run-scoped path and torn down on completion.
- **Done when** `uv run pytest -m slow tests/thymira/test_tools.py -k worktree` passes: creating two worktrees for one repo yields two isolated working directories where run_python writes independently, and removing them leaves the base repo clean.
- **Seam (why FINAL needs no refactor)** Pure additive registry growth over the Tool/Sandbox seam already established; introduces no new contract, and the ToolContext.workspace it targets is unchanged.
- **Specified at** docs/architecture/e2e-baseline-v2.md:618

#### `TOOL-12` — Dataset registry and loader (Artifact kind=dataset with captured schema)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** none
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/datasets.py: register_dataset(store, path, name) that records a CSV/Parquet file as an Artifact(kind=ArtifactKind.DATASET) and captures a DatasetSchema (columns, dtypes, row_count, byte_size, content sha256); load_dataset(store, name, *, max_rows) resolving a registered dataset to a bounded pandas/polars frame with a row cap. Shared dependency for the data-science tools.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k dataset_registry` passes on data/german_credit.csv copied under tmp_path: register_dataset records a DATASET artifact whose sha256 matches the file and whose captured schema lists the expected column count; load_dataset returns at most max_rows and the correct column names.
- **Seam (why FINAL needs no refactor)** The register/resolve seam (logical name -> DatasetSchema + Artifact) is fixed now; FINAL adds object-store and streaming backends behind the same load_dataset signature, and the schema capture is what feeds baseline data-lineage later.
- **Specified at** docs/architecture/e2e-baseline-v2.md:540

#### `TOOL-13` — analyze_dataset tool

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-12`, `TOOL-03`, `TOOL-04`
- **Status** done
- **Deliverable** Add builtins/analyze_dataset.py: AnalyzeDataset Tool (arguments_model: dataset name, optional columns) that loads via TOOL-12 and returns a structured summary (shape, per-column dtype, null counts, unique counts, numeric min/max/mean/std) and writes analysis.json as an Artifact(kind=METRICS). ToolCapability data_access=('dataset',), external_effects=().
- **Done when** `uv run pytest tests/thymira/test_tools.py -k analyze_dataset` passes on data/german_credit.csv: the call is COMPLETED, an analysis.json artifact is created (one artifact.created event), and the returned summary reports the correct row count and column dtypes.
- **Seam (why FINAL needs no refactor)** Standard Tool over the dataset loader and artifact store seams; the returned summary shape is stable so a richer FINAL profiler is a separate tool, not a change to this one.
- **Specified at** docs/architecture/e2e-baseline-v2.md:354

#### `TOOL-14` — profile_dataset tool

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-12`, `TOOL-03`, `TOOL-04`
- **Status** done
- **Deliverable** Add builtins/profile_dataset.py: ProfileDataset Tool that produces per-column distributions/histograms, cardinality, missingness, and a numeric correlation matrix, writing a profile.json (and optional profile.html) Artifact(kind=REPORT). Reuses the TOOL-12 loader with a row cap.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k profile_dataset` passes on data/german_credit.csv: a REPORT artifact is created whose JSON contains a correlation matrix over the numeric columns and a missingness entry per column; oversized profiles spill via TOOL-05.
- **Seam (why FINAL needs no refactor)** Same loader + artifact seam as analyze_dataset; adding profilers later grows the report payload without changing the tool signature or the artifact kind.
- **Specified at** docs/architecture/e2e-baseline-v2.md:355

#### `TOOL-15` — query_sql tool (read-only, embedded engine over the store)

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-12`, `TOOL-03`, `TOOL-04`
- **Status** done
- **Deliverable** Add builtins/query_sql.py: QuerySql Tool running a read-only SQL query (DuckDB) against datasets registered via TOOL-12, returning rows as a bounded result and spilling large result sets to a CSV/Parquet Artifact. ToolCapability data_access=('dataset',), side_effects=('workspace_write',) because the spill path persists a report artifact, reversibility='reversible'; reject non-SELECT statements.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k query_sql` passes: a SELECT with a WHERE/GROUP BY over german_credit returns the expected aggregate; a non-SELECT statement is rejected with a FAILED call; a large result set is spilled to an artifact.
- **Seam (why FINAL needs no refactor)** New registry entry over the Tool + dataset-loader seams; MVP ships no SQL (roadmap and baseline exclude a SQL engine from the MVP), and because it is only a tool addition it needs no earlier seam beyond TOOL-12/TOOL-03.
- **Specified at** docs/architecture/e2e-baseline-v2.md:356

#### `TOOL-16` — run_statistics tool

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-12`, `TOOL-03`, `TOOL-04`
- **Status** done
- **Deliverable** Add builtins/run_statistics.py: RunStatistics Tool exposing descriptive and inferential tests (t-test, chi-square, ANOVA, correlation with p-values, normality tests) over a registered dataset, returning results as metrics and writing a statistics report Artifact(kind=REPORT). ToolCapability data_access=('dataset',), external_effects=().
- **Done when** `uv run pytest tests/thymira/test_tools.py -k run_statistics` passes: a two-sample t-test over two german_credit subgroups returns a statistic and p-value matching scipy within tolerance, and a report artifact is created.
- **Seam (why FINAL needs no refactor)** Backs the FINAL Statistics Agent (MVP has only Data/Coding/Experiment agents); a registry addition over the existing Tool + dataset seams, so no MVP seam is owed beyond them.
- **Specified at** docs/architecture/e2e-baseline-v2.md:358

#### `TOOL-17` — MLflow integration: ExperimentTracker protocol, local impl, and mlflow_* tools

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-03`, `TOOL-04`
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/mlflow/base.py with an ExperimentTracker Protocol (start_run, log_param, log_metric, log_artifact, end_run) and mlflow/local.py with MlflowTracker backed by a file tracking URI under ToolContext.workspace/.mlflow. Add builtins/mlflow_tools.py exposing mlflow_start_run/mlflow_log_param/mlflow_log_metric/mlflow_log_artifact/mlflow_end_run as Tools that call the tracker; each returns the tracker_run_id. Map tracker_run_id onto the Experiment contract (contract open point 4).
- **Done when** `uv run pytest tests/thymira/test_tools.py -k mlflow` passes: mlflow_start_run then log_param/log_metric then log_artifact then end_run produces a readable MLflow run under the file store whose logged metric and param match, and the returned tracker_run_id is a non-empty string.
- **Seam (why FINAL needs no refactor)** The ExperimentTracker protocol is the swap seam: FINAL points the tracking URI at a remote MLflow server via config with no caller change; the five tools are the stable primitives the Experiment Agent composes (roadmap week-1 P3 Thursday).
- **Specified at** docs/architecture/e2e-baseline-v2.md:502

#### `TOOL-18` — run_experiment tool (Experiment record + experiment/model events + model artifact)

- **Tier** MVP · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-17`, `TOOL-12`, `TOOL-08`
- **Status** done
- **Deliverable** Add builtins/run_experiment.py: RunExperiment Tool that trains/evaluates a model from provided code or a configured estimator on a registered dataset with a fixed seed, logs params/metrics/artifacts through the ExperimentTracker (TOOL-17), registers the produced model as Artifact(kind=MODEL), builds an Experiment record (id, parameters, metrics, seed, model_artifact_id, tracker_run_id), and appends EventType.EXPERIMENT_STARTED, MODEL_TRAINED, EXPERIMENT_COMPLETED to context.event_log. Runs training through run_python/Sandbox so it is confined like any code.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k run_experiment` passes on german_credit: the call is COMPLETED, an Experiment record carries a model_artifact_id and a tracker_run_id, a MODEL artifact and a metrics.json exist, the event log contains experiment.started/model.trained/experiment.completed in order and verifies, and re-running with the same seed reproduces the metrics.
- **Seam (why FINAL needs no refactor)** The Experiment contract, the ExperimentTracker protocol, and the experiment/model event vocabulary are the seams; FINAL may move Experiment persistence behind a runtime/state repository and training onto workers/worktrees without changing this tool or its events.
- **Specified at** docs/architecture/e2e-baseline-v2.md:359

#### `TOOL-19` — query_mlflow tool (read runs, params, metrics)

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** S · **Member path** `runtime/tools`
- **Depends on** `TOOL-17`
- **Status** done
- **Deliverable** Add builtins/query_mlflow.py: QueryMlflow Tool that lists runs and returns params/metrics/tags for a given experiment or tracker_run_id via the ExperimentTracker, spilling large listings to an artifact. ToolCapability data_access=('experiment',), external_effects=() for the local store.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k query_mlflow` passes: after TOOL-18 has logged a run, query_mlflow returns that run's metric and param values matching what was logged.
- **Seam (why FINAL needs no refactor)** Read-side registry addition over the ExperimentTracker seam; backs `thymira mlflow` (TOOL-26) and compare_models (TOOL-20) with no contract change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:360

#### `TOOL-20` — compare_models tool

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-19`
- **Status** done
- **Deliverable** Add builtins/compare_models.py: CompareModels Tool that takes two or more experiment or model-artifact ids, fetches their metrics via query_mlflow/the tracker, and produces a side-by-side comparison Artifact(kind=REPORT) plus a structured winner-by-metric summary in the ToolResult.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k compare_models` passes: given two experiments with different accuracy, the comparison report ranks them correctly and a REPORT artifact is created.
- **Seam (why FINAL needs no refactor)** Registry addition over the tracker/experiment seam; backs the FINAL ML Agent and needs no MVP seam beyond TOOL-17/TOOL-19.
- **Specified at** docs/architecture/e2e-baseline-v2.md:361

#### `TOOL-21` — inspect_model tool

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** M · **Member path** `runtime/tools`
- **Depends on** `TOOL-18`, `TOOL-03`
- **Status** done
- **Deliverable** Add builtins/inspect_model.py: InspectModel Tool that loads a MODEL artifact and reports estimator type, hyperparameters, input/output signature, and feature importances/coefficients where available, writing an inspection Artifact(kind=REPORT). Runs the load in the Sandbox.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k inspect_model` passes: inspecting the model produced by TOOL-18 returns the estimator class name and its hyperparameters and a feature-importance vector of the expected length.
- **Seam (why FINAL needs no refactor)** Registry addition over the Tool + artifact-store + Sandbox seams; a FINAL ML Agent tool, so no earlier seam is owed.
- **Specified at** docs/architecture/e2e-baseline-v2.md:362

#### `TOOL-22` — audit_model tool (deterministic governance evidence)

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-18`, `TOOL-12`, `TOOL-03`
- **Status** done
- **Deliverable** Add builtins/audit_model.py: AuditModel Tool that, given a MODEL artifact and a labelled dataset, deterministically computes performance (accuracy/AUC/confusion), calibration, subgroup/fairness metrics across a protected attribute, and simple drift vs a reference dataset, writing an evidence Artifact(kind=REPORT). ToolCapability declares side_effects=('workspace_write',) for its staging/report path and data_access=('dataset','model'). Output is consumed by MIRA's Model Risk agent as evidence; the tool authorizes nothing.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k audit_model` passes on german_credit: the evidence report contains per-subgroup performance for a protected attribute and an overall AUC matching sklearn within tolerance, and the metrics are byte-identical on a re-run (deterministic).
- **Seam (why FINAL needs no refactor)** A deterministic, read-only Tool over the existing Tool/artifact seams; its output is evidence, never an authorization, keeping 'LLM proposes, code authorizes' intact. Registry addition, no contract change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:363

#### `TOOL-23` — MCP server exposing the tool set through the Tool Manager

- **Tier** FINAL · **Owner** P3 (Tools / Execution / MLflow) · **Size** L · **Member path** `runtime/tools`
- **Depends on** `TOOL-03`
- **Status** done
- **Deliverable** Add runtime/tools/src/thymira/tools/mcp/server.py that maps each registered Tool to an MCP tool definition (name, description, input_schema from TOOL-03) and routes every MCP tools/call through ToolManager.execute against a runtime-owned ToolContext, so each MCP invocation is still policy-gated, redacted and recorded. Expose the ToolRegistry as an MCP server entry point.
- **Done when** `uv run pytest -m integration tests/thymira/test_tools_mcp.py` passes: an MCP client lists the registered tools with non-empty input schemas and, calling run_python over MCP, receives the stdout while the run's event log shows the same policy.decision/tool.started/tool.completed sequence a direct call produces.
- **Seam (why FINAL needs no refactor)** A thin exposure over the already-built ToolManager.execute + ToolCapability seam and the TOOL-03 descriptor; no tool changes and every MCP call is still authorized by the Policy Engine (ADR-0005: our gap is exposing the manager as MCP, not consuming MCP).
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:138

#### `TOOL-24` — MIRA control: run executed under partial or unusable confinement

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `TOOL-02`
- **Status** done
- **Deliverable** Add control A19 to runtime/mira/src/thymira/mira/checks/controls.py: scan tool.completed events (or ToolCall sandbox fields) and raise a finding when any code-executing tool ran with sandbox_enforcement in {partial, unusable}. Severity ladder: CRITICAL when enforcement is `unusable` (confinement was requested and could not be applied at all); HIGH when enforcement is `partial` and the mode was `danger_full_access` (unconfined by request); MEDIUM for any other `partial`, which is the MVP's expected state under C-3. NOT_APPLICABLE when no execution recorded an enforcement value. Register it in CONTROLS and add its evidence.
- **Done when** `uv run pytest tests/thymira/test_mira_checks.py -k sandbox` passes: an event log with a tool.completed carrying sandbox_enforcement='partial' yields exactly one A19 finding from audit_run; a log where every execution reports 'full' yields no A19 finding.
- **Seam (why FINAL needs no refactor)** The control reads the enforcement fact the MVP already records (TOOL-02); when FINAL confinement reports 'full', the same control passes untouched. This is enforcement-as-a-reported-fact (ADR-0005 idea 6).
- **Folded in** `CTRL-SANDBOX` (the same control, filed as FINAL) was merged here by correction C-6. It stays MVP: `TOOL-28` is an MVP acceptance gate that depends on this finding actually firing, and under C-3 every MVP run reports `partial`, so the control has something to say from day one. The three-step severity ladder above is CTRL-SANDBOX's (which knew `unusable` is worse) reconciled with this task's (which knew a blanket HIGH on every MVP run is noise); P4 owns both and may retune it.
- **Control id** `A19`, deliberately outside the inherited range. `A1`–`A18` is the closed control
  namespace ported from the thesis meta-auditor (ADR-0002): every number in it already means
  something, including `A8` (decisions answered), which is defined but not ported. `A11` in
  particular is train/test leakage, and `credit_risk.yaml`'s `CR-101` already maps it to **BLOCK**
  with the reason *"evidence of train/test leakage"* — so filing the sandbox control as `A11` would
  make every MVP run BLOCK with a false statement about the data, since under C-3 every MVP run
  reports `partial`. Confinement is a new concern with no legacy ancestor; it gets a new number.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:204

#### `TOOL-25` — CLI: thymira experiment <run_id>

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `TOOL-18`
- **Status** done
- **Deliverable** Add the `experiment` command to adapters/cli that calls the Thymira API and renders a run's experiments (name, status, parameters, metrics, seed, tracker_run_id, model artifact) as a Rich table (UTF-8). No runtime import; thin API client only.
- **Done when** `uv run thymira experiment <run_id>` against a run produced by the E2E test (TOOL-27) prints a table listing the experiment with its metrics and tracker_run_id; a CLI subprocess integration test asserts the metric value appears in stdout.
- **Seam (why FINAL needs no refactor)** Reads the Experiment contract over the API; a client with no state (baseline: clients own no state), so a remote MLflow or a PostgreSQL backend later changes nothing in the CLI.
- **Specified at** docs/architecture/e2e-baseline-v2.md:220

#### `TOOL-26` — CLI: thymira mlflow

- **Tier** FINAL · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `TOOL-19`
- **Status** done
- **Deliverable** Add the `mlflow` command to adapters/cli that browses MLflow runs and their params/metrics via the query_mlflow tool through the API, rendered as a Rich table.
- **Done when** `uv run thymira mlflow --run <run_id>` prints the logged runs with their metrics for a run that has experiments; a CLI subprocess integration test asserts a known metric appears.
- **Seam (why FINAL needs no refactor)** Thin client over the query_mlflow tool (TOOL-19); FINAL because query_mlflow is FINAL. No contract change, purely additive CLI surface.
- **Specified at** docs/architecture/e2e-baseline-v2.md:221

#### `TOOL-27` — QA: end-to-end execution test (run_python + MLflow + artifacts + spill, truthful audit findings)

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** L · **Member path** `tests/thymira`
- **Depends on** `TOOL-08`, `TOOL-18`, `TOOL-05`, `TOOL-04`, `TOOL-12`
- **Status** done
- **Deliverable** Add tests/thymira/test_tools_e2e.py (markers integration + slow): drive the Tool Manager to register a dataset, run explicitly permitted development execution of a tiny classifier on data/german_credit.csv, log to the local MLflow file store, produce model and metrics artifacts, emit a large stdout that spills, then build an AuditContext from the event log + store. Preserve the artifact, digest and numerical checks while asserting the actual A19/A23 findings for the declared execution mode and training path.
- **Done when** `uv run pytest -m 'integration and slow' tests/thymira/test_tools_e2e.py` passes: the event log verifies (verify_events valid), artifact.created events match the manifest (A10 PASSED), the spilled stdout is retrievable from its artifact, and the audit reports the expected development-mode A19 HIGH finding plus the custom-training A23 MEDIUM finding where applicable. The test does not claim an all-clear audit.
- **Seam (why FINAL needs no refactor)** Exercises every tools-execution seam (Sandbox, spill, artifact events, tracker, Experiment) while keeping enforcement and methodology findings truthful. A future fully confined backend may remove the A19 finding only when its recorded enforcement is actually `FULL`; changing the backend does not change this test's expected result automatically.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1676

#### `TOOL-28` — QA: sandbox enforcement is a recorded fact and drives the MIRA finding

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `tests/thymira`
- **Depends on** `TOOL-24`, `TOOL-08`
- **Status** done
- **Deliverable** Add tests asserting the enforcement seam end to end: explicit local development execution records `partial` and A19 HIGH; the actual Python, default experiment and inspection tools under Docker record `partial` and A19 MEDIUM. Container lifecycle failures record `unusable`, without inferring `full` from a requested quota.
- **Done when** `uv run pytest tests/thymira/test_tools.py -k enforcement` passes: the local-sandbox run yields sandbox_enforcement='partial' on the recorded call and one A19 finding; focused container tests preserve the actual `partial`/`unusable` result and never convert a failed or unverified boundary into `full`.
- **Seam (why FINAL needs no refactor)** Proves enforcement-as-fact propagation and the A19 finding path. A future backend may yield no A19 finding only when its recorded enforcement is independently verified as `full`; swapping a backend does not itself establish that result.
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:105

#### `TOOL-29` — QA: tool descriptor and MCP round-trip contract test

- **Tier** FINAL · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `tests/thymira`
- **Depends on** `TOOL-23`, `TOOL-03`
- **Status** done
- **Deliverable** Add a golden test that every tool in the builtins registry exposes a non-empty description and a valid JSON input_schema, that arguments failing the schema are rejected with a FAILED call, and that each tool round-trips through the MCP server (list -> call -> gated execution) with an identical event sequence to a direct ToolManager call.
- **Done when** `uv run pytest -m integration tests/thymira/test_tools_mcp.py -k contract` passes: the registry and its MCP projection expose the same tool names and schemas, and a call over MCP and the same call direct produce the same policy.decision/tool.started/tool.completed sequence.
- **Seam (why FINAL needs no refactor)** Guards the TOOL-03 descriptor seam so tools stay MCP- and agent-callable as the family grows; FINAL because it depends on the MCP server (TOOL-23).
- **Specified at** docs/architecture/e2e-baseline-v2.md:366

### MIRA and governance

*36 tasks — 17 MVP, 19 FINAL.*

#### `API-GOV` — Governance API routes: audit, approvals, approve/reject

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `apps/api`
- **Depends on** `HITL-01`, `MIRA-01`
- **Status** done
- **Deliverable** Add a governance router to apps/api (mounted on P1's FastAPI app): GET /runs/{id}/audit returns the AuditReport + PolicyDecision; GET /runs/{id}/approvals returns pending ApprovalRequests (decision, summary, cost_so_far); POST /runs/{id}/approve and POST /runs/{id}/reject call ApprovalService.resolve with an authenticated Actor and reason, returning the resolved decision. No governance logic in the route — it delegates to audit_run / ApprovalService.
- **Done when** uv run pytest tests/thymira/test_api_governance.py -m integration -q passes against the test app: GET audit on a completed run returns its report and decision, POST approve on a WAITING_FOR_APPROVAL run resolves the pending decision and returns approved True, and approve on a run with nothing pending returns 409.
- **Seam (why FINAL needs no refactor)** Thin transport over ApprovalService and audit_run (invariant: clients own no state). MVP and FINAL expose the same routes; swapping LocalApprovalService for the Postgres/interrupt-backed one (HITL FINAL) is invisible to the routes and to CLI-AUDIT/CLI-APPROVE. Depends on P1's FastAPI app skeleton.
- **Specified at** docs/roadmap/mvp-3-weeks.md:420; docs/roadmap/mvp-3-weeks.md:725

#### `ASSUR-01` — Assurance bundle (report + decision + evidence index + disclaimer)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Add thymira.mira.assurance.AssuranceBundle(run_id, report: AuditReport, decision: PolicyDecision, evidence_index: tuple[Evidence,...], terminal_hash) with build_assurance(run_id, report, decision) and to_markdown() reusing AuditReport.to_markdown plus the existing 'no human-review claim without evidence' disclaimer, and asserting the bundle carries the terminal event hash and policy_sha256 for replay.
- **Done when** uv run pytest tests/thymira/test_mira_assurance.py -q passes: build_assurance produces a bundle whose evidence_index deduplicates the findings' Evidence, whose disclaimer is present, and whose policy_sha256 equals the decision's; to_markdown() renders every control row.
- **Seam (why FINAL needs no refactor)** AssuranceBundle is the stable output object; MVP renders report+decision+disclaimer, FINAL (ASSUR-02) adds the expert-review state machine and full Finding->Evidence->Run/Artifact/Event traceability behind the same type.
- **Specified at** docs/adr/0002-legacy-disposition.md:46

#### `ASSUR-02` — Finding-level expert review + Finding->Evidence->source traceability index

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `ASSUR-01`
- **Status** done
- **Deliverable** Extend AssuranceBundle with one expert-review state per Finding (not_reviewed -> in_review -> reviewed \| rejected, transitions backed only by recorded human.approval events that identify that Finding) and derive any bundle summary from those states. Add a full traceability index mapping each Finding to its Evidence and each Evidence to the concrete Run/Artifact/Event/KB-source it cites, exposed in to_markdown(). A reviewed finding means only that its evidence was reviewed; it neither claims remediation nor changes a Run or PolicyDecision.
- **Done when** uv run pytest tests/thymira/test_mira_assurance_review.py -q passes: an unreviewed bundle reports not_reviewed and the disclaimer; an approval identifying one Finding marks only that Finding reviewed while another remains not_reviewed; an illegal transition raises; and every Finding resolves to at least one traceable Evidence source.
- **Seam (why FINAL needs no refactor)** Extends the ASSUR-01 AssuranceBundle in place (added fields/methods), so consumers of the MVP bundle keep working. Per-finding state prevents a review of one item from being misrepresented as a review or remediation of the whole bundle; the existing 'no human-review claim without evidence' rule is enforced by recorded evidence rather than only stated.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2352; docs/adr/0002-legacy-disposition.md:46

#### `AUD-COMPLIANCE` — Compliance audit agent (requirements coverage narrative)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-02`
- **Status** done
- **Deliverable** Add the Compliance AuditAgentSpec YAML (framework INTERNAL/EU_AI_ACT) and prompt that reads docs/governance/requirements_controls.json for the project's frameworks and reports, per requirement, whether the run produced the mapped evidence — emitting findings for uncovered requirements with Evidence pointing at the missing control.
- **Done when** uv run pytest tests/thymira/test_mira_agents_compliance.py -q passes with ScriptedProvider: for a project declaring EU_AI_ACT, a run lacking the art.12 event-logging evidence yields a compliance finding naming REQ and control_id, and a fully covered run yields none.
- **Seam (why FINAL needs no refactor)** Reads the stable requirements->controls schema (KB-02); runs on the unchanged runner. Complements the deterministic CTRL-COVERAGE (the agent narrates, the control asserts).
- **Specified at** docs/architecture/e2e-baseline-v2.md:81

#### `AUD-CREDIT-RISK` — Credit Risk audit agent

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-01`, `KB-02`
- **Status** done
- **Deliverable** Add the Credit Risk AuditAgentSpec YAML (framework CREDIT_RISK, tool_allowlist=[search_regulation]) and prompt: reviews a credit model against credit-risk regulation and fairness expectations (protected-attribute handling, adverse-action explainability, subgroup disparity), citing KB chunks as Evidence.
- **Done when** uv run pytest tests/thymira/test_mira_agents_credit_risk.py -q passes with ScriptedProvider and a seeded KB: a model trained with a protected attribute yields a CREDIT_RISK finding of severity>=HIGH with an external Evidence citation.
- **Seam (why FINAL needs no refactor)** Spec + prompt consumed by the unchanged MIRA-02 runner; adding it is data plus one line in the spec directory.
- **Specified at** docs/architecture/e2e-baseline-v2.md:77

#### `AUD-EUAIACT` — EU AI Act audit agent (the MVP 'Regulatory' agent)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-01`, `KB-02`
- **Status** done
- **Deliverable** Add the EU AI Act AuditAgentSpec YAML (framework EU_AI_ACT, task audit_judgement, tool_allowlist=[search_regulation]) and prompt: checks event-logging (art. 12), human-oversight (art. 14) and technical-documentation (art. 11) requirements against the run's evidence, citing chunks returned by search_regulation as Evidence(kind='external').
- **Done when** uv run pytest tests/thymira/test_mira_agents_euaiact.py -q passes with ScriptedProvider and a seeded LocalRegulationStore: given a run whose final decision was taken without a recorded human.approval, the agent emits an EU_AI_ACT art.14 finding carrying an external Evidence ref sourced from the KB.
- **Seam (why FINAL needs no refactor)** This is the roadmap's MVP 'Regulatory' agent, named EU_AI_ACT so FINAL simply adds Credit Risk, Compliance, Model Risk and Regulatory Evidence agents beside it — the baseline's seven-agent roster reached by adding specs (baseline section 2).
- **Specified at** docs/architecture/e2e-baseline-v2.md:76; docs/roadmap/mvp-3-weeks.md:966

#### `AUD-METHODOLOGY` — Methodology audit agent (train/test, leakage, validation, metrics)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-01`
- **Status** done
- **Deliverable** Add the Methodology AuditAgentSpec YAML (framework METHODOLOGY, task audit_judgement, tool_allowlist=[search_regulation]) and its system prompt under runtime/mira/.../agents/. The prompt directs the model to review train/test methodology, leakage indicators, validation design, metric choice and statistical limitations (ADR-0002 appendix B) and to emit AuditFindings with Evidence pointing at events/artifacts.
- **Done when** uv run pytest tests/thymira/test_mira_agents_methodology.py -q passes with ScriptedProvider: given an AuditInput whose evidence shows a target-correlated feature, the agent emits a METHODOLOGY finding of severity>=HIGH carrying at least one Evidence ref, and a clean run yields no methodology finding.
- **Seam (why FINAL needs no refactor)** Declared purely as an AuditAgentSpec + prompt consumed by the MIRA-02 runner; carries no bespoke code. FINAL deepens the prompt and adds search_regulation citations without any interface change.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2094

#### `AUD-MODEL-RISK` — Model Risk audit agent

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-01`
- **Status** done
- **Deliverable** Add the Model Risk AuditAgentSpec YAML (framework MODEL_RISK, tool_allowlist=[search_regulation]) and prompt implementing model-risk-management review: model purpose/limitations, validation adequacy, benchmarking against a baseline, and monitoring/assumptions — emitting findings with Evidence tied to experiments and metrics.
- **Done when** uv run pytest tests/thymira/test_mira_agents_model_risk.py -q passes with ScriptedProvider: a run whose best model has no trivial-baseline comparison in evidence yields a MODEL_RISK 'unbenchmarked model' finding with Evidence kind='experiment'.
- **Seam (why FINAL needs no refactor)** Spec + prompt only; same runner and same AuditFinding/Evidence contract.
- **Specified at** docs/architecture/e2e-baseline-v2.md:82

#### `AUD-REGULATORY-EVIDENCE` — Regulatory Evidence agent (citation-backed evidence bundles)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`, `KB-01`
- **Status** done
- **Deliverable** Add the Regulatory Evidence AuditAgentSpec YAML and prompt that, for each other agent's finding, assembles a citation-backed Evidence set linking the finding to the exact KB source (source_id, location, sha256) and the run artifact/event it concerns, strengthening the AssuranceBundle's evidence_index.
- **Done when** uv run pytest tests/thymira/test_mira_agents_reg_evidence.py -q passes with ScriptedProvider and a seeded KB: every enriched finding gains at least one Evidence with kind='external' and a resolvable source_id, and evidence with an unresolvable citation is dropped rather than fabricated.
- **Seam (why FINAL needs no refactor)** Produces the same Evidence type the AssuranceBundle already indexes (ASSUR-01), so its output plugs into the existing bundle with no schema change.
- **Specified at** docs/architecture/e2e-baseline-v2.md:80

#### `AUD-RISK` — Risk audit agent (bias/risk indicators, model limitations)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `KB-01`
- **Status** done
- **Deliverable** Add the Risk AuditAgentSpec YAML (framework MODEL_RISK, task audit_judgement, tool_allowlist=[search_regulation]) and prompt: reviews the finished run for bias/subgroup-disparity indicators, missing validation evidence, model limitations and residual risk, emitting AuditFindings with Evidence. Distinct from RISK-01 (the pre-work classifier); this is MIRA's post-hoc reviewer.
- **Done when** uv run pytest tests/thymira/test_mira_agents_risk.py -q passes with ScriptedProvider: given experiment evidence lacking subgroup metrics, the agent emits a MODEL_RISK 'insufficient validation evidence' finding whose Evidence has kind='experiment', and confidence stays in [0,1].
- **Seam (why FINAL needs no refactor)** Spec + prompt only, same runner. FINAL adds credit-risk-specific bias tests by adding AUD-CREDIT-RISK alongside it, not by editing this agent.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2105

#### `CLI-APPROVE` — thymira approve / thymira reject RUN_ID

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** S · **Member path** `adapters/cli`
- **Depends on** `API-GOV`, `HITL-01`
- **Status** done
- **Deliverable** Add `approve` and `reject` commands to adapters/cli: fetch GET /runs/{id}/approvals, show the pending decision, reason, summary and cost_so_far, then POST approve/reject with the declared actor and an optional --reason. Stable exit codes; every user-facing message redacted.
- **Done when** uv run pytest tests/thymira/test_cli_approval.py -m integration -q passes: `thymira approve RUN` on a pending run posts approve and prints the resolved decision (exit 0); `thymira reject RUN --reason x` posts reject; approving a run with nothing pending exits non-zero with a clear message.
- **Seam (why FINAL needs no refactor)** Client of the approve/reject routes only; identical across HITL MVP and FINAL because the ApprovalService protocol is unchanged.
- **Specified at** docs/roadmap/mvp-3-weeks.md:2173; docs/roadmap/mvp-3-weeks.md:725

#### `CLI-AUDIT` — thymira audit RUN_ID

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `adapters/cli`
- **Depends on** `API-GOV`
- **Status** done
- **Deliverable** Add the `audit` command to adapters/cli: calls GET /runs/{id}/audit and renders controls (id, status, severity, detail), findings and the final Decision with Rich; supports --json for the raw AuditReport. No governance logic in the CLI (it is an API client).
- **Done when** uv run pytest tests/thymira/test_cli_audit.py -m integration -q passes: `thymira audit RUN` against a stub API prints each control row and the decision and exits 0 on PASS; --json emits a document that parses back to the AuditReport shape; a REQUIRE_HUMAN_REVIEW run prints the approval hint.
- **Seam (why FINAL needs no refactor)** Thin client over the audit route; unchanged from MVP to FINAL as MIRA grows from 3 to 7 agents (more findings, same report shape).
- **Specified at** docs/roadmap/mvp-3-weeks.md:2167

#### `CMP-01` — Compaction audit control (envelope, cannot-shadow-forward, payload shape)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** none
- **Status** done
- **Deliverable** Add the deferred compaction control to thymira.mira.checks.controls and register it: over context.compacted events it fails on a shadowed_seqs entry at or after the compaction's own seq (a compaction cannot shadow forward), a malformed shadowed_seqs payload, or a missing summarisation envelope (provider/model/tier/token counts). Uses thymira.events.derive_surface and shadowed_seqs.
- **Done when** uv run pytest tests/thymira/test_mira_compaction.py -q passes: the control is NOT_APPLICABLE with no compaction, FAILS on a context.compacted naming a seq at or after its own, FAILS on a context.compacted with no summarisation envelope, and PASSES a well-formed compaction whose envelope is recorded.
- **Seam (why FINAL needs no refactor)** This is the compaction audit control ADR-0006 explicitly defers to MIRA. It appends to CONTROLS (no rewrite) and reads only the frozen Event.surface machinery already built, so it lands whenever the compaction engine (THY/tools, FINAL) begins emitting context.compacted.
- **Scope note** The bracket-ordering half of this control was withdrawn by [ADR-0007](../adr/0007-compaction-is-atomic-no-bracket.md) (Accepted) and correction C-4: a compaction is a single append, so there is no unterminated state to detect and no opening event to order against. Do not add one.
- **Specified at** docs/adr/0006-log-vs-surface-and-compaction.md:73; docs/adr/0005-deepseek-harness-reuse.md:101

#### `CMP-02` — Compaction fidelity audit agent (summary vs shadowed events)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `CMP-01`
- **Status** done
- **Deliverable** Add an AuditAgentSpec + prompt that, for each context.compacted, compares the persisted safe-summary projection against the events it shadowed (recovered from the log by seq) and emits a finding when the summary omits or misrepresents a material shadowed fact. Never reads raw provider output (none is persisted, ADR-0006/ADR-0004).
- **Done when** uv run pytest tests/thymira/test_mira_compaction_fidelity.py -q passes with ScriptedProvider: a summary dropping a shadowed tool failure yields a fidelity finding with Evidence referencing the shadowed seqs; a faithful summary yields none.
- **Seam (why FINAL needs no refactor)** An audit agent over the runner (MIRA-02), so it answers ADR-0006's deferred 'does the summary faithfully represent what it replaced' without new infrastructure; depends on CMP-01's structural guarantees.
- **Specified at** docs/adr/0006-log-vs-surface-and-compaction.md:107

#### `CTRL-COVERAGE` — Requirements-coverage control (every framework requirement has evidence)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `KB-02`
- **Status** done
- **Deliverable** Add a control to thymira.mira.checks.controls and register it: for each Framework declared in the project's GovernanceConfig, read docs/governance/requirements_controls.json and fail when a mapped requirement's control produced no recorded evidence (event/artifact) in the run; the finding names the uncovered REQ and control_id.
- **Done when** uv run pytest tests/thymira/test_mira_checks_coverage.py -q passes: a run under EU_AI_ACT missing the art.12 event-logging evidence FAILS naming REQ-001-equivalent, a fully covered run PASSES, and a project declaring no frameworks is NOT_APPLICABLE.
- **Seam (why FINAL needs no refactor)** Deterministic counterpart to AUD-COMPLIANCE; both read the stable KB-02 requirements->controls schema. Appends to CONTROLS, no rewrite.
- **Specified at** docs/adr/0002-legacy-disposition.md:55; docs/architecture/e2e-baseline-v2.md:754

#### `CTRL-AUDIT-FRESHNESS` — Audit snapshot freshness control (later material evidence or invalidated artifacts)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`, `RA-CORE-04`
- **Status** done
- **Deliverable** Add the passive deterministic control `A25` in `thymira.mira.checks`: `assess_audit_freshness(report, events, *, store=None)` locates the report's `terminal_hash` in the hash-chained event history and returns a traceable result. It FAILS when later material execution evidence exists or when an artifact referenced by the audited snapshot is later invalidated; MIRA lifecycle and policy events written after that snapshot alone do not make it stale. It is NOT_APPLICABLE when the report has no terminal hash or the supplied history cannot contain that snapshot. The result is MIRA evidence only: it neither reopens a Run, schedules an audit, writes an event, nor calls the Gate.
- **Done when** `uv run pytest tests/thymira/test_mira_audit_freshness.py -q` passes: the unchanged snapshot PASSES; later modelling or tool evidence FAILS with the later event reference; a later artifact.invalidated event for a referenced artifact FAILS with both event and artifact references; later audit/policy lifecycle events alone PASS; and absent snapshot metadata is NOT_APPLICABLE.
- **Seam (why FINAL needs no refactor)** Uses the existing immutable `AuditReport.terminal_hash`, hash-chained `Event` history, and read-only `ArtifactStore` manifest. It is deliberately passive, so a future cross-owner invalidation or re-audit scheduler can consume the same evidence without changing this control, the AuditInput contract, Gate, or RunController.
- **Specified at** docs/adr/0011-mira-preflight-continuous-context-and-control-model.md; docs/adr/0010-local-jsonl-operational-state-and-verifiable-evidence.md

#### `CTRL-CV-BASELINE-REPRO` — Modelling controls A14 (test-once), A21 (fold-local CV), A22 (baseline) and A23 (reproducibility)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Implement A14 (the test partition is read once, for the final evaluation, with the OOF-selected threshold), A21 (CV preprocessing is fold-local and shared by all candidates — no cross-fold leakage), A22 (a trivial baseline was scored on identical folds), and A23 (each model carries reproducibility metadata: seed, library versions, single-thread, explicit estimator arguments). Register all four in CONTROLS.
- **Done when** uv run pytest tests/thymira/test_mira_checks_repro.py -q passes: A14 FAILS when the test set was touched before final evaluation, A21 FAILS when a fold's preprocessing was fitted outside its fold, A22 FAILS when no baseline shares the folds, A23 FAILS a model.trained event missing a seed, and all are NOT_APPLICABLE without modelling evidence.
- **Control ids** Renumbered by correction C-7. `A14` keeps its **inherited** meaning and nothing more: early or out-of-task test access. It previously also carried "a trivial baseline was scored on identical folds", which is a benchmarking gap, not a test-protocol breach — and `CR-101` maps `A14` to BLOCK with the reason *"evidence of train/test leakage"* and sets no `min_severity`, so a leakage-free model that merely skipped a baseline was blocked with a false statement about the data recorded as the reason. **A compound control is split, never widened**: the baseline half is now `A22`, deliberately outside any leakage rule. `A13` and `A18` revert to their inherited meanings — model-selection recomputation and report-versus-artifact fidelity — which **no task currently builds**; both are reserved. Their displaced checks are `A21` and `A23`.
- **Severity** Give `A22` a severity that cannot reach a BLOCK rule on its own. A missing baseline comparison is worth reporting and is not worth stopping a run.
- **Seam (why FINAL needs no refactor)** Appends to CONTROLS; reads the modelling/experiment evidence in the fixed AuditInput (GOV-01). These are the modelling-artifact controls ADR-0002 carried as specifications until THY emits the evidence — no contract change to switch them on.
- **Specified at** docs/adr/0002-legacy-disposition.md:46; docs/adr/0002-legacy-disposition.md:126

#### `CTRL-LEAKAGE-SPLIT` — Modelling controls A11 (split integrity) and A20 (leakage indicators) — recomputation

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Implement data-dependent controls A11 and A20 over the modelling evidence THY emits (dataset + train/test indices as artifacts referenced in AuditInput): A11 recomputes split integrity (no train/test overlap, complete coverage, non-empty, per-class allocation); A20 recomputes leakage indicators (exact target copy, \|corr\|>=0.95, identifier-suspected-yet-target-correlated columns, never winsorizing/imputing/encoding target or sensitive attributes). Register both in CONTROLS.
- **Done when** uv run pytest tests/thymira/test_mira_checks_modelling.py -q passes: A20 FAILS a fixture with a 0.99-correlated leaked feature and PASSES a clean one; A11 FAILS a fixture with overlapping train/test indices; both are NOT_APPLICABLE when no modelling evidence is present.
- **Control ids** Renumbered by correction C-7. Split integrity is the **inherited** meaning of `A11` (`docs/legacy/trazabilidad.md:101-118`), and it is what `credit_risk.yaml`'s `CR-101` already assumes `A11` means; this task previously assigned `A11` to leakage indicators and `A12` to split integrity, swapping both. Leakage indicators are a new check with no inherited ancestor, so they take `A20`. `A12` reverts to its inherited meaning — lineage coherence across split → candidates → selection → model → evaluation — which **no task currently builds**; it is reserved, not free.
- **Policy edit, this PR only** When `A20` is registered in CONTROLS, and not before, replace `LEAKAGE-001` with `A20` in `CR-101` (`runtime/policies/src/thymira/policies/defaults/credit_risk.yaml:19`) and in `CRX-103` (`examples/credit-risk/.thymira/policies.yaml:27`), drop `A12` from both, and rewrite the assertion at `tests/thymira/test_policies.py:181-184`. Editing the policy earlier points a live rule at a control nothing emits — the same dangling reference `LEAKAGE-001` already is.
- **Seam (why FINAL needs no refactor)** Recomputes over evidence rather than trusting THY (invariant: MIRA recomputes). Appends to CONTROLS; depends on THY/P3 emitting dataset+split evidence into the AuditInput MIRA-01 already assembles — the AuditInput shape (GOV-01) is fixed, so wiring the evidence needs no contract change.
- **Specified at** docs/adr/0002-legacy-disposition.md:46; docs/adr/0002-legacy-disposition.md:126

#### `CTRL-LINEAGE` — Modelling control A12 (lineage coherence across the modelling chain)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Implement A12 over the modelling evidence in AuditInput: recompute that the chain split -> candidates -> selection -> model -> evaluation is coherent — every candidate was fitted on the declared split, the selected model is one of the candidates, and the evaluated model is the selected one — resolving each link through artifact lineage (`input_artifact_ids`, `execution_key`) rather than through what THY reported. Register in CONTROLS.
- **Done when** `uv run pytest tests/thymira/test_mira_checks_modelling.py -k lineage` passes: A12 FAILS when the evaluated model is not the selected candidate, FAILS when a candidate references a split artifact other than the declared one, PASSES on a coherent chain, and is NOT_APPLICABLE without modelling evidence.
- **Seam (why FINAL needs no refactor)** Appends to CONTROLS and reads the fixed AuditInput (GOV-01); lineage is recomputed from the artifact manifest, which is the "MIRA recomputes rather than trusting success flags" invariant applied to the modelling chain.
- **Control id** `A12`'s **inherited** meaning, restored by correction C-7 — the roadmap had reused the number for split integrity, which is `A11`'s. Filed so the meaning has an owner instead of only a reservation: an inherited control left with no task is how a meaning disappears without anyone deciding to drop it.
- **Specified at** docs/legacy/trazabilidad.md:112; docs/adr/0002-legacy-disposition.md:46

#### `CTRL-MVP` — Deterministic controls A8, A15 and the routing-floor control

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `RISK-01`
- **Status** done
- **Deliverable** Add three event-fold controls to thymira.mira.checks.controls and register them in CONTROLS: A8 (every REQUIRE_HUMAN_REVIEW policy.decision was resolved by a human.approval before the run reached a terminal state), A15 (a risk-classification agent.message precedes the first tool.started), and A24 (inside each agent.started -> agent.completed lifecycle, every model.selected respects the role floor from thymira.agents.llm.routing.floor_for, every tool.started/tool.completed is preceded in that lifecycle by a valid model.selected, and agent.completed is preceded by at least one valid model.selected). `agent.started` is a non-effectful lifecycle opener and does not itself require a preceding selection; `routed_model` owns the one-selection-before-each-provider-call invariant.
- **Done when** uv run pytest tests/thymira/test_mira_checks.py -q passes: A8 FAILS a log with an unresolved review at run close and is NOT_APPLICABLE with none; A15 FAILS when a tool ran before any risk classification; A24 FAILS a MIRA lifecycle containing tier_applied=FAST, a tool event before any valid selection, or agent.completed with no selection, and does not fail merely because agent.started is the first event in its lifecycle.
- **Control ids** `A24` was filed as `A-ROUTE` until correction C-7. A non-numeric id sits outside the scheme no convention governs — the same anomaly as `LEAKAGE-001` — so the routing-floor control takes the next free number. `A15` is correct as written: it is the inherited meaning, and it is event-folding rather than data-dependent, which is why it is MVP (ADR-0002 carries a correction to that effect).
- **Open question for P4 — is this `A8`?** The inherited `A8` (`docs/legacy/trazabilidad.md:108`) is an *analytical* decision left unanswered. As scoped here it is an unresolved `REQUIRE_HUMAN_REVIEW`, which is the approval channel and sits close to the shipped `A7` (approval requests answered). Before implementing, confirm this is not a duplicate of `A7`, and if the analytical-decision channel is deferred to `RA-CORE-09`, say so — an inherited meaning that lapses silently is the same defect C-7 exists to stop, in reverse.
- **Seam (why FINAL needs no refactor)** Controls are appended to the CONTROLS tuple, so adding them never rewrites audit_run or existing controls. A15 reads the agent.message emitted by RISK-01; A24 reads model.selected emitted by the graphs (ADR-0004 consequence).
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:107; docs/adr/0002-legacy-disposition.md:46

#### `CTRL-REPORT` — Audit control A18 (report facts match the source artifacts)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Implement A18 over the analytical report artifact: every quantitative claim it makes resolves to a value actually recorded — a metric on an Experiment, a figure in a referenced artifact, or a digest in the manifest — and no claim cites a number that appears nowhere in the evidence. Register in CONTROLS.
- **Done when** `uv run pytest tests/thymira/test_mira_checks_report.py -q` passes: A18 FAILS a report quoting an accuracy no Experiment recorded, FAILS a report citing an artifact absent from the manifest, PASSES when every quoted figure resolves, and is NOT_APPLICABLE when the run produced no report artifact.
- **Seam (why FINAL needs no refactor)** Reads the report artifact and the Experiment records through the fixed AuditInput (GOV-01). The check resolves claim -> recorded value, so it is unaffected by how the report is generated or worded.
- **Control id** `A18`'s **inherited** meaning, restored by correction C-7 — the roadmap had reused the number for per-model reproducibility metadata, which is now `A23`. This is the control that stops a run from being reported as better than its own evidence.
- **Profile evidence** TOOL-14 reports include a versioned source declaration. A18 verifies the registered CSV/Parquet and captured schema digests, then independently recomputes the declared row/column profile. It does not promote profile numbers into ordinary report evidence. Missing declarations or mismatching facts are findings; older profiles must be regenerated. See `docs/superpowers/plans/2026-09-07-independent-profile-evidence.md`.
- **Specified at** docs/legacy/trazabilidad.md:118; docs/adr/0002-legacy-disposition.md:46

#### `CTRL-SELECTION` — Modelling control A13 (model selection re-derived independently)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Implement A13: re-derive the model choice from the recorded out-of-fold metrics and the declared selection strategy, and raise a finding when the recomputation disagrees with the model THY selected, or when the strategy is not recorded precisely enough to reproduce the choice at all. Register in CONTROLS.
- **Done when** `uv run pytest tests/thymira/test_mira_checks_modelling.py -k selection` passes: A13 FAILS a fixture whose recorded winner is not the metric-optimal candidate under the declared strategy, FAILS when the strategy is absent or ambiguous, PASSES when the recomputation agrees, and is NOT_APPLICABLE without modelling evidence.
- **Seam (why FINAL needs no refactor)** Reads the Experiment records and OOF metrics already carried by the fixed AuditInput (GOV-01). An unverifiable strategy is a finding in its own right, so the control keeps working when new selection strategies are added — it never needs to know them in advance, only that the recorded one reproduces.
- **Control id** `A13`'s **inherited** meaning, restored by correction C-7 — the roadmap had reused the number for fold-local CV, which is now `A21`.
- **Specified at** docs/legacy/trazabilidad.md:113; docs/adr/0002-legacy-disposition.md:46

#### `GOV-01` — Audit contract: AuditInput, AuditAgentOutput, AuditAgentSpec

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** none
- **Status** done
- **Deliverable** In runtime/mira create thymira.mira.audit_io with pydantic models AuditInput (run_id, events: tuple[Event,...], report: AuditReport \| None, artifact_names: tuple[str,...], experiment_ids: tuple[str,...], frameworks: tuple[Framework,...]) and AuditAgentOutput (agent_name, findings: tuple[AuditFinding,...], model_choice: ModelChoice \| None); and thymira.mira.agents.spec with AuditAgentSpec (name, framework: Framework, task_kinds: tuple[TaskKind,...], tool_allowlist: tuple[str,...], tier: ModelTier \| None, max_turns: int, system_prompt: str) plus load_specs(dir: Path) reading YAML declarations. These are the single interface every MiraGraph node and audit agent share — MIRA's own types, deliberately distinct from `THY-01`'s `AgentSpec`, which carries `role`/`max_depth`/`output_schema_ref` instead (see `THY-01`'s scope note). Neither task may widen its type to serve the other.
- **Done when** uv run pytest tests/thymira/test_mira_audit_io.py -q passes: an AuditAgentSpec round-trips from a YAML file, AuditInput.from_run(events, report) builds from an event sequence, and AuditAgentOutput rejects a finding whose framework differs from the spec's declared framework.
- **Seam (why FINAL needs no refactor)** The full AuditInput/AuditAgentOutput/AuditAgentSpec shapes are frozen now. Agents are declared as data (ADR-0004 decision 4), so going from the 3 MVP agents to the 7 FINAL agents adds YAML specs, never changes this contract or any caller.
- **Scope note — merge findings through the existing dedup, not a new step (correction C-13).** `AuditAgentOutput.findings` is a fourth input to `orchestrator.py::_deduplicate_findings`, which already merges risk, control-evaluation and deterministic-check findings into one tuple before `Gate.review_findings`. Do not add a second Gate call per agent (see correction C-5: exactly one `policy.decision` per audit) or a parallel merge path.
- **Specified at** docs/roadmap/mvp-3-weeks.md:1716; docs/adr/0004-model-routing-and-agent-topology.md:56

#### `GOV-03` — BudgetRule type + Policy.budget_rules field (defaulted empty)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** S · **Member path** `runtime/policies`
- **Depends on** none
- **Status** done
- **Deliverable** Frozen `BudgetRule` (`id`, `description`, `decision: Decision`, `reason`, `max_usd`, `max_tokens`, `max_tool_calls`, `scope`) and `ModelRule` (`POL-02`'s type, frozen alongside it for the same reason) added to `thymira.policies.models`; `Policy.budget_rules`/`.model_rules` default-empty tuples, threaded through `Policy.merged_with()` overlay-first like every other rule list.
- **Shape note (correction C-10)** The shipped shape differs from this task's original prose (which specified `metric: Literal[...]`/`soft_limit`/`hard_limit`/`soft_decision`/`hard_decision`): it matches what `main` had already built and this branch ported (`docs/roadmap/product-final.md` correction C-10), not what this entry originally described. `POL-01`'s deliverable must be written against the shipped fields (`max_usd`/`max_tokens`/`max_tool_calls`/`scope`), not the ones above.
- **Done when** `uv run pytest tests/thymira/test_policies.py -q` passes: `test_base_policy_hash_is_pinned_against_silent_field_additions` pins `policy_sha256(load_default_policy('base'))` against a checked-in constant, and `test_budget_and_model_rules_are_frozen_and_overlay_before_the_base` proves both fields overlay correctly and participate in the hash.
- **Seam (why FINAL needs no refactor)** Policy is content-hashed (policy_sha256, control A16). Adding budget_rules/model_rules LATER would change every policy's hash and break replay across the migration, so both fields land in the frozen Policy now (empty). `POL-01`'s `decide_budget` and `POL-02`'s `decide_model` then attach with zero contract change.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:87

#### `HITL-01` — Async human-approval flow: ApprovalService + PendingApproval fold + deferred Gate mode

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/policies`
- **Depends on** none
- **Status** done
- **Deliverable** Add thymira.policies.approval: PendingApproval derived as a pure fold of the log (human.approval_requested minus human.approval, ADR-0005 idea 8) via pending_approvals(events); an ApprovalService protocol with pending(run_id) and resolve(run_id, decision_id, *, approved, by: Actor) that appends the human.approval event and returns the resolved PolicyDecision; and a Gate 'deferred' mode where a REQUIRE_HUMAN_REVIEW with no in-process approver records human.approval_requested (with cost_so_far) and returns the unresolved decision (needs_human True) rather than blocking. LocalApprovalService resolves over the event log.
- **Scope note — this is the async approval both C-9 and C-15 defer to.** Two synchronous checkpoints already record a `human.approval` event but never trust a synchronous answer as a checkable `Approval`, both denying-and-recording through the one `allows_execution` rule: a `REQUIRE_HUMAN_REVIEW` tool call is denied synchronously by `ToolManager` + `allows_execution` (correction C-9), and THY's plan checkpoint is denied the same way by `Gate.check_action('plan.proposed')` + `allows_execution` (correction C-15). `HITL-01` gives both the asynchronous, checkable `Approval` path — the `ApprovalService` + `PendingApproval` fold + deferred `Gate` mode above — so a gated tool call can re-execute, and a plan can proceed, after a human resolves the decision outside the original request.
- **Done when** uv run pytest tests/thymira/test_policies_approval.py -q passes: in deferred mode a review decision leaves exactly one pending approval and needs_human True; LocalApprovalService.resolve appends a valid, chain-verifying human.approval and returns approved is True; resolving an unknown decision_id raises; approval events are log_only (never model_visible).
- **Seam (why FINAL needs no refactor)** ApprovalService protocol and PendingApproval fold are fixed in MVP. MVP resolves in-process over the local event log; FINAL is a Postgres-backed service driving LangGraph interrupt()/resume — same protocol, so the API and CLI approve path (API-GOV, CLI-APPROVE) never change (ADR-0005 idea 7; roadmap day 10).
- **Specified at** docs/adr/0005-deepseek-harness-reuse.md:106; docs/roadmap/mvp-3-weeks.md:695

#### `KB-01` — RegulationStore protocol + LocalRegulationStore + search_regulation tool

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** none
- **Status** done
- **Implemented (2026-08-28).** `thymira.mira.kb` now exposes a hash-verified
  `RegulationChunk` contract, the stable `RegulationStore` protocol, and a local JSONL store
  with deterministic keyword ranking. `thymira.mira.tools.search_regulation` serializes
  versioned, scored, backend-labelled matches and is invoked only through the shared Tool Manager.
- **Deliverable** Create thymira.mira.kb with RegulationChunk (source_id, version, framework: Framework, location, text, sha256), a RegulationStore Protocol (search(query, *, framework: Framework \| None, k: int) -> tuple[RegulationSearchResult,...]), and LocalRegulationStore (JSONL-backed, deterministic keyword/BM25-style ranking, no network). Register a search_regulation Tool (thymira.mira.tools.search_regulation) implementing the thymira.tools.Tool protocol with a declared ToolCapability (read-only, no external effects) so MIRA audit agents reach it only through the Tool Manager.
- **Done when** uv run pytest tests/thymira/test_mira_kb.py -q passes: LocalRegulationStore.search('human oversight', framework=Framework.EU_AI_ACT) returns chunks whose sha256 verifies, and the search_regulation tool run through a ToolManager produces a tool.started/tool.completed pair and a result_sha256.
- **Seam (why FINAL needs no refactor)** Same RegulationStore protocol and search_regulation tool signature in MVP and FINAL. MVP is a local keyword store; FINAL swaps in the pgvector backend (KB-04) with zero change to audit agents, the tool, or the Tool Manager wiring (baseline: pgvector KB).
- **Specified at** docs/architecture/e2e-baseline-v2.md:364; docs/architecture/e2e-baseline-v2.md:721

#### `KB-02` — requirements->controls mapping (English) + seed corpus + ingest script

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/mira`
- **Depends on** `KB-01`
- **Status** done
- **Deliverable** Port docs/legacy/compliance/mapeo_requisitos_controles.json into docs/governance/requirements_controls.json in English, keyed by Framework + requirement_id -> {source, location, requirement, control_id(s), evidence}, covering EU AI Act (art. 11/12/14), GDPR (art. 5/9/22) and the credit-risk methodology invariants (ADR-0002 appendix B). Add scripts/ingest_regulation.py that loads the mapping and a small seed regulatory/methodology corpus into a RegulationStore JSONL.
- **Done when** uv run python scripts/ingest_regulation.py --out data/regulation.jsonl then uv run pytest tests/thymira/test_mira_kb.py::test_requirements_controls_wellformed -q passes: every requirement resolves to a known Framework and at least one control_id, and each entry ingests into a searchable chunk.
- **Seam (why FINAL needs no refactor)** The requirements->controls file is the stable schema both the Compliance agent (AUD-COMPLIANCE) and CTRL-COVERAGE read. MVP ships a seed corpus; FINAL grows the corpus and re-ingests through the same schema and ingest script.
- **Specified at** docs/adr/0002-legacy-disposition.md:55

#### `KB-04` — PgVectorRegulationStore backend + lexical-vector ingestion

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `KB-01`, `KB-02`
- **Status** done
- **Deliverable** Implement PgVectorRegulationStore satisfying the RegulationStore protocol over PostgreSQL + pgvector: an experimental deterministic lexical term-frequency hashing projection (not a semantic embedding), a regulation table with vector index, and an ingestion path reusing scripts/ingest_regulation.py to write lexical vectors. Wire it behind the same search_regulation tool by configuration; a real embedding provider requires a later architectural decision and explicit configuration.
- **Done when** uv run pytest tests/thymira/test_mira_kb_pgvector.py -m integration -q passes against a dockerized pgvector: search returns the same top chunk as LocalRegulationStore for a shared fixture query, and RegulationChunk.sha256 matches the ingested source.
- **Seam (why FINAL needs no refactor)** Drop-in for LocalRegulationStore: identical RegulationStore protocol and search_regulation tool, so audit agents and the Tool Manager are untouched — the MVP-to-FINAL storage swap the baseline calls for (pgvector).
- **Specified at** docs/architecture/e2e-baseline-v2.md:721

#### `MIRA-01` — MiraAuditFlow + MiraSubgraph: prep -> preflight -> deterministic controls -> agent fan-out/fan-in -> Gate

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `MIRA-02`, `GOV-01`
- **Status** done
- **Implemented (2026-08-28; corrected 2026-09-04 — C-17).** `thymira.mira.flow.MiraAuditFlow` runs `prep -> preflight -> evidence -> deterministic -> agents -> verification -> aggregate`. The injected agent callable receives each ordered `AuditAgentSpec`, Run id and events, and deterministic report; its candidate findings join the existing tuple unchanged. `thymira.core.graph.adapters.MiraSubgraph` calls `MiraAuditFlow.preflight()` from Core's `preflight` node and `MiraAuditFlow.audit()` from its `mira` node; Core's `review` node then performs the one global stable deduplication, records each final `audit.finding`, reviews the resulting set exactly once through the Policy Engine, and emits `audit.completed`. Agent failures escape immediately without a Gate call or completion event. `MiraAuditFlow` still imports neither the runner nor P2/P3/core runtime modules.
- **Deliverable (corrected node order and dependencies — corrections C-12 through C-14, C-17)** `MiraAuditFlow` (`runtime/mira/src/thymira/mira/flow.py`) implements the orchestration `MiraAuditOrchestrator` performs, gate-less: node 1 Audit Preparation builds the snapshot and appends audit.started through an injected `EventLog`; node 2 **preflight** — inherent risk (`BaseRiskEvaluator`), pack applicability (`ApplicabilityResolver`), and evidence controls (`GenericPackControlRunner`), all already built — appends their typed evidence records through that log; node 3 the existing deterministic `audit_run` checks; node 4 fans out over the ordered AuditAgentSpecs applicable to the project's declared governance frameworks through an injected MIRA-owned callable that receives the spec, Run id/events, and deterministic report; node 5 adversarial verification drops unsupported agent candidates before the flow's own deduplication merges every source. `MiraAuditFlow` never calls a Gate itself — `MiraSubgraph` is the one production caller, and Core's `review` node makes the single `Gate.review_findings()` call over the fully merged finding set to produce the PolicyDecision before `MiraAuditFlow.complete()` appends audit.completed. `event_log.run_id` and the input Run id must match; `artifact_store` is read-only; `MiraAuditFlow` has no runtime import of `thymira.agents`, `thymira.tools`, or `thymira.core` (correction C-14, still true after C-17).
- **Done when** `uv run pytest tests/thymira/test_mira_flow.py tests/thymira/test_core_graph_adapters.py -q` passes: on a scripted run the flow emits audit.started, the preflight evidence records (risk assessment, pack bindings, control evaluations), audit.finding entries merged from preflight/deterministic/agent sources, audit.completed, and Core records exactly one policy.decision; the returned decision matches PolicyEngine.decide_findings over the same merged findings; MIRA writes no artifact to the read-only artifact store; the injected event log rejects a mismatched run id; and importing thymira.mira remains independent of thymira.agents/thymira.tools and their optional scientific dependencies.
- **Seam (why FINAL needs no refactor)** `MiraAuditFlow` consumes the existing snapshot/preflight/orchestrator types and returns findings to the Gate through fixed phase boundaries; the agent set is loaded from `AuditAgentSpec` data and filtered by the project's declared governance frameworks. Composition into the runtime is P1's runtime/core (dependency, not this task).
- **Specified at** docs/architecture/e2e-baseline-v2.md:754; docs/roadmap/mvp-3-weeks.md:2081; `docs/roadmap/product-final.md` corrections C-12 through C-14, C-17

#### `MIRA-02` — Audit agent node runner with bounded evidence projection (spec -> structured findings)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `GOV-01`
- **Status** done
- **Implemented (2026-08-28).** `thymira.mira.agents.runner` supplies an immutable
  `AuditAgentContext`, independent evidence projection limits, a read-only hash-pinned
  `EvidenceReader` protocol, and `run_audit_agent`. The runner validates every injected identity
  before appending events, derives a redacted bounded projection from `current_surface`, routes
  only through `routed_model`, exposes registered tools through the shared Tool Manager with the
  spec allowlist enforced at the manager boundary,
  assigns finding identity in code, and returns candidate findings without persisting or deciding
  them. `tests/thymira/test_mira_runner.py` covers the lifecycle, routing floor, tool path,
  evidence boundaries, truncation, and structured-output failure semantics.
- **Known import-boundary limitation (accepted for this P4 task).** Runner exports are lazy, so
  bare `import thymira.mira` does not load the runner or Tool Manager. Loading `AuditAgentSpec`
  still imports P2's routing package, whose existing eager `thymira.agents` initialisation loads
  the Tool Manager. Correcting that package initialisation is a P2 change prohibited from this
  task; no P2 or P3 code was modified.
- **Deliverable (corrected by C-16)** Create `thymira.mira.agents.runner` with a frozen, in-process `AuditAgentContext` carrying the injected `EventLog`, `Actor`, `agent_id`, `task_id`, optional `LLMProvider`, and paired optional `ToolRegistry`/`ToolContext`, plus an optional MIRA-owned read-only `EvidenceReader`. Define internal `EvidenceProjectionLimits` and `EvidenceExcerpt` values, and the protocol method `EvidenceReader.read_excerpt(evidence: Evidence, *, max_chars: int) -> EvidenceExcerpt`; the excerpt carries its reference, expected and actual sha256, bounded text, and whether it was truncated. Expose `run_audit_agent(spec: AuditAgentSpec, input: AuditInput, context: AuditAgentContext) -> AuditAgentOutput`; reject mismatched Run ids, an empty `task_kinds`, or half-configured tool dependencies. Append `agent.started`, then route with `Role.MIRA`, the first declared task kind, and the spec's tier through the existing `routed_model` unchanged; honour the STANDARD floor and use its PydanticAI path so callable tools and schema-validated output coexist (`complete_structured` remains its no-tool branch). Do not preselect a model, append a parallel `model.selected`, wrap a second provider gateway, or modify `runtime/agents`: `routed_model` records exactly one selection immediately before each provider request. Reuse the shared Tool Manager for all tool authorization/execution events, with the caller-owned spec allowlist enforced before policy evaluation, then finish with `agent.completed`. The model returns finding content, not authority-owned identity: the runner assigns `input.run_id`, `context.agent_id`, and `spec.framework`, then builds `AuditAgentOutput.from_spec(...)`. Return candidate findings only — do **not** append `audit.finding`, call the Gate, or merge sources; `MIRA-01` owns those three operations after deduplication.
- **Evidence projection** Before prompting, derive an immutable, redacted projection within the context's positive total/event/excerpt limits from `current_surface(input.events)`, the curated `AuditReport`, and the fixed summary fields on `AuditInput`. Never copy a `LOG_ONLY` or shadowed event payload into the prompt. `EvidenceReader` may return only excerpts for `Evidence(kind="artifact")` references already present in the report and carrying a sha256; the runner must reject an actual digest that differs, preserve the reference beside the excerpt, expose no unreferenced artifact, and have no write operation or capability escalation path.
- **Failure semantics** Exhausted structured-output validation records `agent.completed` with `status=FAILED` and the supplied `task_id`, then raises the stable public `LLMStructuredOutputError`; it never pads, silently drops, or persists a partial finding set.
- **Scope note — the graph receives this runner by injection (correction C-14).** `thymira.mira.graph` must not import this module, `thymira.agents`, or `thymira.tools` at runtime. Runtime/core composition injects `run_audit_agent`; isolated graph tests inject a scripted callable with the same MIRA-owned input/output contract.
- **Done when** `uv run pytest tests/thymira/test_mira_runner.py -q` passes with `ScriptedProvider`: the context rejects Run/tool identity mismatches and empty task kinds; a spec yields findings normalized to its Run, agent, and framework; `agent.started` precedes the first request, every provider request has exactly one immediately preceding `model.selected`, and every applied tier is at least STANDARD; an allowed `search_regulation` call passes through the shared Tool Manager and a call outside the allowlist is denied; injected small limits truncate the prompt deterministically; only a hash-verified referenced excerpt reaches it while a `LOG_ONLY` event, an unreferenced artifact, and a secret-like field do not; the reader performs no write; the runner emits no `audit.finding` or `policy.decision`; and malformed output produces the FAILED completion event before `LLMStructuredOutputError` escapes. Runtime/tool implementation changes remain shared and small; no second tool system is introduced.
- **Seam (why FINAL needs no refactor)** One context and runner drive every audit agent regardless of framework; agents differ only by their `AuditAgentSpec` data. The read-only reader and full execution dependencies are bound before the existing graph injection boundary, so `thymira.mira.graph` retains its MIRA-owned callable and independence from agents/tools/core. FINAL agents and evidence backends replace injected data/dependencies without changing `AuditInput`, `AuditAgentOutput`, or the graph's single aggregation/Gate path.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:84

#### `MIRA-03` — Adversarial finding verification, discovery loop, evaluator-optimizer rework signal

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/mira`
- **Depends on** `MIRA-01`
- **Status** done
- **Deliverable** Replace the MVP identity verification node in MiraGraph with: an adversarial-verification node that re-checks each candidate finding (a second-pass judge) before it reaches the Policy Engine; a discovery loop that re-runs audit agents until no new finding appears; and an evaluator-optimizer boundary emitting a structured rework signal (the PolicyDecision) that THY consumes for rework — MIRA never messages THY directly (ADR-0004 decision 5).
- **Done when** uv run pytest tests/thymira/test_mira_verification.py -q passes with ScriptedProvider: an unsupported candidate finding is dropped by the verification node and never becomes an audit.finding, the discovery loop terminates when a pass yields no new finding, and the rework signal is a PolicyDecision (never an agent.message to THY).
- **Seam (why FINAL needs no refactor)** Slots into the fixed verification/aggregation node boundary MIRA-01 established, so no upstream node or the Gate changes. Only the node's implementation goes from pass-through to adversarial.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:94

#### `POL-01` — Budget-threshold decisions (decide_budget + Gate.check_budget)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** M · **Member path** `runtime/policies`
- **Depends on** `GOV-03`
- **Status** done
- **Deliverable** Add PolicyEngine.decide_budget(run_id, usage: Mapping[str,float]) evaluating Policy.budget_rules (GOV-03): WARNING at a soft limit, REQUIRE_HUMAN_REVIEW to continue past a hard limit, BLOCK-precedence preserved; and Gate.check_budget(usage, ...) recording the decision like any other. Reads a usage snapshot supplied by the runtime's usage ledger.
- **Done when** uv run pytest tests/thymira/test_policies_budget.py -q passes: usage below all limits returns PASS, crossing a soft limit returns WARNING, crossing a hard limit returns REQUIRE_HUMAN_REVIEW, and the decision quotes the matched BudgetRule id and the recomputed policy_sha256.
- **Seam (why FINAL needs no refactor)** Attaches to the Policy.budget_rules field GOV-03 already froze, so no policy hash migration. Budget thresholds are Policy Engine rules enforced through the existing Gate (ADR-0004 decision 6); the usage ledger it reads is P1/runtime/core.
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:87

#### `POL-02` — Model allow-list policy (a substitution is a WARNING)

- **Tier** FINAL · **Owner** P4 (MIRA / Governance) · **Size** S · **Member path** `runtime/policies`
- **Depends on** none
- **Status** done
- **Deliverable** Add a ModelPolicyRule (allowed model-id patterns per role) and PolicyEngine.decide_model(model_choice: ModelChoice) that returns PASS for an allowed model and WARNING for a substitution/blocked model (never silent), recorded via the Gate. Add the defaulted model_rules field to Policy under the same hashing discipline as GOV-03.
- **Done when** uv run pytest tests/thymira/test_policies_model_allowlist.py -q passes: an in-allowlist model.selected returns PASS, an out-of-allowlist one returns WARNING naming the rule, and base.yaml's policy_sha256 is unchanged by the empty-defaulted field.
- **Seam (why FINAL needs no refactor)** The model_rules field is defaulted-empty and added under the A16 hash-stability check, so it is safe in a frozen Policy; the decision path is the same Gate-recorded flow (ADR-0004 table: an allow-list of model ids is a policy rule).
- **Specified at** docs/adr/0004-model-routing-and-agent-topology.md:92

#### `QA-DEMO-GOV` — Credit-risk governance demo path + documentation

- **Tier** FINAL · **Owner** P5 (CLI / UX / QA / Integration) · **Size** M · **Member path** `examples/credit-risk`
- **Depends on** `QA-E2E-GOV`
- **Status** done
- **Already in the repository** examples/credit-risk/.thymira/policies.yaml; examples/credit-risk/.thymira/config.yaml; examples/credit-risk/README.md. Still missing: The 7-agent MiraGraph run through a real `thymira audit`/`thymira approve` CLI, an assurance bundle, a docs/ governance quickstart, and tests/thymira/test_demo_governance.py (none exist).
- **Deliverable** Wire the examples/credit-risk project so the demo governance flow runs against the full 7-agent MiraGraph: `thymira audit RUN` shows the credit-risk findings and REQUIRE_HUMAN_REVIEW, `thymira approve RUN` resolves it, and the run completes with an assurance bundle. Document the flow in examples/credit-risk/README.md and a governance quickstart in docs/.
- **Done when** uv run pytest tests/thymira/test_demo_governance.py -m integration -q passes the scripted credit-risk demo end to end (audit -> require review -> approve -> completed), and the README commands match the CLI output.
- **Seam (why FINAL needs no refactor)** Uses the same CLI (CLI-AUDIT/CLI-APPROVE) and routes (API-GOV) as the MVP E2E test (QA-E2E-GOV); only the agent roster and KB corpus grew, so the demo path is the MVP path with more findings.
- **Specified at** docs/roadmap/mvp-3-weeks.md:927; docs/roadmap/mvp-3-weeks.md:960

#### `QA-E2E-GOV` — E2E governance test: PASS / WARNING / REQUIRE_HUMAN_REVIEW+approve / BLOCK

- **Tier** MVP · **Owner** P5 (CLI / UX / QA / Integration) · **Size** L · **Member path** `tests/thymira`
- **Depends on** `MIRA-01`, `HITL-01`, `CLI-AUDIT`, `CLI-APPROVE`, `AUD-METHODOLOGY`, `AUD-RISK`, `AUD-EUAIACT`
- **Status** done
- **Deliverable** Add tests/thymira/test_e2e_governance.py (integration) driving a full run through CLI -> API -> MiraGraph -> Policy Engine for the four cases required by the roadmap: a clean run (PASS), a medium-finding run (WARNING), a high-confidence credit-risk finding (REQUIRE_HUMAN_REVIEW then thymira approve resumes to COMPLETED), and a critical finding (BLOCK with an audit.block event and nothing executing after it). Uses ScriptedProvider; asserts the event chain verifies at each step.
- **Done when** just test-slow (or uv run pytest tests/thymira/test_e2e_governance.py -m integration -q) passes all four cases: each produces a policy.decision matching its expected Decision, the approve case transitions the run to COMPLETED, and verify_events passes on every run's final log.
- **Seam (why FINAL needs no refactor)** Exercises the governance surface end to end so the MVP demo path is regression-locked; the same four cases carry into the FINAL demo (QA-DEMO-GOV) unchanged.
- **Specified at** docs/roadmap/mvp-3-weeks.md:778

#### `RISK-01` — Deterministic Risk classifier override layer (thymira.agents.risk)

- **Tier** MVP · **Owner** P4 (MIRA / Governance) · **Size** L · **Member path** `runtime/agents`
- **Depends on** none
- **Status** done
- **Deliverable** Port the thesis risk.py into thymira.agents.risk: classify_risk(facts, provider: LLMProvider, *, run_risk_assessment: RiskAssessment \| None) -> RiskProfile — the LLM classifies via complete_structured into a local structured-output schema, then the eleven deterministic override rules apply (code only tightens a level, forces needs_human_review, never lowers), plus the classify -> needs-information -> needs-human-review loop and fail-closed error handling. The returned RiskProfile is thymira.policies.RiskProfile (the exact type Gate.decide_capability consumes). Record the classification as an agent.message event (structured summary, never chain-of-thought).
- **Scope note — `RiskProfile` is floored by `RiskAssessment`, never independent of it (correction C-11).** This task's LLM-structured intermediate schema is a different, local type from `thymira.schemas.RiskAssessment` (MIRA's versioned, evidence-backed inherent-risk record for one `ActivityProfile`) — naming both "RiskAssessment" was a naming collision waiting to happen, not two independent concepts. `classify_risk` must take the Run's current `RiskAssessment` (via the new `run_risk_assessment` parameter above) when one exists, and treat its `risk_level` as a floor: the eleven override rules may escalate `RiskProfile.risk_level` above it, never set it below the activity's already-assessed inherent risk. `RiskAssessment` is not consumed as a legal classification or authorization by doing this — it remains MIRA evidence only (ADR-0008); `classify_risk` reads it as one more fact.
- **Done when** uv run pytest tests/thymira/test_agents_risk.py -q passes using ScriptedProvider: an LLM 'low risk' classification on a case declaring a sensitive attribute is overridden upward to needs_human_review, an LLM error yields a fail-closed high-risk/needs-review RiskProfile, a supplied `RiskAssessment` with `risk_level=HIGH` prevents the LLM from classifying below `high` even when it tries to, and every override path emits exactly one agent.message.
- **Seam (why FINAL needs no refactor)** Output is the existing RiskProfile that the Policy Engine already consumes, so the Tool Manager's capability gating never changes. MVP does a single-pass classify+override; FINAL adds the multi-round needs-information loop behind the same classify_risk signature and the same eleven override rules.
- **Specified at** docs/adr/0002-legacy-disposition.md:48; `docs/roadmap/product-final.md` correction C-11
