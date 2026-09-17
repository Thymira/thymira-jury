# Professional development scaffold for MADS — design spec

- **Date:** 2026-08-20
- **Branch:** `feat/professional-scaffold` (forked from `mario-harnes`, 5 commits ahead of `main`)
- **Status:** implemented under the autonomous-run assumptions listed in §2; owner review requested

## 1. Context and goals

MADS is a ~19k-line Python skeleton of an *auditable* multi-agent data-science system (LangGraph
orchestrator, LiteLLM model gateway, deterministic `PolicyGate`, hash-chained trace, meta-auditor).
Code, docs and comments are in Spanish. The test suite is green (484 passed, 3m24s) but the repo has
no linting, type-checking, task runner, lockfile, CI, container image, changelog or agent
instructions.

**Goal.** Re-found the repository as a professional, English-language development environment that
four coding agents (Claude Code, OpenAI Codex, Cursor, Gemini CLI) can all read and follow, and whose
structure already anticipates the target two-layer architecture (execution orchestrator + latent
assurance orchestrator), without implementing that architecture yet.

**Deliverables** (all new content in English):

| Area | Files |
|---|---|
| Agent instructions | `AGENTS.md` (canonical), `CLAUDE.md`, `GEMINI.md` (pointers), `.claude/settings.json` |
| Agent skills | `.agents/skills/<12 skills>/SKILL.md` (+ scripts), generated copies in `.claude/skills/` |
| Packaging | `pyproject.toml` (rewritten), `uv.lock`, `.python-version` |
| Quality tooling | `ty.toml`, `[tool.ruff]`, `[tool.importlinter]`, `[tool.pytest.ini_options]`, `.pre-commit-config.yaml`, `.yamllint.yaml`, `.pr_agent.toml` |
| Task runners | `justfile` (canonical), `Makefile` (shim) |
| Containers | `Dockerfile`, `.dockerignore`, `compose.yaml` |
| Hygiene | `.gitignore`, `.gitattributes`, `.editorconfig`, `.git-blame-ignore-revs` |
| CI | `.github/workflows/ci.yml`, `.github/workflows/pr-agent.yml` |
| Docs | `CHANGELOG.md`, `CONTRIBUTING.md`, `docs/adr/0001-harness-and-two-layer-architecture.md`, this spec, the implementation plan |
| Repo scripts | `scripts/sync_skills.py`, `scripts/validate_skills.py`, `scripts/ty_ratchet.py`, `scripts/hooks/format_on_edit.py` |

**Non-goals.** Translating existing Spanish code/docs; implementing layer 2, the regulation RAG,
tools, hooks or commands of the future system; restructuring `src/mads` into the target packages;
fixing the god classes found by analysis; adding a LICENSE (a legal choice for the owner).

## 2. Decisions and assumptions

The session ran autonomously; these are the judgment calls, with the reasoning. Anything marked
**(flag)** is surfaced for the owner to confirm or reverse.

1. **Existing code is kept, not deleted.** "Create the repo from scratch" is read as re-founding the
   development environment around the code that will be translated later (the request says the repo
   "will go entirely from Spanish to English"). Deleting 19k lines autonomously is irreversible in
   spirit; keeping them is reversible.
2. **One isolated, blame-ignored formatting commit.** `ruff format` + safe `ruff check --fix` are run
   once over `src/`, `tests/`, `scripts/` in a dedicated commit listed in `.git-blame-ignore-revs`.
   Without it every future PR that touches a legacy file would carry formatting noise from the
   pre-commit hook. The suite must stay green afterwards. **(flag)**
3. **Ruff must be green on this branch.** Remaining findings after autofix are fixed mechanically
   when safe; anything needing a design decision gets a line-scoped `# noqa: CODE  # reason`.
   Rule categories that would flood legacy code (`D` docstrings, `ANN` annotations, `PLR`
   complexity, `ERA` commented-out code) are deferred and listed as a ratchet to enable during the
   English translation pass, which rewrites every docstring anyway.
