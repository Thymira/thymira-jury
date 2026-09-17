# ADR-0003 — Naming: Thymira, THY and MIRA; models are configuration, in two tiers

- Status: Accepted (2026-08-22)
- Deciders: the owner
- Supersedes: the names "Harness", "Sol" and "Opus" used in ADR-0001, the E2E baseline and
  the roadmap

## Context

The early documents called the runtime "Harness" (a generic term for the machinery around
the models) and the two orchestrators "Sol" and "Opus". The owner clarified that those two were
**examples of model names** — a frontier model such as "Claude Opus" or "GPT Sol" running each
orchestrator — not the names of the layers. Meanwhile the product, the company and the
repository were named **Thymira**, with a story built on two complementary intelligences:
THY, which acts, and MIRA, which watches (`docs/brand/story.md`).

Keeping three vocabularies (brand, architecture, code) would have cost every newcomer a
translation table, and binding the layers to model names would have tied the architecture to
a vendor's catalogue.

## Decision

1. **One name family.** The product, company and repository are **Thymira**
   (`github.com/Thymira/thymira`). The Python namespace is `thymira.*`, the distributions are
   `thymira-*`, the CLI is `thymira`, the project context directory is `.thymira/`, the base
   model class is `ThymiraModel`, environment variables start with `THYMIRA_`.
2. **The orchestrators are THY and MIRA.** THY is the execution orchestrator (formerly "Sol";
   member `runtime/thy`, package `thymira.thy`, graph `ThyGraph`). MIRA is the audit
   orchestrator (formerly "Opus"; member `runtime/mira`, package `thymira.mira`, graph
   `MiraGraph`). In prose they are written THY and MIRA; in identifiers `thy` and `mira`.
3. **Models are configuration, in two tiers.** Orchestrators reason with a frontier model
   (`THYMIRA_ORCHESTRATOR_MODEL`); their sub-agents run on a cheaper model
   (`THYMIRA_AGENT_MODEL`); `THYMIRA_MODEL` is the fallback for both.
   `thymira.agents.llm.get_provider(role=...)` resolves the tier (refined into three tiers
   with per-orchestrator models in ADR-0004); no model id is hard-coded as a default and no
   vendor is named in the architecture documents — ids that appear in docstrings or tests only
   illustrate LiteLLM's `<provider>/<model>` format. LiteLLM remains the only gateway.
4. **Frozen documents keep their words.** `docs/legacy/` (thesis) and the dated design
   documents under `docs/superpowers/` are historical and are not rewritten; the living
   documents (README, AGENTS.md, baseline, roadmap, ADRs, skills) use the new names.

## Consequences

- Mechanical rename across the workspace (directories, imports, contracts, CI, docs, skills)
  before any external consumer exists — the cheapest possible moment.
- `thymira.thy` and `thymira.mira` stay independent (import-linter contract); nothing changes
  in the architecture itself.
- Switching models is an `.env` change; evaluating a cheaper orchestrator model or a better
  agent model never touches the repository.
- The old repository URL redirects; `CHANGELOG.md` compare links, the README badge and the
  remotes were updated on the day of the rename.
