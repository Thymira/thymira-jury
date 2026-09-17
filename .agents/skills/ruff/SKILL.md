---
name: ruff
description: Use when ruff check or ruff format fails locally, in pre-commit or in CI, when adding a noqa, changing [tool.ruff] in pyproject.toml, adjusting a rule or per-file ignore, or when asked to lint, format, clean up or sort imports in Python files.
metadata:
  version: "1.0.0"
---

# ruff

ruff is the lint + format gate. The only ways past a finding are to fix the code or to record a
scoped, reasoned exception — never to widen a rule's blind spot.

## When to use

- `ruff check` or `ruff format --check` fails locally, in pre-commit, or in CI.
- You are adding a `# noqa`, editing `[tool.ruff*]` in `pyproject.toml`, or asked to lint,
  format or clean up.
- **Not for**: type errors (see `ty`), import-layer violations (see `check-imports`), or
  choosing test lanes (see `python-testing`).

## Quick start

```bash
just lint          # ruff check + ruff format --check + yamllint (what CI gates on)
just lint-fix      # apply SAFE fixes, then format
just fmt           # format only
uv run ruff check --select CODE --statistics .   # count one rule's hits
uv run ruff check --select CODE .                # locate them
```

## Fix, suppress, or configure

You have a finding. Take the **first** row that applies — never skip to a broader remedy.

| Situation | Do this | Never |
|---|---|---|
| Real defect | Fix the code (a genuinely bad name: rename it; a missing docstring: write a useful one). | suppress it |
| ruff offers a *safe* autofix | `just lint-fix`, then re-check. | `--unsafe-fixes` unattended |
| False positive on one or a few lines | `# noqa: CODE  # reason` on each line. | a bare `# noqa`; a per-file-ignore |
| False positive for a whole **kind** of file (all tests, repo scripts) | add `CODE` to that file glob in `[tool.ruff.lint.per-file-ignores]`. | a glob matching one arbitrary module |
| A naming rule hits a codebase-wide convention (`X`, `y`) | whitelist the name in `[tool.ruff.lint.pep8-naming] extend-ignore-names`. | one per-file-ignore per module using it |
| `TC001`/`TC003` on an import used by a pydantic field | nothing — `runtime-evaluated-base-classes` already covers `ThymiraModel`; if it fires, the class does not inherit from it and the import *should* move under `TYPE_CHECKING`. | a noqa |

## Rules

1. Never a bare `# noqa` — always `# noqa: CODE  # reason`; a bare noqa mutes every rule on the
   line and hides the next unrelated bug.
2. `per-file-ignores` are for *kinds* of files (tests, scripts, skills' scripts); a per-module
   entry blinds you to genuine future hits there.
3. The top-level `ignore` is short and deliberate (formatter conflicts, `TRY003`, `ANN401`,
   `D105`/`D107`, `PLR0913`); adding to it is a repo-wide decision that goes through review —
   never a way to dodge a handful of findings.
4. Never `--unsafe-fixes` unattended — unsafe fixes can change behaviour; hand-write the
   equivalent so the diff is reviewed. (Moving imports under `TYPE_CHECKING` is the one unsafe
   fix used here, and only after checking no pydantic model needs the name at run time.)
5. Format after fixes, never before — pre-commit runs `ruff-check --fix` then `ruff-format` for
   exactly this reason.
6. All config lives in `pyproject.toml` `[tool.ruff*]`; a `ruff.toml` would split and drift the
   configuration.

Then reformat (`just fmt`) and prove it: `uv run ruff check --select CODE .`, `just lint`,
`just test`.

## In this repository

- ruff 0.16.3 (`ruff>=0.16.3,<0.17`); config only in `pyproject.toml`: `[tool.ruff]`
  (`line-length = 100`, `target-version = "py312"`), `[tool.ruff.lint]` (the full set:
  pycodestyle, pyflakes, isort, **pydocstyle (Google)**, **annotations**, bugbear, pyupgrade,
  naming, simplify, ruff, pytest-style, pathlib, print, unused-args, tryceratops, eradicate,
  pylint incl. complexity, bandit, type-checking, …), `[tool.ruff.format]` (excludes `*.md`, so
  Markdown fences stay verbatim). Versions checked: 2026-08.
- `per-file-ignores`: `__init__.py` (`F401`), `tests/**` (asserts, magic numbers, private
  access, missing docstrings/annotations, local imports), `scripts/**` and `.agents/skills/**`
  (prints, subprocess, numeric thresholds). No per-module entries — do not add one.
- `[tool.ruff.lint.pylint] max-args = 7`; `[tool.ruff.lint.flake8-type-checking]
  runtime-evaluated-base-classes` lists `pydantic.BaseModel` and `thymira.schemas.ThymiraModel`.
- pre-commit runs `ruff-check --fix` then `ruff-format`; CI and `just check` gate on `just lint`.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "A per-file-ignore per module that uses the convention — like the `tests/**` entry." | That covers a *kind* of file. A convention in a few functions is a per-line `# noqa: CODE  # reason`; a name used codebase-wide (`X`) goes in `pep8-naming` `extend-ignore-names`. |
| "30 hits of `D103`, docstrings are churn — into the top-level `ignore`." | Docstrings are part of the contract here (agents read them). Write one-line Google docstrings; the rule stays. |
| "ruff wants this import under `TYPE_CHECKING`, so I'll move it." | If a pydantic field uses the name, the model breaks at import time. Check the class first; `ThymiraModel` subclasses are already exempt. |

**Red flags — stop if you catch yourself:** a `per-file-ignores` glob matching a single ordinary
module; a `# noqa` with no code or no reason; removing a rule from `select` or adding it to
`ignore` to make one file pass; reaching for `--unsafe-fixes` or a `ruff.toml`; running
`ruff format` before the fixes.

## Related skills

- **ty** — type diagnostics. **check-imports** — import-layer violations. **task-runner** — the `just lint*` recipes.
