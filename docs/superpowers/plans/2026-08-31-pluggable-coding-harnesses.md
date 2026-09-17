# Pluggable External Coding Harnesses Implementation Plan

> **For implementers:** execute this plan as small PRs in dependency order. Use the repository
> skills named under each task before editing. Do not begin a provider driver until the generic
> authorization, worktree, and evidence slices are green.

**Goal:** Let THY use an explicitly selected Claude Code, Codex, OpenCode, Cursor, or internal
coding backend for coding tasks, while Thymira retains policy authority, state, evidence, audit,
and change promotion.

**Architecture:** External harnesses are optional outbound tools under `thymira.tools`, not
`LLMProvider` implementations and not code under the inbound `adapters/*` clients. A deterministic
THY router selects a configured backend; the Tool Manager authorizes one opaque external
invocation; the driver runs in a task-scoped Git worktree; and a content-addressed change-set is
audited before any separate promotion. Provider operations are never represented as individual
Thymira-authorized tool calls.

**Initial transport:** Official local CLIs. They reuse the user's own supported login without
Thymira reading credentials. SDK/server transports may be added behind the same driver protocol
later.

**Spec:** `docs/superpowers/specs/2026-08-31-pluggable-coding-harnesses-design.md`

## Global constraints

- Everything committed to the repository is in English.
- The default remains the existing internal Coding/Execution agent. No existing project changes
  backend merely by installing another CLI.
- Harness selection comes from typed configuration and is resolved by code. A model never chooses
  a harness id, executable, model, permission mode, fallback, or provider flags.
- Every external invocation declares network and source-code disclosure honestly and passes
  through `ToolManager`, `Gate`, and the deterministic Policy Engine.
- The harness is an opaque external effect. Its internal commands are not falsely recorded as
  Thymira Tool Manager calls.
- No provider wrapper may use a permission/sandbox bypass flag.
- Executables, argv templates, credential locations, environment allowlists, and feature flags are
  operator configuration. `.thymira/config.yaml` may select only a registered id, mode, limits,
  paths, and explicit fallbacks.
- Integrated execution uses a pinned clean Git commit and a task-scoped worktree in the first
  release. Dirty checkouts fail clearly or use handoff; they are never copied implicitly.
- A CLI exit code or final message alone never proves success. Success requires a contained,
  content-addressed change-set and independent validation evidence.
- Unknown cost/tokens remain unknown. They are never recorded as zero and cannot silently satisfy
  a configured budget.
- No raw chain-of-thought, unknown provider event content, credential, or complete user home is
  persisted or mounted. Logs are allowlisted, bounded, and redacted before artifact storage.
- Fallbacks are explicit and recorded. Never fallback after DENY, timeout, sandbox failure,
  malformed output, changed files, path escape, or failed validation.
- No automatic apply, commit, merge, push, deployment, or publication. Promotion is a separate
  policy-gated action and is not required for the first integrated slice.
- Unit and normal integration tests use a fake driver or fake executable; no real model, network,
  subscription, or vendor login in CI.
- Windows and Linux are first-class. Commands use explicit argv and `shell=False` through the
  existing `Sandbox` boundary.

## Proposed roadmap tasks

Add these as **FINAL** tasks with `todo` status to the single catalogue in
`docs/roadmap/product-final.md`. They are deliberately post-MVP; no rows are added to the MVP
member tables.

| Task | Title | Owner | Size | Depends on |
|---|---|---:|:---:|---|
| `HARNESS-01` | External coding harness architecture and Contract 0.6 | P1 | L | — |
| `HARNESS-02` | Exact external-effect authorization and approved-call resume | P4 | L | `HARNESS-01`, `HITL-01` |
| `HARNESS-03` | Harness driver protocol, registry and scripted conformance backend | P3 | L | `HARNESS-01`, `TOOL-06` |
| `HARNESS-04` | Isolated harness tool and verifiable change-set capture | P3 | L | `HARNESS-02`, `HARNESS-03`, `TOOL-11` |
| `HARNESS-05` | Deterministic THY coding router and production tool wiring | P2 | L | `HARNESS-04`, `THY-12` |
| `HARNESS-06` | Run/API configuration, capability discovery and provenance | P1 | M | `HARNESS-05`, `RA-API-02` |
| `HARNESS-07` | CLI backend selection, handoff and harness doctor | P5 | M | `HARNESS-06` |
| `HARNESS-08` | Codex CLI driver | P3 | M | `HARNESS-04` |
| `HARNESS-09` | Cursor Agent CLI driver | P3 | M | `HARNESS-04` |
| `HARNESS-10` | OpenCode CLI driver | P3 | M | `HARNESS-04` |
| `HARNESS-11` | Claude Code CLI driver and subscription review gate | P3 | L | `HARNESS-04` |
| `HARNESS-12` | MIRA external-harness evidence control | P4 | M | `HARNESS-04`, `HARNESS-05` |
| `HARNESS-13` | Harness conformance E2E, support matrix and operator documentation | P5 | L | `HARNESS-06`–`HARNESS-12` |

