---
name: repo-skeleton
description: Use when deciding where a roadmap item lives (which member, subpackage, service, adapter, doc or test), creating folders, modules, packages, services or placeholders, turning a README placeholder into code, organising a member's internals, or planning the files for a week of the MVP roadmap.
metadata:
  version: "1.0.0"
---

# Growing the repository skeleton

The tree is fixed by the E2E baseline (`docs/architecture/e2e-baseline-v2.md`, §29) and filled
in the order of `docs/roadmap/` (`product-final.md`, `mvp-minimum.md`). Every directory has one
purpose, one owner and one place in the import order; growth means turning a placeholder into a
member, and a member
into modules and subpackages — never inventing a new top-level home.

## When to use

- "Where does X go?" for a roadmap task, a new concern, a data file, a compose file, a doc.
- Creating a folder, module, subpackage, workspace member, service, adapter or placeholder.
- A member is growing past a handful of modules, or a `utils.py` is about to appear.
- Not for: the `pyproject.toml`/`uv` mechanics of a member (`packaging-scaffolding`), the
  import rules themselves (`check-imports`), or splitting a class (`python-god-classes`).

## The map

| Directory | What lives there | Rule |
|---|---|---|
| `packages/schemas`, `packages/events` | Contract v0.5 types; canonical JSON, hashing, redaction, event logs | import nothing from the runtime; every shared type lives here once |
| `runtime/<layer>` | the IP, one member per layer: `core` → `thy` \| `mira` → `agents` → `tools` → `policies` → `state` → `events` → `schemas` | a member imports only members below it; THY and MIRA never each other |
| `apps/api`, `apps/web` | FastAPI (the only door to the runtime); the web client (TypeScript) | the API imports `thymira.core`; the web app calls the API |
| `adapters/*` | thin clients: CLI, IDEs, coding agents | no runtime member imported — HTTP to the API only |
| `services/*` | separately deployed processes (workers, sandbox-manager) | a member when it gets code; talks to the runtime through contracts/queues, not imports |
| `infrastructure/*` | compose, Kubernetes, Terraform | no Python; the root `compose.yaml` stays the dev image only |
| `examples/<project>/.thymira/` | reference projects (config, policies, context) | data and YAML, never code |
| `docs/` | `architecture/`, `roadmap/`, `contracts/`, `adr/`, `superpowers/{specs,plans}`, `legacy/` | design before code; decisions as ADRs |
| `tests/thymira/`, `tests/tooling/` | one module per member; one per script | lane by marker (`python-testing`) |
| `scripts/`, `.agents/skills/` | repo tooling; agent skills | tested in `tests/tooling/`; see `skill-creator` |

Roadmap item → exact location: `references/roadmap-to-layout.md`.

## Rules

1. **No new top-level homes.** Everything fits the map; `src/`, `common/`, `utils/`, `lib/` or
   `shared/` at any level is the wrong answer — the shared thing belongs to the lowest member
   that owns the concern (usually `thymira.schemas` or `thymira.events`).
2. **Vertical slice first** (roadmap §3): wire the thinnest end-to-end path through the layers
   (CLI → API → core → THY → tools → events) before widening any member.
3. **Placeholder → member** keeps the README (purpose, owner, priority) and adds
   `pyproject.toml` + `src/thymira/<name>/__init__.py`, registered in the five places
   (`packaging-scaffolding`) plus a `layers` line; the README is rewritten when code lands.
4. **Inside a member, one concern per module**, named by the concern: `models.py` (types),
   pure logic (`engine.py`, `controls.py`), edge classes (`gate.py`, `local_store.py`),
   `loader.py`; data next to the code that reads it (`defaults/*.yaml`, listed in
   `./scripts/check_wheel.py`). Five or more modules on one concern, or a distinct lifecycle →
   a subpackage with its own `__init__.py` (`thymira.mira.checks`).
5. **Public API through `__init__.py` + `__all__`.** Other members import
   `from thymira.policies import Gate`, never a deep path; renaming internals then costs
   nothing.
