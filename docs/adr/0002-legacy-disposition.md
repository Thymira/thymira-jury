# ADR-0002 — Disposition of the legacy `mads` thesis code

- **Status:** Accepted — 2026-08-21
- **Deciders:** project owner ("legacy code is deleted unless useful; useful parts are
  refactored, translated to English and placed where they belong"); scaffold session (audit)
- **Related:** ADR-0001 (target architecture and legacy → runtime map),
  `docs/contracts/contract-v0.1.md`, `docs/roadmap/product-final.md`

## Context

`src/mads` (~19k lines, Spanish) is the thesis prototype: a single-run, single-dataset,
synchronous LangGraph loop with a `PolicyGate`, a hash-chained trace, an artifact manifest, a
meta-auditor with 18 deterministic controls and seventeen hand-written data-science "skills".
The Thymira runtime is multi-agent, durable, event-driven and API-first, so most of the legacy
*code* cannot be reused as written, while much of its *behaviour* is the project's IP: the
"LLM proposes, code authorizes" mechanics, the evidence model, the leakage-safe methodology and
the audit controls. Four independent audits of the tree (2026-08-21) classified every module;
this record fixes the outcome. The thesis code remains retrievable at tag `thesis/mads-v0.1.0`
(commit `1b9aba6`) and on branch `mario-harnes`.

## Decision

### Rules

1. **Port behaviour, not files.** A "port" rewrites the behaviour in English in the named
   `thymira.*` member with its own tests (the legacy tests are the specification: re-express
   their assertions, not their fixtures). Spanish strings, enum values and file formats are
   redesigned, not transliterated.
2. **Pure cores only.** Data-science logic is ported as functions over arrays/DataFrames and
   train indices; persistence, artifact pointers and split rewriting belong to `runtime/state`.
3. **Invariants become checks.** Where the legacy code is trivial but the rationale is the asset
   (dedup before split, baseline on identical folds, bounded grids, never touch target or
   sensitive attributes), the rationale is ported as an MIRA check or tool guidance, not as code.
4. **Everything else is deleted** in the same pull request, together with the Spanish docs that
   describe it; git history and the tag keep it.

### Port (42 modules → English rewrites)

| Legacy | Target | What survives |
|---|---|---|
| `contracts.py`, `orchestrator/types.py` | `thymira.schemas` (**done**: Contract v0.1) | typed-records discipline; `Task`, `Action→ToolCall`, `GateDecision→PolicyDecision`, `RiskAssessment→AuditFinding`, `Case/CaseOutcome→Run`, `ActorContext→Actor`, `Justification` (non-CoT) |
| `tracing.py`, `utils.py` (hashing, redaction) | `thymira.events` | canonical JSON, sha256, append-only JSONL log with hash chain and stateless `verify`, resume guard, redact-before-trace |
| `artifacts.py` | `thymira.state` (`LocalArtifactStore`) | manifest with sha256/producer/validity, history archival, lineage (`input_artifact_ids`, `execution_key`), invalidate-with-reason, `verify()` |
| `llm/base.py`, `llm/litellm_provider.py`, `tests/fakes.py` | `thymira.agents.llm` | `LLMProvider` contract, LiteLLM implementation (error taxonomy, known/unknown cost, validated structured output, no key leakage), a deterministic scripted provider for tests |
| `policies.py`, `gate.py`, `policy_packs/fraude_aml.json` | `thymira.policies` | rules-as-data (ordered rules, first match, BLOCK precedence), action rules + capability rules, fail-safe escalation on uncertainty, policy snapshot hash, approval request/response recorded as events; decision set becomes `PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK` (`allow→PASS`, `needs_approval→REQUIRE_HUMAN_REVIEW`, `blocked→BLOCK`; `WARNING` is new); the pack is split into a generic base and a credit-risk overlay |
| `audit/meta_auditor.py`, `audit/extended_controls.py`, `audit/assurance.py` | `thymira.mira` | controls portable over events + manifest now: chain integrity (A1), run closed (A2), authorization before tool execution (A3), nothing after BLOCK (A4), artifact integrity (A5), structured tool denials (A6), approvals resolved (A7), decisions answered (A8), lifecycle pairing (A9), declared vs produced artifacts (A10), risk classified before work (A15), policy replay over the frozen snapshot (A16), provenance (A17); status model `PASSED/FAILED/NOT_APPLICABLE/NOT_EVALUATED`; assurance bundle with the expert-review state machine and the "no human-review claim without evidence" disclaimer. Modelling-artifact controls are carried as specifications until THY emits the corresponding evidence. **Correction (C-7):** of the inherited ids only `A11` (split integrity) and `A14` (test-once) are rebuilt; `A12`, `A13` and `A18` keep their inherited meanings but no task builds them, so they are reserved. The modelling checks the roadmap once filed under those numbers are `A20`–`A23` |
| `audit/provenance.py` | `thymira.core` | run-environment capture (python, dependency versions, git commit/branch/dirty, input hashes) — "dirty tree is evidence, not a verdict" |
| `risk.py` | `thymira.agents.risk` | the Risk agent's deterministic layer: observable facts, versioned two-block prompt with delimiter escaping, structured schema re-validated locally, the eleven override rules (code only tightens, forces human review, never lowers a level), information requests, the classify → needs-information → needs-human-review loop, fail-closed errors |
| `decisions.py` (channel), `orchestrator/phases.py`, `orchestrator/state.py` (mechanism) | `thymira.core` / `thymira.state` | HITL decision channel (`DecisionResolver`: automatic / LLM / LLM-proposes-human-confirms, every Q&A traced and persisted, separate from authorization); phase machine with scope-aware skip and backward-only reopen; execution idempotency keys and the declared invalidation cascade; a single usage ledger shown at every approval |
| `skills/{numeric,outlier_bounds,splitting,deduplication,tabular,preparation,treatment,encoding,feature_selection}.py` | `thymira.tools.ds` | leakage-safe cores: train-only bounds/imputation/encoding/selection, stratified split with exact allocation, validation + profiling with leakage indicators, identifier detection, safety nets |
| `skills/modeling/{metrics,fold_preprocessing,resampling,common,selection,evaluation}.py` | `thymira.tools.ds` | metric tables and OOF threshold sweep, per-fold preprocessing shared by candidates, fit-only resampling, task-type dispatch, deterministic model selection (imbalance override), subgroup (fairness) metrics |
| `cli.py`, `console.py` (behaviours only) | `adapters/cli` (later) | stable exit codes, redaction of every user-facing message, mandatory declared actor, refuse to reuse a run directory |

### Keep as data or documentation (17, translated)

`policy_packs/fraude_aml.json` → `thymira.policies` default policies (base + credit-risk
overlay); `compliance/*` → `docs/governance/` and a machine-readable requirements→controls
file for MIRA; `model_catalog.py` → catalogue data (families, mandatory preprocessing, bounded
grids, reproducibility rules) under `thymira.tools`; `docs/trazabilidad.md` → traceability and
control specification; `docs/decisiones-analiticas.md`, `docs/orquestador-agentico.md`,
`docs/arquitectura.md`, `docs/referencias-adoptadas.md`, `docs/normativa.md`,
`docs/litellm.md` → `docs/design/`; `docs/skills.md` → `docs/methodology/`;
`data/credito_aleman.csv` (renamed `data/german_credit.csv`) and the two credit cases → the credit-risk example.

### Drop (26)

`orchestrator/*` except the mechanisms above (`agentic`, `actions`, `selection`, `execution`,
`run_lifecycle`, `authorization`, `availability`, `risk_coordination`, `state_transition`,
`phase_control`, `orchestrator`), `skills/base.py`, `skills/registry.py`, `workers/*`,
`skills/{dataset_resolution,feature_engineering,feature_proposal,reporting}.py`,
`skills/modeling/{candidates,cross_validation,training,tuning,baseline}.py`, `rag/`
(contract re-expressed when the knowledge base lands), `probar_agentico.py`,
`grafo_agentico.mmd`, `docs/estado-tfm-11-ago.md`, `data/airbnb_nyc_dirty.csv`,
`data/ENB2012_Y1.csv`, `data/titanic.csv`, the non-credit example cases, and the Spanish
`README.md` (rewritten in English for Thymira). Their design ideas are preserved in the
appendix below.

### Order

1. `thymira.schemas` (done) → 2. `thymira.events`, `thymira.state`, `thymira.agents.llm` →
3. `thymira.policies` → 4. `thymira.core` mechanisms, `thymira.agents.risk`, `thymira.tools.ds`
→ 5. `thymira.mira` checks and assurance → 6. delete `src/mads`, legacy tests and docs;
raise `requires-python` to 3.12; switch ty and ruff to zero-tolerance on `thymira.*`; add the
`thymira` import-linter contracts; rewrite README, AGENTS.md, skills and CHANGELOG.

## Consequences

- The repository ends with only `thymira.*` code; the legacy suite (499 tests) is replaced by
  English tests that re-express its assertions module by module.
- Event types, decision names and risk taxonomy values change; no trace produced by the legacy
  code is readable by the new tools (the legacy code remains available at the tag for that).
- `WARNING` has no legacy equivalent and needs its own rules in the policy base; the meta-auditor's
  `critical/warning` finding severities map onto `Severity` and the assurance status onto
  `PASS/WARNING/BLOCK` at the run level.
- Some ported controls stay `NOT_APPLICABLE` until the runtime emits their evidence
  (`policy_snapshot`, `run_environment`, partition-access events, modelling artifacts); the
  emitters are part of the MVP backlog (P1/P3), not of this port.

## Appendix A — ThyGraph design notes preserved from the dropped orchestrator

1. **Phase gating.** Ordered phases (understanding → preparation → modelling → evaluation →
   reporting) with a scope-aware skip (descriptive objectives go straight to reporting); the LLM
   may *request* to advance, a deterministic per-phase exit gate decides from evidence.
2. **Reopen with justification.** Backward-only, budgeted (`max_phase_reopens`), approved by the
   Policy Engine, invalidates only evidence produced after the target phase and bumps the phase
   revision; a separate, un-gated automatic reopen handles validation loss.
3. **Execution idempotency.** `execution_key = sha256(tool + input fingerprints + parameters +
   phase revision + decision fingerprint)`; a repeat with the same key is refused, which makes
   the loop converge.
4. **Lineage and invalidation.** Each artifact records input fingerprints, producer and execution
   key; datasets form a versioned chain with a parent pointer; a declared downstream graph
   cascades invalidation with a reason instead of deleting files.
5. **One-action loop with gate recomputation.** Observe → the LLM selects exactly one action
   from a code-computed menu → the Policy Engine decides on a freshly built action every
   iteration (never cached) → execute → apply state → re-observe. Invalid proposals loop back as
   feedback; only technical failures and BLOCKs end the graph. Budgets (`max_steps`,
   `max_phase_steps`, recursion limit) bound the loop and every terminal records a reason.
6. **Cost in approvals.** One usage ledger (calls, tokens, cost; unknown cost degrades to `None`)
   is injected everywhere and shown to the human at every `REQUIRE_HUMAN_REVIEW`.

Two worked examples of the core invariant to keep as templates: the risk classifier (the LLM
classifies, deterministic checks only tighten and force review) and the selector/gate split
(the LLM picks from a code-computed menu, the engine authorizes).

## Appendix B — Methodology invariants that become MIRA checks

Never winsorize, impute or encode the target or a sensitive attribute; leakage indicators
(exact target copy, |corr| ≥ 0.95, identifier-suspected-but-target-correlated columns); dedup
before split; split integrity (no overlap, complete coverage, non-empty, per-class allocation);
CV preprocessing is fold-local and shared by all candidates; the test partition is read once,
for the final evaluation, with the OOF-selected threshold; a trivial baseline on identical
folds is mandatory to interpret metrics; subgroup (fairness) disparity per sensitive attribute
is reported; reproducibility metadata (seed, library versions, single-thread, explicit
estimator arguments) accompanies every model.

## Appendix C — Design provenance

The hash chain, typed work orders and manifest come from the *tfm-mario* reference; gate,
policy packs and meta-auditor from *Nucleo*; capability-catalogue routing from
*TFM-Arquitectura-Lucas*. Deliberately discarded then and still: execution sandbox inside the
orchestrator (now the Sandbox Manager), YAML policies as code, cross-case global checks, formal
JSON-Schema validation of everything, hierarchical spans (now OpenTelemetry). Known limits that
carry over: regex redaction is best-effort; the chain detects tampering but cannot prevent a
full rewrite by someone with write access; model confidence is uncalibrated and never an
authorization.

## Status of the migration (2026-08-21)

Landed on `feat/professional-scaffold`, each with English tests under `tests/thymira/`:

| Legacy | Ported to | Notes |
|---|---|---|
| `tracing.py`, `utils.py` (hash chain, canonical JSON, redaction) | `thymira.events` | stateless verification, JSONL + in-memory logs |
| `artifacts.py` (`ArtifactStore`, manifest) | `thymira.state` | protocol + local store, `verify()` |
| `llm/` (LiteLLM provider, deterministic test provider) | `thymira.agents.llm` | `THYMIRA_MODEL`, `ScriptedProvider` |
| `policies.py`, `gate.py`, `policy_packs/` | `thymira.policies` | rules as data (YAML), four-value decisions, fail-safe escalation, `Gate` events |
| `audit/provenance.py`, `orchestrator/phases.py` | `thymira.core` | provenance as evidence, scope-aware phases, budgeted reopen |
| `audit/` controls A1–A7, A9–A10, A16, A17 | `thymira.mira.checks` | over the Event API; A11–A14 and A18 depend on training evidence (THY, MVP). **Correction:** A15 belongs with the first group — it folds events (a tool that ran before `risk_assessed`) and needs no training evidence; it is MVP work, owned by `CTRL-MVP` alongside A8 |

Still to port, in roadmap order (owners in `docs/roadmap/mvp-3-weeks.md`): the Risk agent's
deterministic override rules (→ `thymira.agents.risk`, P2/P4), the data-science tool set
(`skills/` → `thymira.tools.ds`, P3), the HITL decision channel and the idempotency / lineage
mechanism (→ `thymira.core`, P1), the usage ledger (→ `thymira.core`, P1), the data-dependent
audit controls A11–A14 and A18 (→ `thymira.mira.checks`, P4; A15 is event-folding, not
data-dependent — it is MVP work in `CTRL-MVP`). The legacy source for each is at
tag `thesis/mads-v0.1.0`; the legacy documents describing them are in `docs/legacy/`.

Removed as planned: `src/mads`, the legacy test suite, `probar_agentico.py`,
`grafo_agentico.mmd`, `examples/cases`, the extra datasets. Kept as data:
`data/german_credit.csv` (was `credito_aleman.csv`), `docs/legacy/compliance/`.

Decisions closed by the owner on 2026-08-21:

- **(2) Thesis safety net** — the tag `thesis/mads-v0.1.0` is pushed to the remote; no `thesis`
  branch (an immutable tag is the reference the thesis and the documentation point to).
- **(8) Names** — the product, the company and the repository are **Thymira**
  (`github.com/Thymira/thymira`, renamed on 2026-08-22; the old `mads-msct` URL redirects).
  The two orchestrators are **THY** (acts) and **MIRA** (watches); "Sol" and "Opus" in the
  early documents were example model names, not product names (ADR-0003). Packages are
  `thymira.*`, distributions `thymira-*`, the CLI is `thymira`.
- **(9) Licence** — Apache License 2.0: `LICENSE` at the root, `license = "Apache-2.0"` in
  every `pyproject.toml`, copyright "Thymira authors".