When adding the rows, recompute all catalogue totals rather than copying the current counts. Update
the denominator in `mvp-minimum.md` even though all new tasks are FINAL. Also fix the pre-existing
stale counts in `docs/roadmap/README.md` (it currently says 129/81 while the catalogue contains
131/82 before these tasks).

## File map

| Path | Responsibility | Roadmap task |
|---|---|---|
| `docs/adr/0012-pluggable-external-coding-harnesses.md` | Accepted authority/trust-boundary decision | `HARNESS-01` |
| `packages/schemas/src/thymira/schemas/harness.py` | Cross-boundary preferences, probes, executions and change-sets | `HARNESS-01` |
| `packages/schemas/src/thymira/schemas/{enums,project}.py` | Closed mode/status values and project preference | `HARNESS-01` |
| `runtime/policies/src/thymira/policies/{models,engine}.py` | Exact reviewed capability matching | `HARNESS-02` |
| `runtime/policies/src/thymira/policies/defaults/base.yaml` | Fail-closed reviewed harness carve-out | `HARNESS-02` |
| `runtime/tools/src/thymira/tools/manager.py` | Resume one exact approved invocation | `HARNESS-02` |
| `runtime/tools/src/thymira/tools/coding_harness/base.py` | Driver lifecycle protocol | `HARNESS-03` |
| `runtime/tools/src/thymira/tools/coding_harness/registry.py` | Registered ids and capability lookup | `HARNESS-03` |
| `runtime/tools/src/thymira/tools/coding_harness/scripted.py` | Deterministic test backend | `HARNESS-03` |
| `runtime/tools/src/thymira/tools/coding_harness/tool.py` | One Tool Manager wrapper per driver | `HARNESS-04` |
| `runtime/tools/src/thymira/tools/coding_harness/changes.py` | Before/after containment and change-set capture | `HARNESS-04` |
| `runtime/tools/src/thymira/tools/coding_harness/{codex,cursor,opencode,claude_code}.py` | Official CLI drivers | `HARNESS-08`–`HARNESS-11` |
| `runtime/thy/src/thymira/thy/coding_execution.py` | Internal/external deterministic coding router | `HARNESS-05` |
| `runtime/thy/src/thymira/thy/nodes/execute.py` | Route only `ThyAgentKind.CODING` through that seam | `HARNESS-05` |
| `runtime/core/src/thymira/core/graph/{protocol,adapters}.py` | Run-scoped tool context and router injection | `HARNESS-05` |
| `runtime/core/src/thymira/core/runs.py` | Immutable run-start preference/probe provenance | `HARNESS-06` |
| `apps/api/src/thymira/api/{schemas,deps}.py`, routes | Per-Run request and discovery API | `HARNESS-06` |
| `adapters/cli/src/thymira/cli/` | Selection flags, handoff and doctor UX | `HARNESS-07` |
| `runtime/mira/src/thymira/mira/checks/harnesses.py` | External-harness evidence control | `HARNESS-12` |
| `tests/thymira/test_*harness*.py` | Unit, integration and E2E verification | all |

No new workspace member, top-level directory, provider Python dependency, or provider-specific
domain type is planned.

---

### Task 1: Record the decision and register the work (`HARNESS-01`, documentation slice)

**Skills:** `repo-skeleton`, `check-imports`.

**Files:**

