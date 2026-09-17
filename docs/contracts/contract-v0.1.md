# Contract v0.1 — proposal for MVP day 1

*Status: in force. The current shared-schema version is `0.9`; its operational integration is
partial and its JSON/JSONL storage semantics are defined in `contract-v0.3.md`. Every change is a
pull request that also updates the consumers (owner P1).*

## Principles

- **Immutable, strict records.** Every contract is a frozen pydantic model that rejects unknown
  fields. State changes produce new records; human resolutions are separate `Approval` records.
- **Closed vocabularies.** Statuses, event types, decisions, severities and frameworks are
  `StrEnum`s; a new value is a contract change.
- **Ids are self-describing.** `run_…`, `agent_…`, `tool_…` (`new_id(kind)`); display numbers
  are a client concern.
- **Evidence is verifiable.** Artifacts carry `sha256` and lineage; events are hash-chained;
  findings point at events/artifacts/experiments by id (and hash when available).
- **The LLM never decides.** `AuditFinding` carries severity and confidence; only
  `PolicyDecision` carries a `Decision` (`PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`), and
  only the Policy Engine creates one.

## Records

| Record | Purpose | Key fields |
|---|---|---|
| `Session` | a client conversation that starts runs; holds no run state | `client`, `run_ids` |
| `Run` | the backbone object | `prompt`, `status` (`RunStatus`), `git_commit`, id lists of children, `policy_decision_id`, `final_decision` |
| `Agent` | an agent instance in a run | `name`, `layer` (`execution`/`audit`), `parent_agent_id`, `model` |
| `Task` | delegated work and its outcome | `objective`, `status`, `depends_on`, `summary`, `artifact_ids` |
| `ToolCall` | a call through the Tool Manager | `tool_name`, validated model-visible `arguments` (redacted in presentation copies), `status`, `policy_decision_id`, `result_sha256`, `sandbox_mode`, `sandbox_enforcement` |
| `Artifact` | a content-addressed output | `name`, `kind`, `uri`, `sha256`, `produced_by`, `input_artifact_ids`, `execution_key`, `valid` |
| `Experiment` | a tracked training/evaluation | `parameters`, `metrics`, `seed`, `model_artifact_id`, `tracker_run_id` |
| `Event` | one fact in the append-only, hash-chained log | `event_id`, `seq`, `type` (`EventType`), global `schema_version`, `actor`, `surface` (`EventSurface`), producer and correlation metadata, source-credential-scrubbed canonical `payload`, `prev_hash`, `hash` |
| `AuditFinding` | what an audit agent observed | `control_id`, `framework`, `severity`, `confidence`, `evidence[]`, `recommendation` |
| `PolicyDecision` | the engine's deterministic verdict | `decision`, `rule_id`, `policy_name`, `policy_sha256`, `execution_constraints` |
| `ExecutionConstraints` | closed, authorised limits for a THY execution | tool allow/deny lists, local-only execution, required human review/evidence, prohibited actions and tool-call ceiling |
| `RunState` | current state expressed as independent dimensions | `stage`, `condition`, `wait_reason`, `outcome`, `version` |
| `ActivityProfile` | versioned preflight context for one activity | stable activity identity, project or Run owner, purpose, people, effect, autonomy, oversight, jurisdiction, data and evidence |
| `RiskAssessment` | versioned inherent-risk evidence for one profile version | profile id/version, risk facts, evidence, method version, missing information, supersession |
| `PackBinding` | reproducible applicability result | profile id/version, pack and rules versions, jurisdiction, rationale, activating facts and evidence |
| `ControlEvaluation` | evidence-based outcome for one pack control | binding, pack/version, control, closed status, evidence, evaluation time, supersession |
| `DecisionContext` | immutable, bounded MIRA context snapshot — evidence, never an instruction | run id, grounded summary and statement tuples (facts, constraints, risks, evidence available/gaps, open questions, checkpoints), evidence, pinned run version and terminal event seq/hash |
| `DecisionContextStatement` | one bounded context assertion tied to evidence or marked unknown | bounded text, evidence refs, unknown flag; requires evidence or `unknown=true`, never chain-of-thought |
| `ActionIntent` | bounded requested action before authorization | requester, action kind, subject, purpose, evidence, idempotency key |
| `AuthorizationContext` | immutable scope of one potential effect | intent and decision references, policy hash, subject, constraints, approval requirement |
| `Approval` | human response separate from a policy decision | policy decision reference, exactly one authorization-context hash, outcome, actor |
| `ProjectConfig` | typed `.thymira/config.yaml` | `project`, `experiments`, `agents`, `governance.frameworks` |
| `SubagentResult` | how one delegated child settled | `stop_reason` (`StopReason`), `delegation_key`, `delegation_depth`, `result_json`/`result_schema`, bounded `diagnostics` |

