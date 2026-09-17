# thymira-mira

MIRA is the independent audit and governance observer. It reads execution events and evidence,
produces assessments and findings, and never modifies the workspace or authorizes an effect.

A18 verifies versioned dataset profile reports by reading the registered CSV or Parquet and
its captured schema through the ArtifactStore. It checks digests and the declared row/column
scope, then independently recomputes counts, frequencies, histograms and correlations. It never
calls the profiling tool or uses its output as the source of truth. Missing or malformed source
declarations are findings, including on older profiles that must be regenerated. Ordinary report
claims retain their experiment/metrics checks; verified profile numbers cannot support unrelated
claims. This deterministic check makes no model call and never writes an artifact.

| | |
|---|---|
| Import name | `thymira.mira` |
| Owner (MVP roadmap) | P4 (MIRA / Governance) |
| MVP priority | P0 |
| Location | `runtime/mira/` |

## Status

Implemented and tested: deterministic controls A1-A7/A9-A10/A16/A17 and `audit_run()`, plus
deterministic preflight. `BaseRiskEvaluator` applies reviewed, versioned `methodology-base`
rules; `ApplicabilityResolver` binds `methodology-base` and `credit-governance` to one exact
profile version; and `GenericPackControlRunner` checks declared evidence presence, integrity, and
freshness. All outputs remain MIRA evidence only.

The A23 reproducibility control validates the value types and shapes of the four required metadata
facts and the observed thread-pool count. Valid parallel or empty/unavailable observations remain
evidence and fail A23; contradictory declarations are rejected, and `deterministic: true` alone
does not prove single-thread execution. Historical aliases remain accepted when well formed;
synonymous seed and thread declarations must agree. The built-in/default `run_experiment` records
the child process's installed
library versions, effective classifier arguments and split configuration, observed thread-pool
facts, and the generated training-script digest. Custom code retains the A23 missing-metadata
finding and does not inherit the built-in baseline evidence.

A3 and A6 independently reconstruct exact tool approvals and denials from the event log.
Run-wide human-review constraints require separate per-call evidence: approving execution
start does not authorize a tool, and a permissive capability verdict cannot erase an active
review requirement. The ledgers preserve verification of historical blanket-refusal records
alongside the current one-shot review flow.

The inherited requirement applies to THY's execution surface. MIRA's independent reads retain
their own capability authorization. Trusted Core lifecycle transitions delimit that scope:
`begin_audit` suspends it; `reopen`, reporting and terminal exits restore it. A tool's own claim
or preflight `audit.started` event cannot waive review. Compositions using MIRA tools under
this constraint must provide these Core lifecycle records; absent evidence keeps review active.

`GOV-01` provides MIRA's frozen audit-agent boundary. `AuditInput.from_run()` derives one Run id
from explicit event/report evidence and exposes only artifact, experiment, and framework facts
recorded by events. `AuditAgentSpec` declarations load from YAML in deterministic file order with
duplicate-name and value validation. `AuditAgentOutput.from_spec()` validates every finding
against the spec's declared framework without adding a redundant persisted framework field.

`MIRA-02` adds `thymira.mira.agents.run_audit_agent()`: a runner with an immutable execution
context, code-owned finding identities, PydanticAI structured output through `routed_model`, and
an allowlist adapter that delegates every tool call to the unchanged Tool Manager. Its model input
is limited to `current_surface`, the deterministic report, fixed audit facts, and read-only,
hash-pinned excerpts cited by that report; it redacts and deterministically truncates each layer.
The runner emits `agent.started`/`agent.completed` and returns candidates only, leaving finding
persistence, deduplication, and the single Gate review to MIRA-01.

A finding's `evidence` tuple is authored by the model, so `thymira.mira.grounding` resolves every
reference against the Run's own event log before it can become a citation, and `MiraAuditFlow`
repeats that resolution on producer output so an injected runner cannot bypass it. An unbacked
reference is stripped and the refusal recorded on `agent.completed`; the finding survives for the
Policy Engine to weigh. The one thing the log cannot confirm is the exact knowledge-base chunk
behind an `external` citation -- it records the digest of a whole search result, not of each
chunk -- so an external reference is accepted only when the Run recorded a completed
`search_regulation`. Routing the regulatory-evidence agent's hash-verified `CitationIndex` into the
generic runner would close that remainder.

The runner API is lazy, so bare `import thymira.mira` does not load it or the Tool Manager.
Loading `AuditAgentSpec` itself still loads the P2 routing package, whose current eager package
initialisation also loads the Tool Manager. This known import-boundary limitation is intentionally
left unchanged here because its correction belongs to P2's public package initialisation, not P4.

`MiraAuditOrchestrator` separates its bounded flow from an immutable Run snapshot: governance
preflight computes inherent risk and pack bindings before THY; evidence audit evaluates applicable
pack controls after THY has produced evidence; deterministic checks then run in an explicit audit
mode. `IN_FLIGHT` excludes A2, A7 and A8 because Core has not reached its findings Gate or Run
closeout; `FINAL` evaluates them and requires the recorded closeout evidence. It returns every result
to its caller; it neither writes Run state nor calls the Policy Engine or Gate.