- Create: `docs/adr/0012-pluggable-external-coding-harnesses.md`
- Modify: `docs/adr/README.md`
- Modify: `docs/architecture/e2e-baseline-v2.md`
- Modify: `docs/architecture/README.md`
- Modify: `docs/roadmap/product-final.md`
- Modify: `docs/roadmap/mvp-minimum.md`
- Modify: `docs/roadmap/README.md`
- Modify: `.agents/skills/repo-skeleton/references/roadmap-to-layout.md`
- Regenerate: `.claude/skills/repo-skeleton/references/roadmap-to-layout.md`

**Interfaces:** No runtime interface. Establishes the accepted inbound/outbound boundary and the
authoritative task ids above.

- [ ] Write ADR-0012 as `Proposed`; after owner review, change it to `Accepted`. It extends
  ADR-0001/0004/0005 and does not rewrite those immutable records.
- [ ] State explicitly that LiteLLM remains the only gateway for models Thymira invokes directly;
  a foreign harness's own model call is an opaque external tool effect, not a hidden exception.
- [ ] Generalize the baseline's OpenCode-only `THY -> coding tool` diagram to all four harnesses.
- [ ] Keep `adapters/*` as inbound clients; put outbound execution in `runtime/tools`.
- [ ] Add all 13 FINAL roadmap tasks with complete detail sections (`Deliverable`, `Done when`,
  `Seam`, `Specified at`, dependencies, owner, size, and `todo`).
- [ ] Update generated counts and the repo-skeleton mapping, then sync skills.

Run:

```text
just sync-skills
just validate-skills
just check-roadmap
```

Expected: skill copies are byte-identical; roadmap counts/dependencies are consistent; no task is
added to the MVP subset.

Suggested commit: `docs(architecture): define pluggable external coding harnesses`

---

### Task 2: Publish Contract 0.6 (`HARNESS-01`, schema slice)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`, `check-imports`.

**Files:**

- Create: `packages/schemas/src/thymira/schemas/harness.py`
- Modify: `packages/schemas/src/thymira/schemas/enums.py`
- Modify: `packages/schemas/src/thymira/schemas/project.py`
- Modify: `packages/schemas/src/thymira/schemas/__init__.py`
- Create: `docs/contracts/contract-v0.6.md`
- Modify: `docs/contracts/contract-v0.1.md`
- Modify: `tests/thymira/test_schemas.py`
- Create: `tests/thymira/test_schemas_harness.py`

**Interfaces:**

- `CodingExecutionPreference`
- `HarnessProbe`
- `HarnessRequest`
- `HarnessExecution`
- `ChangeSet`
- the minimal closed enums described in the design spec
- `ProjectConfig.coding`, defaulting to the existing internal behavior
- `EventType.CODING_HARNESS_SELECTED`
- `CONTRACT_VERSION = "0.6"`

- [ ] Write failing validation/round-trip tests first: mode/id consistency, explicit fallback
  order, path normalization, positive limits, unknown usage, digest formats, and strict unknown-key
  rejection.
- [ ] Keep provider ids as validated strings. Do not add a provider enum.
- [ ] Ensure no executable, raw argv, environment, credential path, token, or permission flag can
  enter `ProjectConfig`.
- [ ] Bound text, path count, file count, byte count, and collection sizes.
- [ ] Add Contract 0.6 docs and update every version assertion/export.

Run:

```text
uv run pytest tests/thymira/test_schemas.py tests/thymira/test_schemas_harness.py -q
just check-imports
```

Expected: schema tests pass; `Contracts: 4 kept, 0 broken`.

Suggested commit: `feat(schemas): define external coding harness contract`

---

### Task 3: Authorize exact external effects and resume the exact call (`HARNESS-02`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`, `check-imports`.

**Files:**

- Modify: `runtime/policies/src/thymira/policies/models.py`
- Modify: `runtime/policies/src/thymira/policies/engine.py`
- Modify: `runtime/policies/src/thymira/policies/defaults/base.yaml`
- Modify: `runtime/tools/src/thymira/tools/manager.py`
- Modify: `runtime/tools/src/thymira/tools/models.py`
- Modify: `tests/thymira/test_policies.py`
- Create: `tests/thymira/test_tools_approved_resume.py`

**Interfaces:** Capability matching can name/include/exclude exact `ToolCapability.id` values;
the Tool Manager can resume a previously denied invocation only with an `Approval` bound to the
same `AuthorizationContext` hash.

