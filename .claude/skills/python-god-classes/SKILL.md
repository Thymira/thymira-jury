---
name: python-god-classes
description: Use when a class or module is hard to summarise in one sentence, has many methods or 300+ lines, mixes I/O with transformation, has a vague name (Manager, Handler, Service, Processor, Utils), keeps growing with every feature, or when asked to split, decompose, extract or refactor a large class — and before adding one more method to it.
metadata:
  version: "1.0.0"
---

# Splitting Python god classes

## Overview

A large class is a signal to extract a seam, not a home for one more method. The default shape
here is **functional core, imperative shell**: pure functions for transformations and checks,
a thin function or class at the edge for I/O and state. Measure with the detector, pin
behaviour with tests, cut along the cohesion seam — one extraction per commit, re-measuring
after. Cohesion, not raw size, decides whether a class is a god class and where it splits.

## When to use

- **Before adding a method, section, branch or `elif` to an existing large class** — the moment
  this discipline exists for.
- The detector flags the class, or a warning sign matches even under the thresholds: a vague
  name (`Manager`, `Handler`, `Service`, `Processor`, `Utils`); methods that never read `self`;
  a method that both reads a source and decides; testing one method needs more than two fakes.
- You are asked to split, decompose, extract or refactor a big class.
- Not for: a cohesive `@dataclass`/`NamedTuple`/`Enum`/pydantic model, a Protocol, a visitor of
  one-line delegators, or any class neither the detector nor a warning sign flags.

## Rules

1. **Transformations are functions** — hashing, matching, checking, rendering, feature
   computation: module-level, taking and returning values (`redact`, `ActionRule.matches`, the
   MIRA controls). They test directly, import anywhere and compose.
2. **I/O lives at the edges** — one orchestrating function or thin class reads the log, the
   store or a model and calls the pure core (`audit_run`, `Gate`, `LocalArtifactStore`). A
   function that loads *and* decides is two functions.
3. **A class earns its place with state or a lifecycle** — a store, a log, a provider, a graph,
   a model with `fit`/`predict`, a validated configuration. A method that does not use `self`
   is a function in disguise.
4. **Measure before and after**; never quote a size from memory.

## Workflow

The baseline failure is skipping 1, 3 and 6.

1. **Measure first**:
   `uv run python .agents/skills/python-god-classes/scripts/detect_god_classes.py <file>`
2. **Gate on the flag.** Flagged (or a warning sign) and you were about to add behaviour →
   **STOP**: the new behaviour goes in a new single-responsibility function or class, or
   extract the seam first, then add. Not flagged → change it normally.
3. **Pin behaviour** with characterization tests that pass now; models are faked with
   `thymira.agents.llm.ScriptedProvider`.
4. **Cut along the seam the numbers show.** Methods ignoring `self` → Extract Function
   (Recipe 0). Low cohesion → Extract Class along attribute clusters (Recipe 1). One long
   procedure, cohesion already high → Extract Method (Recipe 2). Recipes and precedents:
   `references/refactoring-playbook.md`.
5. **One extraction per commit**, full suite green after each. No big-bang rewrite.
6. **Re-measure.** The flagged aggregate goes **down** (a refactor) or **not up** (when product
   forced new behaviour). If it rose, you piled on — revert and extract.

## Quick reference

| Command | Purpose |
|---|---|
| `just god-classes` | Detector over the workspace members (`packages runtime apps adapters`) |
| `detect_god_classes.py <path> --json` / `--fail-over` / `--top 5` | Machine output / exit 1 if flagged / worst five |

Default thresholds (tunable, not laws): 15 methods, 300 class lines, 600 module lines, 25
top-level defs, 0.30 cohesion — `--max-*` / `--min-cohesion` to override.

## In this repository

- **The detector is the source of truth; run it, do not quote it here.** `just god-classes`
  reports the current flags across `packages runtime apps adapters` — this page names no counts on
  purpose (rule 4: never quote a size from memory), because a number written here is stale by the
  next extraction. The persistence family (`LocalArtifactStore` and the `Local*` stores and
  repositories) recurs in that report: manifest bookkeeping and the typed `save_*`/persistence
  helpers are separate clusters, so a new artifact kind or record type belongs in a serializer
  collaborator, not one more method on the flagged class. A thin persistence or provider seam can
  trip cohesion without being a size problem — so measure with the detector before extracting one.
- Functional-core precedent: `thymira.mira.checks.controls` — eleven pure `a*_…(ctx)` functions,
  one registry, one `audit_run` shell; a new control is a function and a tuple entry, never a
  method.
- Thesis precedent (tag `thesis/mads-v0.1.0`, commit `166fbca`): eight injectable coordinators
  extracted from an orchestrator whose `run_case()` ran over a thousand lines, with the suite
  green, one seam per commit — the
  shape ThyGraph's nodes should keep from day one.

## Common mistakes

| Rationalization | Reality |
|---|---|
| "The store already has `save_json` / `save_text`; a new `save_model` is just being consistent." | Consistency with a flagged class grows the flagged class. New behaviour goes in a new unit. |
| "A `RunService` class will keep the run logic together." | `Service`/`Manager` is where unrelated methods accumulate. Start with functions over the `Run` model; add a class when state appears. |
| "It's ~250 lines, I can see it's fine — no need to run a tool." | Eyeballing is not measuring; the detector flags on methods and cohesion, not lines. |
| "Let me rewrite the whole class cleanly instead." | Big-bang rewrites silently drop behaviour. One seam, one commit, suite green, re-measure. |

**Red flags — STOP:** adding to a flagged class; no characterization test on the code you are
changing; a size quoted from memory; two extractions in one commit; no re-measure step; a new
class named `…Manager`, `…Handler`, `…Service`, `…Processor` or `…Utils`.

## Related skills

- **REQUIRED SUB-SKILL:** python-testing-unit — characterization tests before any extraction.
- python-general — functions vs classes, the baseline design rules. check-imports — after
  extracting a module, confirm the layering still holds.
