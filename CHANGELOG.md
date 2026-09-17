# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **The web console now carries THY/MIRA's brand colours.** The top-bar wordmark splits into
  `THY` (blue) and `MIRA` (orange), matching the product logo. The run stepper's active-phase dot
  now switches between the two depending on which orchestrator drives that stage (THY for
  Plan/Execute/Report, MIRA for Audit); the two orchestrator fields on the model-routing settings
  page get a matching colour swatch; and the Agents/Audit tabs' panels (`Delegation`, `MIRA
  audit`, `Findings`, `Audit findings`) get a thin left-edge accent in the owning orchestrator's
  colour, the same idiom `.card.is-answerable` already used for review-state cards. The home page
  gains a short "Orchestrators" legend explaining the two colours. New `--thy`/`--mira` CSS custom
  properties and `.tone-thy`/`.tone-mira` utility classes back all of it (`apps/web`).

### Added
- **`ml-agent` is now routable and audited.** `ThyAgentKind.ML` ("ml-agent") joins the roster
  `execute_node`/Plan can reach: Plan's own guardrail (`_route_model_plan`) now accepts `ML` as an
  auditable alternative to `EXPERIMENT`, `execute_node` folds a completed `ml-agent` settlement
  into a `schemas.Experiment` the same way it does for `EXPERIMENT` (`experiment_from_ml_result`,
  new in `thymira.thy.experiments`), and a completed `MLResult` that asks for tuning help now
  actually reaches `ml-tuning-helper` through a follow-up delegation in `execute_node` (THY-19's
  helper tier was built but unreachable from a real Run). Plan's own agent description sharpens
  the choice between `experiment` and `ml-agent`: the latter is only for a request that explicitly
  asks for iterative hyperparameter search, not a single baseline training run.
- **MIRA's own `agent.completed` now carries real cost evidence.** `AuditAgentContext` had no
  `RunUsage` of its own, so `credit_risk`/`euaiact`/other audit-pack sub-agents always recorded
  `input_tokens`/`output_tokens`/`cost_usd` as absent on their completion event, even though the
  same call's real cost was already being charged into the run-wide `UsageLedger` via
  `record_model_usage`. `run_audit_agent` (`thymira.mira.agents.runner`) now wraps that same
  callback with a per-invocation `RunUsage` accumulator and attaches the numbers to its
  completion/failure events, matching the shape THY's own `agent.completed` already carries.
  Budget enforcement is unchanged: the run-wide ledger still receives the exact same response.
- **A resumed step now compacts its own already-resolved tool round-trips before failing closed
  on `context_budget`** (`resumed-steps-skip-context-compaction.md`'s vector 2). Vector 1 already
  compacted the whole Run's event-log surface on resume; a step's *own* accumulated
  `ToolCallPart`/`ToolReturnPart` history (`resume.messages`, PydanticAI's native message list)
  was not touched and still failed closed whenever it alone overflowed the context window.
  `_compact_resolved_tool_rounds` (`thymira.agents.runner`) now drops the oldest fully-resolved
  round-trips -- never the still-open, deferred round the resume exists for, and never a request
  message that mixes a resolved return with anything else -- stopping as soon as the request fits
  or no more rounds are eligible. Scope note: this only helps a resume whose *own* tool history
  is what overflows the window (for example one oversized tool output from an earlier, already-
  approved call in the same step); it does not reduce the number of human-review round trips a
  task needs (`GOV-008`), which remains the dominant cost driver for a review-heavy Run.

### Fixed
- **The Data agent's `max_turns` is 10, not 6.** A clean run of `profile_dataset` plus a few
  `query_sql` calls already spent all 6 turns with nothing left to report a final `DataProfile`,
  so the agent was marked `FAILED` (`end_reason: max_turns`, `stop_reason: out-of-room`) even
  when every tool call it made succeeded (observed live on `examples/credit-risk`: 1
  `profile_dataset` + 5 `query_sql` calls, all `COMPLETED`, still exhausted the budget). The
  ceiling now matches the headroom other specialists (`ml`, `statistics`) already carry for a
  wrap-up turn.
- **THY's Plan prompt now says which files may be `kind="report"`.** Nothing in
  `_plan_instructions` (`thymira.thy.nodes.plan`) told the planning model what `ArtifactKind` a
  `required_artifacts` entry should carry, so a prompt naming a notebook "the deliverable" could
  plan it as `kind="report"` -- exactly the REPORT extension `run_python`/`write_file` now refuse
  to register (see the entry below). The prompt now states that `kind="report"` is only for
  Markdown, plain text, PDF, CSV or JSON, and that a `.ipynb` notebook is always `kind="code"`,
  produced by `run_notebook`.
- **`run_python` and `write_file` refuse to register a REPORT with an extension MIRA's A18
  control cannot audit.** An agent could declare `kind="report"` for any file `run_python`'s
  `output_artifacts` or `write_file` wrote — including a `.ipynb` notebook — and it would
  register successfully, only for A18 (`thymira.mira.checks.report`) to fail the whole Run after
  the fact with an unreadable `unsupported extension` finding. `validate_report_kind`
  (`thymira.tools.artifact_validation`) now rejects a REPORT extension outside Markdown, plain
  text, PDF, CSV or JSON at the tool call that would register it, so the producing agent gets an
  actionable error immediately instead of discovering the problem at audit time.
- **A delegated task's instruction now names a required artifact's declared media type.**
  `_delegation_instruction` already told an agent the exact name and `kind` a plan's
  `required_artifacts` entry expects; it now also states the media type when the plan declared
  one, so an agent that would otherwise write a differently named or typed file
  (`run_b345…`, `run_a829…`) has the full contract in its own instruction up front.
- **THY's halt message names the real cause, not just the missing deliverable.** When a Run ends
  with a required artifact missing, `run_thy`'s error now prefixes the plan's first `FAILED`
  task's own diagnosis (Docker unavailable, a storage permission error, `max_turns` exceeded, an
  unknown agent kind, ...) ahead of `"required artifacts missing: ..."`, instead of hiding it. A
  plan that completed cleanly but simply never produced the named artifact keeps the unprefixed
  message.
- **The sandboxed `run_python` gives matplotlib a writable config directory.** Both the container
  (`--read-only` rootfs) and local backends now point `MPLCONFIGDIR` at the same per-run private
  directory already used for `TMPDIR`/`TEMP`/`TMP`, fixing `mkdir -p failed for path
  /home/thymira/.config/matplotlib: [Errno 30] Read-only file system` in the Docker backend.
- **`export_pdf` requires `evidence_paths` for any report that cites a checkable number.**
  Citation checking used to be skipped entirely when the caller passed no `evidence_paths`, so a
  report with decimal/percentage citations rendered unchecked and MIRA's A18 blocked the Run only
  after the fact. A source that cites at least one such number now refuses to render, before any
  work is spent, unless `evidence_paths` names the metrics evidence for it; a source with no
  numeric claims is still skipped. The tool description, `evidence_paths` argument description,
  and the Coding/Experiment prompts say so.
- **THY's fallback Inspect/Plan/Summarize nodes stop detaching `RunUsage` from the caller.**
  Each returned `state.advance().model_dump()`, which recursively serialises non-`ThymiraModel`
  fields — including `state.usage`, the mutable, run-scoped `RunUsage` accumulator — into plain
  dicts, so LangGraph reconstructed a brand-new `RunUsage` for the next node instead of carrying
  the caller's instance forward. A run-wide `max_requests`/`max_tokens`/`max_cost_usd` ceiling
  breach charged the detached copy, leaving the caller's own `RunUsage` at zero. The three nodes
  now return `dict(...)` of the advanced state's field values instead.
- **`export_pdf` accepts as evidence only JSON artifacts registered as `kind="metrics"`.** A
  cited number backed by a JSON registered under any other kind is refused at export time,
  naming the kind and the fix, because MIRA's A18 counts numbers only from metrics artifacts:
  three Runs of 2026-09-14 passed the pre-flight check and were still blocked by A18 for the
  same citations. The `evidence_paths` description and the Experiment prompt now say so.

### Added
- **`export_pdf` can refuse a report before it renders, if a cited number has no evidence.** A
  new optional `evidence_paths` argument names the JSON files that back the Markdown source's
  decimal and percentage citations; when given, a citation that resolves to none of them stops
  the export immediately, naming the exact numbers, instead of letting the same problem surface
  only when MIRA's own audit blocks the finished Run. Off by default (empty `evidence_paths`
  skips the check); does not replace MIRA's independent A18 report-fidelity control. Reads each
  evidence file through the artifact store -- the same registered snapshot A18 checks -- so a
  workspace edit an agent forgot to re-register cannot pass this check either; found live when
  a report that passed this check was still blocked by A18 for citing exactly that kind of edit.
  The Coding and Experiment agent prompts now pass it. `experiment.md` also states
  `run_experiment`'s actual `code` contract and adds a rule against repeatedly re-inspecting the
  same broken file instead of rewriting it, both traced to real Runs.
- **The Experiment agent can finish a report bundled with training.** When a Run's plan
  reassigns a combined training-and-report task to the `experiment` agent (`_route_model_plan`,
  triggered whenever the prompt asks to train a model and the planner omitted a dedicated
  experiment task), the agent now has `write_file` and `export_pdf` to write and export the report
  it was asked for instead of exhausting its turn budget with no tool for that step. `max_turns`
  raised from 10 to 32 for the added work. Training still goes through `run_experiment`/MLflow.
  Its prompt also requires every number the report will cite to be saved in a registered
  JSON metrics artifact first, dataset facts and derived rates included, not only the model's
  own metrics -- found live when MIRA's audit blocked a real report for citing class-balance
  and threshold percentages that existed only in a tool's printed stdout. It also now says to
  split any hand-written training data before fitting an imputer, encoder, scaler or
  feature-selection criterion, and to transform (never re-fit) the test split with what was
  fitted on training only -- a real training script fit its scaler and imputer on the whole
  dataset before splitting it, which is exactly the class of train/test contamination this
  closes. `thymira-tools` gains `xgboost` and `seaborn` as direct dependencies so the sandbox
  image stops rejecting a model an agent tries by default and wasting a turn on the retry.
- **A `run_notebook` tool executes a workspace Jupyter notebook and registers the executed copy.**
  The Coding agent writes the `.ipynb` with `write_file`; `run_notebook` runs every cell in a
  fresh kernel through the Run's sandbox and publishes the source and the executed notebook, with
  each cell's outputs inline, as `kind="report"` Artifacts in one atomic batch. A notebook is code,
  so it is reviewed under `GOV-008` exactly like `run_python`; a failing cell stops the execution
  and registers nothing. New direct dependencies of `thymira-tools`: `nbformat`, `nbclient`,
  `ipykernel`, `jupyter-client`.
- **Tools that only write their own report no longer need a human.** A new `artifact_write` side
  effect marks the built-ins that persist a bounded report, metrics or tracker record inside the
  Run workspace and run no code of the model's (`profile_dataset`, `analyze_dataset`, `query_sql`,
  `run_statistics`, `compare_models`, `export_pdf`, the writing `mlflow_*` tools). The base policy
  passes them locally under the new `GOV-009`, while `GOV-008` now names `workspace_write` (code
  and arbitrary files: `run_python`, `run_experiment`, `write_file`, `edit_file`, git, worktrees,
  `inspect_model`, `audit_model`) and keeps reviewing it; any effect nobody named still reaches
  the reviewing default. The shipped base policy hash changes with this rule change.
- **An uncertain risk classification is reviewed once, before any tool runs.** When the
  classifier leaves the profile uncertain (unknown level or category, missing information, low
  confidence or a review flag), the `execution.start` decision now escalates to a human review
  with the same "Fail-safe escalation" reason the per-call path used, and the pending review's
  summary shows the classification (level, category, factors, missing information, confidence).
  After an authenticated human approves it, tool decisions are no longer escalated for that same
  uncertainty; rules that ask for a human (`GOV-008`, `CR-001`, run-wide review constraints) and
  every `BLOCK` decide exactly as before, and a rejection blocks the Run. A new profile version
  brings the review back.
- **The risk classifier now reads the profile it classifies.** The declared `decision_effect`,
  `autonomy` and `human_oversight` reach the classifier prompt as three separately framed,
  length-capped untrusted blocks (prompt version `risk-classifier-v2`), so the model can tell
  offline research apart from live decisioning. The deterministic overrides are unchanged: MIRA's
  inherent-risk floor, the sensitive-attribute factor and its review still apply whatever the
  model answers.
- **The risk classifier can no longer invent unanswerable missing information.** Core passes the
  classifier the nine interview fields (plus their downstream-context aliases) as the only names
  `missing_information` may keep; a name outside that set is recorded on the classifier's
  `agent.message` as an `unanswerable_concerns` entry instead of forcing a human review nobody can
  resolve. Locally observed absent fields are still re-added, so a real interview gap is never
  dropped.
- **Model routing can be changed live, from the console, with no restart.** A new `#/settings`
  view lets an operator set which model THY, MIRA and each sub-agent tier (frontier/standard/fast)
  call, backed by the existing durable settings API's `models` namespace. A save reaches the very
  next routed model call in the running API process and survives a restart; it never bypasses the
  Run's own frozen model-route allowlist, which still refuses a model this deployment does not
  permit exactly as it would for one set through the environment.
- **A `export_pdf` tool compiles a Markdown deliverable, and the images it embeds, into a PDF.**
  Reads a Markdown file already written to the workspace (`write_file`/`run_python` produce the
  text and plots first), renders it with a fixed report stylesheet, inlines every referenced
  workspace image as a base64 data URI, and always registers the result as a
  `kind="report"`/`media_type="application/pdf"` Artifact together with an exact Markdown audit
  sidecar in one atomic publication. The PDF carries same-batch lineage to that sidecar and a
  reproducible execution key. Available to the Coding agent
  alongside `write_file`/`run_python`/`read_file`; raw HTML/CSS resource loaders, non-local images
  and renderer URI callbacks fail closed, and image/HTML size budgets apply before rendering.
  New direct dependencies of `thymira-tools`: `markdown`, `xhtml2pdf`, `pypdf`, `pillow`.
- **A web console for Runs (`apps/web`, `thymira-web`).** A local browser client of the API lists
  Runs, creates one, answers its risk interview, and shows each Run's overview, plan, agents,
  pending and answered reviews (approve or reject the call a Run is parked on), tool calls,
  artifacts, MIRA audit, live event stream and model usage. It owns no Run state and imports no
  runtime member: the browser keeps the API token for the tab, and the console forwards that header
  unchanged, refusing foreign `Host` names and cross-origin state-changing requests.
- **The API serves a Run's artifacts.** `GET /runs/{run_id}/artifacts` lists every manifest entry,
  and `GET /runs/{run_id}/artifacts/{artifact_id}` returns one artifact's content after recomputing
  its sha256 against the manifest, as a redacted JSON projection (UTF-8 text or base64, up to
  2 MiB). Binary content that redaction would alter is withheld instead of being served corrupted.
- **Project inputs are editable through the API and the web console.** `GET`/`PUT /project/context`
  read and compare-and-set `.thymira/context.md`, refusing text that carries a redaction mask;
  `GET /project/datasets` and `PUT`/`PATCH`/`DELETE /project/datasets/{name}` upload a CSV or
  Parquet file, set its target column or stop declaring it. An upload is read with the dataset
  registration reader and declared in `config.yaml` only once it is readable, so every declared
  file becomes a registered source when the next Run starts. The console edits both beside the
  prompt and from each Run's Inputs tab, marks the Interview and Approvals tabs when a Run waits for
  an answer or a review, and offers "Run again" with the same prompt.
- The CLI now offers a read-only `thymira review RUN_ID` command that explains a pending human
  decision before `approve` or `reject`, while `status` points waiting Runs to that review.
- **The API composition now carries the durable lifecycle boundary and its own signing authority.**
  `build_default_deps` builds the local lifecycle repository behind an HMAC authority verifier and
  exposes an `AuthorityProofBuilder`, establishing a private 32-byte authority key below the state
  root (or reading an explicit `THYMIRA_AUTHORITY_SECRET`) exactly as it establishes its bearer
  token. The key is never derived from the bearer token and never reaches JSON, events or logs. The
  Run routes keep their current request semantics; the durable receipt contract they will answer
  with is available as `CreateRunResponse` and `EnqueueReceipt` but is not wired yet.
- CLI Run and lifecycle commands now observe durable receipts through correlated turn endings,
  repair and deduplicate reconnecting SSE history, and keep headless responses on stdout with
  progress on stderr.

### Changed
- **A long Run no longer slows down as its event log grows.** `JsonlEventLog.append` re-reads
  and re-verifies `events.jsonl` only when the file no longer ends where the writer left it
  (same length, same final line); otherwise the chain head kept in memory is the head on disk and
  an append costs one line. Readers extend their cached parse with the appended lines when the
  new bytes start with exactly the cached bytes, and chain only those lines onto the verified
  head, so the API event stream and every `events()` read stop re-parsing the whole log after
  each event. On yesterday's 78-minute reference Run (2016 events, 13.7 MB) an append measured
  0.4 s and a read after it 0.35 s; both are now under 10 ms and 60 ms, and the internal time
  between events no longer grows with the log. A rewrite of an earlier line is still refused by
  every reader and by `verify`, exactly as before. The post-routing model guard asks the role
  budget only when the policy declares a rule for that role, so a Run under the shipped policies
  records one `budget_within_limits` per model call instead of two.
- **Long Anthropic agent conversations now spend less input context without hiding evidence.**
  Native Anthropic requests explicitly enable ephemeral prompt caching before the effective
  request is recorded, and a superseded `write_file` body is omitted from later model turns only
  after another write to the exact path succeeds. The current body, failed replacements and every
  call/return pair remain visible; the append-only `tool.started` events retain the original full
  arguments.
- **Reading Runs is an order of magnitude faster.** Export redaction reads the credential
  environment once per call instead of once per string, and a parsed event log is reused while the
  sha256 of its bytes is unchanged (up to 256 logs and 64 MiB); any change to a log is parsed and
  verified afresh. On `examples/credit-risk`, `GET /runs?limit=50` fell from 21.5 s to 1.7 s and a
  1,000-event page from about 15 s to 0.25 s.
- When an approval leaves a Run waiting, the CLI now explains the pending-review reason and prints
  the exact `review`, `approve` and `reject` commands for the next decision.
- Approval, rejection, risk-interview answers, resume and cancel commands now print compact
  lifecycle results; the full prompt and run metadata remain available from `thymira status`.
- **Model routes are allowlisted per Session and denied by default.** Every Session snapshots
  the operator's `THYMIRA_ALLOWED_MODEL_ROUTES` (comma-separated exact model ids) as a frozen,
  hash-pinned `ModelRoutePolicy`; each Run copies it into `run.started`, publishes
  `model.route_policy_snapshotted`, and every provider seam checks the selected route against
  it after `model.selected` and before the call, recording `model.route_denied` on refusal.
  **Operators must set the variable**: unset, the allowlist is empty and every model call is
  denied (`.env.example` shows the shape). `scripts/real_e2e_smoke.py` now refuses to start
  without it and prints the configured model ids to list.

### Fixed
- **`CR-001` now fires.** `run_experiment` declares the `model_training` risk tag and
  `compare_models` declares `model_comparison`, the tags the credit-risk rule already matched, so
  training or comparing models on a profile with sensitive attributes is reviewed by a human under
  the rule's own GDPR art. 9 reason instead of only by the generic side-effect rule. The rule now
  reviews that call itself and no longer arms a run-wide review of every other tool call of a Run
  whose profile carries sensitive attributes.
- **The Coding agent's prompt now tells it not to re-list the workspace or re-create/re-read an
  already-confirmed file.** A real chart-generation run showed 9 of 18 approved tool calls were
  the model re-verifying its own already-successful prior work (`os.listdir`/`os.walk` four times,
  the same metrics JSON file created or re-read five times, no errors involved) — each one costing
  a full human-review round trip under `GOV-008` for zero new information. Prompt text only; does
  not change how many approvals genuinely new work needs.
- **A resumed step (after a human approves a paused tool call) now compacts an oversized prompt
  before dispatch, instead of only detecting the overflow and failing.** `GOV-008` requires human
  review for essentially any side-effecting tool call, so most steps in a real task park and
  resume repeatedly; each resume re-rendered the whole run's growing event-log history
  uncompacted, so a long, fully-gated task's resumed requests only ever grew until a downstream
  guard failed the step with `end_reason=context_budget`. Compaction now runs before a resumed
  request is measured, reusing the same tested machinery a fresh step already uses, and a resume
  still gets the full (untruncated) recovery checkpoint whenever it already fits — only once
  compaction actually has to run does the checkpoint projection get bounded, exactly like a
  non-resume call. This does not change how a resumed conversation's own already-accumulated
  tool-call history is handled: if that alone is too large, the step still fails closed exactly as
  before.