4. **ty is a ratchet, not a zero gate.** ty is beta (0.0.73); legacy code has 78 diagnostics.
   `ty.toml` keeps Astral's default severities, CI runs `scripts/ty_ratchet.py`, which fails only if
   the diagnostic count exceeds the committed baseline (`.ty-baseline.json`). `ty.toml` is the
   *only* ty config (no `[tool.ty]` in `pyproject.toml`): ty gives `ty.toml` precedence, so having
   both is split-brain.
5. **Python: `.python-version` = 3.13; `requires-python` stays `>=3.11`.** Ruff/ty target 3.11.
   CI tests 3.11 and 3.13 on Linux and Windows.
6. **Dependency pins reflect the proven lock.** The lock generated from the current metadata
   resolves LangGraph 1.2.11 and LiteLLM 1.97.0 with a green suite, so `langgraph>=1.0,<2` replaces
   `>=0.2` (no code change; the code already runs on 1.x). Other runtime pins are unchanged.
7. **PEP 735 dependency groups replace the `dev` extra.** `boosting` remains a published extra;
   `test`, `lint`, `typecheck` and the `dev` umbrella become groups. `uv sync` installs `dev` by
   default. Tests need the boosting catalog, so `test` includes `mads[boosting]`.
8. **Build backend: `uv_build`** (pure-Python, src layout). Verified by building a wheel and
   asserting `mads/policy_packs/fraude_aml.json` is inside it; if `uv_build` will not ship a data
   directory without `__init__.py`, an `__init__.py` is added to `policy_packs/` (preferred) rather
   than falling back to hatchling.
9. **`justfile` is canonical; `Makefile` forwards.** The owner asked for both. Two full
   implementations would drift, so `make <target>` runs `just <target>` and prints install
   instructions if `just` is missing. Recipes are shell-neutral one-liners (`uv run …`,
   `uv run python scripts/…`); on Windows `just` uses PowerShell via the `[windows]` attribute.
10. **`.claude/` is partially un-ignored.** Commit `5128ce2` ignored the whole directory when it
    only held `settings.local.json`. Claude Code reads skills only from `.claude/skills/`, so the
    generated copies and the shared `.claude/settings.json` must be tracked; personal files
    (`settings.local.json`, `*.local.*`) stay ignored. **(flag)**
11. **Default `pytest` behaviour is unchanged** (full suite). Markers `slow` and `integration` are
    registered and applied; the fast lane is `just test` (`-m "not slow"`), `just test-all` runs
    everything. CI runs the fast lane on the matrix and the full suite once.
12. **import-linter encodes today's layering, with two documented exceptions**, and names the two
    architectural invariants the thesis relies on (skills never import the orchestrator; audit never
    imports the orchestrator). The known upward edge `skills → workers.ml` (a misplaced pure CSV
    loader used by 11 modules) is allowed via `ignore_imports` with a TODO instead of being refactored
    on this branch; the `TYPE_CHECKING`-only `skills.base → decisions` edge is excluded by
    `exclude_type_checking_imports = true`.
13. **README.md stays Spanish** (it is part of the translation pass). A short English banner at the
    top points to `AGENTS.md` and `CONTRIBUTING.md`.
14. **Harness question answered in an ADR, not in code** (§7).
15. **Branch is pushed to `origin`; no PR is opened.** Opening a PR is the owner's call.

## 3. Repository layout after this branch

