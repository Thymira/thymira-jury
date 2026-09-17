# thymira-agents

Individual agents implemented with PydanticAI (Data, Coding/Execution, Experiment, Methodology, Risk, Regulatory ...). LangGraph decides who/when/in which order; PydanticAI decides how each agent works.

| | |
|---|---|
| Import name | `thymira.agents` |
| Owner (MVP roadmap) | P2 (THY / Agents) and P4 (MIRA / Governance) |
| MVP priority | P0 |
| Location | `runtime/agents/` |

## Status

Implemented and tested (`tests/thymira/test_agents_*.py`): the `LLMProvider` contract, the
LiteLLM provider, the scripted test provider and the model router (tiers, floors,
`model.selected`); `AgentSpec`/`AgentCatalog`/`load_agent_specs` (sub-agents declared as data),
`routed_model` (the PydanticAI model that routes every call through the router), `AgentRunner`/
`AgentContext`/`AgentResult` (the spec-driven execution loop), `build_agent_tools` (the tool
bridge exposing only a step's allowlist as PydanticAI tools gated through the Tool Manager +
Gate), `PromptBuilder`/`record_prompt`, `RunUsage`/`UsageLimits`, and `Delegator`/`DepthGuard`
(the hub-and-spoke delegation channel recording each outcome as `agent.message`). The concrete
Data, Coding/Execution and Experiment agent specs live in `thymira.thy`.

Every production provider call also crosses `thymira.agents.request_ledger.RequestLedger`.
LiteLLM capture occurs after its effective settings and schemas are resolved and before gateway
dispatch; injected providers use `LedgerProvider` through `instrument_provider`. The ledger keeps
the exact model-visible request, owned header/surface references, response source sequences and
reasoning metadata without reasoning text. System messages retain their role boundaries and full
surface order. MIRA's `request_replay` reader independently rebuilds
the provider arguments; it does not import this producer assembler. Credential source scrubbing
and export redaction remain G.1 responsibilities.

## Rules

- Contracts live in `thymira.schemas`; do not redefine them here.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