`MIRA-01` `MiraAuditFlow` is the one canonical, Gate-less audit sequence. It performs preparation,
governance preflight, evidence controls, deterministic controls, ordered audit-agent fan-out,
bounded discovery, adversarial verification, deduplication, report assembly, and MIRA evidence
events. It can be split at the preflight boundary so Core runs governance before THY and the rest
after THY; standalone callers run both phases consecutively. Core's `MiraSubgraph` is a thin
adapter around the same flow and leaves its production `Gate` to the composition's `review` node,
so one Run never gets two findings decisions. The flow uses an injected `EventLog`, keeps the
optional `ArtifactStore` read-only, and never receives `LocalRunStore`, `RunController`, or
runner/tool dependencies directly.

Project configuration declares the governance frameworks for every Run. `MiraAuditFlow` uses that
same declaration for A26 requirements coverage and to select the applicable audit-agent specs; a
roster never expands a project's declared scope.


The composition root dispatches the two shipped bespoke implementations explicitly: `reg_evidence`
enriches the already accumulated findings with hash-verified citations, and `compaction_fidelity`
compares persisted summaries with their shadowed events. Every other spec uses the generic bounded
runner. Enrichment updates replace a candidate by its existing id before the canonical verification,
deduplication, and report step, so there is still one findings cycle and one production Gate. Agent
calls remain sequential because they share the append-only event log, provider boundary, usage
accounting, and replay order; the current audit has no clear parallelism gain that justifies
relaxing those guarantees.

`DecisionContextAnalyst` creates a short structured context only when its explicit
`create_context()` operation is called. Its claims cite supplied evidence or are marked unknown;
the local snapshot writer stores each immutable JSON file under `mira-context/` and records
`mira.context_created`. It has no automatic trigger, latest pointer, cache, or THY consumer.
The packaged preflight rules remain local JSON data and introduce no LLM call.

`ActivityProfileElicitor` and every THY consumer remain out of scope.

`KB-01` adds a small read-only regulation knowledge base. `RegulationStore` is the stable search
boundary; `LocalRegulationStore` loads a local JSONL file once, validates every
`RegulationChunk`, verifies the chunk text's SHA-256 digest, and ranks keyword matches
deterministically. `build_mira_tool_registry` creates the explicit MIRA registry (currently one
`search_regulation` entry) around the configured store. Core and the API use the packaged JSONL
corpus by default, so starting MIRA, Core, or the API does not require PostgreSQL. Each result
contains a source id, version, citable fragment, score, backend name, and SHA-256. A finding can
cite that source id/location and digest as `Evidence(kind="external")`.

The packaged corpus covers the EU AI Act, GDPR, the internal methodology invariants, and the
credit-risk scope that the shipped `credit_risk` agent must query. Its `CREDIT_RISK` entries cite
the AI Act creditworthiness classification and Directive (EU) 2023/2225 creditworthiness rules;
the Directive entry records that Member States apply its implementing measures from 20 November
2026. Because the MVP backend is lexical, the shipped agent names a stable scope query that the
packaged corpus is tested to answer. An empty or foreign-framework result still cannot satisfy a
mandatory lookup.

The tool is exposed to an audit agent through its YAML allowlist, then delegated to the shared
`ToolManager`. The manager applies the allowlist and Permission Policy/Gate before invoking the
store and records the tool lifecycle events; the agent and model never receive the store or a
direct external-access path.

`PgVectorRegulationStore` is retained as an experimental backend. Its `embed_text` function hashes
case-folded tokens into a normalized term-frequency vector and pgvector ranks those lexical
vectors by cosine distance. It is not semantic search and does not understand synonyms or meaning.
No embedding provider is configured in the MVP; real semantic embeddings are deferred until a
separate architectural decision and explicit configuration exist.

## First-phase local closeout

`tests/thymira/test_mira_e2e.py` drives the `examples/credit-risk` policy through the complete
local MIRA slice: Run and profile evidence, preflight risk and packs, generic and existing
integrity controls, findings, a bounded review intent, a scripted context snapshot, deterministic
policy evaluation, simulated human approval, and the sole `RunController` transition writer. It
then verifies the authoritative JSONL, `run.json` reconstruction, context-file accessibility, and
tamper detection.

The test deliberately leaves one pack evidence observation absent to produce a reviewable finding.
That finding, the intent, and the context snapshot remain non-authoritative until the separately
recorded Policy Engine, Gate, approval, and authorization-context checks succeed. No THY consumer,
automatic context refresh, PostgreSQL, broker, worker, or distributed deployment is involved.

## Rules

A3/A6 independently fold ticket approvals and denials. A6 shares only the contract-owned pure
refusal vocabulary with the Tool Manager, preserving the lazy import boundary: importing MIRA
does not load `thymira.tools`. Older allowlist denials remain readable through their tool actor
and exact recorded sentence.

- Shared cross-member contracts live in `thymira.schemas`; MIRA's private audit-agent boundary
  lives in `thymira.mira` and remains deliberately distinct from THY's `AgentSpec`.
- MIRA observes and proposes. The Policy Engine authorizes and the Gate records required human
  approval; findings and model output have no direct effect.
- No preflight result authorizes a tool, Run transition, or workspace modification.
- Everything is English; Google-style docstrings; tests next to the feature in `tests/`.
- Follow `AGENTS.md` (conventions) and the import direction in `runtime/README.md`.
