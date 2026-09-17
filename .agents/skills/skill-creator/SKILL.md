---
name: skill-creator
description: Use when creating a new agent skill under .agents/skills, revising an existing SKILL.md, adding a reference or script to a skill, or when just validate-skills or the sync-skills hook fails.
metadata:
  version: "1.0.0"
---

# Authoring agent skills

A skill is a compact, operational instruction package one directory deep:
`.agents/skills/<name>/SKILL.md` plus optional `references/` and `scripts/`. It is read by
Codex, Cursor and Gemini CLI from `.agents/skills/` and by Claude Code from the generated copy in
`.claude/skills/`. Rules beat prose; every sentence should change what an agent does.

## When to use

- Creating a skill, or revising one that drifted from the code it describes.
- Adding a `references/*.md` or a `scripts/*.py` to a skill.
- `just validate-skills` or the `sync-skills` pre-commit hook fails.
- Not for: repository conventions themselves (AGENTS.md) or the justfile recipes (task-runner).

## Quick start

```bash
cp -r .agents/skills/skill-creator/references/skill-template.md .agents/skills/<name>/SKILL.md
just validate-skills && just sync-skills      # then commit .agents/skills and .claude/skills
```

## Rules

1. **One capability per skill**, named by the trigger (`python-debug`, not `debugging-tips`).
   `name` equals the directory name, lowercase letters, digits and hyphens, ≤ 64 characters.
2. **The description is the router.** Start with "Use when …", list the concrete situations
   (failing command names, file names, symptoms) in ≤ 1024 characters, no XML; the agent sees
   only the description when deciding whether to load the skill.
3. **Frontmatter is portable**: `name`, `description`, `metadata` (with `version`), optionally
   `compatibility` — nothing else; `./scripts/validate_skills.py` rejects other keys.
   `metadata.version` is `"1.0.0"` in every skill: skills are versioned with the repository,
   never individually — do not bump it when you revise a skill.
4. **Body ≤ 500 lines, ideally 400–900 words**, in this order: Overview · When to use (with a
   "Not for" line naming the sibling skill) · Quick start · Rules (numbered, each with the
   *why*) · Workflow (ordered steps) · In this repository (facts, versions, precedents, with a
   "Versions checked" date) · Common mistakes (rationalization → reality table + red flags) ·
   Related skills.
5. **Operational, not descriptive.** Imperatives, exact commands, exact file paths; an example
   only where behaviour is ambiguous without it. State rules the agent can verify ("require
   `Contracts: 4 kept, 0 broken`"), not aspirations ("keep the architecture clean").
6. **Ground every claim in this repository** (paths, commands, counts, thresholds) and date it.
   A skill that describes code that no longer exists is worse than none — revise skills in the
   same change that moves the code.
7. **Heavy material goes to `references/`** (annotated configs, playbooks, long tables) and
   **executable help to `scripts/`** (tested in `tests/tooling/`, ruff/ty apply). Cite them by
   their path relative to the skill directory — the validator checks that every such path
   exists, so placeholders are not allowed; repository-level scripts are cited as `./scripts/…`.
8. **Never edit `.claude/skills/`** — it is generated; `just sync-skills` copies, CI fails on
   drift.
9. **Register it**: a row in the AGENTS.md skills table and a `CHANGELOG.md` line.

## Workflow

1. Collect the evidence: the commands, failures and files the skill must handle; run them.
2. Copy `references/skill-template.md`, fill every section, delete what does not apply.
3. Write the rationalization table from real mistakes you have seen (or made) — that table is
   what stops an agent mid-rationalization.
4. `just validate-skills`, `just sync-skills`, then read the generated copy once as the agent
   would: does the description alone trigger correctly? does every rule tell you *how*?
5. Commit `.agents/skills/<name>/`, `.claude/skills/<name>/`, AGENTS.md, CHANGELOG.md together.

## In this repository

- Validator: `./scripts/validate_skills.py` (frontmatter, key allowlist, body length, referenced
  files); sync: `./scripts/sync_skills.py` (`--check` in CI and pre-commit). Gemini CLI asks
  for confirmation the first time a skill activates; keep descriptions specific so that prompt
  is meaningful. Versions checked: 2026-08.
- Fifteen skills exist; the testing trio (`python-testing` router + `-unit` + `-integration`)
  is the precedent for a router/sub-skill split.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "A general best-practices page is more reusable." | The agent loads a skill by its trigger; a page without concrete situations never triggers and never changes behaviour. |
| "I'll describe the design so the agent understands." | Understanding is not action. Write the rule, the command, the file. |
| "Updating the skill can wait for the next PR." | The next PR is written by an agent reading the stale skill. Same change. |

**Red flags — stop:** a description without "Use when"; a rule without a command or path; a
claim you did not verify in this checkout; an edit under `.claude/skills/`.

## Related skills

- **task-runner** — the `validate-skills`/`sync-skills` recipes. **changelog** — the entry.