```
mads-msct/
├── AGENTS.md                 # canonical agent instructions (Codex + Cursor read natively)
├── CLAUDE.md                 # "@AGENTS.md" + Claude Code appendix
├── GEMINI.md                 # "@AGENTS.md" + Gemini CLI appendix
├── CONTRIBUTING.md           # human-facing dev workflow (English)
├── CHANGELOG.md              # Keep a Changelog 1.1.0
├── README.md                 # Spanish (legacy) + English banner
├── pyproject.toml            # project, groups, uv_build, ruff, importlinter, pytest, coverage
├── uv.lock  .python-version  ty.toml  .ty-baseline.json
├── justfile  Makefile
├── Dockerfile  .dockerignore  compose.yaml
├── .pre-commit-config.yaml  .yamllint.yaml  .pr_agent.toml
├── .gitignore  .gitattributes  .editorconfig  .git-blame-ignore-revs
├── .env.example
├── .agents/skills/<name>/SKILL.md [+ scripts/ references/]   # CANONICAL skills (12)
├── .claude/
│   ├── settings.json         # shared: permissions allowlist + format-on-edit hook
│   └── skills/               # GENERATED by scripts/sync_skills.py — do not edit
├── .github/workflows/ci.yml  pr-agent.yml
├── scripts/                  # repo tooling (not shipped in the wheel)
│   ├── sync_skills.py  validate_skills.py  ty_ratchet.py
│   └── hooks/format_on_edit.py
├── docs/
│   ├── adr/0001-harness-and-two-layer-architecture.md
│   ├── superpowers/specs/…  superpowers/plans/…
│   └── *.md (Spanish, legacy)
├── src/mads/ … (unchanged layout; formatted)
├── tests/ … (unchanged layout; 6 files gain `pytestmark = pytest.mark.slow`)
├── compliance/  data/  examples/
```

No symlinks anywhere (Windows, `core.symlinks=false`).

## 4. Cross-tool agent configuration

Verified against vendor docs on 2026-08-20 (research notes R1, R4).

| Tool | Instructions | Skills | Needs config? |
|---|---|---|---|
| Codex | `AGENTS.md` (native, ≤32 KiB) | `.agents/skills/` (native) | no |
| Cursor | `AGENTS.md` (native) | `.agents/skills/` (native) | no |
| Gemini CLI | `GEMINI.md` → `@AGENTS.md` import | `.agents/skills/` (alias of `.gemini/skills/`) | no |
| Claude Code | `CLAUDE.md` → `@AGENTS.md` import | `.claude/skills/` only → generated copies | `.claude/settings.json` (hooks) |

**AGENTS.md** (target < 200 lines, English): what MADS is and the language policy; the `just`
command table; repo map; current architectural invariants (LLM proposes / Gate authorizes; audit
reads from disk; "skill" in `src/mads/skills` = a data-science pipeline step, *not* an agent skill);
target two-layer architecture in ten lines + ADR link; conventions (ruff/ty/import-linter, test
lanes, Conventional Commits, Keep a Changelog, Google docstrings in English, no secrets/PII in
traces); skills index (12 one-liners) and how each tool loads them; Windows gotchas; code-review
rules (used by Codex review mode and PR-Agent `extra_instructions`).

**Pointers.** `CLAUDE.md` = `@AGENTS.md` + short appendix (hook description, `.claude/skills` is
generated). `GEMINI.md` = `@AGENTS.md` + note that Gemini asks for confirmation on first skill
activation. No `.cursor/`, `.codex/` or `.gemini/` directories are needed; creating them would only
add unverified surface.

**Skill sync.** `scripts/sync_skills.py` copies `.agents/skills/*` → `.claude/skills/*` (removing
stale targets); `--check` exits 1 on drift. Wired into `just sync-skills`, a local pre-commit hook
and CI. Canonical `SKILL.md` frontmatter is restricted to the portable subset (`name`,
`description`, `metadata`) so the same file is valid in all four tools and uploadable to the
Skills API; the copies are byte-identical.

**Hooks.** One Claude Code `PostToolUse` hook (matcher `Edit|Write|MultiEdit`) runs
`uv run python scripts/hooks/format_on_edit.py`, which reads the tool JSON from stdin and runs
`ruff format` + `ruff check --fix` on the edited `.py` file under `src/`, `tests/` or `scripts/`;
always exits 0. Pure Python, so it behaves the same on Windows. Equivalent hooks for Cursor/Gemini
are documented in AGENTS.md as a follow-up, not implemented (different stdin schemas, unverified).

## 5. Tooling decisions

### 5.1 `pyproject.toml`
- `[project]`: name `mads`, version `0.1.0`, English description, `requires-python >= 3.11`,
  runtime deps `joblib>=1.3`, `langgraph>=1.0,<2`, `litellm>=1.90,<2`, `pydantic>=2,<3`,
  `scikit-learn>=1.5,<2`; extra `boosting` unchanged; script `mads = "mads.cli:main"`.
