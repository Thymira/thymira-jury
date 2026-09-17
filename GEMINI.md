@AGENTS.md

## Gemini CLI specifics

- Skills are discovered from `.agents/skills/` (an alias of `.gemini/skills/`); Gemini asks for
  confirmation the first time each skill activates.
- No `.gemini/settings.json` is required: this default context file imports `AGENTS.md`.