- [ ] Add tests proving the current generic external-effect rule still BLOCKS an unknown external
  tool.
- [ ] Add reviewed ids for the four harness tools. Exclude only those ids from the generic rule
  and give them a specific `REQUIRE_HUMAN_REVIEW` rule. Preserve BLOCK precedence.
- [ ] Bump the base policy version because its content hash changes.
- [ ] Canonically hash driver id, base commit, objective digest, path scope, capability digest,
  timeout and limits into the authorization context.
- [ ] Persist the denied request without credentials; resolve it through the existing approval
  service; revalidate the exact hash and current commit before execution.
- [ ] Reject reuse of an approval with another harness, commit, task, path set, timeout, or args.
- [ ] Ensure DENY and rejected/cancelled/unavailable approvals never execute and never trigger
  fallback.

Run:

```text
uv run pytest tests/thymira/test_policies.py tests/thymira/test_tools_approved_resume.py -q
just check-imports
```

Expected: reviewed harness requests pause for a human and only the exact approved request can
resume; every unknown external capability remains blocked.

Suggested commit: `feat(policies): authorize exact reviewed harness invocations`

---

### Task 4: Build the driver protocol, registry and scripted backend (`HARNESS-03`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`, `check-imports`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/__init__.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/base.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/models.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/registry.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/scripted.py`
- Modify: `runtime/tools/src/thymira/tools/__init__.py`
- Create: `tests/thymira/test_tools_coding_harness.py`

**Interfaces:** `HarnessDriver`, execution handle, normalized operational event, `HarnessRegistry`,
and `ScriptedHarnessDriver`. Cross-boundary values use Contract 0.6; in-process lifecycle helpers
are typed dataclasses/Protocols.

- [ ] Write one reusable conformance test function that every real and scripted driver must pass.
- [ ] Cover probe availability/version/auth-state without reading credential files.
- [ ] Cover start, normalized event iteration, cancellation, collection, and unsupported resume.
- [ ] Make registry order deterministic and reject duplicate ids.
- [ ] Classify only errors approved for fallback. Unknown errors are non-fallback failures.
- [ ] Accept unknown provider event fields but discard their content; preserve only a digest and
  byte count after redaction.

Run:

```text
uv run pytest tests/thymira/test_tools_coding_harness.py -q
just typecheck
just check-imports
```

Expected: the scripted driver passes the complete conformance suite with no network or subprocess.

Suggested commit: `feat(tools): add coding harness driver protocol`

---

### Task 5: Run one driver as a governed tool and capture a complete change-set (`HARNESS-04`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`, `check-imports`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/tool.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/changes.py`
- Create: `runtime/tools/src/thymira/tools/coding_harness/workspace.py`
- Modify: `runtime/tools/src/thymira/tools/registry.py` or its builtins composition
- Modify: `runtime/tools/src/thymira/tools/builtins/worktrees.py` only if its public seam is
  insufficient; preserve existing behavior
- Create: `tests/thymira/test_tools_harness_changes.py`
- Create: `tests/thymira/test_coding_harness_integration.py`

**Interfaces:** One `CodingHarnessTool` instance per registered driver; a worktree lifecycle and
change collector that returns a strict `HarnessExecution` plus `ChangeSet` artifacts.

- [ ] Unit-test argv/path containment, clean-repository precondition, file/byte limits, symlinks,
  timeouts, and worktree cleanup ordering.
- [ ] Use a real temporary Git repository and a fake executable in the integration test.
- [ ] Capture modified, created, deleted, renamed, untracked, binary and executable-bit changes.
  Do not rely on plain `git diff`, which omits untracked files.
- [ ] Save a binary-capable patch, manifest, per-file digests and sanitized operational log in the
  run's `ArtifactStore`; verify all artifacts before reporting success.
- [ ] Re-run a typed validation argv independently through the sandbox and store its evidence.
- [ ] Mark an out-of-scope path or incomplete output as failed; retain evidence; do not fallback.
- [ ] Report the outer sandbox's actual enforcement. Never upgrade it from a provider's native
  sandbox claim.
- [ ] Leave the parent checkout byte-for-byte unchanged.

Run:

```text
uv run pytest tests/thymira/test_tools_harness_changes.py -q
uv run pytest tests/thymira/test_coding_harness_integration.py -q
just check-imports
```

