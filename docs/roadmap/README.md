# Roadmap

Two documents, one catalogue. **Every task in the project is defined exactly once**, in
`product-final.md`; `mvp-minimum.md` selects and orders the subset marked MVP. A task can never
say two different things in two places.

| Document | What it is | Authority over |
|---|---|---|
| [`product-final.md`](product-final.md) | The 131-task product catalogue, each task with its deliverable, acceptance criterion and seam. Also carries the tier-boundary rule and the corrections to apply before the contract freezes. | **What every roadmap task is.** The single catalogue. |
| [`mvp-minimum.md`](mvp-minimum.md) | The 82-task subset that proves the product thesis end to end: load per member, day-1 starts, the critical path and the four acceptance gates. | **What we build first, and who builds it.** |
| [`mvp-3-weeks.md`](mvp-3-weeks.md) | The original 3-week plan (2026-08-20). **Superseded** by the two documents above for scope, ownership and task definitions. Kept for its strategic reasoning — the levels, what must not change, the management rules. | Historical context only. |
| [`mira-phase-1-closeout.md`](mira-phase-1-closeout.md) | The closeout record of MIRA's first, deliberately bounded local phase and its credit-risk E2E evidence (`tests/thymira/test_mira_e2e.py`). Explicitly excludes THY consumption of MIRA snapshots, automatic context updates, PostgreSQL, brokers/workers, web surfaces, distributed deployment, and real model calls. | Historical record of a completed phase; not a plan. |

## Status, and why it is checked by a script

The MVP's 82 completed tasks describe that bounded delivery. They do not establish completion
of the DeepSeek Harness adoption program or ADR-0013. Its cross-cutting acceptance criteria
are tracked in the [F1–F13 acceptance record](../superpowers/plans/2026-09-07-dsh-acceptance.md),
which supplements these task definitions without replacing their IDs or status gates. A
foundation remains open while any accepted COPY/ADAPT criterion lacks its required evidence;
an aggregate percentage or a completed subset cannot compensate for that gap.

Every task row carries a **Status**: `todo`, `partial` or `done`. It is the answer to the only
question a document like this must answer before you start work — *does this already exist?*
`partial` means a real part of the task is already in the repository (a frozen contract, a
protocol, a written specification) but its acceptance criterion is not yet met; the task's
detail section names what is already there, so you extend it instead of rebuilding it.

A task's row is repeated in three places: the summary table, the detail section and — for MVP
tasks — the per-member table in `mvp-minimum.md`. Five engineers point their own AI assistants
at these documents, and an assistant does not notice a stale row: it implements it. So the
agreement is not left to discipline. `just check-roadmap` (`scripts/check_roadmap.py`, part of
`just check` and of the pre-commit hooks) fails when any two views disagree on a task's title,
tier, owner, size, status or dependencies, when a declared count, the aggregate status line or
the load-per-member table no longer matches the tables, when a dependency does not exist, when
an MVP task depends on FINAL work, or when the dependencies form a cycle.

**When you finish a task, change its status in all three places and run `just check-roadmap`.**

## The rule that governs both

> **The MVP cuts implementations, never contracts or seams.**

Going from the MVP to the final product must require no refactor: no rewritten logic, no changed
call sites, no contract migration. Every MVP task therefore defines the full, final-shape type,
protocol and event vocabulary, and puts a reduced implementation behind it. The tier-boundary
table in `product-final.md` is the decision procedure when you are unsure which side a piece of
work falls on.

## Ownership

The five roles (P1 Runtime/Tech Lead, P2 THY/Agents, P3 Tools/Execution/MLflow, P4
MIRA/Governance, P5 CLI/UX/QA/Integration) are defined in [`mvp-3-weeks.md`](mvp-3-weeks.md); the
`P1`–`P5` labels used across this repository's READMEs refer to them.
