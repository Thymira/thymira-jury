<!-- Title: Conventional Commit (feat(scope): …, fix: …, docs: …). Small PRs, merged daily. -->

## What and why

<!-- One paragraph: the change, the reason, the roadmap item or issue it serves. -->

## How it was verified

<!-- Paste the proof, not a promise: `just check` output, `Contracts: 4 kept, 0 broken`, `N passed`. -->

## Checklist

- [ ] Tests in the right lane (`python-testing`): unit / `integration` / `slow`
- [ ] `CHANGELOG.md` `[Unreleased]` updated, or the commit says `[skip changelog]`
- [ ] `uv.lock` committed next to any `pyproject.toml` change
- [ ] Import direction unchanged or justified (`just check-imports`), no new `ignore_imports`
- [ ] No LLM output becomes an authorization; events stay redacted and chained
- [ ] Docs/skills touched by this change updated (`AGENTS.md` map, `.agents/skills`, READMEs)

Skills applied: <!-- names only, e.g. python-testing-unit, check-imports -->
