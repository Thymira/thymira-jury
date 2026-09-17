---
name: task-runner
description: Use when adding, renaming or changing a justfile recipe or Makefile target, when a recipe works on Linux but not on Windows (PowerShell), or when unsure which command runs lint, tests, type-checks, the skill sync or the container in this repository.
metadata:
  version: "1.0.0"
---

# Task runner (just and make)

The `justfile` is the single source of truth for commands; the `Makefile` only forwards to it.
Recipes stay one command per line and push loops, pipes and timing into a stdlib `scripts/`
helper, so they run the same on Git Bash and on Windows PowerShell.

## When to use

- Adding, renaming or changing a `just` recipe or a `make` target.
- A recipe works on Linux but fails on Windows (PowerShell).
- Deciding which command runs lint, tests, type-checks, the skill sync or the container.
- Not for: the tool config itself (see `ruff`, `ty`, `check-imports`), or the Python inside a
  helper (ordinary code with a `tests/tooling/` test).

## Quick start

A well-shaped recipe: verb-first kebab name, one `[group]`, one `[doc]`, one command line,
`*ARGS` if it takes arguments. Logic lives in the helper, not the recipe.

```just
[group("run")]
[doc("Benchmark: time the titanic case N times and print the mean")]
bench *ARGS:
    uv run python ./scripts/bench.py {{ARGS}}
```

The loop, timing and averaging go in `./scripts/bench.py` (stdlib only, `main() -> int`,
`--help`).

## Rules

1. The `justfile` is canonical; the `Makefile` only forwards (`%: @just $@`) and never gains a
   target body — the shim already covers every recipe.
2. Each recipe line runs in its own shell, PowerShell on Windows, so a line is one command:
   bash-only loops, `$(...)`, pipes, `&&` and `cd` break there.
3. Anything needing a loop, pipe, variable, conditional or `cd` becomes a stdlib `scripts/`
   helper; the recipe stays a one-line `uv run python ./scripts/<name>.py …`.
4. A helper that carries logic gets a `tests/tooling/test_<name>.py` (imported with
   `load_script`), like the other tooling scripts.
5. Every recipe declares `[group("…")]` and `[doc("…")]` with a verb-kebab name; `just --list`
   is the repo's command index.
6. `just check` (pre-push) and `just ci` stay fast and offline, so a recipe needing the network,
   credentials or real API spend never joins them.
7. A task-runner change touches only the `justfile`, `Makefile` and a `scripts/` helper — never
   a committed case, dataset or `src/` file.

## Workflow

1. Choose the shape: a pure one-liner goes in the recipe; anything else gets a `scripts/` helper
   written first, with its `tests/tooling/` test.
2. Add the recipe under the right `[group]` with a `[doc]`, verb-kebab name, and `*ARGS` if it
   takes arguments. Leave the `Makefile` alone.
3. Run it in Git Bash, in PowerShell, and once as `make <name>`; then `just --list`.

## In this repository

Shells: `set shell := ["bash", "-uc"]` and `set windows-shell := ["powershell.exe", "-NoLogo",
"-Command"]`. Helpers live in `scripts/`; the `Makefile` is a pure shim (`make <name>` equals
`just <name>`).

| Need | Recipe |
|---|---|
| list every recipe | `just` (or `just --list`) |
| format / lint / autofix | `just fmt` / `just lint` / `just lint-fix` |
| type-check / ratchet | `just typecheck` / `just typecheck-ratchet` |
| architecture contracts | `just check-imports` |
| tests: fast / full / slow / coverage | `just test` / `just test-all` / `just test-slow` / `just test-cov` |
| sync / validate skills | `just sync-skills` / `just validate-skills` |
| run the CLI / in Docker | `just thymira <args>` / `just docker-run <args>` |
| pre-push gate / CI set | `just check` / `just ci` |

## Common mistakes

| Rationalization | Reality |
|---|---|
| "A quick `for` / `$(date)` / pipe in the recipe is simpler." | It runs under PowerShell on Windows and breaks; move the logic to a helper, keep the recipe one line. |
| "It needs live keys and spends real budget — that's expected." | Then it stays out of `check`/`ci` and documents `.env`; drive `thymira.agents.llm.ScriptedProvider` so a benchmark runs offline. |
| "No helper here has a test, so bench.py needs none." | False: `ty_ratchet`, `sync_skills`, `validate_skills`, `lint_imports` and `check_wheel` each have a `tests/tooling/` test. |

Red flags — stop:

- a recipe line with `for`, `while`, `|`, `&&`, `$(...)`, `>` or `cd`;
- a new `%`-style target or any body added to the `Makefile`;
- editing files outside the `justfile`, `Makefile` and `scripts/` to make a recipe succeed;
- a networked or credentialed recipe added to `check` or `ci`.

## Related skills

- python-testing-integration — the deterministic provider and `slow`/`integration` lanes.
- ruff, ty, check-imports — the gates behind `just lint`, `just typecheck-ratchet`,
  `just check-imports`.
