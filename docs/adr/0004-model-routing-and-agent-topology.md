# ADR-0004 — Model routing and agent topology

- Status: Accepted (2026-08-22)
- Deciders: the owner
- Extends: ADR-0001 (two orchestrators), ADR-0003 (names; models are configuration)

## Context

Three questions from the owner: can THY and MIRA run on *different* frontier models; can
orchestrators and sub-agents use smaller, faster models for mechanical steps (choosing a tool,
summarising, extracting); and can they run workflows the way coding agents such as Claude Code
do, talking to each other along the way.

What the reference practices say (checked 2026-08-22):

- Claude Code declares each subagent with its own `model` (an alias, a full id, or `inherit`),
  a tool allowlist, an isolated context, a depth limit for nested agents, and documents the
  tiered approach explicitly ("haiku for exploration, sonnet for balanced tasks, opus for
  complex reasoning only when necessary"); resolution is environment → per-invocation →
  definition → parent. Its hooks (`PreToolUse`, `PostToolUse`, `SubagentStart/Stop`, …) are
  deterministic gates that can allow, deny or escalate a tool call.
- Anthropic's *Building effective agents* distinguishes workflows (predictable, code-defined
  control flow: prompt chaining, routing, parallelization, orchestrator-workers,
  evaluator-optimizer) from agents (model-driven control flow), recommends starting with
  workflows, and recommends routing easy or common work to small, cost-efficient models.
- PydanticAI supports agent delegation (an agent calls another inside a tool, sharing
  `usage`), programmatic hand-off, graph-based control flow for the complex cases, and
  `UsageLimits` (cost, requests, tokens, tool calls) across agents; it notes that with
  different models per agent, cost cannot be reconstructed from aggregate token counts.

Thymira's invariants already answer part of this: *LLM proposes, code authorizes*; every
tool call goes through the Tool Manager and the Permission Policy; THY and MIRA never import
each other; evidence is verifiable.

## Decision

1. **Each orchestrator has its own model.** `THYMIRA_THY_MODEL` and `THYMIRA_MIRA_MODEL`
   (falling back to `THYMIRA_ORCHESTRATOR_MODEL`, then the FRONTIER tier, then
   `THYMIRA_MODEL`). A different vendor for MIRA is recommended, not required: an auditor
   that shares the auditee's model shares its blind spots.
2. **Three tiers, routed by code.** `FRONTIER` (plan, decide, synthesize, audit_judgement),
   `STANDARD` (analyze, code, review), `FAST` (select_tool, summarize, extract, classify,
   format), configured as `THYMIRA_MODEL_FRONTIER|STANDARD|FAST`. An agent declares the
   *kind of task* and may *request* a tier; `thymira.agents.llm.routing.choose()` applies the
   table and the per-role floor (`THYMIRA_<ROLE>_MIN_TIER`; MIRA's default floor is STANDARD
   so the audit is never cheaper than the work it audits) and returns a `ModelChoice` with a
   reason. **A model never picks a model id.** The choice is written to the thread as a
   `model.selected` event, so cost, speed and quality trade-offs are auditable per call.
3. **Workflows are graphs, not prompts.** ThyGraph and MiraGraph are LangGraph graphs whose
   control flow is code (the Anthropic "workflow" patterns: chaining, routing, parallel audit
   agents, evaluator-optimizer between MIRA's findings and THY's rework). Model-driven
   control flow exists only *inside* a node (an agent choosing among its allowed tools).
   Deterministic hooks are the Tool Manager + Permission Policy + `Gate`: every tool call is
   allowed, escalated or denied by code and recorded (`tool.started/completed/denied`).
4. **Sub-agents are declared as data**, Claude Code style: name, role, task kinds, tool
   allowlist, tier, max turns, max depth (orchestrator → sub-agent → helper; nothing deeper),
   isolated context seeded by the orchestrator's task. Declarations live next to the
   orchestrator that owns them (`runtime/thy/.../agents/`, `runtime/mira/.../agents/`) and are
   loaded, never hard-coded. (Shape to be fixed with the first PydanticAI agent, MVP week 1.)
