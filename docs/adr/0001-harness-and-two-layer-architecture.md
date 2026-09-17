# ADR-0001 — Build Thymira on LangGraph + PydanticAI + LiteLLM with two orchestrators and a Policy Engine

- **Status:** Accepted — 2026-08-20
- **Deciders:** project owner (E2E Architecture Baseline v2); scaffold session (framework analysis)
- **Related:** `docs/architecture/e2e-baseline-v2.md`, `docs/roadmap/product-final.md`,
  `docs/superpowers/specs/2026-08-20-professional-scaffold-design.md` (§7 and §7b)

> **Storage note (2026-08-25):** ADR-0010 supersedes this record's operational-storage
> assumption. The MVP uses local JSON/JSONL state; the managed-database option is future only. The
> remaining harness, orchestration, and Policy Engine decisions in this ADR stay accepted.

## Context

The thesis prototype in `src/mads` already demonstrates the core idea: an LLM may *propose* a
data-science action, but only deterministic code (risk classifier, policy pack, `PolicyGate`)
*authorizes* it, every step is written to a hash-chained trace, and a meta-auditor re-derives
controls from the persisted evidence. The product vision extends this into a two-layer runtime:

- **Layer 1 — production / data science.** An orchestrator (**THY**) coordinates specialised
  agents (data, coding/execution, statistics, experiment, ML, …) that can talk to each other and
  to the orchestrator, run code, register experiments and produce artifacts.
- **Layer 2 — audit / governance.** An independent orchestrator (**MIRA**) coordinates audit
  agents (methodology, risk, EU AI Act, credit-risk regulation, …) that consult a normative and
  methodological knowledge base, produce findings with evidence, and can intervene in layer 1.

