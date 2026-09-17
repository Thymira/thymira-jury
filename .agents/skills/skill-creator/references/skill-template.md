---
name: <directory-name>
description: Use when <situation 1>, <situation 2 — a failing command or file name>, or <symptom>; not for <the sibling skill's job>.
metadata:
  version: "1.0.0"
---

# <Title: the capability in three to five words>

<Two or three sentences: what the skill makes the agent do and the one judgment it protects.>

## When to use

- <Concrete trigger: a command that fails, a file being edited, a request wording.>
- <Another trigger.>
- Not for: <what belongs to another skill> (`<that-skill>`).

## Quick start

```bash
just <recipe>            # <what it proves>
uv run <command>         # <when to use it instead>
```

## Rules

1. **<Rule as an imperative.>** <Why it exists; what goes wrong without it.>
2. **<Rule.>** <Why.>
3. **<Rule.>** <Why.>

## Workflow

1. <First step, with the command.>
2. <Decision point: "if X, stop and …">
3. <Verification: the exact output that means done.>

## In this repository

- <Fact with a path: where the config/code lives.>
- <Fact with a number: counts, thresholds, versions.> Versions checked: <YYYY-MM>.
- <Precedent: a file or commit to copy.>

## Common mistakes

| Rationalization | Reality |
|---|---|
| "<What an agent says to itself to skip the rule.>" | <Why that is wrong and what to do instead.> |
| "<Another one.>" | <Reality.> |

**Red flags — stop:** <observable sign 1>; <sign 2>; <sign 3>.

## Related skills

- **<skill>** — <when to hand over>. **<skill>** — <when to hand over>.
