# Foco 2 + Foco 3 — task failure as evidence, and approval that actually authorizes

## Context

`scripts/real_e2e_smoke.py` (this session, branch `feat/real-e2e-acceptance-test`) reproduced the
same failure twice, byte-for-byte, against a real model: a tool call gets escalated to
`REQUIRE_HUMAN_REVIEW`, is immediately denied instead of paused, the `data` agent exhausts
`max_turns`, THY raises `ThyExecutionError`, and the risk-interview `answer` endpoint — which runs
THY synchronously and has no exception handling — turns that into a raw HTTP 500. The Run is left
with `status: CREATED`, `final_decision: PASS`, no `run.failed` event, and `thymira resume` returns
409. Two commits landed since the bug-hunt report (`af74dfe` "Dev mira" #90, `5bb086a` #107) and
changed a lot — C10 is cleanly fixed, H1 is partially fixed, MIRA's evidence and dual-graph issues
are largely addressed, C6's actor-authenticity bug is mostly fixed, and C7's "two conflicting
approval surfaces" claim no longer matches the code (they're now cleanly separated: one transitions
the Run, one only records evidence). What direct code reading + the live run confirm is **still**
broken, verified against current `HEAD`, not the report's stale snapshot:

- **C1**: [`graph.py:93-94`](runtime/thy/src/thymira/thy/graph.py:93) still routes any `state.error`
  straight to `END`, skipping Summarize and MIRA.
- **Dispatcher asymmetry** (the actual zombie-run mechanism): `InlineDispatcher.submit` catches
  `RuntimeError` and converts it to a recorded `RUN_FAILED` (by design, per its own docstring);
  `InlineDispatcher.resume` has no `try/except` at all, and all three of its callers in
  `runs.py` (`resume`, `answer_risk_interview`, `resolve_approval`) call it unguarded.
- **C5's precise mechanism**: [`manager.py:138`](runtime/tools/src/thymira/tools/manager.py:138)
  calls `allows_execution(decision)` with no `approval`; `Gate._record` *does* consult a
  synchronous approver when one is injected and appends `human.approval`, but returns the
  original, still-`REQUIRE_HUMAN_REVIEW` decision — the approval it just obtained never reaches
  the caller that could use it.

The fix is not "route this one endpoint's exception" or "pass a value at this one call site" —
it's two coherent, small mechanisms: a task failure must remain evidence all the way to MIRA
(reused everywhere `_route_after_execute` fires), and the dispatcher's crash-to-`FAILED` safety net
must be symmetric across every path that can invoke the graph (reused everywhere `resume()` fires),
not just the one PR #105 happened to touch.

