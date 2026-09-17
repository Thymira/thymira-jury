# Refactoring playbook: extracting god classes

Read this once you have *measured* (`.agents/skills/python-god-classes/scripts/detect_god_classes.py`)
and *pinned behaviour*
(characterization tests). It gives the extraction recipes and the repository's own precedent.

## The repo precedent: commit `166fbca`

`AgenticOrchestrator.run_case()` was a single method over a thousand lines that concentrated
risk classification, action selection, the Gate, execution, phase transition, tracing and cost
accounting. None of it could be tested or replaced in parts without standing up the whole
LangGraph.

The fix (`refactor(orchestrator): divide AgenticOrchestrator en ocho componentes inyectables`)
extracted eight injectable, independently testable components — `RunLifecycle`,
`RiskCoordinator`, `ActionAvailabilityService`, `ActionSelector`, `AuthorizationCoordinator`,
`ExecutionCoordinator`, `StateTransitionService`, `PhaseCoordinator` — while keeping **one**
orchestrator, **one** LangGraph flow and the same selector-proposes / Gate-authorizes /
execution-executes / state-updates separation. The full suite stayed green (460 tests, plus 13
new per-component unit tests). Today the detector reports `AgenticOrchestrator` with only 2
methods and cohesion 1.00: it became a thin delegator. That is the target shape.

## Current findings (detector, workspace members)

Regenerate with: `just god-classes` (= `detect_god_classes.py packages runtime apps adapters`).

| Class | File | Flags |
|---|---|---|
| `LocalArtifactStore` | `runtime/state/src/thymira/state/local_store.py:111` | 327 lines > 300; 20 methods > 15; cohesion 0.16 < 0.30 |

Six more classes trip cohesion only (`LiteLLMProvider`, `LocalRunStore`, `RunService`,
`LocalRecordRepository`, `LocalRunRepository`, `LocalSessionRepository`) — thin
persistence/provider seams, not size flags; regenerate the full list with `just god-classes`.

Read the flags: 20 methods with cohesion 0.16 is a *data seam* — the manifest bookkeeping
(`_load_manifest`, `_write_manifest`, `verify`) and the typed `save_*` helpers touch disjoint
attributes. Extract Class (a `Manifest` collaborator, or a serializer per artifact kind), not
Extract Method. The thesis findings this section used to list (a 1,336-line report skill, a
371-line risk classifier) live at tag `thesis/mads-v0.1.0`.

## Recipe 0 — Functional core, imperative shell (methods that ignore `self`, or I/O mixed with logic)

The default shape for checks, transformations and renderers: pure functions in the core, one
thin function or class at the edge doing the reads and writes. Most "god classes" in
read → transform → write code are a bundle of functions wearing a class.

```python
# before: one class loads, decides and writes
class AuditService:
    def __init__(self, store, log): ...
    def load_events(self): ...            # I/O
    def check_chain(self, events): ...    # pure — never touches self
    def check_authorization(self, events): ...   # pure
    def write_report(self, results): ...  # I/O

# after: pure core + one shell (the shape of thymira.mira.checks)
def a1_chain_integrity(ctx: AuditContext) -> tuple[ControlStatus, str]: ...
def a3_authorization_before_tool(ctx: AuditContext) -> tuple[ControlStatus, str]: ...

CONTROLS = (Control("A1", ..., a1_chain_integrity), Control("A3", ..., a3_authorization_before_tool))

def audit_run(ctx: AuditContext) -> AuditReport:      # the shell: iterate, collect, render
    ...
```

Signals: methods that never read `self`; a vague name (`Service`, `Manager`, `Processor`); a
method that both fetches and decides. Keep a class only where state or a lifecycle remains
(`LocalArtifactStore`, `JsonlEventLog`, `LiteLLMProvider`). Tests become direct calls on the
functions; the shell gets one integration-style test on persisted evidence.

## Recipe 1 — Extract Class (low cohesion: methods touch disjoint attribute clusters)

The detector's cohesion score points straight at the seam. Group the methods by the attributes
they share; each cluster becomes a class the god class now holds and delegates to.

```python
# before: one class, two unrelated attribute clusters
class Report:
    def _fairness_section(self): ... # touches self.evaluation, self.subgroups
    def _model_section(self):    ... # touches self.evaluation, self.subgroups
    def _cost_section(self):     ... # touches self.events, self.costs

# after: the god class holds collaborators and delegates
class FairnessSection:   # owns evaluation/subgroups
    def render(self): ...
class CostSection:       # owns events/costs
    def render(self): ...

class Report:
    def render(self):
        return [*self._fairness.render(), *self._cost.render()]
```

## Recipe 2 — Extract Method (one long procedure, cohesion already high)

A class flagged only on lines with 1–2 methods is not a god class of state; it is one long
function. Split the procedure into named steps in the same class first; extract a class only if
the steps then cluster.

```python
# before
def execute(self):
    ...              # 200 lines: validate, transform, score, format

# after
def execute(self):
    data = self._validate()
    scored = self._score(self._transform(data))
    return self._format(scored)
```

## Recipe 3 — Introduce Parameter Object (methods pass the same argument bundle)

When extraction stalls because every method needs the same five arguments, bundle them.

```python
# before
def _model_section(self, features, model, evaluation, task_type, thresholds): ...
# after
@dataclass(frozen=True)
class ReportContext:
    features: list[str]; model: Model; evaluation: dict; task_type: str; thresholds: Thresholds

def _model_section(self, ctx: ReportContext): ...
```

## Recipe 4 — Replace Conditional with Strategy (a shape/type switch repeated across methods)

When the same `if task_type == ... elif ...` (or a branch on the *shape* of a dict) recurs, make
each branch a class with a shared interface and select once.

```python
# before: the same three-way branch in every section method
if "r2" in metrics:        ... # regression
elif "confusion_matrix" in metrics: ... # multiclass
else:                      ... # binary

# after: one selection, polymorphic rendering
strategy = choose_metric_strategy(metrics)   # Regression | Multiclass | Binary
strategy.gap(groups)
```

## Discipline (do not skip)

- **Characterization tests first.** Pin the current observable output before you move a line.
  For anything that talks to a model use `thymira.agents.llm.ScriptedProvider`, not a real LLM.
- **One extraction per commit**, full suite green after each. No big-bang rewrite of the class.
- **Re-measure after each commit.** Rerun the detector; the flagged aggregate must go down (a
  refactor) or at least not up (when you had to add behaviour). If it went up, you piled on
  instead of extracting.
- Thresholds are documented defaults to tune, not laws. A visitor of one-line delegators is
  allowed to be wide; judge by cohesion and responsibility, not raw count.
