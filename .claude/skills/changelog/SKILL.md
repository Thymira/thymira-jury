---
name: changelog
description: Use when about to commit a user-visible change, when a PR touches a workspace member (packages/, runtime/, apps/, adapters/) without a CHANGELOG.md entry, when asked to update the changelog or prepare, tag or publish a release, or when deciding which Conventional Commit type a change is.
metadata:
  version: "1.0.0"
---

# Changelog and releases

CHANGELOG.md is a curated, user-facing history: one line per change a user can observe,
written from the diff in front of you and grouped by Keep a Changelog section. Preparing a
release moves the accumulated `## [Unreleased]` entries into a dated version section. You edit
the existing file from what changed on this branch; you never reconstruct or re-tag versions
that already shipped, and you never audit `git log`/`reflog` to decide what to write.

## When to use

- Before committing a change that touches a workspace member (`packages/`, `runtime/`,
  `apps/`, `adapters/`): add a line under `## [Unreleased]`.
- When asked to update the changelog, or to prepare, tag or publish a release (`X.Y.Z`).
- When deciding which Conventional Commit type a change is, or which section it belongs in.
- When the changelog gate reports a runtime change with no CHANGELOG.md edit.
- Not for internal-only work (private refactor, tests, docs, chore, ci, build) with no
  observable effect — that gets no line; opt out with `[skip changelog]` in the commit.

## Quick start

```
# Does the current branch need a changelog entry it is missing?
uv run python .agents/skills/changelog/scripts/check_changelog.py --base origin/main
```

Exit 1 means a member file changed without a CHANGELOG.md edit and no `[skip changelog]`.

## Rules

1. Curate for users, do not dump commits: turn the diff into user-facing lines, not your git
   subjects.
2. Map the commit type to a section: `feat`->Added, `fix`->Fixed, `perf`->Changed, a breaking
   change -> a **Breaking** callout under Changed or Removed. `docs`/`test`/`chore`/`ci`/
   `build`/`refactor` get **no line** unless a user can observe the effect, because a private
   rename such as `_advance`->`_advance_phase` changes nothing a user sees.
3. Add entries under `## [Unreleased]` with the newest section on top; never append at the
   bottom, because readers scan top-down for the latest.
4. A release cut *moves* the `[Unreleased]` entries into a new `## [X.Y.Z] - YYYY-MM-DD`
   section and leaves a fresh empty `[Unreleased]`; you edit accumulated entries, you do not
   rewrite the file.
5. Cut exactly the one version you are releasing: never invent a section or a `git tag` for a
   version that already shipped, and never record a date you believe is wrong — an inaccurate
   history is worse than a missing one.
6. Commit CHANGELOG.md, the `[project].version` bump and `uv.lock` together, then tag; the tag
   must point at the commit that already contains the changelog.
7. Update the bottom compare links every release, using the owner from `git remote get-url
   origin`, so the diff links keep resolving.

## In this repository

- The changelog is `CHANGELOG.md` (Keep a Changelog 1.1.0). It already has `## [Unreleased]`
  and `## [0.1.0] - 2026-08-19` with compare links at the bottom — edit it, do not recreate it.
- The version lives in the root `pyproject.toml` `[project].version` (the virtual workspace
  root) and in every member's `pyproject.toml`; they move in lockstep, and `uv.lock` records
  them, so run `uv lock` after the bump.
- Compare-link owner is `Thymira` (`git remote get-url origin` -> github.com/Thymira/thymira);
  tags are `vX.Y.Z`. No release is tagged yet, so the first cut adds a `[X.Y.Z]` compare link
  and repoints `[unreleased]`.
- Run the gate (`check_changelog.py`, shown in Quick start) before pushing; pass `--json` for
  the CI verdict.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "The commit isn't in history yet, so I can't write the entry / the task is blocked." | The entry describes the change in front of you (the diff/commit subjects given), not git history — write it from what changed; do not run `git log --all`/`reflog`. |
| "I'll also create the tag for the previous version, retroactively." | A release cuts one version. Tagging or adding a section for a version that already shipped invents history — leave prior releases untouched. |
| "The date looks a day off, but I'll keep it and flag it for the author." | Never write a date you believe is wrong; a released section is dated with today's date in `YYYY-MM-DD`. |
| "This refactor was real work, it deserves a line." | User-visibility, not effort, decides: a private/internal change gets no line (or `[skip changelog]`). |

Red flags — stop if you catch yourself: running `git reflog`/`git log --all` to write a
changelog; adding a section or tag for a version you did not just cut; pasting a commit subject
verbatim; tagging before the changelog commit exists.

## Related skills

- **packaging-scaffolding** — the `[project].version` bump and `uv lock`.
- **task-runner** — where the repository's release and check recipes live.
