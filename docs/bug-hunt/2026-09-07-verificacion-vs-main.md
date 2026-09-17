# Verificación del bug hunt del 2 de septiembre contra `main` (7 de septiembre)

## Contexto

Este documento **no es un nuevo bug hunt** — es una re-verificación completa de los 25 hallazgos
críticos y altos de `2026-09-02-bug-hunt-thymira` (commit auditado `36b5f7f`) contra el estado real
de `main` hoy, commit `2d38eb98` (14 commits después, incluyendo el trabajo de "harness basics",
aprobación humana por-tool, y sandboxing de `audit_model` de hoy mismo).

**No tiene el mismo rigor que el original** (sin 20 buscadores, sin verificación adversarial en dos
pasadas, sin Runs reales nuevos contra un modelo para cada hallazgo). Es lectura directa de código +
grep dirigido, con la línea exacta citada en cada caso, más lo observado en vivo hoy mismo lanzando
Runs reales contra `examples/credit-risk`.

**Nota**: el informe original ya tenía su propio corte de estado a fecha 4 de septiembre (tres PRs
`#97`, `#100`, `#101` cerraban parcialmente H2/H3/H5/H11/H12). Este documento añade lo verificado
**hoy**, 7 de septiembre, encima de ese corte, y cubre los 25 hallazgos sin dejar ninguno sin mirar.

## Resumen

| | Críticos (12) | Altos (13) |
|---|---|---|
| **Arreglados** | C1, C2, C3, C6, C10 (5) | H5, H6, H9 (3) |
| **Parcial / matizado** | C5, C9, C11 (3) | H3, H8 (2) |
| **Siguen abiertos** | C4, C7, C8, C12 (4) | H1*, H2, H4, H7, H10, H12, H13 (7) |

\* H1: el síntoma que citaba el informe (`thymira status` imprimiendo "Agents 0/Tools 0/Artifacts 0")
está arreglado en la práctica — lo vimos con nuestros propios Runs hoy — pero el método concreto que
señalaba el informe (`RunService._hydrate`) sigue sin tocar esos campos; el arreglo real vive en otro
componente. Ver detalle abajo.

**8 de 25 arreglados del todo, 5 matizados (mecanismo distinto o parcial), 12 siguen abiertos.**

## Críticos

