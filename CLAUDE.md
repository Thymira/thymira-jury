@AGENTS.md

## Claude Code specifics

- A `PostToolUse` hook in `.claude/settings.json` runs ruff on every Python file you edit; do not
  format by hand.
- `.claude/skills/` is generated from `.agents/skills/` — edit the canonical copy and run
  `just sync-skills`.
- `.claude/settings.json` is shared with the team; put personal overrides in
  `.claude/settings.local.json` (gitignored).
