# AGENTS.md — repository guide for coding agents

**What this repository is.** The home of **Thymira**, an Agentic Data Science Runtime. An LLM
orchestrator (**THY**) runs data-science work through typed tools while an independent audit
orchestrator (**MIRA**) checks it against methodology and regulation; a deterministic **Policy
Engine** — never an LLM — decides `PASS | WARNING | REQUIRE_HUMAN_REVIEW | BLOCK`, and a human
approves. Clients (CLI first; IDEs, coding agents, web, SDKs later) are replaceable; the runtime
owns all state. The thesis prototype this grew from (`mads`, Spanish) is archived at git tag
`thesis/mads-v0.1.0`; what was worth keeping was ported, in English and with tests, into the
`thymira.*` members (`docs/adr/0002-legacy-disposition.md`). Ownership labels P1–P5 below come
from `docs/roadmap/mvp-minimum.md`.

**Names.** *THY weaves the solution; MIRA follows every thread* (`docs/brand/story.md`,
ADR-0003). In prose the orchestrators are **THY** and **MIRA**; in code they are the members
`thymira.thy` and `thymira.mira`. Each orchestrator runs on its own frontier model
(`THYMIRA_THY_MODEL`, `THYMIRA_MIRA_MODEL`); everything else is routed by code through three
tiers (`THYMIRA_MODEL_FRONTIER|STANDARD|FAST`) from the *kind of task*, with per-role floors, and
every choice is a `model.selected` event (ADR-0004). Model ids are configuration, never names in
the code or the docs.

**Language policy.** Everything — code, comments, docstrings, docs, commits, PRs — is English.
`docs/legacy/` holds the frozen Spanish thesis documents; do not extend them, link to them.

## How to work here

- **Route before acting.** Match the task to a skill (table below) and read it before touching
  code: process skills first (`python-debug` for any failure, `python-testing` for any test),
  then the implementation skill; the most specific skill wins over `python-general`, which
  wins over habit. Instructions closer to the code (a member `README.md`, `CLAUDE.md`,
  `GEMINI.md`) override this file where they are more specific.
- **State assumptions and trace your sources.** When you choose a framework, a lane, a layer or
  a default the request did not fix, say so in one line; in the final message or PR body list
  the skills you applied (names only).
- **Be direct.** Lead with the finding, the change and the reason; exact identifiers and paths;
  quoted code unchanged; no filler. Security warnings and irreversible actions get full
  sentences and a confirmation.
- **Prove it, then say it.** "Done" comes with the output that shows it (`just check`,
  `Contracts: 4 kept, 0 broken`, `N passed`); a skipped step is named as skipped.
- **A foundation needs every accepted criterion.** The MVP's completed tasks do not close
  the DSH/ADR-0013 program. Follow the
  [F1–F13 acceptance record](docs/superpowers/plans/2026-09-07-dsh-acceptance.md): leave a
  foundation open until every accepted COPY/ADAPT criterion has behavioral evidence and its
  applicable independent invariant. Percentages and completed subsets cannot compensate.

## Commands (`justfile` is canonical; `make <target>` forwards to `just <target>`)

| Command | Purpose |
|---|---|
| `just setup` | `uv sync --locked --all-groups --all-packages` (whole workspace) |
| `just hooks` | install the pre-commit hooks |
| `just test` / `just test-all` / `just test-slow` / `just test-cov` | fast lane (everything but `slow`; ~150 tests, seconds) / full suite / only `slow` / with coverage |
| `just lint` / `just lint-fix` / `just fmt` | ruff check + format check + yamllint / apply safe fixes / format |
| `just typecheck` / `just typecheck-ratchet` | ty report / gate: the count may not exceed `.ty-baseline.json` (**0**) |
| `just check-imports` | the four import-linter contracts (via `scripts/lint_imports.py`) |
| `just check-roadmap` | the two roadmap documents must agree on every task (via `scripts/check_roadmap.py`) |
| `just check` | pre-push gate: lint, ty gate, imports, skills, roadmap, fast tests |
| `just sync-skills` / `just validate-skills` | regenerate `.claude/skills/` / validate `SKILL.md` files |
| `just god-classes` | size and cohesion report over the workspace members |
| `just langfuse-dashboards` | apply the Thymira Operations dashboard to Langfuse (no-op without `LANGFUSE_*`) |
| `just thymira <args>` / `uv run thymira <args>` | the `thymira` CLI (stub until MVP week 1) |
| `just web <args>` / `uv run thymira-web <args>` | the web console in front of a running API (`http://127.0.0.1:8080`) |
| `just docker-build` / `just docker-run <args>` | build `thymira:dev` / run it through compose |

Run tools through `uv run …`, never the global `python`. After editing any `pyproject.toml`:
`uv lock`, then commit `uv.lock` in the same change.