**Explicitly out of scope, with reasons, not silently dropped:**
- A fine-grained, `interrupt()`-based pause exactly at a denied tool call, resumed with the human's
  answer threaded back into that specific call. LangGraph's `interrupt()` re-executes a node
  function from the top on resume; `Execute` runs an entire task loop (with real side effects —
  event writes, usage charges) inside one node function, so this needs the loop restructured into
  idempotent, resumable steps to avoid double-charging or duplicating events on resume. That's a
  real, multi-day redesign (the bug-hunt report's own estimate), and rushing it risks the one thing
  proven to work today: the hash-chained event log's integrity. Foco 2 makes the existing,
  already-working park/resume (`Gate.review_findings` → `RunController` `WAITING`/`approval` →
  `resolve_approval()`) *reachable* for a Run that hit a tool denial — MIRA's existing
  `a7_approvals_resolved` control already fails when a `human.approval_requested` has no matching
  `human.approval`, which is exactly what a silent denial produces — so a human gets a real,
  auditable review without new pause/resume machinery.
- Loosening the risk-classification fail-safe (`engine.py:167-171`, `_uncertainty`) so a local,
  read-only tool isn't blocked by an incomplete risk profile. This is real (found live: the `data`
  agent can't even profile the dataset to *complete* the risk profile), but every tool currently
  declares `external_effects=()` regardless of what it actually does (bug-hunt C8) — exempting
  "local" tools from the fail-safe before that metadata is honest would exempt genuinely dangerous
  ones too. Fixing this safely needs C8 first; noted as the natural next task, not patched around.

## Foco 2 — a task failure is evidence, not a crash

**`runtime/thy/src/thymira/thy/nodes/execute.py`** — `_finish`: always call `state.advance()`;
stop setting `updates["error"]` for a task failure (the failure is already fully recorded in
`agent_messages`/`completed` with `TaskStatus.FAILED`, and on the event log via
`agent.completed`/`agent.message`). Drop the now-unused `any_failed` threading through
`_run_sequential`/`_run_parallel`/`_finish` — nothing else reads it once it no longer gates
`error`. `ThyState.error` becomes reserved exclusively for a genuine pre-Execute halt (a
Gate-rejected plan, set independently in `plan.py`, untouched).

**`runtime/thy/src/thymira/thy/graph.py`** — `_route_after_execute`: drop the
`state.error is not None: return END` branch. Keep the `state.rework` check, otherwise always
`"summarize"`. Update the function's and module's docstrings (they currently describe the old
routing). `Summarize` already degrades gracefully with zero experiments
([`summarize.py:70-73`](runtime/thy/src/thymira/thy/nodes/summarize.py:70)), so it needs no change.

**`runtime/core/src/thymira/core/dispatch.py`** — `InlineDispatcher.resume`: wrap
`graph.get_state`/`graph.update_state`/`graph.invoke` in the same
`try: ... except (OSError, RuntimeError, ValueError) as exc: raise ExecutionDispatchError(...) from exc`
that `submit` already has, so a resume-time `ThyExecutionError` (a `RuntimeError` subclass) is
caught here instead of propagating raw.

**`runtime/core/src/thymira/core/runs.py`** — `resume`, `answer_risk_interview`, `resolve_approval`
(the three `self._dispatcher.resume(run_id)` call sites): wrap each the same way `create_run`
already wraps `submit` — catch `ExecutionDispatchError`, and if the Run isn't already
`RunCondition.TERMINAL`, call `self.fail(run_id, actor=Actor.system(), error=str(exc))`, then
return `self.get_run(run_id)` instead of letting the exception reach the API layer as a raw 500.

**`runtime/core/src/thymira/core/graph/adapters.py`** — update `ThySubgraph`'s docstring ("Error
path") to reflect that `ThyOutput.error` now means a structural pre-Execute halt only.

**Tests**: rewrite `test_a_failing_task_is_recorded_failed_and_routes_away_from_summarize` in
`tests/thymira/test_thy_execute.py` to assert the opposite — phase reaches `ThyPhase.SUMMARIZE`,
`final_state.error is None`, the failure is still visible in `agent_messages`. Add a test mirroring
`test_run_service_submits_to_injected_dispatcher`'s failure case in `test_core_dispatch.py` for
`resume()` (a resume that raises is caught and re-raised as `ExecutionDispatchError`). Add three
small tests in `test_core_run_listing.py` (or wherever `RunService.resume`/`answer_risk_interview`/
`resolve_approval` are already tested) confirming each records `RUN_FAILED` instead of propagating
when the dispatcher raises. Confirm `test_thy_subgraph_raises_when_thy_reports_an_error` and
`test_production_factory_thy_failure_leaves_a_consistent_failed_run` are unaffected (both exercise
the plan-rejection path, not Execute) — read them again after the change to be sure.

## Foco 3 — approval that actually authorizes; a named actor

**`runtime/policies/src/thymira/policies/gate.py`** — `_record` (and `check_capability`'s use of
it): when a synchronous approver is consulted and answers, return the resolved `Approval` alongside
the `PolicyDecision` (or a small typed pair) instead of discarding it, so `ToolManager.execute` can
pass it to `allows_execution`.

**`runtime/tools/src/thymira/tools/manager.py`** — `execute`: call
`allows_execution(decision, approval)` using whatever `Gate.check_capability` now returns, so a
synchronous approver's "yes" actually authorizes the call instead of being silently discarded.

**Actor integrity (C6 residual)**: `adapters/cli/src/thymira/cli/commands/approval.py` — default
`--actor` to `THYMIRA_ACTOR` env var, then the OS user, when not passed explicitly, instead of
sending nothing. `apps/api/src/thymira/api/principal.py` / the `approve`/`reject` routes in
`runs.py`: require a named actor (401) rather than silently resolving a fully anonymous call to
`Actor.system()` — an approval is exactly the place a silent default is wrong.

**C7 verification**: read `resolve_approval` (`runs.py`) and the governance-route ApprovalService
path together once more against a concrete scenario (approve via `/runs/{id}/approvals/{decision}/
approve` then via `/runs/{id}/approve`) to confirm they can no longer race/conflict as the report
described; add a regression test pinning the current (correct) separation if none exists yet.

**Tests**: unit test for `Gate`/`allows_execution` threading the approval end to end; CLI test for
the actor default chain; API test asserting 401 on a fully anonymous `/approve`.

## Verification

- `uv run pytest -m "not slow"` (fast lane) plus the specific new/changed test files, showing
  `N passed`.
- `just check-imports` (no new cross-layer imports expected) and `just lint`.
- Optional, costs real API budget: re-run `uv run python scripts/real_e2e_smoke.py` — with Foco 2
  alone it should no longer zombie (the Run should reach a recorded `REQUIRE_HUMAN_REVIEW`/`BLOCK`/
  `FAILED` terminal state visible to `thymira status`/`thymira audit`, even though the risk-interview
  catch-22 means it likely still won't reach a clean `COMPLETED` — that needs the explicitly
  out-of-scope C8 + fail-safe work).

## Estado de tareas

Añadido tras la implementación — el resto del documento arriba es el plan original, sin cambios.

### Foco 2 — completado

- [x] `execute.py::_finish` — siempre avanza de fase; un fallo ya no fija `state.error`.
- [x] `graph.py::_route_after_execute` — eliminada la rama `state.error → END`; docstrings al día.
- [x] `dispatch.py::InlineDispatcher.resume` — mismo `try/except` que `submit`.
- [x] `runs.py` — los tres call sites de `dispatcher.resume(...)` (`resume`, `answer_risk_interview`,
      `resolve_approval`) capturan `ExecutionDispatchError` y llaman a `self.fail(...)`.
- [x] `adapters.py` — docstring de `ThySubgraph` actualizado.
- [x] Tests: `test_thy_execute.py` (la tarea fallida llega a Summarize), `test_core_dispatch.py`
      (el fallo en `resume()` se captura), `test_thy_e2e.py` ajustado.
- [x] **Verificado en vivo, dos veces, con modelo real** (`scripts/real_e2e_smoke.py`): tras el
      fallo del agente `data`, MIRA ejecuta ahora su auditoría real completa (8 agentes); un fallo
      posterior dentro de esa auditoría termina en un `run.failed` limpio y evidenciado, no en un
      Run zombi con un 500 crudo.

### Foco 3 — parcial

- [x] C6 (integridad del actor): `/approve` y `/reject` devuelven 401 ante una llamada totalmente
      anónima (`test_api_runs.py::test_human_decision_refuses_a_fully_anonymous_call`); la CLI
      `thymira approve`/`reject` usa `THYMIRA_ACTOR` o el usuario del SO cuando no se pasa `--actor`
      (`test_cli_approval.py`), en vez de no enviar nada.
- [ ] **C5, el mecanismo central — no implementado.** `runtime/policies/src/thymira/policies/gate.py`
      y `runtime/tools/src/thymira/tools/manager.py` siguen sin tocar en esta rama. Un aprobador
      síncrono que dice "sí" todavía no llega nunca a `allows_execution` en la puerta de la tool —
      sigue exactamente la brecha que describe el contexto de arriba.
- [ ] C7 (verificación explícita de las dos superficies de aprobación) — no se hizo como paso
      propio; no hay test nuevo que fije esa separación.

### Fuera de alcance — sigue así, sin cambios

- El park-and-resume de grano fino con `interrupt()` para una tool call concreta.
- Relajar el fail-safe de clasificación de riesgo para tools locales de solo lectura (bloqueado por
  C8: metadatos `external_effects` deshonestos en las 25 tools).

**Siguiente paso natural**: cerrar C5 (Gate → Approval → `allows_execution`) es ahora el hueco más
grande entre "el Run ya no miente sobre lo que pasó" y "una aprobación humana real autoriza algo".
