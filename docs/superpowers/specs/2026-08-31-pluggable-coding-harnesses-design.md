# Pluggable External Coding Harnesses — Design

- **Status:** Proposed
- **Date:** 2026-08-31
- **Scope:** Let THY delegate coding tasks to Claude Code, Codex, OpenCode, Cursor, or the
  existing internal coding agent without making any external product part of Thymira's domain.
- **Related:** ADR-0001, ADR-0004, ADR-0005, the E2E baseline sections 5, 27 and 28, and
  `docs/superpowers/plans/2026-08-31-pluggable-coding-harnesses.md`.

## 1. Context

Thymira already has a PydanticAI Coding/Execution agent. It is a useful fallback, but a user may
already pay for and prefer a mature coding harness such as Claude Code, Codex, OpenCode, or Cursor.
Thymira should coordinate and audit that harness rather than try to outperform it at repository
editing.

The architecture baseline anticipated this direction for OpenCode:

```text
THY -> Coding Agent -> OpenCode -> Git worktree
```

The same capability must work for all supported harnesses. The current repository does not yet
define the common outbound contract, policy semantics, evidence, or selection rules. Its existing
`adapters/*` placeholders describe the opposite direction: an external client calling the Thymira
API. Reusing them for outbound execution would invert the dependency boundary.

There are also three implementation prerequisites:

1. `ThySubgraph.invoke` does not currently pass a tool registry or `ToolContext` into `run_thy`,
   so production THY cannot use even its existing coding tools through the composition root.
2. the base policy correctly blocks every capability that declares an external effect, while a
   subscription-backed harness necessarily sends repository context to a remote model service;
3. a deferred human approval can be recorded, but the Tool Manager cannot yet resume the exact
   denied invocation under that approval.

Provider wrappers built before those gaps are closed would be unreachable or would require
misreporting their network effect. This design treats those gaps as dependencies, not workarounds.

## 2. Goals

- Let a user select `internal`, `codex`, `cursor`, `opencode`, or `claude_code` for THY coding
  tasks, with future harness ids addable through a registry rather than a schema migration.
- Reuse an official local CLI session where the vendor officially supports it, including a user's
  existing subscription login when permitted.
- Keep selection deterministic and configuration-driven. No model chooses its own harness.
- Execute integrated harnesses in a task-scoped Git worktree and return a verifiable change-set.
- Preserve Thymira's authority model: the Policy Engine authorizes the outer delegation and any
  later promotion; MIRA audits evidence; the harness never commits, merges, deploys, approves, or
  changes Run state.
- Keep the internal coding agent as an explicit backend and optional explicit fallback.
- Support a handoff mode when a harness cannot safely run headlessly on the runtime host.
- Make absence, authentication failure, unknown cost, weak confinement, and incomplete output
  visible facts rather than silent degradation.

## 3. Non-goals

- Replacing THY, MIRA, LangGraph, PydanticAI, LiteLLM, or the Tool Manager.
- Treating an external harness as an `LLMProvider`.
- Routing non-coding agents through external coding harnesses.
- Embedding a vendor login flow, reading credential files, copying OAuth tokens, or promising that
  every subscription is licensed for third-party automation.
- Intercepting every internal file or shell operation performed by a foreign harness. Thymira can
  govern only the outer invocation and its confined workspace.
- Automatically applying, committing, merging, pushing, deploying, or publishing the result.
- Depending on MCP or ACP as the common lifecycle protocol. They remain optional provider
  capabilities; the stable Thymira boundary is its own typed driver protocol.
- Running real vendor calls in CI.

## 4. Two integration directions

The repository must keep these directions separate:

```text
Inbound client integration
Claude Code / Codex / OpenCode / Cursor -> adapters/* -> Thymira API

Outbound coding delegation
THY -> deterministic coding router -> Tool Manager -> harness driver -> isolated worktree
```

`adapters/*` stays a set of thin HTTP/MCP clients with no runtime imports. Outbound drivers live in
`thymira.tools`, the layer that already owns subprocess execution, sandboxes, artifacts, Git tools,
and the Tool Manager. `thymira.thy` owns the semantic decision that one `CODING` task should use an
external backend. `thymira.core` binds project/operator configuration and run-scoped dependencies.

## 5. Architecture

```text
Run request / recipe / project preference
                  |
                  v
        deterministic selector --------------------+
          | selected backend                       | explicit fallback only
          v                                        |
     CodingTaskRunner                               |
       | internal                                   |
       +----> Delegator -> AgentRunner -> LiteLLM   |
       | external                                   |
       +----> ToolManager.execute                   |
                 |                                 |
                 v                                 |
             Policy Engine + exact Approval         |
                 |                                 |
                 v                                 |
          CodingHarnessTool                         |
                 |                                 |
          HarnessDriver registry <-----------------+
       codex | cursor | opencode | claude_code
                 |
                 v
        task-scoped Git worktree
                 |
          change-set artifacts
                 |
             MIRA audit
                 |
       separate governed promotion
```