## Run lifecycle

```
CREATED → PLANNING → RUNNING ⇄ EXPERIMENTING → AUDITING → COMPLETED
                        │            │             │
                        └──────── WAITING_FOR_APPROVAL ──┘
most non-terminal states → FAILED | BLOCKED (CREATED has no BLOCKED transition;
WAITING_FOR_APPROVAL has no FAILED transition — see `RUN_TRANSITIONS`)
```

`can_transition(current, target)` and `Run.with_status()` enforce the table in
`thymira.schemas.enums.RUN_TRANSITIONS`.

## Event API vocabulary

`run.started`, `run.completed`, `run.failed`, `run.transitioned`, `agent.started`, `agent.parked`, `agent.completed`,
`agent.message`, `model.selected`, `tool.started`, `tool.completed`, `tool.denied`,
`experiment.started`, `experiment.completed`, `model.trained`, `artifact.created`,
`artifact.invalidated`, `audit.started`, `audit.finding`, `audit.completed`, `audit.block`,
`policy.decision`, `human.approval_requested`, `human.approval`, `activity_profile.recorded`,
`activity_profile.questioned`, `activity_profile.answered`, `risk_assessment.recorded`,
`risk.classified`, `pack_binding.recorded`, `control_evaluation.recorded`,
`mira.context_created`, `context.compacted`, `subagent.settled`.

## Changes

- **0.9 (2026-09-08)** — Contract 0.9 closes the subagent stop-reason vocabulary, distinguishes
  the non-terminal `agent.parked` checkpoint from `agent.completed`, and adds the
  uniform settlement record. `StopReason` is a closed `StrEnum` with exactly six values —
  `completed`, `stopped`, `out-of-room`, `declined`, `failed`, `abnormal` — and an unknown value
  is rejected at the model boundary rather than coerced or defaulted. `SubagentResult` is the
  frozen record every delegated child settles with: the run/task/agent identity, the child spec
  and its parent, the objective, the `delegation_depth` it ran at, a `delegation_key` (a sha256
  digest of the persisted run, task and agent invocation ids, parent linkage, child spec,
  objective and depth; repeated instructions therefore remain distinct delegations). Each
  invocation has exactly one durable settlement; retries and forks mint new task and agent
  invocation ids and therefore new keys. Canonical selection is defensive within one key, while
  choosing across distinct attempts is a higher-level consumer concern. The
  `stop_reason`, the schema-constrained completion serialised once
  (`result_json`) with the schema that validated it (`result_schema`, `module.path:ClassName`) —
  both present exactly when the reason is `completed` — and the bounded, redacted `diagnostics`
  with the bound recorded as a fact (`diagnostics_limit`, `diagnostics_truncated`). The event type
  `subagent.settled` carries one such record, `log_only`, as the durable settlement notice.
  Additively, `agent.started`/`agent.completed` payloads gain `parent_agent`/`delegation_key`/
  `delegation_depth` when the step is a delegation, `agent.completed` gains `stop_reason`, and
  `agent.message` gains `task_id`/`delegation_key`/`stop_reason`. ADR-0013 decision 6: pre-1.0,
  no compatibility shim and no migration.
  A pending `agent.parked` payload is non-terminal and must retain the complete invocation
  binding: `task_id`, `agent`, `objective`, `parent_agent`, `delegation_depth`,
  `delegation_key`, `step_key`, and `execution_identity_sha256`, alongside `status=PENDING` and
  `end_reason=awaiting_approval`. A continuation reuses that exact binding and emits one
  terminal `agent.completed`, followed by its `subagent.settled` record and keyed `agent.message`.
- **0.8 (2026-09-05)** — Contract 0.8 adds `ProjectConfig.datasets`, a tuple of `DatasetConfig`
  (`name`, unique within the project; a `path` relative to and contained in the project
  directory; an optional `target` column). It is the project's declaration of the datasets THY's
  Inspect registers into the Run before any agent runs; additive and defaulted to empty.
