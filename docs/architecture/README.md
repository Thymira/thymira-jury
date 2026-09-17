# Architecture documents

- `e2e-baseline-v2.md` — the E2E Architecture Baseline v2 (English rendering of the team's
  baseline): the Thymira runtime, THY and MIRA, interfaces, APIs, stack and principles.
- `mira-regulatory-risk-and-control-plane.md` — the accepted MIRA design for preflight,
  inherent-risk assessment, pack applicability, independent controls, versioned JSON context,
  and Policy Engine authorization boundaries. THY does not consume MIRA snapshots in the MVP.
- `../adr/` — Architecture Decision Records: 0001 (why a harness, LangGraph + PydanticAI +
  LiteLLM, the two orchestrators), 0002 (what of the thesis prototype was ported, kept as data
  or dropped, and the open backlog), 0003 (naming; models are configuration), 0004 (model routing
  and agent topology), 0005 (what is reused from DeepSeek Harness), 0006 (the log is not the
  surface: compaction shadows, it never deletes), 0007 (compaction is a single append, so it
  needs no start/end bracket), 0008 (MIRA as the Audit Orchestrator and the control-plane
  authority boundaries), 0009 (transactional operational state — superseded by 0010), 0010 (local
  JSON/JSONL operational state and verifiable evidence, superseding 0009 and ADR-0001's storage
  decision), 0011 (MIRA preflight, continuous context, and control model).
- `../contracts/contract-v0.1.md` — the shared types (`thymira.schemas`) every member speaks
  (currently Contract v0.6; the filename is kept stable for link integrity).
- `../roadmap/product-final.md` — the single task catalogue: who builds what, when, and the
  seam each MVP task must expose so the FINAL version drops in without a refactor;
  `../roadmap/mvp-minimum.md` is the MVP-only view over it, and
  `../roadmap/mira-phase-1-closeout.md` records the bounded first MIRA phase. `mvp-3-weeks.md` is
  superseded but kept for its still-current reasoning (levels, management rules, role definitions).
  `.agents/skills/repo-skeleton/` maps each deliverable to its place in the tree.
- `../legacy/` — the frozen thesis documents (Spanish) and `architecture-overview.md`, the
  English brief of the prototype at tag `thesis/mads-v0.1.0`.
