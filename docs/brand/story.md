# Thymira — the story, the names and how to use them

> **Intelligence must be woven, not merely generated.**

## The myth

Thymira is not a word that exists in a particular mythology. It is our own myth, inspired by
the ancient idea of the weavers of fate.

In the old mythologies, destiny was not a straight line. It was a weave. Every event was a
thread; every decision altered the pattern; and to understand what finally happened, one had
to be able to follow each thread back to its origin.

From that idea Thymira is born: an intelligence built from two forces.

**THY** is the intelligence that acts. It explores, reasons, experiments and builds.

**MIRA** is the intelligence that watches. It questions, traces, challenges and remembers.

THY weaves the solution. MIRA follows every thread. Neither is enough on its own. Together,
they create an intelligence that can not only make decisions, but explain how those decisions
came to exist.

*Every model has a story. Every decision has a thread. Thymira makes both visible.*

## The company story

AI is becoming increasingly capable. But capability alone is not enough.

When an AI system makes a decision, we need to understand not only what it decided, but how it
got there, why it was allowed to proceed, and who was responsible along the way.

That is where Thymira begins.

Inspired by the ancient myth of the threads of fate, Thymira is built around two complementary
intelligences.

**THY — the intelligence that acts.** It orchestrates agents to understand data, build models,
evaluate them and solve problems.

**MIRA — the intelligence that observes.** It traces decisions, challenges assumptions, governs
the process and creates the evidence needed to understand and audit it.

THY weaves. MIRA watches the thread.

Together, they turn a complex chain of autonomous actions into a traceable, explainable and
accountable process.

Because trustworthy AI is not just about producing the right answer. It is about being able to
follow the thread.

## The brand

**THYMIRA** — *Two minds. One accountable intelligence.*

Secondary taglines: *Weaving intelligence into accountability.* · *Follow the thread. Trust the
intelligence.*

## How the names map onto the product

| In the story | In the architecture | In the code |
|---|---|---|
| Thymira | the Agentic Data Science Runtime, its API and clients | namespace `thymira.*`, CLI `thymira`, distributions `thymira-*`, project context `.thymira/` |
| THY, the intelligence that acts | the execution orchestrator (ThyGraph: inspect → plan → execute → summarize) and its sub-agents (data, coding/execution, experiment) | `runtime/thy` → `thymira.thy` |
| MIRA, the intelligence that watches | the audit orchestrator (MiraGraph: audit preparation → deterministic controls → findings) and its sub-agents (risk, methodology, regulation) | `runtime/mira` → `thymira.mira`; the deterministic controls in `thymira.mira.checks` |
| the thread | the hash-chained canonical event log with source credential scrubbing; redacted projections, artifacts with their sha256 and provenance | `thymira.events`, `thymira.state`, `thymira.core` |
| the loom that neither mind controls | the Policy Engine (`PASS / WARNING / REQUIRE_HUMAN_REVIEW / BLOCK`) and the human who approves | `thymira.policies` |

Rules of use:

- **THY** and **MIRA** are written in capitals in prose; `thy` and `mira` in identifiers and
  paths. "Thymira" is the product, the company and the repository.
- Model names are never product names. The early documents used "Sol" and "Opus" as examples
  of frontier model ids; THY and MIRA each run on whatever `THYMIRA_THY_MODEL` and
  `THYMIRA_MIRA_MODEL` point to (ideally different vendors), and their sub-agents on the tier
  the router assigns to the task (`THYMIRA_MODEL_FRONTIER|STANDARD|FAST`). Neither a model
  vendor nor a model name appears in the code or the architecture documents (ADR-0003,
  ADR-0004).
- MIRA never modifies what THY builds; it reads the thread. THY never reads MIRA's findings
  directly; the Policy Engine decides what happens next. The import contracts enforce it
  (`just check-imports`).