6. **Shared types go to the contract once.** A type two members need is a `thymira.schemas`
   model; adding a field bumps `CONTRACT_VERSION`, a breaking change gets an ADR.
7. **Services and clients speak contracts, not imports.** A worker or sandbox manager consumes
   `thymira.schemas` over a queue/HTTP; an adapter consumes the API. If it needs `thymira.core`,
   it is part of the runtime, not a service.
8. **Every directory has a README until it has code**, with purpose, owner (P1–P5) and
   priority; AGENTS.md's repository map and `CHANGELOG.md` change in the same commit as the
   tree.
9. **Design before structure when the change is lasting**: a spec in `docs/superpowers/specs/`,
   a plan in `docs/superpowers/plans/`, an ADR in `docs/adr/` for a decision the next person
   must not reopen.

## Workflow — placing a roadmap item

1. Find the item in the roadmap (person, week, day) and its section in the baseline; read
   `references/roadmap-to-layout.md` for the prescribed location.
2. Pick the member from the map; confirm the import direction allows every dependency the
   item needs (`check-imports`). If a dependency points upward, the item is in the wrong
   member or the shared part belongs lower.
3. Placeholder? Create the member (`packaging-scaffolding`). Existing member? Add the module or
   subpackage (Rule 4), export the public API (Rule 5).
4. Shared types first (`thymira.schemas`), then pure functions, then the edge class; tests in
   `tests/thymira/test_<member>.py` (split by concern past ~300 lines).
5. Update the member README, AGENTS.md map, `CHANGELOG.md`; run `just check`.

## In this repository

- 11 members registered; code in `schemas`, `events`, `state`, `policies`, `agents`, `core`,
  `thy` (ThyGraph + `nodes/` + `agents/`), `mira` (`checks`, `preflight`, `orchestrator`,
  `context`, `graph`), `tools` (registry, manager, builtins, MCP, sandboxes) and `cli` (`run`,
  `status`); `api` is a typed-boundary skeleton (route handlers not yet wired); README
  placeholders in `apps/web`, `adapters/{vscode,opencode,claude-code,codex,generic}`,
  `packages/sdk-*`, `services/*`, `infrastructure/*` (2026-08-26).
- Precedents: a member — `runtime/policies` (`models.py`, `engine.py`, `gate.py`, `loader.py`,
  `defaults/`); a subpackage — `runtime/mira/src/thymira/mira/checks/`; a project —
  `examples/credit-risk/.thymira/`; an ADR — `docs/adr/0002-legacy-disposition.md`.
- Owners: P1 Runtime/Tech Lead (`core`, `state`, `api`, contracts), P2 THY/Agents (`thy`,
  `agents`), P3 Tools/Execution/MLflow (`tools`, `services/sandbox-manager`), P4 MIRA/Governance
  (`mira`, `policies`), P5 CLI/UX/QA (`adapters/cli`, `apps/web`, tests, demo).

## Common mistakes

| Rationalization | Reality |
|---|---|
| "I'll put the helper in `runtime/core`; everything can import it." | `core` is the *top* of the runtime (it composes THY and MIRA); nothing below may import it. Shared helpers go to `events`/`schemas` or the lowest owning member. |
| "A `runtime/common` member for shared code." | Rule 1. Name the concern and put it in the member that owns it; the contract is the only deliberately shared place. |
| "The PostgreSQL repository can live in `apps/api` next to the endpoints." | Persistence is `thymira.state`; the API composes, it does not own state. |
| "The worker can import `thymira.core` directly — same repo." | Then it is not a service. Services consume contracts over a queue/HTTP so they can be deployed apart. |
| "I'll add the compose file for PostgreSQL at the root." | `infrastructure/docker/`; the root `compose.yaml` is the dev image. |

**Red flags — stop:** a new top-level directory; `utils.py`/`helpers.py`/`common/`; a deep
import (`from thymira.policies.engine import …`) from another member; a folder without a
README; a member created without its five registrations.

## Related skills

- **packaging-scaffolding** — creating the member. **check-imports** — the direction.
  **python-general** — module and class design. **changelog** — the entry. **skill-creator** —
  if the new area needs its own skill.
