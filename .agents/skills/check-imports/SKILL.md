---
name: check-imports
description: Use when adding an import between thymira workspace members, creating a new member or a new module that crosses a layer, when lint-imports (scripts/lint_imports.py) reports a broken contract, or when wiring THY, MIRA, agents, tools, policies and core together.
metadata:
  version: "1.0.0"
---

# Check imports

## Overview

import-linter contracts in `pyproject.toml` (`[tool.importlinter]`) define which member may
import which; the layering only holds if you run the check. A passing `pytest` run proves
behaviour, never import direction — so verifying the architecture still holds after an import
change means running `just check-imports`.

## When to use

- Adding an import between members (`packages/*`, `runtime/*`, `apps/*`, `adapters/*`).
- Creating a new workspace member or a module that needs something from another member.
- `just check-imports` reports `Contracts: N kept, M broken`.
- Wiring THY, MIRA, agents, tools, policies or core so one needs data another holds.
- Not for: sorting/reformatting imports (`ruff` skill) or adding a dependency
  (`packaging-scaffolding`).

## Quick start

```bash
just check-imports        # the import-linter contracts — the source of truth
```

Preview one member's internal edges straight from the AST (no install, no config):

```bash
uv run python .agents/skills/check-imports/scripts/import_graph.py runtime/mira/src/thymira/mira
```

## The contracts

```
thymira.api → thymira.core → thymira.thy | thymira.mira → thymira.agents → thymira.tools
            → thymira.policies → thymira.state → thymira.events → thymira.schemas
```

1. **Runtime layers** (above): a member imports only members below it.
2. **THY and MIRA are independent**; they are composed in `thymira.core` only.
3. **`thymira.schemas` / `thymira.events` import no runtime, app or adapter** member.
4. **`thymira.cli` imports no runtime member** — clients own no state and talk HTTP to the API.

`thymira` is a namespace package, so every member is listed in `root_packages`; a member that
is not listed is invisible to every contract.

## Rules

1. Run `just check-imports` after any change to imports or module layout, and treat its
   `0 broken` line as the only proof it holds; a passing suite says nothing about import
   direction.
2. A broken contract is a design signal: fix it by moving the shared thing *down* (a protocol,
   a `thymira.schemas` type, an event) or by reading an artifact the producer already writes —
   never by importing upward.
3. Never reorder `layers` or add `ignore_imports` to silence a violation; allow an exception
   only as a last resort, with a `# TODO(arch):` comment and a tracked issue, because a hidden
   edge is a regression nobody finds.
4. MIRA reads evidence (`thymira.events`, `thymira.state`), never THY's objects; THY never reads
   audit internals. Anything both need lives in `thymira.schemas`.
5. Member `pyproject.toml` dependencies follow the same direction: a `{ workspace = true }`
   source pointing up the list is the same violation one level earlier.
6. `exclude_type_checking_imports = true`: imports under `if TYPE_CHECKING:` are free — use
   them for annotation-only dependencies that would otherwise point upward (but never for
   pydantic field types, which are evaluated at run time).

## Workflow

1. Name both members and find their positions in the layer list (or `CONTRIBUTING.md`).
2. If the edge points up the list or across `thy | mira`, stop and redesign (Rule 2) before
   writing it.
3. Make the change.
4. Verify with `just check-imports`; require `Contracts: 4 kept, 0 broken`. Mandatory, never
   replaced by running tests.
5. If a contract breaks, read the edge, return to step 2; add an `ignore_imports` exception
   (Rule 3) only if redesign is out of scope.

## In this repository

- `just check-imports` runs `lint-imports` behind a UTF-8 wrapper (Windows pipes are cp1252 and
  crash the raw tool). Four contracts, listed above, all kept when last verified (2026-08-22); the module and edge
  counts it prints grow with the code and are not a target.
- A new member is added in the same change to `[tool.uv.workspace].members`, `ty.toml`,
  `root_packages`, one `layers` line and `tests/thymira/test_workspace_imports.py`.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "The tests pass, so the architecture still holds." | pytest never checks import direction; only `just check-imports` does. Run it. |
| "It's one small import so MIRA can read THY's plan." | That is the cross-orchestrator import the contracts forbid; route it through `thymira.schemas`/`thymira.events` or an artifact. |
| "The CLI can import `thymira.core` to save an HTTP call." | Clients own no state; the CLI contract exists precisely to stop that. |
| "I'll reorder the layers / add `ignore_imports` to go green." | That hides the regression; fix the direction, or add a `TODO(arch)` exception with an issue. |

Red flags — stop if you catch yourself: your verification list has `pytest`/`ruff` but no
`just check-imports` line; you added an import that points up the layer list, or between
`thy` and `mira`; you edited the `layers` order or `ignore_imports` to clear a violation.

## Related skills

**packaging-scaffolding**, **ruff**, **python-testing-integration**.