- `[dependency-groups]`: `test` (pytest>=9.1,<10, pytest-cov>=7.1,<8, pytest-xdist>=3.8,<4,
  hypothesis>=6.165, `mads[boosting]`), `lint` (ruff>=0.16.3,<0.17, import-linter>=2.13,<3,
  yamllint>=1.38,<2, pre-commit>=4.6,<5), `typecheck` (`ty==0.0.73`, exact: pre-1.0),
  `dev` = include-group test + lint + typecheck.
- `[tool.uv]`: `default-groups = ["dev"]`, `required-version = ">=0.12"`.
- `[build-system]`: `uv_build>=0.12.5,<0.13`; `[tool.uv.build-backend]` module `mads`, root `src`.
- `[tool.ruff]`: `line-length = 100` (31 legacy lines exceed it vs 204 over 88), `target-version
  = "py311"`, `src = ["src","tests","scripts"]`, exclude `runs`, `data`, `.pytest-tmp`.
  `[tool.ruff.lint] select = ["E","W","F","I","B","UP","N","SIM","C4","RUF","PT","PTH","T20",
  "ARG","TRY","BLE","DTZ","ISC","PIE","RET","PERF","FURB","PLE","PLW","PLC","S","A","G","RSE",
  "SLF","TID","TC"]`, `ignore = ["COM812","ISC001","TRY003"]` (formatter conflicts; TRY003 is
  noise). Per-file: `tests/**` ignores `S101,ARG,PLR2004,SLF001`; `src/mads/cli.py`,
  `src/mads/console.py`, `scripts/**` ignore `T20` (print is the UI); `__init__.py` ignores `F401`.
  `[tool.ruff.lint.isort] known-first-party = ["mads"]`; pydocstyle convention `google` (ready for
  when `D` is enabled); `[tool.ruff.format] docstring-code-format = true`.
  The final `select` list may shrink at implementation time if a category proves unfixable on
  legacy code within this branch; any removal is recorded in the CHANGELOG ratchet list.
- `[tool.importlinter]`: see 5.3.
- `[tool.pytest.ini_options]`: `testpaths`, `addopts = "--basetemp=.pytest-tmp --strict-markers"`,
  `markers = ["slow: full orchestrator/end-to-end runs (minutes)", "integration: touches the on-disk
  ArtifactStore, real scikit-learn training or the CLI"]`, existing `filterwarnings` preserved.
- `[tool.coverage.run] source = ["mads"]`, `branch = true`.

### 5.2 `ty.toml`
`[environment] python-version = "3.11"`, `root = ["./src"]`; `[src] include = ["src","tests",
"scripts"]`; `[terminal] output-format = "concise"`; `[rules]` left at defaults. The ratchet script
parses `ty check --output-format concise`, counts diagnostics per rule, compares with
`.ty-baseline.json`, fails on any increase of the total, and `--update-baseline` rewrites it after
a cleanup.

### 5.3 import-linter contracts (derived from the measured import graph, research A2)
```toml
[tool.importlinter]
root_packages = ["mads"]
exclude_type_checking_imports = true

[[tool.importlinter.contracts]]
name = "Layered architecture (current modules)"
type = "layers"
containers = ["mads"]
exhaustive = true
exhaustive_ignores = ["policy_packs"]
layers = [
  "cli",
  "console",
  "orchestrator",
  "audit",
  "workers",
  "gate",
  "decisions | policies | risk",
  "skills",
  "contracts | artifacts | tracing",
  "llm | model_catalog | rag",
  "utils",
]
ignore_imports = [
  # TODO(arch): workers.ml is a pure CSV loader misplaced under workers; move it to the
  # foundation layer (e.g. mads.tabular_io) and drop this exception.
  "mads.skills.** -> mads.workers.ml",
]

[[tool.importlinter.contracts]]
name = "Skills never depend on the orchestrator"
type = "forbidden"
source_modules = ["mads.skills"]
forbidden_modules = ["mads.orchestrator", "mads.cli", "mads.console"]

[[tool.importlinter.contracts]]
name = "Audit observes runs from disk, never imports the orchestrator"
type = "forbidden"
source_modules = ["mads.audit"]
forbidden_modules = ["mads.orchestrator", "mads.workers", "mads.cli", "mads.console"]
```
`orchestrator` sits above `workers`, so `orchestrator/actions.py → workers.ml` is legal.

