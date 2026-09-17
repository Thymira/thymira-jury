# Contributing

This guide is for people; coding agents read `AGENTS.md` (same rules, shorter). Everything in
this repository — code, docs, commits — is written in English.

## Prerequisites

| Tool | Install | Why |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) ≥ 0.12 | `irm https://astral.sh/uv/install.ps1 \| iex` (Windows) · `curl -LsSf https://astral.sh/uv/install.sh \| sh` | interpreter, lockfile, workspace, every tool runs through `uv run` |
| just | `uv tool install rust-just` | task runner (the `justfile` is canonical) |
| Docker (optional) | Docker Desktop / Engine | container image, `docker compose` |
| GNU make (optional) | `winget install ezwinports.make` | only if you prefer `make <target>`; it forwards to `just` |

uv installs the pinned interpreter (`.python-version` → 3.13) automatically; the code supports
Python ≥ 3.12 and CI tests 3.12 and 3.13.

## Set up

```bash
git clone <repo> && cd thymira
just setup        # uv sync --locked --all-groups --all-packages
just hooks        # pre-commit hooks
cp .env.example .env   # add the provider key LiteLLM needs (never commit .env)
just check        # lint, ty gate, import contracts, skills, roadmap, fast tests
```

## Daily loop

```bash
just test          # fast lane: everything but `slow` (~150 tests, a few seconds)
just lint-fix      # ruff safe fixes + format
just check         # before pushing
just test-all      # full suite before opening a PR
```

`just --list` shows every recipe with a one-line description. Recipes are single commands so
they behave the same in PowerShell and bash; anything with pipes or conditionals lives in
`scripts/*.py`.

## Test lanes

| Lane | Marker | Runs in | What belongs there |
|---|---|---|---|
| unit | none | `just test`, CI matrix | pure logic and fast I/O under `tmp_path` (event logs, the local store, policies) |
| integration | `pytestmark = pytest.mark.integration` | `just test`, CI matrix | external services (such as MLflow), the API server, the CLI as a subprocess, real training |
| slow | `pytestmark = pytest.mark.slow` | `just test-all`, CI full job | complete graph runs that take minutes |

LLM calls are never real in tests: use `thymira.agents.llm.ScriptedProvider`. Tests write only
under `tmp_path`. Concurrent pytest runs need their own `--basetemp=.pytest-tmp/<name>`.
One test module per workspace member: `tests/thymira/test_<member>.py`.

## Quality gates

- **ruff** lints and formats (`line-length = 100`, the full rule set including docstrings,
  annotations and pylint); config in `pyproject.toml`. Suppress only per line with
  `# noqa: CODE  # reason`; per-file ignores are for kinds of files (tests, scripts).
- **ty** type-checks with a **zero** baseline: `just typecheck-ratchet` fails on any diagnostic.
  Only a ty upgrade may move `.ty-baseline.json` (`scripts/ty_ratchet.py --update-baseline`), in
  the same commit as the pin bump.
- **import-linter** enforces the architecture contracts in `pyproject.toml`
  (`just check-imports`): the runtime layers, THY/MIRA independence, contracts importing no
  runtime, the CLI importing no runtime member. A violation is a design signal: move the
  dependency down (schema, event, artifact), do not reorder layers or add `ignore_imports`.
- **pre-commit** runs the whitespace/yaml/toml checks, ruff, `uv lock`, yamllint, import-linter,
  the ty gate, the roadmap check and the skill checks on every commit. `pre-commit run --all-files`
  only sees tracked files — stage new files first.

## The workspace

The repository is a uv workspace with a *virtual* root (`package = false`): the root
`pyproject.toml` holds tool configuration and dependency groups, nothing installable. Code lives
in members that expose `thymira.<name>` namespace packages:

```
packages/schemas  packages/events
runtime/core  runtime/state  runtime/tools  runtime/policies  runtime/agents  runtime/thy  runtime/mira
apps/api  adapters/cli
```

Where a roadmap item goes (member, subpackage, service, adapter, doc, test) is the
`repo-skeleton` skill's job (`.agents/skills/repo-skeleton/references/roadmap-to-layout.md`
maps every MVP deliverable to a location).

To add a member: copy an existing member's `pyproject.toml` (change `name`, `description`,
`module-name`), create `src/thymira/<name>/__init__.py`, add the path to
`[tool.uv.workspace].members` in the root `pyproject.toml`, the `src` path to ty `root`
(`ty.toml`), the module to `[tool.importlinter] root_packages` and to one `layers` line, the
import to `tests/thymira/test_workspace_imports.py`, then `uv lock` and `just setup`.
Dependencies: `uv add --package thymira-<name> <dependency>`; sibling members are declared with
`{ workspace = true }` in `[tool.uv.sources]` and must point *down* the layer list.

Import direction (top may import bottom, never the reverse; THY and MIRA never import each other):
`thymira.api` → `thymira.core` → `thymira.thy` | `thymira.mira` → `thymira.agents` →
`thymira.tools` → `thymira.policies` → `thymira.state` → `thymira.events` → `thymira.schemas`.
`thymira.cli` talks to the API over HTTP and imports no runtime member.

## Commits and pull requests

- Conventional Commits: `feat(scope): …`, `fix: …`, `docs: …`, `refactor: …`, `test: …`,
  `build: …`, `ci: …`, `chore: …`. Branches: `feat/…`, `fix/…`, `docs/…`.
- Small PRs, merged daily (MVP roadmap rule); CI must be green (`.github/workflows/ci.yml`).
- User-visible changes add a line under `[Unreleased]` in `CHANGELOG.md` (or the commit message
  contains `[skip changelog]`).
- `pyproject.toml` changes ship with the regenerated `uv.lock`.

## Agent skills

Skills for coding agents live in `.agents/skills/<name>/SKILL.md` (Codex, Cursor and Gemini CLI
read that directory). Claude Code reads `.claude/skills/`, which is generated: after editing a
skill run `just validate-skills && just sync-skills` and commit both directories. The
`skill-creator` skill (and its `references/skill-template.md`) holds the structure, frontmatter
and writing rules: a "Use when …" description, numbered rules with their why, an "In this
repository" section with dated facts, a rationalization table; heavy material goes to
`references/` or `scripts/` next to the skill.

## Docker

`just docker-build` builds `thymira:dev` (the whole workspace, non-root, entrypoint `thymira`);
`just docker-run <args>` runs it through `compose.yaml` with `.env`, `runs/`, `data/` and
`examples/` mounted. The MVP keeps Run state locally in JSON/JSONL; optional service integration belongs under
`infrastructure/docker/` (MVP week 1–3).

## Release checklist

1. Move the `[Unreleased]` entries of `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and open a
   fresh `[Unreleased]`; update the compare links at the bottom.
2. Bump `version` in the root `pyproject.toml` and in every member (they move in lockstep), run
   `uv lock`.
3. `just ci` locally; commit `chore(release): vX.Y.Z`; tag `vX.Y.Z`; push with tags.

## Windows notes

- Git is configured with `core.autocrlf=true` on many machines; `.gitattributes` forces LF in the
  repository. Write files with `newline="\n"` from Python.
- No symlinks (`core.symlinks=false`); the skill copies are real copies.
- Rich-based CLIs crash under cp1252 pipes; `scripts/lint_imports.py` forces UTF-8 for
  import-linter, and `PYTHONUTF8=1` helps for anything else.
- In bash, never put backticks inside a double-quoted string (commit messages!): they run as
  commands. Write the message to a file and use `git commit -F`.
