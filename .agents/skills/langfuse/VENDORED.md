# Vendored skill — do not edit

`SKILL.md` and `references/` in this directory are a **verbatim copy** of the upstream
Langfuse agent skill. They are vendored (not symlinked: Windows checkouts run with
`core.symlinks=false`) so Codex, Cursor and Gemini CLI read them from `.agents/skills/` and
Claude Code reads the generated copy in `.claude/skills/`.

| | |
|---|---|
| Upstream | <https://github.com/langfuse/skills> (`skills/langfuse`) |
| Plugin version | 1.7.1 |
| Commit | `4a77f3a0c1a15c6e1e62d0e060e0eabe74ad5bda` (2026-08-28) |
| Vendored on | 2026-09-01 |
| Licence | MIT — see `LICENSE`, © Langfuse GmbH |

Only the line endings were normalised to LF (`.gitattributes`); the content is unmodified.

## Updating

```bash
git clone --depth 1 https://github.com/langfuse/skills.git /tmp/langfuse-skills
# copy /tmp/langfuse-skills/skills/langfuse/{SKILL.md,references} over this directory, LF endings
just validate-skills && just sync-skills
```

Then update the table above with the new commit and version. Never hand-edit `SKILL.md`: the
repository's own conventions live in `AGENTS.md` and in the sibling `python-*` skills, and a
local edit here would be silently reverted by the next update.
