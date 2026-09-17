---
name: ty
description: Use when ty check reports diagnostics, when just typecheck-ratchet or the ty CI step fails with "diagnostics increased" or "FAIL (strict)" (stale baseline), when adding or fixing type annotations, editing ty.toml, or upgrading the pinned ty version.
metadata:
  version: "1.0.0"
---

# ty: type checking with a zero baseline

ty is a pre-1.0 type checker, pinned exactly. This repository demands **zero diagnostics**: the
ratchet script compares the count with `.ty-baseline.json`, whose total is 0, so any new
diagnostic fails the gate. The baseline moves only on purpose — when a ty upgrade surfaces
noise that cannot be fixed — in the same commit as the pin bump.

## When to use

- `just typecheck-ratchet`, the pre-commit `ty-ratchet` hook, or the CI ty step reports
  `diagnostics increased` / `FAIL`.
- You are adding or fixing annotations, or `ty check` flags a line you touched.
- You are editing `ty.toml`, or bumping the `ty==` pin in the `typecheck` group.
- Not for: ruff or style findings (use the **ruff** skill); pytest markers or lanes
  (use **python-testing**).

## Quick start

| Goal | Command |
|---|---|
| Full diagnostic report | `just typecheck` |
| Local gate (fails if the count exceeds the baseline, i.e. on any diagnostic) | `just typecheck-ratchet` |
| CI's exact gate (also fails on a stale/lower baseline) | `uv run python ./scripts/ty_ratchet.py --strict` |
| Move the baseline after a deliberate ty upgrade | `uv run python ./scripts/ty_ratchet.py --update-baseline` |

## Decide: fix or suppress

For each diagnostic, keyed to what is observably true of it:

- **Fix** when a precise annotation is cheap and obviously correct (a missing return type, a
  `cast`, a narrow). Reach for a real type, never `Any`, which only disables the check.
- **Suppress** one specific, understood, deliberate line with `# ty: ignore[rule]  # reason`,
  or a scoped `[[overrides]]` block in `ty.toml` for a whole test file that fakes a module
  (the existing `tests/thymira/test_agents_llm.py` override is the model).
- **Baseline** (`--update-baseline`) only bulk findings that a ty upgrade surfaced and that
  cannot be fixed or scoped — after reading each new line and confirming none is a real
  regression. Never run `--update-baseline` before reading the new diagnostics, and never to
  land code that added a diagnostic.

## Rules

1. `ty.toml` is the only ty config; never add `[tool.ty]` to `pyproject.toml`, because
   `ty.toml` takes precedence and the two drift into a silent split brain.
2. New code adds zero diagnostics. The baseline rises only through a deliberate
   `--update-baseline` tied to a ty upgrade; never hand-edit `.ty-baseline.json`.
3. Never blanket-silence (`all = "ignore"`, a file-wide ignore without a reason, or
   `# type: ignore`); scope every suppression and say why.
4. A ty upgrade is one atomic commit — pin bump + `uv lock` + (if unavoidable)
   `--update-baseline` land together — so `main` never holds a pin and a baseline that disagree.
5. `python-version` in `ty.toml` follows `requires-python` (3.12), not the `.python-version`
   dev pin (3.13), because CI must catch what the oldest supported runtime sees.
6. A new workspace member adds its `src` path to `root` in `ty.toml`, or ty resolves its
   imports as unknown.

## Workflow: a diagnostics-increased failure

1. Read them: `just typecheck` lists every diagnostic in concise form
   (`file:line:col: severity[rule] message`).
2. If it follows a ty bump, `git diff -- pyproject.toml uv.lock` must show only the pin and its
   lock block; a mixed diff means you cannot attribute the new diagnostics to the tool, so
   stop and separate the changes.
3. Triage each line with **Decide** above; fix first, then re-run `just typecheck` until it
   prints `All checks passed!`.
4. Only for unavoidable upgrade noise: `uv run python ./scripts/ty_ratchet.py --update-baseline`.
5. Verify the gate CI runs: `uv run python ./scripts/ty_ratchet.py --strict` prints `OK`.
6. Commit the fixes (and, for an upgrade, `pyproject.toml`, `uv.lock`, `.ty-baseline.json`)
   together.

## In this repository

- Baseline: `.ty-baseline.json` = `{"total": 0, "by_rule": {}}`.
- Config: `ty.toml` is the single source (`python-version = "3.12"`, `root` lists every
  member's `src`, scope in `[src].include`, concise output, one `[[overrides]]` for the fake
  `litellm` module in the LLM tests). Pin: `typecheck = ["ty==0.0.73"]` in `pyproject.toml`.
- Whether the ratchet is strict differs by gate:

| Gate | Strict? | Fails on |
|---|---|---|
| `just typecheck-ratchet`, `just check`, `just ci`, pre-commit `ty-ratchet` | no | any diagnostic (count > 0) |
| GitHub Actions `quality` job (`--strict`) | yes | any diagnostic **or** a stale baseline |

Versions checked: 2026-08 (ty 0.0.73).

## Related skills

- **ruff** — style, lint, imports, and `noqa` (not ty diagnostics).
- **packaging-scaffolding** — the `typecheck` group, the pin, `uv lock`, new members.
- **python-debug** — when a diagnostic points at a real bug rather than a typing gap.