### 5.4 pre-commit (`.pre-commit-config.yaml`)
`pre-commit/pre-commit-hooks v6.0.0` (trailing-whitespace, end-of-file-fixer, check-yaml,
check-toml, check-json, check-added-large-files, check-merge-conflict, detect-private-key,
mixed-line-ending `--fix=lf`, debug-statements, check-illegal-windows-names, name-tests-test
`--pytest-test-first` excluding `tests/(conftest|fakes).py`); `astral-sh/ruff-pre-commit v0.16.3`
(`ruff-check --fix`, then `ruff-format`); `astral-sh/uv-pre-commit 0.12.5` (`uv-lock`);
`adrienverge/yamllint v1.38.0`; local hooks (`language: system`, `pass_filenames: false`,
`always_run: true`): `lint-imports`, `ty-ratchet`, `sync-skills --check`, `validate-skills`.
`ci.skip` lists the local hooks (pre-commit.ci cannot run them). Tests are not run in pre-commit.

### 5.5 `.yamllint.yaml`
`extends: default`; `line-length: {max: 120, level: warning}`; `document-start: disable`;
`truthy: {check-keys: false}` (GitHub Actions `on:`); `comments: {min-spaces-from-content: 1}`;
`indentation: {spaces: 2, indent-sequences: consistent}`; ignore `.venv/`, `runs/`, `.pytest-tmp/`.

### 5.6 Task runners
`justfile` (settings: `set shell := ["bash","-uc"]`, `[windows] set shell := ["powershell.exe",
"-NoLogo","-Command"]`, `set dotenv-load`), grouped recipes: **setup** `setup` (uv sync
--all-groups --locked), `hooks` (pre-commit install), `lock`, `lock-check`; **quality** `fmt`,
`lint`, `lint-fix`, `typecheck`, `typecheck-ratchet`, `check-imports`, `god-classes`; **test**
`test` (fast), `test-all`, `test-slow`, `test-cov`; **agents** `sync-skills`, `validate-skills`;
**docker** `docker-build`, `docker-run *ARGS`; **run** `mads *ARGS`; **meta** `check` (lint +
typecheck-ratchet + check-imports + validate-skills + test), `ci` (check + test-all), `clean`.
Default recipe lists recipes. Every recipe body is a single command line that is valid in both
bash and PowerShell; anything more complex lives in `scripts/`.

`Makefile`: `.DEFAULT_GOAL := help`; `help:` → `just --list`; `%:` → `just $@`; a guard prints
`uv tool install rust-just` if `just` is absent.

### 5.7 Containers
Multi-stage `Dockerfile`: builder `python:3.13-slim` + `COPY --from=ghcr.io/astral-sh/uv:0.12.5
/uv /uvx /bin/`, `UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0`, cache mount,
`uv sync --locked --no-install-project --no-dev` with bind-mounted `pyproject.toml`/`uv.lock`,
then `COPY src/ …` and `uv sync --locked --no-dev --no-editable`, `ARG EXTRAS=""` to opt into
`boosting`; runtime stage copies `/app/.venv` only, non-root `mads` user, `PATH=/app/.venv/bin`,
`ENTRYPOINT ["mads"]`, `CMD ["--help"]`. `.dockerignore` excludes `.venv`, `.git`, caches, `runs/`,
`data/`, `tests/`, agent dirs. `compose.yaml`: service `mads`, `env_file: .env`, volumes for
`./runs`, `./data`, `./examples`. Verified with `docker build` locally.

### 5.8 CI (`.github/workflows/ci.yml`)
Actions pinned by SHA with version comments (`actions/checkout@3d3c42e5… # v7.0.1`,
`astral-sh/setup-uv@20cfd1bf… # v10.0.1`, `enable-cache: true`). Jobs: `lock-check` (`uv lock
--check`); `quality` (ruff check, ruff format --check, yamllint, lint-imports, ty ratchet,
validate-skills, sync-skills --check, `uv build` + wheel content assertion); `test` matrix
{ubuntu, windows} × {3.11, 3.13}, fast lane; `test-full` ubuntu/3.13 full suite with coverage XML
artifact. `pr-agent.yml`: `the-pr-agent/pr-agent@v0.42.0` (the project moved from `qodo-ai` to
the community org on 2026-04-23), runs only when the `OPENAI_KEY` secret exists (step-level guard).