## Repository map

- `packages/schemas` (P1) — `thymira.schemas`, **Contract v0.9** (`docs/contracts/contract-v0.1.md`,
  "Changes"): frozen pydantic models (Run, Session, Agent, Task, ToolCall, Artifact, Experiment,
  Event, AuditFinding, PolicyDecision, ProjectConfig), the Contract 0.3 authorization records
  (`RunState`, `ActionIntent`, `AuthorizationContext`, `Approval`), the Contract 0.4/0.5 MIRA
  evidence records (`ActivityProfile`, `RiskAssessment`, `PackBinding`, `ControlEvaluation`,
  `DecisionContext`), closed enums, prefixed ids, `RUN_TRANSITIONS`.
- `packages/events` (P1) — `thymira.events`: canonical JSON + sha256, source credential scrubbing,
  fail-closed export redaction, hash-chained `InMemoryEventLog` / `JsonlEventLog`, stateless
  `verify_events` / `verify_log`, and the log-vs-surface fold (`derive_surface` / `current_surface`,
  ADR-0006).
  Both packages import nothing from `runtime/`, `apps/` or `adapters/`.
- `runtime/core` (P1) — `thymira.core`: provenance capture (`capture_run_environment`), the
  phase machine (`Phase`, `next_phase`, `can_reopen`), and `MiraControlPlane`/`RunController` —
  the sole writer of persistent Run transitions for MIRA's five bounded control-plane actions
  (`request_information`, `pause_run`, `resume_run`, `request_approval`, `review_findings`);
  `RunService`/`SessionService`, `RuntimeState`/`Subgraph`, `StateCheckpointer`, idempotency/
  lineage and `UsageLedger` are the implemented P1 foundation — `RunService` is rewired to
  `RunController` + `LocalRunStore` next (`docs/roadmap/product-final.md` correction C-8), then
  the LangGraph wiring that composes ThyGraph + MiraGraph (`RA-CORE-06`). `route_after_thy` parks
  a Run on a tool-call review before MIRA.
- `runtime/state` (P1/P3) — `thymira.state`: `ArtifactStore` protocol + `LocalArtifactStore`
  (manifest with sha256, `verify()`); MVP Run state is local JSON/JSONL: `events.jsonl` is
  authoritative and append-only, while `run.json` is reconstructible.
- `runtime/observability` (P1) — `thymira.observability`: the only member that imports `langfuse`.
  One redacted observation per seam a Run passes through (Run, phase, agent, generation, tool,
  evidence, guardrail); off unless both `LANGFUSE_*` keys are set, isolated so it can never fail a
  Run, and non-authoritative — `events.jsonl` stays the record (ADR-0012).
- `runtime/policies` (P4) — `thymira.policies`: rules as data (`ActionRule`, `CapabilityRule`,
  `FindingRule`, YAML in `defaults/`), `PolicyEngine` (fail-safe escalation), `Gate` (emits
  `policy.decision` → `human.approval_requested` → `human.approval`), loaders.
- `runtime/agents` (P2/P4) — `thymira.agents.llm`: `LLMProvider`, `LiteLLMProvider`
  (`THYMIRA_MODEL`), `ScriptedProvider` for tests; `AgentSpec`/`AgentCatalog`, `AgentRunner`,
  `PromptBuilder`, the tool bridge — the spec-driven execution loop every THY sub-agent runs
  through.
- `runtime/mira` (P4) — `thymira.mira`: deterministic controls A1–A7/A9–A10/A16/A17
  (`thymira.mira.checks`, `audit_run` → `AuditReport`), preflight (inherent risk, pack
  applicability, evidence controls), `MiraAuditOrchestrator`, the bounded `DecisionContextAnalyst`,
  and the partial `MiraGraph` (`MIRA-01`); findings and proposed actions go to the Policy Engine
  and `MiraControlPlane`, never straight to a Run. A18 independently checks versioned profile
  REPORT artifacts against their registered CSV/Parquet source and captured schema; it never
  imports the producer's profile implementation.
- `runtime/tools` (P3) — `thymira.tools`: Tool Manager + Permission Policy (`ToolRegistry`, the
  policy-gated, fail-closed `ToolManager`, with the one-shot approval ticket,
  `tool_intent_sha256`), `run_python`, `read_file`/`write_file`/`edit_file`/
  `glob`/`grep`, git, MLflow and dataset tools, and the per-tool prompt guidance (`tool_guidance`),
  and the MCP surface. `runtime/thy` (P2) — ThyGraph (Inspect — which registers the project's
  declared datasets — → Plan → Execute → Summarize) and the Data/Coding/Experiment agents.
  `apps/api` (P1) — typed Run/event models; route modules wired (`runs`, `events`, `audit`,
  `artifacts`, `governance`, `mlflow`, `project`, `settings`, `tools`). `adapters/cli` (P5) — thin
  API client;
  twelve commands over HTTP (`run`, `answer`, `runs`, `resume`, `events`, `approve`, `reject`,
  `status`, `audit`, `experiment`, `mlflow`, `plan`), all implemented. `apps/web` (P5) —
  `thymira.web`, the browser console (`thymira-web`): static ES-module views plus a same-origin
  pass-through that forwards the caller's own bearer token; imports no runtime member.
  Other `adapters/*`, `packages/sdk-*`, `services/*`, `infrastructure/*` are still README
  placeholders (purpose, owner, priority).