Expected: integration test verifies the event chain and artifact store, captures every change
class, and proves the parent checkout did not change.

Suggested commit: `feat(tools): capture governed harness change sets`

---

### Task 6: Route coding tasks and close production tool wiring (`HARNESS-05`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`, `check-imports`.

**Files:**

- Create: `runtime/thy/src/thymira/thy/coding_execution.py`
- Modify: `runtime/thy/src/thymira/thy/nodes/execute.py`
- Modify: `runtime/thy/src/thymira/thy/graph.py`
- Modify: `runtime/thy/src/thymira/thy/agents/coding.py`
- Modify: `runtime/core/src/thymira/core/graph/protocol.py`
- Modify: `runtime/core/src/thymira/core/graph/adapters.py`
- Modify: `apps/api/src/thymira/api/deps.py`
- Create: `tests/thymira/test_thy_coding_harness.py`
- Modify: `tests/thymira/test_core_graph_adapters.py`
- Modify: `tests/thymira/test_core_composition.py`

**Interfaces:** `CodingTaskRunner` selects internal or external execution for
`ThyAgentKind.CODING`; the core composition root injects run-scoped `ToolManager`, registry,
workspace, Gate, ArtifactStore and risk context into production THY.

- [ ] First write a regression test showing a tool-using coding task through
  `build_runtime_graph_factory` currently lacks tools.
- [ ] Close that wiring generically; do not special-case provider names in core.
- [ ] Route only CODING tasks through `CodingTaskRunner`. Every other agent uses the unchanged
  Delegator/AgentRunner path.
- [ ] Internal mode reproduces the existing `CodeResult`, Task outcome, events and retry behavior.
- [ ] External mode calls the preselected harness tool directly through the Tool Manager and maps
  its strict result to the same Task/state boundary.
- [ ] Emit `coding_harness.selected` once per external attempt. Emit no synthetic
  `model.selected` and no fake PydanticAI agent lifecycle.
- [ ] Make selection/fallback deterministic, immutable for the Run, and sequential in the first
  release. Do not enable external fan-out over the shared artifact store.

Run:

```text
uv run pytest tests/thymira/test_thy_coding_harness.py tests/thymira/test_core_graph_adapters.py tests/thymira/test_core_composition.py -q
just typecheck
just check-imports
```

Expected: both internal and scripted-external coding complete through the production composition;
the external trace contains a harness selection and tool lifecycle but no fabricated model choice.

Suggested commit: `feat(thy): route coding tasks to configured harnesses`

---

### Task 7: Add per-Run configuration, discovery API and immutable provenance (`HARNESS-06`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`, `check-imports`.

**Files:**

- Modify: `apps/api/src/thymira/api/schemas.py`
- Create: `apps/api/src/thymira/api/routes/harnesses.py`
- Modify: `apps/api/src/thymira/api/app.py`
- Modify: `apps/api/src/thymira/api/deps.py`
- Modify: `runtime/core/src/thymira/core/runs.py`
- Modify: `runtime/core/src/thymira/core/provenance.py`
- Modify: `docs/contracts/api-v0.1.md`
- Create: `tests/thymira/test_api_harnesses.py`
- Modify: `tests/thymira/test_api_runs.py`
- Modify: `tests/thymira/test_core.py`

**Interfaces:** `POST /runs` accepts an optional `coding` preference; `GET /harnesses` returns
non-secret probe/capability facts; run-start evidence pins the requested preference, registry and
probe digests used for that Run.

- [ ] Implement precedence: authenticated Run request -> recipe -> project -> operator default.
- [ ] Reject a Run override that the operator registry does not enable. Never treat install
  discovery as authorization.
- [ ] Probe with a bounded cache; return availability/auth state without identities, paths to
  token stores, raw environment, or tokens.
- [ ] Persist the resolved preference and probe digest in `run.started`; resume reads the pinned
  value instead of re-reading changed project configuration.
- [ ] Preserve the existing API response when `coding` is omitted.

Run:

```text
uv run pytest tests/thymira/test_api_harnesses.py tests/thymira/test_api_runs.py tests/thymira/test_core.py -q
just check-imports
```

Expected: API contract tests pass; old clients need no new field; resume uses original provenance.

Suggested commit: `feat(api): expose coding harness selection and status`