### 5.9 `.pr_agent.toml`
Minimal overrides only: `[config] response_language = "en-US"`, `[pr_reviewer]`
(`require_tests_review`, `require_security_review`, `extra_instructions` pointing at AGENTS.md's
review rules), `[pr_description] enable_pr_diagram = true`, `[pr_code_suggestions]
extra_instructions`, `[ignore] glob = ["uv.lock", ".claude/skills/**", "data/**", "runs/**"]`.
Keys that could not be verified in the current schema (`maximal_review_effort`,
`num_code_suggestions`) are not used.

### 5.10 Hygiene files
`.gitignore` (existing entries kept; adds `.ruff_cache/`, `.import_linter_cache/`, `.coverage*`,
`htmlcov/`, `.ipynb_checkpoints/`, `.idea/`, `.vscode/*` except `extensions.json`,
`.claude/settings.local.json`, `.claude/*.local.*`, `*.log`, `.pytest-tmp/`; `!.env.example`;
removes the blanket `.claude/`). `.gitattributes` (`* text=auto eol=lf`, CRLF for `*.bat`/`*.ps1`,
`-text` for `*.csv`/`*.joblib`/binaries) — the index is already 100 % LF so no renormalisation
diff. `.editorconfig` (utf-8, LF, 4 spaces, 2 for yaml/json/toml, tabs for Makefile).
`.git-blame-ignore-revs` lists the format commit. `CHANGELOG.md`: `[Unreleased]` with this branch,
`[0.1.0] - 2026-08-19` summarising the pre-existing skeleton. `CONTRIBUTING.md`: setup, lanes,
commit/PR conventions, how to add a skill, Windows notes.

## 6. Agent skills

Twelve skills under `.agents/skills/`, each `SKILL.md` ≤ 300 lines, trigger-first third-person
description (≤ 400 chars), `metadata: {version: "1.0.0"}`; heavy material in `references/`
(one level deep), deterministic steps in `scripts/` (pure stdlib, forward-slash paths, helpful
errors). Names satisfy agentskills.io (`^[a-z0-9]+(-[a-z0-9]+)*$`, ≤ 64, equal to directory, no
"claude"/"anthropic").

| Skill | Trigger (description gist) | Ships |
|---|---|---|
| `packaging-scaffolding` | new package/module tree, pyproject edits, dependency groups, extras, build backend, `uv lock` | `references/pyproject-anatomy.md` |
| `python-god-classes` | class/module too big or mixed responsibilities; "split/decompose/refactor"; before growing one | `scripts/detect_god_classes.py` (AST: methods, LOC, LCOM-style cohesion, module-level defs; `--json`, `--fail-over`) |
| `python-debug` | failing test, stack trace, unexpected behaviour — before any fix | `scripts/bisect_test.py` |
| `python-testing` | which test kind, where, which marker, which lane | — (router to the two below) |
| `python-testing-unit` | fast isolated tests, fixtures, parametrize, fakes vs mocks, hypothesis | — |
| `python-testing-integration` | ArtifactStore on disk, real sklearn training, full orchestrator runs, CLI; `slow`/`integration` markers, deterministic LLM fake | — |
| `ruff` | lint/format/imports/noqa/config changes; rule rollout on legacy code | — |
| `ty` | type errors, annotations, `ty.toml`, ratchet failures | — (uses `scripts/ty_ratchet.py`) |
| `check-imports` | new cross-package import, `lint-imports` failure, evolving layers toward execution/assurance | `scripts/import_graph.py` (prints mads import edges + layer violations) |
| `dockerfile` | image/compose/dockerignore changes; size/speed problems | `references/uv-docker-patterns.md` |
| `task-runner` | adding/changing just recipes or make targets; which command runs what | — |
| `changelog` | before user-visible changes, releases | `scripts/check_changelog.py` (verifies `[Unreleased]` touched when `src/` changed) |

