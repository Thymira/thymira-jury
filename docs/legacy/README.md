# Legacy documentation (thesis prototype, Spanish)

These documents describe the **MADS thesis prototype** — the Spanish-language implementation
that lived in `src/mads` until it was absorbed into the Harness runtime (ADR-0002). They are
kept verbatim as historical context and are **not maintained**: file names, commands and module
paths refer to the code at git tag `thesis/mads-v0.1.0`, not to the current workspace.

| File | What it covered |
|---|---|
| `architecture-overview.md` | English brief of the legacy code base (written during the migration) |
| `arquitectura.md` | architecture of the LangGraph orchestrator and its eight coordinators |
| `orquestador-agentico.md` | the agentic orchestrator, phases and reopen rules |
| `decisiones-analiticas.md` | analytical decision catalogue (deterministic rules) |
| `trazabilidad.md` | the hash-chained trace and artifact manifest |
| `skills.md` | the data-science pipeline steps (not agent skills) |
| `normativa.md` | regulatory basis (EU AI Act, credit-risk guidance) |
| `litellm.md` | how the prototype talked to models through LiteLLM |
| `referencias-adoptadas.md` | adopted references |
| `estado-tfm-11-ago.md` | status report of the thesis on 11 August 2026 |
| `compliance/` | requirement → control mapping and sources for the meta-auditor |

The behaviour worth keeping was ported, in English and with tests, to `harness.events`,
`harness.state`, `harness.agents.llm`, `harness.policies`, `harness.core` and
`harness.opus.checks`; `docs/adr/0002-legacy-disposition.md` records what moved where and what
was dropped. New documentation is written in English under `docs/architecture`, `docs/roadmap`,
`docs/contracts` and `docs/adr`.
