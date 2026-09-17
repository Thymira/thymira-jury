---
name: packaging-scaffolding
description: Use when adding or changing a dependency, creating a new workspace member or module tree, editing a pyproject.toml (dependencies, dependency groups, build backend, entry points, tool sections, uv workspace), or when uv lock, uv sync --locked or uv build fail or uv.lock is out of date.
metadata:
  version: "1.0.0"
---

# Packaging and scaffolding

This repository is a **uv workspace** with a *virtual* root: the root `pyproject.toml`
(`package = false`) carries tool configuration and dependency groups, and the code lives in
`thymira.*` members under `packages/`, `runtime/`, `apps/` and `adapters/`, resolved by **one**
root `uv.lock`. Make every change with a `uv` command, then re-lock, sync and let the contracts
prove it. See `references/pyproject-anatomy.md` for the annotated pyprojects and the full `uv`
command table.

## When to use

- Adding, changing or removing a dependency (runtime dep of a member, or a local tool).
- Creating a workspace member, a subpackage or a module tree.
- Editing a `pyproject.toml`: dependencies, groups, build backend, scripts, tool sections, or
  `[tool.uv.workspace]`.
- `uv lock`, `uv sync --locked` or `uv build --all-packages` fails, or `uv.lock` is reported
  out of date.
- **Not for:** picking a test lane (python-testing), adding an import edge between members
  (check-imports), or writing justfile recipes (task-runner).

## Quick start

| Goal | Command |
|---|---|
| Runtime dep of a member | `uv add --package thymira-tools <pkg>` |
| Sibling member as a dep | add `"thymira-<x>"` to `dependencies` + `thymira-<x> = { workspace = true }` in `[tool.uv.sources]`, then `uv lock` |
| Local tool that never ships (pytest, ruff) | `uv add --group <name> <pkg>` (root) |
| Remove a dep | `uv remove [--package <m> \| --group <g>] <pkg>` |
| After any **hand** edit to a pyproject | `uv lock` then `just setup` |
| Verify | `just lock-check`, `just check-imports`, `uv build --all-packages && uv run python ./scripts/check_wheel.py` |

## Rules

1. Match the table to what the dependency *is*: runtime → a member's `dependencies`; a local
   tool that never ships → a root `[dependency-groups]` entry. The root has no runtime
   dependencies and no extras; groups never reach a wheel.
2. A tool group is **opt-in**: leave it out of `dev`. `just setup` and CI install every group
   with `--all-groups`, so folding a `docs`/build group into `dev` only forces that tool onto
   everyone's bare `uv sync`.
3. Prefer `uv add`/`uv remove`; hand-edit a pyproject only for what `uv` cannot express (a
   `tool` section, a workspace source). After **any** hand edit run `uv lock` before
   `uv sync --locked` — a hand edit leaves `uv.lock` stale and CI's `just lock-check` fails.
4. Commit the pyproject(s) and the regenerated `uv.lock` in the **same** commit; a lagging lock
   fails `uv lock --check` and the `uv-lock` pre-commit hook.
5. Give every dependency an upper bound at the next major (`>=x.y,<x+1`); reuse a range a
   sibling already pins so the single lock agrees.
6. `requires-python = ">=3.12"` everywhere is the compatibility floor — never raise it to match
   `.python-version` (`3.13`, the dev pin only); CI tests both.
7. A member dependency must point *down* the import-linter layer list (check-imports); a
   `{ workspace = true }` source pointing upward is an architecture violation.
8. A new subpackage needs an `__init__.py` with a module docstring; data files (YAML policies,
   prompts) must land in the wheel — add them to `REQUIRED` in `./scripts/check_wheel.py` and run
   `uv build --all-packages && uv run python ./scripts/check_wheel.py`.
9. Console scripts go in the member's `[project.scripts]` and are smoke-tested with
   `uv run <name> --help`.

## Workflow — add or change a dependency

1. Pick the table (Rule 1) and run `uv add …` with `--package`/`--group` — this edits the
   pyproject and `uv.lock` together.
2. If you hand-edited instead, run `uv lock`.
3. `just setup` (`uv sync --locked --all-groups --all-packages`) — fails loudly if the lock is stale.
4. Commit the pyproject(s) + `uv.lock` together.

## Workflow — create a workspace member (`runtime/experiments` → `thymira.experiments`)

1. Write `runtime/experiments/pyproject.toml` (shape in `references/pyproject-anatomy.md`):
   `name = "thymira-experiments"`, `requires-python = ">=3.12"`, `uv_build` build system, and
   `[tool.uv.build-backend]` with `module-name = "thymira.experiments"` **and**
   `namespace = true`. Sibling deps use `[tool.uv.sources] <dep> = { workspace = true }`.
2. Create `runtime/experiments/src/thymira/experiments/__init__.py` (plus `tracker.py`). Do
   **not** add `src/thymira/__init__.py`: `thymira` is a namespace package.
3. Register the member in five places: `"runtime/experiments"` in `[tool.uv.workspace].members`
   (root pyproject), `"./runtime/experiments/src"` in `root` in `ty.toml`,
   `"thymira.experiments"` in `[tool.importlinter] root_packages` **and** in one `layers` line,
   and `"thymira.experiments"` in `MEMBERS` in `tests/thymira/test_workspace_imports.py`.
   Ruff's `src` already globs `runtime/*/src`; `./scripts/check_wheel.py` `EXPECTED_WHEELS` gets
   the new wheel name.
4. `uv lock && just setup`, then verify: `uv run pytest tests/thymira/test_workspace_imports.py`
   and `just check-imports`.
5. Commit the new files, both pyprojects, `ty.toml`, the test and `uv.lock` together.

## In this repository

- Member name `thymira-<x>` ↔ module `thymira.<x>` (e.g. `thymira-tools` is `runtime/tools`).
  One `uv.lock` at the root governs the whole workspace; CI runs `just lock-check`
  (`uv lock --check`) and builds every member wheel.
- Root groups: `test`, `lint`, `typecheck` (ty pinned exactly), `dev` = all three.

## Related skills

- **check-imports** — layers placement and architecture contracts. **task-runner** — the
  `setup`/`lock-check` recipes. **python-testing** — where a new test goes.