### C1 — Un subagente que falla aborta todo el Run antes de MIRA
**FIJO.** [`graph.py:99-115`](../../runtime/thy/src/thymira/thy/graph.py#L99) — `_route_after_execute`
ya no tiene la rama `state.error is not None → END`; una tarea fallida es evidencia
(`agent_messages`), nunca corta el grafo antes de Summarize/MIRA.

### C2 — La causa de un fallo se descarta dos veces y no queda en ningún sitio
**FIJO.** [`dispatch.py:84-90`](../../runtime/core/src/thymira/core/dispatch.py#L84) —
`InlineDispatcher.resume` tiene ahora el mismo `try/except` que `submit`, citando literalmente
"bug-hunt: the two were asymmetric".

### C3 — Ningún dataset se registra nunca, ocho tools de datos muertas
**FIJO.** `register_dataset` tiene ahora un llamador real en producción:
[`inspect.py:97`](../../runtime/thy/src/thymira/thy/nodes/inspect.py#L97). Verificado en vivo hoy:
nuestro Run de EDA perfiló `german_credit` correctamente.

### C4 — El "Agentic Data Science Runtime" no tiene librerías de data science
**SIGUE ABIERTO.** Sin `pandas` en ningún `pyproject.toml`; `import pandas` falla con
`ModuleNotFoundError` en el venv real. Confirmado en vivo hoy: el agente `coding` escribió
`import pandas as pd` en un Run real nuestro.

### C5 — Una aprobación humana nunca autoriza una tool
**MATIZADO, arreglado por un mecanismo distinto al propuesto.** `Gate._record` sigue devolviendo
solo `PolicyDecision` (el fix literal que pedía el informe no se hizo). En su lugar existe
[`approval_ticket.py`](../../runtime/tools/src/thymira/tools/approval_ticket.py): un
`tool_intent_sha256` por llamada, y `ToolManager.execute` relee el log para encontrar una
aprobación ya registrada para ese ticket exacto. Esto sí resuelve el síntoma original, pero
introduce un problema nuevo — ver "Hallazgo nuevo" más abajo.

### C6 — Una aprobación anónima se registra como el actor `system` autenticado
**FIJO.** [`approval.py:72-81`](../../adapters/cli/src/thymira/cli/commands/approval.py#L72) —
`_default_actor()` usa `THYMIRA_ACTOR` o el usuario del SO. Además, en la API,
[`runs.py:363-376`](../../apps/api/src/thymira/api/routes/runs.py#L363) rechaza con 401 una
llamada sin ningún actor identificable (cita literalmente "bug-hunt C6").

### C7 — Aprobar una decisión libera otra: `/plan` + `/approve` salta el gate de auditoría
**FIJO.** [`runs.py:266-306`](../../runtime/core/src/thymira/core/runs.py#L266) —
`resolve_approval` ahora exige que el Run esté realmente `waiting`/`approval`
(`RunNotAwaitingApprovalError` si no), y resuelve `self._parked_decision(run_id)` — la decisión
exacta en la que el Run aparcó, leída de su propia transición `wait_for_approval` — cayendo a "la
más reciente" solo si no hay registro de aparcado. Esto es justo la recomendación del informe
("`RunController` persiste el id de la decisión en la que aparcó y `resolve_approval` solo acepta
ese id").

*Corrección sobre mi borrador anterior: había marcado esto como "no reverificado"; con el código
delante, sí está arreglado.*

### C8 — La política de permisos no puede denegar nada
**SIGUE ABIERTO.** Las tools revisadas (`audit_model.py`, `compare_models.py`, `data_analysis.py`,
`files.py`, `git.py`, `inspect_model.py`, `mlflow_tools.py`, `query_mlflow.py`) siguen declarando
`external_effects=()` sin excepción.

### C9 — `run_python` no está confinado
**MATIZADO.** El modo por defecto de `run_python` sigue siendo
[`SandboxMode.WORKSPACE_WRITE`](../../runtime/tools/src/thymira/tools/builtins/run_python.py#L60)
(subprocess local) — el problema original persiste para la ejecución normal. Pero hoy mismo se
mergeó `2d38eb98` ("sandbox model audit execution"), que mueve `audit_model` a un sandbox
inyectado, y los commits de hace unas horas (#124, #125) añadieron "refuse unsupported local
sandbox modes" y "execute Python tools inside containers". El confinamiento existe y se está
desplegando para las operaciones de más riesgo, pero `run_python` genérico sigue sin contenedor
por defecto.

### C10 — La redacción destruye los floats del log autoritativo
**FIJO.** [`redaction.py:307-317`](../../packages/events/src/thymira/events/redaction.py#L307) —
`_card_qualifies` exige separadores de tarjeta o checksum de Luhn antes de redactar; un float64 de
16 dígitos seguidos ya no cae en el patrón `CARD`.

### C11 — MIRA audita a ciegas y su veredicto es una constante
**MATIZADO.** Hoy hay más sitios que marcan eventos `MODEL_VISIBLE` además de `agent.message`:
[`decisions.py:324,348`](../../runtime/core/src/thymira/core/decisions.py#L324) (decisiones de
política), `compaction.py:183`, `delegation.py:160`. Además `runtime_context.py` (nuevo) inyecta un
resumen de datasets/artefactos activos en cada paso. El reclamo original de "6% del log, solo
`agent.message`" ya no es exacto. No verifiqué si las tool calls (argumentos/resultados) en sí ya
llegan como eventos visibles o siguen fuera — matizado, no cerrado del todo.

### C12 — Un crash dentro de THY deja el Run irrecuperable
**SIGUE ABIERTO, confirmado con precisión.**
[`checkpoint.py:104-133`](../../runtime/core/src/thymira/core/graph/checkpoint.py#L104) — `put`
ahora *lee* `checkpoint_ns` e incluye en el config devuelto, pero la escritura real sigue siendo
`self._repository.put(thread_id, ...)` — sin `checkpoint_ns` en la clave. Un `ThyGraph` anidado con
el mismo `thread_id` que la composición exterior puede seguir pisando su checkpoint.

## Altos

### H1 — El registro del Run nunca se pliega del log
**SÍNTOMA ARREGLADO, mecanismo original sin tocar.**
[`runs.py:455-483`](../../runtime/core/src/thymira/core/runs.py#L455) — `RunService._hydrate`
sigue rellenando solo `status`, `started_at`, `completed_at`, `error`; **no** escribe `agent_ids`,
`tool_call_ids`, `artifact_ids`, `finding_ids` ni `final_decision`, exactamente como decía el
informe. Pero el síntoma concreto que citaban ("`thymira status` imprime Agents 0/Tools 0/Artifacts
0") no lo reproduje hoy: nuestros Runs reales mostraron contadores correctos (`Agents 7, Tools 2,
Artifacts 11`, etc.) y un `run.json` final con `agent_ids`/`artifact_ids`/`task_ids` bien poblados.
Conclusión: el arreglo real vive en otro componente (probablemente el `LocalRunStore`/
`RunController` hacia el que AGENTS.md dice que se está migrando `RunService`), no en el método que
señalaba el informe — la vista legada sigue rota, la vista que de verdad se usa hoy no.

### H2 — No hay workspace por Run
**SIGUE ABIERTO.** [`adapters.py:348`](../../runtime/core/src/thymira/core/graph/adapters.py#L348)
— `workspace=self.project_dir or Path()`: el mismo directorio de proyecto para todos los Runs.

### H3 — El humano aprobaba a ciegas: coste 0,00 USD siempre
**PARCIAL, más avanzado que en el corte del 4 de septiembre.** `deps.gate.check_budget(...)` tiene
ahora tres llamadores reales en
[`adapters.py:1077,1094,1144`](../../runtime/core/src/thymira/core/graph/adapters.py#L1077)
(el informe original decía "sin llamadores"). No encontré evidencia de un techo de gasto máximo
configurado (`UsageLimits` con un límite duro) — el ledger existe y se consulta, pero no confirmé
que pueda parar un Run por exceso de coste.

### H4 — El contexto que el runtime conoce nunca llega a los agentes
**SIGUE ABIERTO.** Cero referencias a `project_context` en `plan.py` ni en `prompts.py`.
`.thymira/context.md` se carga en `ThyState.project_context` (confirmado hoy, en Inspect) pero
sigue sin leerse en ningún prompt real enviado al modelo.

### H5 — La procedencia del prompt nunca se registraba
**FIJO.** Módulo dedicado
[`prompt_provenance.py`](../../runtime/agents/src/thymira/agents/prompt_provenance.py) con
`record_prompt()`, enchufado en `model_binding.py:430`. Verificado en vivo hoy: cada llamada real
generó un artefacto `prompts/agent-*-artifact_*.txt`.

### H6 — Evidencia fabricada por diseño
**FIJO.** [`data_analysis.py:134,192`](../../runtime/tools/src/thymira/tools/builtins/data_analysis.py#L134)
— `profile_dataset` ahora calcula `null_count()` con Polars sobre el dataset completo registrado,
no contando a ojo sobre un `read_file` truncado a 64 KiB. Verificado en vivo hoy: es la misma tool
que nos dio el perfil real de `german_credit`.

### H7 — THY se cree al modelo y nunca re-planifica
**SIGUE ABIERTO.** [`execute.py:466`](../../runtime/thy/src/thymira/thy/nodes/execute.py#L466) —
una tarea es `COMPLETED` si `outcome.status is COMPLETED and outcome.summary is not None`; no
encontré ninguna comprobación cruzada contra `exit_code` o la lista de `artifacts` — el caso que
citaba el informe (`exit_code: 1`, `artifacts: []`, igualmente `COMPLETED`) parece seguir siendo
posible.

### H8 — 14 de 25 tools son inalcanzables
**PARCIAL.** `profile_dataset` (agente `data`), `run_statistics` (agente `statistics-agent`) y
`run_experiment` (agente `experiment`) son alcanzables ahora — antes no lo eran; verificado en vivo
para `profile_dataset`. Pero revisando `experiment.py` y `ml.py` completos, **`audit_model`,
`compare_models` e `inspect_model` siguen sin estar en ningún `tool_allowlist`** — irónico, dado que
`audit_model` fue justo la tool que se blindó de seguridad hoy mismo (commit `2d38eb98`) y sigue
sin que ningún agente pueda llamarla. `query_sql`, `query_mlflow` y la mayoría de `git_*` tampoco
aparecen en ningún allowlist revisado.

### H9 — Hay dos MIRAs y la provenance apunta a la que no corre
**FIJO.** `runtime/mira/src/thymira/mira/graph.py` (la vieja `MiraGraph`, 366 líneas) ya no existe
— la borró el commit `ff76148` ("Dev mira #110"). Solo queda `MiraSubgraph` en
[`adapters.py:472`](../../runtime/core/src/thymira/core/graph/adapters.py#L472) como implementación
real y única.

### H10 — Sin resiliencia en el gateway y un puente que reimplementa PydanticAI
**SIGUE ABIERTO.** Cero referencias a `num_retries`, `timeout` o `ModelSettings` en
`litellm_provider.py`.

### H11 — API: ejecución inline en `def` síncronos
**No reverificado con el mismo detalle que el resto** (requiere probar concurrencia real, no solo
leer código) — se mantiene el `PARCIAL` que ya fijaba el corte del 4 de septiembre (`#97` separó
los timeouts de connect/read; threadpool, lock, journal, SSE y audit view seguían igual en esa
fecha). Nada de lo tocado hoy apunta a este mecanismo.

### H12 — La suite no cruza ningún límite de proceso
**SIGUE ABIERTO.** `test_api_server_integration.py` — pese al nombre prometedor — usa
`fastapi.testclient.TestClient`, que invoca la app ASGI en el mismo proceso, sin socket real. El
único sitio que de verdad arranca `thymira-api` como proceso aparte y le habla por HTTP real sigue
siendo `scripts/real_e2e_smoke.py`, explícitamente fuera de `just test`/`just check` por su propio
docstring.

### H13 — Documentación que describe otro sistema
**SIGUE ABIERTO.** La CLI tiene hoy unos 13 comandos reales (`run`, `status`, `answer`, `runs`,
`resume`, `events`, `approve`, `reject`, `audit`, `experiment`, `mlflow`, `plan`...) y la API 9
módulos de rutas — AGENTS.md sigue describiendo la CLI como "thin API client; run/status
implemented over HTTP, audit/approve/reject next", una fotografía muy anterior a lo que hay
realmente construido hoy.

## Hallazgo nuevo, no estaba en el informe original

**El sistema de ticket de aprobación de un solo uso no converge para tools de código libre.**
Verificado en vivo hoy con un Run real, aprobando cada tool call a mano: el agente `coding` propuso
5 llamadas `run_python` distintas en sucesión (comprobar dtypes, listar directorio, buscar el
fichero por nombre...) sin repetir nunca el mismo código, así que ninguna aprobación coincidía con
la siguiente propuesta. Solo convergió una vez, con `profile_dataset`/`read_file`, porque sus
argumentos son simples y el modelo los reprodujo por casualidad. Causa raíz exacta:
[`runner.py:308-312`](../../runtime/agents/src/thymira/agents/runner.py#L308) llama
`agent.run_sync(assembled.user, ...)` sin `message_history` ni `deferred_tool_results` — cuando un
paso se difiere (`DeferredToolRequests`), no se persiste nada de la conversación, así que "reanudar"
es literalmente empezar de cero. Es exactamente el mecanismo que el informe de "Foco 2/3" del 4 de
septiembre ya identificó y aplazó a propósito como rediseño de varios días (`interrupt()`-based
fine-grained pause/resume) — confirmación en vivo de un riesgo ya escrito, no una sorpresa.

## Qué queda por hacer, priorizado

1. **C4** — declarar pandas/matplotlib (¿y mlflow?) en el miembro `tools`. Bloquea cualquier
   análisis real; lo vimos fallar en directo hoy.
2. **El hallazgo nuevo** (ticket de aprobación no converge) — bloquea cualquier tarea de código bajo
   revisión por-tool, que es justo el modo que activa MIRA cuando su confianza es baja.
3. **C12** — incluir `checkpoint_ns` en la clave de almacenamiento, no solo leerlo.
4. **C8** — metadatos `external_effects` honestos en las tools destructivas.
5. **H8** — dar a algún agente acceso a `audit_model`/`compare_models`/`inspect_model` — hoy son
   inalcanzables pese a que `audit_model` se blindó de seguridad esta misma tarde.
6. **H7** — validar `exit_code`/`artifacts` antes de marcar una tarea `COMPLETED`.
7. **H4** — inyectar `project_context` en el prompt de Plan.
8. **C9** — extender el confinamiento por contenedor de `audit_model` a `run_python` en general.
9. **H10** — pasar `num_retries`/`timeout` a `LiteLLMProvider`.
10. **H13** — actualizar AGENTS.md/README para reflejar la CLI y la API reales de hoy.
11. Reproducir H11 con concurrencia real (41 Runs simultáneos) para confirmar si sigue igual que el
    4 de septiembre.