An external harness is one opaque, high-capability tool invocation. Its internal operations are
not represented as if they passed individually through the Tool Manager. The Tool Manager records
the outer lifecycle, requested scope, policy decision, actual confinement, exit state, and artifact
digests.

## 6. Stable contracts

Contract 0.6 should add the minimum records that cross a process, persistence, or API boundary.
Provider-specific parser values remain private to `thymira.tools`.

### 6.1 Selection and configuration

`CodingExecutionPreference`:

- `mode`: `internal | external | handoff`;
- `harness_id`: optional validated slug, required for `external` or `handoff`;
- `fallbacks`: an ordered, explicit tuple of harness ids or `internal`;
- `timeout_seconds` and task limits;
- `require_clean_worktree`, true for the first integrated release.

Harness ids are strings validated by a stable slug pattern, not a closed provider enum. The four
initial ids are registry data. This keeps a fifth harness from requiring Contract 0.7.

Selection precedence is deterministic:

1. an authenticated per-Run request;
2. a run recipe;
3. `.thymira/config.yaml`;
4. an operator default;
5. `internal` only when explicitly configured as a fallback.

The repository may choose a harness id and limits. It may not provide an executable path, raw
argv, environment variables, permission-bypass flags, credential paths, or mounts. Those are
operator-owned configuration because repository content is untrusted input.

Fallback is allowed only for a configured, classified availability failure such as executable
missing, unsupported version, unauthenticated session, or an explicitly recognized rate limit.
There is no fallback after policy denial, sandbox failure, timeout, path-scope violation, malformed
output, failed validation, or a harness that already changed files. A fallback decision is always
recorded. `internal` never reappears silently.

### 6.2 Capability negotiation

`HarnessProbe` records only non-secret facts:

- harness id, executable identity and version;
- availability and coarse authentication state (`authenticated`, `not_authenticated`, `unknown`);
- supported transports and capabilities;
- whether subscription authentication is officially supported for the detected path;
- native sandbox/permission features, which never replace Thymira's sandbox;
- driver version and probe time.

Negotiated capabilities include structured stream, structured final output, resume, fork,
ephemeral session, native sandbox, permission rules, MCP control, and subscription authentication.
The selector refuses a requested mode whose required capabilities are absent.

### 6.3 Request, execution, and change-set

`HarnessRequest` carries a Run id, Task id, objective, acceptance criteria, allowed relative paths,
base commit, validation recipe, timeout, and requested observability. It never contains secrets or
arbitrary vendor flags.

`HarnessExecution` carries:

- execution id, selected harness and driver/harness versions;
- base commit and normalized changed paths;
- terminal status and exit code;
- sandbox mode and actual enforcement;
- observability level (`structured`, `partial`, `opaque`);
- usage and cost when reported, with measurement source; unknown remains `None`, never zero;
- patch, change-set manifest, sanitized operational log, and validation artifact ids;
- a digest of any external session reference, never a credential or raw login state;
- error classification suitable for deterministic fallback decisions.

`ChangeSet` pins the base commit, binary patch digest, created/modified/deleted paths, per-file
digests, total bytes, producing execution, and independent validation artifacts. A successful CLI
exit without a complete change-set is not a successful coding task.

The first integrated slice requires a clean Git worktree and a concrete base commit. Dirty working
state is recorded but not copied implicitly: the user can choose handoff mode or explicitly clean
the checkout. A later snapshot seed can be added behind the same request contract after secret and
untracked-file handling is designed.

### 6.4 Events

Add `coding_harness.selected`. It records the requested and selected ids, selection source, reason,
fallback position, capability digest, driver/harness versions, and Task id. It is log-only.

Execution itself uses the existing `policy.decision`, `human.approval_requested`,
`human.approval`, `tool.started`, `tool.denied`, `tool.completed`, and `artifact.created` events.
No synthetic `model.selected` is emitted: Thymira did not choose or observe the model used inside
the external product. External execution stays outside a PydanticAI `agent.started` /
`agent.completed` lifecycle. The existing A24 model-routing control remains true for Thymira model
agents; a new harness-evidence control validates the external path.

Raw model reasoning is never stored. Driver parsers accept unknown provider fields for forward
compatibility but retain only allowlisted operational fields after redaction. Unknown fields may
contribute to a digest and byte count; their content is not persisted.