5. **Communication is hub-and-spoke and on the thread.** Sub-agents talk to their
   orchestrator through the graph state and `Task` records; an exchange worth keeping is an
   `agent.message` event (structured summary, never chain-of-thought). Sub-agents never call
   each other directly. THY and MIRA never exchange messages: MIRA reads the thread; its
   findings reach THY only as Policy Engine decisions (`policy.decision` → rework, human
   review, or block).
6. **Budgets are shared and enforced.** A run carries usage limits (cost, requests, tokens,
   tool calls) that every delegated call charges against (PydanticAI `usage=ctx.usage`);
   per-call cost is taken from the provider response (`LLMResponse.cost_usd`) because models
   differ in price. The usage ledger (ADR-0002 backlog) aggregates them.

## Reference: Claude Code dynamic workflows → Thymira

Studied on 2026-08-22 (`/workflows`, docs: code.claude.com/docs/en/workflows). A dynamic
workflow is a JavaScript script the runtime executes in the background: *the script holds the
plan* (who runs next, loops, branching), intermediate results live in script variables rather
than in a model's context, the orchestration is repeatable (saved as a `/name` command under
`.claude/workflows/`, distributable in plugins, parameterised with `args`), and a run is
resumable because the runtime journals every agent's result. That is the shape Thymira's
graphs must have; the table fixes the correspondence so the MVP builds the right thing.