Each skill is written with the `superpowers:writing-skills` discipline (gap → minimal
instructions → review), validated by `scripts/validate_skills.py` (frontmatter constraints, body
length, referenced paths exist, portable key allowlist) and reviewed by an independent agent
against the agentskills.io/Anthropic checklist before the sync copies are generated.

## 7. Architecture guidance for the future system (recorded in ADR-0001)

Answer to "should we build on an existing open-source harness, e.g. DeepSeek's?":

- **Terminology.** What is being built is an *application* that contains a *harness* (tool
  registry, hook/event bus, subagents, permissions/gates, provenance) built with a *framework*.
  Adopting someone else's finished *product* as a base is the wrong layer.
- **`deepseek-ai/deepseek-harness`** exists (MIT, created 2026-08-13, TypeScript/Node, plugin
  runtime in developer preview with announced breaking changes). Wrong language and maturity for
  an auditable Python thesis system. The likely intended reference is **`langchain-ai/deepagents`**
  ("the batteries-included agent harness", MIT, built on LangGraph, 0.7.8 released 2026-08-20).
- **Recommendation.** Keep **LangGraph as runtime + LiteLLM as the single model gateway** (already
  in place, green suite on LangGraph 1.2): model-agnostic (GPT orchestrator *and* Claude auditor
  through one gateway), `interrupt()` = human/audit gates, checkpointers = provenance/replay,
  supervisor/swarm = subagent topologies. Use `deepagents` for sub-agents that benefit from the
  planning/filesystem/subagent loop. Do not base on the Claude Agent SDK (Claude-only) or the
  OpenAI Agents SDK (non-OpenAI models "best-effort/beta").
- **Custom thin harness owns only:** typed tool registry, hook/event bus, audit-intervention
  protocol (`Block | RequireChanges | Annotate` with regulatory citations), provenance ledger,
  policy gates. Do not re-implement model routing, graph execution, checkpointing, interrupts.
- **Run surface.** Own Typer + Textual CLI/TUI is the authoritative, trust-boundary surface; expose
  MADS additionally as an MCP server so the data scientist can call it from Claude Code / Codex /
  Cursor while coding.
- **Target package layout** (not created now): `mads.kernel` (contracts, llm, tools, events,
  tracing, provenance, policies), `mads.knowledge` (regulation KB/RAG), `mads.execution` (layer 1),
  `mads.assurance` (layer 2), `mads.runtime` (wires both; control channel), `mads.cli`, `mads.mcp`.
  Migration map from today's modules is in the ADR. The import-linter `layers` contract evolves to
  `cli | mcp → runtime → execution | assurance → knowledge → kernel`, plus an `independence`
  contract between `execution` and `assurance`, turning "the assurance layer cannot be captured by
  the execution layer" into a CI-checked invariant.

## 8. Verification plan (all must pass before the final report)

1. `uv lock --check`, `uv sync --locked --all-groups`, `uv build` + wheel contains `policy_packs/fraude_aml.json`.
2. `uv run ruff check .` and `uv run ruff format --check .` → clean.
3. `uv run ty check` runs; `uv run python scripts/ty_ratchet.py` passes at the recorded baseline.
4. `uv run lint-imports` → all contracts kept.
5. `uv run yamllint .` → clean.
6. `uv run pre-commit run --all-files` → all hooks pass.
7. `just --list`, `just check`, `make help`, `make lint` work on Windows (PowerShell) and Git Bash.
8. `docker build -t mads:dev .` succeeds; `docker run --rm mads:dev --help` prints usage.
9. `uv run pytest` full suite still 484 passed; `just test` fast lane excludes the 6 slow files.
10. `uv run python scripts/validate_skills.py` and `scripts/sync_skills.py --check` pass; an
    independent reviewer agent signs off each skill; skill scripts run on this repo.
11. `AGENTS.md` < 200 lines and < 32 KiB; `CLAUDE.md`/`GEMINI.md` import resolves.
12. Every new file is English.

## 7b. Addendum (2026-08-20, late) — alignment with the Harness E2E Baseline v2 and the MVP roadmap

