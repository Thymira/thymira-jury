# pyproject anatomy (workspace root and one member)

Two shapes exist in this repository: the **virtual root** (`pyproject.toml`) and a **member**
(`<dir>/pyproject.toml`). Everything else in the root file is tool configuration (ruff, pytest,
coverage, import-linter) — ty lives in `ty.toml` on purpose.

## The virtual root

```toml
[project]
name = "thymira-workspace"          # never published, never installed
version = "0.1.0"                   # moves in lockstep with the members
requires-python = ">=3.12"          # the floor; .python-version (3.13) is only the dev pin
dependencies = []                   # the root owns no runtime dependency

[dependency-groups]                 # PEP 735: local tooling, never in a wheel
test = ["pytest>=9.1,<10", "pytest-cov>=7.1,<8", "pytest-xdist>=3.8,<4", "hypothesis>=6.165"]
lint = ["ruff>=0.16.3,<0.17", "import-linter>=2.13,<3", "yamllint>=1.38,<2", "pre-commit>=4.6,<5"]
typecheck = ["ty==0.0.73"]         # pre-1.0: pinned exactly
dev = [{ include-group = "test" }, { include-group = "lint" }, { include-group = "typecheck" }]

[tool.uv]
package = false                     # virtual root: no build backend, nothing to install
default-groups = ["dev"]            # a bare `uv sync` installs dev
required-version = ">=0.12"

[tool.uv.workspace]
members = ["packages/schemas", "packages/events", "runtime/core", "runtime/state",
           "runtime/tools", "runtime/policies", "runtime/agents", "runtime/thy",
           "runtime/mira", "apps/api", "adapters/cli"]
```

- `uv sync --all-packages` installs every member (editable) plus the default groups;
  `--all-groups` adds the opt-in groups; `--locked` refuses a stale lock.
- There are no extras at the root. An optional feature belongs to the member that implements
  it, as that member's `[project.optional-dependencies]`.

## A member (`runtime/policies/pyproject.toml`)

```toml
[project]
name = "thymira-policies"           # distribution name: thymira-<x>
version = "0.1.0"
description = "Policy Engine: rules as data, four-value decisions, Gate."
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "thymira-schemas",              # siblings by distribution name …
    "thymira-events",
    "pyyaml>=6,<7",                 # … third parties with an upper bound
]

[tool.uv.sources]
thymira-schemas = { workspace = true }   # … resolved from the workspace, not PyPI
thymira-events = { workspace = true }

[build-system]
requires = ["uv_build>=0.12.5,<0.13"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "thymira.policies"    # the import path
namespace = true                    # `thymira` has no __init__.py anywhere
```

- Source layout: `runtime/policies/src/thymira/policies/__init__.py` (+ modules, + data such as
  `defaults/*.yaml`, which `uv_build` includes automatically — verify with
  `./scripts/check_wheel.py`).
- A member's sibling dependencies must point *down* the import-linter layer list
  (`api → core → thy | mira → agents → tools → policies → state → events → schemas`).
- Console scripts: `[project.scripts] thymira = "thymira.cli.__main__:main"` in `adapters/cli`.

## uv command table

| Task | Command |
|---|---|
| Install everything | `uv sync --locked --all-groups --all-packages` (`just setup`) |
| Add a runtime dep to a member | `uv add --package thymira-tools <pkg>` |
| Add a tool to a group | `uv add --group lint <pkg>` |
| Remove | `uv remove --package thymira-tools <pkg>` / `uv remove --group lint <pkg>` |
| Re-lock after a hand edit | `uv lock` |
| Is the lock current? | `uv lock --check` (`just lock-check`) |
| Build every member | `uv build --all-packages` then `uv run python ./scripts/check_wheel.py` |
| Run a member's script | `uv run thymira --help` |
| Upgrade one package within its range | `uv lock --upgrade-package <pkg>` |

## Where a new member is registered

1. `[tool.uv.workspace].members` (root `pyproject.toml`)
2. `root` in `ty.toml` (`./<dir>/src`)
3. `[tool.importlinter] root_packages` and one `layers` line (root `pyproject.toml`)
4. `MEMBERS` in `tests/thymira/test_workspace_imports.py`
5. `EXPECTED_WHEELS` in `./scripts/check_wheel.py`

Ruff's `src` globs (`packages/*/src`, `runtime/*/src`, `apps/*/src`, `adapters/*/src`) need no
change for a member under one of those directories.