The owner asked whether to build on an existing open-source "thymira" (for example "the
DeepSeek one") rather than writing tools, hooks and commands from scratch. The team's baseline
then fixed the stack: LangGraph runtime, PydanticAI agents, LiteLLM model layer, MCP tools behind
a Tool Manager and Permission Policy, MLflow experiment tracking, FastAPI API, thin clients.

## Decision

1. **Terminology.** A *harness* is the machinery around the models — tools, context management,
   subagents, hooks/events, permissions, durable state and provenance. We build Thymira as an
   *application* assembled from frameworks; we do not adopt another vendor's finished coding-agent
   *product* as the base. Existing products (OpenCode, Aider, Continue, Cline, Claude Code, Codex,
   Cursor) are integration targets and sources of inspiration, not dependencies.
2. **Runtime: LangGraph.** State, checkpoints, cycles, subgraphs, parallelism, delegation,
   human-in-the-loop (`interrupt()`) and durable execution. `ThyGraph` and `MiraGraph` are
   separate subgraphs composed only in `runtime/core`; LangGraph stays internal to the runtime.
3. **Agents: PydanticAI.** LangGraph decides *who, when and in which order*; PydanticAI decides
   *how each agent works* (typed inputs/outputs, tools, retries).
4. **Models: LiteLLM** as the single gateway (OpenAI, Anthropic, Azure, Bedrock, local models).
   *Open integration point (week 1, P2):* PydanticAI ships its own provider layer, so LiteLLM is
   used either as an OpenAI-compatible proxy that PydanticAI talks to, or through a thin PydanticAI
   model adapter over the `litellm` SDK. Until that is decided, agents depend on an
   `LLMProvider` interface so the choice remains replaceable; the legacy
   `mads/llm/litellm_provider.py` is the reference for cost/usage capture.

   **Resolved (`THY-02`): the adapter, not a proxy.** `thymira.agents.model_binding.routed_model`
   builds a PydanticAI `Model` (via `pydantic_ai.models.function.FunctionModel`, so the streaming/
   event-iterator machinery is PydanticAI's own, not reimplemented here) whose function calls
   `routing.choose()`/`LLMProvider.complete()` directly — never a network proxy. Every call
   appends a `model.selected` event before the request, and `LLMResponse.input_tokens`/
   `output_tokens` populate PydanticAI's `RequestUsage`, so cost/usage accounting stays ours, not
   PydanticAI's or the proxy's.
5. **Tools: MCP** is the exposure standard; every call passes through the **Tool Manager** and the
   **Permission Policy**. Agents never touch subprocesses or the filesystem directly.
6. **Governance.** MIRA produces findings (`finding`, `evidence`, `severity`, `confidence`,
   `recommendation`); the deterministic **Policy Engine** turns them into
   `PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`; humans approve or reject. MIRA reads events and
   evidence and never modifies the workspace.
7. **Clients.** The CLI is the first official interface and a thin client of the FastAPI
   Thymira API; MCP/SDK/IDE adapters follow. No client owns run state: runs, events, artifacts and
   experiments live in the runtime (local JSON/JSONL, `ArtifactStore`, MLflow).
8. **Repository.** A uv-workspace monorepo following the baseline (`apps/`, `runtime/`,
   `adapters/`, `packages/`, `services/`, `infrastructure/`); `src/mads` stays as the legacy
   reference implementation until its pieces are absorbed (mapping below).

## Alternatives considered

| Candidate | Why not as the base |
|---|---|
| `deepseek-ai/deepseek-harness` | Real (MIT, created 2026-08-13) but a TypeScript/Node plugin-based product in developer preview with announced breaking changes; wrong language and maturity for an auditable Python runtime. |
| `langchain-ai/deepagents` | A LangGraph-based "batteries-included" agent harness (planning tool, filesystem, subagents). Close to the idea the owner had in mind, but its opinionated agent loop overlaps with PydanticAI; kept as a reference for subagent/planning patterns, not a dependency. |
| Claude Agent SDK | Rich hooks/permissions, but Claude-only; cannot host a GPT orchestrator and a Claude auditor under one runtime. Reference for hook and permission design only. |
| OpenAI Agents SDK | Good primitives and tracing, but non-OpenAI models are "best-effort/beta"; vendor-centric. |
| Google ADK | Multi-provider via LiteLLM, but Google-centric and not what the team standardised on. |
| Pydantic AI alone | Excellent typed agents (chosen for agents) but no durable graph runtime with checkpoints/interrupts; paired with LangGraph instead of replacing it. |
| smolagents, AG2 (AutoGen fork), CrewAI | Thin persistence, governance uncertainty, or opinionated role-play topologies that do not fit two independent orchestrators. |

## Consequences

- **Skeleton now, implementation per roadmap.** The workspace members exist (`thymira.schemas`,
  `thymira.events`, `thymira.core`, `thymira.state`, `thymira.tools`, `thymira.policies`,
  `thymira.agents`, `thymira.thy`, `thymira.mira`, `thymira.api`, `thymira.cli`) with owners and
  priorities; Contract v0.1 (`Run`, `Session`, `Agent`, `Task`, `ToolCall`, `Artifact`,
  `Experiment`, `Event`, `AuditFinding`, `PolicyDecision`) is frozen on MVP day 1 by P1.
- **Import direction is a CI invariant.** `apps/api`, `adapters/*` → `runtime/thy` |
  `runtime/mira` → `runtime/agents`, `runtime/policies` → `runtime/tools`, `runtime/state` →
  `runtime/core` → `packages/events` → `packages/schemas`. import-linter contracts for `thymira.*`
  are added as soon as modules exist: a `layers` contract (`containers = ["thymira"]`), an
  `independence` contract between `thymira.thy` and `thymira.mira`, and a `forbidden` contract
  keeping `thymira.schemas` / `thymira.events` free of runtime imports.
- **Legacy → runtime mapping** (what to port, where):

  | `src/mads` today | Runtime home | Notes |
  |---|---|---|
  | `contracts.py`, `orchestrator/types.py` | `packages/schemas` | typed contracts; keep the "LLM never authorizes" fields |
  | `tracing.py` (hash-chained `trace.jsonl`) | `packages/events` + `runtime/state` | events become the Event API; the hash chain stays as audit evidence |
  | `gate.py` (`PolicyGate`), `policies.py`, `policy_packs/*.json` | `runtime/policies` | becomes the Policy Engine; packs become `.thymira/policies.yaml` |
  | `audit/meta_auditor.py`, `audit/extended_controls.py` (A1–A18) | `runtime/mira` + `runtime/agents` | deterministic checks behind the Methodology and Risk agents |
  | `risk.py`, `decisions.py` | `runtime/agents` (Risk agent), `runtime/thy` (analytical decisions) | |
  | `skills/*` (data-science steps), `workers/` | `runtime/tools` (tools behind the Tool Manager) | "skill" in MADS means a pipeline step, not an agent skill |
  | `orchestrator/*` (LangGraph, eight coordinators) | `runtime/thy` + `runtime/core` | graph shape and coordinators inform ThyGraph |
  | `llm/litellm_provider.py` | `runtime/agents` / LLM layer | LiteLLM gateway, cost capture |
  | `artifacts.py`, `runs/` layout | `runtime/state` (`LocalArtifactStore`) | manifest + lineage |
  | `cli.py`, `console.py` | `adapters/cli` (`thymira`) | the new CLI is an API client |
  | `rag/` (normative RAG contracts) | knowledge base consumed by MIRA agents | future retrieval design; not MVP storage |

- **Risks.** PydanticAI ↔ LiteLLM integration (decide in week 1); ty is pre-1.0 (pinned, ratchet);
  MLflow and future service infrastructure add weight to the local `docker compose` stack; the translation of the
  legacy code base competes for time with the MVP — port behaviour, not files.

## Sources

- Team E2E Architecture Baseline v2 and MVP roadmap (2026-08-20), rendered in
  `docs/architecture/e2e-baseline-v2.md` and `docs/roadmap/mvp-3-weeks.md`.
- Framework survey (2026-08-20): GitHub API / PyPI for `deepseek-ai/deepseek-harness`,
  `langchain-ai/deepagents`, `openai/openai-agents-python`, `anthropics/claude-agent-sdk-python`,
  `google/adk-python`, `pydantic/pydantic-ai`, `huggingface/smolagents`, `ag2ai/ag2`,
  `crewAIInc/crewAI`, `langgraph`, `litellm`; official docs: https://docs.langchain.com/oss/python/langgraph/persistence,
  https://openai.github.io/openai-agents-python/models/litellm/, https://code.claude.com/docs/en/agent-sdk/python,
  https://adk.dev/agents/models/, https://pydantic.dev/docs/ai/overview/, https://huggingface.co/docs/smolagents/index,
  https://docs.ag2.ai/latest/docs/quick-start/, https://docs.crewai.com/en/introduction,
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents,
  https://import-linter.readthedocs.io/en/stable/.
