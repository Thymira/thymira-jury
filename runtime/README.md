# runtime/

The Thymira Runtime: the product's core and our IP. Everything here is Python, API-first,
client-agnostic and provider-neutral. Import direction, enforced by import-linter
(`just check-imports`, contracts in the root `pyproject.toml`):

```
apps/api
    |
 runtime/core                       (Run/Session services; composes the two graphs)
    |
 runtime/thy   |   runtime/mira     (two independent orchestrators — never import each other)
    |
 runtime/agents
    |
 runtime/tools
    |
 runtime/policies
    |
 runtime/state
    |
 packages/events
    |
 packages/schemas
```

A member imports only members below it. `adapters/*` import no runtime member at all: they
talk to the API over HTTP. Where a roadmap item goes inside this tree: the `repo-skeleton`
skill (`.agents/skills/repo-skeleton/`).