- **0.7 (2026-09-03, PR #90, recorded after the fact)** — Contract 0.7 added
  `ExecutionConstraints` / `ExecutionAction` and `PolicyDecision.execution_constraints` (the typed
  execution contract the pre-THY Gate records), plus the event types `rework.started` and
  `rework.escalated`. The code bumped `CONTRACT_VERSION` to `0.7` without this entry.
- **0.6 (2026-08-31)** — Contract 0.6 adds the closed event types
  `activity_profile.questioned`, `activity_profile.answered`, and `risk.classified` for the
  event-backed risk interview and the deterministic classification recorded before THY executes.
- **0.5 (2026-08-25)** — Contract 0.5 adds bounded immutable `DecisionContext` snapshots and
  grounded `DecisionContextStatement` records. Statements cite known evidence or explicitly mark
  an unknown; the snapshot is pinned to a Run version and terminal event-log head.
  `mira.context_created` records an explicit creation without authorizing an action, changing Run
  state, or connecting THY to MIRA.
- **0.4 (2026-08-25, ADR-0011)** — Contract 0.4 adds immutable, strict MIRA
  `ActivityProfile`, `PackBinding`, and `ControlEvaluation` records. Risk assessments now pin one
  activity-profile version and represent inherent risk only. `AuditDisposition`
  (`PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`) is separate from
  `AuthorizationDecision` (`ALLOW | ALLOW_WITH_WARNING | REQUIRE_HUMAN_REVIEW | DENY`); in
  particular, an audit `BLOCK` is never an authorization action. The published Contract 0.1
  `Decision` name remains an explicit alias for `AuditDisposition`; new authorization contracts
  must use `AuthorizationDecision`. Four narrowly scoped event types record the new immutable
  evidence records. No storage, packs, preflight service, orchestrator, or THY integration is
  added.
- **0.3 (2026-08-25, ADR-0008, ADR-0010 and ADR-0011)** — Contract 0.3 adds a strict composite Run
  state, versioned `RiskAssessment`, `ActionIntent`, `AuthorizationContext`, and separate
  `Approval` records. `PolicyDecision` no longer embeds a human resolution. Event envelopes add
  `event_id`, `schema_version`, `producer`, `producer_version`, `correlation_id`, `causation_id`,
  and `authorization_context_sha256`. `Event.schema_version` is also the global event-log format
  version and is mandatory on every serialized event; readers refuse unsupported or missing
  versions and unknown event types. Authorization contexts hash only canonical semantic fields,
  excluding record ids and timestamps. Contract 0.2 event migration is deliberately out of scope.
- **0.2 (2026-08-22 – 2026-08-23)** — additive; two record shape changes, both new defaulted
  fields: `Event.surface` and the `ToolCall` sandbox pair:
  - *ADR-0004* — two event types: `model.selected` (the router's choice for one LLM call: role,
    task kind, tier requested/applied, model id, reason) and `agent.message` (a message between a
    sub-agent and its orchestrator, always through the orchestrator's graph state — never peer to
    peer, never between THY and MIRA).
  - *ADR-0006* — `Event.surface` (`EventSurface`: `model_visible` | `log_only`, default
    `log_only`) records whether an event may ever be shown to a model, and the `context.compacted`
    event type names the seqs a compaction shadowed. Shadowing (`SurfaceState`) is derived by
    folding the log (`thymira.events.derive_surface`), never stored, so a chained event is never
    edited.
  - *Sandbox vocabulary* — `SandboxMode` (`read_only` | `workspace_write` | `danger_full_access`)
    and `SandboxEnforcement` (`full` | `partial` | `unusable`), with `ToolCall.sandbox_mode` and
    `ToolCall.sandbox_enforcement` (both defaulted `None`). Enforcement is a reported fact: a run
    that was not confined records `partial`, never `full`. **No `CONTRACT_VERSION` bump:** these
    fields are part of 0.2 and this entry is what says so, so at 0.2 `CONTRACT_VERSION = "0.2"`
    already described the shape a consumer got (the value has since advanced — the current one is
    the status line at the top of this document and `thymira.schemas.CONTRACT_VERSION`). A version
    number names what a version contains, not when the sentence describing it was written — bumping
    to 0.3 for a field 0.2 already carries would have told every consumer to re-pin for a change
    that never happened. Roadmap task `TOOL-01` closes
    what actually remains: the schema round-trip tests.
- **0.1 (2026-08-20)** — initial contract.

## Open points for day 1

1. Ids: random UUID4 with prefix now; switch to UUID7 (time-ordered) when Python 3.14 is the
   floor, or adopt `uuid6` — decide before the first migration.
2. `Run.final_decision` duplicates the referenced `PolicyDecision.decision` for cheap reads;
   keep or drop.
3. Whether `Event.payload` size is capped (large outputs belong in artifacts).
4. Experiment ↔ MLflow field mapping (`tracker_run_id`, artifact URIs) once P3 wires MLflow.

The HTTP shape for the first clients is frozen in [`api-v0.1.md`](api-v0.1.md).