- `tests/thymira/` workspace tests (one module per member) · `tests/tooling/` script tests ·
  `scripts/` repo tooling · `.agents/skills/` canonical agent skills · `.claude/` Claude Code
  settings + generated skill copies · `examples/credit-risk/.thymira/` reference project ·
  `data/german_credit.csv` demo dataset · `docs/` architecture, roadmap, contracts, ADRs,
  `superpowers/` specs and plans, `legacy/` (Spanish thesis docs, frozen).

## Architecture invariants

- **LLM proposes, code authorizes.** A model's selection, confidence or finding never becomes an
  authorization: the Policy Engine decides; humans approve `REQUIRE_HUMAN_REVIEW`; the `Gate`
  records every decision as events. A human's approval of a tool call authorizes exactly that
  call, once, through `allows_execution` — never a retry the model rephrased.
- **THY and MIRA never import each other.** They share only `thymira.schemas` / `thymira.events`
  and are composed in `runtime/core`. MIRA reads events and evidence; it never modifies the
  workspace.
- **`RunController` is the only persistent transition writer** (ADR-0008); MIRA observes and
  proposes, and an audit `BLOCK` is not an authorization — authorization is the Policy Engine's
  `AuthorizationDecision` (`DENY` is its refusal), never a finding.
- **Clients own no state.** CLI, web and IDE adapters call the Thymira API; the MVP keeps Run
  history in local `events.jsonl`, reconstructible state in `run.json`, artifacts in the local
  `ArtifactStore` and experiments in MLflow (ADR-0010: no database in the MVP).
- **Every tool call goes through the Tool Manager + Permission Policy** (MCP is the exposure
  standard); agents never call subprocess or the filesystem directly.
- **Layering is machine-checked** (`just check-imports`, `[tool.importlinter]`): `api` → `core` →
  `thy | mira` → `agents` → `tools` → `policies` → `state` → `observability` → `events` →
  `schemas`; `thy`/`mira` independent; `schemas`/`events` import no runtime; `cli` and `web`
  import no runtime member. Member `pyproject.toml` dependencies follow the same direction.
- **Evidence is verifiable.** Events preserve model-visible values and are hash-chained; known
  credentials are scrubbed at source; export and trace projections are redacted fail-closed;
  artifacts carry sha256 in a manifest; provenance is captured at run start; MIRA recomputes
  everything instead of trusting success flags.