| Claude Code workflows | Thymira |
|---|---|
| "Who decides what runs next: the script" (vs. subagents/skills/teams, where the model decides turn by turn) | ThyGraph / MiraGraph: control flow is code; a model decides only *inside* a node, among its allowed tools (Decision 3) |
| `agent(prompt, {schema})` — the subagent is forced to return validated structured output, never free text | a PydanticAI agent returning a `ThymiraModel` through `complete_structured`; an invalid response is an error, never padded |
| `pipeline()` (per-item stages, no barrier), `parallel()` (barrier), `phase()` | LangGraph fan-out/fan-in nodes; Thymira's `Phase` machine (`understanding → preparation → modeling → evaluation → reporting`) is the phase list the user approves and follows |
| `agent()` returns `null` when stopped or on a terminal API error; results are `.filter(Boolean)`-ed | a failed sub-agent is a `Task` in `FAILED` state on the thread; the graph decides (retry node, degrade, or stop), never silently drops |
| `budget` — a hard ceiling shared by the whole run; `agent()` throws once spent; "Large workflow" warning at 25 agents / 1.5M projected tokens | per-run `UsageLimits` charged by every delegated call; the usage ledger (ADR-0002 backlog); budget thresholds are **Policy Engine rules** (`WARNING` at the soft limit, `REQUIRE_HUMAN_REVIEW` to continue past the hard one) |
| Every run writes its script to a file; the `/workflows` view shows phases, agent counts, token totals, elapsed time; each agent's prompt, tool calls and result are inspectable | provenance at `run.started` gains the **graph definition hash** (ThyGraph/MiraGraph version) next to `policy_sha256`; the Event API + `thymira status` render phases, agents, tokens and cost from the thread |
| Journal + resume: completed agents replay from cache in start order; cached results stop at the first unfinished agent; a fan-out of small agents preserves more progress than one long agent | the **idempotency / lineage mechanism** (ADR-0002 backlog, `thymira.core`): every agent/tool step is keyed by a hash of its inputs; resume replays the thread to the first unfinished step; LangGraph checkpoints are stored through `thymira.state`. Design rule: many small steps over one long one |
| The plan (phases) is shown and approved before the run; consent is recorded per workflow and project; no mid-run user input — "for sign-off between stages, run each stage as its own workflow" | THY's plan is a `policy.decision` → `human.approval` before execution (HITL, roadmap day 10); phase boundaries are `Gate` checkpoints; reopening a phase is a budgeted, justified decision (`can_reopen`) |
| Subagents always run in `acceptEdits` with the session's tool allowlist; unlisted commands can still prompt | Tool Manager + Permission Policy: per-agent tool allowlists declared as data; anything outside → `Gate` → `tool.denied` or a human |
| Per-stage `model:`; `CLAUDE_CODE_SUBAGENT_MODEL` overrides; an `availableModels` allowlist substitutes a blocked model and warns | the router (Decision 2): tier per task kind, `THYMIRA_*` overrides, `model.selected` with the reason; an allow-list of model ids is a policy rule, and a substitution is a `WARNING`, never silent |
| Saved workflows with `args`; project vs personal location; plugin namespacing | **run recipes** as data (`.thymira/recipes/<name>.yaml`: graph, phases, agents, budgets, policies) — post-MVP, see the roadmap reference |
| Quality patterns: adversarial verify, judge panel, loop-until-dry, evaluator-optimizer, completeness critic | MiraGraph: parallel audit agents, findings adversarially verified before they reach the Policy Engine, discovery loops until no new finding; the Policy Engine is the judge; THY ↔ MIRA rework is the evaluator-optimizer loop |
| Prompt-cache sharing in fan-outs (matching agents are held up to 5 s so they read the first agent's cached prefix) | MIRA's parallel audit agents share one system prefix per role; keep prompts stable and prefix-heavy so LiteLLM's provider caching applies |
| 16 concurrent agents, 1,000 agents per run | concurrency and agent caps are run-level configuration enforced by the graph runner, recorded in `run.started` |

What we deliberately do **not** copy: the plan lives in a script the model writes per run.
In Thymira the graphs are versioned code reviewed like any other code; a model proposes
*tasks* inside them, never a new orchestration. Flexibility comes from recipes (data), not
from generated control flow — that is what keeps every thread auditable.

## Consequences

- Contract 0.2 adds `model.selected` and `agent.message` (additive).
- `thymira.agents.llm.routing` ships now (tiers, floors, resolution chain, `ModelChoice`,
  `provider_for`); `.env.example` documents the variables without naming any model.
- MIRA's deterministic controls gain a future check: every `agent.*`/`tool.*` call under a
  role has a preceding `model.selected` whose `tier_applied` respects the role's floor.
- Swapping THY's or MIRA's vendor, or making summaries cheaper, is an `.env` change and an
  auditable event — never a code change.
- **Plan-gate mechanism (integration note, 2026-08-26; correction C-15).** THY's plan checkpoint
  above is implemented with the same single authorization rule as a tool call: `plan_node` calls
  `gate.check_action(subject_kind="run", subject_id=..., action_type="plan.proposed", payload=...,
  summary="plan.proposed")` — the Policy Engine decides and the Gate records
  `policy.decision` → `human.approval_requested` → `human.approval` exactly as described here — then
  trusts the decision through `thymira.policies.allows_execution(decision)` with no approval object,
  precisely as `ToolManager` does (C-9). The `Gate`'s synchronous approver answer is recorded as a
  `human.approval` event, evidence only, never authority: `allows_execution` grants
  `REQUIRE_HUMAN_REVIEW` only on a separately matched `Approval` the plan node never has, so under
  the default policy the graph halts at Plan. Contract 0.3's `check_intent` → `AuthorizationContext`
  → `Approval` path cannot carry a plan (`ActionKind` has no plan value); a plan has no effect of
  its own and every tool it later runs is gated independently the same way. An asynchronous,
  checkable plan `Approval` is `HITL-01` work.

## Sources

- Claude Code subagents: https://code.claude.com/docs/en/sub-agents
- Claude Code hooks: https://code.claude.com/docs/en/hooks
- Claude Code dynamic workflows (`/workflows`): https://code.claude.com/docs/en/workflows
- Anthropic, *Building effective agents*: https://www.anthropic.com/research/building-effective-agents
- PydanticAI, multi-agent applications: https://pydantic.dev/docs/ai/guides/multi-agent-applications/