## 7. Driver boundary

The in-process protocol belongs to `runtime/tools/src/thymira/tools/coding_harness/`:

```text
HarnessDriver
  probe() -> HarnessProbe
  start(request, invocation) -> execution handle
  events(handle) -> normalized operational events
  cancel(handle) -> cancellation outcome
  resume(session reference, request, invocation) -> execution handle
  collect(handle) -> HarnessExecution
```

The initial runtime may implement `start/events/collect` synchronously over one subprocess while
preserving this lifecycle. Every CLI command is built by a driver from typed values, uses an
explicit argv with `shell=False`, and rejects arbitrary passthrough flags.

Each registered harness is projected as a distinct tool (`coding_harness_codex`,
`coding_harness_cursor`, `coding_harness_opencode`, `coding_harness_claude_code`). Separate tool
identities make policy, fallback attempts, metrics, and evidence unambiguous.

## 8. Policy and approval

Every integrated driver truthfully declares repository read/write, process execution, source-code
disclosure, and network/model-provider effects. It must never claim `external_effects=()` merely to
pass the current base policy.

The generic external-effect BLOCK remains fail-closed. Policy matching gains explicit capability
ids and exclusions so only reviewed harness tool ids are carved out of that generic rule. Those ids
receive `REQUIRE_HUMAN_REVIEW` in the first release. An unknown new external tool remains BLOCKED.
This preserves strictest-decision resolution; it does not let a permissive overlay outrank a BLOCK.

Approval is scoped to the canonical hash of the exact harness request, selected driver,
capabilities, base commit, paths, and limits. Resuming after approval revalidates that hash and the
current base commit before executing. A changed request needs a new decision. A denial is never a
fallback signal.

The current Tool Manager deferred-approval gap must be closed before any provider driver is
enabled. Tests must prove that approval for one request cannot authorize another harness, commit,
path set, or command.

## 9. Isolation and evidence

- Integrated mode creates a detached, task-scoped worktree under `.thymira/worktrees/` from the
  pinned base commit.
- The harness receives only that worktree plus the minimum read-only credential material its
  official CLI requires. The user's full home directory is never mounted or copied.
- Local execution reports `PARTIAL`; full confinement is claimed only when the backend controls
  filesystem, process, resource, and declared network boundaries. Native provider sandbox labels
  are additional evidence, not an upgrade to Thymira's enforcement result.
- Before/after collection includes tracked diffs, binary changes, untracked files, deletions,
  symlinks, path containment, file count, and byte limits.
- A path outside the request allowlist invalidates the outcome and disables fallback/promotion.
- Worktree cleanup occurs only after durable artifacts exist. Destructive removal never runs on an
  unresolved or unverified path.
- Validation is independently re-run by Thymira from a typed, operator/project-approved argv
  recipe. A harness's claim that tests passed is evidence but not validation.

The initial feature returns a reviewable worktree and change-set. Applying it to the user's
checkout is a separate future action/tool with its own Gate decision and human-visible diff. There
is no automatic commit or merge.

## 10. Authentication and product support

Thymira discovers an official CLI and asks it for coarse status where an official command exists.
It never parses a vendor token store. Authentication remains owned by the user's installed tool.
Integrated subscription mode is local-host only: a remote Thymira server cannot borrow credentials
from the user's laptop. Remote deployments use API/operator credentials or handoff/client-side
execution in a later phase.

Capabilities reviewed on 2026-08-31:

| Harness | Initial path | Structured output | Subscription position | Initial support |
|---|---|---|---|---|
| Codex | `codex exec` CLI | JSONL and final schema | Sign in with ChatGPT is documented | Reference driver |
| Cursor | Agent CLI | JSON / stream JSON | Browser account login is documented | Integrated when probe succeeds; handoff otherwise |
| OpenCode | `opencode run` CLI | JSON stream | Several subscription/OAuth providers; not Claude Pro/Max | Integrated with external sandbox |
| Claude Code | `claude -p` CLI | JSON / stream JSON and final schema | CLI subscriptions exist, but third-party embedding is restricted | API-key path supported; local subscription experimental behind review |

Claude Code subscription automation is deliberately not declared generally supported until a
legal/product review confirms that invoking the user's local official CLI in this manner is
permitted. The Agent SDK cannot be used to recreate claude.ai login or rate limits without
Anthropic approval. OpenCode must not advertise Claude Pro/Max authentication; its official
provider documentation says that path was removed.

The drivers negotiate capabilities by detected version. Documentation gives tested versions, not
a promise that a flag is permanent.

## 11. Package placement

