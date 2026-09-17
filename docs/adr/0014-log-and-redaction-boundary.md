# ADR-0014 — The log/redaction boundary: what the chained log, exports, traces and prompts hold

- **Status:** Accepted — 2026-09-08
- **Deciders:** the owner (as data-protection owner)
- **Related:** ADR-0005's superseded "redact before the event enters the chain" stance,
  [ADR-0012](0012-llm-tracing-langfuse.md), [ADR-0013](0013-harness-fundamentals-six-decisions.md),
  `AGENTS.md`, and DSH acceptance record G.1 and F1.1-F1.6.

## Problem

ADR-0013 decision 1 says that the canonical event log records model-visible values and that PII
redaction belongs to exported and traced copies. Repository code and guidance still applied the
older redact-before-write rule. The difference is material: redacting a decimal, an identifier or
an email before hashing means the log no longer proves what the model saw and cannot reconstruct a
provider request. The local log also contains personal data, so the accepted policy must state its
storage boundary and its independent presentation boundaries together.

## Decision

The owner approved ADR-0013 decision 1 and the following four sub-decisions. The canonical local
log is evidence in the Run trust domain; every projection that leaves that domain is an export.

1. **Canonical evidence and storage.** `events.jsonl`, in-memory events and PostgreSQL events
   retain model-visible prompts, tool arguments, results and outputs exactly enough to rebuild the
   request and verify the chain. Known credential values are scrubbed before provider, child
   process, diagnostic or event exposure. PII is not pattern-redacted on the canonical append
   path. Local Run roots, Run directories, context/export directories and files are secured and
   independently verified: POSIX uses exact `0700` directories and `0600` files; Windows uses a
   native protected DACL allowing only the current user, `SYSTEM` and `BUILTIN\\Administrators`.
   Failure to apply or verify the allowlist fails closed. The native permission primitive lives in
   `packages/events`, the owner of `JsonlEventLog`, so direct canonical writer construction is
   protected before its first write; runtime/state reuses that primitive for projections and other
   local state. Canonical logs never become `ArtifactStore` exports.

2. **Exports and API surfaces.** Files under `runs/<run_id>/exports/`, JSON API responses and
   SSE event frames are presentation projections. They pass through a JSON-shaped, fail-closed
   redaction boundary; an unclassifiable value is rejected without publishing its source bytes.
   An assurance export omits its embedded `verified_events` copy and carries no
   `bundle_sha256` for the altered projection. Its `event_head_hash` and report/decision facts are
   independently verified against the canonical event chain supplied by the runtime. A redacted
   projection is never presented as the hash-identical canonical bundle.

3. **MIRA findings.** MIRA may inspect the canonical evidence needed for deterministic controls,
   but every finding producer and every validated/copying path applies the finding boundary before
   a finding enters a report, review intent, event or export. Finding titles, prose,
   recommendations and evidence notes are redacted; evidence kinds, references, digests and
   computed numeric facts remain. Findings therefore point to canonical evidence instead of
   copying raw personal data. MIRA cannot authorize a Run or a tool call.

4. **Prompts, traces and reasoning.** Prompt assembly, model binding, compaction and MIRA
   provider paths scrub known credential values before the provider sees them. Prompt provenance
   records the credential-scrubbed model-visible prompt in the restricted local log/artifact path.
   Langfuse and every trace/export field uses the fail-closed redaction projection and remains
   non-authoritative. Chain-of-thought text is never persisted; only approved presence/count/digest
   metadata may be recorded.

The policy is implemented at named consumers rather than by a generic sink: both event builders,
prompt/model call sites, `ToolManager`, `LocalRunStore`, `LocalEventStore`, the API JSON/SSE
surfaces, Langfuse tracing and all MIRA finding producers. The canonical writer still scrubs
known credential values as defense in depth, but this does not replace source exclusion before a
provider or process call.

## Alternatives considered

| Option | Why it was rejected |
|---|---|
| Redact PII before writing the chained event | It destroys model-visible/request-reconstruction evidence and can alter metrics or identifiers. |
| Keep a raw and a redacted event log | It creates a second integrity and lifecycle surface; exports can be projections of the one canonical chain. |
| Use `chmod(0600/0700)` alone on Windows | Windows mode bits do not prove the effective ACL or remove inherited `Everyone`/`Users` grants. |
| Parse localized `icacls` output | Textual command output is a weak proof and violates the state-layer subprocess boundary; native Win32 ACL APIs return and verify the protected DACL directly. |
| Allow an unclassifiable export with a warning | A warning cannot establish that the response omitted secrets; export and trace boundaries fail closed. |
| Copy raw MIRA finding prose into reports | Reports are exports. References, digests and computed facts provide auditability without copying personal data. |
| Persist reasoning text for replay | Reasoning is not an explanation of an authorization; presence, count and digest are sufficient metadata. |

## Consequences

- `events.jsonl` and the PostgreSQL/in-memory builders remain hash-verifiable and preserve
  model-visible PII; known credential values cannot enter their payloads through the source guard.
- `runs/` is personal data at rest and its restrictive permission evidence is part of the storage
  contract. Anyone granted access to that directory remains inside the Run trust boundary.
- API, SSE, file exports and traces are safe presentation copies and cannot silently publish an
  unsupported value. Assurance verification uses the canonical chain as its independent oracle.
- MIRA reports remain useful for evidence review while limiting copied free text. F1.5 remains an
  open acceptance criterion until its independent provider-request oracle is completed; this ADR
  does not claim it closed.