---

### Task 8: Add CLI selection, handoff and doctor UX (`HARNESS-07`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`.

**Files:**

- Modify: `adapters/cli/src/thymira/cli/client.py`
- Modify: `adapters/cli/src/thymira/cli/commands/run.py`
- Create: `adapters/cli/src/thymira/cli/commands/harnesses.py`
- Modify: `adapters/cli/src/thymira/cli/__main__.py`
- Create: `tests/thymira/test_cli_harnesses.py`
- Modify: `tests/thymira/test_cli_integration.py`

**Interfaces:**

- `thymira run ... --coding-harness ID`
- `--coding-mode internal|external|handoff`
- repeatable explicit `--coding-fallback ID`
- `thymira harnesses` and `thymira harnesses doctor`

- [ ] Render availability, detected version, coarse auth state, capabilities, tested status and
  experimental warnings.
- [ ] Make `handoff` generate a bounded task bundle and expected change-set instructions without
  claiming execution occurred.
- [ ] Surface every fallback and weak-confinement warning prominently.
- [ ] Keep the CLI a pure HTTP client with no runtime import and no direct provider subprocess.

Run:

```text
uv run pytest tests/thymira/test_cli_harnesses.py tests/thymira/test_cli_integration.py -q
just check-imports
```

Expected: CLI tests pass and the adapter still imports no runtime member.

Suggested commit: `feat(cli): select and inspect coding harnesses`

---

### Task 9: Implement Codex as the reference CLI driver (`HARNESS-08`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/codex.py`
- Create: `tests/thymira/fixtures_harness_codex.py`
- Create: `tests/thymira/test_tools_harness_codex.py`

**Interfaces:** Official `codex exec` non-interactive path, JSONL operational stream, final output
schema, optional ephemeral session, and coarse login probe.

- [ ] Start with a spike against the installed official CLI and current official docs; record the
  tested minimum/version behavior in the driver README/support matrix.
- [ ] Use `--json`, an output schema, explicit worktree, workspace-write sandbox, and the most
  restrictive noninteractive approval mapping that remains functional. Never pass a bypass flag.
- [ ] Parse normal completion, tool failure, refusal, truncation, cancellation, unsupported
  version, unauthenticated session and rate limit.
- [ ] Reuse "Sign in with ChatGPT" only through the official CLI. Do not inspect `CODEX_HOME`.
- [ ] Run the generic driver conformance suite over representative fixtures.

Run:

```text
uv run pytest tests/thymira/test_tools_harness_codex.py tests/thymira/test_tools_coding_harness.py -q
```

Expected: all parser/conformance tests pass offline.

Optional manual smoke: opt-in only, skipped without executable/login, and confined to a disposable
temporary repository.

Suggested commit: `feat(tools): add Codex coding harness driver`

---

### Task 10: Implement Cursor Agent with handoff fallback (`HARNESS-09`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/cursor.py`
- Create: `tests/thymira/fixtures_harness_cursor.py`
- Create: `tests/thymira/test_tools_harness_cursor.py`

**Interfaces:** Official Cursor Agent CLI when its probe confirms a supported headless command;
otherwise the same registered id exposes handoff capability only.

- [ ] Detect the current official executable/command and version rather than assuming the editor
  launcher and agent launcher are identical.
- [ ] Parse JSON/stream JSON without assuming a final JSON Schema feature.
- [ ] Require exit state + terminal event + complete diff/change-set; a final message is
  insufficient.
- [ ] Treat model-based permission classification as advisory. Only deterministic allow/deny
  configuration contributes to authorization.
- [ ] Cover browser-account login state coarsely without reading user data.

Run:

```text
uv run pytest tests/thymira/test_tools_harness_cursor.py tests/thymira/test_tools_coding_harness.py -q
```

Expected: supported fixtures run integrated; unsupported/ambiguous versions select handoff and do
not launch a process.

Suggested commit: `feat(tools): add Cursor coding harness driver`

---

### Task 11: Implement OpenCode with an external sandbox (`HARNESS-10`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/opencode.py`
- Create: `tests/thymira/fixtures_harness_opencode.py`
- Create: `tests/thymira/test_tools_harness_opencode.py`

**Interfaces:** Official `opencode run --format json` CLI path. Server/OpenAPI and ACP remain later
transports behind the same protocol.