After the scaffold tooling landed, the owner shared the team's **E2E Architecture Baseline v2**
and a **3-week, 5-person MVP roadmap** ("Hoja de ruta MVP"). They supersede §7's target layout:

- **Product:** an Agentic Data Science Runtime ("Harness"). "The client is replaceable. The
  Harness is the product." Clients (CLI first; later VS Code/Cursor, OpenCode, Claude Code, Codex,
  Web, SDKs) reach the runtime through adapters/MCP and a FastAPI **Harness API**; no client owns
  run state.
- **Two orchestrators:** **Sol** (data-science: Data, Coding/Execution, Experiment agents in the
  MVP) and **Opus** (audit/governance: Methodology, Risk, Regulatory agents in the MVP), both
  LangGraph graphs composed by the runtime; agents are implemented with **PydanticAI**; models go
  through **LiteLLM**; tools through a **Tool Manager + Permission Policy** (MCP as the exposure
  standard); experiments in **MLflow**; Run evidence in local JSON/JSONL; artifacts behind an
  `ArtifactStore`. ADR-0010 supersedes the earlier database assumption for the MVP.
- **Governance:** Opus produces findings (finding, evidence, severity, confidence,
  recommendation); a deterministic **Policy Engine** decides `PASS | WARNING |
  REQUIRE_HUMAN_REVIEW | BLOCK`; humans approve/reject. This is the same LLM-proposes /
  code-authorizes invariant MADS already enforces with `PolicyGate`.
- **Repository layout (monorepo):** `apps/{api,web}`, `runtime/{core,sol,opus,agents,tools,
  policies,state}`, `adapters/{cli,vscode,opencode,claude-code,codex,generic}`,
  `packages/{schemas,events,sdk-python,sdk-typescript}`, `services/{workers,sandbox-manager}`,
  `infrastructure/{docker,kubernetes,terraform}`, `tests/`, `docs/`, plus an
  `examples/credit-risk/.harness/` project.

**Consequences applied on this branch:**
1. The skeleton above is created now (the roadmap's day-1 task for P1) as a **uv workspace**:
   Python members expose `harness.<name>` namespace packages (`uv_build`, `namespace = true`),
   phase-2 areas are README-only, each directory states purpose, owner (P1–P5) and MVP priority.
   `src/mads` stays as the legacy reference implementation to be absorbed into `runtime/*`.
2. `docs/architecture/e2e-baseline-v2.md` and `docs/roadmap/mvp-3-weeks.md` carry English
   renderings of the baseline and the roadmap; ADR-0001 records the harness decision in these
   terms and maps MADS components onto the runtime (PolicyGate → Policy Engine, hash-chained trace
   → events/evidence, meta-auditor controls → Methodology/Risk audit checks, policy packs →
   `.harness/policies.yaml`, skills → tools behind the Tool Manager, `LiteLLMProvider` → LLM layer,
   `artifacts.py` → `LocalArtifactStore`).
3. `AGENTS.md` describes this structure and ownership; the `check-imports`, `packaging-scaffolding`
   and `dockerfile` skills reference the workspace rather than the §7 layout.
4. Open technical note for week 1 (P2): PydanticAI ships its own provider layer; using LiteLLM as
   the single gateway means either running LiteLLM as an OpenAI-compatible proxy for PydanticAI or
   writing a thin PydanticAI model adapter over the `litellm` SDK — decide before building agents.

## 9. Risks and open questions for the owner

- **(flag)** Formatting commit touches ~100 legacy files (whitespace only, blame-ignored).
- **(flag)** `.claude/skills/` and `.claude/settings.json` become tracked (reverses `5128ce2`).
- **(flag)** `langgraph` pin tightened to `>=1.0,<2` (already what the lock resolves).
- 3 `F821 undefined-name` findings in legacy code may be real bugs; they will be listed in the
  final report rather than silently patched.
- No `LICENSE` file exists; skills/pyproject omit license metadata until one is chosen.
- PR-Agent workflow needs an `OPENAI_KEY` repository secret to do anything.
- `ty` is pre-1.0; config keys may churn between releases (pinned exactly).