- **`thymira cancel` (and the web console's identical Cancel-run button) now recognises a
  delegation the run already settled on its own.** Two of `AgentRunner`'s five completion paths
  (a context-budget failure and a resume-history failure) omitted the delegation identity every
  other completion path stamps onto `agent.completed`, so `finalize_parked_invocation` could not
  match that completion back to its `agent.parked` checkpoint and refused to cancel with "a
  settlement without terminal completion." Both paths now carry the same identity; the settlement
  reconstruction itself is unchanged.
- **A long Coding-agent task under full human-review friction no longer starves before its
  deliverable is written.** `DEFAULT_CONTEXT_WINDOW` (128,000, applied uniformly regardless of the
  routed model's real window) and `CODING_AGENT_SPEC.max_turns` (24) were both found live to be
  reachable by a real report-compilation task whose every tool call needed
  `REQUIRE_HUMAN_REVIEW`, ending the Run with the required PDF never produced. The context ceiling
  is raised to 180,000 (still a real margin under the smallest configured tier's window) and
  `max_turns` to 40; the Coding agent's prompt also now tells it to proceed once a file's content
  is confirmed once, instead of re-reading a file it already verified.
- **A catalog change can no longer orphan a THY task while its tool call awaits review.** If the
  agent needed to resume was removed, the original parked task and delegation identity settle as
  failed exactly once and the approved call does not execute. Unsupported later tasks in the
  restored plan remain durable evidence and still reach MIRA and the findings Gate.
- **Billable structured-output failures now count before every retry or fallback.** The routed
  model and all seven direct structured-output call sites charge a returned response into Core's
  usage ledger before retrying, falling back or re-raising. Hard ceilings therefore see every
  provider attempt, while parse failures remain auditable without exposing returned field values
  through exception messages, chained causes, output-tool validation or MIRA wrapper tracebacks.
- **Direct interview and MIRA risk calls now have Run-scoped request evidence.** Ordinary and
  terminal-audit composition pass the canonical request ledger alongside the usage ledger; every
  prompt is credential-scrubbed before the provider and digest boundary, and a shared LiteLLM
  provider is copied before rebinding so independent operations neither form false retry chains
  nor redirect another Run's evidence.
- **THY context compaction now crosses every model governance boundary.** A real compaction runs
  the budget guards, records its own model selection, enforces the route policy and Gate, and
  charges both usage ledgers. Production lazily resolves the FAST model when compaction is needed,
  while durable response evidence contains only the safe validated projection and is recorded
  exactly once even when an injected LiteLLM provider already had a ledger binding.
- **THY's structured planner now exposes only agents and runtime skills the current Run can
  dispatch.** The same catalog-derived rosters drive both the prompt and a bounded JSON Schema, so
  a model can no longer select an unavailable specialist or invented skill and strand dependent
  analysis or PDF tasks. One invalid answer gets one correction; a repeated mismatch fails before
  the plan reaches the Policy Engine, and the ordinary valid path spends no additional model
  request.
- **The shipped credit-risk auditor can retrieve its mandatory regulation evidence.** The local
  corpus now includes integrity-hashed `CREDIT_RISK` summaries of the AI Act creditworthiness
  scope and the EU Consumer Credit Directive's data, explanation and review requirements
  (including its application date), while the agent uses a tested lexical scope query and
  distinguishes a descriptive Run from a model or lending decision. Empty results remain a hard
  audit failure.
- **MIRA's local regulation lookup no longer inherits uncertainty about the activity it audits.**
  Core classifies the fixed audit-evidence operation separately from the Run while still routing
  every call through the Tool Manager and Policy Engine. A read-only `search_regulation` can now
  satisfy a mandatory audit check after a sensitive-data Run; external effects remain blocked,
  local side effects remain review-gated, and the Run's recorded risk profile is unchanged.
- **Exported PDF reports now use portable list markers and keep plot sections together.**
  Markdown unordered lists render with visible ASCII hyphens instead of a `Symbol`-font glyph
  that some PDF viewers displayed as infinity, while plot headings and their introductory text
  stay on the same page as the following image when space runs out.
- **An active Run can no longer be dispatched twice through `Resume`.** Core refuses a resume
  while the event-backed composite state is already `active`, without writing an event or calling
  the dispatcher, and the web console offers `Resume` only for a genuinely `paused` state.
  Approval and interview continuations remain on their dedicated controls.
- **MIRA now repairs one skipped mandatory audit lookup before failing the Run.** When an audit
  model returns `final_result` without ever attempting a declared required tool such as
  `search_regulation`, the bounded agent protocol tells it to perform the exact missing lookup
  and gives it one corrective attempt. A denied, failed, empty, or framework-inapplicable lookup
  still fails closed, and ignoring the correction still produces the existing explicit required-
  tool failure instead of accepting an ungrounded audit.
- **A failed Run that already entered THY Execute no longer appears to have failed in planning.**
  Core now records `BEGIN_EXECUTION` at the actual Plan-to-Execute node boundary, before delegated
  work can fail or final deliverable validation can reject the pass. Inspect failures and
  policy-gated Plan halts remain in `planning`, and `RunController` remains the sole persistent
  lifecycle writer.
- **A prohibition can no longer reroute descriptive work into model experimentation.** The
  deterministic model-work guard now removes bounded `do not`/`don't` scopes and direct
  `without`/`no` model-work phrases before looking for positive training or evaluation intent.
  This fixes the live plotting run where `Do not ... train a model` overwrote THY's correct Coding
  plan with an Experiment task, while contrastive requests such as `do not use generic code, but
  train a model` remain forced through tracked experimentation.
- **A transient Windows sync-client lock no longer strands a workspace publication after the new
  tree is already live.** Journal transitions use the same bounded atomic-replace retry as other
  local state, so a brief `WinError 5` cannot leave a valid run in `published` recovery state or
  report its otherwise successful sandbox tool as failed.
- **A18 no longer treats official structured REPORT values as narrative claims.** Its numeric
  claim scanner now reads only compatible UTF-8 Markdown/plain reports and the Markdown source of
  a PDF. JSON and CSV REPORT artifacts remain validated evidence but their unrelated machine
  decimals no longer create false critical findings; dataset-profile JSON keeps its independent
  recomputation, and inconsistent extension/MIME declarations fail closed.
- **Failed experiment publication no longer exposes a model without its metrics.**
  `run_experiment` completes local experiment tracking first, then publishes the model and metrics
  through one heterogeneous ArtifactStore commit. A failure in tracking or either staged output
  leaves no new active Run artifact and no partial `artifact.created` evidence.
- **PDF reports can no longer bypass A18 because their bytes are not UTF-8 text.** A18 requires
  exactly one active `REPORT` Markdown source, independently re-hashes the source and PDF,
  recomputes a versioned execution key bound to both exact names and digests, and requires the
  authenticated successful `export_pdf` Tool Manager event for that ordered artifact pair. A
  plausible 64-hex key or unrelated completion no longer authenticates a PDF. A18 audits the
  source once and fails orphan PDFs, invalid sidecars and unreadable non-PDF reports instead of
  silently treating them as claim-free. Pre-fix PDFs must be exported again. Artifact citations
  may be root-relative or relative to the Markdown report directory, while paths escaping the
  artifact root fail closed.
- **Resolved interview reviews no longer remain visually pending.** Risk-interview status now
  recognizes an answered question-limit review, while the credit-risk project's stable context
  supplies shared activity facts without imposing one Run's goal on later Runs.
- **A18 accepts bounded grouped and scientific evidence values.** Valid report claims such as
  `7,879.5` and `4.19e-12` resolve against recorded metrics, while extreme exponents and non-finite
  values fail safely instead of consuming unbounded resources in the final audit.
- **A failed multi-file `run_python` publication no longer leaves a successful prefix behind.**
  Heterogeneous outputs now reach the ArtifactStore in one atomic batch with per-file kind, MIME
  and lineage; a failed member leaves zero new active artifacts and zero `artifact.created`
  events, while a derived artifact can point to an exact source sidecar from the same commit. If
  the manifest replacement committed before a later durability check reports an error, its exact
  durable match is returned as success instead of exposing artifacts from a nominally failed call.
- **Workspace publication refuses to use the live workspace as its incoming generation.** This
  closes a malformed no-op path that could otherwise delete the live tree while attempting to
  discard what should have been a separate staging directory.
- **`query_sql` now enforces its registered-dataset boundary before DuckDB execution.** Model SQL
  may read only the in-memory `dataset` table and CTEs or subqueries derived from it; table
  functions, filename replacement scans, unknown or catalog-qualified relations, and attempts to
  shadow the dataset fail closed. DuckDB external access remains disabled as a second barrier.
- **Complete activity profiles no longer create a synthetic deployment-context gap.** Core maps
  the interview's declared `autonomy` fact to the risk classifier's `deployment_context` key, and
  skips model-assisted context extraction when no interview fields are missing. This avoids an
  unnecessary review park and one no-op model request for already complete profiles.
- **A Run can no longer complete after silently missing an explicitly named deliverable.** A
  filename such as `german_credit_eda_report.pdf` now requires an exact artifact name and infers
  its expected MIME when the plan omits it; an unrelated JSON with the broad `REPORT` kind cannot
  satisfy it. `write_file` validates registered JSON and refuses binary suffixes, while
  `run_python` parses PDF bytes before publishing them. Extensionless semantic requirements such
  as `evaluation metrics` still resolve by typed kind.
- **Tool retries preserve the provider's conversation protocol.** Tool-scoped PydanticAI retry
  prompts are returned as `tool` messages with the original call id instead of becoming unrelated
  `user` turns that Anthropic rejects. Coding guidance also no longer recommends `edit_file` to an
  agent that is not allowed to call it.
- **Invalid tool calls now say which fields need repair without echoing their payload.** The Tool
  Manager returns a credential-scrubbed, bounded summary of schema locations and messages while
  omitting rejected inputs, validation context and documentation URLs; authorization and execution
  still never occur for an invalid call.
- **A missing, denied, failed, empty or framework-inapplicable required regulation lookup can no
  longer become a successful MIRA audit.** Credit-risk and EU AI Act specs declare
  `search_regulation` mandatory; only a task-scoped successful call with a complete canonical
  result whose non-empty match set is entirely applicable satisfies that evidence contract. EU AI
  Act requires only EU AI Act material, while credit-risk audits accept credit-risk or GDPR
  material. External citations also require an exact `source_id/location` and chunk digest from
  the current audit task's latest resolved canonical, applicable search with no failing exit code;
  each digest is recomputed from the recorded UTF-8 fragment, another task or a superseded lookup
  cannot lend its chunks to the finding, and merely having run a search cannot back an invented
  citation.
- **A reviewed later risk classification retires MIRA's stale synthetic uncertainty warning.**
  The final audit removes `MIRA-RISK-001` only when the current Core classification is linked to
  the exact immutable MIRA assessment and activity-profile version, classifies a real level and
  category, and the uncertainty review has an authenticated human approval scoped to that exact
  assessment/profile request.
- **Quota-backed Docker tools can update directories created by an earlier invocation on
  Windows.** The fixed helper and capability-dropped worker now use the same container identity on
  Docker Desktop, preventing staged directories from becoming unwritable after the first
  writeback while retaining the non-root identity on native POSIX hosts. An unchanged writeback
  is now committed without replacing the live workspace with an identical generation, avoiding
  needless Windows backup cleanup failures for read-only Python calls.
- **Risk interviews no longer spend the entire question budget repeating one unresolved fact.**
  Three model-rejected answers for the same field now trigger the existing explicit human-review
  route, while the twelve-question global bound still protects interviews spanning several fields.
- **MIRA A18 now accepts the producer's exact current dataset-schema contract.** The independent
  verifier recognizes the optional `source_path` field while still rejecting missing, extra or
  malformed schema fields, so valid CSV and Parquet profiles are evaluated instead of failing on
  contract drift.
- **MIRA A18 now interprets report numbers in their written units.** Signed values retain their
  sign, percentage-point claims resolve against fractional evidence, and Markdown section numbers
  and references are not misclassified as analytical claims. Decimal comparisons use the claim's
  exact half-step, avoiding binary/banker's-rounding false failures without widening the boundary.
  The Coding agent is also required to register every cited derived statistic, including base
  rates, in JSON metrics evidence before writing an analytical report.
- **The protection against replayed parallel tool batches now reaches the model provider.**
  Both `AgentRunner` and MIRA's audit-agent runner request one tool call per turn, and Thymira's
  custom PydanticAI `FunctionModel` adapter forwards that setting to LiteLLM. The provider contract
  records `parallel_tool_calls=false`, so the Anthropic request that prevents the split deferred
  batch responsible for the real A3 failure is actually dispatched on both execution paths.
- **The Coding agent no longer starves mid-report under full tool-call review friction.** When a
  Run's inherent-risk classification stays unresolved, every tool call requires human review, not
  only ones with real side effects — a real report-compilation task hit `agent exceeded max_turns`
  after six approval round trips, never reaching its own `write_file` call. `max_turns` raised
  from 8 to 24 (documented on the constant with the finding that motivated it).
- **Experiment deliverables now use typed, run-scoped evidence.** Semantic requirements such as
  "trained model" and "evaluation metrics" resolve against the artifacts actually produced by
  the run, completed experiment records keep the tool's recorded metrics, and local tracker
  publication uses validated bytes instead of a temporary staging path.
- **Model requests now use auditable experiments.** THY routes training and evaluation work to
  the Experiment agent, dataset schemas retain their declared workspace path for report
  provenance, and MIRA A18 accepts only registered source paths and recorded split evidence.
- **Runs now enforce their declared deliverables.** THY plans record exact required artifact paths,
  `run_python` can publish raw binary outputs such as models and plots, and a finished pass reports
  missing deliverables as an error so the Core cannot mark an incomplete run as `COMPLETED`.
- **Verified private Docker workspaces can now complete without an A19 confinement finding.**
  The quota-backed sandbox publishes `full` enforcement only after it has verified its private
  tmpfs volume, worker mount, bounded publication, and cleanup. The isolated Coding agent no
  longer offers Git diffing, because a Run workspace is deliberately not a Git repository.
- **Coding agents now receive each registered dataset's exact workspace path.** Logical dataset
  names and immutable artifact-store keys no longer compete with the project-declared file path
  in the runtime context, so `read_file` and `run_python` use the staged input (for example,
  `data/applications.csv`) rather than incorrectly treating `datasets/<name>.csv` as a local
  filesystem path.
- **Approving a risk-interview question-limit review now continues the Run instead of failing it.**
  The park recorded no resume target, so the approval re-entered the Gate — which has no decision
  to route on before the interview finishes — and the Run died with `run.failed`; it now re-enters
  the interview, which records the resolved review and moves on to a real governance decision.
  A review refused out of band (the governance reject route) followed by `thymira resume` now
  blocks the Run with the rejector's attribution instead of requesting the same review again.
- **MIRA's preflight can no longer strand a Run in an unrecoverable wait.** A fully-declared
  activity profile can still leave MIRA's inherent-risk preflight uncertain, and it durably parks
  the Run for more context — but the graph kept running past its own park in the same pass, and
  neither `resume` (which refused every information wait outright) nor `answer` (there is no
  risk-interview question this park ever creates) could ever clear the stale `waiting` state left
  behind. The graph now stops when preflight parks, matching every other conditional wait, and
  `resume` can clear exactly the information waits that carry no answerable interview question.
- **MIRA's preflight no longer retries an uncertain risk judgement.** `resume` previously
  re-attempted MIRA's own inherent-risk classification, but nothing about a retry changes the
  profile or the model's input, so a judgement landing just under `RISK_CONFIDENCE_THRESHOLD`
  could re-park the Run on the same unanswerable `wait_reason: information` indefinitely (found
  live against `examples/credit-risk`: three real resumes reproduced the identical park). The
  first uncertain judgement now asks a human to accept proceeding (`REQUIRE_HUMAN_REVIEW`,
  mirroring the risk interview's own question-limit escalation) instead of spending more tokens on
  unchanged calls, and an approval lets the Run continue past the uncertainty for good.
- **A `run_python` script that prints plain Unicode text no longer crashes the sandboxed child on
  Windows.** The local sandbox built the child's environment from nothing but `PATH`/`SYSTEMROOT`,
  so without `PYTHONIOENCODING`/`PYTHONUTF8` a child's own `print()` fell back to the Windows
  console codepage and raised `UnicodeEncodeError` on ordinary report characters (`≤`, `±`, curly
  quotes, accented text) before any output reached the tool. The child's stdio is now forced to
  UTF-8 unconditionally.
- **`thymira review`/`status`/any command rendering Run content no longer crashes on plain Unicode
  text on Windows.** The CLI's own stdout hit the same `UnicodeEncodeError` as the sandboxed child
  above whenever a MIRA question or tool-call description carried a character outside the console's
  codepage. `thymira` now forces UTF-8 on its own stdout/stderr at start-up.
- **A task can no longer lose a correctly-produced deliverable to a forgotten `write_file` `kind`
  argument.** A coding agent wrote a plan's declared report in full — `write_file` reported success
  — but the Run still failed with "required artifacts missing", because nothing before that point
  ever told the agent that an unregistered file is invisible to the Run's own completion check. A
  delegated task now has its plan-declared required deliverables (name and kind) spelled out in
  its own instruction, naming the exact `write_file`/`run_python.output_artifacts` argument that
  registers each one.
- **The API server can restart after a stopped or crashed process left its token file behind.**
  `resolve_process_credential` refused to start whenever `<state_root>/api-token` already existed,
  with no recovery path -- reproduced after `scripts/real_e2e_smoke.py` ran the server once. Clean
  shutdown (`thymira-api`'s normal exit or a `KeyboardInterrupt`) now removes the token the process
  minted, and an explicit `--replace-credential` flag atomically replaces a leftover token for the
  case a clean shutdown cannot cover (a crash). The default stays fail-closed: without the flag, an
  existing token still refuses the process exactly as before, so a second server still cannot
  accidentally invalidate a live one. The opt-in replacement now tolerates a bounded transient
  Windows scanner or sync-client sharing denial. The token value is never logged; only the file
  path is.
- **A failed process tool keeps the output it produced.** Validating a producer's result swapped in
  the typed failure value and, for every declared result model, also overwrote the envelope's
  `stdout` with the error text and blanked its `stderr`: a failed `run_python` reported
  `python execution failed` instead of the child's own output, and a failed `git_commit` lost the
  Git error altogether. `stdout`, `stderr` and `exit_code` are the process facts the sandbox
  recorded for that call, so they now survive validation unchanged; the error text keeps its own
  `error` field and the typed failure value, and what a model or an MCP client sees is unchanged
  because those projections already derive their fields from the canonical value.
- **Sandboxed tools create their workspace root again.** Moving runtime input staging into the
  sandbox backends also dropped the workspace directory the backends require, so `run_python`,
  `run_experiment`, `inspect_model` and `audit_model` failed with `sandbox workspace does not
  exist` and no confinement evidence whenever the workspace had not been created for them. The
  four tools create the empty root through one helper before calling the backend — staging the
  contents stays the backend's job, so a quota refusal still leaves that workspace empty — and
  `LocalSubprocessSandbox` no longer creates it for `danger_full_access` alone: every backend now
  enforces the same precondition in every mode.
- **Immutable artifact publication no longer spends a Run's Windows path budget.** A batch object
  is published at `.batches/<batch id>/<object name>` — 63 characters rather than 119 — so a store
  rooted in a deep directory stays under the 260-character limit instead of failing dataset
  registration with a `FileNotFoundError` on a staged object. The manifest still holds the
  authoritative full `sha256`, and a batch that would give two members one object is refused.
- **CLI commands work again against an API without the durable lifecycle owner.** `thymira run`,
  `resume`, `cancel`, `answer`, `approve` and `reject` read the publication envelope and the
  enqueue receipt when the API publishes them, and the canonical Run record — the settled snapshot
  — when it does not. A body that claims to be a receipt is still validated strictly, and
  `run --headless` still refuses a create response with no publication receipt.

- **MIRA A10 now resolves rewritten artifacts against the store's archived revisions.** A logical
  name announced more than once (a step parked twice rewrites `resume/<agent_id>.json`) no longer
  reports a false digest mismatch: each `artifact.created` digest is matched to the live entry or
  to the superseded revision the store kept under `.history`. An announcement no held revision
  backs still fails, and so does a live revision the log never announced (an overwrite through the
  store's own API with no new `artifact.created`). The acceptance demo's workspace oracle now lists
  the ship's two live resume bundles instead of main's four.

- **Delegated review steps now have an explicit in-flight lifecycle.** A parked invocation keeps its
  complete identity through resume, selects exactly one matching checkpoint, emits one terminal
  completion and settlement, and is finalized before Run cancellation or failure can leave an
  orphaned child.
- **Grep discovery now bounds the real producer and refuses credential-bearing source witnesses.**
  Binary chunk reads detect post-stat growth within the per-file and aggregate scan budgets, one
  monotonic deadline covers enumeration through persistence, and a known credential in a retained
  grep source fails before execution or duplicate evidence is written.

- **Local artifact manifest publication no longer fsyncs a read-only replaced handle on Windows.**
  Writable temporary bytes and the containing directory remain flushed before publication reports
  success.

- **Bounded artifact reads now resolve G1 logical names to immutable content URIs.** MIRA's A33
  reader can verify content-addressed discovery artifacts through the public store boundary.

- **The real-model E2E smoke now reports only verified governance outcomes.** It validates the
  canonical event hash chain and requires the terminal transition's exact Policy Engine decision
  to bind to the latest applicable preceding MIRA audit report, including its revision, terminal
  hash, findings and policy snapshot. A later audit invalidates an older terminal binding. Crashes
  and contradictory or malformed evidence fail closed, and the smoke refuses to terminate
  processes that own a requested port.
- **LLM proxy routing now fails closed.** The installed and routed provider paths share one
  runtime-declared endpoint resolver, forward only an explicitly allow-listed endpoint to LiteLLM,
  refuse explicit proxy requests when an injected provider cannot prove that it will honor the
  destination, and refuse direct dispatch when ambient OpenAI or mutable LiteLLM endpoint settings
  could silently redirect the request.
- **Model execution tools now bound and type-check every child sidecar.** Inspection reports,
  experiment metrics and prediction IPC must be bounded regular files with strict schemas, while
  model artifacts have an explicit 256 MiB staging ceiling. Replaced or linked sidecars fail
  closed, and post-child failures retain the sandbox evidence without publishing untrusted data.
- **Model-facing runtime and evidence text now uses nonce-bound untrusted-data frames.** Tool
  output, event history, dataset/project context, experiment results, compaction history, and
  MIRA evidence are escaped and bounded with complete delimiters; direct Run objectives remain
  user instructions. The rendered event history shares one frame however many events it holds, and
  context compaction subtracts that frame's fixed cost before it chooses what to keep, so a
  compacted prompt fits the model's window as it is actually sent rather than as raw text.
  user instructions.
- **Sandbox CPU settings now match Docker's nanocpu evidence precision.** Container and local
  constructors reject positive decimal limits with more than nine fractional digits before a
  child starts, so A30 never receives an unrepresentable requested ceiling.
- **Quota-backed executable tools now stage runtime inputs inside the observed boundary.**
  `run_python`, `run_experiment`, `inspect_model`, and `audit_model` pass immutable inputs to the
  quota helper instead of writing them to the host workspace first. Helper copies pin descriptors,
  enforce byte and deadline bounds, reject growth or ENOSPC, and remove runtime inputs before
  writeback; MIRA A32 recomputes request, accounting, and final/export agreement.

- **Configured container workspace quotas now use a private, observed tmpfs volume.** The trusted
  helper stages and exports a bounded portable tree, the worker receives only the execution-scoped
  named volume, and Tool Manager/MIRA record independent capacity, digest, publication, and
  cleanup evidence. Unsupported bind quotas remain fail-closed.

- **Sandbox resource configuration now fails closed at the execution boundary.** Output budgets and
  requested workspace quotas are runtime-owned and recorded in every resolved specification;
  quota requests refuse before a child or container starts because the current workspace bind mount
  has no quota boundary. Docker inspection now records `NanoCpus`, and MIRA A29/A30 reject malformed
  or contradictory resource evidence instead of treating it as enforcement.
- **Local attachment publication now serializes manifest transactions across processes.** A batch
  writer reloads the current manifest under a cross-process lock, readers retain immutable
  in-process snapshots, and staged objects remain recoverable when publication reports a fault.
  Single `save_bytes`/`save_text`/`save_json` writes now use the same content-addressed path, so
  dataset registration, experiment models and registered `write_file` outputs all use immutable
  objects. The API owns durable settings reads and compare-and-set writes with recursive
  credential-shaped field rejection.

- **Local persistence now replays Runs from their verified event logs and publishes attachment
  batches through one durable manifest snapshot.** User settings use namespace CAS with explicit
  environment/file secret references, while workspace registry removal leaves user files intact.
- **Model route authorization now fails closed and stays bound to each Run.** API and worker
  composition attach an explicit empty snapshot when the operator allowlist is unset, persist the
  selected snapshot in the Run creation record, and only permit later policy changes to narrow it.
  Production provider factories and injected provider seams reject an unavailable policy before
  constructing or invoking a gateway; the session snapshot is addressable in the same Run event
  chain without creating a second log.
- **Root orchestration can recover delegated settlements from the fenced owner inbox.** A typed
  `WorkResult` retains the child settlement and immutable queue/Task invocation source facts,
  including its claim attempt and token. `OrchestratorBoard.recover_settlement(s)` can idempotently
  rebuild a missing CAS/DAG projection after an owner settlement, while canonical selection reads
  only the verified owner record and board projection.

- **Delegation and MIRA critic inputs now keep their orchestration boundaries explicit.** A
  delegated context binds nested hub calls to the actual child identity, refusing caller-supplied
  sibling or authority-handoff labels before a task is created. MIRA gives every producer and its
  critic a deep-copied Run evidence snapshot captured before producer execution; producer events
  remain live-log grounding facts and cannot become critic evidence.

- **Goal and DAG board projections now recover from owner-settlement gaps.** Lifecycle work results
  retain the validated child settlement and immutable source binding, a restarted root can rebuild
  a result after repeated topology CAS conflicts, and MIRA replays owner-published board history
  independently before selecting runnable work.

- **Sandbox reap results now verify the final observed deadline.** A process that returns from
  `wait()` only after its shared cleanup bound has elapsed is recorded as `unbounded`, preserving
  the tree scope and exit facts without claiming quiescence.

- **Sandbox cleanup now has one global Windows reap deadline and durable timing evidence.** The
  `taskkill /T` tree request and the final child wait share the same bound, while a local or
  container backend records the observed reap duration beside that bound. MIRA A30 now treats a
  skipped reap's null timing as valid, rejects cleanup overruns, and rejects completed executions
  whose exit channel is absent, unvalidated, or incompatible with the backend.

- **Agent Notes validation now requires non-empty fixed sections and concrete alternatives.** The
  boilerplate re-litigation marker alone can no longer satisfy `just check-notes`.
- **MIRA A31 now rejects malformed and mismatched subagent settlements.** It validates the raw
  `SubagentResult`, joins lifecycle evidence by the complete invocation identity, and counts
  duplicate stopped settlements even when no start event exists.

- **MIRA now verifies the full delegated lifecycle matrix.** A keyed invocation must record one
  `agent.started`, at most one matching `agent.completed`, and its settlement in strict order;
  terminal stopped or abnormal children may omit completion after a crash, while orphaned,
  duplicated or reordered lifecycle facts fail closed. Every keyed parent message is checked
  against a preceding settlement, including earlier messages that a later rendering cannot hide.
  Starts must carry every persisted invocation identity field, including `objective`; the current
  completion record's intentional objective omission remains allowed only for `agent.completed`.

- **Repeated subagent instructions now settle under distinct invocation identities.** Settlement
  keys include the persisted task and agent invocation ids as well as the canonical parent
  linkage, so MIRA's A31 no longer merges separate plan tasks while auditing their messages.
- PostgreSQL lifecycle state now has a transactional repository backend. Publication, Session
  inverse links, initial work, authenticated control inbox records and broker outbox notifications
  commit together; Run ownership holds a dedicated advisory-lock connection and fences every
  mutation with a durable epoch. Expired dispatched work is settled as an embedded unknown result
  instead of being retried silently.
- Runtime THY and MIRA skill catalogs now load from configured precedence layers per Run,
  select bodies by name under a bounded UTF-8 projection, and publish verifiable source manifests.

### Fixed
- Parallel THY tasks with runtime skills now keep each selection on the canonical Run event
  chain before provider dispatch, so independent request replay preserves the exact event identity.

- Runtime catalog manifest verification now binds selected projection digests to the append-only
  `model.selected` event and immutable selection/dispatch identities plus durable provider-request
  associations, so replacing a selected export with a valid catalog-only export is rejected by the
  independent MIRA consumer. Selected states use versioned exports so interleaved THY and MIRA
  selections remain separately verifiable.
- **Cancelled deferred tool calls now produce a canonical aborted-before-dispatch result.** The
  original runtime ticket and provider call id are retained, no tool or provider is invoked, and
  the saved resume transcript records the matching interrupted return exactly once. Cancellation
  requires the exact run, agent and task identity plus a hash-verified manager completion before
  it can alter the parked transcript.

- **Typed tool projections now stay aligned with durable evidence.** Bare list, tuple and set
  result fields are rejected at registration, producer stderr outside a declared typed value is
  withheld from model and MCP surfaces, and child exit code 124 is no longer mistaken for a
  sandbox timeout without an explicit deadline fact.

- **Workspace discovery now fails closed when its complete evidence cannot be persisted.**
  `glob`, `grep` and `list_files` retain a bounded recovery rendering but return a failed tool
  result with `FS_DISCOVERY_PERSISTENCE`; successful calls persist the full ordered list first.
  The manager also records an independent execution-time source witness and refuses a call when
  that source changes during execution. Witness enumeration errors now fail the call instead of
  dropping an inaccessible subtree, and MIRA recomputes glob, grep and `list_files` projections
  from retained source facts. Witness hashing is streamed with bounded source, content, match and
  serialized-artifact budgets, and MIRA reads those artifacts through a bounded reader. File-tool
  output limits now include the caller-facing footer and `list_files` bounds its JSON envelope.

- **Discovery witnesses now enforce their bounds during every phase.** The manager hashes and
  retains files incrementally against the remaining aggregate budget, checks a monotonic deadline
  during traversal, reads and projection, uses a real timeout for producer and MIRA regular
  expressions, and serializes witness fields incrementally before accepting the 32-MiB artifact
  limit. A file that grows after metadata inspection therefore fails before `tool.started`.

- **Effectful tool rationales now reject whitespace-only text and normalize accepted descriptions.**
  The shared argument contract collapses repeated whitespace before recording the explanatory
  metadata, while the exact authorization intent remains unchanged.

- **MIRA no longer crashes a Run when an audit report cites an artifact larger than the bounded
  evidence excerpt limit.** The audit agent receives a bounded unavailable-evidence marker, so
  the control can report that the claim could not be verified without turning the whole Run into
  an infrastructure failure. THY also no longer accepts a completed task after an unresolved
  failed tool execution; task-scoped tool evidence preserves valid retries and non-process tools.

- Capability policy rules now match declared local side effects as well as external effects.
  Read-only tools may still pass automatically, while tools that write workspace or report
  artifacts require human review before execution (bug-hunt C8).

- Checkpoint storage keys on the composite `(thread_id, checkpoint_ns)` identity in
  `StateCheckpointer` and both `CheckpointRepository` backends, so two subgraphs sharing a Run
  thread no longer overwrite each other's checkpoints and pending writes (DSH record F8.7). Local
  checkpoints move to `checkpoints/<thread_id>/<namespace digest>.bin`; the PostgreSQL
  `checkpoints` table gains a composite primary key. No migration or shim (ADR-0013 decision 6):
  local checkpoints written under the old layout are not read, and an already-migrated PostgreSQL
  database must be recreated rather than upgraded in place.

- **`LiteLLMProvider` now bounds every call with a request timeout (bug-hunt H10).** A bare
  `litellm.completion()` call had no `timeout`, so a stalled connection could hang a Run
  indefinitely instead of failing into the existing `ModelRetry` seam. Configurable via
  `THYMIRA_MODEL_TIMEOUT_S` (default 120s); deliberately does not add `num_retries` -- the
  module's own retry-free design stays, a stalled call now just fails fast instead of never
  returning.

- **Project context now reaches THY planning and delegated-agent prompts (bug-hunt H4).**
  `.thymira/context.md` is loaded into the plan prompt and each delegated step's runtime-context
  snapshot, with the latter bounded to 4,000 characters.

- **A tool call awaiting review now resumes its original agent conversation.**
  Once every deferred decision has a human answer, THY restores the saved PydanticAI messages and
  replays the original call through the Tool Manager, preserving one-shot approval enforcement.
  The parked bundle also records and verifies the original model choice, exact ticketed sandbox
  mode, task/checkpoint identity and continuation association, so changed routing or tool
  configuration cannot silently authorize a different resume. A carried review that arrives
  without its durable recovery context now becomes a failed, ticket-attributed task without
  starting a replacement model call, including when a default graph is configured for parallel
  dispatch.
  The checkpoint-aware history is measured at the actual provider boundary, and an over-cap or
  ambiguous parked history fails closed before a tool or model call.

- **THY's planner advertises only agents installed in its catalog, and `query_sql` documents its
  fixed `dataset` table name.** Plans can no longer select an unregistered agent, and data agents
  no longer need to guess the SQL table name.

- **MIRA's adversarial finding verifier now receives authoritative tool lifecycle evidence.**
  LOG_ONLY tool events remain outside THY's global prompt surface, while MIRA can still verify a
  finding against the tool execution, denial, policy and artifact events that support it
  (bug-hunt C11).

- **THY records an unsupported planned agent as a failed task and still reaches MIRA.** The
  model-visible failure names the requested kind and the catalog registered for that Run, while
  parallel Execute preserves each result by plan occurrence even when plan-local task IDs repeat.

### Added
- **Lifecycle controls now use durable next-turn and next-step lanes.** Owner-fenced claims transfer
  lanes atomically, expire and requeue safely, and preserve ordered inputs. A live model-step hook
  applies steering before every provider request, including tool and retry turns; a rejected first
  claim closes a recorded zero-step turn with its cause.

- **Provider requests now have a durable, replayable ledger.** Effective model settings,
  schemas, message surfaces, retry identities, response provenance and safe reasoning metadata are
  captured before dispatch; MIRA independently rebuilds and checks each gateway request. Provider
  response records now carry explicit terminal outcomes and proven chunk counts, including durable
  timeout, cancellation, provider-error and structured-parse failure evidence.
- **Tool Manager sandbox enforcement now has an actual mode matrix.** Real child probes cover
  read-only, workspace-write and explicit development danger-full-access requests through a
  human-approved capability, with a fresh JSONL replay and independent MIRA checks. Unsupported
  local confinement and an unavailable container image return honest `UNUSABLE` tool results
  without a host fallback.
- **MIRA now registers and replays owned observable relationships.** Core observes relevant facts
  after durable append, while final audits compare the control with an independent relationship
  observer and expose stable failure codes. The package inventory is machine checked and names
  pending controls and scoped absences explicitly; malformed observer results remain attributed,
  A30's independent resource projection covers memory and filesystem facts, and an unknown
  observed process limit fails when a container requested one.
- **Local lifecycle publication now stages Runs privately and assigns one durable owner per Run.**
  A committed publication links its Session only after Run materialisation, persists initial work
  and outbox notifications before broker delivery, and fences canonical writes with a real OS lock
  and monotonic epoch. Durable controls require a configured verifier over their exact authority
  envelope. The owner loop now consumes notifications and all closed control kinds through bounded
  owner callbacks, with typed work results embedded in the authoritative work record for replay.
  Creation idempotency keys and their exact request digests are stored in the private publication
  manifest, so a retry after process restart returns the original durable receipt or a conflict.
  Distributed PostgreSQL lifecycle configuration fails closed until its advisory-lock
  implementation is available.
- **Non-successful owner turns now close through the canonical MIRA audit flow.** Failed,
  cancelled, host-paused and recovered turns persist a closed `turn.ended` event before MIRA
  records its final evidence; settled work is repaired after a crash without re-executing it.
  Host pauses retain the authenticated initiator and an explicit pre-dispatch abort fact.

- **Terminal MIRA evidence is now scoped to its exact owner exit.** Recovery no longer treats an
  unrelated `audit.completed` event as proof for a settled work item; completion carries the
  work, turn, closed reason, outcome and result digest needed for safe replay.
- **Tool calls now return one validated typed result across the manager, model bridge, MCP and
  API.** Every registered tool declares an output schema; malformed results fail closed with a
  recorded typed error, and clients project the canonical value with its source facts and bounds.

- **Tool completions now persist a discriminated success or failure envelope.** Failed values can
  be reconstructed from the registry, post-start bookkeeping faults replace stale success values,
  SQL rows use a closed JSON scalar schema, and process timeouts record `TOOL_TIMEOUT`, their
  budget and whether the child was aborted.
- **THY now recovers overflowing context through a durable source checkpoint.** Compaction keeps
  tool call/result/approval groups together, emits deterministic head/marker/tail accounting, and
  records exactly eight revisioned checkpoint sections. The running graph retries only after an
  independently measured prompt reduction and feeds the recovered checkpoint into the next agent
  request.
- **Provider requests now have a durable, replayable ledger.** Effective model settings,
  schemas, message surfaces, retry identities, response provenance and safe reasoning metadata are
  captured before dispatch; MIRA independently rebuilds and checks each gateway request.

- **Sessions now snapshot an immutable model-route allowlist.** Explicit operator routes carry a
  version, authority and sha256; route selection stays code-owned, while every THY/MIRA, risk and
  compaction provider seam refuses an unlisted route before dispatch and records a separate stable
  denial event.

- **`thymira-tools` now ships pandas, matplotlib, mlflow and statsmodels (bug-hunt C4).**
  `run_python` shares this interpreter, so a coding step can now actually do the analysis a
  data-science run asks for instead of crashing on `ModuleNotFoundError` for the libraries the
  project's own name promises. The runtime-context snapshot every step receives now reports
  exactly which known data-science libraries are importable (and names the ones that are not),
  resolved live from `importlib.metadata` rather than copied from `pyproject.toml`, so a step no
  longer has to discover the environment by crashing.

- **THY now gives each Run an isolated scratch workspace and stages declared datasets into it.**
  Coding tools can access raw datasets at their declared paths without sharing output files with
  another Run against the same project.

- **Data and experiment agents can reach their registered analysis and model tools.**
  Their catalogued tool schemas now include the applicable SQL, MLflow, inspection, comparison and
  audit operations.

### Changed
- Risk interviews now seed every activity-profile fact, including purpose, as `undeclared`; before
  the first question, one bounded model call fills the facts that the Run prompt and
  `.thymira/context.md` settle with certainty, and only the remaining fields are asked, with
  model-phrased questions, static fallbacks, bounded sufficiency judgments and directed follow-ups
  under the existing question limit. Context-derived and live-human answers are distinguishable in
  the append-only event log.
- MIRA preflight can use its own routed `Role.MIRA` classification judgement over the complete
  activity profile and reviewed risk taxonomy; uncertain judgements remain `UNKNOWN` and request
  human context through the policy-gated control plane.
- Approval persistence and every approval replay fold now require authenticated human provenance;
  declared human identities remain pending and cannot authorize execution or lifecycle transitions.
- **Breaking:** The local API now requires a bearer credential for every served operation except its
  fixed, stateless `GET /healthz` liveness probe. The server mints a per-process token (or accepts
  `THYMIRA_API_TOKEN`), the CLI sends it on ordinary and streaming requests, route verification
  covers mounted, WebSocket and opaque ASGI routes, and approval identities come from the
  authenticated principal rather than caller-supplied actor labels.
- **The canonical Run log now preserves model-visible values while exported and traced copies are
  redacted fail-closed.** Known credential values are scrubbed before provider/process/event
  exposure, local Run storage records verified restrictive OS permissions, API/SSE and file
  exports redact keys and values, and MIRA findings keep evidence references and computed facts
  without copying raw personal data (ADR-0014; DSH G.1/F1.1 advanced).
- **Event-log readers now fail closed on format drift.** Every event carries the single supported
  global format version; missing or malformed versions and unknown event types are refused before
  replay, append or consumer dispatch, with no pre-1.0 compatibility shim (DSH record F1.2).
  PostgreSQL append also verifies the complete persisted prefix under its transaction-scoped run
  lock before inserting a new row, so an invalid earlier event cannot be hidden by a valid head.

- **Effectful built-in calls now require a bounded description.** The fifteen registered dataset,
  model, Git, worktree, MLflow, SQL and statistics operations require a caller-supplied rationale
  in their argument schema; the production inventory test fails if a new effectful registration
  omits it. The description remains explanatory metadata and does not alter the exact tool intent
  used for authorization.

- A human's approval of a tool call now carries a recorded scope (the Run, the exact intent, an
  absolute deadline of 24 hours by default, and the delegation depth) and closes fail-closed: it
  authorizes nothing after the Run is cancelled, blocked, failed or completed, after the THY pass
  that raised it ends, after its deadline, from another delegation depth, or for a resumed call
  with different arguments. A policy `BLOCK` is checked before any human answer is read. Every
  denial records a closed ticket outcome (`allowed_once`, `rejected`, `cancelled`, `unavailable`,
  `pending`) and every start records the scope it spent; MIRA control A3 recomputes the scope
  from the log, and a pending approval on a Run that has already ended can no longer be answered
  (DSH record F5.1 and F5.4 advanced, nothing closed).
- **Breaking:** the sandbox backend is selected by runtime configuration at the two composition
  roots (the API dependencies and the Run worker) instead of always being the refusing local
  backend: `THYMIRA_SANDBOX_BACKEND` defaults to `container` (`thymira:dev`, tuned by
  `THYMIRA_SANDBOX_IMAGE`, `_MEMORY`, `_CPUS`, `_PIDS_LIMIT`), and `local` is accepted only as
  the explicit development escape hatch together with `THYMIRA_SANDBOX_MODE=danger_full_access`.
  An unknown backend, mode or argv-unsafe value refuses to start. Every sandbox execution records
  its resolved execution specification (backend, image, workspace mount, network, limits,
  forwarded and excluded environment names, unenforced controls) on the tool result and the
  `tool.completed` event; credential-shaped and interpreter-bootstrap names (`PATH`,
  `PYTHONPATH`, `PYTHONUSERBASE`, `LD_PRELOAD`, `*_PROXY`, ...) never reach a child. A container
  cleanup failure is now a separate recorded fact instead of rewriting a confirmed exit outcome.
  MIRA control A29 independently recomputes the recorded specification (DSH record F6.2 and
  F6.6 closed; F6.1, F6.5 and F6.7 advanced).
- Bounded sandbox executions now record termination and cleanup evidence from the runtime's own
  clock, process wait or container state. Slow container removal no longer counts against the
  child deadline, failed Windows tree termination is recorded as direct-child fallback, and MIRA
  control A30 rejects missing, malformed or contradictory termination evidence.
- **Breaking:** a project `.env` can no longer set an interpreter-bootstrap, proxy or
  `THYMIRA_SANDBOX_*` variable: the API refuses to start and names the variable, the CLI skips
  it, and `compose.yaml` forwards an explicit list of variables instead of the whole file.
- Git status, diff, log and commit support standalone repositories through the injected container
  sandbox; the runtime image now includes Git. All Git tools declare code execution, including
  repository hooks and filters. Confined worktree operations refuse before staging host files;
  explicit local development worktrees retain validated, workspace-relative paths.
- Git commits interpret explicit paths literally and exclude unrelated staged changes, including
  staging left by an earlier failed commit, while preserving that staging for its owner.
- Sandbox subprocess output now has a shared eight-MiB stdout/stderr capture budget, configurable
  by runtime code. Exceeding it stops execution with a named failure and bounded evidence before
  Tool Manager spilling; Docker containers also disable separate daemon log storage.
- **Breaking:** model audit now deserializes models and runs predictions through the injected
  sandbox, declares code execution and workspace writes, and binds approvals to its requested
  mode. Unsupported local and read-only requests refuse execution. The runtime validates a
  bounded prediction response before calculating audit metrics; it never loads the model itself.
- **Breaking:** `ContainerSandbox` uses the locally built `thymira:dev` image and refuses
  implicit pulls. Confirmed child execution reports `partial`; failed creation, indeterminate
  execution and cleanup failures report `unusable`. The unsupported `filesystem_limit` option
  and its false `full` claim are removed. Python execution, default training and model inspection
  now use portable workspace paths; read-only requests refuse before staging files. Real Docker
  tests preserve the A19 finding and verify the default training's A23 evidence.
- **Breaking:** the local subprocess backend now refuses `read_only` and `workspace_write`
  requests before launching a process (exit 125, `unusable` enforcement). Local execution requires
  runtime-owned `danger_full_access`, declares host filesystem/network effects, and still needs
  policy authorization. Default API and worker registries therefore refuse local subprocess
  tools; the production demo reaches a critical A19 finding and a blocked Run. Human approvals
  bind to the requested sandbox mode, and MIRA detects contradictory execution modes. THY keeps
  unscored experiment attempts available for audit instead of crashing during model comparison.

### Added
- `POST /runs/{run_id}/cancel` and `thymira cancel` abort a Run through the `RunController`
  (idempotent on a terminal Run); a delegated sub-agent now runs on a tool context of its own
  (its own agent, task and delegation depth) rather than its parent's.
- **A human's answer to a tool call is a one-shot ticket for exactly that call, and the Run waits
  for it.** When the Policy Engine escalates a tool call to `REQUIRE_HUMAN_REVIEW`, the agent's
  step ends cleanly (`agent.completed` with `status: PENDING`, `end_reason: awaiting_approval`;
  the started/completed pair MIRA's A9 reads stays paired), THY stops before Summarize, and the
  Run parks before MIRA. `thymira approve` / `reject` (or the API) resumes it into THY: the seeded
  pass re-plans nothing and re-runs only the task that asked; the Tool Manager recognises the call
  by `tool_intent_sha256` — the tool plus its validated, redacted arguments minus the model's
  `description` — carried on `human.approval_requested`, `tool.denied` and `tool.started`,
  reconstructs the human's `Approval` from their own event and re-verifies it through
  `allows_execution`; the answer is spent by the one `tool.started` it authorizes. A rejection is
  final for that call in that Run and is a denial the agent adapts to; an automatic (test
  approver) answer authorizes nothing. `human.approval_requested` names the tool and its
  arguments, and `thymira approve` shows them. Known limitations: a step that defers several tool
  calls at once is answered one review per call and the earlier requests stay unanswered until
  re-asked (A7 reports them at close); a Run parked on a tool review whose answer arrives out of
  band while a sibling fan-out review is still unanswered keeps answering 409 to `resume` until
  that sibling is answered through the governance route; the composed proof's production-factory
  scenario audits FAILED on A15 (no risk interview: the unclassified profile that escalates the
  call is also what A15 reports); `usage.charge_tool` charges the deferred attempt and again on
  the resumed pass.
- **MIRA's tool controls (A3, A6) recompute the human's one-shot ticket instead of trusting the
  id an event names.** A3 (authorization before execution) accepts a `tool.started` only when its
  own decision, or — naming another call's — a ticket that `ToolAuthorizationLedger` recomputes
  exactly as the Tool Manager enforces it: same call and tool, human answers pooled per ticket,
  the first ticketed request wins, any human rejection final, one execution per approval, an
  automatic answer authorizes nothing. A6 (denials explained by decisions) recomputes the same
  refusal from the recorded `ExecutionConstraints`, a non-allowing run-level decision such as a
  budget refusal, or a rejected ticket, using the refusal vocabulary shared between the Tool
  Manager and MIRA (`thymira.schemas.tool_refusals`) rather than trusting a denial's own claimed
  decision id.
- Harness basics 1 (ADR-0013): every sub-agent's system prompt opens with a persona line
  (agent, model, working directory) and per-tool guidance; a runtime-context snapshot
  (workspace, sandbox mode, registered datasets, artifacts) precedes every step as a
  superseding, logged, model-visible message; `edit_file`, `glob` and `grep` tools; `read_file`
  paging; a mandatory 5-10 word `description` on `run_python`, `write_file`, `edit_file` and
  `run_experiment`; tool results rendered with `[exit code: N]` last; and a recorded acceptance
  Run whose prompts, tool schemas and resulting artifacts are committed sidecars.
- **A project declares its datasets, and THY registers them at intake.** `ProjectConfig.datasets`
  (Contract 0.8: `name`, a `path` relative to and contained in the project directory, an optional
  `target`) lists the data a Run works on; the Inspect node registers each declared dataset into
  the Run's artifact store before any agent runs — idempotently, so a resumed Run re-enters
  Inspect safely — records them, with their declared `target`, on `ThyState.datasets` (a
  `RegisteredDataset` each) so the planner is told what exists and what it predicts, and halts
  the graph before Plan when a declared dataset is missing or unreadable (a configuration error
  is a pre-Execute halt, exactly like a rejected plan). A config declaring two datasets under
  one name is rejected, and a declared file over `MAX_DATASET_BYTES` (256 MiB) is refused,
  because intake runs before any policy. Registration is a runtime intake step outside
  the Tool Manager, like Summarize's report: the Policy Engine decides what agents may do with the
  data, not whether the project may declare it. `examples/credit-risk` declares `german_credit`
  at `data/applications.csv`.
- `thymira.tools.schema_artifact_name(name)` — the one place a dataset's schema artifact name is
  built.

### Fixed
- `run_experiment`'s built-in baseline now records validated reproducibility evidence on
  `model.trained`: the seed actually used by the built-in classifier, the child process's
  installed library versions, effective classifier arguments and split configuration, and
  observed thread-pool facts covering fit and predict, plus the generated training-script SHA-256.
  A normal default run records a single-thread pool; valid parallel or unavailable observations
  remain evidence for MIRA to fail A23. MIRA validates metadata shapes, rejects contradictory
  seed and thread declarations, preserves valid aliases, and does not treat `deterministic: true`
  alone as proof; custom code retains the A23 missing-metadata finding.
- MIRA independently verifies dataset profile reports against the registered CSV or Parquet
  source, including the declared row and column selection. Correct profiles no longer depend
  on unrelated experiment metrics; altered facts and missing or changed source evidence remain
  A18 findings. Older profiles without the source declaration must be regenerated.
- Runs under an execution constraint requiring human review can continue through separate
  one-shot tool approvals after their start is approved. The inherited review requirement
  reaches every capability decision, other restrictions remain enforced, and MIRA independently
  verifies the per-call evidence. The start summary explains the additional approvals.
- A human review required by `execution.start` now parks the Run before THY. Approval resumes
  the preserved execution decision after a restart, while rejection or a direct policy `BLOCK`
  terminates the Run before execution. Synchronous answers follow the same lifecycle. Tool
  permissions and execution constraints remain enforced; a start approval does not waive the
  Tool Manager's separate `requires_human_review` restriction. The approval request explains
  this restriction before the human answers.
- Risk-interview answers resume the interview itself, so every missing activity fact is collected
  before classification or tool execution, including when the dispatcher is rebuilt between answers.
- `run_experiment` returns the actual model-artifact and tracker-run identifiers with its metrics,
  allowing the Experiment agent to report the recorded lineage without inventing identifiers.
- A tool-call answer during authorized post-audit rework resumes the interrupted execution.
  The continuation verifies the recorded reopen, original policy and graph, and current artifact
  integrity without treating the earlier audit as current; the resumed graph must audit again.
- MIRA independently recomputes tool approval tickets from recorded tool names and validated
  arguments. Copied or malformed ticket evidence cannot authorize an execution or explain a
  denial, including a denial that reuses the reviewed call's subject with different arguments.
- The CLI reports a tool-call approval or rejection separately from the resulting Run status;
  rejecting one tool call no longer announces that the whole Run was rejected.
- **`run.failed` carries the cause of an inline execution or resume failure**, not only the fixed
  string "inline execution failed" (bug-hunt C2).
- **The governance approvals route refuses an anonymous approval.** `POST /runs/{id}/approvals
  /{decision_id}/approve` and `.../reject` used to record `Actor.system()` for a call with no
  principal, no `X-Thymira-Actor` header and no body `actor` — indistinguishable on the
  append-only log from an automated approval; they now return 401 `actor_required`, exactly as
  the run-lifecycle `/runs/{id}/approve` route already did (bug-hunt residual of C6).
- **A non-bool answer from an injected approver is no answer.** The Gate coerced whatever a
  synchronous approver returned with `bool()`, so `"no"` became an approval; now a non-bool
  answer writes no `human.approval` (the request stays pending for a real human) and
  `resolve_pending_approval` raises the `ValueError` the API maps to 409 — never a coerced yes,
  never a fabricated rejection.
- **Any crash at the execution boundary becomes a recorded `run.failed`.** `InlineDispatcher`
  caught only `OSError`, `RuntimeError` and `ValueError`, so a defect raising anything else
  escaped on submit or on resume and left the Run RUNNING forever with the human's approval
  already spent; every `Exception` is now turned into the recorded failure, cause included.
- **A Run's usage survives a park.** The graph factory built a fresh `UsageLedger` on every
  rebuild, so after each human review the Policy Engine's budget decisions saw a Run that had
  spent nothing; `UsageLedgerRegistry` keeps one ledger per Run across the graphs built for it
  (process-scoped: a resume in another process starts a fresh ledger).
- **`tool.started` records the validated arguments**, the same dict `human.approval_requested`
  and `tool.denied` record and the ticket is computed from; the raw request stays on the
  recorded `ToolCall`.
- Malformed human-approval metadata no longer crashes tool execution, and an unresolvable decision
  id requires a fresh Gate decision instead of raising; a budget denial records the guard's own
  decision, or no decision when none was provided, instead of misattributing the allowing
  capability decision; MIRA explains a zero-call limit and a constraint refusal for tool names
  containing quotes or backslashes.
- **`inspect_model` reports a pipeline through its final estimator.** `class_name`, `parameters`,
  `coefficients` and `feature_importances` describe the final estimator of a persisted `Pipeline`,
  and the payload gains `pipeline_steps` and `input_signature.encoded_feature_names`, so the
  coefficient evidence of the default baseline survives its encoder; the tool's model-facing
  description says so.
- **The shipped demo dataset registers as shipped.** `data/german_credit.csv` lost the two rows
  that were missing a field (lines 391 and 789; 998 rows remain), and `register_dataset` now
  refuses a ragged CSV with the 1-based line numbers of the offending rows instead of polars'
  raw error. The four test copies of the "drop the ragged rows" workaround are gone.
- **`run_experiment`'s default baseline handles categorical columns.** The default training
  script is a persisted scikit-learn `Pipeline`: numeric-ness comes from the registered dataset's
  schema — its polars dtypes, where booleans count as numeric too — not a runtime guess; blanks
  are imputed (the median for numeric columns, the constant `"missing"` for categorical ones); the
  numeric branch is standardized (`StandardScaler`) inside the persisted pipeline; and every other
  column is one-hot encoded, labels stay raw — so `audit_model` audits the artifact unchanged with
  the exact same training-time encoding, and the one-call baseline runs end to end on the demo
  dataset. Callers that passed their own `code` to work around `float(row[name])` no longer need
  to.
- **An unreadable runtime context no longer aborts the step.** When the snapshot cannot be
  rendered (an unreadable store, a corrupt dataset schema), the runner records the reason as the
  model-visible snapshot itself and the step runs; previously the exception escaped before
  `agent.started` and took the Run down with it.
- **`audit_model` no longer drops rows with a missing protected value silently**: they are
  excluded from subgroup evidence and counted in `subgroups_excluded_missing_protected`.
- The acceptance Run (`tests/thymira/test_acceptance_demo_run.py`) now registers its dataset
  through Inspect, trains the default baseline, reports the metrics the tool returned (a
  callable `ScriptedProvider` turn) and pins the real accuracy in `metrics.expected.json`.
- **A failed task no longer hides a Run from MIRA.** Execute used to route any task failure
  straight to `END`, skipping Summarize and the audit entirely; `_route_after_execute` now always
  reaches Summarize (which already degrades cleanly with no experiments), so a failure is evidence
  MIRA and the Policy Engine can weigh instead of a reason to discard everything that ran before
  it (bug-hunt C1). `ThyState.error` is reserved for a genuine pre-Execute halt (a Gate-rejected
  plan or a failed dataset intake) from here on.
- **A crash on resume no longer leaves a Run a zombie with no terminal event.**
  `InlineDispatcher.resume` had no exception handling at all, unlike `submit`; a `ThyExecutionError`
  or any other structural failure during a risk-interview answer, an approval resume, or a plain
  `resume` call propagated as a raw HTTP 500 while `run.json` stayed on `CREATED` forever and
  `resume` returned 409 with no way forward. `resume` now catches the same exceptions `submit`
  does and `RunService` records a consistent `RUN_FAILED`, exactly as it already did for `submit`.
- **An approval or rejection can no longer be recorded with no named actor.** `POST /runs/{id}
  /approve` and `/reject` used to fall back to `Actor.system()` for a fully anonymous call —
  indistinguishable on the append-only log from an automated decision (bug-hunt C6 residual). They
  now require an authenticated principal, the `X-Thymira-Actor` header, or a body `actor`, and the
  `thymira approve`/`reject` CLI commands default to `THYMIRA_ACTOR` or the OS user when `--actor`
  is not given, instead of silently sending nothing.
- **Long decimal metrics remain intact in persisted event text.** CARD redaction no longer
  mistakes the fractional part of a metric for a payment card, while formatted cards and
  unformatted numbers that pass the Luhn checksum remain redacted; internal identifiers are
  not redacted when they contain a card-shaped numeric suffix.
- **Completed Runs retain their terminal state after a late dispatcher error.** A post-completion
  error can no longer attempt an invalid failed transition over a completed Run.
- **A MIRA audit agent can no longer cite evidence the Run never produced.** Every reference a
  model proposes is now resolved against the Run's own event log before it can become a citation:
  an event sequence and hash that exist, an artifact the log announced under the digest it
  announced, a recorded experiment or tool call, and an external regulation citation only when the
  Run recorded a completed `search_regulation`. Unbacked references are stripped and the refusal is
  recorded on `agent.completed`; the finding itself survives for the Policy Engine to weigh. Both
  the shipped runner and `MiraAuditFlow` enforce it, so an injected runner cannot bypass it.
  Previously a complete assurance bundle verified as valid while citing a non-existent artifact.
- **The requirements-coverage control (A26) now runs in the composed runtime.** The deterministic
  audit declares the governance frameworks its audit-agent roster covers, so the KB-02
  requirements->controls mapping is finally contrasted against real Runs; A26 was permanently
  `NOT_APPLICABLE` ("no governance frameworks declared") before. It now asserts over the control
  outcomes already recorded in the same audit instead of re-invoking every check, which also stops
  one `audit_run` from evaluating every deterministic control twice.
- **The routing-floor control (A24) now audits model selections made outside an agent lifecycle.**
  Core's risk classification and MIRA's own finding verifier both route without opening one, and
  their selections were skipped entirely; a below-floor selection there now fails the control. The
  control is `NOT_APPLICABLE` only when the Run recorded neither a lifecycle nor a selection.

### Changed
- Every denied tool call renders with a `[denied]` last line; `agent.completed` carries
  `end_reason` (`completed` | `max_turns` | `awaiting_approval`); the composed graph's
  `("thy", "mira")` edge is conditional, so its definition hash changed and a Run mid-flight
  across this deploy will have its audit snapshot flagged stale (ADR-0013 decision 6); an
  escalated call counts twice against `max_tool_calls`.
- **The assurance bundle is now produced when a Run completes, and reading it is read-only.** The
  composition assembles and persists `exports/assurance-bundle.json` through the completion node,
  so finishing a Run guarantees the bundle exists. `GET /runs/{id}/assurance` now only reads that
  export and re-verifies it against the current event chain and artifact manifest; it no longer
  builds or writes anything, and reports `assurance_not_found` for a Run that produced none.

### Added
- **Policy-authorised rework now completes the Core-controlled correction loop.** A MIRA finding
  is translated by Core into a bounded `ReworkSignal` only after the Policy Engine decision and
  any required human approval; THY produces revisioned artifacts, MIRA audits the new evidence,
  and a tamper-evident assurance bundle exposes the exact Run, audit, decisions, approvals,
  events, hashes, and lineage through the API and CLI.
- **Audit snapshots are now freshness-gated before reuse.** A25 records invalidation evidence for
  later execution or activity-context events, changed artifacts, flow or policy versions, and
  changed activity profiles; Core sends that evidence through the Policy Engine and
  `RunController`, records the stale reason, and blocks approval/resume without recalculating on
  `GET`.
- **Production model and tool calls now consume one Core usage ledger through the Gate.** Budget
  decisions run before model selection, provider invocation, extra MIRA discovery rounds, and tool
  execution; routed models and the THY tool registry are checked before use, and denied calls emit
  auditable policy and lifecycle events.
- **MIRA's shipped bespoke audit agents now run in production.** The fixed dispatcher routes
  regulatory-evidence enrichment and compaction-fidelity checks to their specialized
  implementations, while ordinary specs retain the bounded generic runner; all candidates still
  pass through one verification, deduplication, report, and findings Gate cycle.
- **MIRA audit agents can now perform policy-gated regulation searches.** Core and the API build a
  small explicit MIRA registry over the packaged deterministic local corpus by default; the shared
  Tool Manager enforces each agent's YAML allowlist and records the call lifecycle. Search results
  carry source, version, fragment, score, backend and digest data, and findings can cite them as
  external evidence. The PostgreSQL/pgvector option is documented as experimental lexical-vector
  search, not semantic embeddings.
- **MIRA audits now persist and serve one evidence-backed report snapshot.** Core projects the
  Run's events, artifact manifest and tool results into MIRA, injects a bounded redacting artifact
  reader into audit agents, and the API serves the applicable `audit.completed` report without
  recalculating it.
- **Standalone and Core MIRA audits now share one canonical gate-less flow.** The standalone
  `MiraGraph` is a thin wrapper that adds its Gate once, while the production composition reuses
  the same preflight, evidence controls, deterministic checks, agents, discovery, verification,
  deduplication, report and event-writing path before its own single findings decision.
- **Policy-authorised execution constraints now bind THY and every tool call.** The shared,
  typed `ExecutionConstraints` contract is derived from applicable capability rules and recorded
  on the pre-execution Gate decision. Core passes that decision's constraints — not MIRA's
  internal context — to THY planning. The Tool Manager deterministically denies disallowed or
  prohibited tools, external effects under local-only execution, missing required evidence,
  pending human review, and exhausted tool-call limits. A model request cannot expand this
  recorded surface; a missing or mismatched Gate decision fails closed.
- **Runs now collect the activity facts that MIRA needs before THY can use a tool.** A Run with
  undeclared purpose, affected population, decision effect, autonomy, human oversight,
  jurisdiction, data categories, sensitive attributes or potential consequences stops on one
  event-backed question. `GET` and `POST /runs/{run_id}/risk-interview` expose and answer that
  question, and `thymira status` / `thymira answer` guide the same flow in the CLI. Every answer
  creates an immutable profile version; the resumed graph records MIRA's inherent-risk assessment
  and classifies it before tool execution. Repeatedly undeclared answers are bounded and escalate
  to human review.

### Fixed
- MIRA deterministic audits now reject missing policy snapshots, preserve audit revisions across
  resumed flows, pair persisted pack bindings by id, and keep finding identities reproducible;
  invalid enrichment candidates are logged and discarded.
- **MIRA provenance now names the code that actually runs.** `run.started` retains the composed
  Core graph digest while each MIRA report records the canonical `MiraAuditFlow` definition it
  executed; the unused standalone `MiraGraph` wrapper and its competing digest are removed.
- **A project's governance declaration now limits MIRA's audit-agent roster.** Core passes
  `ProjectConfig.governance.frameworks` into MIRA; the flow uses that one declaration for both
  A26 requirements coverage and agent applicability, so undeclared-framework agents make no
  model calls.
- MIRA audit-agent projections now select hash-chained Run evidence by event type, including tool,
  policy, artifact and lifecycle events, while bounding recent agent messages separately; cited
  artifact excerpts also have an explicit cardinality cap so narrative evidence stays bounded
  alongside the Run log.
- MIRA deterministic findings now cite the event sequences, artifacts and manifest evidence each
  control actually inspected, so the assurance bundle's traceability index leads to the relevant
  source instead of the first twenty Run events.
- MIRA no longer treats an in-flight snapshot's closeout controls as applicable: A2, A7 and A8
  are evaluated only in a final audit. Report fidelity A18 is `NOT_APPLICABLE` when no
  `experiment.completed` event or METRICS artifact exists, and a new evidence-sufficiency control
  (A28) rejects an empty or abruptly halted Run instead of allowing a clean `passed` verdict.
- MIRA finding deduplication now retains the highest-severity equivalent record, including its
  confidence and evidence, instead of the first arrival -- a deterministic `CRITICAL` can no
  longer be downgraded because an agent restated it at a lower severity.
- Model-proposed finding control IDs are explicitly namespaced under `model:`, so a finding a
  model authored can no longer match a Policy Engine rule written for a deterministic control
  such as credit-risk `A20`.
- MIRA records each verifier-rejected finding and the judge's rationale in `audit.completed`
  (`verified_drops`), while retaining the existing `verified_dropped` count for compatibility.
- **A MIRA audit agent can no longer cite evidence the Run never produced.** Every reference a
  model proposes is now resolved against the Run's own event log before it can become a citation:
  an event sequence and hash that exist, an artifact the log announced under the digest it
  announced, a recorded experiment or tool call, and an external regulation citation only when the
  Run recorded a completed `search_regulation`. Unbacked references are stripped and the refusal is
  recorded on `agent.completed`; the finding itself survives for the Policy Engine to weigh. Both
  the shipped runner and `MiraAuditFlow` enforce it, so an injected runner cannot bypass it.
  Previously a complete assurance bundle verified as valid while citing a non-existent artifact.
- **The requirements-coverage control (A26) now runs in the composed runtime.** The deterministic
  audit receives the project's declared governance frameworks, so the KB-02
  requirements->controls mapping is finally contrasted against real Runs; A26 was permanently
  `NOT_APPLICABLE` ("no governance frameworks declared") before. It now asserts over the control
  outcomes already recorded in the same audit instead of re-invoking every check, which also stops
  one `audit_run` from evaluating every deterministic control twice.
- **The routing-floor control (A24) now audits model selections made outside an agent lifecycle.**
  Core's risk classification and MIRA's own finding verifier both route without opening one, and
  their selections were skipped entirely; a below-floor selection there now fails the control. The
  control is `NOT_APPLICABLE` only when the Run recorded neither a lifecycle nor a selection.

### Changed
- **The assurance bundle is now produced when a Run completes, and reading it is read-only.** The
  composition assembles and persists `exports/assurance-bundle.json` through the completion node,
  so finishing a Run guarantees the bundle exists. `GET /runs/{id}/assurance` now only reads that
  export and re-verifies it against the current event chain and artifact manifest; it no longer
  builds or writes anything, and reports `assurance_not_found` for a Run that produced none.

### Added
- **Policy-authorised rework now completes the Core-controlled correction loop.** A MIRA finding
  is translated by Core into a bounded `ReworkSignal` only after the Policy Engine decision and
  any required human approval; THY produces revisioned artifacts, MIRA audits the new evidence,
  and a tamper-evident assurance bundle exposes the exact Run, audit, decisions, approvals,
  events, hashes, and lineage through the API and CLI.
- **Audit snapshots are now freshness-gated before reuse.** A25 records invalidation evidence for
  later execution or activity-context events, changed artifacts, flow or policy versions, and
  changed activity profiles; Core sends that evidence through the Policy Engine and
  `RunController`, records the stale reason, and blocks approval/resume without recalculating on
  `GET`.
- **Production model and tool calls now consume one Core usage ledger through the Gate.** Budget
  decisions run before model selection, provider invocation, extra MIRA discovery rounds, and tool
  execution; routed models and the THY tool registry are checked before use, and denied calls emit
  auditable policy and lifecycle events.
- **MIRA's shipped bespoke audit agents now run in production.** The fixed dispatcher routes
  regulatory-evidence enrichment and compaction-fidelity checks to their specialized
  implementations, while ordinary specs retain the bounded generic runner; all candidates still
  pass through one verification, deduplication, report, and findings Gate cycle.
- **MIRA audit agents can now perform policy-gated regulation searches.** Core and the API build a
  small explicit MIRA registry over the packaged deterministic local corpus by default; the shared
  Tool Manager enforces each agent's YAML allowlist and records the call lifecycle. Search results
  carry source, version, fragment, score, backend and digest data, and findings can cite them as
  external evidence. The PostgreSQL/pgvector option is documented as experimental lexical-vector
  search, not semantic embeddings.
- **MIRA audits now persist and serve one evidence-backed report snapshot.** Core projects the
  Run's events, artifact manifest and tool results into MIRA, injects a bounded redacting artifact
  reader into audit agents, and the API serves the applicable `audit.completed` report without
  recalculating it.
- **MIRA has one canonical gate-less audit flow.** The production composition reuses its preflight,
  evidence controls, deterministic checks, agents, discovery, verification, deduplication, report
  and event-writing path before Core's single findings decision.
- **Policy-authorised execution constraints now bind THY and every tool call.** The shared,
  typed `ExecutionConstraints` contract is derived from applicable capability rules and recorded
  on the pre-execution Gate decision. Core passes that decision's constraints — not MIRA's
  internal context — to THY planning. The Tool Manager deterministically denies disallowed or
  prohibited tools, external effects under local-only execution, missing required evidence,
  pending human review, and exhausted tool-call limits. A model request cannot expand this
  recorded surface; a missing or mismatched Gate decision fails closed.
- **Runs now collect the activity facts that MIRA needs before THY can use a tool.** A Run with
  undeclared purpose, affected population, decision effect, autonomy, human oversight,
  jurisdiction, data categories, sensitive attributes or potential consequences stops on one
  event-backed question. `GET` and `POST /runs/{run_id}/risk-interview` expose and answer that
  question, and `thymira status` / `thymira answer` guide the same flow in the CLI. Every answer
  creates an immutable profile version; the resumed graph records MIRA's inherent-risk assessment
  and classifies it before tool execution. Repeatedly undeclared answers are bounded and escalate
  to human review.

### Fixed
- **A failing Run sent the exception's own text, and its stacktrace, to Langfuse unredacted.**
  OpenTelemetry records an exception whenever a context manager is closed with one —
  `use_span` defaults to `record_exception=True` and `set_status_on_exception=True`, and the SDK
  overrides neither — attaching `str(exc)` and a full stacktrace as span *events*. Nothing
  redacted them: this repository's redaction and the SDK's own `mask=` hook both reach `input`,
  `output` and `metadata` only, and the exporter copies `events=` and `status=` verbatim. A
  structured-output parse failure was enough on its own to ship the model's raw answer off the
  machine. A failing observation is now marked `ERROR` with the exception's *class name* and
  closed as if it had returned, so a reviewer still sees which step failed and nothing else
  leaves the process.
- **`LANGFUSE_SAMPLE_RATE` did nothing.** The SDK turns that variable into a sampler only inside
  the `TracerProvider` it builds for itself, and skips that build whenever one is supplied —
  which Thymira always does, to keep Langfuse's spans off the API's exporter. A bare provider
  samples everything, so the documented knob was silently discarded and every trace was exported.
  The sampler is now applied where the provider is built, `ParentBased` so a sampled Run keeps
  every observation below it rather than being recorded in half. An unparseable or out-of-range
  value keeps every trace instead of turning tracing off.
- **Stopping the API server dropped the end of whatever Run was in flight.** The SDK batches and
  drains on a timer, and its `atexit` handler only runs on the orderly path — a container that
  does not stop in time is killed. The API now drains Langfuse from its lifespan, which is
  reached either way. The lost tail was the part that matters: a Run that failed failed at its end.
- **Langfuse's Available Tools and Tool Calls views were empty.** The previous change recorded the
  tool surface and the model's decision as *custom metadata*, which the UI does not read: Langfuse
  fills those views by parsing known request and response formats, and its own OpenAI integration
  records the input as `{"tools": [...], "messages": [...]}` and the output as
  `{"role": ..., "tool_calls": [...]}` (`langfuse/openai.py::_extract_chat_prompt`). A generation
  now emits exactly those shapes — tool definitions with `type`/`function`, calls with `id`,
  `type` and `arguments` as a JSON string, the way an OpenAI response carries them — so the counts,
  the per-tool filters and the "had this tool available but never called it" query all work.
  PydanticAI's structured-output tool is excluded from both, so an agent is not reported as having
  one more tool than it can use, nor as calling a tool when it simply answered. The metadata now
  carries only what no parser can infer: the routing tier, the router's reason, and whether the
  turn ended by producing the structured answer.

### Fixed
- **MIRA's deterministic evidence steps were typed `chain`.** Langfuse documents `chain` as "a link
  between different application steps, like passing context from a retriever to a LLM call", which
  these are not: they link nothing and feed no model. They are `evaluator` now — the closest
  documented fit rather than an exact one, since MIRA audits a whole Run's methodology and
  recomputes its artifacts, not one model's output. The type only affects filtering, so what
  changes is that a reviewer filtering for chains is no longer told a lie.
- **A tool's output reached the trace with no size bound.** `write_file` refuses more than a
  megabyte before it writes; `run_python` captures whatever the subprocess printed with no cap at
  all, and both feed the same observation. A step that forgot to bound a `print` — a whole
  dataframe, or the SVG buffer the Visualization agent is told to emit — would hand Langfuse a
  multi-megabyte output, scanned by the full redaction on the way, against Langfuse Cloud's 5 MB
  per-request ceiling with nothing able to truncate it: the SDK batches by event count, never by
  size, and dropped its own limit in v3. The trace now records a bounded copy; the agent still
  receives all of it, so no model's context is shortened by this.
- **An object handed to the trace was serialised past both redaction layers.** `redact_value`
  descends into strings and containers and returns anything else unchanged — right for the event
  log, where a datetime must survive canonicalisation as itself — so a pydantic model or dataclass
  would have had every string field on it written out whole. No call site did, and ADR-0012 says
  the public API takes primitives only, but that rule was a convention neither the annotations nor
  the redaction enforced. Anything that is not a JSON primitive or a container is now scrubbed as
  its `repr` on the way out. Fixed at the egress boundary rather than in `redact_value`, which
  `events.jsonl` shares and where the current behaviour is correct.
- **Prompt provenance (THY-06) was wired into `AgentRunner` and never reached it.** `record_prompt`
  only fires when `AgentContext.artifact_store` is set, and the Execute node built its context
  without one — although it already held the tool context's store, to fold produced artifacts back
  onto the state. So on the single path that runs real agents, no prompt was ever persisted and
  every `model.selected` stayed THY-02 shaped, while the roadmap recorded the task as done and
  "wired into `AgentRunner` so every model call is covered". The parallel wave path still has no
  store, by design: it is engaged only for a tool-less run.

### Added
- **The Thymira Operations dashboard is defined in code** (`scripts/langfuse_dashboards.py`, via
  `just langfuse-dashboards`): cost by model and over time, phase latency p95, the Policy Engine's
  decision mix and its trend, MIRA control outcomes, failed controls per Run, and audit status —
  all from data the runtime already emits. Rerunning updates in place rather than duplicating, so
  the definition survives a project being recreated and a chart change shows up in a diff. The
  dashboard API is **unstable** by Langfuse's own label, so the script sits outside the runtime:
  nothing imports it and an upgrade that breaks the contract breaks a recipe, never a Run. Its
  tests check every view, chart type and aggregation against the installed SDK's own enums, so
  that breakage surfaces in the fast lane instead of against a live project.
- **A trace says which commit produced it.** `Run.git_commit` was in the Contract and written by
  nothing: the commit was captured as provenance and put only in the creation event's payload.
  Provenance is now captured before the Run is built, and the commit rides the trace as its
  `version` — propagated to every observation, not set on the root alone, because Langfuse counts
  an observation in an aggregation over an attribute only if that observation carries it.
- **`reasoning_effort` is recorded as a model parameter, not as loose metadata.** Langfuse
  serialises `model_parameters` onto an attribute of its own, shows it in a dedicated panel and can
  match a pricing tier on one of its keys; a setting that changes both the answer and the bill
  belongs there. Only the effort the provider will actually apply is reported — one dropped for a
  model that has no such parameter would otherwise read as a setting that shaped an answer it
  never reached.
- **A generation carries the sha256 of the prompt that was sent**, the same key `model.selected`
  records, so a span in the trace leads back to the exact content-addressed artifact.
- **MIRA's verdicts and the Policy Engine's decisions are now Langfuse Scores.** Every
  `policy.decision` the Gate records is mirrored as a categorical score, and every deterministic
  MIRA control as `mira.control.<id>` with its status, beside `mira.controls.failed` and
  `mira.audit.status`. MIRA's are recorded from the composed graph rather than from
  `MiraAuditOrchestrator.assemble_result`: the graph drives preflight and the deterministic
  controls separately and assembles the findings itself, so that method — the obvious home — has
  no production caller and a score written there would never have been emitted. The status is categorical rather than a pass/fail number because a control
  can also be `NOT_APPLICABLE` or `NOT_EVALUATED`, and calling either a failure — or a pass — would
  be a claim MIRA never made. These are projections written *after* the authoritative event and
  read back by nothing; what they buy is the comparison `events.jsonl` cannot make, so "the BLOCK
  rate rose this week" or "control A3 started failing" becomes a chart and an alert instead of a
  script over every Run on disk.
- **`thymira status` links to the Run's trace.** A Run and its trace already share an id
  derivation, but the URL names the Langfuse project, which only the runtime's API keys know — so
  it comes from the new `GET /runs/{run_id}/trace`. Deliberately not a field on `Run`: the trace is
  telemetry *about* a Run, not part of the record the Contract freezes.
- **Cached and reasoning tokens now reach the trace.** LiteLLM reports both, nested inside the
  totals that contain them, and both were discarded. They are now split into Langfuse's exclusive
  buckets — `input` excludes `input_cached_tokens`, `output` excludes `output_reasoning_tokens` —
  because a flat `usage_details` is stored unchanged and overlapping buckets would double-count and
  overstate the inferred cost. The budget ledger is still charged the inclusive totals, which is
  what the provider actually bills.
- **A trace now shows the choices a model faced, not only the one it made.** Every generation
  records the model the router picked (at open, so a call that fails before answering is still
  attributable), the tier requested and applied with the router's reason, the tools the model was
  offered, and what it decided to do with them — `tool_calls`, `tool_call_count` and
  `answered_directly`, as filterable attributes rather than something to be parsed back out of the
  output. The distinction matters: an agent offered two tools that answers directly is a decision,
  while an agent offered none is a constraint, and the two were indistinguishable. PydanticAI
  exposes the structured result as a tool of its own, so `final_result` is counted apart from real
  tool calls instead of overstating how often agents reach for their tool surface. The attributes
  are set on every path, including the two that have no tools, so "no calls" never looks like "not
  recorded".

### Fixed
- **The Gate no longer weighs findings against a cost of zero.** `UsageLedger.charge` had no
  caller anywhere in production: the ledger was constructed in `build_runtime_graph_factory` and
  read in the composition's `review` node, so every `human.approval_requested` recorded
  `cost_so_far: {cost_usd: 0.0, requests: 0, tokens: 0}` and any policy rule gating on cost was
  structurally unreachable. `ThyOutput` now reports what the run charged, THY's own plan and
  summarize calls charge the run accumulator they already had in scope (they were unmetered),
  MIRA's audit agents thread one through `AuditAgentContext`, and both subgraphs fold their totals
  into the Run's ledger. Measured on a live Run: `{cost_usd: 0.01674085, requests: 12, tokens:
  61540}` where it previously reported zeros.
- **`thymira run` can finish a real Run.** The CLI's HTTP client used one 10-second timeout for
  both connecting and waiting, but `POST /runs` executes the whole graph inside the request
  (`InlineDispatcher`), so every real run reported "Cannot connect to the Thymira API" while the
  Run it had just started ran to completion on the server. Connecting still fails fast; waiting for
  an answer now allows 15 minutes, overridable with `THYMIRA_API_TIMEOUT`.
- **`just lint` is no longer red because of a git worktree.** A Run executed inside
  `.claude/worktrees/` writes the `run_python` scripts a model wrote into its workspace's
  `.thymira/`, and ruff linted that scratch code — failing the gate for reasons no commit could
  fix. Both are now excluded from ruff and from git.
- **The `thy` and `mira` nodes of a trace now say what they produced, and `thy` no longer lies.**
  Audited against three real Runs against a live model. Both orchestrator phases — the coarsest
  breakdown of a Run — carried no input and no output, so a reviewer clicking either saw nothing.
  `mira` now reports its finding count. `thy` reports THY's summary, experiment and artifact
  counts, and it is written by `ThySubgraph.invoke` rather than by the composing node: the adapter
  returns the runtime state unchanged, so `RuntimeState.plan` is always empty where `phase("thy")`
  is opened, and a summary derived from it there reported `planned_tasks: 0` for a Run that had
  planned and executed a task. `thymira.observability.update_current` is the seam that lets the
  code which knows an outcome record it on an observation it did not open.
- **The API server's "loaded environment from …" line now actually appears.** `stdout` is
  block-buffered when it is a pipe — a log file, a container's collector — and the process then
  runs until it is killed, so the one line that tells an operator whether the env file was found
  was never flushed. Confirmed by reproduction, fixed with `flush=True`.

### Added
- **`.env` is now actually read.** `.env.example` and `AGENTS.md` both said "keys live in `.env`",
  but nothing in the codebase loaded one: a repo-wide search for `dotenv` returned nothing, and
  the file reached a process only through `compose.yaml`'s `env_file`. Running `uv run thymira-api`
  or the `thymira` CLI directly ignored the very file the documentation told you to write, so an
  unset `LANGFUSE_PUBLIC_KEY`, `OPENAI_API_KEY` or `THYMIRA_MODEL_*` looked like an unconfigured
  feature rather than an unread file. Both entry points now load the nearest `.env` at or above the
  working directory — `thymira-api --env-file`, or `THYMIRA_ENV_FILE`, names a different one — via
  `thymira.api.env.load_env_file` and its CLI counterpart. Two properties make that safe: **the
  real environment always wins** (loading never overrides a variable that is already set, so a
  shell export, a container's `-e`, a CI secret and compose's own `env_file` all still take
  precedence), and **only entry points load it** — `create_app` and `build_default_deps`
  deliberately do not, because a library factory reaching for a developer's `.env` would make every
  test that builds an app depend on that developer's working tree, which a test now pins. Naming a
  file that does not exist is a start-up error; having no `.env` at all stays the normal case.
- **`THYMIRA_REASONING_EFFORT` is forwarded to LiteLLM.** When set (`none`, `minimal`, `low`,
  `medium`, `high` or `xhigh` — the values LiteLLM's own `REASONING_EFFORT` literal accepts, pinned
  by a test so a LiteLLM upgrade that renames a level fails the suite rather than a live call),
  every completion includes that `reasoning_effort`; unset, the provider default is left alone, and
  an unknown value fails at provider construction with `LLMConfigurationError`. It is one setting
  for a process that routes models *per tier*, so it is sent only to models
  `litellm.utils.get_supported_openai_params` reports as accepting it: LiteLLM does not ignore an
  unsupported parameter (`litellm.drop_params` is False by default) but raises
  `UnsupportedParamsError`, which this provider reports as `LLMCallError` and `routed_model` turns
  into a `ModelRetry` — so sending it blindly would have burned a FAST-tier agent's whole retry
  budget on every call and read as a model failure rather than a misconfiguration. Every response
  records `reasoning_effort` and `reasoning_effort_applied` in its metadata, and the Langfuse
  generation carries both, so a setting that changes the answer and the bill is visible beside them
  instead of only in the process environment.
- **A Run can now be read as one Langfuse trace, without any of it becoming evidence.** The new
  `thymira.observability` member (`runtime/observability/`, layered between `thymira.state` and
  `thymira.events`) is the only code that imports `langfuse`, and it opens one observation at each
  seam a Run already passes through: `InlineDispatcher.submit`/`.resume` wrap `graph.invoke` in the
  Run's trace, whose id is derived from the Run id so the same Run always maps to the same trace;
  `compose.run_thy`/`run_mira` become `agent` observations; `AgentRunner.run`,
  `mira.agents.runner.run_audit_agent` and THY's own Plan and Summarize agents become nested
  `agent`s; `routed_model._call` becomes a `generation`; `tool_bridge` and MIRA's own tool closure
  become `tool`s beside the generation that requested them; MIRA's preflight and deterministic
  controls become `chain`s; and `gate.check_action("plan.proposed")` and `gate.review_findings`
  become `guardrail`s. A generation records the model that *answered* (`LLMResponse.model`) rather
  than the router's synthetic `routed:{role}:{task}` binding — PydanticAI's `FunctionModel`
  overwrites the response's model name with that binding, so the seam is the last place the real
  id exists — together with `usage_details` and an ingested `cost_details`, left unset rather than
  reported as zero when the model's price is unknown. The root records how the Run ended (its
  decision, reason and finding count) so the trace list is readable without opening each Run, and
  a Run parked for human review opens its second, post-approval episode as `run-resumed` rather
  than a second root called `run` — the agent-graph view groups nodes by name, and two roots with
  one name render as a single node that ran twice, which is not what happened. An agent that
  exhausts its turns and a model call that fails are marked `ERROR` with a redacted reason, so a
  reviewer can filter for them instead of opening every Run. Every input, output and metadata
  value is
  redacted by `thymira.events.redact_value`, the same function `EventLog.append` uses, before it
  reaches the SDK, and the SDK's own `mask=` hook is registered with it as a second line of
  defence. Nothing here can fail a Run: every Langfuse and OpenTelemetry call is isolated and
  degrades to a no-op handle, while an exception from the traced body propagates unchanged.
  Tracing is off unless `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set (see
  `.env.example`), and while off `langfuse` is never imported at all. The trace remains a
  non-authoritative mirror — `events.jsonl` is still the record (ADR-0012).
- **The agent skills now include Langfuse's own.** `.agents/skills/langfuse/` vendors
  [langfuse/skills](https://github.com/langfuse/skills) verbatim (MIT, provenance and update
  instructions in its `VENDORED.md`), so Codex, Cursor, Gemini CLI and — through
  `just sync-skills` — Claude Code all read the same guidance on tracing, prompt management,
  datasets and evaluation.

### Fixed
- **Run creation now returns before long graph execution completes.** The local API dispatches the
  graph after returning the created `run_id`, the CLI distinguishes response timeouts from a
  refused connection, and Run summaries fold agent, tool, experiment and artifact identifiers
  from the authoritative event log instead of reporting empty counters.
- **`scripts/validate_skills.py` no longer rejects a valid SKILL.md.** Its frontmatter parser
  understood only `key: value` and one level of nested mapping, so a folded block scalar
  (`description: >-`) — the way third-party skills, including Langfuse's, are actually written —
  failed with `unexpected indented line in frontmatter`, and a block sequence (`allowed-tools:`
  followed by `- item` lines) was silently misparsed into a mapping of nonsense keys. It now reads
  block scalars (`>`, `>-`, `|`, `|-`), block sequences and nested mappings recursively, so a
  skill that any other agent harness accepts passes `just validate-skills` too. Its
  reference-existence check also stopped swallowing the full stop that ends a sentence
  (`see references/guide.md.` named `guide.md.`): the bug was invisible on Windows, whose Win32
  API strips a path's trailing dots so the non-existent file resolved anyway, and surfaced only
  as a red CI on Linux.
- **Two tool calls in the same THY turn no longer corrupt a Run's event log.** Production's
  `RunEventLog.append()` reads `LocalRunStore.version(run_id)` and then calls
  `LocalRunStore.append(..., expected_version=...)` with no lock between the two — two threads
  appending for the same Run (a model requesting more than one tool call in one turn is a real
  case) could both read the same current version, both pass the optimistic check, and both write
  a line to `events.jsonl` with the same `seq`, breaking the hash chain irrecoverably (surfaced
  live as `ValueError: ... broken sequence (seq=N)` on a later `RunController.advance`, escaping
  as an uncaught 500). `JsonlEventLog`/`InMemoryEventLog.append()` also had the same unprotected
  read-mutate-write on their own `_seq`/`_prev_hash` state when one instance is shared across
  threads. `LocalRunStore` now serialises `append()` per Run under a `threading.Lock` (not a
  global lock, so unrelated Runs never block each other), and `_ChainState` guards each log's own
  `append()` the same way; a caller that decided something *from* the history it read still gets a
  clean stale-version `ValueError` instead of a torn write. `RunEventLog` is not such a caller —
  it read the version back purely to satisfy the store's signature — so it now appends with
  `expected_version=None`, resolved inside the store's own per-Run lock. Otherwise the race was
  only moved: `ToolManager.execute` appends `tool.started` and `tool.completed` around every call,
  so a losing writer raised out of the tool boundary, failing the Run and — on the
  `tool.completed` side — leaving behind exactly the unpairable `tool.started` MIRA's A9 control
  exists to catch.
- **The credit-risk demo can now actually run against a real model.** The base governance policy
  has no `action_rules` entry for `plan.proposed`, so it fell through to
  `default_decision: REQUIRE_HUMAN_REVIEW` — a decision the MVP's synchronous `InlineDispatcher`
  cannot satisfy (there is no mid-execution pause/resume for THY's own plan gate, unlike MIRA's
  post-hoc findings review). Every real (non-scripted) `thymira run` against
  `examples/credit-risk` halted as `FAILED` before touching the dataset. The example project's
  policy overlay now carries `CRX-001`, an `action_rules` entry that lets THY's own plan proceed
  — mirroring `tests/thymira/test_core_graph_adapters.py`'s own `_PLAN_PASSES` test policy — while
  the existing `CRX-101`/`CRX-102`/`CRX-103` finding rules still gate the Run's completion exactly
  as before.
- **A workspace's own governance policy now actually reaches the live Gate.** `build_default_deps`
  built the Gate's `PolicyEngine` from the bare packaged base policy regardless of the configured
  workspace, so a project's own `.thymira/policies.yaml` — loaded and unit tested against
  `load_policy`/`load_policy_stack` — never reached a real Run's Gate calls; the fix above had no
  effect against the live API composition. `build_default_deps` now layers each configured
  framework's packaged default, then the project's own overlay, on the base policy.
- **THY's Execute node can now resolve the FINAL-wave specialist agents a real Plan proposes.**
  `build_runtime_graph_factory` composed `default_thy_catalog()` — the three-agent MVP roster —
  as its default catalog, a leftover from before THY-32 opened `ThyAgentKind` to the shipped
  `statistics`/`data-quality`/`visualization` specialists. A real (non-scripted) THY's own Plan,
  which the `ThyAgentKind` enum told it those specialists exist, could propose delegating to one
  of them, and Execute then raised `KeyError: unknown agent spec` for every such task. Production
  composition now defaults to `full_agent_catalog()`, which carries every shipped specialist.
- **THY's delegated agents can now actually call their tools in production.** `ThySubgraph.invoke`
  never passed a `tool_registry`/`tool_context` to `run_thy`, and `AgentRunner` binds an agent's
  tools only when both are given — so every real (non-scripted) THY agent ran with an empty tools
  tuple: `run_python` never executed, MLflow never logged anything, and a delegated agent's
  reported "tool output" was the model generating text shaped like a plausible result rather than
  a real one. `ToolManager` now exposes its wrapped `ToolRegistry` (`.registry`), and
  `ThySubgraph.invoke` builds a real `ToolContext` from the composition's own event log, Gate and
  artifact store whenever a workspace is configured — mirroring the pattern
  `apps/api/src/thymira/api/routes/mlflow.py` already used for a one-off tool call. A Run with no
  configured workspace stays tool-less exactly as before.
- **The durable-resume worker subprocess starts again.** `test_core_queue_dispatch` and
  `test_e2e_durable` double as the worker entry point and import
  `tests.thymira.docker_support` at import time, but launched the child as a bare file path, which
  puts only `tests/thymira` on `sys.path` — so every worker died with
  `ModuleNotFoundError: No module named 'tests'` before consuming a task and the parent reported
  only "worker exited before reaching MIRA". Both now launch it with `-m` from the repository
  root. (Pre-existing on `main`; the Actions outage meant no full-suite run ever reported it.)
- **THY is now composed over the project root, not over its `.thymira` directory.**
  `build_default_deps` passed `ProjectResolution.workspace` -- which is `config.yaml`'s own
  directory, i.e. `.thymira` itself -- as the composition's `project_dir`, but everything
  downstream appends `.thymira` on its own: `load_project_context` reads
  `project_dir / ".thymira"`, and `inspect_model`/`audit_model` write under
  `invocation.workspace / ".thymira"`. Inspect therefore looked in `.thymira/.thymira`, found
  nothing, and silently seeded every real Plan with no domain, no `context.md` and no frameworks;
  and once the tools above were threaded in, every delegated agent's file tool was sandboxed
  inside the config directory, so `read_file("data/...")` and `run_python` could not reach the
  project's own dataset at all -- the real cause of the missing-file crash fixed above.
  `ProjectResolution` now names the two apart (`workspace` stays the governance directory,
  `project_dir` is the root that contains it), and the composition and the experiments route both
  take `project_dir` -- the latter so `MlflowTracker`'s `workspace / ".mlflow"` root is the same
  one THY writes through.
- **`read_file` on a missing file no longer crashes the whole Run.** `ReadFile.execute` called
  `Path.read_text` uncaught, so a missing file raised a bare `FileNotFoundError` —
  `ToolManager.execute` only turns a `ToolExecutionError` into a failed tool result and lets any
  other exception propagate, so this crashed the Run instead of giving the delegated agent a
  result it could act on. Surfaced live: a real THY agent explored the credit-risk project's
  workspace, tried `read_file("data/README.md")` (a path that doesn't exist in that layout), and
  the Run died with no further evidence. `ReadFile.execute` now catches `OSError` and raises
  `ToolExecutionError`, matching `ListFiles`' existing pattern.
- **The artifact store no longer accepts a name that overwrites its own bookkeeping.**
  `manifest.json`, `manifest.json.tmp` and anything under the `.history` archive were ordinary
  artifact names, so a tool could clobber the manifest every integrity check reads — the `.tmp`
  variant silently, and irreversibly. They are refused case-folded, on writes and queries, the way
  the reserved Windows device names already were, along with any name carrying a colon: on NTFS
  `manifest.json::$DATA` is the same file under a second name, which defeats every rule that works
  on names. A nested `reports/manifest.json` is still yours to write.
- **A rolled-back transaction leaves an event log that can still be appended to.** Rollback restored
  the log file but not the log object, whose sequence number and previous-hash cursor still pointed
  past the end of what was now an empty file — so the next append wrote a stale sequence number and
  the hash chain could never verify again. There is no wired caller today, which is why nobody had
  hit it.
- **Listing runs by status returns the runs that are in that status.** The store filtered on the
  status frozen into `run.json` at creation while the service computed the real one from the event
  chain afterwards, so `?status=completed` was always empty and `?status=created` returned bodies
  saying `"completed"`. The store now hydrates before it filters and pages, through a callback the
  caller supplies: only `RunController` can know a Run's current status, and it lives a layer above
  the store.
- **An unusable pagination cursor fails once, by name.** A well-formed cursor carrying a timestamp
  without a timezone decoded happily and then raised `TypeError` on comparison, and two malformed
  cursor shapes raised two different exception types for the same condition.
- **An agent spec with no task kinds is refused when it is loaded, not when it is run.** It was
  accepted and then raised a bare `IndexError` from the runner, far from the file that caused it.
- **A project's `agents.yaml` can no longer grant an agent a tool capability that does not exist.**
  The built-in spec loader refused exactly that; the project-overlay path, which re-implemented the
  same validation, accepted it. They now share one check instead of agreeing by coincidence.
- **Redaction no longer masks ordinary identifiers as if they were API keys.** The heuristic matched
  any `key`/`token`/`secret` prefix followed by sixteen characters, which is also the shape of an
  ordinary snake_case name — `token_usage_by_model_tier` was redacted. That was an accepted
  trade-off until spilled tool output began to be redacted before storage, which made the stored
  artifact the only copy and every false positive a silent loss of evidence. A match now needs a
  digit or a capital in it: credential material has one, a run of lowercase words does not. The
  requirement is applied in Python rather than in the pattern, because expressing it as a lookahead
  made redaction quadratic on repeated prefixes — and redaction runs synchronously before every
  event is written.
- **`thymira-thy` declares the dependency it imports.** It imports `thymira.policies` and resolved
  it only transitively through `thymira-tools`, so installing it on its own, or any change to what
  `tools` depends on, would have broken it with no warning.
- **A release can no longer be tagged against a version that publishes nothing.** The tag was
  checked against the virtual root package; the eleven member wheels that actually get released were
  unchecked. Every member wheel's version is now held against the tag.
- **Durable run resume no longer re-executes a completed node after a crash.** The run worker
  invoked each Run's composition graph with LangGraph's default `async` durability, which persists a
  superstep's checkpoint on a background thread without waiting; a worker killed at the THY/MIRA
  boundary before that write landed was resumed from before THY, re-running its experiments and
  breaking the exactly-once guarantee — intermittently, and only under load. `InlineDispatcher` now
  runs the graph with `sync` durability, so each node's checkpoint is committed before the next node
  runs and a redelivered task always resumes from the last committed boundary.
- **Local state writes no longer lose an atomic replace on Windows.** Every store that swaps a
  temporary file into place — checkpoints, the run projection, the artifact manifest, records and
  repositories — now retries the replace on the transient access-denied error Windows raises when
  another process holds a brief lock. A checkpoint is written after every graph superstep, so an
  API-driven run could fail or be recorded as FAILED for a reason that had nothing to do with the
  run.
- **Agents now actually run with their system prompt.** `routed_model` read the system prompt only
  from a PydanticAI `SystemPromptPart`, but every THY and MIRA call site builds its agent with
  `instructions=`, which pydantic-ai carries on `ModelRequest.instructions` instead — so the
  provider received an empty system message and every `AgentSpec`'s behavioural contract was
  silently dropped. Both the tool and no-tool paths now honour it, and a caller supplying both
  keeps both.
- **A version string is no longer mistaken for an email address.** The redaction pattern accepted
  a numeric top-level label, so `name@major.minor` was replaced with `[REDACTED:EMAIL]` — it ate
  MIRA's own `AuditReport.control_set` (`thymira-mira-checks@0.1`) out of the evidence handed to
  the audit agents. The domain now requires a top-level label starting with a Unicode letter, or
  an IPv4 literal, so real addresses — internationalized ones included — stay redacted.
- **MIRA audit-agent identity validation.** The runner now rejects malformed or wrongly prefixed
  agent and task ids before emitting lifecycle events or invoking a model.

### Changed
- **The MVP API now has an official local server entrypoint.** `thymira-api` validates a configured
  workspace and serves the existing FastAPI boundary with local JSON/JSONL persistence and inline
  dispatch, making the HTTP surface usable by the CLI and local integration tests without implying
  the production database, queue or external-service architecture.
- **Three checks that could report success without having checked now fail instead.** The type-check
  ratchet discarded `ty`'s exit code, so a crashed `ty` parsed to zero diagnostics, compared equal to
  a zero baseline and passed — including under `--strict`, where the gate exists precisely to stop
  things. The CI image smoke test captured the container's exit code and never asserted it, and
  matched the word `thymira` against output that includes Docker's own `Unable to find image
  'thymira:ci'` — so it passed on the failure it exists to detect. The roadmap checker paired
  catalogue entries with their detail sections by position, so one heading outside the catalogue
  shifted every pair after it and reported mismatches in domains nobody had edited.
- **No tool call is left without a completed record, whatever the tool returns or raises.** The Tool Manager caught only the failure type its own tools raise, so any other
  exception escaped between `tool.started` and `tool.completed` — killing the run, skipping the
  artifact and usage bookkeeping, and leaving MIRA's lifecycle control an opened call it can never
  see closed, on a log that is append-only and cannot be corrected afterwards. A reproduction:
  `query_sql` with `SELECT INTERVAL 2000000000 DAY` raised `OverflowError` straight through the
  manager. Every exception a tool raises is now recorded as a failed call, with the exception type
  in the error text so an unexpected crash stays distinguishable from an expected failure; process
  signals still propagate and no traceback is persisted. The same guarantee now covers the events a
  tool declares, whose payloads are equally tool-controlled. An unknown tool name — ordinary
  traffic, since a model proposes it — was an unguarded `KeyError` that produced no events at all,
  and is now a recorded failed call that authorises and runs nothing — with the name bounded and
  stripped of control characters first, since it is caller-supplied and the log does not redact an
  actor id. The fields a tool returns are validated at the same boundary: `ToolResult` is a plain
  dataclass, so a `NaN` exit code or an arbitrary object in `sandbox_mode` used to reach the log's
  canonicalizer and raise there — closing the record is now what cannot fail, and a malformed field
  costs the field and is named in the error.
- **Oversized tool output is redacted before it is stored, and its digest can be recomputed.** Text
  spilled to a log artifact went to disk verbatim while the event log's copy of the same text was
  redacted, so a secret in a large stdout was persisted in the clear with a sha256 manifest entry.
  Spilling now redacts once and derives the inline head, the stored artifact and the digest from
  that same redacted text. The digest itself was worse than absent: it was a sha256 over a private
  concatenation of the streams that nothing in the repository can recompute, and it silently
  overwrote the honest digest a tool had computed for its own result. A tool's digest is now never
  discarded; a single spilled stream carries the sha256 the artifact manifest holds, and two
  streams carry none, because each artifact already records its own.
- **`audit_model` no longer reports fairness and accuracy numbers derived from the audited data
  instead of the model.** The tool rebuilt the label space and the feature order from whatever
  frame it was handed, so a cohort carrying one class raised `IndexError`, and — silently, which is
  far worse in an audit tool — a model whose class order or column order disagreed with the frame
  produced a wrong accuracy, a wrong confusion matrix and wrong per-subgroup metrics with no error
  anywhere. Both now come from the model. The underlying cause was upstream: `run_experiment` fitted
  on the output of a `LabelEncoder` and then discarded the encoder, leaving a saved model whose
  class space was a list of positional codes that no longer named anything. It fits on the labels
  themselves now, and a model that cannot predict a label the audited column carries is refused
  rather than scored against codes it cannot match. Predictions are matched against the class space
  by value; the positional `labels[int(prediction)]` fallback that was the mechanism of the
  mis-labelling is gone. Labels the dataset and the model spell differently — `1` read by polars,
  `1.0` read by `csv.DictReader` — are recognised as one label rather than treated as a mismatch.
- **`run_statistics` no longer publishes a claim the test it ran does not support.** Welch's t-test
  is defined for two samples; asked for three or more groups it computed on the first two and
  reported every group as having taken part — a false scientific claim, hash-chained into an event
  and sha256'd into an artifact. It now requires exactly two and says so. `chi_square` crashed on a
  null category, because the null-dropping guard compared against the string `"null"` while polars
  yields `None`; `correlation` lacked the minimum-count guard all its siblings have and failed on
  an unpacking error instead of saying what was wrong; and a non-finite statistic was written into
  the result as `NaN`, which is not valid JSON, so the recorded evidence could not be read back. An
  undefined statistic is now a failed test rather than a successful one carrying `NaN`.
- **A histogram of a column containing NaN no longer fails the tool.** `_histogram` was the one
  numeric path in the profiler not routed through the finite-value helper, and `drop_nulls` does not
  drop NaN in polars.
- **A tracker query for run `""` no longer returns every run.** An empty id is falsy, so the filter
  was skipped entirely and the missing-id guard in `compare_models` never fired. The tracker now
  compares against `None`, and an empty id is refused at validation.
- **`git_status`'s `porcelain` argument does something.** It was published in the MCP schema, so a
  model could set it, and the implementation ignored it — an advertised argument that did nothing.
- **The finding verifier's prompt is bounded, as its docstring already claimed.** The events half
  was capped and the evidence half was not, so a finding carrying long evidence rendered without
  limit; truncation is now marked, so the verifier can tell "no more evidence" from "evidence I was
  not shown".
- **A cost ceiling is no longer silently switched off when its input becomes unknown.** LiteLLM
  reports no price for a new, self-hosted or proxied model, and the usage ledger correctly poisons
  the run total to unknown from the first such response — but all three places that enforce a
  ceiling then read "unknown" as "nothing wrong". `UsageLedger.exceeds` reported the cap clean for
  the rest of the run, `RunUsage` coerced the unpriced charge to zero so a hard ceiling could never
  be reached, and the Policy Engine's own `BudgetRule.crossed` — the one the Gate actually decides
  on — simply did not compare it. All three now treat a configured ceiling plus an unmeasured value
  as crossed. They stay three implementations because the layer order forbids sharing one, so each
  names the other two in its docstring.
- **A HIGH audit finding that carries evidence no longer decides more leniently than the same
  finding without it.** A HIGH finding whose author self-assessed confidence below 0.9 fell through
  to a WARNING when it had evidence, while the identical finding with no evidence escalated to human
  review. A model's own confidence number is exactly the kind of output that must not relax an
  authorization, so a catch-all rule now escalates every HIGH finding regardless.
- **A stricter policy rule is no longer silently shadowed by a permissive one.** `decide_capability`
  returned the first matching rule rather than the highest-precedence one, so a rule appended to
  tighten an overlay never took effect and the decision recorded no trace that it had matched. The
  same defect was live in `decide_action` and `decide_model`, where it reached further: because a
  project overlay is *prepended* to the shipped policy, an overlay permitting `deploy_model`
  outranked the hard prohibition the EU AI Act rule ships, and a rule permitting every model
  shadowed a deny-list. All three now resolve by precedence, the way finding decisions already did,
  with policy order only breaking a tie, and each names the matches it outranked in the recorded
  reason so the decision can be replayed.
- **The CLI no longer prints `$0.0000` for work whose cost is unknown.** The runner records a null
  `cost_usd` when the gateway priced no step of a call, precisely so nothing downstream claims the
  work was free; the per-agent usage table coerced that null back to zero and folded it into a
  confident dollar total. An unmeasured cost now stays unmeasured through the fold and renders as
  `unknown`.
- **The CLI no longer labels delegated-agent cost as the whole Run total.** `status` now calls the
  amount an `Agent subtotal`, states that THY/MIRA/preflight costs are excluded, and separately
  sums every recorded `model.response_chunk` into an all-provider token row (including cached
  input and reasoning-output subsets). Whole-run price remains explicitly sourced from provider or
  Langfuse billing because request events do not persist a dollar estimate.
- **An operator can raise a role's model tier floor but no longer lower it.**
  `THYMIRA_<ROLE>_MIN_TIER` replaced the default floor instead of raising it, so
  `THYMIRA_MIRA_MIN_TIER=fast` ran the audit orchestrator below the floor its own configuration
  calls mandatory — and the `model.selected` event, which exists to record exactly this decision,
  recorded nothing about the override. A refused override is now recorded too.
- **`query_sql` can no longer read files outside the registered dataset.** The tool's read-only guard
  was a blocklist of SQL verbs, but DuckDB's reach into the filesystem is not a verb: `read_csv`,
  `read_parquet`, `read_text`, `glob` and the direct-filename replacement scan all live inside an
  ordinary `SELECT` and passed every check. A model-supplied query could therefore read any file on
  the host — including `.env` — and return its contents as tool output, while the capability the
  Policy Engine authorised the call under declared `data_access=("dataset",)` and no risk tags. The
  connection now runs with external access disabled, which closes reads, writes and `glob` at the
  engine instead of by enumerating verbs; the verb guard stays as defence in depth. A query the
  engine refuses is recorded as a failed tool call rather than escaping as an unhandled error.
- **A private key that arrives without its `-----END-----` marker is no longer written to the event
  log in the clear.** Redaction required the closing marker, so a key truncated by an output limit or
  a partial paste matched nothing and was persisted verbatim to the append-only, hash-chained log,
  where no later edit can scrub it. The same pattern also missed whole key formats that were complete
  and well-formed: PGP `PRIVATE KEY BLOCK`, `SSH2` labels containing a digit, RFC 4716 four-dash
  markers, tab or multi-space separators, and lowercase banners. Redaction now recognises those
  banners and masks an unterminated key by its body structure, while text that merely mentions a
  banner — a security instruction, a finding that quotes one, prose about the PEM format — is left
  intact, as is any `sha256` digest near it. `redact_value` also now descends into `bytes`,
  `bytearray`, `set`, `frozenset` and `memoryview`.
- **An approval recorded through the API no longer claims an identity was authenticated when
  nothing checked it.** Three doors onto that evidence asserted it: the Run approve/reject routes
  and the governance approval routes both stamped `authenticated=True` on the caller-supplied
  `actor` string, and `resolve_actor` did the same for the `X-Thymira-Actor` header, which any
  caller can set. `Actor.authenticated` records whether an identity was verified, and
  `human.approval` is hash-chained and append-only, so the claim could never be corrected
  afterwards. All three now record a declared identity as unauthenticated; a request carrying a real
  bearer token still records the resolved principal as authenticated.
- **MIRA no longer reports a correctly approved run as a failed audit.** Controls paired approval
  requests with approvals on one payload key, but the two writers disagree: the Gate's action path
  writes `decision_id` while the control-plane path writes the `Approval` record's own
  `policy_decision_id`, which is frozen in Contract 0.3. Every approval granted through MIRA's
  bounded control plane therefore read as unanswered. A tolerant accessor now lives in
  `thymira.schemas` beside `Approval` and is used by all eight readers across `mira`, `policies` and
  `core` — the reader was fixed rather than the writer because `events.jsonl` is append-only, so
  only a tolerant reader repairs evidence already recorded.
- **Two audit controls no longer pass on evidence they could not check.** A8 compared a review's
  decision id against the approval's, and when a review payload carried no id and an approval named
  no decision, `None == None` marked an unanswerable review resolved. A9 pairs agent lifecycle
  events by `subject_id`, and every event written before this release carries none, so the started
  and ended sets were both `{None}` and an agent that started and died read as paired — a green pass
  on exactly the evidence the control exists to police. Neither treats a missing identity as an
  identity any more, and A9 now reports pairing as not applicable, with the reason, when the
  lifecycle events identify nobody.
- **A human approval with a redactable identity is no longer refused.** `RunController` compared a
  freshly built `Approval` payload against the stored event without redacting it, while the
  `PolicyDecision` comparison thirty lines above did redact. Since events are redacted on append,
  any approver identified by an email, phone, IBAN or DNI could never match, and the control plane
  raised "approval does not have a matching Gate record" for a valid approval that was on the chain.
- **Agent lifecycle events now carry the agent they belong to.** `AgentRunner` appended
  `agent.started` and both `agent.completed` branches with no `subject_id`, and MIRA's A9 control
  pairs them by that field, so as soon as any one agent completed, every started agent read as
  paired and an agent that never finished could not be detected.
- **MIRA's "do not address THY" guard no longer rejects ordinary words, nor the compliance its own
  prompt asks for.** It was a bare substring test for `thy`, so a statement containing "healthy",
  "lengthy" or the product's own name aborted the whole decision context, discarding every other
  statement and citation in the draft. The system prompt also asked the model for no "instructions
  for THY", so a model that complied and said so tripped the guard on its own compliance; the prompt
  no longer asks the model to name the orchestrator. The guard was simultaneously easier to evade
  than to satisfy, because `\b` does not stop at an underscore and `THY_PLANNER must retrain the
  model` passed; the boundary is now letter-based, and the comment states the one lexical invariant
  the guard actually enforces instead of a form-of-address rule it never did.
- **A reviewed pack that declares no required evidence is rejected at load time** instead of
  producing a control that is satisfied by nothing, which the schema then refused with an unhandled
  validation error that aborted the entire preflight — including every other pack and control in it.

### Added
- **`RA-STATE-04` — PostgreSQL backend.** `thymira.state.postgres` implements all six repository
  protocols with SQLAlchemy tables and an Alembic baseline migration. Nothing above
  `runtime/state` learns which backend it is talking to: the backend-agnostic conformance suite
  that passes for the local stores passes unchanged against a dockerized Postgres, and the event
  hash chain still verifies after a reload.
- **`RA-CORE-11` and `RA-QA-03` — the durable worker.** A queue-backed dispatcher and a worker that
  resumes a Run from its committed checkpoint, proven by an end-to-end test that kills a worker
  mid-flight and asserts the resumed run completes exactly once.
- **`CMP-01`, `CMP-02` — compaction fidelity.** The log-vs-surface guarantees ADR-0006 asks for,
  and the control that answers whether a summary faithfully represents what it replaced.
- **`MIRA-03`, `KB-04`, `ASSUR-02`.** The remaining MIRA graph work, semantic regulation retrieval,
  and finding-level expert review with a finding→evidence→source traceability index.
- **`THY-25`, `THY-26`.** The remaining prompt and provenance work in THY.
- **`QA-DEMO-GOV` — the credit-risk governance demo.** The documented demo path, with its stated
  outcome pinned by a test rather than asserted in prose.
- **The full THY specialist roster is reachable from a real run.** `ThyAgentKind` now opens to the
  statistics, data-quality and visualization agents through a composed catalog, a Research agent
  ships whose every claim carries a citation by schema rather than by prompt advice, and a project
  can tailor THY's agents through a `.thymira/agents.yaml` overlay without touching code.
- **Parallel execution, rework and recipes in THY.** Independent plan tasks dispatch together, a
  rework node reopens work the summary showed to be unsound, and reusable recipes capture a proven
  workflow.
- **Container sandbox and richer tool surface.** `run_python` can execute under a real container
  with full confinement, alongside the new git, model-inspection and MCP descriptor work.
- **API authentication, delegated tool routes and a decision surface in core.**
- **MIRA's modelling, report and coverage controls.** Seven deterministic controls land together —
  split integrity and leakage indicators, lineage coherence, model selection re-derived
  independently, the cross-validation, baseline and reproducibility checks, report-versus-artifact
  fidelity, requirements coverage and audit-snapshot freshness. Each recomputes from the recorded
  evidence and reports NOT_APPLICABLE where a run carries no modelling evidence, so existing runs
  audit exactly as before.
- **Four more audit agents.** Model risk, compliance, credit risk and a regulatory-evidence pass
  that enriches findings with resolvable citations and **drops** an unresolvable one rather than
  fabricating it. The shipped roster is now seven agents, still one YAML declaration each.
- **MLflow querying and comparison.** `query_mlflow` and the model-comparison tool, plus
  `thymira mlflow` in the CLI over a new API route.
- **Budget and model-allowlist policy rules.** Both evaluated by the same deterministic,
  fail-safe engine — an unmatched case escalates, it never passes by omission.
- **`QA-E2E-GOV` — the governance loop, proven end to end.** A run driven through the CLI, the API
  and the composed graph now has all four outcomes covered: a clean run passes, a medium finding
  warns, a high-confidence finding parks the run for human review until `thymira approve` resumes
  it to completion, and a critical finding blocks with nothing executing afterwards. Every case
  asserts on the hash-chained log and ends with the chain verifying.
- **MIRA's audit agents now run inside every Run.** The runtime composition dispatches the shipped
  audit-agent roster and merges their findings with the deterministic controls under one shared
  deduplication, while the composition's Gate stays the single run-level decision — one
  `policy.decision` per Run, asserted.
- **`THY-30` — THY activity in the CLI.** Token and cost figures are now event-sourced, so
  `thymira` can render the plan, the delegation tree and per-agent usage without the client ever
  importing a runtime member.
- **`THY-31` — THY completes the data-science workflow end to end.** Execute now maps an Experiment
  agent's `ExperimentResult` onto a `schemas.Experiment` and folds the artifacts a delegation
  produced back onto the graph state, Summarize carries its typed `Recommendation` on `ThyOutput`,
  and `build_thy_graph`/`run_thy` accept a tool registry and context so a delegated agent can
  actually call a tool. Covered by a happy path and a failing run that ends FAILED rather than
  hanging.
- **`TOOL-27` and `TOOL-28` — the execution QA gates.** An end-to-end run exercising `run_python`,
  MLflow tracking, artifacts and the spill policy, whose audit comes back clean with every
  `artifact.created` digest matching the manifest; and the proof that sandbox enforcement is a
  recorded fact that drives MIRA's A19 finding.
- **`ASSUR-01` — Assurance bundle.** The audit report, the policy decision, an evidence index that
  resolves each finding back to the event, artifact digest or experiment it rests on, and a
  disclaimer a bundle cannot be constructed without.
- **`THY-33` — Lazy public imports.** Importing routing types or an agent that has no tools no
  longer drags in the runner, the tool bridge or `thymira.tools`.
- **`AUD-METHODOLOGY`, `AUD-RISK`, `AUD-EUAIACT` — the three MVP audit agents.** MIRA now ships a
  roster of audit-agent declarations (`thymira.mira.agents.load_default_specs()`): methodology
  (train/test discipline, leakage, validation, metrics), model risk (bias and risk indicators,
  model limitations) and EU AI Act, the last citing the seeded regulation store through
  `search_regulation`. Each is data, not code — one more YAML declaration adds an agent. Their
  findings are evidence for the Policy Engine, never an authorization, and each prompt says so.
- **`API-GOV` — Governance API routes.** The API exposes the pending approvals a Run is waiting on
  and resolves them through the `ApprovalService`, rather than re-folding the event log at every
  call site.
- **`RISK-01` — Deterministic risk-classifier override layer.** `thymira.agents.risk` treats an
  LLM risk classification as a proposal and applies eleven deterministic overrides that only ever
  tighten it — never lower a level, never authorize — floored by MIRA's `RiskAssessment`. It
  returns the `RiskProfile` the Gate consumes, records one `model.selected` and one `agent.message`
  per call, and fails closed to high-risk/needs-review on any model-path error.
- **`HITL-01` — Async human-approval flow.** `thymira.policies` gains the `pending_approvals` fold
  and `PendingApproval`, the `ApprovalService` protocol with `LocalApprovalService`, and a deferred
  `Gate` mode that records `human.approval_requested` and returns an unresolved decision for
  out-of-band resolution — so a `REQUIRE_HUMAN_REVIEW` no longer needs a synchronous answer.
- **`CTRL-MVP` and `TOOL-24` — Four more deterministic MIRA controls.** A8 (human review resolved
  before a run closes), A15 (risk classified before the first tool call), A19 (the run's recorded
  confinement was partial or unusable) and A24 (model routing respected the role floor), all
  registered in `CONTROLS` and recomputed from the hash-chained log.
- **`KB-02` — Regulation corpus and requirements-to-controls mapping.** An English
  requirements→controls mapping, a seed regulatory and methodology corpus, and
  `scripts/ingest_regulation.py`, which ingests both into a `RegulationStore` the audit agents can
  cite as external evidence.
- **`RA-CORE-06` — Live runtime-graph wiring.** The API now executes real Runs by default:
  `ThySubgraph` and `MiraSubgraph` adapt the real ThyGraph and the bounded MIRA audit to the
  `Subgraph` protocol, and `build_runtime_graph_factory` composes THY -> MIRA -> Gate per Run.
  `build_default_deps` no longer fails closed with an unconfigured graph factory, and the recorded
  provenance hash is the real composed-graph hash.
- **`CLI-AUDIT` — Audit command.** `thymira audit RUN_ID` runs MIRA's deterministic audit through
  the API and renders every control, its findings and the final decision; `--json` prints the raw
  audit report.
- **`TOOL-25` — Experiment command.** `thymira experiment RUN_ID` lists a Run's experiments with
  their parameters, metrics, seed, tracker run and model artifact.
- **`THY-29` — Plan command.** `thymira plan RUN_ID` produces and shows a Run's ordered phase plan
  through the API without entering execution.
- **`RA-QA-02` — API contract test suite.** Every registered route, response model and error
  envelope is now pinned by a contract suite, so an unreviewed change to the HTTP surface fails.
- **`TOOL-29` — Tool descriptor and MCP round-trip contract test.** Every registered tool projects
  to a valid MCP descriptor, and a call through the MCP surface records the same evidence as the
  native `ToolManager` path.
- **Runtime-spine integration coverage.** The API/CLI path is now exercised through local
  persistence for completed, failed, warning and human-approval execution outcomes.
- **`RA-API-09` — API observability seam.** The FastAPI boundary now creates an OpenTelemetry
  request span with no-op export by default and can send spans to an OTLP collector through
  standard environment variables; Run provenance accepts the composition graph hash.
- **`RA-CLI-04` — Human approval commands.** The CLI now supports `thymira approve` and
  `thymira reject` with an optional actor and note, and renders the resulting Run status.
- **`RA-CLI-03` — Run event history and streaming.** The CLI now shows buffered Run events and
  can follow the API event stream with reconnection from the last received sequence.
- **`RA-CLI-02` — Run resume command.** The CLI now supports `thymira resume RUN_ID` and
  renders the resulting run state after requesting continuation from the API checkpoint.
- **`RA-CLI-01` — Run listing and richer status.** The CLI now lists runs with `thymira runs`
  and shows tool, experiment and final-decision details in `thymira status` when supplied by
  the API.
- **`RA-API-05` — Audit and experiment read endpoints.** The API now exposes deterministic
  MIRA audit reports with the recorded run-level policy decision, plus persisted experiments
  in insertion order, with project scoping and integrity errors.
- **`RA-API-08` — Consistent API errors, safe retries and actor resolution.** API writes now use
  stable `problem+json` mappings, optional `Idempotency-Key` deduplication for Run creation and
  a single MVP actor resolver with a trusted system default.
- **`RA-API-06` — Run planning endpoint.** Clients can request the canonical ordered phase plan;
  the proposal is recorded by the Policy Engine and Gate as a human approval request before
  execution.
- **`RA-API-04` — Event history and SSE streaming.** The API now exposes ordered, cursor-based
  Run events as JSON and can stream new events over Server-Sent Events with reconnect support.
- **`THY-19` — ML agent, and the first sub-agent -> helper delegation.**
  `thymira.thy.agents.ml.ML_AGENT_SPEC` trains a model through `run_python`/`mlflow_*` and reports
  an `MLResult`; when it signals `needs_tuning_help` with a `tuning_objective`, the new
  `run_ml_agent` wrapper — not the model, and not a tool call — delegates to
  `ML_TUNING_HELPER_SPEC` (`max_depth=2`) through the existing `Delegator`, exactly once, recorded
  as an `agent.message`. `AgentRunner`'s tool-calling loop has no path for an agent to trigger a
  second delegation itself mid-turn, so this reads the primary run's structured output afterward
  instead — the same pattern `execute_node` already uses for THY's own plan. The primary run's
  `MLResult` is returned unchanged; the helper's `TuningResult` is never merged into it, since
  nothing in the deliverable asks for that. FINAL-tier, not part of `ThyAgentKind`.
- **`THY-21` — Visualization agent.** `thymira.thy.agents.visualization.VISUALIZATION_AGENT_SPEC`
  renders a plot through `run_python` and persists it through `write_file`, which grew an
  optional `kind` argument: passed, it also registers the written file as a real `Artifact`
  (`ArtifactStore.save_bytes`), the only way a tool without its own artifact-producing logic can
  produce one — without it, this task's own "Done when" (a `PLOT` artifact with a sha256 in the
  manifest) was not reachable with the `run_python`/`write_file` pair the deliverable names.
  `PlotResult` (one `Plot` per rendered figure: `artifact_id`, `caption`) is its output schema.
  FINAL-tier, same not-yet-`ThyAgentKind`-dispatchable state as THY-14/18/20.
- **`THY-20` — Data Quality agent.**
  `thymira.thy.agents.data_quality.DATA_QUALITY_AGENT_SPEC` reads a dataset through `read_file`
  and, when a fixed check is not enough, computes through `run_python`, returning a
  `DataQualityReport` (leakage risks, imbalance, drift signals, sensitive attributes) — narrative
  findings the model itself judges, the same way `CodeResult.artifacts` is self-reported rather
  than tool-verified. Verified against a real CSV: the agent flags a leakage risk and a declared
  sensitive attribute on a scripted fixture.
- **`THY-18` — Statistics agent.** `thymira.thy.agents.statistics.STATISTICS_AGENT_SPEC` runs one
  of five deterministic SciPy-backed tests (`welch_t`/`anova`/`chi_square`/`correlation`/
  `normality`) through the real `run_statistics` tool (P3) against a dataset registered via
  `register_dataset`, and reports a `StatsResult`: the tool's own `statistic`/`p_value` plus the
  model's narrative `effect_size`/`assumptions`/`caveats` — the numbers are never invented, the
  interpretation always is. FINAL-tier, not part of `ThyAgentKind`.
- **`RA-API-03` — Resume and human-decision endpoints.** The API can now resume a Run from its
  verified checkpoint and exposes approve/reject routes that record the human decision through
  the existing Gate before continuing or blocking the Run.
- **`RA-CORE-08` — Durable Run resume.** `RunService.resume` verifies the append-only event log,
  continues the LangGraph checkpoint without replaying completed child nodes, and fails closed
  on tampered or missing checkpoint state.
- **`RA-API-02` — Run endpoints.** The FastAPI boundary can now create, inspect and cursor-list
  Runs for an explicitly configured project, resolving optional sessions safely and returning
  stable problem+json errors for invalid scope or unknown Runs.
- **`RA-API-01` — FastAPI composition root.** The API now exposes a typed `RuntimeDeps`
  dependency container, a local MVP builder, `/healthz`, application-level validation errors,
  and the stable route groups reserved for the remaining API tasks. The default dispatcher fails
  closed until the real THY/MIRA graph adapters are injected; no fake execution path is added.
- **P3 execution and evidence tools.** The tool surface now includes deterministic dataset
  profiling, safe SQL and statistics, custom sandboxed experiments, model inspection and audit
  evidence, detached worktrees, and an MCP protocol adapter, while retaining policy-gated manager
  execution and portable local tracking.
- **`RA-CORE-10` — Injectable execution dispatcher seam.** `RunService` can submit newly created
  Runs through an injected `ExecutionDispatcher`; the MVP `InlineDispatcher` compiles and runs
  the composed THY -> MIRA -> Gate graph synchronously, while reserving `background` for the
  future queue-backed dispatcher.
- **`RA-CORE-03` — Usage ledger context in Gate reviews.** The composed runtime graph now passes
  the authoritative per-Run usage snapshot (requests, tokens and measured or unknown cost) to
  Gate reviews and preserves the same snapshot in the checkpointable `RuntimeState`.
- **`THY-17` — Coding agent error recovery (diagnose a failed run, bounded retry).**
  `prompts/coding.md` now tells the agent to read `run_python`'s `stderr`/`exit_code` and fix the
  specific problem before retrying, never repeat the same code unchanged, and stop after a second
  failure rather than loop forever — the retry itself needed no new mechanism, `AgentRunner`'s
  existing multi-turn loop already hands a failed tool result back to the model within the same
  call. What *did* need one: `Delegator.delegate` (THY-08) hardcoded `"agent exceeded max_turns"`
  for every `FAILED` result, and the `UnexpectedModelBehavior`/`UsageLimitExceeded` PydanticAI
  raises on that path carries nothing useful (verified empirically — just "Exceeded maximum
  output retries (N)", no message history). `Delegator` now reads back the events its own
  `AgentRunner.run()` call just appended and uses the last failed tool call's real `error` as the
  diagnosis, falling back to the generic message only when the agent never reached a tool call at
  all — a delegation-layer fix, not Coding-specific, so every agent's failure carries its real
  cause from now on. Verified with the real `run_python` sandbox, not a fake: a script that
  divides by zero, corrected on the second attempt, completes; a script that keeps failing past
  the turn budget ends the `Task` `FAILED` with `ZeroDivisionError` (the tool's own `stderr`) in
  `task.error`, never the generic message and never an unhandled exception.
- **`RA-CORE-06` — Runtime composition graph.** Core now composes the injected THY and MIRA
  subgraphs in order, routes their findings through the Gate, and persists completed, blocked or
  human-review lifecycle outcomes through the single Run transition writer.
- **Auditable Run lifecycle control.** Ordinary Run progress now goes through the single-writer
  `RunController` and the event-backed `LocalRunStore`, preserving one authoritative transition
  history for `RunService` callers.
- **`KB-01` — local regulation knowledge base.** MIRA now provides a hash-verified local JSONL
  regulation store with deterministic keyword search and a read-only `search_regulation` tool
  that remains authorized and recorded by the Tool Manager.
- **`MIRA-01` — complete audit-agent fan-out.** MiraGraph now runs injected audit-agent specs
  sequentially after deterministic controls, merges their candidate findings with preflight and
  deterministic evidence, then deduplicates and reviews the complete set through one Gate call.
- **`MIRA-02` — bounded audit-agent runner.** MIRA now executes an injected
  `AuditAgentSpec` through code-owned model routing and Tool Manager boundaries, returns only
  normalised candidate findings, and records the agent/model lifecycle without deciding policy.
  Its evidence projection is redacted, surface-aware, size-bounded, and limited to hash-verified
  excerpts explicitly cited by the deterministic audit report.
- **`GOV-01` — MIRA audit-agent contract.** MIRA now exposes immutable `AuditInput`,
  `AuditAgentOutput`, and `AuditAgentSpec` models. Run evidence is assembled with strict Run-id
  ownership checks, audit-agent declarations load deterministically from validated YAML, and the
  public output factory rejects findings outside the spec's declared framework while keeping
  findings as non-authoritative evidence.
- **Reusable CLI presentation.** The welcome screen keeps the existing THYMIRA banner and
  adds a concise getting-started guide, while run, status and error output now share aligned
  terminal formatting.
- **`RA-CORE-07` — LangGraph checkpoint persistence.** Core now saves and restores the latest
  graph checkpoint through the backend-agnostic `CheckpointRepository`, enabling a paused run to
  continue from its saved state.
- **`RA-CORE-05` — Neutral runtime graph contract.** Core now exposes a validated,
  checkpointable `RuntimeState` plus structural `Subgraph` and injected `SubgraphDeps` contracts,
  keeping THY and MIRA independent while preparing their future composition.
- **`RA-CORE-04` — Execution idempotency and lineage invalidation.** Core now derives stable
  execution keys, rejects duplicate in-memory executions, and cascades artifact invalidation
  through declared downstream dependencies while preserving invalidation reasons.
- **`THY-14` — Data agent (inspect + profile dataset).** The first real specialist `AgentSpec`
  in production, `thymira.thy.agents.data.DATA_AGENT_SPEC` — named `data` to match
  `ThyAgentKind.DATA.value` exactly, since `execute_node` resolves a delegated task by that
  string. `tool_allowlist=("list_files", "read_file")` (`TOOL-09`); `DataProfile` (`columns`,
  `dtypes`, `missing`, `target_candidates`) is its output schema, replacing the thinner
  test-only `DataProfileOutput` stand-in used since THY-03. `data_agent_catalog()` builds the
  ready-to-use `AgentCatalog` entry, reading `prompts/data.md` from disk. Verified end to end
  through `AgentRunner` directly (`AgentContext.tool_registry`/`.tool_context` are still unset in
  `execute_node`, so wiring this into `ThyGraph`'s dispatch is a separate, later concern): a
  `ScriptedProvider` calls `list_files` then `read_file` against a real CSV fixture through the
  real `ReadFile`/`ListFiles` tools (`TOOL-09`), and only those two are ever exposed even when
  other tools are registered alongside them.
- **`THY-15` — Coding/Execution agent (generate + execute Python, read result).**
  `thymira.thy.agents.coding.CODING_AGENT_SPEC` — named `coding` to match
  `ThyAgentKind.CODING.value` — follows the same drop-in shape as THY-14, with
  `tool_allowlist=("write_file", "run_python", "read_file", "git_diff")` (`git_commit` is
  deliberately excluded: no agent is wired to commit automatically). `CodeResult` (`stdout`,
  `exit_code`, `artifacts`) is its output schema. Verified end to end through `AgentRunner` with
  the real `WriteFile`/`RunPython`/`ReadFile`/`GitDiff` tools, no direct `subprocess` use: a
  `ScriptedProvider` drives the write -> run -> read loop the roadmap deliverable describes — the
  script is persisted, executed for real through `RunPython`'s sandbox, and its own output file is
  read back to confirm what happened before it is reported — and `tool.started`/`tool.completed`
  land on the event log.
- **`THY-16` — Experiment agent (create experiment, log metrics/params, save artifacts).**
  `thymira.thy.agents.experiment.EXPERIMENT_AGENT_SPEC` — named `experiment` to match
  `ThyAgentKind.EXPERIMENT.value` — follows the same drop-in shape as THY-14/THY-15, with
  `tool_allowlist` naming the five real MLflow tracker tools (`mlflow_start_run`/`log_param`/
  `log_metric`/`log_artifact`/`end_run`, `TOOL-17`) plus `run_python`. `ExperimentResult`
  (`parameters`, `metrics`, `seed`, `model_artifact_id`, `tracker_run_id`) deliberately mirrors
  what the tracker tools actually persist — `parameters: dict[str, str]`, never `dict[str, Any]`,
  since MLflow only ever logs string values; coercing those into the richer
  `thymira.schemas.Experiment` record is a separate, later mapping this task does not own.
  Verified end to end through `AgentRunner` with the real `mlflow_*` tools: a `ScriptedProvider`
  starts a tracker run, logs a parameter/metric/artifact and ends it, and the run this task's own
  test reads back from the tracker afterwards (not just the agent's reported output) matches.
- **`THY-11`/`THY-12`/`THY-13` — Plan, Execute and Summarize, ThyGraph closed end to end.**
  All four ThyGraph nodes are now real. **Plan** (`thymira.thy.nodes.plan.plan_node`) calls THY's
  own (FRONTIER, `task='plan'`) model — built directly on `routed_model`, never
  `AgentRunner`/`Delegator`, since THY has no `AgentSpec` — to produce a `PlanOutput` (a
  non-empty, validated `tuple[AgentTask, ...]`), then gates it through
  `gate.check_action(action_type='plan.proposed')` and `allows_execution(decision)` — the same
  single rule `ToolManager` uses for a tool call (C-9), with no approval object — so a plan the
  decision does not allow to execute (`BLOCK`, or `REQUIRE_HUMAN_REVIEW`, which no synchronous
  approver answer can satisfy) records the reason on `state.error` and never advances past
  `ThyPhase.PLAN`. Under the shipped default policy (no `ActionRule` for `plan.proposed`) that
  decision is `REQUIRE_HUMAN_REVIEW`, so the graph halts at Plan until `HITL-01` or an explicit
  `plan.proposed` PASS rule; the approver's answer is recorded as a `human.approval` event, evidence
  only.
  **Execute** (`thymira.thy.nodes.execute.execute_node`) extends THY-09's own seam with
  dependency order: `AgentTask.id`/`.depends_on` (new fields) let a task whose dependency did not
  COMPLETE be recorded SKIPPED — via a real `Task`, never silently dropped — while independent
  tasks still run; a FAILED task keeps `state.phase` at `EXECUTE` (it never claims to have
  reached `SUMMARIZE` for a phase it didn't). **Summarize**
  (`thymira.thy.nodes.summarize.summarize_node`) keeps the deterministic/narrative split "the LLM
  proposes, code authorizes" calls for: `compare_experiments(experiments, metric=...)` (code)
  alone picks `best_model`/`metrics`; THY's own (FRONTIER, `task='synthesize'`) call only ever
  supplies the `tradeoffs`/`limitations` narrative (`SynthesisNarrative`), merged into the final
  `Recommendation` and written as `analysis.md` (`ArtifactKind.REPORT`) via `ArtifactStore`.
  Plan and Execute are the two nodes whose outcome can halt the graph: `add_conditional_edges`
  (not a plain edge) routes a rejected plan or a failed task straight to `END` instead of the
  next node — `graph_definition_hash()` now folds `_CONDITIONAL_EDGES` in alongside
  `_GRAPH_NODES`/`_GRAPH_EDGES` for exactly that reason. `build_thy_graph`/`run_thy` grow three
  more optional parameters (`gate`, `artifact_store`, `metric`); each of Plan/Execute/Summarize
  degrades to its bare `state.advance()` placeholder when its own dependency is omitted, so every
  existing caller that never passed one keeps its exact original behavior. Caught by this task's
  own tests before it shipped: Execute's happy path advanced `state.phase` to `SUMMARIZE` even on
  a failure, which the conditional routing masked (the graph still halted correctly) but left
  `ThyState.phase` claiming a phase the run never reached — fixed to only advance on success.
- **`THY-10` — Inspect node + project-context loader.**
  `thymira.thy.context.load_project_context(project_dir)` parses `.thymira/config.yaml` into
  `ProjectConfig` and reads `.thymira/context.md`; a project with no `.thymira` directory (or a
  missing file inside it) degrades to an empty `ProjectContext` rather than raising — a malformed
  `config.yaml` that *is* present still raises, since that is a real error, not an absence.
  `thymira.thy.nodes.inspect.inspect_node(project_dir)` builds the real Inspect node from it,
  seeding `ThyState`'s three new fields (`domain`, `governance_frameworks`, `project_context`).
  Verified against `examples/credit-risk`: domain `credit_risk`, frameworks `EU_AI_ACT` +
  `CREDIT_RISK`. Dataset facts via the Data agent / tool bridge (the roadmap's fuller deliverable
  text) are not built here — THY-14 and the concrete tools they'd need don't exist yet, and this
  task's own acceptance test doesn't exercise them either; additive later through the same
  `state.model_copy(update=...)` shape this node already uses.
  `build_thy_graph`/`run_thy` grow one more optional parameter, `project_dir`: omitted, Inspect
  stays THY-09's bare phase-advance placeholder (every existing caller keeps its exact original
  behavior); given, Inspect becomes this real node.
- **Run usage ledger.** `thymira.core.UsageLedger` accumulates measured requests, tokens and
  model cost for a run, preserves unknown costs as unknown, and exposes the snapshot used by
  approval requests and budget checks.
- **`THY-09` — ThyState + ThyGraph scaffold, closed.** `ThyState` gains the fields the deliverable
  named but the earlier partial pass didn't: `plan`/`completed: tuple[AgentTask, ...]`,
  `agent_messages: tuple[Task, ...]`, `usage: RunUsage` (`thymira.agents.usage`, THY-07 — a plain
  mutable dataclass, verified to round-trip through `model_dump()`/`model_validate()` and to
  survive LangGraph's in-process node hand-off natively, no `arbitrary_types_allowed` needed) and
  `policy_signals: tuple[PolicyDecision, ...]`. "Surface source" (the roadmap's deliverable text)
  is `run.id`, already on `ThyState.run` — nothing new needed for it.
  `thymira.thy.graph.graph_definition_hash()` hashes the same `_GRAPH_NODES`/`_GRAPH_EDGES` data
  `build_thy_graph` wires the graph from, so the two can never drift and adding a node changes the
  hash by construction. Execute is the node that closes THY-09's own gap — no longer
  `state.advance()` alone, it dispatches every `state.plan` item through `Delegator` (THY-08),
  which drives `AgentRunner` (THY-03) for real; verified end to end with a `ScriptedProvider`
  reaching `Summarize` with `completed`/`agent_messages`/`usage` populated and one `agent.message`
  event on the log. Inspect, Plan and Summarize stay placeholders — their real work belongs to
  THY-10/THY-11/THY-13, three separately sized tasks this scaffold does not preempt.
  `build_thy_graph`/`run_thy` now take a `catalog`/`event_log` (nothing outside `runtime/thy`
  called either function yet, so this is not a breaking change to any consumer).
- **`THY-08` — Delegation contract: hub-and-spoke `agent.message` + depth guard.**
  `thymira.agents.delegation.Delegator.delegate(parent_agent, spec, objective, depth)` is the
  only way one agent reaches another: it creates a `Task`, runs it through `AgentRunner`, and
  records the outcome as one `agent.message` event carrying a structured summary
  (`AgentResult.output.model_dump_json()`) — never chain-of-thought, since this runtime never
  captures a model's reasoning as data to begin with. `depth` is always the *caller's own*
  depth, never the target's: `.delegate()` always creates its child at `depth + 1`, so a
  sibling relationship (peer-to-peer) is unrepresentable through this API, not merely detected
  and rejected. `DepthGuard.check` then refuses that child depth against the target
  `AgentSpec.max_depth` (`THY-01`) — orchestrator (0) → sub-agent (1) → helper (2) is the MVP
  shape. `Delegator`'s only public method is `delegate` (verified: no second method exists that
  could address an already-running agent directly).
- **`TOOL-03` — Self-describing tools and fail-closed argument validation.** The Tool protocol now
  exposes a human-readable description and an optional Pydantic arguments model. Its JSON schema
  is reused by the PydanticAI bridge, while the Tool Manager validates every declared model before
  policy evaluation and records invalid calls as `FAILED` without executing them. **`TOOL-01`** is
  also closed with sandbox round-trip, enum rejection, omitted-field, and public-export tests.
- **Durable local Run and Session lifecycle.** Run and Session repositories now persist canonical
  JSON with pagination; RunService records provenance and hash-chained lifecycle events through
  EventStore and UnitOfWork seams that PostgreSQL can implement later.
- **`THY-07` — RunUsage / UsageLimits: shared budget charged per delegated call.**
  `thymira.agents.usage` adds `UsageLimits{max_cost_usd, max_requests, max_tokens,
  max_tool_calls}` — **the single definition of that type for the whole runtime**;
  `RA-CORE-03`'s `UsageLedger` imports it from here, never the reverse, since `thymira.core`
  sits above `thymira.agents` — and `RunUsage`, a mutable run-scoped accumulator (not a frozen
  contract: it has to be shared and charged into repeatedly across every delegated call in a
  run). `.charge(response)` sums the *real* `LLMResponse.cost_usd`/tokens a provider reported,
  never a token estimate; `.charge_tool()` counts one tool call. Either raises
  `UsageLimitExceededError` (named with the project's `*Error` convention, not the roadmap
  text's bare `UsageLimitExceeded`) the instant a configured ceiling is crossed — `routed_model`
  (`THY-02`) and `build_agent_tools` (`THY-04`) both charge into `AgentContext.usage` when set,
  and the exception propagates out of `AgentRunner.run` uncaught: a budget breach is a run-level
  concern, never folded into a per-task `FAILED` result. `AgentContext` grows one more optional
  field, `usage`; unset, nothing is charged and no existing test needed a change.
- **`THY-06` — Request/prompt provenance: record what the model actually saw.**
  `thymira.agents.prompt_provenance.record_prompt(ctx, assembled, choice)` persists the
  *redacted* rendered prompt (`AssembledPrompt.system` + `.user`) as a content-addressed
  `ArtifactKind.LOG` artifact and appends `model.selected` enriched with `{prompt_sha256,
  surface_seqs}` — never model reasoning, only what was actually sent, and only after
  `redact()`. `routed_model` (`THY-02`) calls it instead of its own plain `model.selected`
  append whenever `assembled`, `artifact_store` and `agent_id` are all given. This wiring also
  closes a gap left open since `THY-05`: `AgentRunner` (`THY-03`) now actually builds an
  `AssembledPrompt` via `PromptBuilder` before every run — `assembled.system` becomes the
  `Agent`'s `instructions` and `assembled.user` (history + `task.objective`) is what
  `run_sync` sends, not `task.objective` alone. `AgentContext` grows one more optional field,
  `artifact_store`: unset, behavior is byte-for-byte `THY-03`'s original shape (verified: no
  existing test needed a change); set, every real model call for the run gets a provenance
  artifact and an enriched `model.selected`.
- **`THY-05` — PromptBuilder over `current_surface` + prefix-stable system prompt.**
  `thymira.agents.prompts.PromptBuilder.build(spec, task, events) -> AssembledPrompt{system, user,
  surface_seqs}` reads only `current_surface(events)` for history (ADR-0006) — a shadowed event
  (one a `context.compacted` has folded away) never reaches the prompt, so compaction (`THY-25`)
  is a drop-in later without this module changing. `system` is a pure function of `spec.name`
  (byte-identical across two builds of the same spec with different tasks — verified), never of
  `task`/`events`, so a provider caching on the system prefix always hits. Design decision: no
  separate `SystemPromptCatalog` — the roadmap text names one, but `AgentCatalog.system_prompt`
  (`THY-01`/`THY-03`) already resolves `spec.system_prompt_ref` to file text, refusing at load
  time if missing; a second catalog would only duplicate that. `PromptBuilder` is built directly
  on `AgentCatalog`.
- **`THY-04` — Tool bridge: agent tool-calling through the Tool Manager + Gate under an
  allowlist.** `thymira.agents.tool_bridge.build_agent_tools(spec, ToolRegistry, ToolContext)`
  exposes as PydanticAI tools exactly `spec.tool_allowlist` — a tool the registry holds but the
  spec does not list is never built, so an agent cannot even attempt it. Each wrapped tool calls
  `ToolManager.execute` (fail-closed, gated through `Gate.check_capability`); a denied capability
  comes back as a bounded string the model can read and adapt to, never an exception, since
  `ToolManager.execute` already returns a `ToolExecution` on denial rather than raising.
  `AgentRunner` (`THY-03`) grows two optional `AgentContext` fields, `tool_registry`/
  `tool_context`: unset, a run has no tools (`THY-03`'s original shape, unchanged); set,
  `build_agent_tools` attaches them to the `pydantic_ai.Agent` it builds. Verified against a real
  `pydantic_ai.Agent` + `FunctionModel` driving a full tool-call round trip — the model's only
  route to a tool is the wrapped function, never `execute`, subprocess or the filesystem directly.
  No registered tool declares a parameter schema yet (the concrete tools — `run_python`, etc. —
  are still `todo`), so every bridged tool currently accepts an open JSON object; a per-tool
  schema is additive when a concrete tool needs one, not a change to this bridge.
- **`THY-03` — AgentRunner: generic spec-driven agent execution loop.**
  `thymira.agents.runner.AgentRunner.run(spec, task, ctx)` builds a real `pydantic_ai.Agent` from
  the spec's resolved system prompt and output schema plus `routed_model`, seeded only by the
  task (never the caller's full event thread — the isolated-context seam ADR-0005 calls for), and
  drives it to a validated `AgentResult{output, usage, task_status}`. The step loop bounded by
  `spec.max_turns` is PydanticAI's own output-retry loop (`Agent(retries=max_turns - 1)`):
  `routed_model` now answers a structured call through `LLMProvider.complete_structured` and
  raises `pydantic_ai.ModelRetry` on an invalid or failed completion, so PydanticAI redrives the
  next attempt itself rather than this module reimplementing that loop. `agent.started` is
  appended before the run and `agent.completed` after, on both outcomes; exhausting `max_turns`
  surfaces as `pydantic_ai.exceptions.UnexpectedModelBehavior`, caught and translated into
  `TaskStatus.FAILED` — never a silent drop. Closes a design gap found while starting this task:
  `AgentSpec.output_schema_ref`/`system_prompt_ref` were declared but never resolved (see the
  `THY-01` entry below); `AgentCatalog`/`load_agent_specs` (`thymira.agents.spec`) now resolve
  `system_prompt_ref` to a text file's contents and `output_schema_ref` to an imported
  `module.path:ClassName` `BaseModel` subclass, eagerly and refused at load time on either
  failing to resolve, and expose the resolved values via `AgentCatalog.system_prompt`/
  `.output_schema`. `AgentTask` in the roadmap prose is `thymira.schemas.Task` — the frozen,
  layer-compliant "unit of delegated work with its outcome" contract; `thymira.thy`'s own
  `AgentTask` (`THY-09`) cannot be imported here (`thymira.agents` sits below `thymira.thy` in the
  layer order) and is a different, THY-specific type.
- **`THY-02` — PydanticAI model binding over the router + LLMProvider.**
  `thymira.agents.model_binding.routed_model` builds a PydanticAI `Model` (via
  `pydantic_ai.models.function.FunctionModel`, so PydanticAI owns the streaming/event-iterator
  machinery) whose calls go through `routing.choose()`/`LLMProvider.complete()` directly, never a
  LiteLLM proxy — closing ADR-0001 dec.4's open integration point (resolution note added there).
  Every call appends a `model.selected` event before the request; `LLMResponse` tokens populate
  PydanticAI's `RequestUsage`. Verified end to end with a real `pydantic_ai.Agent` and an injected
  `ScriptedProvider`: no network call, exactly one `model.selected` event per request, tier
  respects the calling role's floor.
- **`THY-01` — AgentSpec: sub-agent declaration as data + catalog loader.** `thymira.agents.spec`
  adds `AgentSpec` (name, role, task_kinds, tool_allowlist, tier, max_turns, max_depth,
  system_prompt_ref, output_schema_ref), `AgentCatalog`, and `load_agent_specs(dir, *,
  known_capabilities)`, which refuses at load time — never at agent-run time — a spec whose
  `tool_allowlist` names a tool capability outside the set the caller passes in (built from the
  populated `ToolRegistry`, never hard-coded here). Declares no PydanticAI agent and makes no LLM
  call: this is the data seam ADR-0004 dec.4 fixes, so `THY-02`/`THY-03` add execution behind it
  without touching this contract. Not `AuditAgentSpec` (`GOV-01`, MIRA's own type) — the two are
  intentionally separate.
- **Roadmap task status, machine-checked.** Every task in `docs/roadmap/` now carries a
  `todo` / `partial` / `done` status, so the documents answer *does this already exist?* before
  anyone starts work. `just check-roadmap` (`scripts/check_roadmap.py`, wired into `just check`
  and the pre-commit hooks) fails when the summary table, the detail section and the per-member
  table disagree on a task's title, tier, owner, size, status or dependencies, when a declared
  count, the aggregate status line or the load-per-member table no longer matches the tables,
  when a dependency does not exist, when an MVP task depends on FINAL work, or when the
  dependency graph contains a cycle.
- **Correction C-7 — `A1`–`A18` is a closed control namespace.** A spec audit of the 14 unblocked
  MVP tasks found that the `A11` collision fixed in `TOOL-24` was one of several: five modelling
  ids (`A11`, `A12`, `A13`, `A14`, `A18`) are used in the roadmap with meanings that disagree with
  the inherited definitions in `docs/legacy/trazabilidad.md`, while the shipped `credit_risk.yaml`
  still speaks the inherited vocabulary. `A14` is the sharpest: it was widened into a compound
  control, so a leakage-free model that merely skipped a baseline comparison raises `A14`, and
  `CR-101` — which sets no `min_severity` — blocks the run recording *"evidence of train/test
  leakage"* as the reason. C-7 records the rule: a new check takes an id at `A19` or above; an
  inherited id keeps its meaning; a compound control is split, not widened.
- **The three reserved control meanings have owners.** `CTRL-LINEAGE` (`A12`), `CTRL-SELECTION`
  (`A13`) and `CTRL-REPORT` (`A18`) are filed as FINAL P4 tasks behind `MIRA-01`. Reserving a
  number is not the same as keeping its meaning: an inherited control with no task is how a
  meaning disappears without anyone deciding to drop it. **128 tasks, 81 MVP.**
- **The catalogue now says what it deliberately does not cover.** Baseline **§24 Infrastructure**
  (Kubernetes, KEDA, RabbitMQ, the Sandbox Manager as a deployed service) has no task and needs
  none yet — it changes no contract and no seam, and `TOOL-06`'s Sandbox protocol already holds
  the one that matters. An absent subsystem and a descoped one look identical in a task list, and
  only one of them is a decision, so it is recorded as deferred. **§22 Security** and **§23
  Observability** were checked and are covered — the permission-separation model lives in
  `ToolCapability`, `tool_allowlist` and the sandbox modes, and `RA-API-09` installs the
  OpenTelemetry seam with a no-op exporter so the backends are configuration, not a retrofit.
- **`CONTRACT_VERSION` stays at `0.2`.** The sandbox pair is part of 0.2 and the contract's own 0.2
  entry documents it, so the recorded version already describes the shape consumers get. A version
  number names what a version contains, not when the sentence describing it was written; bumping
  to 0.3 would make every consumer re-pin for a change that never happened. `TOOL-01` keeps only
  the round-trip tests.
- **`findings_default_decision` is documented as a floor, not a fail-open gap.** It reads like one
  next to the two `REQUIRE_HUMAN_REVIEW` defaults and has been raised as one more than once, but
  `load_policy_stack` always loads the base policy first and its ladder covers every severity
  above `LOW` (`GOV-101` CRITICAL → BLOCK, `GOV-102`/`GOV-103` HIGH → REQUIRE_HUMAN_REVIEW,
  `GOV-104` MEDIUM → WARNING). A test now pins that only a `LOW` finding ever reaches the default.
- **C-7 applied to the documents.** Every inherited id now carries its inherited meaning and every
  displaced check has its own number: `A11` is split integrity again (leakage indicators become
  `A20`), `A14` is test-once and nothing more (the trivial-baseline half becomes `A22`, kept out
  of any leakage rule), fold-local CV becomes `A21`, reproducibility metadata `A23`, and the
  routing-floor control moves from the non-numeric `A-ROUTE` to `A24`. `A12`, `A13` and `A18`
  revert to their inherited meanings, which **no task builds** — they are recorded as reserved,
  because an inherited meaning quietly dropped is the same defect in reverse. Each affected task
  carries a **Control ids** note explaining the change so it is not renumbered back.
  `runtime/policies` is deliberately untouched: `CR-101` becomes `[A11, A14, A20]` and retires
  `LEAKAGE-001`, but only in the pull request that registers `A20` in `CONTROLS` — pointing a live
  rule at a control nothing emits would just move the dangling reference. That change also carries
  the `CRX-103` edit, the rewrite of the live assertion in `tests/thymira/test_policies.py`, and
  P4's decision on `CR-101`'s missing `min_severity`.
- **Roadmap task specs corrected before parallel implementation.** `THY-01` no longer claims MIRA
  reuses its `AgentSpec` — `GOV-01` ships a distinct `AuditAgentSpec`, and both tasks are
  unblocked at once, so each now names the other's type as deliberately separate. `UsageLimits` is
  defined once, in `thymira.agents.usage` (`THY-07`), and imported by `RA-CORE-03`'s `UsageLedger`
  instead of being declared twice in two layers that cannot import each other. `THY-03` no longer
  reads `spec.output_schema` (`THY-01` defines `output_schema_ref`), and the ref→type resolution
  is specified. `load_agent_specs` takes the capability set as a parameter, so its "raises on an
  unknown capability" criterion is writable without hard-coding a tool list.
- **The sandbox-confinement MIRA control is `A19`, not `A11`** (`TOOL-24`). `A1`–`A18` is the
  closed namespace inherited from the thesis meta-auditor, where `A11` is train/test leakage —
  and `credit_risk.yaml`'s `CR-101` already maps `A11` to **BLOCK** with the reason *"evidence of
  train/test leakage"*. Since C-3 has every MVP run report `partial` confinement, filing the
  control as `A11` would have blocked every MVP credit-risk run and recorded a false statement
  about the data as the reason. `check_roadmap.py` now also reports a domain whose declared
  count line it cannot parse, instead of dropping that domain from the check.
- **ADR-0007 accepted**: compaction is atomic — one `context.compacted` append, no start/end
  bracket. `EventType` keeps exactly one compaction value, so no contract slot is spent on a state
  that cannot occur. The decision is propagated: ADR-0006's superseded bullet is struck through in
  its own file, `THY-25` no longer asks the writer to emit an opening event, and `CMP-01` drops the
  bracket-ordering half of its control (its acceptance criterion asked for a test that cannot be
  written).
- **Correction C-6 applied**: `GOV-02` folded into `TOOL-01` and `CTRL-SANDBOX` into `TOOL-24`
  (125 tasks, 81 MVP). The schema pair disagreed on the values of a frozen enum and on whether
  the MVP may report `SandboxEnforcement.FULL`; whichever task was picked up second would have
  overwritten the first.
- **Log vs surface in the Event contract** (ADR-0006). `Event.surface` (`EventSurface`:
  `model_visible` | `log_only`, defaulting to `log_only`) records whether an event's payload may
  ever be shown to a model, and the new `context.compacted` event type names the seqs a
  compaction shadowed. `thymira.events.derive_surface` / `current_surface` fold the log into
  `SurfaceState` (`current` | `shadowed` | `log_only`); shadowing is **derived, never stored**, so
  compaction can shrink what a model sees without ever editing a chained event. Adopted from
  DeepSeek Harness (ADR-0005).
- `SessionService` with explicit session resolution and a repository boundary ready for a
  PostgreSQL implementation.
- `RunService` with Run lifecycle transitions and an injected repository boundary.
- `RunGraphState` and its initial builder for the runtime-to-LangGraph boundary.
- Frozen MVP Run API v0.1 boundary models and endpoint contract for creating, retrieving,
  resuming and reading events from Runs.
- `thymira` CLI welcome screen with terminal-safe branding, discoverable root help, an
  installed-package `--version` option, and HTTP-backed `run` and `status` commands with clear
  handling for unavailable APIs, missing runs and invalid responses.
- `thymira.tools` — typed Tool API, tool registry and policy-gated Tool Manager with redacted
  arguments and hash-chained tool lifecycle events.
- `thymira.thy` — typed ThyGraph contract and compiled execution skeleton for the THY orchestrator.
- `thymira.mira` — the partial `MIRA-01` MiraGraph runs audit preparation, deterministic
  preflight and A1-A17 controls, merges their findings once, and records one aggregate Gate
  decision without writing artifacts or Run state. The audit-agent fan-out remains explicitly
  pending `GOV-01` and `MIRA-02`.
- `thymira.policies` — `BudgetRule` and `ModelRule`, and `Policy.budget_rules`/`.model_rules`
  (defaulted empty), ported from `main` onto the `Policy` model this branch keeps (`GOV-03`,
  `docs/roadmap/product-final.md` correction C-10). Both fields overlay before the base policy
  like every other rule list; only the deciding logic (`POL-01`, `POL-02`) remains FINAL work.
  `base`'s policy hash is now pinned by a regression test against future silent field additions.
- `thymira.mira`: the local `credit-risk` E2E closeout proves the first MIRA phase end to end:
  preflight, risk, packs, controls, findings, bounded intents, scripted immutable context,
  deterministic authorization, simulated approval, and the sole RunController transition path.
  It verifies JSONL integrity and tamper detection, `run.json` reconstruction, and immutable
  historical context access without connecting snapshots to THY or adding automatic updates.
- `thymira.core`: `MiraControlPlane` and `RunController` implement the first bounded MIRA
  control-plane path. MIRA intents are deterministically evaluated and recorded by the Policy
  Engine and Gate; only an exact, unexpired, single-use authorization context (and matching human
  approval when required) can write `run.transitioned`. `run.json` now rebuilds its operational
  state from those authoritative events without claiming a multi-file transaction.
- `thymira.mira` — `DecisionContextAnalyst` creates bounded, structured and evidence-grounded
  MIRA context snapshots through the shared LLM provider. Each explicit local context snapshot
  is immutable and recorded as `mira.context_created`; it neither authorizes an effect nor
  reaches THY.
- `thymira.mira` — `MiraAuditOrchestrator` executes the deterministic MIRA sequence from an
  immutable Run snapshot and returns assessments, bindings, controls, findings, reports, and
  bounded review intents without authorizing or persisting an effect.
- `thymira.mira` — deterministic preflight assessment with reviewed base-risk rules,
  reproducible pack applicability, and evidence presence/integrity/freshness controls for the
  `methodology-base` and `credit-governance` packs. These MIRA results do not authorize effects.
- `thymira.state` — `LocalRunStore` persists a single-writer Run under local JSON/JSONL,
  verifies its event chain on open, uses event-derived optimistic versions, and atomically
  replaces the reconstructible `run.json` projection.
- **Contract v0.5:** immutable, output-bounded MIRA decision contexts whose statements cite
  supplied evidence or are explicitly marked unknown, pinned to a Run version and event-log head.
- **Contract v0.4:** immutable MIRA activity profiles, inherent-risk assessments pinned to one
  profile version, reproducible pack bindings, and evidence-based control evaluations. Audit
  dispositions are now explicitly distinct from authorization decisions.
- **Contract v0.3**: composite Run state, versioned risk assessments, bounded action intents,
  authorization contexts, separate approvals, and versioned event provenance. The pure Run state
  machine now returns the canonical contract state and a deterministic transition event.
- **Thymira workspace** (E2E Architecture Baseline v2): uv workspace members
  `packages/{schemas,events}`, `runtime/{core,state,tools,policies,agents,thy,mira}`, `apps/api`,
  `adapters/cli` (namespace packages `thymira.<name>`), README placeholders for `apps/web`, the
  remaining adapters, SDKs, services and infrastructure, and the `examples/credit-risk/.thymira/`
  reference project.
- `thymira.schemas` — **Contract v0.1** (`docs/contracts/contract-v0.1.md`): frozen pydantic
  models for Run, Session, Agent, Task, ToolCall, Artifact, Experiment, Event, AuditFinding,
  PolicyDecision and ProjectConfig; closed enums (`Decision` with precedence, `RunStatus` with
  allowed transitions, `EventType`, `Severity`, `Framework`); prefixed ids.
- `thymira.events` — canonical JSON and sha256 helpers, secret/PII redaction, hash-chained
  `InMemoryEventLog` / `JsonlEventLog` and stateless `verify_events` / `verify_log`.
- `thymira.state` — `ArtifactStore` protocol and `LocalArtifactStore` with a sha256 manifest and
  `verify()`.
- `thymira.agents.llm` — `LLMProvider`, `LiteLLMProvider` (`THYMIRA_MODEL`, no hard-coded
  default) and `ScriptedProvider` for tests.
- `thymira.policies` — rules as data (`ActionRule`, `CapabilityRule`, `FindingRule`; YAML
  defaults `base` and `credit_risk`), `PolicyEngine` with fail-safe escalation, `Gate` recording
  `policy.decision` → `human.approval_requested` → `human.approval`, loaders and policy hashing.
- `thymira.core` — provenance capture (interpreter, dependency versions, git state recorded as
  evidence, input hashes) and the phase machine (scope-aware `next_phase`, budgeted backward-only
  `can_reopen`).
- `thymira.mira.checks` — deterministic audit controls A1–A7, A9–A10, A16, A17 over the event log and
  the store (`PASSED | FAILED | NOT_APPLICABLE | NOT_EVALUATED`), `audit_run` producing an
  `AuditReport` with findings for the Policy Engine and a Markdown assurance summary.
- Professional development scaffold: uv-managed `pyproject.toml` with PEP 735 dependency groups
  (`test`, `lint`, `typecheck`, `dev`), `uv.lock`, `.python-version` (3.13); ruff (lint + format);
  ty with `ty.toml` and a zero-diagnostic gate (`scripts/ty_ratchet.py`); import-linter
  architecture contracts (`scripts/lint_imports.py`); pre-commit hooks; yamllint.
- Task runners: `justfile` (canonical, PowerShell-aware) and a forwarding `Makefile`.
- Container image (`Dockerfile`, `compose.yaml`, `.dockerignore`) for the workspace, built with
  uv (multi-stage, non-root `thymira` user, entrypoint `thymira`).
- GitHub Actions, least privilege and pinned by SHA: CI (lock check; lint, types, architecture,
  skills, wheel contents; `zizmor` over the workflows; TruffleHog secrets scan; changelog gate;
  fast-lane matrix on Linux/Windows × Python 3.12/3.13; full suite with a 90 % coverage floor;
  container image build + smoke test), CodeQL (activates when the repository is public),
  weekly dependency audit, release on `vX.Y.Z` tags, Dependabot (actions, uv, docker), PR and
  issue templates, and PR-Agent configuration (`.pr_agent.toml`, workflow gated on `OPENAI_KEY`).
- Agent instructions for Claude Code, Codex, Cursor and Gemini CLI (`AGENTS.md`, `CLAUDE.md`,
  `GEMINI.md`, `.claude/settings.json` with a format-on-edit hook) and fifteen agent skills under
  `.agents/skills/` (synced to `.claude/skills/` by `scripts/sync_skills.py`), including
  `python-general` (the Python baseline for this stack), `skill-creator` (how to write a
  skill, with a template) and `repo-skeleton` (where each roadmap item lives, with a
  roadmap → layout reference); `AGENTS.md` states how agents route, trace and communicate.
- Documentation in English: `README.md`, `CONTRIBUTING.md`, `docs/architecture/e2e-baseline-v2.md`,
  `docs/roadmap/mvp-3-weeks.md`, ADR-0001 (harness choice, two orchestrators), ADR-0002 (legacy
  disposition: what was ported, kept as data or dropped), the Contract v0.1 note, design spec and
  plan under `docs/superpowers/`, `docs/legacy/README.md` indexing the thesis documents.
- Model routing (`thymira.agents.llm.routing`, ADR-0004): per-orchestrator models
  (`THYMIRA_THY_MODEL`, `THYMIRA_MIRA_MODEL`), three tiers (`FRONTIER`/`STANDARD`/`FAST`) chosen
  by code from the kind of task with per-role floors, and a `ModelChoice` recorded as the new
  `model.selected` event; `agent.message` event for sub-agent ↔ orchestrator exchanges
  (Contract 0.2). `.github/CODEOWNERS` with the P1–P5 area mapping.
- Licence: Apache License 2.0 (`LICENSE`; `license = "Apache-2.0"` in every `pyproject.toml`).
- Repository tooling under `scripts/`: `ty_ratchet.py`, `lint_imports.py`, `sync_skills.py`,
  `validate_skills.py`, `check_wheel.py` (every member wheel), `clean.py`,
  `hooks/format_on_edit.py`, with tests in `tests/tooling/`.

- **Contract fields that had to land before the freeze.** `SandboxMode`
  (`read_only`/`workspace_write`/`danger_full_access`) and `SandboxEnforcement`
  (`full`/`partial`/`unusable`) are declared at full width; `ToolCall` and `ToolResult` carry
  `sandbox_mode` and `sandbox_enforcement`, and the Tool Manager copies what the sandbox reported
  rather than inferring it. `Policy` gains `budget_rules` and `model_rules` (`BudgetRule`,
  `ModelRule`) ahead of the engine that will evaluate them — `policy_sha256` hashes the whole
  model dump, so a field added later would change every existing policy's hash and break A16
  replay for every decision already recorded.
- **Two roadmaps over one catalogue** (`docs/roadmap/`). `product-final.md` defines all 125 tasks
  exactly once — deliverable, runnable acceptance criterion, dependencies and, for MVP tasks, the
  *seam* that makes the FINAL version a drop-in. `mvp-minimum.md` is a view over the 81 MVP tasks:
  load per member, day-1 starts, the critical path and the four acceptance gates. The governing
  rule is that **the MVP cuts implementations, never contracts or seams**, so reaching the final
  product needs no refactor. `mvp-3-weeks.md` is superseded for scope and ownership and kept for
  its strategic reasoning.

### Fixed
- **A delegated agent never actually saw an earlier step's result.** `PromptBuilder` (THY-05)
  reads history from `agent.message.payload["text"]`; `Delegator` (THY-08) never wrote that key
  (only `summary`) and never opted the event into `surface=EventSurface.MODEL_VISIBLE` (the
  default is `LOG_ONLY`, on purpose — an event reaches a model only by saying so). Each task was
  fully tested alone — THY-05's own tests hand-craft a `text` payload, THY-08's only ever checked
  `summary` — but the two were never wired to each other, so `current_surface(events)` silently
  dropped every `agent.message` and a later step's prompt was always just its own objective, with
  zero memory of the run so far. `Delegator` now renders a one-line `text` summary of the outcome
  (`"{agent} completed: {summary}"` / `"{agent} failed: {error}"`) and marks the event
  `MODEL_VISIBLE`; a delegated task's outcome now actually reaches the next step that reads the
  surface.
- **Cross-platform `list_files`, `ContainerSandbox` and project identity.** `list_files`
  (`TOOL-09`) now returns POSIX-style (`/`) relative paths on every platform instead of
  `str(Path)`'s OS-native separator, which broke round-tripping a listed path back into
  `read_file` on Windows. `ContainerSandbox` (`TOOL-07`) now omits Docker's `--user uid:gid` flag
  on platforms without `os.getuid`/`os.getgid` (Windows) instead of raising `AttributeError`, and
  no longer fails `ty check`'s zero-diagnostic baseline there. `ProjectResolver.resolve` now
  hashes a POSIX-style workspace path into `project_id` instead of interpolating the `Path`
  object's OS-native form, so the same project resolves to the same id on Windows and POSIX
  (previously it silently disagreed across platforms, defeating the "stable project id" the
  function promises).
- **Stable child-record insertion order.** Local per-run records now retain their save order
  after reopening the runtime store, while failed transactions restore the order index.
- **Atomic local run writes.** A failed local lifecycle write now restores the prior run,
  session, record and event state instead of leaving partially persisted runtime data.
- **Stable local state queries.** Malformed repository cursors now raise the documented validation
  error, and child-record listing no longer depends on filesystem modification times.
- **Audited Run creation.** `RunService` exposes the roadmap's event-backed creation boundary so
  a compatibility path cannot persist a Run without provenance and its `run.started` event.
- **A hostile artifact name no longer suppresses the whole audit.** `a10_declared_artifacts`
  reads the name straight out of an `artifact.created` payload and hands it to
  `ArtifactStore.exists`, which had begun raising `ValueError` on any name it would refuse to
  write. The exception propagated out of the control and out of `audit_run`, so a single crafted
  payload produced **no report at all** — precisely inverting the auditor's contract, since the
  event log is untrusted input MIRA exists to recompute from. `exists` and `get` are now total: a
  name the store would refuse to write answers `False` / `None`, because a query about an
  impossible name has a correct answer. Every write path still refuses loudly, which is where a
  caller asking to escape the root is a bug worth surfacing. A10 now reports
  `announced but missing/invalid` for those names, which is what it was always meant to do.
- **The archived copy of a revision is written through the store's own containment check.**
  `_archive_existing` resolved its destination as `root / key`, the one write in the class that
  skipped `_contained` and therefore the only one that did not resolve symlinks; a link planted
  at `.history/<stem>` could redirect the copy outside the store while the manifest went on
  claiming the revision was held inside it.
- **The fail-closed branch of the Tool Manager is pinned by a test.** `allows_execution`
  authorises an escalated call only when `approved is True` — the whole distance between *a human
  approved this* and *a model asked nicely* — and nothing covered it: every context in
  `test_tools.py` built the `Gate` with `auto_approve`, and the only denial exercised was a
  straight capability refusal, never a review that was requested and then declined. The new test
  drives the fail-safe path (a PASS capability escalated by a risk profile under
  `minimum_risk_confidence`) with `auto_reject`, and asserts the tool was never reached rather
  than merely that the record was labelled denied. It is the only test in the suite that fails
  when the guard is loosened to `approved is not None` or dropped altogether.
- **Redaction no longer leaks the secrets it exists to mask.** The upper bounds on the credential
  patterns did not shorten a long secret, they un-redacted it, in two different ways. A capped
  class with no trailing anchor (`BEARER`, `JWT`, `CREDENTIALS`) matched the cap and wrote the
  overflow verbatim: a 1200-character bearer token left 688 characters of itself in cleartext, as
  an exact suffix of the token. A capped class followed by `\b` (`API_KEY`, `GITHUB_TOKEN`,
  `SLACK_TOKEN`) could never satisfy the boundary once the secret ran past the cap, so the match
  was abandoned and the *whole* key was written — `sk-` plus 255 characters was redacted, `sk-`
  plus 256 was not. Azure AD, Auth0 and Google tokens routinely exceed 512 characters, and
  `redact` runs before an event is hashed, so the leak landed on the immutable chain and left
  through `GET /runs/{id}/events`. Those six patterns are unbounded again. The caps that encode a
  format stay (AWS, Google, IBAN, and the RFC 5321 local part and domain label), as does `EMAIL`'s
  — that class is the one that can genuinely backtrack quadratically, which is what the bounds
  were introduced for. A boundary case per family, and a `slow` test pinning the linear scaling,
  now stand in the way of anyone restoring a bound "for safety".
- **The artifact store no longer writes archive keys it will then refuse to read.**
  `_next_archive_name` built the `.history` key from the raw stem, so a name whose stem ends in a
  dot — `x..json`, or `metrics.{version}.json` rendered with an empty `version` — minted a key
  that the store's own trailing-dot rule rejects. It was persisted to `manifest.json` all the
  same, and from then on *every* open of that store raised: not that one artifact, the whole run's
  evidence, with `verify()`, `load_*` and MIRA's A5/A10 all out of reach because constructing the
  store came first. The stem is now sanitised the way an incoming name is, and the result is
  canonicalised through the same `_store_key` before it is returned, so any future divergence
  fails at write time instead of poisoning the manifest.
- **One artifact file can no longer carry two manifest keys.** A name is refused when it is a
  Windows device (`NUL`, `CON`, `COM1`, …, with or without an extension), when a component ends
  in a dot or a space, or when it differs from a registered name only in case — on every
  platform, so a run's evidence does not depend on where it executed. `save_bytes("NUL", data)`
  previously succeeded while Windows discarded the bytes: the store recorded `size_bytes=0` and
  the sha256 of the *empty* file, `load_bytes` returned `b""`, and `verify()` reported the store
  clean, because the manifest agreed with the device it had just read back. `metrics.json` and
  `Metrics.json` became two keys over one file, overwriting a revision without archiving it and
  then making `verify()` report *both* as modified — an integrity alarm about a collision the
  store caused itself. A reloaded manifest holding two such entries is refused as well.
- **`invalidate()` and `set_lineage()` accept the name that saved the artifact.** Both looked the
  manifest up by the raw string while `save`/`get` canonicalised it, so `invalidate(["./x.md"])`
  silently changed nothing and `set_lineage("./x.md")` raised `KeyError` for an artifact the
  store could hand you. `invalidate()` now also refuses a bare `str`: it is an `Iterable[str]`,
  so it was consumed character by character and reported success having invalidated nothing.
- **`LocalArtifactStore` contains every name.** An artifact name is canonicalised to one
  store-relative key and refused if it is blank, rooted (POSIX absolute, Windows drive or UNC
  share) or contains `..`; paths are re-checked after symlink resolution, and a manifest whose key
  or `uri` escapes the root is refused on reload. Previously `save_text("../x", …)` wrote outside
  the store and `verify()` still reported it clean, and `a.json` / `./a.json` collided on one file
  — overwriting the first revision without archiving it despite `preserve_history=True`.
- **Redaction no longer leaks URL and DSN credentials.** `https://user:pass@host` matched the
  `EMAIL` pattern from inside the password, leaving the username and part of the password in
  cleartext behind a misleading `[REDACTED:EMAIL]` marker; a `CREDENTIALS` pattern now removes the
  whole userinfo before `EMAIL` runs.
- **Redaction is no longer quadratic.** Every quantifier is bounded, so a crafted string on the
  mandatory append path cannot stall the runtime (a 200 KB adversarial input went from ~90 s to
  under a second). Coverage added for AWS, GitHub, Slack and Google keys, JWTs and PEM private-key
  blocks.
- **An `Event` payload can no longer hold a value JSON cannot represent.** `nan` and `±inf`
  serialise to `null`, so four different values shared one digest and a diverged model's
  `loss=inf` was recorded as nothing; such a payload is now refused, naming the offending path.
- **`PolicyEngine.policy_sha256` is recomputed per decision.** It was cached at construction while
  the mappings inside the rules stayed mutable, so a rule could be flipped from `BLOCK` to `PASS`
  while every recorded decision kept quoting the original digest — and restoring it left no trace.
- **A tool can no longer forge a governance event.** `Tool.execute` now receives a narrowed
  `ToolInvocation` (run, agent, workspace, artifact store) instead of the runtime-owned
  `ToolContext`, which carried a writable event log and the `Gate`. A tool holding those could
  append its own `human.approval` — validly chained, passing `verify()` — and manufacture the
  approval a `REQUIRE_HUMAN_REVIEW` decision was waiting for. A tool reports; the manager
  authorises and records.
- **A tool receives its real arguments, not the redacted ones.** Redaction protects the evidence,
  never the execution: the recorded `ToolCall` and the event payloads stay redacted, but a tool no
  longer authenticates with the literal `[REDACTED:API_KEY]`.
- **MIRA control A10 cross-checks the hash chain.** It now compares the digest recorded in the
  `artifact.created` event against the store, so an attacker who rewrites an artifact *and* its
  manifest to agree is detected — previously A1, A5 and A10 all passed and the audit reported
  `passed`.

### Changed
- **`CLI-APPROVE` — Approve and reject show what is pending.** `thymira approve` and
  `thymira reject` now fold the pending human-approval decision (reason, summary, cost so far) out
  of the Run's event history and show it before posting; a Run with nothing pending is reported
  without calling the decision route. Both commands gained `--reason` as an alias of `--note`.
- **Reconciled `main` with the MIRA reconcile branch.** `Event` now requires `event_id`,
  `schema_version`, `producer` and `producer_version` when constructed directly (`EventLog.append`
  still fills them); `PolicyDecision` no longer carries `approved`/`approved_by`/`approved_at` — a
  human resolution is a separate `Approval` record; `ToolManager` denies a `REQUIRE_HUMAN_REVIEW`
  tool call synchronously (correction C-9), recording the approver's answer as evidence only;
  `ToolContext`/`ToolInvocation` lose `record_repository` and `run_experiment` records the full
  `Experiment` inside the `experiment.completed` event payload; `runtime/core` now depends on
  `thymira-policies`; `CONTRACT_VERSION` is `0.5`.
- **ThyGraph now halts at Plan under the default policy (correction C-15).** `plan_node` gates
  THY's own plan through `gate.check_action('plan.proposed')` + `allows_execution` — the same rule
  as a tool call (C-9), no approval object — instead of `main`'s `PolicyDecision.approved` boolean.
  With the shipped default policy `plan.proposed` escalates to `REQUIRE_HUMAN_REVIEW`, which no
  synchronous approver answer satisfies, so ThyGraph now stops at Plan until `HITL-01` lands or a
  project policy PASSes `plan.proposed` with an explicit `ActionRule` — a behaviour change from
  `main`'s `THY-11`, where an `auto_approve` approver advanced the plan.
- **MIRA and Run evidence design:** the MVP uses local JSON/JSONL. `events.jsonl` is
  authoritative and append-only; `run.json` is reconstructible; MIRA snapshots and evaluations
  are immutable JSON. The design explicitly permits one writer per Run and makes no multi-file
  transaction or production fault-tolerance claim.
- **Renamed to Thymira** (ADR-0003, `docs/brand/story.md`): repository `Thymira/thymira`,
  namespace `thymira.*`, distributions `thymira-*`, CLI `thymira`, project context `.thymira/`,
  base model `ThymiraModel`, environment `THYMIRA_*`. The orchestrators are **THY** (acts;
  `runtime/thy`, `ThyGraph`) and **MIRA** (watches; `runtime/mira`, `MiraGraph`) — "Sol" and
  "Opus" were example model names. Models are configuration, never names in the code
  (`thymira.agents.llm.get_provider(role=...)`); `THYMIRA_ORCHESTRATOR_MODEL`,
  `THYMIRA_AGENT_MODEL` and `THYMIRA_MODEL` are fallbacks inside the resolution chain the routing
  entry above introduced; `thymira.agents.llm.routing.model_for` documents the exact order per
  tier and role.
- The repository is now **Thymira**, an Agentic Data Science Runtime, written entirely in
  English; the root `pyproject.toml` is a virtual workspace root (`package = false`) and
  `requires-python` is `>=3.12` in every member.
- ruff runs the full rule set (including pydocstyle, annotations, pylint and eradicate) with no
  deferred categories; ty's baseline is 0; import-linter enforces the `thymira` layers,
  THY/MIRA independence, contracts-import-nothing and CLI-owns-no-state contracts.
- Member dependencies follow the layering: `thymira-core` composes `thymira-thy` and
  `thymira-mira`, never the reverse.
- `data/credito_aleman.csv` renamed `data/german_credit.csv`.
- Hygiene: `.gitattributes` (LF everywhere, CSV/binary untouched), `.editorconfig`; `.gitignore`
  and `.dockerignore` cover every real env file (`.env.*` except the example), keys and
  certificates, hypothesis/MLflow/SQLite state, Node and Terraform artefacts, OS junk, and
  everything in `data/` except the tracked demo dataset (`data/README.md`); pre-commit hooks
  never rewrite `data/` or `runs/`; `.env.example` in English.

### Removed
- The thesis prototype: `src/mads` (LangGraph orchestrator, data-science skills, `PolicyGate`,
  risk classifier, tracing, meta-auditor, LiteLLM wrapper, CLI and console), its Spanish test
  suite (`tests/test_*.py`, `tests/conftest.py`, `tests/fakes.py`), `probar_agentico.py`,
  `grafo_agentico.mmd`, `examples/cases/*.json`, the `mads` console script and Docker image, and
  the datasets `airbnb_nyc_dirty.csv`, `ENB2012_Y1.csv`, `titanic.csv`. The code stays reachable
  at git tag `thesis/mads-v0.1.0`; ADR-0002 lists what was ported and what was dropped.
- Spanish documentation moved, frozen, to `docs/legacy/` (with the `compliance/` mapping).

## [0.1.0] - 2026-08-19

### Added
- Thesis skeleton (`mads`): LangGraph agentic orchestrator with eight injectable coordinators,
  data-science skills catalogue, `PolicyGate` with JSON policy packs, hash-chained trace,
  meta-auditor with controls A1–A18, LiteLLM as the single model integration, CLI (`run`,
  `verify`, `audit`, `catalog`, `skills`, `runs`, `status`, `llm-test`) and observability console.

[unreleased]: https://github.com/Thymira/thymira/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Thymira/thymira/releases/tag/v0.1.0