- [ ] Treat application-level allow/ask/deny permissions as defense in depth, not OS confinement.
- [ ] Require the outer Thymira sandbox/worktree and report its actual enforcement.
- [ ] Parse sessions, events, abort and diff facts needed by the common contract while ignoring
  unknown content safely.
- [ ] Support only officially documented subscription/OAuth providers. Explicitly reject any UI
  claim that Claude Pro/Max is available through OpenCode.

Run:

```text
uv run pytest tests/thymira/test_tools_harness_opencode.py tests/thymira/test_tools_coding_harness.py -q
```

Expected: offline fixtures pass; permissive provider defaults never weaken Thymira policy.

Suggested commit: `feat(tools): add OpenCode coding harness driver`

---

### Task 12: Implement Claude Code with a subscription review gate (`HARNESS-11`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`,
`python-testing-integration`.

**Files:**

- Create: `runtime/tools/src/thymira/tools/coding_harness/claude_code.py`
- Create: `tests/thymira/fixtures_harness_claude_code.py`
- Create: `tests/thymira/test_tools_harness_claude_code.py`

**Interfaces:** Official `claude -p` JSON/stream-JSON path and final JSON Schema. API-key mode and
local-subscription mode are separate probe capabilities.

- [ ] Complete and record the legal/product/security review before enabling subscription mode.
  Until accepted, the probe reports it as installed but experimental/disabled.
- [ ] Do not use the Agent SDK to recreate claude.ai login or rate limits.
- [ ] Never pass `--dangerously-skip-permissions` or equivalent.
- [ ] Account for the fact that reproducible bare mode does not reuse OAuth/keychain subscription
  login. Subscription mode must report the extra user hooks/plugins/settings trust surface and
  remain local-only.
- [ ] Do not mount the full home into a sandbox. If minimum credential exposure cannot be achieved
  on an OS, integrated subscription mode is unavailable there and handoff remains supported.
- [ ] Parse normal/failure/truncated output and run the common conformance suite.

Run:

```text
uv run pytest tests/thymira/test_tools_harness_claude_code.py tests/thymira/test_tools_coding_harness.py -q
```

Expected: API-key fixtures pass; subscription execution remains disabled unless the recorded review
and operator flag both permit it.

Suggested commit: `feat(tools): add guarded Claude Code harness driver`

---

### Task 13: Audit external-harness evidence without inventing model provenance (`HARNESS-12`)

**Skills:** `python-general`, `python-testing`, `python-testing-unit`, `check-imports`.

**Files:**

- Create: `runtime/mira/src/thymira/mira/checks/harnesses.py`
- Modify: `runtime/mira/src/thymira/mira/checks/__init__.py`
- Modify: `runtime/mira/src/thymira/mira/checks/controls.py`
- Create: `tests/thymira/test_mira_checks_harnesses.py`
- Modify: `docs/governance/requirements_controls.json` if the new control maps to a requirement

**Interfaces:** New deterministic control `A25` (confirm the next free id at implementation time)
for the harness-selection -> authorized tool lifecycle -> verified change-set chain.

- [ ] PASS only when selection, policy decision, exact approval where required, paired tool
  lifecycle, pinned base commit, complete change-set, verified artifacts, path containment and
  validation evidence agree.
- [ ] FAIL or require review for missing artifacts, digest mismatch, changed base, unknown driver,
  out-of-scope path, or unpaired lifecycle.
- [ ] Reuse the existing sandbox-enforcement control for PARTIAL/UNUSABLE findings; do not duplicate
  it.
- [ ] Keep A24 unchanged for actual Thymira model-agent lifecycles. Assert that external harnesses
  do not need or receive a fabricated `model.selected`.
- [ ] MIRA reads events and artifacts only; it never imports THY or a driver and never modifies the
  worktree.

Run:

```text
uv run pytest tests/thymira/test_mira_checks_harnesses.py tests/thymira/test_mira_checks.py -q
just check-imports
```

Expected: evidence gaps produce deterministic findings and the THY/MIRA independence contract is
kept.

Suggested commit: `feat(mira): audit external harness change-set evidence`

---

### Task 14: Complete E2E verification and user/operator documentation (`HARNESS-13`)