| Concern | Owner and location |
|---|---|
| Cross-boundary records and `coding_harness.selected` | `packages/schemas/src/thymira/schemas/harness.py`, `enums.py` |
| Policy matching and reviewed egress rules | `runtime/policies` |
| Driver protocol, registry, CLI drivers, tool wrapper, change-set capture | `runtime/tools/src/thymira/tools/coding_harness/` |
| Internal/external coding task routing | `runtime/thy/src/thymira/thy/coding_execution.py` and `nodes/execute.py` |
| Run-scoped binding and provenance | `runtime/core/src/thymira/core/graph/adapters.py`, `runs.py` |
| Request and capability/status API | `apps/api` |
| User flags and doctor/status rendering | `adapters/cli` |
| Inbound external-client integrations | existing `adapters/{claude-code,codex,opencode,vscode,generic}` |
| Evidence checks | `runtime/mira/src/thymira/mira/checks/` |

No new workspace member or top-level directory is needed.

## 12. Verification strategy

- Contract tests round-trip every new strict record and pin Contract 0.6.
- Policy tests prove reviewed harness ids require exact human review and every unknown external
  capability remains blocked.
- Driver unit tests parse checked-in representative JSON/JSONL fixtures, unknown fields,
  truncation, non-zero exits, and missing terminal output with no network.
- Router unit tests prove deterministic precedence, explicit fallback, and that an external coding
  task emits no fabricated `model.selected` event.
- Worktree integration tests run a fake executable under `tmp_path`, capture tracked/untracked/
  binary/deleted files, verify all artifact hashes, and leave the parent checkout unchanged.
- Composition tests close the existing production tool-wiring gap before exercising a harness.
- An end-to-end test uses a fake driver through THY -> Tool Manager -> change-set -> MIRA -> Gate,
  verifies the event chain, and performs no real vendor request.
- Real CLI smoke tests are opt-in integration tests or scripts that skip without an executable and
  login. They never run in the normal suite or CI.
- `just check-imports` must report `Contracts: 4 kept, 0 broken`; final verification is `just check`,
  `just test-all`, and `just test-cov`.

## 13. Rollout

1. Accept ADR-0012, add FINAL roadmap tasks, and publish Contract 0.6.
2. Close production tool wiring and exact deferred authorization.
3. Land the registry, fake driver, worktree/change-set collector, and deterministic THY router.
4. Land Codex as the reference CLI driver.
5. Land Cursor and OpenCode using the same conformance suite.
6. Land Claude Code API-key mode; enable local subscription mode only after the review gate.
7. Add API/CLI selection, capability doctor, MIRA evidence control, and user documentation.
8. Consider client-side/remote workers, dirty-worktree snapshots, and governed change promotion as
   later tasks behind the same contracts.

Each driver is independently feature-flagged. Shipping one never changes the default backend for
existing projects.

## 14. Open decisions requiring owner approval

1. Whether local Claude Code subscription invocation passes the required legal/product review.
2. Whether the first release should always require human approval for source-code egress or permit
   an operator policy to pre-authorize named repositories and harnesses.
3. Whether remote client-side execution belongs in this feature or a later worker/lease protocol.
4. Whether change-set promotion remains manual for the first release (recommended) or becomes a
   separate governed API task immediately.

None of these changes the common driver or evidence contracts. Until decided, the fail-closed
defaults are: Claude subscription disabled, human review required, local integrated execution
only, and manual promotion.

## 15. Official integration references

- Claude Code: [headless mode](https://code.claude.com/docs/en/headless),
  [CLI reference](https://code.claude.com/docs/en/cli-usage),
  [authentication](https://code.claude.com/docs/en/authentication),
  [Agent SDK restriction](https://code.claude.com/docs/en/agent-sdk/overview), and
  [sandboxing](https://code.claude.com/docs/en/sandboxing).
- Codex: [non-interactive mode](https://developers.openai.com/codex/noninteractive/),
  [CLI reference](https://developers.openai.com/codex/cli/reference/),
  [authentication](https://developers.openai.com/codex/auth/), and
  [SDK](https://developers.openai.com/codex/sdk/).
- OpenCode: [CLI](https://opencode.ai/docs/cli/),
  [providers](https://opencode.ai/docs/providers/),
  [permissions](https://opencode.ai/docs/permissions/), and
  [server](https://opencode.ai/docs/server/).
- Cursor: [CLI overview](https://cursor.com/docs/cli/overview),
  [parameters](https://cursor.com/docs/cli/reference/parameters),
  [output format](https://cursor.com/docs/cli/reference/output-format),
  [authentication](https://cursor.com/docs/cli/reference/authentication), and
  [permissions](https://cursor.com/docs/cli/reference/permissions).
