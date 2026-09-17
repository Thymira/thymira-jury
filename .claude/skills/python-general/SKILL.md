---
name: python-general
description: Use when writing or reviewing any Python in this repository — module layout, typing, class vs function design, signatures, exceptions, docstrings, secrets — before a more specific skill (testing, ruff, ty, god-classes, check-imports) applies.
metadata:
  version: "1.0.0"
---

# Python in Thymira

Idiomatic Python 3.12+, typed end to end, machine-checked by ruff (full rule set), ty (zero
diagnostics) and import-linter. This skill is the baseline every other Python skill assumes;
when a more specific skill applies, it wins.

## When to use

- Writing a new module, class or function in any `thymira.*` member or in `scripts/`.
- Reviewing Python for style, typing, structure or safety.
- Unsure whether something should be a class, a function, a pydantic model or an enum.
- Not for: lint failures (`ruff`), type diagnostics (`ty`), test design (`python-testing`),
  splitting a large class (`python-god-classes`), imports between members (`check-imports`).

## Rules

1. **Layout.** One responsibility per module; a module docstring first; imports at the top
   (stdlib → third-party → `thymira.*`), absolute, never inside functions (`PLC0415`) and never
   wrapped in `try/except` unless optional-dependency behaviour is the feature. Members are
   `thymira.<name>` namespace packages — never add `thymira/__init__.py`.
2. **Typing.** Annotate every signature (`ANN`): `X | None`, builtins generics (`list[str]`),
   `collections.abc` for `Callable`/`Sequence`/`Iterator`/`Mapping`, `Literal` and `StrEnum` for
   closed sets, `Protocol` for boundaries (`ArtifactStore`, `EventLog`, `LLMProvider`). `Any`
   only where it is the honest type (JSON payloads) — never to silence ty.
3. **Records are pydantic.** Anything that crosses a boundary or is persisted is a frozen
   `ThymiraModel` (`thymira.schemas`): validated, immutable, `extra="forbid"`. Plain
   `@dataclass` is for in-process helpers (`AuditContext`, `Control`). Imports used by model
   fields stay real imports — pydantic evaluates them at run time (`runtime-evaluated-base-classes`).
4. **Functions for transformations, classes for state.** Pure functions for hashing,
   matching, checks and rendering; a class only when it owns state or a lifecycle (a store, a
   log, a provider, a graph). Prefer composition over inheritance. See `python-god-classes`.
5. **Signatures.** No mutable defaults. Keyword-only (`*`) when parameters share a type, are
   options (timeouts, limits, flags) or are added to a public API — not everywhere by reflex.
   `max-args` is 7; beyond that, pass a model. Change signatures minimally.
6. **Exceptions.** Catch precise types (`BLE001`, `TRY`); raise the member's own classes
   (`LLMConfigurationError`, `LLMCallError`) with a message that names the value; `TypeError`
   for wrong kinds, `ValueError` for wrong values; never swallow with `except Exception: pass`.
7. **I/O.** `pathlib.Path`; text files with `encoding="utf-8"` and `newline="\n"`; timestamps
   via `utc_now()` (tz-aware, `DTZ`); `subprocess` only in the Tool Manager and in `scripts/`.
8. **Output.** `print` only in CLIs and `scripts/`; libraries use
   `logging.getLogger(__name__)` and log nothing that `thymira.events.redact` would mask.
9. **Secrets and evidence.** Keys come from the environment (`.env`, never code, never
   tests); LiteLLM is the only model gateway; events are redacted before they are written;
   chain-of-thought is never persisted; an LLM output never becomes an authorization.
10. **Docstrings.** Google style on every public module, class and function (`D`): a one-line
    summary ending in a period; `Args`/`Returns`/`Raises` only when they add something the
    signature does not say.
11. **Commands.** `uv run …` and `just …`, never the global `python`; check `uv run python -V`
    (3.13 dev pin) before anything version-sensitive.

## Workflow

1. Place the code in the member that owns the concern (AGENTS.md repository map); confirm the
   import direction (`check-imports`).
2. Write the types first (model, enum, protocol), then the functions, then the class if state
   remains.
3. Run `just lint-fix`, `just typecheck`, `just test`; read every finding — a suppression needs
   `# noqa: CODE  # reason` or `# ty: ignore[rule]  # reason`.

## In this repository

- Style is enforced, not negotiated: ruff `line-length = 100`, `target-version = "py312"`,
  Google docstrings; ty `python-version = "3.12"`, baseline 0. Versions checked: 2026-08.
- Precedents to copy: `thymira.policies.models` (frozen models + pure `matches()`),
  `thymira.events.log` (Protocol + two implementations), `thymira.mira.checks.controls` (pure
  checks over a dataclass context, one registry, one orchestrating function).

## Common mistakes

| Rationalization | Reality |
|---|---|
| "A dict is fine for this record." | A dict has no validation and no frozen guarantee; a `ThymiraModel` costs three lines and is what every other member expects. |
| "`Optional[int]` / `List[str]` is what the docs show." | Python ≥ 3.12 here: `int \| None`, `list[str]`; `UP` rules fail otherwise. |
| "I'll add `*` to every signature for safety." | Keyword-only everywhere is noise; use it where positional order is ambiguous or the parameter is an option. |
| "Catching `Exception` keeps the run alive." | It also hides the cause and breaks MIRA's reconstruction of what happened; catch the type you can handle and let the rest surface. |

**Red flags — stop:** an import inside a function; `Any` added to make ty pass; a class with no
state; `print` in a library module; a key or token in code, a test or an event payload.

## Related skills

- **ruff**, **ty** — the gates. **python-god-classes** — when a class grows. **check-imports** —
  where code may live. **python-testing** — which test to write.