**Skills:** `python-testing`, `python-testing-integration`, `changelog`, `check-imports`.

**Files:**

- Create: `tests/thymira/test_coding_harness_e2e.py`
- Modify: `README.md`
- Modify: `.env.example` only for non-secret operator feature flags/paths
- Modify: `AGENTS.md`
- Modify: `runtime/tools/README.md`
- Modify: `runtime/thy/README.md`
- Modify: `runtime/core/README.md`
- Modify: `adapters/README.md`
- Modify: `adapters/{claude-code,codex,opencode,vscode,generic}/README.md`
- Create: `docs/architecture/external-coding-harnesses.md`
- Modify: `docs/architecture/README.md`
- Modify: `CHANGELOG.md`

**Interfaces:** Published support matrix, setup/doctor instructions, trust model, fallback behavior,
local-vs-remote limitations, and manual change-set review workflow.

- [ ] E2E with `ScriptedHarnessDriver`: Run -> THY coding selection -> exact policy/HITL -> isolated
  worktree -> change-set -> MIRA A25 -> terminal decision. Verify event chain and every artifact.
- [ ] Add cases for internal default, unavailable requested driver with no fallback, explicit
  fallback, path violation, partial sandbox, unknown cost under a configured ceiling, and denied
  egress.
- [ ] Document inbound adapters separately from outbound drivers in every placeholder README.
- [ ] Publish per-provider tested versions/capabilities and label experimental support honestly.
- [ ] Explain that subscription auth remains vendor-owned and may be unsupported by OS, deployment
  topology, account type, or terms.
- [ ] Document that the first release produces a reviewable change-set/worktree and does not
  auto-apply it.
- [ ] Add a user-visible `[Unreleased] -> Added` changelog entry.

Run the feature tests, then the complete gates:

```text
uv run pytest tests/thymira/test_coding_harness_e2e.py -q
just lint
just typecheck-ratchet
just check-imports
just validate-skills
just check-roadmap
just test
just test-all
just test-cov
just check
```

Expected:

- `Contracts: 4 kept, 0 broken`;
- all tests pass without a vendor executable or network;
- default internal runs preserve their previous event/output behavior;
- coverage does not decrease;
- the roadmap, skill copies and Contract 0.6 docs agree.

Suggested commit: `feat: document and verify pluggable coding harnesses`

## Provider smoke-test policy

Real CLI smoke tests are never part of `just test`, `just test-all`, or CI. Provide one explicit
operator command that:

1. creates a disposable temporary Git repository;
2. probes one named CLI and coarse auth state;
3. prints the exact data categories that will leave the machine;
4. asks for a human confirmation;
5. executes a harmless bounded edit under the same driver/sandbox path;
6. verifies and displays the resulting change-set;
7. removes only the validated disposable worktree.

It must skip cleanly when the executable/login is unavailable and must never run against the
Thymira repository itself by default.

## Definition of done

The feature is complete only when all of the following are true:

- A project with no coding configuration behaves exactly as before.
- A user can explicitly select each of the four harness ids and see whether integrated or handoff
  mode is currently available.
- At least the supported path of every driver passes the same offline conformance suite.
- No driver can execute without an honest external-effect capability and policy decision.
- An approval cannot authorize a different request.
- An external task cannot change the parent checkout directly.
- Every successful task yields a verifiable, contained change-set and independent validation.
- MIRA can reconstruct the external path without importing THY or provider code.
- No `model.selected` claims knowledge Thymira did not have.
- Missing usage/cost, weak confinement, fallback, and experimental auth are visible.
- No provider credential, raw reasoning, or unbounded output reaches the event log or artifacts.
- Documentation distinguishes subscription support from API-key support and local execution from
  remote deployments.
- `just check`, `just test-all`, and `just test-cov` pass, with their outputs recorded in the PR.

## Deferred follow-ups

These use the same contracts but are intentionally outside this plan's first complete release:

- snapshotting a dirty working tree, including safe treatment of untracked files and secrets;
- a client-side worker/lease protocol for remote Thymira servers using a laptop's subscription;
- SDK or loopback-server transports for richer cancellation/streaming;
- encrypted durable external-session resume;
- parallel external coding worktrees;
- a separate governed `apply_change_set`/merge flow;
- repository-specific pre-authorization of egress after policy and legal review.