- **LiteLLM is the only model gateway.** Keys live in `.env` — loaded at the process entry
  points (`thymira.api.server.main`, the `thymira` CLI's `main`), never by a library factory, and
  never overriding a variable the environment already sets — and never in code or traces;
  chain-of-thought is never persisted.
- **Models are routed by code, never chosen by a model.** An agent declares a task kind (and
  may request a tier); `thymira.agents.llm.routing` applies the tier table and the role floor
  and records the `ModelChoice` as `model.selected`. Sub-agents talk only to their orchestrator
  (graph state, `agent.message`); THY and MIRA never talk to each other — MIRA reads the
  thread and answers through the Policy Engine (ADR-0004).

Read more: `docs/architecture/e2e-baseline-v2.md` (target design),
`docs/roadmap/` (`product-final.md` — every task, defined once; `mvp-minimum.md` — what we build
first and who builds it), `docs/adr/` (decisions), `docs/contracts/contract-v0.1.md` (the shared
types, currently v0.9), `CONTRIBUTING.md` (workflow).

## Conventions

- Python ≥ 3.12 (`requires-python` everywhere); dev interpreter 3.13 (`.python-version`).
  Workspace members expose `thymira.<name>` namespace packages (`uv_build`, `namespace = true`);
  a new member is added to `[tool.uv.workspace].members`, ty `root`, `[tool.importlinter]`
  (`root_packages` + a layer) and `tests/thymira/test_workspace_imports.py`.
- ruff (`line-length = 100`, full rule set incl. `D`, `ANN`, `PL`, `ERA`; Google docstrings);
  precise exception types; `pathlib`; `print` only in CLIs and scripts; `# noqa: CODE  # reason`
  only per line. ty: **zero diagnostics** (`.ty-baseline.json` total 0); re-baseline only with a
  ty upgrade, in the same commit.
- Tests: `tests/thymira/test_<member>.py`; markers `slow` (minutes) and `integration` (external
  services, API server, CLI subprocess, real training) — everything else runs in `just test`.
  LLM calls use `thymira.agents.llm.ScriptedProvider`; never the network; write only under
  `tmp_path`; concurrent pytest runs need their own `--basetemp`.
- Commits: Conventional Commits; branches `feat/…`, `fix/…`, `docs/…`; small PRs merged daily
  (roadmap rule). `CHANGELOG.md` `[Unreleased]` is updated for user-visible changes, or the commit
  says `[skip changelog]`.
- Never commit `.env`, `runs/`, `data/creditcard.csv` or `.claude/settings.local.json`.

## Agent skills

Canonical: `.agents/skills/<name>/SKILL.md` — read natively by Codex, Cursor and Gemini CLI.
Claude Code reads the generated copies in `.claude/skills/`: edit the canonical file, run
`just sync-skills`, commit both (CI fails on drift). Gemini CLI asks for confirmation the first
time a skill activates.

| Skill | Use when |
|---|---|
| `python-general` | any Python: layout, typing, functions vs classes, signatures, exceptions, docstrings, secrets |
| `repo-skeleton` | where a roadmap item lives; creating folders, modules, subpackages, members, services, placeholders |
| `packaging-scaffolding` | dependencies, workspace members, `pyproject.toml`, uv lock/sync/build problems |
| `python-god-classes` | a class or module is too big or mixes responsibilities; before adding to one |
| `python-debug` | failing test, stack trace, unexpected or flaky behaviour — before any fix |
| `python-testing` | which kind of test, where it goes, which marker, which lane |
| `python-testing-unit` | fast isolated tests: fixtures, parametrize, fakes vs mocks |
| `python-testing-integration` | external services, API, CLI, real training; `slow` / `integration` |
| `ruff` | lint/format failures, `noqa`, rule configuration |
| `ty` | type diagnostics, annotations, the zero baseline |
| `check-imports` | imports between members, lint-imports failures, the THY/MIRA boundary |
| `dockerfile` | images, compose, dockerignore, size or rebuild problems |
| `task-runner` | justfile/Makefile recipes, Windows shell differences |
| `changelog` | user-visible changes, releases |
| `skill-creator` | writing or revising a skill, its references or scripts; `validate-skills` / `sync-skills` failures |
| `langfuse` | LLM observability: tracing, prompt management, datasets, evals, scores — vendored from [langfuse/skills](https://github.com/langfuse/skills), see `.agents/skills/langfuse/VENDORED.md`; never hand-edit it |

## Gotchas

- Windows: `core.symlinks=false` (never symlink); `just` runs recipes in PowerShell, so pipes and
  conditionals belong in `scripts/*.py`; Rich-based CLIs need UTF-8 (`scripts/lint_imports.py`);
  write files with `newline="\n"`.
- Windows hides two path bugs that CI catches: the Win32 API **strips trailing dots** (so
  `Path("a/guide.md.").exists()` is True locally and False on Linux) and the filesystem is
  case-insensitive. A repo script that resolves a path parsed out of prose must be tested on the
  parse, not on `.exists()` — see `REFERENCE_RE` in `scripts/validate_skills.py`.
- pydantic evaluates annotations at run time: imports used in model fields stay real imports
  (ruff knows through `runtime-evaluated-base-classes`); only annotation-only imports elsewhere go
  under `TYPE_CHECKING`.
- ty is pre-1.0 and pinned; upgrade it and re-baseline in the same commit.
- `pre-commit run --all-files` only sees tracked files; stage new files before trusting it.

## Code review rules

Every non-trivial decision gets an Agent Note under `.agents/notes/{proposed,implemented,rejected,archived}`
with the fixed `Problem`, `Decision`, `Alternatives considered` and `Consequences` headings. The
`Alternatives considered` section must name what the decision beat, and `just check-notes` is the
mechanical gate. Simplifications are deleted wholesale and record `needs a named consumer` before
they can be reintroduced; generalize only after two consumers.

- Reject any path where an LLM output becomes an authorization or bypasses the Policy Engine /
  `Gate`, where a known credential reaches a provider/event source, where an export or trace
  bypasses fail-closed redaction, or where an event is written outside the hash chain.
- Reject imports across the THY/MIRA boundary, from `packages/*` into the runtime, or that break
  the layer order; no new `ignore_imports` without a `TODO(arch)` note.
- Require tests in the right lane, English, a `CHANGELOG.md` entry for user-visible changes, and
  `uv.lock` next to any `pyproject.toml` change.
- Prefer small single-responsibility units; point at `python-god-classes` when a class grows.
